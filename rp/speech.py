# -*- coding: utf-8 -*-
"""本地语音：用 voco 把文本念出来。

voco 把运行时和模型拆开，每个模型是一个插件包：

    pip install voco            # 核心，零依赖
    pip install voco-kokoro     # 装你要的那个模型

换模型只改设置里的插件名，这里一行不用动。插件里往往带着 torch 权重，
所以全部懒加载——没念过之前不会 import 任何重东西，启动不受影响。
"""
import importlib
import io
import threading
import wave

from .config import speech_conf

_lock = threading.Lock()
_router = None
_loaded_key = None
# 一句一回可能念几十秒，并发点在「用户连点两条」，串行比抢显存安全
_speak_lock = threading.Lock()


class SpeechError(RuntimeError):
    """念不出来时抛这个，里面的句子直接给用户看。"""


def parse_options(text):
    """把「每行一个 key=value」读成 dict。值保留字符串，类型让插件自己转。"""
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


# 构造参数跟推理参数要分开：前者进 router.load()，后者透给 infer()
LOAD_KEYS = ("variant", "model_id", "dtype")


def _import_plugin(name):
    """voco 0.0.8 的 AudioRouter.load() 只查内存里的注册表，不扫 entry point。

    插件模块在 import 时自己调 register_model，所以这一步不能省。
    装没装都不报错，真正缺了会在 router.load() 那里报更直白的错。
    """
    mod = "voco_" + str(name or "").replace("-", "_")
    try:
        importlib.import_module(mod)
    except ImportError:
        pass


def _get_router(conf):
    global _router, _loaded_key
    key = (conf.get("plugin"), conf.get("alias"), conf.get("device"), conf.get("options"))
    with _lock:
        if _router is not None and _loaded_key == key:
            return _router
        try:
            from voco.core import AudioRouter
        except ImportError as exc:
            raise SpeechError(
                "没装 voco。先 pip install voco，再装一个模型插件（例如 pip install voco-kokoro）。"
            ) from exc
        _import_plugin(conf.get("plugin"))
        load_kw = {k: v for k, v in parse_options(conf.get("options")).items() if k in LOAD_KEYS}
        try:
            router = AudioRouter(cache=True)
            router.load(conf["plugin"], alias=conf["alias"], device=conf["device"], **load_kw)
        except Exception as exc:
            raise SpeechError("voco 加载插件「%s」失败：%s" % (conf.get("plugin"), exc)) from exc
        _router, _loaded_key = router, key
        return router


def _first_audio(result):
    """voco 的 infer 有时返回生成器、有时直接给结果，两种都认。"""
    if isinstance(result, (bytes, bytearray)):
        return result
    if hasattr(result, "audio"):
        return result.audio
    try:
        it = iter(result)
    except TypeError:
        return result
    for item in it:
        return getattr(item, "audio", item)
    raise SpeechError("voco 没返回音频，插件可能不认这些参数。")


def _wav_bytes(audio):
    """统一成 wav 字节流：已经是编码好的就直接用，是波形数组就自己封头。"""
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    if isinstance(audio, tuple) and len(audio) == 2:
        data, rate = audio
    else:
        data, rate = audio, 24000
    try:
        import numpy as np
    except ImportError as exc:
        raise SpeechError("拿到的是原始波形，但没有 numpy 可以封装成 wav。") from exc
    arr = np.asarray(data, dtype=np.float32).reshape(-1)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(int(rate) or 24000)
        f.writeframes(pcm)
    return buf.getvalue()


def voice_for(conf, role):
    """角色名 -> 音色。音色表是「角色=Vivian」一行一个，* 是兜底。"""
    name = (role or "").strip()
    table = parse_options(conf.get("voices"))
    if not table:
        return ""
    if name and name in table:
        return table[name]
    return table.get("*") or ""


def speak(text, role=None, **overrides):
    """把一段文字念成 wav 字节。

    每次调用都是独立的：generate_custom_voice 本身没有跨请求状态。
    role 先按音色表换成 speaker，overrides 再盖上去（speaker / instruct / language…）。
    """
    body = (text or "").strip()
    if not body:
        raise SpeechError("没有要念的文字。")
    conf = speech_conf()
    raw = parse_options(conf.get("options"))
    options = {k: v for k, v in raw.items() if k not in LOAD_KEYS}

    speaker = voice_for(conf, role)
    if speaker:
        options["speaker"] = speaker
    for key, val in overrides.items():
        if val is None or val == "":
            continue
        options[str(key)] = str(val)

    # voco 的缓存 key 只有 alias + text，换音色/换语气也会命中旧音频。
    # 把这次真正生效的参数掺进指纹；它进不了 generate 的签名，被 **kwargs 吃掉
    options["_vo"] = "|".join(str(options.get(k) or "") for k in (
        "variant", "model_id", "speaker", "instruct", "language"))
    router = _get_router(conf)
    with _speak_lock:
        try:
            raw = router.infer(conf["alias"], text=body, **options)
            return _wav_bytes(_first_audio(raw))
        except SpeechError:
            raise
        except Exception as exc:
            raise SpeechError("念到一半出错：%s" % exc) from exc


def reset():
    """设置改了就丢掉旧的 router，下次念按新插件重新加载。"""
    global _router, _loaded_key
    with _lock:
        _router = None
        _loaded_key = None
