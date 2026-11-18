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
THUMBS_DIR = os.path.join(COVERS_DIR, "thumbs")
# 列表里只显示 48px，但高分屏要撑 2-3 倍。存 160 够用，一张几 KB。
THUMB_SIZE = 160
# ComfyUI 写进来的文件名前缀。与历史上那批 cover_ 开头的区开，免得把旧图当成新生成的拿走。
SAVE_PREFIX = "gen"
WORKFLOW_PATH = os.path.join(BASE_DIR, "moss_workflow_api.json")

DEFAULT_COMFY = "http://127.0.0.1:8188"

def comfy_url():
    """设置里填的优先，其次 LT_COMFY 环境变量，都没有才用本机 8188。"""
    from .config import image_conf
    return (image_conf().get("url") or os.environ.get("LT_COMFY")
            or DEFAULT_COMFY).rstrip("/")

NODE_PROMPT = "4"
NODE_NEG = "5"
NODE_LATENT = "6"
NODE_SEED = "7"
NODE_SAVE = "30"

# 封面是竖图：832x1216 在 SDXL 系的常见比例里不会糊
COVER_W, COVER_H = 832, 1216
def gen_timeout():
    """生一张要多久取决于显卡。设置里填的优先，其次 LT_COVER_TIMEOUT，默认 180 秒。"""
    from .config import image_conf
    raw = image_conf().get("timeout") or os.environ.get("LT_COVER_TIMEOUT") or 180
    try:
        return max(30, int(raw))
    except (TypeError, ValueError):
        return 180
ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".webp")

os.makedirs(COVERS_DIR, exist_ok=True)

def _load_workflow():
    # 这份文件会被导入工作流改写，所以不进版本库（.bak 才是出厂默认）。
    # 新克隆的仓库里只有 .bak，回退到它。
    for path in (WORKFLOW_PATH, WORKFLOW_PATH + ".bak"):
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    raise RuntimeError("找不到工作流文件 moss_workflow_api.json")


# 换工作流时这几个节点号要跟着改，所以导入时得说清楚缺哪个
NODE_ROLES = {
    NODE_PROMPT: "正向提示词",
    NODE_NEG: "负向提示词",
    NODE_LATENT: "空 latent（尺寸）",
    NODE_SEED: "KSampler（种子）",
    NODE_SAVE: "保存",
}


def workflow_text():
    """导出：把当前工作流原样吐成文本。"""
    return _load_workflow()


# 生成时会直接改写的字段。缺了不是"不好看"，是运行时抛 KeyError。
NODE_FIELDS = {
    NODE_PROMPT: ("text",),
    NODE_NEG: ("text",),
    NODE_SEED: ("seed",),
    NODE_LATENT: ("width", "height"),
}


def workflow_problems(workflow):
    """导入前检查。返回问题列表，空表示能用。

    只看结构，不猜语义：是对象、每个值是带 class_type 的节点、
    五个固定节点号都在。少一个就生成不了，与其生成时才炸不如现在说。
    """
    if not isinstance(workflow, dict) or not workflow:
        return ["不是 ComfyUI 的 API 格式（顶层应该是一个非空对象）"]
    bad = [k for k, v in workflow.items()
           if not isinstance(v, dict) or not v.get("class_type")]
    if bad:
        return ["这些节点缺少 class_type：%s" % ", ".join(map(str, bad[:5]))]
    missing = [("%s（%s）" % (n, r)) for n, r in NODE_ROLES.items() if n not in workflow]
    if missing:
        return ["缺少节点：" + "、".join(missing)]
    # 生成时是往 workflow[节点]["inputs"][字段] 里直接写的。只查节点号在不在
    # 不够：节点有 class_type 却根本没 inputs，校验通过，跑的时候才 KeyError。
    no_inputs = [("%s（%s）" % (n, NODE_ROLES[n])) for n in NODE_ROLES
                 if not isinstance(workflow[n].get("inputs"), dict)]
    if no_inputs:
        return ["这些节点没有 inputs：" + "、".join(no_inputs)]
    lacks = []
    for node, fields in NODE_FIELDS.items():
        gone = [f for f in fields if f not in workflow[node]["inputs"]]
        if gone:
            lacks.append("%s（%s）缺 %s"
                         % (node, NODE_ROLES[node], "、".join(gone)))
    if lacks:
        return ["节点字段不全：" + "；".join(lacks)]
    return []


