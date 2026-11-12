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
        # cover 只存文件名，不存路径：路径由 DATA_DIR 推，换机器能跑
        "cover": "",
        # 生图的提示词要留一份，否则下次想重来就得重现想词
        "cover_prompt": "",
        "cover_history": [],
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


COVER_HISTORY_MAX = 6


def set_cover(story, name, prompt=None):
    """换封面。旧的不删，压进历史，撤销就是把它拿回来。"""
    if not story:
        return None
    old = story.get("cover") or ""
    if old and old != name:
        hist = list(story.get("cover_history") or [])
        hist.insert(0, {"name": old, "prompt": story.get("cover_prompt") or "",
                        "ts": time.time()})
        story["cover_history"] = hist[:COVER_HISTORY_MAX]
    story["cover"] = name or ""
    if prompt is not None:
        story["cover_prompt"] = prompt
    return story


def undo_cover(story):
    """回到上一张。没有上一张就清空，不报错。"""
    if not story:
        return None
    from . import covers as cover_store

    hist = list(story.get("cover_history") or [])
    dropped = story.get("cover") or ""
    if not hist:
        story["cover"] = ""
    else:
        top = hist.pop(0)
        story["cover"] = top.get("name") or ""
        if top.get("prompt"):
            story["cover_prompt"] = top["prompt"]
        story["cover_history"] = hist
    # 被撤掉的那张没人用了。不删的话每撤一次多一份三百 KB，且再也回不到历史里。
    if dropped:
        cover_store.drop(dropped)
    return story



def cover_list(story):
    """当前这张加历史里的，按"现在用的在最前"排。"""
    if not story:
        return []
    from . import covers as cover_store

    out = []
    cur = story.get("cover") or ""
    if cur and cover_store.exists(cur):
        out.append({"name": cur, "prompt": story.get("cover_prompt") or "",
                    "thumb": cover_store.url_thumb(cur),
                    "url": cover_store.url_of(cur), "current": True})
    for h in story.get("cover_history") or []:
        n = h.get("name") or ""
        if not n or n == cur or not cover_store.exists(n):
            continue
        out.append({"name": n, "prompt": h.get("prompt") or "",
                    "thumb": cover_store.url_thumb(n),
                    "url": cover_store.url_of(n), "current": False})
    return out


def pick_cover(story, name):
    """把历史里的某张拿回来当封面。当前这张压回历史，不丢。"""
    if not story or not name:
        return None
    from . import covers as cover_store

    if not cover_store.exists(name):
        return None
    hist = list(story.get("cover_history") or [])
    keep = [h for h in hist if (h.get("name") or "") != name]
    if story.get("cover"):
        keep.insert(0, {"name": story["cover"], "prompt": story.get("cover_prompt") or ""})
    picked = next((h for h in hist if (h.get("name") or "") == name), {})
    story["cover"] = name
    story["cover_prompt"] = picked.get("prompt") or story.get("cover_prompt") or ""
    story["cover_history"] = keep[:COVER_HISTORY_MAX]
    return story


def forget_cover(story, name):
    """把某张从这份设定里彻底删掉，连图一起。"""
    if not story or not name:
        return None
    from . import covers as cover_store

    hist = [h for h in (story.get("cover_history") or []) if (h.get("name") or "") != name]
    if (story.get("cover") or "") == name:
        if hist:
            top = hist.pop(0)
            story["cover"] = top.get("name") or ""
            story["cover_prompt"] = top.get("prompt") or ""
        else:
            story["cover"] = ""
    story["cover_history"] = hist
    cover_store.drop(name)
    return story

def cover_prompt_from(story):
    """把故事的设定拼成一句能直接喂给生图模型的提示词。

    只取前两百字：世界观往往前面就把场景说完了，再往后是规则条款，
    那些对画面没帮助，反而会把模型带到写字上去。
    """
    if not story:
        return ""
    bits = []
    title = (story.get("title") or "").strip()
    world = (story.get("world") or "").strip()
    if world:
        bits.append(world[:200])
    elif title:
        bits.append(title)
    names = []
    for c in (story.get("characters") or [])[:3]:
        if isinstance(c, dict) and c.get("name"):
            role = (c.get("role") or "").strip()
            names.append("%s（%s）" % (c["name"], role) if role else c["name"])
    if names:
        bits.append("出场：" + "、".join(names))
    if not bits:
        return ""
    # 用空格接：世界观尾已经带标点，再拼顿号会出现"。、"
    text = " ".join(b.strip().rstrip("。．.") for b in bits if b.strip())
    return text + "。电影感构图，氛围强，细节丰富。"


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



def _live_cover(name):
    """存的是文件名，但文件可能已经不在了。不在就当没封面。"""
    if not name:
        return ""
    from . import covers as cover_store
    return name if cover_store.exists(name) else ""


def _live_thumb(name):
    """列表用的小图地址。没小图就空着，前端自己回退到原图。"""
    name = _live_cover(name)
    if not name:
        return ""
    from . import covers as cover_store
    return cover_store.url_thumb(name)

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
                "cover": _live_cover(story.get("cover")),
                "thumb": _live_thumb(story.get("cover")),
                "cover_prompt": story.get("cover_prompt") or "",
                "cover_history_count": len(story.get("cover_history") or []),
            })
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return items


def delete_story(sid):
    path = story_path(sid)
    if not path or not os.path.isfile(path):
        return False
    story = load_story(sid) or {}
    with _lock:
        try:
            os.remove(path)
        except OSError:
            return False
    # 故事没了，它独占的封面也没人用了。历史里那几张一样清掉，
    # 不然删完故事还有图躺在 covers 里。
    from . import covers as cover_store
    names = [story.get("cover") or ""]
    names += [h.get("name") or "" for h in (story.get("cover_history") or [])]
    for name in names:
        cover_store.drop(name)
    return True

def update_from(story, sess):
    for key in STORY_FIELDS:
        story[key] = sess.get(key) or ""
    story["characters"] = json.loads(json.dumps(sess.get("characters") or []))
    story["lore"] = json.loads(json.dumps(sess.get("lore") or []))
    # 角色表是唯一来源，第一个角色就是「AI 角色」
    # 先过滤非 dict：请求里一个字符串就能把这里炸成 500
    dicts = [c for c in (story.get("characters") or []) if isinstance(c, dict)]
    first = dicts[0] if dicts else {}
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
