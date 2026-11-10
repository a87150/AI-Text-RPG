# -*- coding: utf-8 -*-
"""角色表：新增、轮换、设定演进，以及模型输出的拆分。"""
import re
from difflib import SequenceMatcher

from .config import mode_for, writing
from .llm import (extract_json, extract_json_all, loose_status_json,
                  strip_status_leaks, strip_thinking)

# ============================== 提示词构造 ==============================
def guess_name(text):
    if not text:
        return ""
    m = re.search(r"(?:姓名|名字|名称|角色名|代号)[：:]\s*([^\n，,。；;、\s]{1,20})", text)
    return m.group(1).strip() if m else ""


CHARACTER_HISTORY_KEEP = 4    # 每个角色保留的最近变化条数


def default_character(sess):
    name = (sess.get("ai_name") or "").strip() \
        or guess_name(sess.get("ai_role", "")) or "AI 角色"
    return {
        "name": name,
        "role": (sess.get("ai_role") or "").strip(),
        "state": "",
        "history": [],
    }


def ensure_characters(sess):
    """归一化角色表；老会话没有角色表时用 ai_name/ai_role 兜底成一个。"""
    chars = sess.get("characters")
    cleaned = []
    if isinstance(chars, list):
        for item in chars:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()[:24]
            if not name:
                continue
            cleaned.append({
                "name": name,
                "role": (item.get("role") or "").strip()[:400],
                "state": (item.get("state") or "").strip()[:200],
                "history": [h for h in (item.get("history") or [])
                            if isinstance(h, str)][-CHARACTER_HISTORY_KEEP:],
            })
    if not cleaned:
        cleaned = [default_character(sess)]
    sess["characters"] = cleaned
    return cleaned


def next_speaker(sess):
    """轮流发言：由代码决定，模型只负责内容。一般模式没有发言人。"""
    if mode_for(sess) == "normal":
        return ""
    chars = ensure_characters(sess)
    on, _off = scene_split(chars, sess.get("status"))
    pool = on or chars              # 全离场（状态写坏）时退回全体，总得有人说话
    turn = int(sess.get("turn") or 0)
    return pool[turn % len(pool)]["name"]


def advance_turn(sess):
    sess["turn"] = int(sess.get("turn") or 0) + 1


def upsert_character(sess, data):
    """登记新角色 / 更新已有角色的设定与变化，返回 (名字, 是否新增)。"""
    if not isinstance(data, dict):
        return None
    name = (data.get("name") or "").strip()[:24]
    if not name:
        return None
    chars = ensure_characters(sess)
    for c in chars:
        if c["name"] == name:
            # 已存在的角色：只让模型推进状态，人设文字归用户所有，不被覆盖
            change = (data.get("change") or "").strip()
            if change:
                c["history"] = (c["history"] + [change[:120]])[-CHARACTER_HISTORY_KEEP:]
                c["state"] = change[:120]
            return (name, False)
    if len(chars) >= writing()["max_characters"]:
        # 满了不要静默丢掉：记下来让前端问用户是踢掉谁还是不要
        sess["pending_character"] = {
            "name": name,
            "role": (data.get("role") or "").strip()[:400],
            "state": (data.get("relation") or "").strip()[:120],
        }
        return None
    chars.append({
        "name": name,
        "role": (data.get("role") or "").strip()[:400],
        "state": (data.get("relation") or "").strip()[:120],
        "history": [],
    })
    return (name, True)


def apply_change(sess, data):
    if not isinstance(data, dict):
        return None
    name = (data.get("name") or "").strip()[:24]
    change = (data.get("change") or "").strip()
    if not name or not change:
        return None
    chars = ensure_characters(sess)
    for c in chars:
        if c["name"] == name:
            c["history"] = (c["history"] + [change[:120]])[-CHARACTER_HISTORY_KEEP:]
            c["state"] = change[:120]
            return name
    return None


def role_labels(sess):
    ai_name = (sess.get("ai_name") or "").strip() \
        or guess_name(sess.get("ai_role", "")) or "AI 角色"
    user_name = (sess.get("user_name") or "").strip() \
        or guess_name(sess.get("user_role", "")) or "用户角色"
    return user_name, ai_name
