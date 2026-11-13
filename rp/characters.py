# -*- coding: utf-8 -*-
"""角色表：新增、轮换、设定演进，以及模型输出的拆分。"""
import re
from difflib import SequenceMatcher

from . import metrics
from .config import mode_for, writing
from .llm import (extract_json, extract_json_all, loose_status_json, model_text,
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
                # 不截。role_chars 是「注入多少给模型」的档位策略，
                # 不是存储上限——在这里截等于把用户写的设定真删掉，
                # 而且截完还写回文件，救不回来
                "role": (item.get("role") or "").strip(),
                "state": (item.get("state") or "").strip()[:200],
                "history": [h for h in (item.get("history") or [])
                            if isinstance(h, str)][-CHARACTER_HISTORY_KEEP:],
            })
    # 不再兜底造一个。角色表空就是空：以前删完下一次读取又长回来，
    # 用户永远删不掉一个没建过的角色。新建会话时由 create 那边自己准备好。
    sess["characters"] = cleaned
    return cleaned

def next_speaker(sess):
    """圆桌式之后不再由代码指定发言人，一轮里在场角色各自发言。

    保留这个名字是为了让调用方不用判空；它永远返回空字符串。
    """
    return ""

def advance_turn(sess):
    sess["turn"] = int(sess.get("turn") or 0) + 1

# 模型会把「无」「暂无」当成名字写出来，这些不是人
_BAD_NAMES = {"无", "没有", "暂无", "未知", "不详", "空", "none", "null",
              "-", "—", "（无）", "无。", "无人物", "路人", "众人"}

def is_bad_name(name):
    return str(name or "").strip().lower() in _BAD_NAMES

def looks_like_name(name):
    """从在场名单里捞人用：像人名才建卡，免得把整句话塞进角色列。"""
    text = str(name or "").strip()
    if not text or is_bad_name(text) or len(text) > 8:
        return False
    # 「卖面的老头」「穿红衣服的女人」是描述，不是名字。中文人名基本不带「的」，
    # 带上就是临时称呼——放它建卡会占掉名额，主要角色反而进不来。
    if "的" in text:
        return False
    return not re.search(r"[，,。；;：:！？!?\s0-9]", text)

# 用户在消息里直接点名要加的人。靠模型写【新角色】靠不住，
# 它聊到后面就把规则当没看见，建卡这件事得自己做。
_ADD_CAST_RE = re.compile(
    r"(?:加入角色|新增角色|加个角色|加一个角色|新角色|加个人)"
    r"[：: ]*【?([^\s，。；,;、【】\[\]]{1,10})")

def parse_add_cast(text):
    """从用户这句话里拿出要加的名字，不靠模型。

    只认「加入角色：某某」这种明说带名字的写法；没名字就不猜，交回给模型去编。
    """
    out = []
    for m in _ADD_CAST_RE.finditer(str(text or "")):
        nm = re.sub(r"^[叫做是为]", "", m.group(1).strip())
        if nm and looks_like_name(nm) and nm not in out:
            out.append(nm)
    return out

def upsert_character(sess, data):
    """登记新角色 / 更新已有角色的设定与变化，返回 (名字, 是否新增)。"""
    if not isinstance(data, dict):
        return None
    name = (data.get("name") or "").strip()[:24]
    if not name or is_bad_name(name):
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


STATE_SYSTEM = (
    '你在给一段故事做状态更新。读正文，只输出 JSON，不要任何解释：'
    '{"changes":[{"name":"名字","state":"40 字以内"}],'    
    '"present":["现在和主角在同一个地方的角色"],'    
    '"away":["已经分开、不在这个场景里的角色"],'    
    '"place":"此刻的地点，6 字以内"}'
    '规则：present 和 away 只写【角色】名单里的名字。名单外的人一律不写——'
    '跑堂的、路过的、只露一面的，都不是角色，写进来会占掉名额。'
    'present 和 away 必须把所有人分完，一个不能漏：不在 present 就在 away。'
    '有人走开、跟上、分头行动、换了地点，就要照实改。'
    '不要把不在现场的人写进 present——他们不在这里，不该发言。'
    'changes 只写状态真变了的人；没变化的人不要写；一条都没有就写 []。'
)

