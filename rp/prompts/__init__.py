# -*- coding: utf-8 -*-
"""提示词模块。

文案都在同目录的 .md 里，用 $变量 占位（string.Template）。
改规则、改字数表述、改格式示例都只需要动 md，不用碰代码。
"""
import os
import re
from difflib import SequenceMatcher
from string import Template

from ..characters import ensure_characters, guess_name, role_labels, scene_split
from ..config import creativity, mode_for, tier, tier_limits, writing
from ..llm import strip_status_leaks, strip_thinking
from ..stories import PLACEHOLDER, fill_names

_DIR = os.path.dirname(os.path.abspath(__file__))

def load(name):
    with open(os.path.join(_DIR, name), encoding="utf-8") as f:
        return f.read()

def render(name, **kw):
    return Template(load(name)).safe_substitute(**kw).strip()

# 贴在最后一条用户消息末尾的格式提醒，越弱的模型越需要
TAIL_HINT = {
    "basic": "（正文写完后，另起一行写一行状态，形如："
             "[[地点：某酒馆|时间：深夜|关系：戒备|目标：等他开口]]）",
    "standard": "（正文后另起一行写 [[地点：…|时间：…|关系：…|目标：…]]，没有的字段就不写）",
    # 高级档原来靠 system 里的示例，实测强模型聊久了就当没看见，
    # 状态和在场名单一直不更新
    # 必须跟 status_advanced.md 用同一种格式：
    # 两套并存时模型每轮都收到互相矛盾的要求，哪个都不全遵守
    "advanced": "（正文后另起一行写 <StatusBlock>{\"location\":\"…\",\"time\":\"…\",\"present\":[\"名字\"]}</StatusBlock>，"
                "没有的字段就不写）",
}

# 用户这一轮明说了要加人。光靠 rules 里那一条，聊久了模型就当没看见。
_ADD_CAST_RE = re.compile(
    r"加入角色|加个角色|加一个角色|加个新人|加个人|"
    r"新增角色|新角色|来个角色|登场一个|"
    r"多一个角色|加一个人")

def cast_hint(sess):
    """用户要求加人时把格式钉到他眼前。

    平时不加：每轮都提醒这句，模型会硬塞一个不该出现的人。
    """
    last = ""
    for m in reversed(sess.get("messages") or []):
        if m.get("role") == "user":
            last = str(m.get("content") or "")
            break
    if not last or not _ADD_CAST_RE.search(last):
        return ""
    names = [c.get("name") for c in ensure_characters(sess) if c.get("name")]
    who = ("；已有的 " + "、".join(names) + " 不要重写") if names else ""
    return ('（这一轮要登记新角色。正文之后另起一行写：'
            '【新角色】{"name":"名字","role":"身份与性格","relation":"和在场角色的关系"}。'
            '只写这一轮新出现的那个' + who + '。）')

def clip(text, limit, tail="…"):
    """按字数截断，尽量断在句号上，别把人设砍成半句话。

    截断了要明确告诉模型这是节选：突然断掉的人设会被它当成写完了，
    然后凭空补出一段跟自己冲突的性格。
    """
    text = (text or "").strip()
    if not limit or len(text) <= limit:
        return text
    head = text[:limit]
    for sep in ("。", "！", "？", "；", "\n"):
        pos = head.rfind(sep)
        if pos >= limit * 0.6:
            return head[:pos + 1] + tail
    return head.rstrip("，、, ") + tail

def role_range(cap=None):
    """AI 生成角色卡的目标字数，落在这一档实际能注入的上限之内。

    写超了就白写：超出 role_chars 的部分在注入时会被 clip 掉，
    用户看到的是一张卡，模型看到的是前半张。
    """
    cap = tier_limits()["role_chars"] if cap is None else cap
    if not cap:
        return 320, 520
    return max(60, int(cap * 0.6)), max(120, int(cap * 0.9))

# 三类设定的相对篇幅：角色卡是基准（受 role_chars 限制），
# 世界观要铺时代地点规则，开场白要留出第一段互动，都比一张卡长。
# 初级档 role_range 是 120~180，乘出来正好 200~300 和 160~240。
_GEN_GROUP = {"character": "role", "user_role": "role",
              "world": "world", "opening": "open"}
_GEN_SCALE = {"role": (1, 1), "world": (5, 3), "open": (4, 3)}

def gen_range(field):
    """AI 补设定的目标字数，三类各一组设置。

    没设（0）就跟模型档位走：档位决定人设能注入多少字，写超了注入时会被
    截断，所以默认区间本来就该贴着档位。
    """
    group = _GEN_GROUP.get(field, "world")
    w = writing()
    lo = int(w.get("gen_%s_min" % group) or 0)
    hi = int(w.get("gen_%s_max" % group) or 0)
    if lo and hi:
        return lo, hi
    rlo, rhi = role_range()
    num, den = _GEN_SCALE.get(group, (1, 1))
    if num == den:
        return rlo, rhi
    return rlo * num // den, rhi * num // den