def save_workflow(workflow):
    """导入：校验通过才落盘，旧的先留一份 .bak，导错了能退回去。"""
    problems = workflow_problems(workflow)
    if problems:
        raise ValueError("；".join(problems))
    if os.path.isfile(WORKFLOW_PATH):
        try:
            os.replace(WORKFLOW_PATH, WORKFLOW_PATH + ".bak")
        except OSError:
            pass
    # 先写临时文件再换名，写到一半断电也不会留下半个 json
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(WORKFLOW_PATH) or ".", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(workflow, f, ensure_ascii=False, indent=2)
        os.replace(tmp, WORKFLOW_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return workflow_text()

def _submit(workflow):
    res = requests.post(comfy_url() + "/prompt",
                        json={"prompt": workflow, "client_id": uuid.uuid4().hex},
                        timeout=30)
    if res.status_code != 200:
        # ComfyUI 的报错把节点名和原因放在 body 里，原样带出去比自己编一句有用
        raise RuntimeError("提交失败：%s" % (res.text or "")[:300])
    data = res.json()
    if not data.get("prompt_id"):
        raise RuntimeError("ComfyUI 没有返回任务号：%s" % str(data)[:200])
    return data["prompt_id"]

def _wait(prompt_id, timeout=None):
    start = time.time()
    while time.time() - start < timeout:
        try:
            res = requests.get("%s/history/%s" % (comfy_url(), prompt_id), timeout=15)
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

def _written(img):
    """ComfyUI 把图写到哪了。不在本机（还是老版本没同意 custom_path）就返回 None。"""
    fn = str(img.get("filename") or "")
    sub = str(img.get("subfolder") or "").strip("/\\")
    if fn:
        cand = os.path.join(COVERS_DIR, sub, fn) if sub else os.path.join(COVERS_DIR, fn)
        if os.path.isfile(cand):
            return cand
    # custom_path 生效时，history 里报的名字和落盘名可能对不上。
    # 既然前缀是我们自己设的，按前缀找最新那张就是了。
    try:
        hits = [os.path.join(COVERS_DIR, f) for f in os.listdir(COVERS_DIR)
                if f.startswith(SAVE_PREFIX + "_")]
    except OSError:
        return None
    if not hits:
        return None
    hits.sort(key=os.path.getmtime)
    return hits[-1]

def _fetch(img):
    params = {"filename": img["filename"],
              "subfolder": img.get("subfolder") or "",
              "type": img.get("type") or "output"}
    res = requests.get(comfy_url() + "/view", params=params, timeout=60)
    if res.status_code != 200 or not res.content:
        raise RuntimeError("取图失败：HTTP %d" % res.status_code)
    return res.content

def generate(prompt, negative="", width=COVER_W, height=COVER_H, timeout=None):
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
        # 让 ComfyUI 直接写进 covers 目录。不设的话它会写自己的 output，我们再从
        # /view 拉一份存进来，同一张图两份。目录是 COVERS_DIR，算出来的。
        # ComfyUI 跑在别的机器上时这个路径它写不了，那时会自动回退到 /view。
        workflow[NODE_SAVE]["inputs"]["custom_path"] = COVERS_DIR
        workflow[NODE_SAVE]["inputs"]["filename_prefix"] = SAVE_PREFIX

    entry = _wait(_submit(workflow), timeout=timeout)
    img = _first_image(entry)
    if not img:
        raise RuntimeError("生成完了但没有输出图片")
    # 延用 ComfyUI 输出的真实扩展名。它的保存节点可以出 jpg/png/webp，
    # 写死一个会让扩展名与内容不符。
    ext = os.path.splitext(str(img.get("filename") or ""))[1].lower()
    if ext not in ALLOWED_EXT:
        ext = ".jpg"
    name = "%s-%s%s" % (time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:6], ext)
    dest = os.path.join(COVERS_DIR, name)

    # 它已经写在目录里就直接改名接管，不再拉一份；拿不到才走 /view。
    # ComfyUI 已经就地写好了。那就直接用它写的那一份，不拄也不拄过来——
    # Windows 上它刚写完还握着句柄，改名和删除都会失败，硬拄只会多出一份来。
    written = _written(img)
    if written:
        got = os.path.basename(written)
        if got != name:
            make_thumb(got)
            return got

    blob = _fetch(img)
    # 先写临时文件再改名：中途失败不会留下半张图被当成封面
    fd, tmp = tempfile.mkstemp(dir=COVERS_DIR, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, dest)
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
    # 整段都得包住，不只是 generate()。以前缩略图和写盘在 try 外面，
    # 那一步抛个 OSError，线程就带着 state="running" 死掉，而 start_job
    # 看到 running 直接返回旧任务——这个故事的封面从此再也生不出来。
    try:
        name = generate(prompt, negative=negative, timeout=timeout)
        story = load_story(story_id)
        if not story:
            _set_job(story_id, state="error", error="故事已被删掉")
            return
        make_thumb(name)
        set_cover(story, name, prompt)
        save_story(story)
    except Exception as exc:                       # 后台线程里没人接异常
        _set_job(story_id, state="error", error=str(exc) or "生成失败")
        return
    _set_job(story_id, state="done", cover=name, url=url_of(name),
             thumb=url_thumb(name),
             prompt=prompt, error="",
             history_count=len(story.get("cover_history") or []))

