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
_pending = {}          # 当前这一轮的中间状态：发出时间、首字节时间
LAST = {
    "prompt_chars": 0,     # 这一轮发给模型的全部字符数
    "system_chars": 0,     # 其中 system 部分
    "first_token_ms": 0,   # 从发请求到第一个 delta 的耗时
    "total_ms": 0,         # 整轮到 done
    "messages": 0,
}


def _path():
    return os.path.join(LOG_DIR, time.strftime("%Y-%m-%d") + ".log")


def note(event, **fields):
    """写一行 JSON 事件。任何异常都不往外抛。"""
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
    _pending.clear()
    _pending["t0"] = time.perf_counter()
    _pending["sid"] = session_id
    _pending["first"] = None
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
    if _pending.get("first") is not None:
        return
    t0 = _pending.get("t0")
    if t0 is None:
        return
    ms = int((time.perf_counter() - t0) * 1000)
    _pending["first"] = ms
    LAST["first_token_ms"] = ms
    note("chat.first_token", sid=_pending.get("sid"), ms=ms,
         prompt_chars=LAST["prompt_chars"])


def end(session_id, ok=True, **extra):
    """一轮结束：总耗时、首字节、prompt 大小一起记，方便算 prefill 占比。"""
    t0 = _pending.get("t0")
    total = int((time.perf_counter() - t0) * 1000) if t0 else 0
    LAST["total_ms"] = total
    note("chat.done", sid=session_id, ok=ok, total_ms=total,
         first_token_ms=LAST["first_token_ms"],
         prompt_chars=LAST["prompt_chars"], **extra)
    _pending.clear()


def snapshot():
    """面板要读的数字。"""
    out = dict(LAST)
    # 首 token 之后模型还在吐，用总耗时推个粗略的生成速度
    if out.get("total_ms") and out.get("first_token_ms"):
        out["decode_ms"] = max(0, out["total_ms"] - out["first_token_ms"])
    return out