def active_facts(sess, limit=None):
    """已确立且没被推翻的事实，按档位取最近若干条。"""
    cap = limit if limit is not None else tier_limits()["facts"]
    book = [f for f in (sess.get("facts") or [])
            if isinstance(f, dict) and f.get("state") == "active"]
    return book[-cap:] if cap else book

# 规则也按档位分：小模型能可靠执行的硬约束就五条左右，多了互相打架，
# 而"文风跟着场景能量走"这类元指令 9B 执行不了，占着位置反而挤掉更要紧的。
RULES_FILE = {
    "basic": "rules_basic.md",
    "standard": "rules_standard.md",
    "advanced": "rules_advanced.md",
}

# 创意档位的写法倾向。只调温度是让用词变花，剧情还是最顺的那条路；
# 这两句才是真正把"换个反应"推到模型面前。
CREATIVE_HINT = {
    "standard": "",
    "creative": "（这一轮别写第一反应。如果最顺的接法是顺从或解释，换一个同样合理但没那么顺的。）",
    "wild": "（允许节外生枝：环境里可以冒出意外，角色可以做出不符合期待的选择。"
            "但人设的底线不能变，也不能替用户决定。）",
}

STATUS_FILE = {
    "basic": "status.md",          # 单行标记 + 两条示例
    "standard": "status_standard.md",
    "advanced": "status_advanced.md",
}

def status_prompt():
    return render(STATUS_FILE.get(tier(), "status.md"))

def build_rules(user_name, mode=None, names="", sess=None):
    w = writing()
    mode = mode or mode_for({})
    status_block = status_prompt()
    # 四个字数设置合成一轮的总量：旁白那份 + 台词那份。
    # 一般模式整段都是叙述，用总量；rp 模式拆开写，但也要给出合计上限
    # 总量要按在场人数算：只算一个人的话，旁白一长模型就把其他角色段砍了
    headcount = max(1, len([x for x in (names or "").split("、") if x.strip()]))
    total_min = w["narrator_min"] + w["line_min"] * headcount
    total_max = w["narrator_max"] + w["line_max"] * headcount
    narrator_min, narrator_max = w["narrator_min"], w["narrator_max"]
    line_min, line_max = w["line_min"], w["line_max"]

    # 区间原样用设置里的数。按密度缩放的那套已经去掉：那是在替用户改他自己设的值，
    # 用户填了旁白 50~300，thin 一触发就变成 22，等于程序盖过设置。现在靠
    # DENSITY_HINT 那几句话让模型自己控制往区间的哪一端写。

    if mode == "normal":
        # 一般模式是把 rp 里拆开的那几段合成一段：旁白 + 每个角色说的话，
        # 合起来才是同一段情节。所以字数也要用合计，不是只用旁白那份——
        # 只算旁白的话，人物干脆不说话了，等于换个方式写 rp
        return render(
            "normal.md",
            user_name=user_name or "用户角色",
            character_names=names or "（暂无）",
            line_min=narrator_min + line_min,
            line_max=narrator_max + line_max,
            max_characters=w["max_characters"],
            status_block=status_block,
        )
    return render(
        RULES_FILE.get(tier(), "rules_standard.md"),
        user_name=user_name or "用户角色",
        narrator_min=narrator_min,
        narrator_max=narrator_max,
        line_min=line_min,
        line_max=line_max,
        total_max=total_max,
        max_characters=w["max_characters"],
        status_block=status_block,
    )
def build_character_block(chars, speaker=None, status=None):
    """角色卡整段展开，并分「在场 / 不在场」。

    压成一行只剩标签，模型就写不出人；不分在场离场，设定里的人会无脑出现。
    """
    on, off = scene_split(chars, status)
    if not on:                        # 状态写坏了也不能一个人都没有
        on, off = list(chars), []
    role_cap = tier_limits()["role_chars"]     # 小模型吃长设定会串味
    lines = []
    for c in sorted(on, key=lambda c: 0 if c["name"] == speaker else 1):
        raw = (c.get("role") or "").strip()
        role = (clip(raw, role_cap, "…（这条设定还有后续，此处只是节选）")
                or "（未设定，按名字与场景合理发挥）")
        # 用 ◆ 而不是【】：方括号留给格式标记，别让模型把角色卡当成输出格式
        if c["name"] == speaker and speaker:
            lines.append("◆ 你这一轮演的就是：%s\n%s" % (c["name"], role))
        else:
            lines.append("◆ 在场：%s\n%s" % (c["name"], role))
        # 只给一条当前状态。state 就是最近这次变化，history 里存的是同一句加上更早的，
        # 两行都写等于把同一句话喂给模型两遍，白占字数还容易让它以为有两件事
        state = (c.get("state") or "").strip()
        if not state and c.get("history"):
            state = (c["history"][-1] or "").strip()      # 老会话只写过 history
        if state:
            lines.append("｜状态：%s" % state)
    if off:
        lines.append("◆ 不在这一场（已经离开，不要让他们出现在这一轮）：%s"
                     % "、".join(c["name"] for c in off))
    if speaker:
        lines.append("这一轮只写【%s】一个人的话，别人只出现在旁白里。" % speaker)
    return "\n".join(lines)

