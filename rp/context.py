# -*- coding: utf-8 -*-
"""上下文统计与剧情压缩。"""
import threading
import time

from .characters import role_labels
from .config import MAX_ENHANCE_ATTEMPTS, app, writing
from .llm import model_text
from .prompts import build_context_debug, build_summary_prompt
from .snapshots import snapshot
from .storage import (
    get_session_lock, load_session, save_session,
)

def context_chars(sess):
    total = 0
    for key in ("user_role", "ai_role", "world", "rules", "summary"):
        total += len(sess.get(key) or "")
    for c in sess.get("characters") or []:
        if isinstance(c, dict):
            total += len(c.get("name") or "") + len(c.get("role") or "")
            total += len(c.get("state") or "")
            total += sum(len(h) for h in (c.get("history") or []) if isinstance(h, str))
    for m in sess.get("messages") or []:
        total += len(m.get("content") or "") + len(m.get("narrator") or "")
    return total


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
        "used": context_chars(sess),
        "limit": writing()["context_limit"],
        "debug": debug,
        "messages": len(msgs),
        "keep_recent": writing()["keep_recent"],
        "summary_chars": len(sess.get("summary") or ""),
        "compress": {
            "times": int(comp.get("times") or 0),
            "dropped": int(comp.get("dropped") or 0),
            "dropped_chars": int(comp.get("dropped_chars") or 0),
            "updated_at": comp.get("updated_at") or 0,
        },
    }

# ============================== 剧情压缩 ==============================
def compress_session(sid, force=False, max_attempts=2):
    with get_session_lock(sid):
        sess = load_session(sid)
        if not sess:
            return False
        messages = list(sess.get("messages") or [])
        keep = writing()["keep_recent"]
        limit = writing()["context_limit"]
        if len(messages) <= keep + 2:
            return False
        if not force and context_chars(sess) <= limit:
            return False
        snapshot(sess, "compress")     # 压缩不可逆，先留退路
        split = len(messages) - keep
        old = messages[:split]
        if len(old) < 2:
            return False
        prev_summary = sess.get("summary") or ""
        user_name, ai_name = role_labels(sess)
        snapshot_len = len(messages)

    text = "\n".join(
        ("【旁白】%s\n" % m["narrator"] if m.get("narrator") else "")
        + "%s：%s" % (
            m.get("speaker") or (user_name if m["role"] == "user" else ai_name),
            m["content"],
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
        prev = sess.get("compress") or {}
        sess["compress"] = {
            "times": int(prev.get("times") or 0) + 1,
            "dropped": int(prev.get("dropped") or 0) + len(old),
            "dropped_chars": int(prev.get("dropped_chars") or 0) + len(text),
            "updated_at": time.time(),
        }
        sess["summary"] = summary
        sess["messages"] = current[snapshot_len - keep:]
        save_session(sess)
        return True


def background_compress(sid):
    def worker():
        try:
            compress_session(sid)
        except Exception as exc:
            app.logger.warning("后台压缩异常：%s", exc)
    threading.Thread(target=worker, daemon=True).start()
