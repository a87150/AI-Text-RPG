# -*- coding: utf-8 -*-
"""常量、路径、Flask 实例与模型提供方配置。"""
import json
import os
import re
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
KEEP_RECENT_MAX = 60        # 高级档不限条数，压缩后最多留这么多
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
}
# 自定义提供方存在 _provider_overrides 里，键是用户起的 id，带 label 才算数
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
# 写作参数：字数、上下文上限等，可在设置里改
WRITING_DEFAULTS = {
    "narrator_min": 40,
    "narrator_max": 100,
    "line_min": 120,
    "line_max": 280,
    "context_limit": 8000,
    "keep_recent": 16,
    "max_characters": 4,
    "summary_chars": 400,
}
WRITING_BOUNDS = {
    "narrator_min": (10, 300),
    "narrator_max": (20, 600),
    "line_min": (40, 1200),
    "line_max": (60, 2000),
    "context_limit": (2000, 60000),
    "keep_recent": (4, 60),
    "max_characters": (1, 10),
    "summary_chars": (150, 1200),
}
_writing = dict(WRITING_DEFAULTS)

# 自动备份：默认关，开了以后每隔 every_minutes 分钟打一个包
# 档位对应的注入策略。小模型的真实限制不只是窗口小，还有注意力散：
# 长历史会稀释近因，设定给多了会串味，账本读多了会开始抄。
# 0 表示不限（history_msgs）或不截断（role_chars）。
TIER_LIMITS = {
    "basic": {"context": 8000, "history_msgs": 16, "role_chars": 200,
              "facts": 6, "anchor_every": 8, "attempts": 3},
    "standard": {"context": 16000, "history_msgs": 40, "role_chars": 400,
                 "facts": 12, "anchor_every": 15, "attempts": 3},
    "advanced": {"context": 32000, "history_msgs": 0, "role_chars": 0,
                 "facts": 24, "anchor_every": 24, "attempts": 2},
}
TIER_CONTEXT = {k: v["context"] for k, v in TIER_LIMITS.items()}

BACKUP_DEFAULTS = {"enabled": False, "every_minutes": 1440, "keep": 30}
BACKUP_BOUNDS = {"every_minutes": (5, 20160), "keep": (1, 30)}   # 5 分钟 ~ 14 天
_backup = dict(BACKUP_DEFAULTS)
_loaded = False        # load_settings 跑过没有：没跑过就不敢覆盖磁盘配置

# 模型能力档位：决定状态行用多重的格式，能力越差给的结构要越简单
TIERS = ("basic", "standard", "advanced")
TIER_DEFAULT = "basic"
TIER_LABELS = {
    "basic": "初级 —— 小模型（9B 以下），单行标记 + 两条示例",
    "standard": "标准 —— 中等模型，单行标记 + 一条示例",
    "advanced": "高级 —— 大模型，结构化 JSON，不给示例",
}
_tier = TIER_DEFAULT

# 思考强度：本地支持思考的模型默认会想很久，显式给一档
REASONING_LEVELS = ("none", "low", "medium", "high")
REASONING_DEFAULT = "medium"
_reasoning = REASONING_DEFAULT

# 创意程度：只调温度是让措辞变花，不会让剧情变意外，所以还配一句写法倾向
CREATIVITY_LEVELS = ("standard", "creative", "wild")
CREATIVITY_DEFAULT = "standard"
CREATIVITY_LABELS = {
    "standard": "标准 — 顺着人设往下写，稳定",
    "creative": "创意 — 换一个角度接，愿意出点意外",
    "wild": "发散 — 允许节外生枝，可能跑偏",
}
CREATIVITY_TEMP = {"standard": 0.75, "creative": 0.95, "wild": 1.1}
_creativity = CREATIVITY_DEFAULT


def creativity():
    with _model_lock:
        return _creativity


def creativity_temp(level=None):
    return CREATIVITY_TEMP.get(level or creativity(), CREATIVITY_TEMP["standard"])


def save_creativity(value, persist=True):
    global _creativity
    if value not in CREATIVITY_LEVELS:
        return creativity()
    with _model_lock:
        _creativity = value
    if persist:
        persist_settings()
    return creativity()


def tier():
    with _model_lock:
        return _tier


def save_tier(value, persist=True):
    global _tier
    if value not in TIERS:
        return tier()
    with _model_lock:
        _tier = value
    # 换档位就换整份预算：上下文上限和压缩后保留的条数一起换，
    # 否则高级档能带更多历史，压缩却还只留 16 条，等于白换
    save_writing({"context_limit": TIER_LIMITS[value]["context"],
                  "keep_recent": tier_keep(value)}, persist=False)
    if persist:
        persist_settings()
    return tier()