def build_status_scene(sess):
    """状态字段本身是数据，读起来像后台日志；串成一句话模型才当情境用。"""
    status = sess.get("status") or {}
    bits = []
    if status.get("time") and status.get("location"):
        bits.append("现在是%s，%s。" % (status["time"], status["location"]))
    elif status.get("location"):
        bits.append("此刻在%s。" % status["location"])
    elif status.get("time"):
        bits.append("现在是%s。" % status["time"])
    other = role_labels(sess)[0]
    for key, label in (("relations", "你和她（%s）之间" % other), ("items", "你身上带着"),
                       ("goal", "你这会儿想")):
        value = status.get(key)
        if not value:
            continue
        if isinstance(value, dict):
            value = "、".join(v for v in value.values() if v)
        if value:
            bits.append("%s：%s。" % (label, value))
    return "".join(bits)

# 正文语言。这句必须每轮都在：模型不看这个就按自己的习惯走
LANGUAGE_LINES = {
    "zh": ("正文一律用简体中文写。不要夹整句外语，不要写成中英混排；"
           "专有名词、术语、通讯里的英文原名可以保留，其余一律用中文。"),
    "en": ("Write everything in English. Do not drop whole sentences in another language "
           "and do not mix languages; proper nouns and technical terms may keep their "
           "original spelling."),
}

def build_system_prompt(sess, speaker=None, parts_out=None):
    """拼这一轮的 system prompt。parts_out 传字典就顺手告一段落各占多少字，
    面板要画占比条，分段统计只能在这儿做——拼完再切开就认不回来了。"""
    user_name, _ = role_labels(sess)
    chars = ensure_characters(sess)
    mode = mode_for(sess)
    # 没配角色就没人说话，退回叙台口吻，不要硬造一个名字出来
    if mode == "rp" and not speaker and chars:
        speaker = chars[0]["name"]
    if mode == "normal" or not chars:
        speaker = ""
    names = "、".join(c["name"] for c in chars)

    parts = []

    def add(label, text):
        parts.append(text)
        if parts_out is not None:
            # 记 token 不记字数：占比条要跟模型窗口对齐，中文 0.65、英文 1.3，
            # 按字数画出来的比例会骗人
            parts_out[label] = parts_out.get(label, 0) + _tokens(text) + 1

    if mode == "rp" and speaker:
        # 先把身份钉死，再说别的。缺这一句模型就退化成全知叙述者，谁都不像
        add("identity",
            "你这一轮就是【%s】本人，不是旁观者、不是作者。下面写的是你的处境、你的脾气、"
            "你和【%s】之间的账，不是给你参考的资料。用%s的眼睛看，用%s的语气说，"
            "只写这一轮，不解释、不总结、不跳出角色。"
            % (speaker, user_name, speaker, speaker))
    else:
        add("identity", "你正在写一段沉浸式的长篇故事叙述，用第三人称旁白的口吻，"
                     "把场景、动作和在场角色的反应写出来。")

    # 语言必须钉死。只靠用户输入带动的话，一遇上糊涂就开始夹英文句子。
    add("language", LANGUAGE_LINES.get(writing().get("language"), LANGUAGE_LINES["zh"]))

    # 主角 = 用户扮演的角色。不是角色表第一位（那是 AI 扮演的人）
    lead = (sess.get("user_name") or "").strip()
    if sess.get("world"):
        add("world", "【故事背景与世界观】" + "\n" + fill_names(sess["world"], lead))
    add("characters", build_character_block(chars, speaker, sess.get("status")))
    if sess.get("user_role"):
        who = ("【%s（和你说话的人）】" % user_name) if mode == "rp" else ("【用户扮演的角色：%s】" % user_name)
        add("user_role", who + "\n" + sess["user_role"])
    if sess.get("summary"):
        add("summary", "【前情提要（更早发生过的事）】" + "\n" + sess["summary"])
    scene = build_status_scene(sess)
    if scene:
        add("status", "【此刻的处境】" + "\n" + scene)
    book = active_facts(sess)
    if book:
        # 不带"第几轮"：模型本来就不知道现在是第几轮，给个数字它反而会当成
        # "已经过了这么多轮"，跟手上只有几轮的历史对不上，时间感就散了
        lines = ["【这些已经发生，不能当成没发生、也不能反悔】"]
        for f in book:
            lines.append("· %s" % (f.get("fact") or ""))
        add("facts", "\n".join(lines))
    # 额外要求（sess["rules"]）是写设定时用的提示词，不进这里。
    # 故事推进时拿着它只会跟「写法」抢位置，真正要生效的地方是 _extra_block。
    _density, _ = input_density(sess)
    _tail = chr(10).join(x for x in (DENSITY_HINT.get(_density, ""),
                                     STEP_HINT.get(_density, ""),
                                     timeline_hint(sess)) if x)
    add("rules", "【写法】" + "\n" + build_rules(user_name, mode, names, sess)
        + (("\n\n" + _tail) if _tail else ""))
    return ("\n" + "\n").join(parts)


LORE_MAX_ITEMS = 6
LORE_MAX_CHARS = 400
LORE_SCAN_MESSAGES = 6