SEG_RE = re.compile(r"【([^】]{1,24})】")
STATUS_RE = re.compile(r"<StatusBlock>(.*?)</StatusBlock>", re.S | re.I)
# 单行写法：[[地点：银月酒馆|时间：深夜|关系：戒备|物品：湿伞|目标：等对方开口|事实：甲；乙]]
MARK_KEYS_RX = "在场|离场|退场|地点|位置|场景|时间|关系|人物|物品|携带|目标|意图|事实"
# 模型常常把 [[…]] 写成【…】，两个竖线还在；不认这种写法的话整行状态就丢了
MARK_RE = re.compile(
    r"^[ \t]*\[\[([^\[\]]{2,400})\]\][ \t]*$"
    r"|^[ \t]*【((?:" + MARK_KEYS_RX + r")[：:][^【】]{0,380})】[ \t]*$",
    re.M,
)
MARK_KEYS = {
    "地点": "location", "位置": "location", "场景": "location",
    "时间": "time",
    "关系": "relations", "人物": "relations",
    "物品": "items", "携带": "items",
    "目标": "goal", "意图": "goal",
    "事实": "facts",
    # 谁在这一幕里：角色会随剧情进出场景，不能永远全员在场
    "在场": "present", "离场": "away", "退场": "away",
}
NAME_SPLIT_RE = re.compile(r"[,，、/]")


def _take_mark_line(text):
    """剥掉 [[…]] 状态行并解析成 status；格式不对就整行丢弃。"""
    src = text or ""
    match = MARK_RE.search(src)
    if not match:
        return src, {}
    src = (src[:match.start()] + src[match.end():]).strip()
    status = {}
    for chunk in re.split(r"[|｜]", match.group(1) or match.group(2) or ""):
        if "：" in chunk:
            key, value = chunk.split("：", 1)
        elif ":" in chunk:
            key, value = chunk.split(":", 1)
        else:
            continue
        key = MARK_KEYS.get(key.strip())
        value = value.strip()
        if not key or not value:
            continue
        if key == FACT_KEY:
            got = split_facts(value)
            if got:
                status[FACT_KEY] = got
        elif key == "items":
            status[key] = _parse_items(value)
        elif key in ("present", "away"):
            names = [n.strip() for n in NAME_SPLIT_RE.split(value) if n.strip()]
            if names:
                status[key] = names[:writing()["max_characters"]]
        else:
            status[key] = value
    return src, status


def _parse_items(value):
    """物品可以绑到具体人身上：物品：玉芬=纱裙、皮鞭；谭谈=手机。

    没写等号的当成公共物品整串留着，跟以前的写法兼容。
    返回 dict（人名 -> 物品）或原来的字符串。
    """
    parts = [p.strip() for p in re.split(r"[；;]", value or "") if p.strip()]
    grouped, loose = {}, []
    for part in parts:
        if "=" in part or "＝" in part:
            name, own = (part.replace("＝", "=").split("=", 1) + [""])[:2]
            name, own = name.strip(), own.strip()
            if name and own:
                grouped[name] = own
                continue
        loose.append(part)
    if grouped:
        if loose:
            grouped[""] = "、".join(loose)
        return grouped
    return value


def merge_status(old, new):
    """状态是累积的：这一轮没写到的字段沿用上一轮，别把已有信息清空。

    小模型经常只写两三个字段，整体覆盖会让地点、在场名单来回丢。
    """
    out = dict(old or {})
    new = new or {}
    on_off = ("present", "away")
    if any(new.get(k) for k in on_off):     # 在场名单成对更新，避免只写一半
        for k in on_off:
            names = [n for n in (new.get(k) or []) if n]
            if names or k in new:
                out[k] = names
    else:
        for k in on_off:
            if old and k in old:
                out[k] = old[k]
    for key, value in new.items():
        if key in on_off:
            continue
        if not value:
            continue
        # 物品是按人分的字典：新的一轮可能只提到一个人，别把别人的东西冲掉
        if key == "items" and isinstance(value, dict):
            merged = dict(out.get("items") or {}) if isinstance(out.get("items"), dict) else {}
            merged.update({k: v for k, v in value.items() if v})
            out[key] = merged
            continue
        out[key] = value
    return out


def scene_split(chars, status):
    """把角色分成「这一幕在场」和「这一幕不在场」。没标过就都算在场。"""
    status = status or {}
    present = [n for n in (status.get("present") or []) if n]
    away = [n for n in (status.get("away") or []) if n]
    if not present and not away:
        return list(chars), []
    if present:                       # 明确列了在场，其余都算离场
        on = [c for c in chars if c["name"] in present]
        off = [c for c in chars if c["name"] not in present]
        return on, off
    return [c for c in chars if c["name"] not in away], [c for c in chars if c["name"] in away]


