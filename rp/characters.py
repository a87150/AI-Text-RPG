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

# 模型会把「无」「暂无」当成名字写出来，这些不是人
_BAD_NAMES = {"无", "没有", "暂无", "未知", "不详", "空", "none", "null",
              "-", "—", "（无）", "无。", "无人物", "路人", "众人"}

_NOT_NAMES = {
    "我", "你", "他", "她", "它", "咱们", "我们", "你们",
    "他们", "她们", "自己", "主角", "别人", "对方", "大家",
    "朋友", "路人", "众人", "老头", "女人", "男人", "家伙",
    "吧", "呢", "吗", "啊", "呀", "了", "的", ""
}

# 名字后面跟的括号补注不是名字的一部分：模型爱写【叶岩（在篮球场）】
# 交代人在哪儿，照抄就多出一张「叶岩（在篮球场）」的卡，跟「叶岩」并存。
# 只剥结尾那一段，剥空了就当它本来就没名字
_NAME_NOTE = re.compile(r"\s*[（(〔\[【][^（()）〔〕\[\]【】]{0,24}[)）〕\]】]\s*$")

def strip_note(name):
    """去掉名字尾部的括号补注，剥没了就退回原文。"""
    text = str(name or "").strip()
    out = _NAME_NOTE.sub("", text).strip()
    return out or text

def is_bad_name(name):
    text = strip_note(name).strip()
    return text.lower() in _BAD_NAMES or text in _NOT_NAMES

# 代词和虛词不是人名。分隔符是可选的，所以「加个角色我说两句」里的「我」
# 会被当成名字建卡——用户设了角色上限，这些假人把名额占满了。

# 名字开头不是这些字。中文人名很少以动词或指示词开头，「说两句」这种
# 从动词起头的其实是用户话的残片，不是人
_VERB_LEAD = "说想要会能是在就也都还又只走去来问看让给拿放做叫喊答回点站坐转"
# 剥掉开头的连接词，和开头的代词
_LEAD_STRIP = "叫做是为把让给么这那以及和跟"

def state_cells(text):
    """把「［当前好感度：75/100］」一行一项的状态拆成 [(键, 值)]。

    用户会直接给某个角色写这么一组，模型每轮在此基础上更新。
    """
    out = []
    for line in str(text or "").splitlines():
        s = line.strip().strip("［］[]【】").strip()
        if not s:
            continue
        for sep in ("：", ":"):
            if sep in s:
                k, v = s.split(sep, 1)
                k, v = k.strip()[:24], v.strip()[:200]
                if k and v:
                    out.append((k, v))
                break
    return out


def has_cells(text):
    """是不是用户手写的那组键值对（至少两项）。"""
    return len(state_cells(text)) >= 2


def ensure_characters(sess):
    """归一化角色表；老会话没有角色表时用 ai_name/ai_role 兜底成一个。"""
    chars = sess.get("characters")
    cleaned = []
    if isinstance(chars, list):
        for item in chars:
            if not isinstance(item, dict):
                continue
            name = strip_note(item.get("name") or "").strip()[:24]
            # 这里是角色表唯一的归一化入口，四条写入路径（新建、更新、
            # 保存设定、兜底建卡）最后都过这儿。模型会把「我」当成角色名
            # 交上来，存进去就占掉一个位置，而且用户删了下次还会回来
            if not name or is_bad_name(name):
                continue
            cleaned.append({
                "name": name,
                # 不截。role_chars 是「注入多少给模型」的档位策略，
                # 不是存储上限——在这里截等于把用户写的设定真删掉，
                # 而且截完还写回文件，救不回来
                "role": (item.get("role") or "").strip(),
                # 不截 200 字：用户会把整组键值对（十好几行）粘进来，
                # 截断等于把他写的状态真删掉一半，而且截完还写回文件
                "state": (item.get("state") or "").strip()[:2000],
                # 这个角色朗读时用哪个音色。空 = 走设置里的音色表 / 默认音色
                "voice": (item.get("voice") or "").strip()[:40],
                "style": (item.get("style") or "").strip()[:60],
                "history": [h for h in (item.get("history") or [])
                            if isinstance(h, str)][-CHARACTER_HISTORY_KEEP:],
            })
    # 剥完括号补注可能撞名（「叶岩」和「叶岩（在篮球场）」本来是同一个），
    # 不合并就是两张同名卡并列，删一张另一张还在
    merged = {}
    order = []
    for item in cleaned:
        name = item["name"]
        base = merged.get(name)
        if base is None:
            merged[name] = item
            order.append(name)
            continue
        # 设定取长的、状态和音色取后到的非空，历史接上
        if len(item["role"]) > len(base["role"]):
            base["role"] = item["role"]
        for key in ("state", "voice", "style"):
            if item[key]:
                base[key] = item[key]
        for h in item["history"]:
            if h not in base["history"]:
                base["history"].append(h)
    cleaned = [merged[n] for n in order]

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