def lore_hits(sess, recent_text=""):
    """本轮命中的世界书条目（含命中的关键词，方便前端高亮）。"""
    entries = sess.get("lore") or []
    haystack = (recent_text or "").lower()
    if not entries or not haystack:
        return []
    hits = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        keys = [k.strip().lower() for k in str(entry.get("keys") or "").replace("，", ",").split(",")]
        keys = [k for k in keys if k]
        body = str(entry.get("content") or "").strip()
        if not keys or not body:
            continue
        matched = [k for k in keys if k in haystack]
        if matched:
            hits.append({"keys": ",".join(matched), "content": body[:LORE_MAX_CHARS]})
        if len(hits) >= LORE_MAX_ITEMS:
            break
    return hits

def build_lore_block(sess, recent_text=""):
    """只把最近对话里点到名的世界书条目塞进去，设定再多也不稀释人设。"""
    hits = lore_hits(sess, recent_text)
    if not hits:
        return ""
    lead = (sess.get("user_name") or "").strip()
    return ("【世界书（本轮相关设定）】\n"
            + "\n".join("· " + fill_names(h["content"], lead) for h in hits))



def build_anchor_block(sess):
    """人设锚定：聊久了角色卡会被新对话稀释，隔一段把核心重插到近处。"""
    chars = ensure_characters(sess)
    lines = []
    for c in chars[:3]:
        role = (c.get("role") or "").strip()
        if role:
            lines.append("· %s：%s" % (c["name"], clip(role, 160, "…（只是节选，人设还有后续）")))
    if not lines:
        return ""
    return ("【人设锚定 · 别把他们写成好说话的助手】\n" + "\n".join(lines)
            + "\n保持这些人的脾气、说话方式和底线，不要因为聊久了就变软、变话痨。")

def build_context_debug(sess, speaker=None):
    """这一轮到底发了什么：给「上下文」面板用，先能看见才能调。"""
    parts_out = {}
    system = build_system_prompt(sess, speaker, parts_out)
    recent = " ".join(
        str(m.get("content") or m.get("narrator") or "")
        for m in (sess.get("messages") or [])[-LORE_SCAN_MESSAGES:]
    )
    hits = lore_hits(sess, recent)
    lore_len = sum(len(h["content"]) for h in hits)
    turn = int(sess.get("turn") or 0)
    every = tier_limits()["anchor_every"]
    anchored = bool(turn and turn % every == 0 and build_anchor_block(sess))
    facts = active_facts(sess)
    # summary 和 facts 已经拼进 system 了，再加一遍是虚报；
    # lore 是另外一条 system 消息，不在 system 里，要算
    total = len(system) + lore_len
    return {
        "system": system,
        "lore": hits,
        "lore_tokens": _tokens("".join(h["content"] for h in hits)) + _tokens("".join(h.get("keys") or "" for h in hits)),
        "summary_tokens": _tokens(sess.get("summary") or ""),
        "status": sess.get("status") or {},
        "facts": facts,
        "facts_tokens": _tokens("".join(f.get("fact") or "" for f in facts)),
        "anchored": anchored,
        "anchor_every": every,
        "chars": {"total": total, "system": len(system)},
        # 系统提示词按来源拆开：面板画占比条，才知道是哪一段在膨胀
        "system_parts": parts_out,
        # 按字数乘一个系数不行：中英比例不同精度差很多。
        # lore 是另一条 system 消息，走同一套估算
        "tokens": _tokens(system) + _tokens("".join(h["content"] for h in hits)),
    }

# 动作类动词：用来判断用户是"点了个头"还是"推门、拔刀、转身就跑"
_ACTIONS = ("推", "拉", "拔", "抓", "握", "踏", "跨", "转身", "抬手", "低头",
            "后退", "靠近", "踢", "挥", "扔", "抢", "抱", "按", "坐", "站",
            "走", "跑", "扑", "抵", "撑", "捏", "扯", "掀", "探")

def input_density(sess):
    """用户这一轮写了多细。固定字数在长段动作后必然失衡，得让目标跟着走。

    返回 (档位, 目标区间)。区间是写作设置里那个范围的倍数换算，不是另写一套数字。
    """
    w = writing()
    last = ""
    for m in reversed(sess.get("messages") or []):
        if m.get("role") == "user":
            last = str(m.get("content") or "")
            break
    n = len(last.strip())
    acts = sum(last.count(v) for v in _ACTIONS)

    if mode_for(sess) == "normal":
        lo, hi = w["narrator_min"], w["narrator_max"]
    else:
        lo, hi = w["narrator_min"] + w["line_min"], w["narrator_max"] + w["line_max"]
    # 还没有用户发言（开场、刚建会话）不算「只写了半句」，按正常长度走
    if not last.strip():
        return "normal", (int(lo), int(hi))
    # 这里定的是上限，不是目标值：能力强的模型一看到区间就往高处写，
    # 写成一场完整的戏，用户还没接话故事就翻篇了
    if n < 15 and acts == 0:
        # 用户只写了半句，就别替他把后面几步走完。给短一点的上限，
        # 写一个完整的短反应，好过写一长段跳三格
        return "thin", (int(lo), int(hi))
    if n > 120 or acts >= 4:
        # 上限跟着用户设的上限走，不是下限的倍数：写 74 字带 9 个动作，
        # 却只准回 80 字，对话就卡死了。留一成余量，别让模型顶格写
        return "thick", (int(lo), int(hi))
    return "normal", (int(lo), int(hi))