STATUS_FIELDS = ("location", "time", "relations", "items", "goal")
# 状态块里额外允许一条事实账本：模型每轮声明"刚刚定下的事"
FACT_KEY = "facts"


def extract_status(text):
    """取出状态块与事实行，并从正文里删掉它们。

    事实两种写法都认：StatusBlock 里的 facts 数组，或正文末尾单独一行
    「【事实】甲；乙」。小模型维护 JSON 数组会犹豫，纯文本行反而稳定。
    """
    src = text or ""
    # 先认新的单行写法：[[地点：X|时间：Y|…]]，小模型写这个比写 XML 稳
    src, mark_status = _take_mark_line(src)
    match = STATUS_RE.search(src)
    if match:
        cleaned = STATUS_RE.sub("", src).strip()
        obj = extract_json(match.group(1)) or {}
    else:
        # 没写标签、直接把 JSON 甩在正文里的情况也要接住
        cleaned = src
        loose = loose_status_json(src)
        obj = extract_json(loose) or {} if loose else {}
    facts = obj.get(FACT_KEY)
    if isinstance(facts, str):
        facts = [facts]
    facts = [str(f).strip()[:120] for f in facts if str(f).strip()] if isinstance(facts, list) else []
    status = {}
    if facts:
        status[FACT_KEY] = facts[:3]
    for key in ("present", "away"):
        names = obj.get(key)
        if isinstance(names, str):
            names = NAME_SPLIT_RE.split(names)
        if isinstance(names, list):
            names = [str(n).strip()[:24] for n in names if str(n).strip()]
            if names:
                status[key] = names[:writing()["max_characters"]]
    for key in STATUS_FIELDS:
        value = obj.get(key)
        if isinstance(value, list):
            value = "，".join(str(v).strip() for v in value if str(v).strip())
        elif value is not None:
            value = str(value).strip()
        if value:
            status[key] = value[:160]
    for key, value in (mark_status or {}).items():
        if key == FACT_KEY:
            status[FACT_KEY] = (status.get(FACT_KEY) or []) + value
            status[FACT_KEY] = status[FACT_KEY][:3]
        elif key not in status:
            if key in ("present", "away"):
                names = [str(n).strip()[:24] for n in value if str(n).strip()] \
                    if isinstance(value, list) else [str(value).strip()]
                if names:
                    status[key] = names[:writing()["max_characters"]]
            elif key == "items":
                status[key] = value          # {人名: 东西}，别按字符串切
            else:
                status[key] = str(value)[:160]
    line = FACT_LINE_RE.search(cleaned)
    if line:
        cleaned = (cleaned[:line.start()] + cleaned[line.end():]).strip()
        extra = split_facts(line.group(1))
        if extra:
            status[FACT_KEY] = (status.get(FACT_KEY) or []) + extra
            status[FACT_KEY] = status[FACT_KEY][:3]
    # 兜底：模型常把状态写成裸 JSON 或半截标签，别让它们留在正文里
    cleaned = strip_status_leaks(cleaned)
    return cleaned, (status or None)


FACT_LINE_RE = re.compile(r"^[ \t]*【事实】[ \t]*[:：]?[ \t]*(.+)$", re.M)


def split_facts(text):
    """一句一条，太短的丢掉，最多三条。"""
    parts = re.split(r"[；;。\n]", text or "")
    out = []
    for part in parts:
        item = part.strip(" ·、,，\t")
        if len(item) >= 4:
            out.append(item[:120])
    return out[:3]


FACTS_MAX = 40
FACTS_KEEP = 12


def _norm(text):
    return re.sub(r"\s+", "", (text or "").lower())


