# -*- coding: utf-8 -*-
"""封面图：调用本机 ComfyUI 生成，或者手工传一张上来。

工作流来自项目根目录的 moss_workflow_api.json，节点号在下面固定几条：
4 正向提示词、5 负向提示词、6 空 latent（尺寸）、7 KSampler（种子）、30 保存。
换工作流时这五个节点号大概率要跟着改。

所有路径都从 BASE_DIR 推出来，不写死绝对路径：换机器、换数据目录都能跑。
"""
import json
import os
import random
import tempfile
import threading
import time
import uuid

import requests

from .config import BASE_DIR, DATA_DIR

COVERS_DIR = os.path.join(DATA_DIR, "covers")
WORKFLOW_PATH = os.path.join(BASE_DIR, "moss_workflow_api.json")

# 同机的 ComfyUI 默认在 8188；换机器用环境变量 LT_COMFY 指过去
COMFY_URL = (os.environ.get("LT_COMFY") or "http://127.0.0.1:8188").rstrip("/")

NODE_PROMPT = "4"
NODE_NEG = "5"
NODE_LATENT = "6"
NODE_SEED = "7"
NODE_SAVE = "30"

# 封面是竖图：832x1216 在 SDXL 系的常见比例里不会糊
COVER_W, COVER_H = 832, 1216
# 生一张要多久取决于显卡。默认 180 秒，快卡三十几秒就出，慢卡也能撑到。
# 要改用 LT_COVER_TIMEOUT 环境变量，不要改这个默认值。
try:
    GEN_TIMEOUT = max(30, int(os.environ.get("LT_COVER_TIMEOUT") or 180))
except (TypeError, ValueError):
    GEN_TIMEOUT = 180
ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".webp")

os.makedirs(COVERS_DIR, exist_ok=True)


def _load_workflow():
    if not os.path.isfile(WORKFLOW_PATH):
        raise RuntimeError("找不到工作流文件 moss_workflow_api.json")
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        return json.load(f)


def _submit(workflow):
    res = requests.post(COMFY_URL + "/prompt",
                        json={"prompt": workflow, "client_id": uuid.uuid4().hex},
                        timeout=30)
    if res.status_code != 200:
        # ComfyUI 的报错把节点名和原因放在 body 里，原样带出去比自己编一句有用
        raise RuntimeError("提交失败：%s" % (res.text or "")[:300])
    data = res.json()
    if not data.get("prompt_id"):
        raise RuntimeError("ComfyUI 没有返回任务号：%s" % str(data)[:200])
    return data["prompt_id"]


def _wait(prompt_id, timeout=GEN_TIMEOUT):
    start = time.time()
    while time.time() - start < timeout:
        try:
            res = requests.get("%s/history/%s" % (COMFY_URL, prompt_id), timeout=15)
        except requests.RequestException:
            time.sleep(1.5)
            continue
        if res.status_code == 200:
            history = res.json() or {}
            if prompt_id in history:
                return history[prompt_id]
        time.sleep(1.5)
    raise RuntimeError("生成超时（%d 秒），看看 ComfyUI 后台" % timeout)


def _first_image(entry):
    """从 history 里挖出第一张图的位置描述。"""
    for out in (entry.get("outputs") or {}).values():
        for img in (out.get("images") or []):
            if img.get("filename"):
                return img
    return None


def _fetch(img):
    params = {"filename": img["filename"],
              "subfolder": img.get("subfolder") or "",
              "type": img.get("type") or "output"}
    res = requests.get(COMFY_URL + "/view", params=params, timeout=60)
    if res.status_code != 200 or not res.content:
        raise RuntimeError("取图失败：HTTP %d" % res.status_code)
    return res.content