# 推进量跟着用户这一轮的输入走，不是一个固定值。
# 原来这里是一句恒定的「只往前挪一小步」，不管用户写了一句还是一整段：
# 用户连推十二步，模型也只回一个反应就收住，读起来像卡住了。
# 短输入才该收——那是用户自己只走了一步。
STEP_HINT = {
    "thin": ("（我这一轮只写了一个动作。别替我把后面几步走完："
             "只写眼前这一个反应就收住，剩下的留给我接。）"),
    "normal": ("（我这一轮写了一小段。你按同样的跨度往下接：一个动作配一个反应，"
               "别只回半句就停，也别把后面好几步一次走完。）"),
    "thick": ("（我这一轮写了好几件事，时间在往前走。跟上这个跨度："
              "该发生的让它发生，该接的话接上，别只回我一个反应就停住。"
              "但别跨场景、别跳时段。）"),
}

DENSITY_HINT = {
    # thin 不再重复 STEP_HINT 的意思，改成一句只说长度的话
    "thin": "（我这一句很短，别写成长篇。）",
    # thick 原来是「不要复述我刚写过的动作」，模型连提都不敢提，
    # 读起来像它没看见用户干了什么。改成要求衔接而不是回避
    "thick": "（我这段写完了，就停在那儿。你从这里往下接，接着写接下来的反应和后果。）",
    "normal": "",
}

def timeline_hint(sess):
    """时间只被记录、没人用它，模型就会在原地打转或者凭空跳档。"""
    time = ((sess.get("status") or {}).get("time") or "").strip()
    if not time:
        return ""
    return ("（上一轮结束时是「%s」。本轮时间要从那里自然往下走；"
            "跨度大就在正文里交代清过了多久、天色怎么变的。）" % time)

def build_messages(sess, speaker=None):
    """历史消息还原成「旁白 + 角色」的形式，让模型明白谁说过什么。"""
    messages = [{"role": "system", "content": build_system_prompt(sess, speaker)}]
    recent = " ".join(
        str(m.get("content") or m.get("narrator") or "")
        for m in (sess.get("messages") or [])[-LORE_SCAN_MESSAGES:]
    )
    lore = build_lore_block(sess, recent)
    if lore:
        messages.insert(0, {"role": "system", "content": lore})
    turn = int(sess.get("turn") or 0)
    if turn and turn % tier_limits()["anchor_every"] == 0:
        anchor = build_anchor_block(sess)
        if anchor:
            # 贴在最近几轮之前，而不是只留在最顶上被稀释
            messages.insert(max(1, len(messages) - 3), {"role": "system", "content": anchor})
    # 压缩不删消息，只是把游标推到摘要覆盖到的位置；
    # 消息本身留着给界面回看、导出和重写用
    history = sess.get("messages") or []
    _start = int(sess.get("compressed_upto") or 0)
    if _start:
        history = history[_start:]
    cap = tier_limits()["history_msgs"]
    if cap and len(history) > cap:
        history = history[-cap:]
        # 开头是 assistant 会读成「它自己接自己」，往前让出一条
        while history and history[0].get("role") != "user":
            history = history[1:]
    for m in history:
        content = m.get("content") or ""
        narrator = m.get("narrator") or ""
        # 一般模式的回复正文全在 narrator 里，content 是空的。
        # 早先这里只看 content，等于把 AI 写过的每一段都丢了——模型看不到
        # 自己上一轮说了什么，每轮都从头起笔，用多好的模型都救不回来。
        if not content and not narrator:
            continue
        if m.get("role") == "user":
            if content:
                messages.append({"role": "user", "content": content})
        elif m.get("role") == "assistant":
            narrator = m.get("narrator") or ""
            if not content and narrator:      # 一般模式：整段叙述都记在 narrator 里
                messages.append({"role": "assistant", "content": narrator})
                continue
            name = (m.get("speaker") or "").strip()
            head = "【旁白】%s\n" % narrator if narrator else ""
            if name:
                messages.append({"role": "assistant", "content": head + "【%s】%s" % (name, content)})
            else:
                messages.append({"role": "assistant", "content": head + content})
    level, (tlo, thi) = input_density(sess)
    if mode_for(sess) == "rp":
        # 尾巴和 rules 必须同一个口径。rules 里的合计上限按「旁白 + 每人一份」
        # 算（narrator_max + line_max × 人数），而 input_density 只给单人量；
        # 两处说法不一致时模型挑窄的那个听，人数越多差得越远
        _n = max(1, len(ensure_characters(sess)))
        _w2 = writing()
        tlo = _w2["narrator_min"] + _w2["line_min"] * _n
        thi = _w2["narrator_max"] + _w2["line_max"] * _n
    # 原句是「最多 thi 字，写到 tlo 字也够」：模型照着 tlo 写，用户把下限
    # 从 50 调到 200、输出几乎没变，因为下限被当成了目标。这里是区间，两头都说清
    pace = "（这一轮写 %d~%d 字。这是篇幅要求，不是情节量——"            "写多长由你定，但该发生的别省。）" % (tlo, thi)
    tail = (TAIL_HINT.get(tier(), "") + CREATIVE_HINT.get(creativity(), "")
            + pace + DENSITY_HINT[level] + STEP_HINT[level] + timeline_hint(sess)
            + cast_hint(sess))
    if tail:
        # 格式要求写在 system 里离生成点太远，小模型容易忘；补在最后一条 user 尾巴上
        for item in reversed(messages):
            if item.get("role") == "user":
                item["content"] = item["content"].rstrip() + "\n\n" + tail
                break
    return messages

