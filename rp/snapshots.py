# -*- coding: utf-8 -*-
"""会话快照：改动前后各留一份，崩了能退回上一版。

只保留最近 KEEP 份，文件名带时间戳与原因，排序即时间序。
"""
import json
import os
import time

from . import metrics
from .config import DATA_DIR

SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
KEEP = 10

import threading
_lock = threading.RLock()
os.makedirs(SNAP_DIR, exist_ok=True)

def _dir(sid):
    safe = "".join(ch for ch in (sid or "") if ch.isalnum() or ch in "-_")
    if not safe:
        return None
    path = os.path.join(SNAP_DIR, safe)
    os.makedirs(path, exist_ok=True)
    return path

def snapshot(sess, reason="manual"):
    """把整份会话存一份；名字里带时间和原因，方便人看。"""
    path = _dir((sess or {}).get("id"))
    if not path:
        return None
    metrics.note("snapshot.create", sid=sess.get("id"), reason=reason)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    tag = "".join(ch for ch in str(reason) if ch.isalnum()) or "manual"
    name = "%s-%s.json" % (stamp, tag)
    target = os.path.join(path, name)
    with _lock:
        try:
            with open(target, "w", encoding="utf-8") as f:
                json.dump(sess, f, ensure_ascii=False)
        except OSError:
            return None
        files = sorted(f for f in os.listdir(path) if f.endswith(".json"))
        for stale in files[:-KEEP]:
            try:
                os.remove(os.path.join(path, stale))
            except OSError:
                pass
    return name

def list_snapshots(sid):
    path = _dir(sid)
    if not path:
        return []
    items = []
    for fn in sorted((f for f in os.listdir(path) if f.endswith(".json")), reverse=True):
        full = os.path.join(path, fn)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        reason = fn[:-5].split("-", 2)[-1] if fn.count("-") >= 2 else "manual"
        items.append({"name": fn, "reason": reason, "size": stat.st_size,
                      "at": int(stat.st_mtime)})
    return items

def load_snapshot(sid, name):
    path = _dir(sid)
    if not path or "/" in name or "\\" in name or not name.endswith(".json"):
        return None
    full = os.path.join(path, name)
    if not os.path.isfile(full):
        return None
    try:
        with open(full, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
