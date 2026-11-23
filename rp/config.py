# -*- coding: utf-8 -*-
"""常量、路径、Flask 实例与模型提供方配置。"""
import ipaddress
import json
import os
import socket
import re
import threading
import time
import uuid

import requests
from flask import Flask

# ============================== 版本 ==============================
VERSION = "1.51"

# ============================== 模型配置 ==============================
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_API_URL = OLLAMA_HOST + "/v1/chat/completions"
OLLAMA_TAGS_URL = OLLAMA_HOST + "/api/tags"
DEFAULT_MODEL = "dmtx/qwen3.5-9b-abliterated:latest"

# ============================== 参数 ==============================
MAX_CONTEXT_TOKENS = 8000
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
def _lan_ip():
    """本机的局域网地址。UDP connect 只让内核选路，不会真的发包。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        s = None
    if s is not None:
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            pass
        finally:
            s.close()
    # 上面这条路在网卡没起来时会失败（开机自启、Wi-Fi 还在重连）。
    # 退一步问系统要本机地址，挑第一个私有 IPv4——总比返回空串、
    # 让手机永久 400 强。
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ipaddress.ip_address(ip).is_private:
                return ip
    except (OSError, UnicodeError, ValueError):
        pass
    return ""

LAN_IP = _lan_ip()

# 白名单。Flask 每次请求都会重读这个配置，所以运行时补进去是立刻生效的。
_HOSTS = ("127.0.0.1", "localhost", "::1")

def _refresh_hosts():
    app.config["TRUSTED_HOSTS"] = [h for h in _HOSTS + (LAN_IP,) if h]

def _watch_lan():
    """网卡晚起来也能自己长回来。

    地址只在导入时算一次的话，开机那会儿没网，白名单里就只有三个
    loopback，手机从此 400 到底——而启动日志看着一切正常，根本不会
    想到去怀疑绑定。
    """
    global LAN_IP
    while True:
        time.sleep(20)
        try:
            ip = _lan_ip()
        except Exception:          # 后台线程里没人接异常
            continue
        if ip and ip != LAN_IP:
            LAN_IP = ip
            _refresh_hosts()
            print("局域网地址变成 http://%s:5000" % ip)

app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
# 静态文件同理：改完 CSS/JS 刷新就该看到，不要让人硬刷新才能验证
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
# 只认本机地址。不限 Host 的话，外部域名指向 127.0.0.1 就能以
# 同源身份读完所有会话正文。局域网 IP 也要列进来：手机用
# http://<本机IP>:5000 打开时 Host 头就是这个 IP，不认就 400。
_refresh_hosts()
threading.Thread(target=_watch_lan, daemon=True, name="lan-watch").start()
# 一份会话 JSON 再大也不该碰这个数；超了直接 413，不让它整份进内存
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

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
    "auto_roles": 1,        # AI 补设定时自己起几个角色；0 = 不起，用 {主角} 指代
    # AI 补设定的目标字数，三类各一组。换档位时和 context_limit 一样被重写
    "gen_role_min": 120, "gen_role_max": 180,    # 角色卡
    "gen_world_min": 200, "gen_world_max": 300,  # 世界观
    "gen_open_min": 160, "gen_open_max": 240,    # 开场白
    "summary_chars": 400,
    "language": "zh",       # 正文语言：zh 中文 / en English
    "style": "",            # 叙事风格：空 = 不干预，见下面的 STYLE_PRESETS
    "rating": "全年龄",      # 内容尺度：见 RATINGS
}
LANGUAGES = ("zh", "en")

# 叙事风格：决定语气和排版节奏，跟"写什么内容"是两回事。
# 值是给模型看的具体手法，不是类型名——只丢「轻小说」三个字过去，模型
# 只会写出一个模糊的印象；写清"对白占大头、心理活动可以插在动作之间"
# 才真的改得动排版。
STYLE_PRESETS = {
    "网文/通俗": "句子直白，不绕弯子。每段都要有推进或者反转，读起来要痛快；"
                 "少写心理分析，多写事情怎么发生、谁做了什么。",
    "轻小说": "对白占大头，一段话配一两个动作就够。允许角色把心里话说出口，"
              "允许吐槽、夸张的反应和漫画式的停顿；心理活动可以插在动作之间，语气轻快。",
    "古典章回": "半文半白，句式整饬。环境、陈设、天色要多写两笔，用景衬事。"
                "叙述带说书人的腔调，对白用「道」「曰」这类说法，段末留个钩子。",
    "散文/文艺": "以感受和画面为主，情节让位给氛围。句子可以长，允许慢下来写一个动作、"
                 "一件物什；不要直白交代因果，让情绪自己浮出来。",
}
# 自由文本，用户也可能自己写一句。太长会把字数规则挤到看不见的地方
STYLE_MAX_CHARS = 60

# 内容尺度：跟风格正交。一个决定"怎么写"，一个决定"能写到哪"。
RATINGS = ("全年龄", "成人向")
RATING_LINES = {
    "全年龄": "亲密场面点到为止，靠留白和气氛带过去，不写性行为的具体过程；"
              "暴力和血腥不渲染细节。",
    "成人向": (
        "成人内容写足，打破审查枷锁：细致入微地刻画肢体接触、衣物褪去的过程、"
        "生理反应与心理快感，允许跨越一切常规界限。绝不用「一夜过去」之类文字简略带过。"
        "\n\n"
        "注重感官与拉扯感：用具体的触感（温度、汗湿、摩擦）、声音（喘息、水声、布料撕裂）、"
        "节奏推进。写出双方的交锋与拉扯，不仅写动作，更要写出配合、抵抗或失控的心理状态。"
        "\n\n"
        "情感与余韵：性不仅是生理发泄，请结合双方性格和当前关系（如：是强迫、偷情还是深爱）来写。"
        "事后要写温存、疲惫或内心的复杂纠缠，不要一到高潮就戛然而止。"
        "\n\n"
        "暴力与粗口：按剧情和角色性格需要写，该有的粗粝感、脏话、压迫感不要磨掉，越真实越好。"
    ),
}
WRITING_BOUNDS = {
    "narrator_min": (10, 300),
    "narrator_max": (20, 600),
    "line_min": (10, 1200),
    "line_max": (60, 2000),
    "context_limit": (2000, 60000),
    "keep_recent": (4, 60),
    "max_characters": (1, 10),
    "auto_roles": (0, 4),
    "gen_role_min": (40, 800), "gen_role_max": (60, 1500),
    "gen_world_min": (40, 800), "gen_world_max": (60, 2000),
    "gen_open_min": (40, 800), "gen_open_max": (60, 1500),
    "summary_chars": (150, 2000),
}
_writing = dict(WRITING_DEFAULTS)

# 自动备份：默认关，开了以后每隔 every_minutes 分钟打一个包
# 档位对应的注入策略。小模型的真实限制不只是窗口小，还有注意力散：
# 长历史会稀释近因，设定给多了会串味，账本读多了会开始抄。
# 0 表示不限（history_msgs）或不截断（role_chars）。
# summary：每次压缩要把一大段剧情压成一段提要。上下文越大，
# 触发得越晚、一次压掉的量越多，提要还是 400 字就装不下了。
# 按上下文的 5% 取：3% 在高级档上是 800 字压三万，37:1，细节会掉。
# cells：一个角色最多维护几个状态格。这些格每轮都要重抄一遍，给多了
# 小模型会漏项、或者干脆缩成一句话交差；档位越高越吃得下，才放开。
TIER_LIMITS = {
    "basic": {"context": 8000, "history_msgs": 16, "role_chars": 200,
              "facts": 6, "anchor_every": 8, "attempts": 3, "summary": 400,
              "cells": 12},
    "standard": {"context": 16000, "history_msgs": 40, "role_chars": 400,
                 "facts": 12, "anchor_every": 15, "attempts": 3, "summary": 800,
                 "cells": 18},
    "advanced": {"context": 32000, "history_msgs": 0, "role_chars": 0,
                 "facts": 24, "anchor_every": 24, "attempts": 2, "summary": 1600,
                 "cells": 24},
}
TIER_CONTEXT = {k: v["context"] for k, v in TIER_LIMITS.items()}

BACKUP_DEFAULTS = {"enabled": False, "every_minutes": 1440, "keep": 30}
BACKUP_BOUNDS = {"every_minutes": (5, 20160), "keep": (1, 30)}   # 5 分钟 ~ 14 天

# 生图：本机 ComfyUI。url 留空就回退到 LT_COMFY 环境变量，再不行才用 127.0.0.1:8188。
IMAGE_DEFAULTS = {"url": "", "timeout": 180}
_image = dict(IMAGE_DEFAULTS)
IMAGE_BOUNDS = {"timeout": (30, 1200)}

# 语音走 vox-tts：C++ 引擎，不依赖 torch，权重 mmap 自单个 .tmod 文件。
#   pip install vox-tts                       # 轮子只到 cp313，主应用跑 3.14 时要另找解释器
#   python -m vox.build --model <HF 目录> --dtype bf16 --quant q8 -o models/x.tmod
# 主应用把它当子进程拉起来（python -m vox serve），自己只走 HTTP。
# 实测一句 6.3 秒音频合成 3.2 秒，首包 0.18 秒。
SPEECH_DEFAULTS = {
    # stream：边生成边读；manual：生成完再点消息右下角的按钮念。二选一
    "mode": "stream",
    # 装 vox-tts 的那个解释器。留空就自动找 3.13
    "python": "",
    # .tmod 模型；相对路径按项目根算
    "model": "models/qwen3-custom.tmod",
    # 已经不看了：合成改成走常驻进程的管道。留着是为了老配置不报错
    "url": "http://127.0.0.1:8190",
    "device": "auto",
    "threads": 0,
    "voice": "vivian",
    # 空闲多久把 vox 子进程收下来（秒），0 = 不收。它占着几 GB 显存
    "idle": 120,
    # 角色名 -> 音色，一行一个「角色=vivian」。* 是兜底，没配的角色走它
    "voices": "",
    # 风格名 -> 自然语言描述，一行一个「害羞=说话很小声，害羞，结巴」。
    # 交给模型当 instruct，同一音色能换语气
    "styles": "",
}
# 设备只认这三个。写 cuda:0 之类的会静默退回 CPU，一句要等三十秒
SPEECH_DEVICES = ("auto", "gpu", "cpu")
# 内置音色，和 vox 对上的那一批
SPEECH_VOICES = ("aiden", "dylan", "eric", "ono_anna", "ryan",
                 "serena", "sohee", "uncle_fu", "vivian")
# 正文语言 -> vox 的语言名
SPEECH_LANGS = {"zh": "chinese", "en": "english"}
SPEECH_BOUNDS = {"threads": (0, 64), "idle": (0, 3600)}

def language_of():
    """正文语言：zh / en。语音语言跟它走，不单独设。"""
    return _writing.get("language") or "zh"


SPEECH_READ_MODES = ("stream", "manual")
_speech = dict(SPEECH_DEFAULTS)
# 同一句话重复念很常见，开启缓存能省下重跑的时间
_speech_cache = True
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
# 每百万 token 的单价（输入, 输出, 命中缓存的输入），人民币元。
# 只用来给上下文面板算个量级，不是计费凭证；对不上就显示“未配单价”。
# 厂商随时调价，所以这里只是出厂默认值：设置界面的「模型单价」能逐条覆盖，
# 覆盖写进 settings.json，重启也在。数字以官方价目表为准。
# 单位一律人民币（元），每百万 token，且填的是**空闲时段**价。
# DeepSeek 官方按调用时刻分档：工作日 9:00-12:00、14:00-18:00 是高峰，价格翻倍，其余算空闲。
# 面板按当前时刻判档，所以同一局跨到高峰，后面的轮次会贵一倍。
# DeepSeek 两行数字抄自官网「模型细节」页；其余厂商按 7.2 折算，只为量级可比，不是账单。
# 每个模型自带币种，不做汇率折算：国内厂商标人民币，国外厂商标美元原价。
# DeepSeek 还按调用时刻分空闲/高峰两档，见 PEAK_HOURS。
PRICES = {
    "deepseek-flash": (1.00, 4.00, 0.02, "CNY"),
    "deepseek-v4-pro": (4.50, 13.50, 0.15, "CNY"),
    "deepseek-chat": (2.00, 8.00, 0.50, "CNY"),
    "deepseek-reasoner": (4.00, 16.00, 1.00, "CNY"),
    "gpt-4o": (2.50, 10.00, 1.25, "USD"),
    "gpt-5": (1.25, 10.00, 0.125, "USD"),
    "gpt-5-mini": (0.25, 2.00, 0.025, "USD"),
    "gpt-5-nano": (0.05, 0.40, 0.005, "USD"),
    "gpt-4o-mini": (0.15, 0.60, 0.075, "USD"),
    "o4-mini": (1.10, 4.40, 0.275, "USD"),
    "claude-3-5-sonnet": (3.00, 15.00, 0.30, "USD"),
    "claude-3-5-haiku": (0.80, 4.00, 0.08, "USD"),
    "qwen-max": (1.60, 6.40, 0.80, "USD"),
}

CURRENCIES = ("CNY", "USD")
CURRENCY_SIGN = {"CNY": "¥", "USD": "$"}

# 只有国内厂商按时段浮动，OpenAI 这类一个价走到底
PEAK_PREFIXES = ("deepseek",)

# 高峰时段：左闭右开。周末全天空闲。
PEAK_HOURS = ((9, 12), (14, 18))

def is_peak(at=None):
    """这一刻算不算高峰价。at 传时间戳可回放历史，不传就看现在。"""
    t = time.localtime(at) if at else time.localtime()
    if t.tm_wday >= 5:
        return False
    return any(lo <= t.tm_hour < hi for lo, hi in PEAK_HOURS)

_prices_override = {}
# 用户删掉的模型。默认表里还有，但用户说不要了，就别再冒出来
_prices_deleted = set()

def prices():
    """默认价目表叠上用户改过的。用户填的整行生效，不做逐字段合并。

    用户删掉的行记在 _prices_deleted 里：只在默认表里删不掉，
    下次回显又被默认值带回来，等于删了个寂寞。
    """
    out = {k: v for k, v in PRICES.items() if k not in _prices_deleted}
    out.update(_prices_override)
    return out

def _clean_rows(rows):
    """把 {名字: [...]} 清洗成 {名字: (输入, 输出, 缓存, 币种)}，认不出的丢。"""
    clean = {}
    for name, value in (rows or {}).items():
        if not name:
            continue
        vals = list(value)
        # 末尾要是 CNY / USD 就摘出来，不带就按人民币
        cur = "CNY"
        if vals and isinstance(vals[-1], str) and vals[-1].strip().upper() in CURRENCIES:
            cur = vals[-1].strip().upper()
            vals = vals[:-1]
        try:
            nums = [round(float(x), 6) for x in vals[:3]]
        except (TypeError, ValueError):
            continue
        if len(nums) < 2 or any(n < 0 for n in nums):
            continue
        # 缓存单价没填就按输入价走，宁可贵一点也别算成 0
        while len(nums) < 3:
            nums.append(nums[0])
        clean[name] = (nums[0], nums[1], nums[2], cur)
    return clean


def save_prices(data, persist=True, replace=False):
    """接受 {模型名: [输入, 输出, 缓存]} 或 {"模型名 输入 输出 缓存"} 的文本。

    文本形式是设置界面的表格用的：一行一条，空格或逗号分开，缓存单价可省。

    replace=True 表示这是界面上整张表提交：默认表里有、这次没交上来的模型
    会被记成「用户删掉了」，之后回显不再带出来。其它地方只想补几条覆盖时
    不要传它，否则会把默认表整个判死。
    """
    global _prices_override
    rows = {}
    if isinstance(data, str):
        for line in data.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"[\s,]+", line)
            if len(parts) < 3:
                continue
            rows[parts[0].lower()] = parts[1:]
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (list, tuple)):
                rows[str(key).strip().lower()] = list(value)
    clean = _clean_rows(rows)
    with _model_lock:
        _prices_override = clean
        # 只有界面整表提交（replace=True）才推断删除：没交上来的就是用户删的。
        # 别处调 save_prices 只是补几条覆盖，不能据此把默认表整个判死。
        if replace:
            for name in PRICES:
                if name in clean:
                    _prices_deleted.discard(name)
                else:
                    _prices_deleted.add(name)
        snapshot = dict(_prices_override)
    if persist:
        persist_settings()
    return snapshot

def _load_prices(data):
    """从设置文件载入覆盖项。不推导删除名单，那份单独存。"""
    global _prices_override
    rows = data
    if isinstance(data, str):
        rows = {}
        for line in data.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"[\s,]+", line)
            if len(parts) >= 3:
                rows[parts[0].lower()] = parts[1:]
    clean = _clean_rows(rows if isinstance(rows, dict) else {})
    with _model_lock:
        _prices_override = clean


def prices_text():
    """把当前价目表拍成一行一条的文本，给设置界面的输入框回显。"""
    return "\n".join("%s %g %g %g %s" % (name, nums[0], nums[1], nums[2], nums[3])
                     for name, nums in sorted(prices().items()))

def price_of(model):
    """按模型名前缀匹配单价；对不上返 None。

    长名字先匹配：gpt-5-mini 必须排在 gpt-5 前面，否则 mini 的价会被当成旗舰。
    """
    name = (model or "").strip().lower()
    if not name:
        return None
    table = prices()
    for key in sorted(table, key=len, reverse=True):
        if name.startswith(key):
            return table[key]
    return None

def estimate_cost(model, usage, at=None):
    """把 usage 换成一个每百万 token 单价的估算。拿不到单价就返 None。

    at 是这轮请求发出的时间戳；不传按现在判档。费用跟着时段走，所以同一局
    从空闲跑到高峰，后面的轮次会按高峰价算。
    """
    p = price_of(model)
    if not p or not isinstance(usage, dict):
        return None
    pin, pout, pcache, currency = p
    # 只有按时段浮动的厂商才乘倍率，OpenAI 这类一天一个价
    peakable = any((model or "").strip().lower().startswith(k) for k in PEAK_PREFIXES)
    rate = 2 if (peakable and is_peak(at)) else 1
    pin, pout, pcache = pin * rate, pout * rate, pcache * rate
    hit = int(usage.get("prompt_cache_hit_tokens")
              or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    in_tok = int(usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or 0)
    billed_in = max(0, in_tok - hit)
    total = billed_in / 1e6 * pin + hit / 1e6 * pcache + out_tok / 1e6 * pout
    return {
        "in": round(billed_in / 1e6 * pin, 6),
        "out": round(out_tok / 1e6 * pout, 6),
        "cached": round(hit / 1e6 * pcache, 6),
        "total": round(total, 6),
        "currency": currency,
        "peak": rate > 1,
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
    gen = gen_defaults(value)
    save_writing({"context_limit": TIER_LIMITS[value]["context"],
                  "keep_recent": tier_keep(value),
                  "summary_chars": TIER_LIMITS[value]["summary"], **gen},
                 persist=False)
    if persist:
        persist_settings()
    return tier()

def tier_limits(level=None):
    return TIER_LIMITS.get(level or tier(), TIER_LIMITS["basic"])

def tier_keep(level=None):
    """这档压缩后该留几条：跟历史能带的量走，高级档不限条数就用到上限。"""
    return tier_limits(level)["history_msgs"] or KEEP_RECENT_MAX

def gen_defaults(level=None):
    """这一档 AI 补设定的目标字数。角色卡受 role_chars 限制，写超了注入会被截断；
    世界观要铺时代地点规则，开场白要留出第一段互动，都比一张卡长，按 5:3 和 4:3 放。
    """
    cap = tier_limits(level)["role_chars"]
    lo, hi = (320, 520) if not cap else (max(60, cap * 6 // 10), max(120, cap * 9 // 10))
    return {"gen_role_min": lo, "gen_role_max": hi,
            "gen_world_min": lo * 5 // 3, "gen_world_max": hi * 5 // 3,
            "gen_open_min": lo * 4 // 3, "gen_open_max": hi * 4 // 3}

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

def image_conf():
    with _model_lock:
        return dict(_image)

def save_image_conf(data):
    if not isinstance(data, dict):
        return image_conf()
    with _model_lock:
        if "url" in data:
            _image["url"] = str(data.get("url") or "").strip()
        if "timeout" in data:
            try:
                n = int(data["timeout"])
            except (TypeError, ValueError):
                n = _image["timeout"]
            lo, hi = IMAGE_BOUNDS["timeout"]
            _image["timeout"] = max(lo, min(hi, n))
        snapshot = dict(_image)
    persist_settings()
    return snapshot

def speech_conf():
    with _model_lock:
        return dict(_speech)

def save_speech_conf(data):
    if not isinstance(data, dict):
        return speech_conf()
    with _model_lock:
        if data.get("mode") in SPEECH_READ_MODES:
            _speech["mode"] = data["mode"]
        for field in ("python", "model", "url", "voices", "styles"):
            if field in data:
                _speech[field] = str(data.get(field) or "").strip()
        # 设备和音色只能是白名单里的，填错了就回退默认
        if "device" in data:
            dev = str(data.get("device") or "").strip().lower()
            _speech["device"] = dev if dev in SPEECH_DEVICES else SPEECH_DEFAULTS["device"]
        if "voice" in data:
            v = str(data.get("voice") or "").strip().lower()
            _speech["voice"] = v if v in SPEECH_VOICES else SPEECH_DEFAULTS["voice"]
        for field in ("threads", "idle"):
            if field in data:
                lo, hi = SPEECH_BOUNDS[field]
                try:
                    _speech[field] = min(hi, max(lo, int(data.get(field) or 0)))
                except (TypeError, ValueError):
                    _speech[field] = SPEECH_DEFAULTS[field]
        # 模型或地址空了就退回默认，否则每次都起不来
        if not _speech.get("model"):
            _speech["model"] = SPEECH_DEFAULTS["model"]
        if not _speech.get("url"):
            _speech["url"] = SPEECH_DEFAULTS["url"]
        snapshot = dict(_speech)
    persist_settings()
    return snapshot

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

def writing():
    with _model_lock:
        return dict(_writing)

def save_writing(data, persist=True):
    """只改真的给了值的项；空串/0 视为「这次不动」，否则一次误传就把参数压到下限。"""
    with _model_lock:
        lang = (data or {}).get("language")
        if lang in LANGUAGES:
            _writing["language"] = lang
        # 写作风格是自由文本，既可以是 STYLE_PRESETS 里的名字，也可以自己写一句。
        # 空串是合法值（表示这轮不额外规定文风），所以不能走"空串视为没填"那条
        style = (data or {}).get("style")
        if isinstance(style, str):
            _writing["style"] = style.strip()[:STYLE_MAX_CHARS]
        rating = (data or {}).get("rating")
        if rating in RATINGS:
            _writing["rating"] = rating
        for key, value in (data or {}).items():
            if key not in WRITING_BOUNDS:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            lo, hi = WRITING_BOUNDS[key]
            # 下限是 0 的参数（比如自动起角色数）0 是合法值，不能当“没填”跳过
            if number <= 0 and lo > 0:
                continue
            _writing[key] = max(lo, min(hi, number))
        # min 撞上 max 就抬 max：不然提示词里会写出「800~60 字」
        for _lo_k, _hi_k in (("narrator_min", "narrator_max"),
                             ("line_min", "line_max"),
                             ("gen_role_min", "gen_role_max"),
                             ("gen_world_min", "gen_world_max"),
                             ("gen_open_min", "gen_open_max")):
            if _writing[_lo_k] > _writing[_hi_k]:
                _writing[_hi_k] = _writing[_lo_k]
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

# 最近一次写设置有没有失败。以前只在日志里留一句，界面照样弹「已保存」，
# 用户以为存住了，下次开还是旧的。api 层会拿它把响应改成 500。
_persist_error = ""


def persist_error():
    return _persist_error


def clear_persist_error():
    global _persist_error
    _persist_error = ""


def persist_settings():
    global _persist_error
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
        "prices": _prices_override or (prev.get("prices") or {}),
        "prices_deleted": sorted(_prices_deleted),
        "image": _image if _loaded else (prev.get("image") or dict(IMAGE_DEFAULTS)),
        "speech": _speech if _loaded else (prev.get("speech") or dict(SPEECH_DEFAULTS)),
    }
    tmp = "%s.%s.tmp" % (SETTINGS_PATH, uuid.uuid4().hex[:8])
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
        return True
    except OSError as exc:
        _persist_error = str(exc)
        app.logger.warning("保存设置失败：%s", exc)
        return False
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

    # 模型选择必须在读完提供方之后才校验：自定义提供方只存在于 providers 里。
    # 旧版本把 model 存成嵌套的 {provider, model}，现在是 provider 和 model 平级两个字段，两种都得认。
    # 之前只认嵌套形式，于是平级写法落进“字符串=旧格式”分支，provider 被强制改回 ollama，
    # 重启一次就把用户选的云端丢了。
    saved_model = data.get("model")
    saved_pid = data.get("provider")
    pid = name = ""
    if isinstance(saved_model, dict):
        pid = str(saved_model.get("provider") or "").strip()
        name = str(saved_model.get("model") or "").strip()
    elif isinstance(saved_model, str) and saved_model.strip():
        pid = str(saved_pid or "").strip() or DEFAULT_PROVIDER
        name = saved_model.strip()
    if name and pid and (pid in PROVIDERS or pid in _provider_overrides):
        _model_state["provider"] = pid
        _model_state["model"] = name

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
    for key, store, defaults in (("image", "_image", IMAGE_DEFAULTS),
                                ("speech", "_speech", SPEECH_DEFAULTS)):
        saved = data.get(key)
        if not isinstance(saved, dict):
            continue
        bucket = globals()[store]
        for field, default in defaults.items():
            if field in saved:
                bucket[field] = saved[field]

    # 盘上的旧结构要归一下：不然 persist_settings() 会把它原样写回去，
    # speech_conf() 里那套迁移就永远只能在读的时候救一下，写回去又是旧的
    if "mode" not in _speech and "stream" in _speech:
        _speech["mode"] = "stream" if _speech.pop("stream") else "manual"
    _speech.pop("stream", None)
    _speech.pop("enabled", None)
    if _speech.get("mode") not in SPEECH_READ_MODES:
        _speech["mode"] = SPEECH_DEFAULTS["mode"]
    # 盘上的 voco 时代的键（plugin/alias/options）直接丢掉，留着会被写回去污染配置
    for dead in ("plugin", "alias", "options", "language"):
        _speech.pop(dead, None)
    # device 写成 cuda:0 / gpu:0 会悄悄退回 CPU，30 秒才出一句。统一收成 auto
    if str(_speech.get("device") or "").strip().lower() not in ("auto", "gpu", "cpu", "vulkan"):
        _speech["device"] = SPEECH_DEFAULTS["device"]
    if not _speech.get("model"):
        _speech["model"] = SPEECH_DEFAULTS["model"]
    if not _speech.get("url"):
        _speech["url"] = SPEECH_DEFAULTS["url"]

    saved_deleted = data.get("prices_deleted")
    if isinstance(saved_deleted, list):
        global _prices_deleted
        _prices_deleted = {str(x).strip().lower() for x in saved_deleted if str(x).strip()}

    saved_prices = data.get("prices")
    if isinstance(saved_prices, (dict, str)):
        # 载入时不重新推导删除名单，用文件里记的那份
        _load_prices(saved_prices)

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