def build_locked_block(user_name, ai_name):
    lines = []
    if user_name == PLACEHOLDER:
        # 写设定时主角只给占位符（见 build_field_prompt）。
        # 要是把它当真名写成「必须叫「{主角}」」，等于让模型去编一个名字，
        # 生成出来的世界设定就把人名写死了。
        lines.append("· 主角一律写成 %s，不要换成任何具体人名" % PLACEHOLDER)
    elif user_name:
        lines.append("· 用户角色名必须叫「%s」：不得改名、不得替换、不得省略" % user_name)
    if ai_name:
        lines.append("· AI 角色名必须叫「%s」：不得改名、不得替换、不得省略" % ai_name)
    if not lines:
        return ""
    return render("locked.md", lines="\n".join(lines))

def _extra_block(rules, user_name, ai_name):
    parts = []
    if rules and rules.strip():
        parts.append("· 额外要求：\n" + rules.strip())
    locked = build_locked_block(user_name, ai_name)
    if locked:
        parts.append(locked)
    return ("\n\n" + "\n\n".join(parts)) if parts else ""

def _state_block(opening="", lore=None, mode=""):
    """已经定下来的其余内容也给它看，不然单项生成会和别的字段打架。"""
    parts = []
    if mode:
        parts.append("· 对话模式：%s" % ("角色扮演（AI 演一个角色，轮流说话）"
                                        if mode == "rp" else "一般（系统叙述，不轮流）"))
    if opening and opening.strip():
        parts.append("· 已写好的开场（后面的内容必须接得上，不要改设定）:\n" + opening.strip()[:800])
    entries = [e for e in (lore or []) if isinstance(e, dict) and str(e.get("content") or "").strip()]
    if entries:
        lines = ["· 已有的世界书条目（提到这些词时会带进上下文，新的内容不能和它们冲突）："]
        for e in entries[:12]:
            keys = str(e.get("keys") or "").strip()
            body = str(e.get("content") or "").strip()[:120]
            lines.append("  - %s：%s" % (keys or "（无关键词）", body))
        parts.append("\n".join(lines))
    return ("\n" + "\n".join(parts)) if parts else ""

def _others_block(characters, skip_first=True):
    """角色列表里除第一个以外的角色，扩写时必须整份保留。"""
    items = [c for c in (characters or []) if isinstance(c, dict) and str(c.get("name") or "").strip()]
    if skip_first:
        items = items[1:]
    if not items:
        return ""
    lines = ["", "【其他在场角色（必须原样保留：可以补充细节，不得删除、不得改名）】"]
    for c in items:
        line = "· %s：%s" % (str(c["name"]).strip(),
                             str(c.get("role") or "（未写设定，请补全）").strip())
        state = str(c.get("state") or "").strip()
        if state:
            line += "｜当前：%s" % state[:80]
        lines.append(line)
    return "\n".join(lines)

def _ai_block(ai_name, ai_role, ai_state="", auto_roles=1):
    """AI 角色空着的时候别让模型自己乱编，先说清这是可以推断的。

    auto_roles 是用户设的：0 表示这次不要替他起角色，全文用占位符就行。
    """
    name = (ai_name or "").strip()
    role = (ai_role or "").strip()
    if not auto_roles and not name:
        return ("· AI 角色这次不用你定。正文里一律写 {主角}，不要自己发明名字，"
                "也不要写“主角”以外的称呼。")
    if not name and not role:
        return ("· AI 角色还没定：从上面用户写的想法里推断一个最合适的对手或搭档，"
                "再给他一个名字，写清他为什么和用户角色纠缠在一起。")
    lines = ["· AI 的角色名：%s" % (name or "（未填写，请自拟）"), "· AI 扮演的角色原文："]
    lines.append(role or "（未填写，请合理创作，但要贴合上面用户的原话）")
    state = (ai_state or "").strip()
    if state:
        lines.append("· 他的当前状态（剧情里已经发生了，必须保留）：" + state[:120])
    return "\n".join(lines)

# 生成世界观和开场白时附一行名单，代码拿去补角色表，不留在正文里
CAST_TAIL = ("如果这段里出现了「我」和 AI 角色之外的人物，"
             "在最后另起一行写「角色：名字、名字」，只列真正露面的人；"
             "整行不加修饰，不要写进正文段落里。没有就别写这行。")

