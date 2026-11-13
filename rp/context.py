# -*- coding: utf-8 -*-
"""上下文统计与剧情压缩。"""
import threading
import time

from .characters import role_labels
from . import metrics
from .config import MAX_ENHANCE_ATTEMPTS, app, tier_limits, writing
from .llm import model_text
from .prompts import _tokens, build_context_debug, build_summary_prompt
from .snapshots import snapshot
from .storage import (
    IO_TIMING, get_session_lock, load_session, save_session,
)

# 到上限八成压。真等撞线才压，那一轮已经是贴着 num_ctx 在跑，
# 最早的历史被 Ollama 悄悄丢了，还没摘要兜着
COMPRESS_AT = 0.80




def context_tokens(sess):
    """这一局会发给模型的量，按 token 估。

    以前数的是字符，跟模型窗口不是一个量纲：中文一个字约 0.65 个 token，
    英文能到 1.3，同一套阈值在两种语言下差一倍。统一成 token 才对得上 num_ctx，
    也才看得出「还剩多少余量」。
    """
    total = 0
    for key in ("user_role", "ai_role", "world", "rules", "summary"):
        total += _tokens(sess.get(key) or "")
    for c in sess.get("characters") or []:
        if isinstance(c, dict):
            total += _tokens(c.get("name") or "") + _tokens(c.get("role") or "")
            # 只算会进 prompt 的那一条。state 就是 history 的最后一条，
            # 全量 history 早就不发给模型了，算进来是虚报
            state = (c.get("state") or "").strip()
            if not state and c.get("history"):
                state = (c["history"][-1] or "")
            total += _tokens(state)
    # 世界书、事实账本、状态行也会随消息一起发给模型，
    # 不计进来的话阈值会偏低，真正的 prompt 可能撞破模型窗。
    for e in sess.get("lore") or []:
        if isinstance(e, dict):
            total += _tokens(e.get("content") or "") + _tokens(e.get("keys") or "")
    for f in sess.get("facts") or []:
        if isinstance(f, dict):
            total += _tokens(f.get("fact") or "")
    st = sess.get("status")
    if isinstance(st, dict):
        for v in st.values():
            if isinstance(v, str):
                total += _tokens(v)
    # 已经写进摘要的那些不再计：它们不再发给模型，算进去会让阈值永远撞线
    start = int(sess.get("compressed_upto") or 0)
    for m in (sess.get("messages") or [])[start:]:
        total += _tokens(m.get("content") or "") + _tokens(m.get("narrator") or "")
    return total

def total_tokens(sess):
    """真正会发出去的量：会话数据 + system 模板。

    context_tokens 只数会话自己带的内容，system 那两千 token 是另外算的。
    压缩阈值和面板的占用条都该看这个总数——只看会话数据会以为还早，
    实际上已经贴着 num_ctx 在跑了。
    """
    body = context_tokens(sess)
    try:
        system = build_context_debug(sess).get("system") or ""
    except Exception:
        return body
    return body + _tokens(system)


def context_info(sess):
    """上下文用量与压缩统计，供前端展示。"""
    sess = sess or {}
    msgs = sess.get("messages") or []
    comp = sess.get("compress") or {}
    try:
        debug = build_context_debug(sess)
    except Exception:      # 检查器坏了也不能挡住正常对话
        debug = {}
    return {
        "used": total_tokens(sess),
        "limit": writing()["context_limit"],
        "debug": debug,
        "messages": len(msgs),
        # 读写一份会话 JSON 的耗时：超过 100ms 就该拆数据层了
        "io": dict(IO_TIMING),
        "stream": metrics.snapshot(),   # 上一轮的 prompt 大小与首字节耗时
        # 显示压缩真正会留下的条数，别跟实际行为对不上
        "keep_recent": max(writing()["keep_recent"],
                           tier_limits()["history_msgs"] or writing()["keep_recent"]),
        "summary_tokens": _tokens(sess.get("summary") or ""),
        "compress": {
            "times": int(comp.get("times") or 0),
            "dropped": int(comp.get("dropped") or 0),
            "dropped_tokens": int(comp.get("dropped_tokens") or 0),
            "updated_at": comp.get("updated_at") or 0,
        },
    }

