# -*- coding: utf-8 -*-
"""会话文件的读写、锁与忙标记。"""
import json
import os
import re
import threading
import time
import uuid

from .config import DATA_DIR, mode_for

# 进程级锁：按会话 ID 分锁，写文件串行
_file_lock = threading.RLock()
_session_locks = {}
_session_locks_guard = threading.Lock()
_busy_sessions = set()
_busy_guard = threading.Lock()


TEXT_FIELDS = (
    "title", "user_name", "ai_name", "user_role", "ai_role",
    "world", "rules", "opening", "summary",
)
DEFAULT_TITLE = "未命名故事"


def get_session_lock(sid):
    with _session_locks_guard:
        lock = _session_locks.get(sid)
        if lock is None:
            lock = threading.RLock()
            _session_locks[sid] = lock
        return lock


def try_acquire_busy(sid):
    with _busy_guard:
        if sid in _busy_sessions:
            return False
        _busy_sessions.add(sid)
        return True


def release_busy(sid):
    with _busy_guard:
        _busy_sessions.discard(sid)


# ============================== 存储层 ==============================
SID_RE = re.compile(r"[0-9a-f]{12}")


def session_path(sid):
    if not isinstance(sid, str) or not SID_RE.fullmatch(sid):
        return None
    return os.path.join(DATA_DIR, sid + ".json")


class StaleSession(Exception):
    """磁盘上的版本比调用方拿着的新：另一个标签页先写了。"""

    def __init__(self, expected, actual):
        super().__init__("会话已在别处更新")
        self.expected = expected
        self.actual = actual


def current_stamp(sid):
    """读磁盘上的 updated_at，不建立完整对象。"""
    path = session_path(sid)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("updated_at") if isinstance(data, dict) else None


# 最近一次读/写耗时（毫秒），供上下文面板显示。
IO_TIMING = {"load_ms": 0.0, "save_ms": 0.0, "bytes": 0}


def save_session(sess, expect=None):
    """写盘。expect 给就是乐观锁：磁盘上的 updated_at 和它对不上就报 StaleSession。

    只有用户交互式的写（改设定、改消息、半截入库）需要传；后端自己在锁内 load 完立刻写的路径不需要，竞争窗口已经被锁封住。
    """
    sid = sess.get("id")
    path = session_path(sid)
    if not path:
        raise ValueError("非法的会话 ID")
    with get_session_lock(sid):
        if expect is not None:
            actual = current_stamp(sid)
            if actual is not None and abs(float(actual) - float(expect)) > 1e-6:
                raise StaleSession(expect, actual)
        sess["updated_at"] = time.time()
        # 每次写入用独立临时名，避免并发写同一 .tmp 把文件写坏
        tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex[:8])
        try:
            _t0 = time.perf_counter()
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sess, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            IO_TIMING["save_ms"] = (time.perf_counter() - _t0) * 1000
            IO_TIMING["bytes"] = os.path.getsize(path) if os.path.isfile(path) else 0
        finally:
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def load_session(sid):
    path = session_path(sid)
    if not path or not os.path.isfile(path):
        return None
    with get_session_lock(sid):
        try:
            _t0 = time.perf_counter()
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            IO_TIMING["load_ms"] = (time.perf_counter() - _t0) * 1000
            IO_TIMING["bytes"] = os.path.getsize(path)
            return data
        except (OSError, json.JSONDecodeError):
            return None


def new_session(title=DEFAULT_TITLE, mode="normal"):
    now = time.time()
    return {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "mode": mode,        # 一个对话固定一种模式
        "story_id": "",      # 设定来自哪个故事，可复用开多局
        "user_name": "", "ai_name": "",
        "user_role": "", "ai_role": "",
        "world": "", "rules": "", "opening": "", "summary": "",
        "messages": [],
        "characters": [],   # [{name, role, state, history}]
        "turn": 0,          # 轮流发言进度
        "created_at": now, "updated_at": now,
    }


def list_sessions():
    with _file_lock:
        try:
            files = [f for f in os.listdir(DATA_DIR) if f.endswith(".json")]
        except OSError:
            files = []
    items = []
    for fn in files:
        sid = fn[:-5]
        if not SID_RE.fullmatch(sid):
            continue
        s = load_session(sid)
        if not s:
            continue
        items.append({
            "id": s["id"],
            "title": s.get("title") or DEFAULT_TITLE,
            "updated_at": s.get("updated_at", 0),
            "message_count": len(s.get("messages") or []),
            "has_summary": bool(s.get("summary")),
            "ai_name": s.get("ai_name") or "",
            "user_name": s.get("user_name") or "",
            "mode": mode_for(s),
            "story_id": s.get("story_id") or "",
        })
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return items