def looks_like_name(name):
    """从在场名单里捞人用：像人名才建卡，免得把整句话塞进角色列。"""
    text = strip_note(name).strip()
    if not text or is_bad_name(text) or len(text) > 8:
        return False
    # 代词、虚词、通用称呼不是人。模型会把「我」写进 present，
    # 建卡那条路会给它开一张空卡，把角色名额占掉
    if text in _NOT_NAMES:
        return False
    # 「顾清岚及警员」「阿杰和沈知微」是两个人被连接词并成了一条。拆开两边
    # 都够两个字，说明它根本不是某一个人的名字；只拆出单字的才可能是真名，
    # 「林和平」被「和」拆成「林」「平」，那种不拦
    if all(len(x) >= 2 for x in _CAST_WEAK_RE.split(text) if x.strip()) \
            and len([x for x in _CAST_WEAK_RE.split(text) if x.strip()]) > 1:
        return False
    # 「卖面的老头」「穿红衣服的女人」是描述，不是名字。中文人名基本不带「的」，
    # 带上就是临时称呼——放它建卡会占掉名额，主要角色反而进不来。
    if "的" in text:
        return False
    return not re.search(r"[，,。；;：:！？!?\s0-9]", text)

# 用户在消息里直接点名要加的人。靠模型写【新角色】靠不住，
# 它聊到后面就把规则当没看见，建卡这件事得自己做。
_ADD_CAST_RE = re.compile(
    r"(?:加入角色|新增角色|新建角色|加个角色|加一个角色|新角色|加个人)"
    r"[：: ]*【?([^\s。；;【】\[\]]{1,10})")


# 拆名字用的分隔符，分两档。顿号和逗号是明确的列举符号，先用它们；
# 「和/与/跟/及」在名字里也常见（张和平），只在明确符号拆不出来时才动
_CAST_STRICT_RE = re.compile(r"[、,，]\s*")
_CAST_WEAK_RE = re.compile(r"[和与跟及]\s*")

def _clean_name(raw, allow_single=False):
    """把捕获到的片段洗成名字，洗不出来就返回空。"""
    nm = str(raw or "").strip()
    # 剥两层：「我朋友」→「朋友」→ 仍然不是名字，就在下面被筛掉
    for _ in range(2):
        for _pre in sorted(_NOT_NAMES | set(_LEAD_STRIP), key=len, reverse=True):
            if _pre and nm.startswith(_pre):
                nm = nm[len(_pre):].strip()
                break
        nm = re.sub(r"^[吧呢吗啊呀了的]", "", nm).strip()
    if not nm or nm in _NOT_NAMES:
        return ""
    # 名字最多四个字；以动词开头的不是名字。拆分场景额外要求至少两个字：
    # 「林和平」被「和」拆成「林」「平」，两段都太短，说明拆错了
    lo = 1 if allow_single else 2
    if not lo <= len(nm) <= 4 or nm[0] in _VERB_LEAD:
        return ""
    return nm if looks_like_name(nm) else ""