# 一次只写一个字段，输出短、失败只重试这一项
FIELD_TASKS = {
    "user_name": "给「我扮演的角色」起一个名字：2~4 个汉字，贴合身份与时代。只输出名字，不要解释。",
    "user_role": ("把「我扮演的角色」写成 {lo}~{hi} 字的角色卡：外貌、性格、处境、"
                  "在意什么、说话习惯。"),
    "ai_name": "给 AI 角色起一个名字：2~4 个汉字，贴合用户的想法。只输出名字，不要解释。",
    "world": ("把用户写的背景补全成 {lo}~{hi} 字的世界观：时代、地点、规则与禁忌、当前局势。"
              "已有背景里的事实全部保留，但要用连贯的写法重新组织成一段，"
              "不要先照抄一遍再补一段。" + CAST_TAIL),
    "opening": ("写一段 {lo}~{hi} 字的开场：第二人称「你」，让 AI 角色登场，"
                "与用户角色产生第一段互动，结尾留出接话余地。" + CAST_TAIL),
    # 不受角色上限管：设定里写死的人必须全认出来，否则剧情提到他们时模型不认识
    "cast": (
        "读上面所有设定（世界观、我扮演的角色、AI 角色、角色列表、开场白、世界书），"
        "把里面真正出现过的具体人物全列出来，一人一行，格式："
        "名字｜一句话（身份、跟我的关系、此刻在干什么）。只列有名有姓、"
        "故事里露过面的人；不要“那个女孩”“众人”这种没名字的，"
        "不要把我扮演的角色列进去，不要补充设定里没提过的人。最多 12 行。"),
    # 一次写几条：世界书单条写太慢，而且条目之间要能互相区分
    "lore_batch": (
        "读上面所有设定，找出模型容易写错、或需要额外说明的专有名词与设定点，"
        "写 3 条世界书，一条一行，格式：关键词｜60~120 字设定。"
        "关键词 2~4 个，用中文逗号隔开（比如「某酒馆,城南」）。"
        "三条之间不要重复，不要写成角色卡，"
        "不要把用户已经写清楚的东西再说一遍。只输出三行，不加前言后语。"),
    "lore": ("写一条世界书条目，用来在剧情提到它时给模型补设定。"
             "第一行只写触发关键词，2~4 个词，用中文逗号隔开（比如「某酒馆,城南」），"
             "第二行起写 60~120 字的设定：是什么、谁管着、有什么规矩或秘密。"
             "不要写成角色卡，不要和已有条目重复。"),
}

def build_field_prompt(field, name="", user_name="", ai_name="", user_role="",
                       ai_role="", world="", rules="", characters=None,
                       opening="", lore=None, mode="", ai_state=""):
    if field == "character":
        who = (name or "").strip() or "AI 角色"
        lo, hi = gen_range("character")
        task = ("把「%s」写成 %d~%d 字的角色卡：外貌、性格、说话方式与口头禅、"
                "和用户角色的关系与张力。用户写过的这个人设要合并进结果，不要另起一段重复。"
                % (who, lo, hi))
        if not name.strip():
            task += "\n这个角色还没有名字：第一行写「名字：XXX」，第二行起再写设定。"
    else:
        task = FIELD_TASKS.get(field) or FIELD_TASKS["world"]
        # 设定生成的目标字数由用户设，不是写死的。用 replace 不用 format：
        # CAST_TAIL 里本来就有花括号（{主角}）
        # 用户没设（0）就跟模型档位走：档位决定人设注入多少字，写超了会被截断，
        # 所以默认区间本来就该贴着档位。设了具体数字就用用户的
        _lo, _hi = gen_range(field)
        task = task.replace("{lo}", str(_lo)).replace("{hi}", str(_hi))
    if (user_name or "").strip():
        # 生成设定时主角（= 用户扮演的角色）只给占位符。
        # AI 扮演的人不是主角，他的名字该照常给。
        #
        # 只处理两个字以上的名字：单字名（甲、乙）在中文里和普通字撞得太狠，
        # 「他站在甲板上」会变成「他站在{主角}板上」。
        # ponytail: 单字名主角在角色卡里会原样保留，升级路径是提示用户改用 {主角} 写法。
        _name = user_name.strip()
        if len(_name) >= 2:
            user_role = (user_role or "").replace(_name, PLACEHOLDER)
        user_name = PLACEHOLDER
    return render(
        "field.md",
        task=task,
        others_block=_others_block(characters),
        user_name=user_name or "（未填写）",
        ai_block=_ai_block(ai_name, ai_role, ai_state, writing()["auto_roles"]),
        user_role=(user_role or "").strip() or "（未填写，请合理创作）",
        world=(world or "").strip() or "（未填写，请合理创作）",
        # 正在生成哪个名字，就不能把那个名字锁住：
        # 任务说「起一个名字」、约束说「必须叫旧名」，只会互相抵消。
        extra=_extra_block(
            rules,
            "" if field == "user_name" else user_name,
            "" if field == "ai_name" else ai_name,
        ),
        state_block=_state_block(opening, lore, mode),
    )