def start_job(story_id, prompt, negative="", timeout=None):
    """提交一个生图任务，立即返回。已经有一个在跑就不重复发。"""
    with _jobs_guard:
        cur = _JOBS.get(story_id)
        if cur and cur.get("state") == "running":
            # 线程要是卡住不返回（不是抛异常，是真的不动了），状态会一直挂在
            # running。等过头就当它没了，允许重新提交，别让整份故事永久报废。
            limit = (timeout or gen_timeout()) + 120
            if time.time() - float(cur.get("started") or 0) < limit:
                return dict(cur)
        _JOBS[story_id] = {"state": "running", "started": time.time(),
                           "prompt": prompt, "error": "", "cover": ""}
    t = threading.Thread(target=_run_job,
                         args=(story_id, prompt, negative, timeout or gen_timeout()),
                         daemon=True)
    t.start()
    return job_of(story_id)

def thumb_path(name):
    return os.path.join(THUMBS_DIR, name)

def make_thumb(name):
    """按原图生一张小图。失败就返回 False，回退到用原图。"""
    if not exists(name):
        return False
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        os.makedirs(THUMBS_DIR, exist_ok=True)
        with Image.open(os.path.join(COVERS_DIR, name)) as im:
            im = im.convert("RGB")
            im.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
            ext = os.path.splitext(name)[1].lower()
            # 小图一律存 JPEG，比 PNG 小一个量级
            out = thumb_path(name)
            if ext in (".jpg", ".jpeg"):
                im.save(out, "JPEG", quality=82, optimize=True)
            else:
                out = thumb_path(os.path.splitext(name)[0] + ".jpg")
                im.save(out, "JPEG", quality=82, optimize=True)
                return True
        return True
    except Exception:
        return False

def thumb_of(name):
    """缩略图文件名；没生成过就现场生成一张。"""
    if not exists(name):
        return ""
    base = os.path.splitext(name)[0] + ".jpg"
    if os.path.isfile(thumb_path(base)):
        return base
    return base if make_thumb(name) else ""

def url_thumb(name):
    t = thumb_of(name)
    return "/covers/thumbs/" + t if t else ""

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
    # exists() 已经拦过路径，但下面拼缩略图路径时又用了原始 name；
    # 入口是直接传用户输入的，这里必须自己再拦一道。
    if not name or os.path.basename(name) != name:
        return
    if exists(name):
        try:
            os.unlink(os.path.join(COVERS_DIR, name))
        except OSError:
            pass
    t = thumb_path(os.path.splitext(name or "")[0] + ".jpg")
    if os.path.basename(t) and os.path.isfile(t):
        try:
            os.unlink(t)
        except OSError:
            pass

def is_thumb(name):
    """只放行 thumbs 目录里实存的文件名（不含路径）。"""
    if not name or os.path.basename(name) != name:
        return False
    return os.path.isfile(thumb_path(name))

def url_of(name):
    return "/covers/" + name if name else ""