# 状态里的中文也要跟随语言设置，否则英文剧情里冒出中文状态
STATE_SYSTEM_EN = (
    'You are updating the state of a story. Read the text and output JSON only: '
    '{"changes":[{"name":"name","state":"under 40 characters"}],'
    '"present":["characters in the same place as the protagonist right now"],'
    '"away":["characters who have left this scene"],'
    '"place":"where this is happening, under 6 characters"}'
    'Rules: only use names from the character list. '
    'Passers-by with no name are not characters; listing them wastes a slot. '
    'present and away must cover everyone exactly once. '
    'If someone leaves, follows, splits off, or the location changes, say so.'
)

def refresh_states(sess, text):
    """正文写完之后再过一遍角色状态。

    指望模型自己在正文里补【变化】靠不住：实测二十几个角色只写了两三个。
    主输出要顾文笔，这件事单独给它一次调用，只做这一件。
    失败了就当没发生：状态是锦上添花，不能因为它弄丢这一轮正文。
    """
    chars = [c for c in ensure_characters(sess) if (c.get("name") or "").strip()]
    body = (text or "").strip()
    if not chars or not body:
        return 0
    names = [c["name"] for c in chars]
    now = chr(10).join("%s：%s" % (c["name"], (c.get("state") or "（未记录）")[:120])
                       for c in chars)
    # 把上一轮的场景也给它：没有基准它就会把所有人都算在现场
    st0 = sess.get("status") or {}
    scene = "%s；地点：%s" % (
        "、".join(str(x) for x in (st0.get("present") or [])) or "（未记录）",
        (st0.get("location") or st0.get("place") or "（未记录）"))
    try:
        raw = model_text([
        {"role": "system", "content": STATE_SYSTEM_EN if writing().get("language") == "en" else STATE_SYSTEM},
            {"role": "user", "content": "【角色】%s" % "、".join(names)
             + chr(10) + "【上一轮的场景】" + scene
             + chr(10) + "【现在的状态】" + chr(10) + now
             + chr(10) + "【这一轮正文】" + chr(10) + body[:4000]},
        ], temperature=0.2, reasoning="low")   # 这是判断活，不需要深度思考
    except Exception:
        return 0
    obj = extract_json(raw) or {}
    if not isinstance(obj, dict):
        return 0
    book = {c["name"]: c for c in chars}
    n = 0
    # 在场名单：模型不填 present 就不动，别把已经对的名单清空
    me = (role_labels(sess)[0] or "").strip()
    st = sess.setdefault("status", {})
    if isinstance(st, dict):
        def _clean(value):
            if not isinstance(value, list):
                return None
            out = []
            for x in value:
                nm = str(x or "").strip()[:24]
                if nm and (nm in book or nm == me) and nm not in out:
                    out.append(nm)
            return out
        on = _clean(obj.get("present"))
        off = _clean(obj.get("away"))
        # 主角永远在场：他去哪儿由自己定，不能被这份名单判离场。
        # 只在真填了名字时才补，不把「用户角色」这种占位词写进去
        _me_name = (sess.get("user_name") or "").strip()
        if on is not None and _me_name and _me_name not in on:
            on = [_me_name] + on
        if off is not None and _me_name and _me_name in off:
            off = [x for x in off if x != _me_name]
        if on is not None:
            if (st.get("present") or []) != on:
                st["present"] = on
                n += 1
        if off is not None:
            if (st.get("away") or []) != off:
                st["away"] = off
                n += 1
        # 写 location，不写 place：界面、导出、下一轮 prompt 全都只认 location，
        # 写进 place 等于把地点丢到一个无人读的键里。
        # 已有 location 就不覆盖：[[地点：X]] 是模型直接标的，比这一步更可信。
        place = (obj.get("place") or "").strip()[:12]
        if place and not (st.get("location") or "").strip():
            st["location"] = place
            n += 1
    rows = obj.get("changes")
    if not isinstance(rows, list):
        return n
    for row in rows:
        if not isinstance(row, dict):
            continue
        nm = (row.get("name") or "").strip()
        st = (row.get("state") or "").strip()[:120]
        c = book.get(nm)
        if not c or not st or st == (c.get("state") or "").strip():
            continue
        c["state"] = st
        c["history"] = ((c.get("history") or []) + [st])[-CHARACTER_HISTORY_KEEP:]
        n += 1
    return n

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
# 单行写法：[[地点：某酒馆|时间：深夜|关系：戒备|物品：湿伞|目标：等对方开口|事实：甲；乙]]
MARK_KEYS_RX = "在场|离场|退场|地点|位置|场景|时间|关系|人物|物品|携带|目标|意图|事实"
# 模型常常把 [[…]] 写成【…】，两个竖线还在；不认这种写法的话整行状态就丢了
# 模型常把状态跟正文挤在一行：「她笑了。[[地点：某酒馆|时间：深夜]]」
# 整行锚定的规则吃不到这种，状态就整段漏进正文。下面这条只认带状态键的方括号。
INLINE_MARK_RE = re.compile(
    r"\[\[((?:" + MARK_KEYS_RX + r")[:：][^\[\]]{0,400})\]\]"
)
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
    if match:
        src = (src[:match.start()] + src[match.end():]).strip()
        body = match.group(1) or match.group(2) or ""
    else:
        inline = INLINE_MARK_RE.search(src)
        if not inline:
            return src, {}
        src = (src[:inline.start()] + src[inline.end():]).strip()
        body = inline.group(1)
    status = {}
    for chunk in re.split(r"[|｜]", body):
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
            names = _clean_names(value)
            if names:
                status[key] = names
        else:
            status[key] = value
    return src, status

