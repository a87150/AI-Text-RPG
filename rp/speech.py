# -*- coding: utf-8 -*-
"""本地语音：vox-tts（C++ 引擎）通过 HTTP 合成。

vox 是独立进程，主应用只当客户端：

    python -m vox serve --model models/qwen3-custom.tmod --port 8190

为什么要绕一层：vox 的轮子只到 cp313，而主应用跑在 3.14，装不进同一个解释器。
引擎本身是 C++，换解释器只影响怎么启动，合成路径完全一样。
实测一句 6.3 秒的音频 3.2 秒合成完，首包 0.18 秒，比原来的纯 Python 实现快一个数量级。

模型是 .tmod 单文件，由 python -m vox build 从 HF 的 Qwen3-TTS 权重转出来：
一次转换，之后每次启动只 mmap，2.5 秒进内存，不用过 torch。
"""
import json
import os
import subprocess
import sys
import threading
import time

import requests

from .config import BASE_DIR, speech_conf

_serve_lock = threading.Lock()
_proc = None
_ready_at = ""


class SpeechError(RuntimeError):
    """念不出来时抛这个，里面的句子直接给用户看。"""


def parse_options(text):
    """把「每行一个 key=value」读成 dict。音色表用的就是它。"""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def voice_for(conf, role):
    """角色名 -> 音色。音色表是「角色=vivian」一行一个，* 是兜底。"""
    name = (role or "").strip()
    table = parse_options(conf.get("voices"))
    if not table:
        return ""
    if name and name in table:
        return table[name]
    return table.get("*") or ""


def model_path(conf):
    """模型文件的绝对路径。设置里可以写相对路径，相对的是项目根目录。"""
    raw = (conf.get("model") or "").strip()
    if not raw:
        raise SpeechError("还没设语音模型。用 python -m vox build 转一个 .tmod 出来，再在设置里填路径。")
    return raw if os.path.isabs(raw) else os.path.join(BASE_DIR, raw)


def _base_url(conf):
    return (conf.get("url") or "http://127.0.0.1:8190").rstrip("/")


def _find_python(conf):
    """找能 import vox 的解释器。设置里填了就用它，否则按常见位置挨个试。"""
    want = (conf.get("python") or "").strip()
    if want:
        return want
    # vox 的轮子只到 3.13；主应用是 3.14，所以不能拿 sys.executable 凑合
    candidates = ["py -3.13", "python3.13", "python3"]
    local = os.path.join(os.path.dirname(sys.executable), "..", "Python313", "python.exe")
    if os.path.isfile(local):
        candidates.insert(0, local)
    for exe in candidates:
        try:
            probe = subprocess.run(
                (exe.split() if " " in exe else [exe]) + ["-c", "import vox"],
                capture_output=True, timeout=20)
            if probe.returncode == 0:
                return exe
        except (OSError, subprocess.SubprocessError):
            continue
    raise SpeechError("找不到装了 vox-tts 的 Python 3.13。装一个：pip install vox-tts，再在设置里指明解释器。")


def _alive(conf):
    try:
        return requests.get(_base_url(conf) + "/health", timeout=3).status_code == 200
    except requests.RequestException:
        return False


def _ensure_server(conf):
    """把 vox 服务拉起来；已经在跑就直接返回。只启一次。"""
    global _proc, _ready_at
    if _alive(conf):
        return _base_url(conf)
    with _serve_lock:
        if _alive(conf):
            return _base_url(conf)
        model = model_path(conf)
        if not os.path.isfile(model):
            raise SpeechError("模型文件不存在：%s" % model)
        python = _find_python(conf)
        port = (_base_url(conf).rsplit(":", 1)[-1] or "8190")
        cmd = (python.split() if " " in python else [python]) + [
            "-m", "vox", "serve",
            "--model", model,
            "--host", "127.0.0.1",
            "--port", port,
            "--device", str(conf.get("device") or "auto"),
            "--threads", str(conf.get("threads") or 0),
        ]
        try:
            _proc = subprocess.Popen(
                cmd, cwd=BASE_DIR,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise SpeechError("起不来 vox 服务：%s" % exc) from exc
        # 加载要几秒，给它 40 秒。超时多半是模型路径或显存的问题
        deadline = time.time() + 40
        while time.time() < deadline:
            if _proc.poll() is not None:
                raise SpeechError("vox 服务启动就退了，检查模型文件和 Python 解释器。")
            if _alive(conf):
                _ready_at = _base_url(conf)
                return _ready_at
            time.sleep(0.4)
        raise SpeechError("vox 服务 40 秒还没起来，多半是模型太大或显存不够。")


def speak(text, role=None, **overrides):
    """把一段文字念成 wav 字节。

    每次调用都是独立的：参数只跟着这一次请求走，服务端不存任何会话状态。
    role 先按音色表换成 voice，overrides 再盖上去（voice / language / temperature / seed）。
    """
    body = (text or "").strip()
    if not body:
        raise SpeechError("没有要念的文字。")
    conf = speech_conf()
    base = _ensure_server(conf)

    payload = {
        "text": body,
        "voice": voice_for(conf, role) or conf.get("voice") or "vivian",
        "language": conf.get("language") or "chinese",
        "format": "wav",
    }
    for key, val in overrides.items():
        if val is None or val == "":
            continue
        payload[str(key)] = val

    try:
        res = requests.post(base + "/v1/tts", json=payload, timeout=180)
    except requests.RequestException as exc:
        raise SpeechError("连不上 vox 服务：%s" % exc) from exc
    if res.status_code != 200:
        detail = ""
        try:
            detail = (res.json() or {}).get("error") or ""
        except ValueError:
            detail = (res.text or "")[:200]
        raise SpeechError("vox 合成失败（%s）：%s" % (res.status_code, detail or "没给原因"))
    audio = res.content
    if not audio:
        raise SpeechError("vox 返回了空音频。")
    return audio


def reset():
    """设置改了就停掉旧服务，下次念按新配置重起。"""
    global _proc, _ready_at
    with _serve_lock:
        if _proc is not None and _proc.poll() is None:
            try:
                _proc.terminate()
                _proc.wait(timeout=8)
            except (OSError, subprocess.SubprocessError):
                pass
        _proc, _ready_at = None, ""