def tier_limits(level=None):
    return TIER_LIMITS.get(level or tier(), TIER_LIMITS["basic"])


def tier_keep(level=None):
    """这档压缩后该留几条：跟历史能带的量走，高级档不限条数就用到上限。"""
    return tier_limits(level)["history_msgs"] or KEEP_RECENT_MAX


def tier_context(level=None):
    return tier_limits(level)["context"]


def reasoning_effort():
    with _model_lock:
        return _reasoning


def backup_conf():
    with _model_lock:
        return dict(_backup)


def save_backup_conf(data):
    global _backup
    if not isinstance(data, dict):
        return backup_conf()
    with _model_lock:
        if "enabled" in data:
            _backup["enabled"] = bool(data["enabled"])
        if "every_minutes" in data:
            try:
                minutes = int(data["every_minutes"])
            except (TypeError, ValueError):
                minutes = _backup["every_minutes"]
            lo, hi = BACKUP_BOUNDS["every_minutes"]
            _backup["every_minutes"] = max(lo, min(hi, minutes))
        if "keep" in data:
            try:
                keep = int(data["keep"])
            except (TypeError, ValueError):
                keep = _backup["keep"]
            lo, hi = BACKUP_BOUNDS["keep"]
            _backup["keep"] = max(lo, min(hi, keep))
    persist_settings()
    return backup_conf()


def save_reasoning(value, persist=True):
    global _reasoning
    if value not in REASONING_LEVELS:
        return reasoning_effort()
    with _model_lock:
        _reasoning = value
    if persist:
        persist_settings()
    return value

# 回复模式：normal = 系统旁白叙述；rp = 每轮先旁白、再由在场角色各自发言
MODES = ("normal", "rp")
MODE_DEFAULT = "normal"
MODE_LABELS = {"normal": "一般模式", "rp": "角色扮演"}
_mode = MODE_DEFAULT


def mode_of():
    with _model_lock:
        return _mode


def mode_for(sess):
    """对话自己的模式优先；老会话没有就跟随全局默认。"""
    value = (sess or {}).get("mode")
    return value if value in MODES else mode_of()


def reset_settings():
    """把写作参数、模式、档位、思考强度、创意程度恢复出厂。

    不动模型选择和云端 Key —— 那是用户配的接入凭据，还原默认不该让人重填一遍。
    """
    global _writing
    save_tier(TIER_DEFAULT, persist=False)
    save_mode(MODE_DEFAULT, persist=False)
    save_reasoning(REASONING_DEFAULT, persist=False)
    save_creativity(CREATIVITY_DEFAULT, persist=False)
    with _model_lock:
        # save_tier 会把 context_limit 写进 _writing，所以放它后面整体覆盖
        _writing = dict(WRITING_DEFAULTS)
    persist_settings()
    return writing()


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


def provider_ids():
    """内置的在前，用户自己加的在后。"""
    with _model_lock:
        extra = [pid for pid, over in _provider_overrides.items()
                 if pid not in PROVIDERS and (over or {}).get("label")]
    return list(PROVIDERS) + extra