def build_summary_prompt(prev_summary, text, summary_chars=None):
    # 摘要长度是写作参数之一：短了省 token 但细节留不住，长了占预算
    chars = writing()["summary_chars"] if summary_chars is None else summary_chars
    head = ""
    if prev_summary:
        head = "【已有前情提要】\n%s\n" % prev_summary
    return render("summary.md", head=head, text=text, summary_chars=chars)


_SENT_SPLIT = re.compile(r"[。！？；\n]+")
# 模型爱写的开场客套与标题，一律不是设定正文
_PREAMBLE = re.compile(
    r"^(好的|行|当然|以下是|下面是|这是|根据你|按照你|我来|让我|下面为你|为你|明白)",
)
_HEADING = re.compile(r"^[^\n。！？]{1,16}[:：]$")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 设定正文里出现整行英文，基本是模型在解释「Here is the setting:」之类
ASCII_WORD_RE = re.compile(r"[A-Za-z]{3,}")

def clean_generated(text):
    """只留设定正文：去掉开场客套、小标题、Markdown 记号与重复段落。"""
    body = strip_thinking(text or "").strip()
    if not body:
        return ""
    body = strip_status_leaks(body).replace("**", "").replace("__", "")
    body = "\n".join(
        line for line in body.splitlines()
        if not (line.strip() and not CJK_RE.search(line) and ASCII_WORD_RE.search(line))
    )
    lines = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
            if not line:
                continue
        if len(line) <= 60 and _PREAMBLE.match(line):
            continue          # 「好的，以下是角色设定：」
        if _HEADING.match(line):
            continue          # 「角色卡：」「【世界观】」这类小标题
        lines.append(line)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    # 段落级去重：同一段意思换个说法再说一遍就删掉
    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    kept = []
    for para in paras:
        norm = _norm(para)
        if len(norm) >= 20 and any(
                SequenceMatcher(None, norm, _norm(k)).ratio() > 0.75 for k in kept):
            continue
        kept.append(para)
    return "\n\n".join(kept).strip() or body

# token 估算的字符分类。不能拿 CJK_RE 算：它只认汉字，中文标点会掉进
# ASCII 那一档，而标点在词表里几乎各占一个 token，一段规则里十几处就能差出一成。
_TOKEN_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_TOKEN_PUNCT_RE = re.compile(
    r"[\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff60]")

def _tokens(text):
    """粗估 token 数，按 Qwen 系词表校准（默认模型就是 qwen）。

    汉字不是一字一个：这类词表把常用词合并成整词，实测长段中文只有 0.55～0.67。
    标点和换行反而贵，基本各占一个。对着本机 tokenizer 对过：整份
    system prompt 误差 +1%，纯英文 +10%。短的结构行（`1. 写人设`）会低估，
    但它们在总量里占比很小。

    宁可偏高：这个数是给人看「还剩多少余量」的，低估会让人以为还写得下，
    实际上已经撞到上限被截断了。
    """
    text = text or ""
    han = len(_TOKEN_HAN_RE.findall(text))
    punct = len(_TOKEN_PUNCT_RE.findall(text))
    lines = text.count("\n")
    rest = max(0, len(text) - han - punct - lines)
    return int(round(han * 0.65 + punct * 0.9 + lines * 1.0 + rest * 0.25))

def _norm(text):
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", text or "")

def _sentences(text):
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]

def _bigrams(text):
    return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) > 1 else set()

def covered_by(original, generated, min_ratio=0.35):
    """简述是否已经"被吸收"进生成结果。

    改写会换说法，所以不比字面包含，比双字组的覆盖率。
    原文不会被逐字保留，只看生成结果有没有拿简述当底稿。
    """
    src = _norm(original)
    dst = _norm(generated)
    if not src:
        return True
    if not dst or len(dst) < len(src):
        return False
    if src[:8] and src[:8] in dst:
        return True
    grams = _bigrams(src)
    if not grams:
        return True
    return len(grams & _bigrams(dst)) / len(grams) >= min_ratio

def _drop_rewritten(original, generated):
    """生成结果里跟简述重复的句子丢掉，避免同一句话说两遍。"""
    src = [_norm(s) for s in _sentences(original)]
    kept = []
    for sent in _sentences(generated):
        body = _norm(sent)
        if len(body) >= 6 and any(
                SequenceMatcher(None, body, s).ratio() > 0.55 for s in src):
            continue
        kept.append(sent)
    return "。".join(kept).strip()

def ensure_original_kept(original, generated):
    """简述是底稿：生成结果已经吸收它就整段替换，漏了才把原文补回去。"""
    original = (original or "").strip()
    generated = (generated or "").strip()
    if not original:
        return generated
    if not generated:
        return original
    # 默认整段覆盖：只要生成结果还沾着简述的内容，就让它顶掉原文，
    # 否则用户会看到「自己那句话 + 一句重写的同一个意思」。
    if covered_by(original, generated, min_ratio=0.12):
        return generated
    rest = _drop_rewritten(original, generated)
    return (original + "\n\n" + rest).strip() if rest else original