# ============================== 剧情压缩 ==============================
def compress_session(sid, force=False, max_attempts=2):
    with get_session_lock(sid):
        sess = load_session(sid)
        if not sess:
            return False
        all_msgs = list(sess.get("messages") or [])
        start = int(sess.get("compressed_upto") or 0)
        start = min(max(start, 0), len(all_msgs))
        messages = all_msgs[start:]      # 只管还没摘要过的那一截
        # 压缩后留下的条数不能少于历史能带的条数，否则压了个寂寞。
        # 切档位时 save_tier 已把两者对齐，这里的 max 是给旧配置兜底
        keep = max(writing()["keep_recent"], tier_limits()["history_msgs"] or 0)
        limit = writing()["context_limit"]
        if len(messages) <= keep + 2:
            return False
        # 两个闸门，撞哪个都得压：
        # 只看字符数的话，短消息几十条就能把历史顶出去，而总字符还没到上限，
        # 于是压缩不触发、早期消息被 build_messages 悄悄截掉，连摘要都没有
        hist_cap = tier_limits()["history_msgs"]
        too_many = bool(hist_cap) and len(messages) > hist_cap
        if not force and total_tokens(sess) <= limit * COMPRESS_AT and not too_many:
            return False
        snapshot(sess, "compress")     # 压缩不可逆，先留退路
        # 切点对齐到一条用户发言，否则留下的大半是半截对话，
        # build_messages 还得再把开头那条 assistant 丢掉
        split = len(messages) - keep
        while split < len(messages) and messages[split].get("role") != "user":
            split += 1
        old = messages[:split]
        if len(old) < 2:
            return False
        prev_summary = sess.get("summary") or ""
        user_name, ai_name = role_labels(sess)
        snapshot_len = len(all_msgs)

    text = "\n".join(
        ("【旁白】%s\n" % m["narrator"] if m.get("narrator") else "")
        + "%s：%s" % (
            # 直接下标碰上缺字段的旧消息会抛 KeyError，
            # 而它在后台线程里，异常只落一条 warning，整次压缩就白跑了
            m.get("speaker") or (user_name if m.get("role") == "user" else ai_name),
            m.get("content") or "",
        )
        for m in old
    )
    prompt = build_summary_prompt(prev_summary, text)

    summary = ""
    for attempt in range(1, max_attempts + 1):
        try:
            summary = model_text(
                [{"role": "user", "content": prompt}],
                temperature=0.3 + 0.1 * (attempt - 1),
            )
        except Exception as exc:
            app.logger.warning("压缩第 %d 次失败：%s", attempt, exc)
            summary = ""
            continue
        if summary:
            break

    if not summary:
        return False

    with get_session_lock(sid):
        sess = load_session(sid)
        if not sess:
            return False
        current = sess.get("messages") or []
        if len(current) != snapshot_len:
            app.logger.info("压缩放弃：期间会话内容变化")
            return False
        new_start = start + split
        prev = sess.get("compress") or {}
        sess["compress"] = {
            "times": int(prev.get("times") or 0) + 1,
            "dropped": int(prev.get("dropped") or 0) + len(old),
            "dropped_tokens": int(prev.get("dropped_tokens") or 0) + _tokens(text),
            "updated_at": time.time(),
        }
        sess["summary"] = summary
        # 只把游标往前推，消息本身一条不删：界面要能一直回看，
        # 存档、导出、重写也都还是完整的
        sess["compressed_upto"] = new_start
        save_session(sess)
        return True


_compress_fail_at = {}       # sid -> 上次失败时间
_COMPRESS_RETRY_SECS = 300  # 失败了先歇五分钟，别每轮都白调一次模型

def background_compress(sid):
    last = _compress_fail_at.get(sid)
    if last and time.time() - last < _COMPRESS_RETRY_SECS:
        return

    def worker():
        try:
            compress_session(sid)
            _compress_fail_at.pop(sid, None)
        except Exception as exc:
            _compress_fail_at[sid] = time.time()
            app.logger.warning("后台压缩异常：%s", exc)
    threading.Thread(target=worker, daemon=True).start()