def parse_add_cast(text):
    """从用户这句话里拿出要加的名字，不靠模型。

    只认「加入角色：某某」这种明说带名字的写法；没名字就不猜，交回给模型去编。
    """
    out = []
    for m in _ADD_CAST_RE.finditer(str(text or "")):
        raw = m.group(1).strip()
        # 一句话点两个名字：「新增角色：阿杰和沈知微」。顿号先拆，拆不动
        # 再用「和」；拆完每一段都像名字才认——「林和平」被「和」拆成
        # 「林」「平」，两段太短说明拆错，退回整条
        parts = [x for x in _CAST_STRICT_RE.split(raw) if x.strip()]
        if len(parts) < 2:
            parts = [x for x in _CAST_WEAK_RE.split(raw) if x.strip()]
        names = [x for x in (_clean_name(x) for x in parts) if x]
        # 拆不出完整的两段就退回整条：「林和平」被「和」拆成「林」「平」，
        # 单字不算名字，这时按整条再判一次，它是个正常的三字名
        if len(parts) < 2 or len(names) != len(parts):
            names = [x for x in [_clean_name(raw, allow_single=True)] if x]
        if not names:
            continue
        for nm in names:
            if nm not in out:
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
        # 音色归用户所有，模型给的 data 里没有这个字段，别让它被覆盖
        "voice": "",
        "style": "",
    })
    return (name, True)


STATE_SYSTEM = (
    '你在给一段故事做状态更新。只输出下面这种格式，不要 JSON、不要代码块、不要任何解释。\n'
    '第一行是场景，固定这么写：\n'
    '场景：地点=天台边缘｜在场=谭谈,孙雨晴｜离场=\n'
    '然后每个状态有变化的角色各写一段：第一行只写名字，下面一行一项「键：值」。\n'
    '孙雨晴\n'
    '当前好感度：75/100\n'
    '当前性欲值：35/100\n'
    '规则：'
    '地点 6 字以内。在场、离场只写角色名单里的名字，名单外的人一律不写——'
    '跑堂的、路过的、只露一面的都不是角色，写进来会占掉名额。'
    '所有人必须分完，不重不漏；有人走开、跟上、分头行动、换了地点，就照实改。'
    '角色的状态本来是一组「键：值」的，就把整组照抄一遍：变了的换新值，没变的照抄原值，'
    '别只写变动的那几项，也别压成一句话。状态是一句话的角色，写一行「状态：那句话」。'
    '每一格只填这一项本身的内容，短——一般不超过三十字。'
    '不要复述角色说过的话，不要把动作场景写成一段叙述：状态栏是让人扫一眼的，不是正文。'
    '有几格就填几格，没写明的不要自己加格子；确实没什么可写就留空不写。'
    '没有任何变化的角色不要写。'
)

# 状态里的中文也要跟随语言设置，否则英文剧情里冒出中文状态
STATE_SYSTEM_EN = (
    'You are updating the state of a story. Output only this format: no JSON, no code fence, '
    'no explanation.\n'
    'First line is the scene, exactly like this:\n'
    'Scene: place=rooftop|present=Tan,Sun|away=\n'
    'Then one block per character whose state changed: the name alone on the first line, '
    'then one "key: value" per line.\n'
    'Rules: only use names from the character list; passers-by are not characters. '
    'present and away must cover everyone exactly once. '
    'If a character state is a list of "key: value" lines, copy the whole list back - '
    'new value for what changed, old value for what did not. '
    'Do not write characters that did not change.'
)

_SCENE_KEYS = {
    "地点": "place", "位置": "place", "place": "place", "location": "place",
    "在场": "present", "present": "present",
    "离场": "away", "away": "away",
}