def _parse_items(value):
    """物品可以绑到具体人身上：物品：乙=纱裙、皮鞭；甲=手机。

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

def _clean_names(seq, cap=None):
    """在场/离场名单：剔掉「无」这类占位词，去重，上限比角色数宽一点好接新人。"""
    if isinstance(seq, str):
        seq = NAME_SPLIT_RE.split(seq)
    cap = cap or (writing()["max_characters"] + 4)
    out = []
    for raw in seq or []:
        name = str(raw).strip()[:24]
        if not name or name.lower() in _BAD_NAMES:
            continue
        if name not in out:
            out.append(name)
    return out[:cap]

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
            names = _clean_names(names)
            if names:
                status[key] = names
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
                names = _clean_names(value)
                if names:
                    status[key] = names
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
    _active = sum(1 for x in book if isinstance(x, dict) and x.get("state") == "active")
    metrics.note("ledger.merge", added=len(fresh), total=len(book), active=_active,
                 superseded=len(fresh) and sum(1 for x in book if x.get("state") == "resolved"))
    # 观测：新事实里有没有其实能从物品格推出来的（物品名 + 持有人）。
    # 不删不改，只数数；跑一段时间看这个数高不高，再决定要不要做过滤。
    _items = (sess.get("status") or {}).get("items")
    if isinstance(_items, dict):
        _dup = 0
        for entry2 in fresh:
            t2 = entry2.get("fact") or ""
            for owner, owned in _items.items():
                for thing in str(owned or "").replace("，", "、").split("、"):
                    thing = thing.strip()
                    if len(thing) >= 2 and thing in t2 and str(owner) in t2:
                        _dup += 1
                        break
                else:
                    continue
                break
        if _dup:
            metrics.note("ledger.redundant", count=_dup, sample=fresh[0].get("fact"))
    return fresh

# 跟设定面板那条手动编辑的上限对齐
LORE_MAX = 40

def add_lore(sess, entry, turn=None):
    """对话里攒出来的词条：按 keys 去重，满了就不加。只写这一局，不碰故事模板。

    记上轮次是为了重发：重写到第三轮，第五轮才长出来的词条就该一起撤掉。
    """
    if not isinstance(entry, dict):
        return False
    raw_keys = str(entry.get("keys") or "").replace("，", ",")
    keys = ",".join(k.strip() for k in raw_keys.split(",") if k.strip())
    body = str(entry.get("content") or "").strip()[:2000]
    if not keys or not body:
        return False
    book = sess.setdefault("lore", [])
    if not isinstance(book, list):
        book = sess["lore"] = []
    low = keys.lower()
    for old in book:
        if isinstance(old, dict) and (old.get("keys") or "").strip().lower() == low:
            old["content"] = body        # 同一条：以这一轮的为准
            return False
    if len(book) >= LORE_MAX:
        return False
    book.append({"keys": keys[:120], "content": body,
                 "turn": int(turn if turn is not None else sess.get("turn") or 0)})
    return True

def _fact_subject(text):
    """拿句子开头当主体，够用来分组，不用做分词。"""
    head = re.split(r"[，,。；;：:！？!?\s]", text.strip(), maxsplit=1)[0]
    return head[:12]

def active_facts(sess, limit=FACTS_KEEP):
    book = sess.get("facts") or []
    live = [f for f in book if isinstance(f, dict) and f.get("state") == "active"]
    return live[-limit:]

def is_character_slot(tag):
    """【名字】这种两到四个汉字的短标签，才可能是角色段。"""
    text = str(tag or "").strip()
    return bool(text) and len(text) <= 8 and not re.search(r"[，,。；;：:！？!?\s0-9]", text)

def _sync_new_character(result):
    """new_characters 是准的，new_character 留第一个给旧调用方。"""
    if result["new_characters"]:
        result["new_character"] = result["new_characters"][0]

# 认人时要去掉的装饰符
_TRIM = "「」【】\"' \u3000"

def parse_cast(text):
    """解析「认人」的输出：一行一人，名字在前、一句话设定在后。

    这一步不受 max_characters 管：用户自己写在设定里的人，不能因为表满了
    就丢掉。表满该管的是模型现编的人，不是用户写死的。
    """
    out = []
    seen = set()
    for raw in str(text or "").splitlines():
        line = raw.strip().lstrip("-·*• 　")
        if not line:
            continue
        name, role = line, ""
        for sep in ("｜", "|", "：", ":"):
            if sep in line:
                name, _, role = line.partition(sep)
                break
        name = name.strip().strip(_TRIM)[:24]
        # {主角} 是占位符，不是一个人名
        if not name or len(name) > 12 or is_bad_name(name) or name in seen:
            continue
        if name in ("{主角}", "主角"):
            continue
        seen.add(name)
        out.append({"name": name, "role": role.strip()[:400]})
    return out


_CAST_LINE = re.compile("^\\s*(?:\u89d2\u8272|\u4eba\u7269)[\uff1a:]\\s*(.+)$")

def split_cast_line(text):
    """把生成结果末尾的「角色：甲、乙」剥出来，返回 (正文, 名字列表)。

    只认最后几行里的那一行；模型没按格式写就原样返回、不猜。
    这一行是给代码看的，不能留在设定正文里。
    """
    body = str(text or "")
    lines = body.splitlines()
    lo = max(0, len(lines) - 4)
    for i in range(len(lines) - 1, lo - 1, -1):
        m = _CAST_LINE.match(lines[i])
        if not m:
            continue
        names = []
        for raw in re.split("[\u3001,\uff0c;\uff1b/|]", m.group(1)):
            n = raw.strip().strip(_TRIM)[:24]
            if n and len(n) <= 12 and not is_bad_name(n) and n not in names                     and n not in ("{\u4e3b\u89d2}", "\u4e3b\u89d2"):
                names.append(n)
        rest = "\n".join(lines[:i] + lines[i + 1:]).strip()
        return rest, names
    return body.strip(), []

def parse_lore_batch(text):
    """解析“一次写几条世界书”的输出：一行一条，关键词在前、内容在后。

    关键词用全角或半角竖线分开；没分隔符的行当没看到。
    """
    out = []
    for raw in str(text or "").splitlines():
        line = raw.strip().lstrip("-·*• 　0123456789.、")
        if not line:
            continue
        if "｜" in line:
            keys, _, body = line.partition("｜")
        elif "|" in line:
            keys, _, body = line.partition("|")
        else:
            continue
        keys = keys.strip().strip(_TRIM)[:60]
        body = body.strip()[:400]
        if not keys or not body:
            continue
        out.append({"keys": keys, "content": body})
    return out

def parse_turn(text, speaker="", names=None):
    """拆出旁白和每个角色的发言。

    names 给了就按「一轮多人发言」处理：所有【名字】段落各收一条到 lines。
    names 没给（一般模式）时退回老行为——不认识的标签原样留在正文里。
    speaker 只是兼容旧调用，现在不再靠它筛人。
    """
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    text = strip_thinking(text or "").strip()
    text, status = extract_status(text)
    facts = list((status or {}).pop(FACT_KEY, []) or [])
    result = {"narrator": "", "speaker": speaker, "line": "", "lines": [],
              "new_character": None, "new_characters": [],
              "updates": [], "status": status,
              "facts": facts, "lore": []}
    if not text:
        return result

    marks = list(SEG_RE.finditer(text))
    if not marks:
        # 模型没分段：rp 模式整段算这个人的台词，一般模式算旁白
        if speaker or names:
            result["line"] = text
        else:
            result["narrator"] = text
        return result

    # 【旁白】之前的正文就是这一轮的旁白
    head = text[:marks[0].start()].strip()
    known = set(names)
    if speaker:
        known.add(speaker)
    narrator_parts = [head] if head else []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        tag = m.group(1).strip()
        body = text[m.end():end].strip()
        if not body:
            continue
        if tag in ("旁白", "叙述", "系统"):
            narrator_parts.append(body)
        elif tag in ("新角色", "新增角色", "角色"):
            result["new_characters"].extend(
                o for o in extract_json_all(body) if (o.get("name") or "").strip())
        elif tag in ("新词条", "新设定", "世界书"):
            result["lore"].extend(
                o for o in extract_json_all(body)
                if (o.get("keys") or "").strip() and (o.get("content") or "").strip())
        elif tag in ("变化", "状态", "设定变化"):
            result["updates"].extend(
                o for o in extract_json_all(body)
                if (o.get("name") or "").strip() and (o.get("change") or "").strip())
        elif tag in known or (names and is_character_slot(tag)):
            # 同一个人被拆成两段（模型写完又想补一句）：并回一段，
            # 不让一个人在一轮里占两个气泡
            if result["lines"] and result["lines"][-1]["speaker"] == tag:
                result["lines"][-1]["text"] += chr(10) * 2 + body
            else:
                result["lines"].append({"speaker": tag, "text": body})
        else:
            # 认不出的人名（模型临时起意写的路人）也别丢，当旁白接着写
            narrator_parts.append("%s%s" % (m.group(0), body))

    # 最后一个人的段里常常被模型塞了别人：主角的动作和台词、
    # 第三个人的话（甚至忘了写右括号）。按空行切开认领：
    # 以【已知名】开头的归那个人，其余全归旁白——旁白再次也比贴错人强。
    if result["lines"]:
        last = result["lines"][-1]
        parts = [x.strip() for x in re.split(r"\n\s*\n", last["text"]) if x.strip()]
        keep = parts[:1]
        for seg in parts[1:]:
            m2 = re.match(r"^【\s*([^】\n]{1,12})", seg)
            owner = m2.group(1).strip() if m2 else ""
            if owner:
                # 模型忘了写右括号：【唐糖心里咚嗖一下…一直到行尾
                # 没有】，切出来的就不只是名字。认名字开头就行。
                for nm in sorted(known, key=len, reverse=True):
                    if owner.startswith(nm):
                        owner = nm
                        break
            if not owner and seg.startswith(("他", "她")):
                # 以第三人称接着写：指的是她自己，继续算她的
                keep.append(seg)
                continue
            if owner and (owner in known or is_character_slot(owner)):
                seg = re.sub(r"^【\s*[^】\n]{1,12}", "", seg, count=1).strip()
                # 并回已有的同名段：同一个人不在一轮里占两个气泡
                same = next((x for x in result["lines"] if x["speaker"] == owner), None)
                if same is not None:
                    same["text"] += (chr(10) * 2) + seg
                else:
                    result["lines"].append({"speaker": owner, "text": seg})
            else:
                narrator_parts.append(seg)
        last["text"] = (chr(10) * 2).join(keep).strip()
    result["narrator"] = (chr(10) * 2).join(p for p in narrator_parts if p).strip()
    result["line"] = result["lines"][0]["text"] if result["lines"] else result["narrator"]
    if result["lines"]:
        result["speaker"] = result["speaker"] or result["lines"][0]["speaker"]
    _sync_new_character(result)
    return result
