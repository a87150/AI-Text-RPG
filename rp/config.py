# -*- coding: utf-8 -*-
"""常量、路径、Flask 实例与模型提供方配置。"""
import json
import os
import threading
import uuid

import requests
from flask import Flask

# ============================== 模型配置 ==============================
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_API_URL = OLLAMA_HOST + "/v1/chat/completions"
OLLAMA_TAGS_URL = OLLAMA_HOST + "/api/tags"
DEFAULT_MODEL = "dmtx/qwen3.5-9b-abliterated:latest"

# ============================== 参数 ==============================
MAX_CONTEXT_CHARS = 8000
KEEP_RECENT_MESSAGES = 16
REQUEST_TIMEOUT = (15, 300)

MAX_CHAT_ATTEMPTS = 3
MAX_OPENING_ATTEMPTS = 3
MAX_ENHANCE_ATTEMPTS = 3
MIN_CHAT_CHARS = 50         # 少于这个字符数视为失败
RETRY_TEMP_STEP = 0.05      # 每次重试温度微调

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("LT_DATA_DIR") or os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
# 本地小应用：改模板不该还要重启，模板缓存会让人以为是"代码没生效"
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
_model_lock = threading.Lock()

# 提供方：ollama 走本地；cloud 走 OpenAI 兼容的 /chat/completions
PROVIDERS = {
    "ollama": {
        "label": "本地 Ollama", "kind": "ollama",
        # OpenAI 兼容端点固定在 /v1 下
        "base_url": OLLAMA_HOST + "/v1", "models": [],
    },
    "deepseek": {
        "label": "DeepSeek", "kind": "cloud",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "openai": {
        "label": "OpenAI", "kind": "cloud",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o"],
    },
    "custom": {
        "label": "自定义（OpenAI 兼容）", "kind": "cloud",
        "base_url": "", "models": [],
    },
}
# 写作参数：字数、上下文上限等，可在设置里改
WRITING_DEFAULTS = {
    "narrator_min": 40,
    "narrator_max": 100,
    "line_min": 120,
    "line_max": 280,
    "context_limit": 8000,
    "keep_recent": 16,
    "max_characters": 4,
}
WRITING_BOUNDS = {
    "narrator_min": (10, 300),
    "narrator_max": (20, 600),
    "line_min": (40, 1200),
    "line_max": (60, 2000),
    "context_limit": (2000, 60000),
    "keep_recent": (4, 60),
    "max_characters": (1, 8),
}
_writing = dict(WRITING_DEFAULTS)

# 思考强度：本地支持思考的模型默认会想很久，显式给一档
REASONING_LEVELS = ("none", "low", "medium", "high")
REASONING_DEFAULT = "medium"
_reasoning = REASONING_DEFAULT


def reasoning_effort():
    with _model_lock:
        return _reasoning


def save_reasoning(value, persist=True):
    global _reasoning
    if value not in REASONING_LEVELS:
        return reasoning_effort()
    with _model_lock:
        _reasoning = value
    if persist:
        persist_settings()
    return value

# 回复模式：normal = 系统旁白叙述；rp = 角色轮流按人设说话
MODES = ("normal", "rp")
MODE_LABELS = {"normal": "一般模式", "rp": "角色扮演"}
_mode = "normal"


def mode_of():
    with _model_lock:
        return _mode


def mode_for(sess):
    """对话自己的模式优先；老会话没有就跟随全局默认。"""
    value = (sess or {}).get("mode")
    return value if value in MODES else mode_of()


def save_mode(value, persist=True):
    global _mode
    if value not in MODES:
        return mode_of()
    with _model_lock:
        _mode = value
    if persist:
        persist_settings()
    return value


def _clamp(key, value):
    lo, hi = WRITING_BOUNDS[key]
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return WRITING_DEFAULTS[key]


def writing():
    with _model_lock:
        return dict(_writing)


def save_writing(data, persist=True):
    """只改真的给了值的项；空串/0 视为「这次不动」，否则一次误传就把参数压到下限。"""
    with _model_lock:
        for key, value in (data or {}).items():
            if key not in WRITING_BOUNDS:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number <= 0:
                continue
            lo, hi = WRITING_BOUNDS[key]
            _writing[key] = max(lo, min(hi, number))
        if _writing["narrator_min"] > _writing["narrator_max"]:
            _writing["narrator_max"] = _writing["narrator_min"]
        if _writing["line_min"] > _writing["line_max"]:
            _writing["line_max"] = _writing["line_min"]
        snapshot = dict(_writing)
    if persist:
        persist_settings()
    return snapshot


DEFAULT_PROVIDER = "ollama"
_model_state = {"provider": DEFAULT_PROVIDER, "model": DEFAULT_MODEL}
_provider_overrides = {}   # pid -> {base_url, api_key, models}

def current_target():
    with _model_lock:
        return _model_state["provider"], _model_state["model"]


def provider_public(pid):
    """对外的提供方信息：不下发 API Key，只告诉前端有没有配。"""
    base = PROVIDERS.get(pid) or {}
    over = _provider_overrides.get(pid) or {}
    models = over.get("models")
    if models is None:
        models = base.get("models") or []
    return {
        "id": pid,
        "label": base.get("label") or pid,
        "kind": base.get("kind") or "cloud",
        "base_url": over.get("base_url") or base.get("base_url") or "",
        "models": list(models),
        "has_key": bool((over.get("api_key") or "").strip()),
    }


def provider_endpoint(pid):
    info = provider_public(pid)
    base = (info["base_url"] or "").rstrip("/")
    if not base:
        raise RuntimeError("%s 还没有设置接口地址" % info["label"])
    return base + "/chat/completions"


def provider_usable(pid):
    """本地随时可用；云端要配好地址和 Key 才算可用。"""
    info = provider_public(pid)
    if info["kind"] == "ollama":
        return True
    return bool(info["has_key"]) and bool(info["base_url"].strip())


def provider_headers(pid):
    info = provider_public(pid)
    if info["kind"] != "cloud":
        return {}
    key = ((_provider_overrides.get(pid) or {}).get("api_key") or "").strip()
    if not key:
        raise RuntimeError("%s 还没有配置 API Key，请在「云端模型」里填一下" % info["label"])
    return {"Authorization": "Bearer " + key}


def persist_settings():
    payload = {
        "provider": _model_state["provider"],
        "model": _model_state["model"],
        "providers": _provider_overrides,
        "writing": _writing,
        "writing_version": 1,
        "mode": _mode,
        "reasoning": _reasoning,
    }
    tmp = "%s.%s.tmp" % (SETTINGS_PATH, uuid.uuid4().hex[:8])
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except OSError as exc:
        app.logger.warning("保存设置失败：%s", exc)
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return
    if not isinstance(data, dict):
        return

    saved = data.get("model")
    if isinstance(saved, str) and saved.strip():   # 旧格式
        _model_state["provider"] = DEFAULT_PROVIDER
        _model_state["model"] = saved.strip()
    elif isinstance(saved, dict):
        pid = (saved.get("provider") or DEFAULT_PROVIDER).strip()
        name = (saved.get("model") or "").strip()
        if pid in PROVIDERS and name:
            _model_state["provider"] = pid
            _model_state["model"] = name

    global _reasoning
    level = data.get("reasoning")
    if level in REASONING_LEVELS:
        _reasoning = level

    providers = data.get("providers")
    if isinstance(providers, dict):
        for pid, conf in providers.items():
            if pid in PROVIDERS and isinstance(conf, dict):
                _provider_overrides[pid] = {
                    k: v for k, v in conf.items()
                    if k in ("base_url", "api_key", "models") and isinstance(v, (str, list))
                }

    # 旧版本的 writing 没有版本号：默认值在那一版是 6 个角色，按新版重置一次
    write_conf = data.get("writing")
    if isinstance(write_conf, dict) and data.get("writing_version") == 1:
        save_writing(write_conf, persist=False)
    if data.get("mode") in MODES:
        save_mode(data["mode"], persist=False)

    # 云端 Key 被清掉后，不能把当前模型留在一个已不可用的提供方上
    if not provider_usable(_model_state["provider"]):
        _model_state["provider"] = DEFAULT_PROVIDER
        _model_state["model"] = DEFAULT_MODEL


def set_current_model(provider, model):
    provider = (provider or DEFAULT_PROVIDER).strip()
    model = (model or "").strip()
    if provider not in PROVIDERS:
        raise ValueError("未知的提供方：%s" % provider)
    if not model:
        raise ValueError("模型名不能为空")
    with _model_lock:
        _model_state["provider"] = provider
        _model_state["model"] = model
    persist_settings()
    return provider, model


def save_provider(pid, data):
    if pid not in PROVIDERS:
        raise ValueError("未知的提供方：%s" % pid)
    over = dict(_provider_overrides.get(pid) or {})
    if PROVIDERS[pid].get("kind") == "ollama":
        over.pop("base_url", None)   # 本地地址固定，不接受覆盖
    elif isinstance(data.get("base_url"), str):
        over["base_url"] = data["base_url"].strip()
    key = data.get("api_key")
    if isinstance(key, str) and key.strip():
        over["api_key"] = key.strip()
    if data.get("clear_key"):
        over.pop("api_key", None)
    models = data.get("models")
    if isinstance(models, str):
        over["models"] = [m.strip() for m in models.split(",") if m.strip()]
    elif isinstance(models, list):
        over["models"] = [str(m).strip() for m in models if str(m).strip()]
    _provider_overrides[pid] = over
    persist_settings()
    return provider_public(pid)


def list_local_models(timeout=8):
    resp = requests.get(OLLAMA_TAGS_URL, timeout=timeout)
    resp.raise_for_status()
    names = []
    for item in (resp.json().get("models") or []):
        name = item.get("name") or item.get("model")
        if name:
            names.append(name)
    return sorted(names)


_file_lock = threading.RLock()
_session_locks = {}
_session_locks_guard = threading.Lock()