def parse_state_reply(raw):
    """解析状态更新的纯文本回复：首行场景，之后每个角色一段。

    不用 JSON 是踩出来的：模型会把十几行状态塞进字符串、写到一半改成对象数组，
    整段就成了非法 JSON，一条都读不出来——内容明明是对的。纯文本它写得稳。
    """
    scene, blocks, cur = {}, {}, None
    for line in (raw or "").splitlines():
        s = line.strip().strip("`").strip()
        if not s:
            continue
        if s.startswith("场景") or s.lower().startswith("scene"):
            body = re.split(r"[：:]", s, 1)[-1]
            for part in re.split(r"[｜|]", body):
                if "=" in part:
                    k, v = part.split("=", 1)
                    key = _SCENE_KEYS.get(k.strip()) or _SCENE_KEYS.get(k.strip().lower())
                    if key:
                        scene[key] = v.strip()
            cur = None
            continue
        # 不带冒号的一行就是这一段的名字
        if "：" not in s and ":" not in s:
            name = s.strip(" 　［］[]【】#*-")[:24]
            if name and not name.startswith("规则"):
                cur = name
                blocks.setdefault(cur, [])
            continue
        if not cur:
            continue
        k, v = re.split(r"[：:]", s, 1)
        k = k.strip().strip(" 　［］[]")[:24]
        v = v.strip().strip(" 　［］[]")[:200]
        if k and v:
            blocks[cur].append((k, v))
    return scene, blocks


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
    # 键值对状态整组交给模型：截断之后它只看得见前两项，
    # 后面十几项永远轮不到被更新，表现就是「状态一直不变」
    def _brief(c):
        raw = (c.get("state") or "").strip()
        return raw if has_cells(raw) else raw[:120]
    now = chr(10).join("%s：%s" % (c["name"], _brief(c) or "（未记录）")
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
    got, blocks = parse_state_reply(raw)
    st = sess.get("status")
    if not isinstance(st, dict):
        st = sess["status"] = {}
    book = {c["name"]: c for c in chars}
    n = 0
    me = (role_labels(sess)[0] or "").strip()

    def _names(value):
        out = []
        for x in re.split(r"[,，、\s]+", value or ""):
            nm = x.strip()[:24]
            if nm and nm not in ("无", "-", "空", "none") and (nm in book or nm == me) and nm not in out:
                out.append(nm)
        return out

    # 模型没给这个键就别动：已经对的名单不能被清空
    on = _names(got.get("present")) if "present" in got else None
    off = _names(got.get("away")) if "away" in got else None
    # 主角永远在场：他去哪儿由自己定，不能被这份名单判离场。
    # 只在真填了名字时才补，不把「用户角色」这种占位词写进去
    _me_name = (sess.get("user_name") or "").strip()
    if on is not None and _me_name and _me_name not in on:
        on = [_me_name] + on
    if off is not None and _me_name and _me_name in off:
        off = [x for x in off if x != _me_name]
    if on is not None and (st.get("present") or []) != on:
        st["present"] = on
        n += 1
    if off is not None and (st.get("away") or []) != off:
        st["away"] = off
        n += 1
    # 写 location，不写 place：界面、导出、下一轮 prompt 全都只认 location，
    # 写进 place 等于把地点丢到一个无人读的键里。
    # 已有 location 就不覆盖：[[地点：X]] 是模型直接标的，比这一步更可信。
    place = (got.get("place") or "").strip()[:12]
    if place and not (st.get("location") or "").strip():
        st["location"] = place
        n += 1
    chars_book = st.get("chars")
    if not isinstance(chars_book, dict):
        chars_book = {}
    for nm, cells in blocks.items():
        c = book.get(nm)
        if not c or not cells:
            continue
        # 一整组「键：值」：整组换掉，并同步进 status.chars（弹窗读这个）
        if len(cells) >= 2:
            text2 = chr(10).join("［%s：%s］" % (k, v) for k, v in cells)
            if text2 != (c.get("state") or "").strip():
                c["state"] = text2
                c["history"] = ((c.get("history") or []) + [text2[:120]])[-CHARACTER_HISTORY_KEEP:]
                n += 1
            chars_book[nm] = [[k, v] for k, v in cells]
            st["chars"] = chars_book
            continue
        k, v = cells[0]
        if k not in ("状态", "state") or not v:
            continue
        if v == (c.get("state") or "").strip():
            continue
        # 卡上本来是键值对，这次只回了一句：只记历史，别把整块盖掉
        if not has_cells(c.get("state")):
            c["state"] = v[:120]
            n += 1
        c["history"] = ((c.get("history") or []) + [v[:120]])[-CHARACTER_HISTORY_KEEP:]
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
            if not has_cells(c.get("state")):
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
# 先行断言只要求整块里**至少有一个**已知状态键（说明这确实是状态行），
# 块内其余键可以是用户在设定里自己加的格子：好感度、性欲值、子宫状态……
INLINE_MARK_RE = re.compile(
    r"\[\[(?=(?:[^\[\]]*)(?:" + MARK_KEYS_RX + r")[:：])([^\[\]]{2,400})\]\]"
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
# 用户自己加的格子存在这里：[[标签, 值], …]，顺序就是模型写的顺序
EXTRA_KEY = "extra"
# 按角色分的状态行：[[孙雨晴|好感度：75/100|性欲值：30/100]]。
# 第一段没有人名冒号是场景行（[[地点：…|时间：…]]），靠这个区分，两者不会互吃
# 方括号一层两层都认：模型经常把 [[…]] 写成 […]，只认双层的话整行漏进正文。
# 第一段不能有冒号——有冒号的是场景行（[[地点：…]]），靠这个区分
CHAR_LINE_RE = re.compile(
    r"^[ \t]*[\[［]{1,2}([^\[\]［］|｜：:]{1,24})((?:[|｜][^\[\]［］]*)+)[\]］]{1,2}[ \t]*$", re.M)


def _take_char_lines(text):
    """剥掉「每人一行」的状态行，按名字收成 {名字: [[格子, 值], …]}。

    这些格子是各自的：孙雨晴的好感度是她自己的，主角那份也是他自己的。
    混进场景那行就没法区分谁是谁了。
    """
    got = {}
    for m in CHAR_LINE_RE.finditer(text or ""):
        name = m.group(1).strip()
        cells = []
        for chunk in re.split(r"[|｜]", (m.group(2) or "").lstrip("|｜")):
            if "：" in chunk:
                k, v = chunk.split("：", 1)
            elif ":" in chunk:
                k, v = chunk.split(":", 1)
            else:
                continue
            k, v = k.strip()[:20], v.strip()[:160]
            if k and v:
                cells.append([k, v])
        if name and cells:
            got.setdefault(name, []).extend(cells)
    if not got:
        return text or "", {}
    return CHAR_LINE_RE.sub("", text or "").strip(), got

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
        raw_key = key.strip()
        key = MARK_KEYS.get(raw_key)
        value = value.strip()
        if not value:
            continue
        if not key:
            # 白名单外的格子照原样留下：设定里自己加的「好感度」「性欲值」
            # 「子宫状态」全靠这一条才露得出来。以前这里是 continue，整格丢掉
            status.setdefault(EXTRA_KEY, []).append([raw_key[:20], value[:160]])
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
        # 物品两种形态都会来：按人分的 dict，和不带人名的公共物品字符串。
        # 字符串原样覆盖会把每个人手里的东西全清掉——模型只要有一轮
        # 没写「名字=东西」，账本就退化成一行公共物品
        if key == "items":
            prev = out.get("items")
            merged = dict(prev) if isinstance(prev, dict) else ({
                "": prev} if prev else {})
            if isinstance(value, dict):
                merged.update({k: v for k, v in value.items() if v})
            else:
                merged[""] = value
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
    # 先摘掉「每人一行」的分角色状态行，它们跟场景行是两回事
    src, char_cells = _take_char_lines(src)
    # 再认场景的单行写法：[[地点：X|时间：Y|…]]，小模型写这个比写 XML 稳
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
            elif key == EXTRA_KEY:
                status[key] = value          # [[标签, 值], …]，原样带过去
            else:
                status[key] = str(value)[:160]
    line = FACT_LINE_RE.search(cleaned)
    if line:
        cleaned = (cleaned[:line.start()] + cleaned[line.end():]).strip()
        extra = split_facts(line.group(1))
        if extra:
            status[FACT_KEY] = (status.get(FACT_KEY) or []) + extra
            status[FACT_KEY] = status[FACT_KEY][:3]
    # 分角色的格子：这一轮写了谁就更新谁，没提到的人保留上一轮的
    if char_cells:
        if not status:
            status = {}
        chars_map = status.get("chars")
        if not isinstance(chars_map, dict):
            chars_map = {}
        for nm, cells in char_cells.items():
            chars_map[nm] = cells
        status["chars"] = chars_map
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
# 手填的事实是用户自己写下的，跟模型每轮自动记的不是一回事：
# 自动的按 40 条滚动，手填的留到 200（和 /api/facts 里的上限一致）。
MANUAL_FACTS_MAX = 200

def _norm(text):
    # 去掉空格和全部标点。中文条目常只差一个句号或引号：
    # 「报警电话已接通。」和「报警电话已接通」按空白归一算是两条，
    # 于是同一句话能进账本两次。\W 在 Unicode 下保留汉字和字母数字
    return re.sub(r"[\W_]+", "", (text or "").lower())

def _fact_sim(a, b):
    """两句话是不是在说同一件事。

    用字符集合的重合度，不用 SequenceMatcher：后者按顺序比，
    「甲拿走了钥匙」和「钥匙被甲拿走了」明明是同一件事，它只给 0.62；
    而「甲拿到了钥匙」和「甲拿到了地图」只差两个字，它给 0.67。
    一正一反，阈值定在哪都会错一边。集合比法只看用了哪些字：
    语序换了不掉分，换了对象才掉分（0.86 vs 0.50）。
    """
    A, B = set(a or ""), set(b or "")
    return len(A & B) / max(1, len(A | B))


def _dedupe_book(book):
    """把说的是同一件事的条目并成一条。

    两种重复都要处理。一字不差的是一种；更常见的是包含关系——第 0 轮
    记「丈夫出差第7天，还有23天」，第 15 轮又记「丈夫出差第7天」，两条
    归一化之后集合相似度只有 0.63，任何相似度阈值都碰不到，但它们显然
    是一件事。

    留哪条按优先级排：手填 > 现行 > 信息更全 > 后出现的说法。保留者如果
    原本是已失效的，要标回现行——它吃掉了另一条，说明这条才是现行的说法。
    """
    items = [f for f in book if isinstance(f, dict)]
    norms = [_norm(f.get("fact") or "") for f in items]
    drop = set()
    for i in range(len(items)):
        if i in drop or not norms[i]:
            continue
        for j in range(i + 1, len(items)):
            if j in drop or not norms[j]:
                continue
            if not (norms[i] == norms[j] or norms[i] in norms[j] or norms[j] in norms[i]):
                continue
            a, b = items[i], items[j]
            sa = (bool(a.get("manual")), a.get("state") == "active", len(norms[i]))
            sb = (bool(b.get("manual")), b.get("state") == "active", len(norms[j]))
            win, lose = (i, j) if sa >= sb else (j, i)
            if items[lose].get("state") == "active":
                items[win]["state"] = "active"
            drop.add(lose)
            if lose == i:
                break
    book[:] = [f for k, f in enumerate(items) if k not in drop]

def _trim_book(book):
    """各自滚动：自动记的留 40 条，手填的留 200 条。

    以前是总数一刀切 40，按先后删最老的——手填的长线设定正好是最老的
    那批，写到第 41 条就被自己后来的自动记账挤出局，而且一句话都不说。
    """
    manual = [i for i, f in enumerate(book)
              if isinstance(f, dict) and f.get("manual")]
    auto = [i for i, f in enumerate(book)
            if not (isinstance(f, dict) and f.get("manual"))]
    drop = set()
    if len(auto) > FACTS_MAX:
        drop.update(auto[:len(auto) - FACTS_MAX])
    if len(manual) > MANUAL_FACTS_MAX:
        drop.update(manual[:len(manual) - MANUAL_FACTS_MAX])
    if drop:
        book[:] = [f for i, f in enumerate(book) if i not in drop]


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
        same = None
        superseded = None
        for old in book:
            if not isinstance(old, dict):
                continue
            old_norm = _norm(old.get("fact") or "")
            # 一字不差：这话又说了一遍而已，把它复活，别在账本里再抄一份。
            # 以前这里照样走下面那条路，于是同一条事实留下「resolved + active」两份
            if old_norm == norm:
                same = old
                break
            if old.get("state") != "active":
                continue
            # 见 _fact_sim：顺序比对分不清「换语序」和「换对象」，
            # 集合比对可以。0.7 是实测分界，两边都有余量
            if _fact_sim(norm, old_norm) > 0.7:
                superseded = old
                break
        if same is not None:
            same["state"] = "active"
            same["turn"] = int(turn or sess.get("turn") or 0)
            fresh.append(same)
            continue
        if superseded is not None:
            superseded["state"] = "resolved"
        entry = {"turn": int(turn or sess.get("turn") or 0), "fact": text,
                 "subject": _fact_subject(text), "state": "active"}
        book.append(entry)
        fresh.append(entry)
    _dedupe_book(book)
    _trim_book(book)
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

# 账本取几条属于"这一轮给模型看多少"，和档位有关，实现在 prompts.active_facts。
# 这里原来还有一份固定取 12 条的，两处口径会分叉，自检测的又正好是这份没人用的。

def is_character_slot(tag):
    """【名字】这种两到四个汉字的短标签，才可能是角色段。"""
    text = strip_note(tag).strip()
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
        tag = strip_note(m.group(1)).strip()
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
