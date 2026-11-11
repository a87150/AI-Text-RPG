# -*- coding: utf-8 -*-
"""故事：可复用的设定模板，一个故事能开多个对话。"""
import json
import os
import threading
import time
import uuid

from .config import DATA_DIR
from .storage import DEFAULT_TITLE, SID_RE

STORY_DIR = os.path.join(DATA_DIR, "stories")
STORY_FIELDS = ("title", "user_name", "user_role", "world", "rules", "opening")

_lock = threading.RLock()
os.makedirs(STORY_DIR, exist_ok=True)


def story_path(sid):
    if not sid or not SID_RE.fullmatch(sid):
        return None
    return os.path.join(STORY_DIR, sid + ".json")


def new_story(title=DEFAULT_TITLE):
    now = time.time()
    return {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "user_name": "", "ai_name": "", "user_role": "", "ai_role": "",
        "world": "", "rules": "", "opening": "",
        "characters": [],
        "created_at": now, "updated_at": now,
    }


def load_story(sid):
    path = story_path(sid)
    if not path or not os.path.isfile(path):
        return None
    with _lock:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
    return data if isinstance(data, dict) else None


def save_story(story):
    path = story_path(story.get("id"))
    if not path:
        raise ValueError("非法的故事 ID")
    with _lock:
        story["updated_at"] = time.time()
        tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex[:8])
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(story, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return story


def list_stories():
    with _lock:
        try:
            files = [f for f in os.listdir(STORY_DIR) if f.endswith(".json")]
        except OSError:
            files = []
    items = []
    for fn in files:
        story = load_story(fn[:-5])
        if story:
            items.append({
                "id": story["id"],
                "title": story.get("title") or DEFAULT_TITLE,
                "updated_at": story.get("updated_at", 0),
                "user_name": story.get("user_name") or "",
                "ai_name": story.get("ai_name") or "",
                "character_count": len(story.get("characters") or []),
            })
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return items


def delete_story(sid):
    path = story_path(sid)
    if not path or not os.path.isfile(path):
        return False
    with _lock:
        try:
            os.remove(path)
        except OSError:
            return False
    return True

def update_from(story, sess):
    for key in STORY_FIELDS:
        story[key] = sess.get(key) or ""
    story["characters"] = json.loads(json.dumps(sess.get("characters") or []))
    story["lore"] = json.loads(json.dumps(sess.get("lore") or []))
    # 角色表是唯一来源，第一个角色就是「AI 角色」
    first = story["characters"][0] if story["characters"] else {}
    story["ai_name"] = first.get("name") or ""
    story["ai_role"] = first.get("role") or ""
    story["title"] = (sess.get("title") or story.get("title") or DEFAULT_TITLE).strip()
    story["title"] = story["title"] or DEFAULT_TITLE
    return story


def apply_to(sess, story):
    """用故事模板填一个空会话；对话内容（消息/前情/回合）保持干净。"""
    for key in STORY_FIELDS:
        if key == "title":
            continue
        sess[key] = story.get(key) or ""
    sess["title"] = (story.get("title") or DEFAULT_TITLE).strip() or DEFAULT_TITLE
    sess["characters"] = json.loads(json.dumps(story.get("characters") or []))
    sess["lore"] = json.loads(json.dumps(story.get("lore") or []))
    sess["story_id"] = story.get("id") or ""
    sess["messages"] = []
    sess["summary"] = ""
    sess["turn"] = 0
    return sess

PLACEHOLDER = "{主角}"   # 设定里的主角占位符


def fill_names(text, lead=""):
    """把设定里的 {主角} 换成这一局实际的名字。

    模板一层只写占位符，运行时才知道真名；换名只改角色表，世界观和开场白自动跟上，
    不用全局替换。只认带花括号的写法，裸的“主角”不动，免得把普通词也替了。
    """
    if not text:
        return text
    lead = (lead or "").strip()
    if not lead:
        return str(text)
    return str(text).replace(PLACEHOLDER, lead)