def generate(prompt, negative="", width=COVER_W, height=COVER_H, timeout=GEN_TIMEOUT):
    """跑一次生成，返回落在 covers 目录里的文件名（不含目录）。"""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("提示词是空的")
    workflow = _load_workflow()
    if NODE_PROMPT in workflow:
        workflow[NODE_PROMPT]["inputs"]["text"] = prompt
    if negative and NODE_NEG in workflow:
        workflow[NODE_NEG]["inputs"]["text"] = negative
    if NODE_SEED in workflow:
        workflow[NODE_SEED]["inputs"]["seed"] = random.randint(0, 2 ** 48)
    if NODE_LATENT in workflow:
        workflow[NODE_LATENT]["inputs"]["width"] = width
        workflow[NODE_LATENT]["inputs"]["height"] = height
    if NODE_SAVE in workflow:
        # 不设 custom_path。设了会双写：ComfyUI 自己往那个目录写一张，我们拿回来
        # 又存一张，一次生成两份 3MB 的图。只要 filename_prefix，图还是从 /view 拉，
        # 落盘位置由 COVERS_DIR 决定，所以依然是算出来的而不是写死的。
        workflow[NODE_SAVE]["inputs"]["filename_prefix"] = "cover"

    entry = _wait(_submit(workflow), timeout=timeout)
    img = _first_image(entry)
    if not img:
        raise RuntimeError("生成完了但没有输出图片")
    blob = _fetch(img)

    # 延用 ComfyUI 输出的真实扩展名。它的保存节点可以出 jpg/png/webp，
    # 写死一个会让扩展名与内容不符。
    ext = os.path.splitext(str(img.get("filename") or ""))[1].lower()
    if ext not in ALLOWED_EXT:
        ext = ".jpg"
    name = "%s-%s%s" % (time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:6], ext)
    # 先写临时文件再改名：中途失败不会留下半张图被当成封面
    fd, tmp = tempfile.mkstemp(dir=COVERS_DIR, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, os.path.join(COVERS_DIR, name))
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return name


# 正在生成的任务：story_id -> {状态、提示词、开始时间、结果或错误}
# 只存内存。进程重启后没有任何任务在跑，落盘会把"生成中"永远留在文件里。
_JOBS = {}
_jobs_guard = threading.Lock()


def job_of(story_id):
    """看这份故事现在有没有正在生成的封面。没有就返回 None。"""
    with _jobs_guard:
        job = _JOBS.get(story_id)
        return dict(job) if job else None


def _set_job(story_id, **fields):
    with _jobs_guard:
        job = _JOBS.get(story_id) or {}
        job.update(fields)
        _JOBS[story_id] = job


def _run_job(story_id, prompt, negative, timeout):
    from .stories import load_story, save_story, set_cover

    _set_job(story_id, state="running", started=time.time(), error="")
    try:
        name = generate(prompt, negative=negative, timeout=timeout)
    except Exception as exc:                       # 后台线程里没人接异常
        _set_job(story_id, state="error", error=str(exc) or "生成失败")
        return
    story = load_story(story_id)
    if not story:
        _set_job(story_id, state="error", error="故事已被删掉")
        return
    set_cover(story, name, prompt)
    save_story(story)
    _set_job(story_id, state="done", cover=name, url=url_of(name),
             prompt=prompt, error="",
             history_count=len(story.get("cover_history") or []))


def start_job(story_id, prompt, negative="", timeout=None):
    """提交一个生图任务，立即返回。已经有一个在跑就不重复发。"""
    with _jobs_guard:
        cur = _JOBS.get(story_id)
        if cur and cur.get("state") == "running":
            return dict(cur)
        _JOBS[story_id] = {"state": "running", "started": time.time(),
                           "prompt": prompt, "error": "", "cover": ""}
    t = threading.Thread(target=_run_job,
                         args=(story_id, prompt, negative, timeout or GEN_TIMEOUT),
                         daemon=True)
    t.start()
    return job_of(story_id)


def save_upload(stream):
    """收一张上传的图，返回文件名。只认扩展名白名单，不认 Content-Type。"""
    ext = os.path.splitext(getattr(stream, "filename", "") or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError("只收 png / jpg / webp")
    name = "%s-%s%s" % (time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:6], ext)
    fd, tmp = tempfile.mkstemp(dir=COVERS_DIR, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            stream.save(f)
        os.replace(tmp, os.path.join(COVERS_DIR, name))
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return name


def exists(name):
    if not name or os.path.basename(name) != name:
        return False
    return os.path.isfile(os.path.join(COVERS_DIR, name))


def drop(name):
    if exists(name):
        try:
            os.unlink(os.path.join(COVERS_DIR, name))
        except OSError:
            pass


def url_of(name):
    return "/covers/" + name if name else ""