def provider_public(pid):
    """对外的提供方信息：不下发 API Key，只告诉前端有没有配。"""
    base = PROVIDERS.get(pid) or {}
    over = _provider_overrides.get(pid) or {}
    models = over.get("models")
    if models is None:
        models = base.get("models") or []
    return {
        "id": pid,
        "label": over.get("label") or base.get("label") or pid,
        "kind": base.get("kind") or "cloud",
        "custom": pid not in PROVIDERS,
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


def _read_disk_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def persist_settings():
    # 没跑过 load_settings 的进程（命令行小脚本、单元检查）内存里全是默认值，
    # 直接写盘会把用户真实的自定义提供方、api_key、写作参数一起抹掉。
    # 这种进程只写它真正改过的那一项，其余沿磁盘原值。
    prev = {} if _loaded else _read_disk_settings()
    payload = {
        "provider": _model_state["provider"] if _loaded else (prev.get("provider") or DEFAULT_PROVIDER),
        "model": _model_state["model"] if _loaded else (prev.get("model") or ""),
        "providers": _provider_overrides or (prev.get("providers") or {}),
        "writing": prev.get("writing") or _writing,
        "writing_version": 1,
        "mode": prev.get("mode") or _mode,
        "reasoning": prev.get("reasoning") or _reasoning,
        "creativity": prev.get("creativity") or _creativity,
        "tier": prev.get("tier") or _tier,
        "backup": _backup,
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
    global _loaded
    _loaded = True
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
            if isinstance(conf, dict) and (pid in PROVIDERS or conf.get("label")):
                _provider_overrides[pid] = {
                    k: v for k, v in conf.items()
                    if k in ("base_url", "api_key", "models", "label") and isinstance(v, (str, list))
                }

    # 旧版本的 writing 没有版本号：默认值在那一版是 6 个角色，按新版重置一次
    write_conf = data.get("writing")
    if isinstance(write_conf, dict) and data.get("writing_version") == 1:
        save_writing(write_conf, persist=False)
    if data.get("mode") in MODES:
        save_mode(data["mode"], persist=False)
    if data.get("tier") in TIERS:
        save_tier(data["tier"], persist=False)
    if data.get("creativity") in CREATIVITY_LEVELS:
        save_creativity(data["creativity"], persist=False)
    backup_saved = data.get("backup")
    if isinstance(backup_saved, dict):
        if "enabled" in backup_saved:
            _backup["enabled"] = bool(backup_saved["enabled"])
        # 旧版本存的是小时，读到就换算成分钟
        if "every_minutes" in backup_saved or "every_hours" in backup_saved:
            try:
                if "every_minutes" in backup_saved:
                    minutes = int(backup_saved["every_minutes"])
                else:
                    minutes = int(backup_saved["every_hours"]) * 60
            except (TypeError, ValueError):
                minutes = _backup["every_minutes"]
            lo, hi = BACKUP_BOUNDS["every_minutes"]
            _backup["every_minutes"] = max(lo, min(hi, minutes))
        if "keep" in backup_saved:
            try:
                keep = int(backup_saved["keep"])
            except (TypeError, ValueError):
                keep = _backup["keep"]
            lo, hi = BACKUP_BOUNDS["keep"]
            _backup["keep"] = max(lo, min(hi, keep))

    # 云端 Key 被清掉后，不能把当前模型留在一个已不可用的提供方上
    if not provider_usable(_model_state["provider"]):
        _model_state["provider"] = DEFAULT_PROVIDER
        _model_state["model"] = DEFAULT_MODEL


def set_current_model(provider, model):
    provider = (provider or DEFAULT_PROVIDER).strip()
    model = (model or "").strip()
    if provider not in provider_ids():
        raise ValueError("未知的提供方：%s" % provider)
    if not model:
        raise ValueError("模型名不能为空")
    known = provider_public(provider)["models"]
    if known and model not in known:
        # 换提供方却带着上一个提供方的模型名，请求会打到错误的端点然后 404
        raise ValueError("%s 里没有 %s，先把这个模型名加到该提供方下"
                         % (provider_public(provider)["label"], model))
    with _model_lock:
        _model_state["provider"] = provider
        _model_state["model"] = model
    persist_settings()
    return provider, model


def save_provider(pid, data):
    if not pid:
        raise ValueError("提供方 id 不能为空")
    if pid not in PROVIDERS and not _PROVIDER_ID.match(pid):
        raise ValueError("提供方 id 只能用字母数字和 - _：%s" % pid)
    over = dict(_provider_overrides.get(pid) or {})
    label = data.get("label")
    if isinstance(label, str) and label.strip():
        over["label"] = label.strip()
    if pid not in PROVIDERS and not over.get("label"):
        raise ValueError("自定义提供方要填个名字")
    if PROVIDERS.get(pid, {}).get("kind") == "ollama":
        # 本地地址固定、模型列表由 Ollama 自己报，都不接受手填。
        # 手填一个云端模型名进来，选中后请求照样打到本地，只会吃 404。
        over.pop("base_url", None)
        over.pop("models", None)
    elif isinstance(data.get("base_url"), str):
        over["base_url"] = data["base_url"].strip()
    key = data.get("api_key")
    if isinstance(key, str) and key.strip():
        over["api_key"] = key.strip()
    if data.get("clear_key"):
        over.pop("api_key", None)
    models = data.get("models")
    if PROVIDERS.get(pid, {}).get("kind") != "ollama":
        if isinstance(models, str):
            over["models"] = [m.strip() for m in models.split(",") if m.strip()]
        elif isinstance(models, list):
            over["models"] = [str(m).strip() for m in models if str(m).strip()]
    _provider_overrides[pid] = over
    persist_settings()
    return provider_public(pid)


def delete_provider(pid):
    """只删自己加的；内置的删了没意义，下次启动又回来。"""
    if pid in PROVIDERS:
        raise ValueError("内置提供方不能删")
    if pid not in _provider_overrides:
        raise ValueError("这个提供方不存在")
    with _model_lock:
        _provider_overrides.pop(pid, None)
        if _model_state["provider"] == pid:
            _model_state["provider"] = DEFAULT_PROVIDER
            _model_state["model"] = DEFAULT_MODEL
    persist_settings()
    return provider_ids()


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
