# -*- coding: utf-8 -*-
"""本地语音：vox-tts（C++ 引擎）常驻进程，走管道合成。

vox 的轮子只到 cp313，而主应用跑在 3.14，装不进同一个解释器，所以起一个
独立进程（rp/vox_worker.py）当引擎，主应用用 stdin/stdout 跟它说话。

不用 vox 自带的 serve：它的 /v1/tts 只认预先注册的风格名，内置发音人只有
default，没法按请求传一句自然语言风格。直接拿 Python 接口就有 instruct，
"说话很小声，害羞" 这种描述才生效。实测同一句同一音色，害羞比默认长 15%，
气势汹汹短 45%。

模型是 .tmod 单文件，由 python -m vox build 从 HF 的 Qwen3-TTS 权重转出来：
一次转换，之后每次启动只 mmap，不用过 torch。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid


from .config import BASE_DIR, SPEECH_LANGS, language_of, speech_conf

# 可重入：_ensure_server 持着这把锁起进程，失败时调 reset() 收尾，
# 而 reset() 自己也要这把锁。用普通 Lock 就是同一个线程把自己锁死，
# 之后语音设置存不了、朗读不出声，只有重启才能恢复。
_serve_lock = threading.RLock()
_proc = None
_ready_at = ""
_last_used = 0.0
_idle_thread = None

# 同一句话正在合成时，后来的请求等同一个结果，不重复跑模型。
# 双击按钮、预取和正式请求撞在一起都很常见，去掉这一层就是白烧几秒 GPU。
_inflight = {}
_inflight_lock = threading.Lock()

# 管道只能一问一答，并发写会把两句话的音频搅在一起
_synth_lock = threading.Lock()


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


def styles_of(conf):
    """风格表：一行一个 风格名=自然语言描述。"""
    return parse_options((conf or {}).get("styles"))


def instruct_of(conf, style):
    """风格名换成描述；表里没有就把传进来的当描述用，方便直接试一句。"""
    name = (style or "").strip()
    if not name:
        return ""
    return styles_of(conf).get(name) or name


def model_path(conf):
    """模型文件的绝对路径。设置里可以写相对路径，相对的是项目根目录。"""
    raw = (conf.get("model") or "").strip()
    if not raw:
        raise SpeechError("还没设语音模型。用 python -m vox build 转一个 .tmod 出来，再在设置里填路径。")
    return raw if os.path.isabs(raw) else os.path.join(BASE_DIR, raw)


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
    """常驻进程活着且握过手，才算能合成。"""
    return _proc is not None and _proc.poll() is None and bool(_ready_at)


def _ensure_server(conf):
    """把合成进程拉起来；已经在跑就直接返回。"""
    global _proc, _ready_at
    if _alive(conf):
        return _ready_at
    with _serve_lock:
        if _alive(conf):
            return _ready_at
        model = model_path(conf)
        if not os.path.isfile(model):
            raise SpeechError("模型文件不存在：%s" % model)
        python = _find_python(conf)
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vox_worker.py")
        cmd = (python.split() if " " in python else [python]) + [
            "-u", worker, model,
            str(conf.get("device") or "auto"),
            str(conf.get("threads") or 0),
        ]
        try:
            _proc = subprocess.Popen(
                cmd, cwd=BASE_DIR,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                # 崩了要看栈：丢进 devnull 的话，「合成进程已经不在了」永远查不出原因
                stderr=open(os.path.join(BASE_DIR, "_vox_worker.err"), "a", encoding="utf-8"),
                text=True, encoding="utf-8", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise SpeechError("起不来 vox 合成进程：%s" % exc) from exc

        # 第一行是就绪握手。加载要几秒，给它 60 秒；读超时靠线程兜住
        box = {}

        def reader():
            try:
                box["line"] = _proc.stdout.readline()
            except (OSError, ValueError):
                box["line"] = ""

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        th.join(60)
        line = (box.get("line") or "").strip()
        if not line:
            reset()
            raise SpeechError("vox 合成进程 60 秒没就绪，多半是模型太大或显存不够。")
        try:
            info = json.loads(line)
        except ValueError:
            reset()
            raise SpeechError("vox 合成进程握手失败：%s" % line[:120])
        if not info.get("ok"):
            reset()
            raise SpeechError("vox 合成进程起不来：%s" % (info.get("error") or ""))
        _ready_at = "worker"
        return _ready_at


def _language():
    """语音语言跟正文语言走，不单独设。"""
    return SPEECH_LANGS.get(language_of(), "chinese")


def _start_idle_watch():
    """空下来就把 vox 收掉，它占着好几 GB 显存。"""
    global _idle_thread

    def loop():
        while True:
            time.sleep(15)
            conf = speech_conf()
            limit = int(conf.get("idle") or 0)
            if limit <= 0 or not _last_used:
                continue
            if time.time() - _last_used < limit:
                continue
            if _proc is not None and _proc.poll() is None:
                reset()

    with _serve_lock:
        if _idle_thread is not None and _idle_thread.is_alive():
            return
        _idle_thread = threading.Thread(target=loop, name="vox-idle", daemon=True)
        _idle_thread.start()


def speak(text, role=None, **overrides):
    """把一段文字念成 wav 字节。

    每次调用都是独立的：参数只跟着这一次请求走，服务端不存任何会话状态。
    role 先按音色表换成 voice，overrides 再盖上去（voice / language / temperature / seed）。
    """
    global _last_used

    body = (text or "").strip()
    if not body:
        raise SpeechError("没有要念的文字。")
    conf = speech_conf()

    style = overrides.pop("style", None)
    payload = {
        "text": body,
        "voice": voice_for(conf, role) or conf.get("voice") or "vivian",
        "language": _language(),
        "format": "wav",
    }
    for key, val in overrides.items():
        if val is None or val == "":
            continue
        payload[str(key)] = val
    # 风格只影响这一句怎么念，不换音色
    payload["style"] = instruct_of(conf, style)

    key = (body, payload["voice"], payload["style"], payload["language"],
           str(payload.get("temperature") or ""), str(payload.get("seed") or ""))

    with _inflight_lock:
        slot = _inflight.get(key)
        owner = slot is None
        if owner:
            slot = {"done": threading.Event(), "audio": None, "error": None}
            _inflight[key] = slot

    if not owner:
        # 有人正在合成同一句，等他的结果
        slot["done"].wait(200)
        if slot["error"]:
            raise SpeechError(slot["error"])
        if slot["audio"]:
            _last_used = time.time()
            return slot["audio"]
        # 等超时了就当没这回事，自己重跑
        with _inflight_lock:
            slot = {"done": threading.Event(), "audio": None, "error": None}
            _inflight[key] = slot

    try:
        audio = _synth(conf, payload)
        slot["audio"] = audio
        _last_used = time.time()
        _start_idle_watch()
        return audio
    except SpeechError as exc:
        slot["error"] = str(exc)
        raise
    finally:
        slot["done"].set()
        with _inflight_lock:
            if _inflight.get(key) is slot:
                _inflight.pop(key, None)


def _synth(conf, payload, _retry=True):
    """一句话一次问询：写一行 json，读一行结果，音频落在临时文件里。"""
    _ensure_server(conf)

    out = os.path.join(tempfile.gettempdir(), "lt-tts-%s.wav" % uuid.uuid4().hex)
    req = {
        "op": "synth",
        "text": payload.get("text") or "",
        "speaker": payload.get("voice") or "vivian",
        # 风格是自然语言，vox 的 instruct 收的正是这个
        "instruct": payload.get("style") or "",
        "out": out,
    }

    with _synth_lock:
        proc = _proc
        if proc is None or proc.poll() is not None:
            raise SpeechError("vox 合成进程已经不在了，再念一次就会重起。")
        try:
            proc.stdin.write(json.dumps(req, ensure_ascii=False) + chr(10))
            proc.stdin.flush()
            line = proc.stdout.readline()
        except (OSError, ValueError) as exc:
            reset()
            raise SpeechError("和 vox 合成进程的管道断了：%s" % exc) from exc

    if not line.strip():
        reset()
        raise SpeechError("vox 合成进程没回话就退了，再念一次就会重起。")
    try:
        info = json.loads(line)
    except ValueError:
        raise SpeechError("vox 回了个看不懂的结果：%s" % line[:120])
    if not info.get("ok"):
        raise SpeechError("vox 合成失败：%s" % (info.get("error") or "没给原因"))
    # 空转超时退出的进程，管道里还留着上一轮的回复行：它同样是 ok，
    # 但 out 是上一轮那个临时文件，早被删掉了——照本轮路径去读就是「读不到音频」。
    # 拿 out 对一遍才认得出，顺手把进程重启再发一次。
    if info.get("out") != out:
        reset()
        if _retry:
            return _synth(conf, payload, False)
        raise SpeechError("vox 回的是上一轮的结果，重发也没拿到新的，再念一次。")

    try:
        with open(out, "rb") as fh:
            audio = fh.read()
    except OSError as exc:
        raise SpeechError("读不到合成出来的音频：%s" % exc) from exc
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    if not audio:
        raise SpeechError("vox 返回了空音频。")
    return audio


def reset():
    """设置改了就停掉旧服务，下次念按新配置重起。"""
    global _proc, _ready_at
    with _serve_lock:
        if _proc is not None and _proc.poll() is None:
            try:
                _proc.stdin.write('{"op":"close"}' + chr(10))
                _proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                _proc.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                try:
                    _proc.terminate()
                    _proc.wait(timeout=8)
                except (OSError, subprocess.SubprocessError):
                    pass
        _proc, _ready_at = None, ""