def merge_facts(sess, new_facts, turn=None):
    """把这一轮声明的事实并进账本：相似的旧事实让位，总数封顶。"""
    if not new_facts:
        return []
    book = sess.setdefault("facts", [])
    if not isinstance(book, list):
        book = sess["facts"] = []
    fresh = []
    for raw in new_facts:
        text = str(raw or "").strip()[:120]
        if not text:
            continue
        norm = _norm(text)
        superseded = None
        for old in book:
            if not isinstance(old, dict) or old.get("state") != "active":
                continue
            if SequenceMatcher(None, norm, _norm(old.get("fact") or "")).ratio() > 0.55:
                superseded = old
                break
        if superseded is not None:
            superseded["state"] = "resolved"
        entry = {"turn": int(turn or sess.get("turn") or 0), "fact": text,
                 "subject": _fact_subject(text), "state": "active"}
        book.append(entry)
        fresh.append(entry)
    if len(book) > FACTS_MAX:
        del book[:len(book) - FACTS_MAX]
    return fresh


def _fact_subject(text):
    """拿句子开头当主体，够用来分组，不用做分词。"""
    head = re.split(r"[，,。；;：:！？!?\s]", text.strip(), maxsplit=1)[0]
    return head[:12]


def active_facts(sess, limit=FACTS_KEEP):
    book = sess.get("facts") or []
    live = [f for f in book if isinstance(f, dict) and f.get("state") == "active"]
    return live[-limit:]


def _sync_new_character(result):
    """new_characters 是准的，new_character 留第一个给旧调用方。"""
    if result["new_characters"]:
        result["new_character"] = result["new_characters"][0]


def parse_turn(text, speaker):
    """拆出旁白与指定角色的台词。

    只认【本回合发言人】那一段：模型额外写给其它角色的台词一律丢弃，
    保证一轮只有一个人说话，不会出现角色互相刷屏。
    """
    text = strip_thinking(text or "").strip()
    text, status = extract_status(text)
    facts = list((status or {}).pop(FACT_KEY, []) or [])
    result = {"narrator": "", "speaker": speaker, "line": "",
              "new_character": None, "new_characters": [],
              "updates": [], "status": status,
              "facts": facts}
    if not text:
        return result

    if not speaker:
        # 一般模式：整段都是叙述者的话，不拆角色。但【新角色】【变化】仍要认，
        # 否则用户说「加个角色」永远没反应，那行 JSON 还会原样漏进正文。
        marks = list(SEG_RE.finditer(text))
        if not marks:
            result["narrator"] = text
            return result
        keep = []
        head = text[:marks[0].start()].strip()
        if head:
            keep.append(head)       # 第一个【】之前还有正文，别吞
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            tag = m.group(1).strip()
            body = text[m.end():end].strip()
            if not body:
                continue
            obj = None
            if tag in ("新角色", "新增角色", "角色"):
                objs = [o for o in extract_json_all(body) if (o.get("name") or "").strip()]
                if objs:
                    result["new_characters"].extend(objs)
                    continue
            elif tag in ("变化", "状态", "设定变化"):
                ups = [o for o in extract_json_all(body)
                       if (o.get("name") or "").strip() and (o.get("change") or "").strip()]
                if ups:
                    result["updates"].extend(ups)
                    continue
            # 认不出来的标签、或格式写坏的，原样留在正文里，不吞内容
            keep.append(m.group(0) + body)
        result["narrator"] = "\n\n".join(keep).strip()
        _sync_new_character(result)
        return result

    marks = list(SEG_RE.finditer(text))
    if not marks:
        result["line"] = text
        return result

    segments = []
    head = text[:marks[0].start()].strip()
    if head:
        segments.append(("旁白", head))     # 第一个【】之前的内容按旁白算
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        segments.append((m.group(1).strip(), text[m.end():end].strip()))

    narrator_parts, speaker_parts = [], []
    for tag, body in segments:
        if not body:
            continue
        if tag in ("旁白", "叙述", "系统"):
            narrator_parts.append(body)
        elif tag in ("新角色", "新增角色", "角色"):
            result["new_characters"].extend(
                o for o in extract_json_all(body) if (o.get("name") or "").strip())
        elif tag in ("变化", "状态", "设定变化"):
            result["updates"].extend(
                o for o in extract_json_all(body)
                if (o.get("name") or "").strip() and (o.get("change") or "").strip())
        elif tag == speaker:
            speaker_parts.append(body)
        # 其它角色的段落：丢弃

    line = "\n\n".join(speaker_parts).strip()
    if not line:
        # 模型没给指定角色台词时，用旁白兜底，避免整轮空白
        line = "\n\n".join(narrator_parts).strip()
        narrator_parts = []
    result["narrator"] = "\n\n".join(narrator_parts).strip()
    result["line"] = line
    _sync_new_character(result)
    return result
