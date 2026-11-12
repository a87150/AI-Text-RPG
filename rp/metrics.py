"""事件日志与最近一次请求的指标。

两个用途，一套代码：

1. 一行一个 JSON 追加到 logs/YYYY-MM-DD.log，出事时 grep 事件名就能定位
   （status.parse_fail 带原文、session.stale 带两边版本号）。不进 stdout，
   免得和 Flask 自己的输出混在一起。
2. 记住最近一次请求的几个关键数字（prompt 大小、首 token 耗时），上下文
   面板直接读，不用等日志。

写日志失败一律吞掉：观测不能反过来把主流程弄挂。
"""
import json
import os
import threading
import time

from .config import BASE_DIR

LOG_DIR = os.environ.get("LT_LOG_DIR") or os.path.join(BASE_DIR, "logs")

_lock = threading.Lock()
# 按会话分开存。两个会话同时聊天时，一个公共 dict 会让首字耗时
# 和总耗时互相覆盖，面板上的数字就不是这个会话的了。
_pending = {}          # sid -> {发出时间, 首字节时间}
LAST = {
    "prompt_chars": 0,     # 这一轮发给模型的全部字符数
    "system_chars": 0,     # 其中 system 部分
    "first_token_ms": 0,   # 从发请求到第一个 delta 的耗时
    "total_ms": 0,         # 整轮到 done
    "messages": 0,
    # 服务端报的真实用量；拿不到就是空字典，不要拿估算冒充
    "usage": {},
    "model": "",
    "cost": None,
}
# 累计：这一会话问过多少次、花了多少
TOTAL = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
         "cached_tokens": 0, "cost": 0.0, "ms": 0}


KEEP_DAYS = 14          # 一天一个文件，单日实测 400KB 左右，两周就是一个上限
_pruned = {"done": False}

def _path():
    return os.path.join(LOG_DIR, time.strftime("%Y-%m-%d") + ".log")

def _prune():
    """启动后第一次写日志时清一遍过期文件。

    不每次都扫：日志目录只会慢慢变。扫不到/删不掉都当没事，观测不能反过来弄排主流程。
    """
    if _pruned["done"]:
        return
    _pruned["done"] = True
    cutoff = time.time() - KEEP_DAYS * 86400
    try:
        for name in os.listdir(LOG_DIR):
            if not name.endswith(".log"):
                continue
            full = os.path.join(LOG_DIR, name)
            if os.path.getmtime(full) < cutoff:
                os.remove(full)
    except Exception:
        pass

def note(event, **fields):
    """写一行 JSON 事件。任何异常都不往外抛。"""
    _prune()
    try:
        fields["ts"] = round(time.time(), 3)
        fields["event"] = event
        line = json.dumps(fields, ensure_ascii=False, default=str)
        with _lock:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass

def begin(session_id, prompt_chars, system_chars, messages):
    """一轮请求开始：记下起点，首 token 的耗时从这一刻算。"""
    slot = _pending.setdefault(session_id or "", {})
    slot["t0"] = time.perf_counter()
    slot["first"] = None
    LAST.update({
        "prompt_chars": prompt_chars,
        "system_chars": system_chars,
        "first_token_ms": 0,
        "total_ms": 0,
        "messages": messages,
    })
    note("chat.request", sid=session_id, prompt_chars=prompt_chars,
         system_chars=system_chars, messages=messages)

def first_token():
    """第一个正文增量到了——用户真正感觉到的那段等待到此结束。"""
    for sid, slot in _pending.items():
        if slot.get("first") is not None or slot.get("t0") is None:
            continue
        ms = int((time.perf_counter() - slot["t0"]) * 1000)
        slot["first"] = ms
        LAST["first_token_ms"] = ms
        note("chat.first_token", sid=sid, ms=ms, prompt_chars=LAST["prompt_chars"])
        return

def end(session_id, ok=True, **extra):
    """一轮结束：总耗时、首字节、prompt 大小一起记，方便算 prefill 占比。"""
    slot = _pending.pop(session_id or "", {})
    t0 = slot.get("t0")
    total = int((time.perf_counter() - t0) * 1000) if t0 else 0
    LAST["total_ms"] = total
    note("chat.done", sid=session_id, ok=ok, total_ms=total,
         first_token_ms=LAST["first_token_ms"],
         prompt_chars=LAST["prompt_chars"], **extra)

def record(usage, model=""):
    """一轮结束后把用量记进来，顺带算个估算费用。"""
    from .config import estimate_cost
    usage = usage if isinstance(usage, dict) else {}
    LAST["usage"] = dict(usage)
    LAST["model"] = model or LAST.get("model") or ""
    LAST["cost"] = estimate_cost(LAST["model"], usage)
    hit = int(usage.get("prompt_cache_hit_tokens")
              or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    TOTAL["calls"] += 1
    TOTAL["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
    TOTAL["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    TOTAL["cached_tokens"] += hit
    if LAST["cost"]:
        TOTAL["cost"] = round(TOTAL["cost"] + LAST["cost"]["total"], 6)
    note("chat.usage", model=LAST["model"], usage=usage, cost=LAST["cost"])

def snapshot():
    """面板要读的数字。"""
    out = dict(LAST)
    # 首 token 之后模型还在吐，用总耗时推个粗略的生成速度
    if out.get("total_ms") and out.get("first_token_ms"):
        out["decode_ms"] = max(0, out["total_ms"] - out["first_token_ms"])
    out["total"] = dict(TOTAL)
    return out
