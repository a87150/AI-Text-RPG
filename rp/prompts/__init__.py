# -*- coding: utf-8 -*-
"""提示词模块。

文案都在同目录的 .md 里，用 $变量 占位（string.Template）。
改规则、改字数表述、改格式示例都只需要动 md，不用碰代码。
"""
import os
import re
from difflib import SequenceMatcher
from string import Template

from ..characters import ensure_characters, guess_name, role_labels
from ..config import mode_for, writing
from ..llm import strip_status_leaks, strip_thinking

_DIR = os.path.dirname(os.path.abspath(__file__))


def load(name):
    with open(os.path.join(_DIR, name), encoding="utf-8") as f:
        return f.read()


def render(name, **kw):
    return Template(load(name)).safe_substitute(**kw).strip()


def build_rules(user_name, mode=None, names=""):
    w = writing()
    mode = mode or mode_for({})
    status_block = render("status.md")
    if mode == "normal":
        return render(
            "normal.md",
            user_name=user_name or "用户角色",
            character_names=names or "（暂无）",
            line_min=w["line_min"],
            line_max=w["line_max"],
            status_block=status_block,
        )
    return render(
        "rules.md",
        user_name=user_name or "用户角色",
        narrator_min=w["narrator_min"],
        narrator_max=w["narrator_max"],
        line_min=w["line_min"],
        line_max=w["line_max"],
        max_characters=w["max_characters"],
        status_block=status_block,
    )


def build_character_block(chars, speaker=None):
    lines = ["【在场角色】"]
    for c in chars:
        line = "· %s：%s" % (c["name"], c["role"] or "（未设定）")
        if c.get("state"):
            line += "｜当前：%s" % c["state"]
        if c.get("history"):
            line += "｜近况：%s" % "；".join(c["history"][-2:])
        lines.append(line)
    if speaker:
        lines.append("【本回合发言人】%s —— 这一轮只让这一位说话" % speaker)
    return "\n".join(lines)


def build_system_prompt(sess, speaker=None):
    user_name, _ = role_labels(sess)
    chars = ensure_characters(sess)
    mode = mode_for(sess)
    if mode == "rp" and not speaker:
        speaker = chars[0]["name"]
    if mode == "normal":
        speaker = ""
    names = "、".join(c["name"] for c in chars)
    parts = ["你正在进行一场沉浸式的长篇角色扮演（roleplay）。"
             if mode == "rp" else "你正在写一段沉浸式的长篇故事叙述。"]
    if sess.get("world"):
        parts.append("【故事背景与世界观】\n" + sess["world"])
    parts.append(build_character_block(chars, speaker))
    if sess.get("user_role"):
        parts.append("【用户扮演的角色：%s】\n%s" % (user_name, sess["user_role"]))
    if sess.get("summary"):
        parts.append("【前情提要（更早剧情）】\n" + sess["summary"])
    status = sess.get("status") or {}
    if status:
        labels = (("location", "地点"), ("time", "时间"), ("relations", "关系"),
                  ("items", "物品"), ("goal", "目标"))
        lines = ["【当前状态（上一轮结束时）】"]
        for key, label in labels:
            if status.get(key):
                lines.append("· %s：%s" % (label, status[key]))
        if len(lines) > 1:
            parts.append("\n".join(lines))
    book = [f for f in (sess.get("facts") or [])
            if isinstance(f, dict) and f.get("state") == "active"][-12:]
    if book:
        lines = ["【已经定下的事（不要写反过来）】"]
        for f in book:
            lines.append("· 第 %s 轮：%s" % (f.get("turn") or "?", f.get("fact") or ""))
        parts.append("\n".join(lines))
    parts.append("【旁白与扮演规则】\n" + build_rules(user_name, mode, names))
    if sess.get("rules"):
        parts.append("【额外要求】\n" + sess["rules"])
    return "\n\n".join(parts)


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
    return "【世界书（本轮相关设定）】\n" + "\n".join("· " + h["content"] for h in hits)


ANCHOR_EVERY = 15


def build_anchor_block(sess):
    """人设锚定：聊久了角色卡会被新对话稀释，隔一段把核心重插到近处。"""
    chars = ensure_characters(sess)
    lines = []
    for c in chars[:3]:
        role = (c.get("role") or "").strip()
        if role:
            lines.append("· %s：%s" % (c["name"], role[:160]))
    if not lines:
        return ""
    return ("【人设锚定 · 别把他们写成好说话的助手】\n" + "\n".join(lines)
            + "\n保持这些人的脾气、说话方式和底线，不要因为聊久了就变软、变话痨。")


def build_context_debug(sess, speaker=None):
    """这一轮到底发了什么：给「上下文」面板用，先能看见才能调。"""
    system = build_system_prompt(sess, speaker)
    recent = " ".join(
        str(m.get("content") or m.get("narrator") or "")
        for m in (sess.get("messages") or [])[-LORE_SCAN_MESSAGES:]
    )
    hits = lore_hits(sess, recent)
    lore_len = sum(len(h["content"]) for h in hits)
    turn = int(sess.get("turn") or 0)
    anchored = bool(turn and turn % ANCHOR_EVERY == 0 and build_anchor_block(sess))
    facts = [f for f in (sess.get("facts") or [])
             if isinstance(f, dict) and f.get("state") == "active"][-12:]
    facts_len = sum(len(str(f.get("fact") or "")) for f in facts)
    summary_len = len(sess.get("summary") or "")
    total = len(system) + lore_len + summary_len + facts_len
    return {
        "system": system,
        "lore": hits,
        "lore_chars": lore_len,
        "summary_chars": summary_len,
        "status": sess.get("status") or {},
        "facts": facts,
        "facts_chars": facts_len,
        "anchored": anchored,
        "anchor_every": ANCHOR_EVERY,
        "chars": {"total": total, "system": len(system)},
        "tokens": int(total * 0.7),   # 中文粗估，够用来判断"还塞不塞得下"
    }


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
    if turn and turn % ANCHOR_EVERY == 0:
        anchor = build_anchor_block(sess)
        if anchor:
            # 贴在最近几轮之前，而不是只留在最顶上被稀释
            messages.insert(max(1, len(messages) - 3), {"role": "system", "content": anchor})
    fallback = role_labels(sess)[1]
    for m in sess.get("messages") or []:
        content = m.get("content") or ""
        if not content:
            continue
        if m.get("role") == "user":
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
    # 格式要求写在 system 里离生成点太远，小模型容易忘；补在最后一条 user 尾巴上
    for item in reversed(messages):
        if item.get("role") == "user":
            item["content"] = item["content"].rstrip() + (
                "\n\n（正文写完后，另起一行写一行状态，形如："
                "[[地点：银月酒馆|时间：深夜|关系：戒备|目标：等他开口|事实：钥匙到了沈砚手里]]）"
            )
            break
    return messages


def build_locked_block(user_name, ai_name):
    lines = []
    if user_name:
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


def _ai_block(ai_name, ai_role, ai_state=""):
    """AI 角色空着的时候别让模型自己乱编，先说清这是可以推断的。"""
    name = (ai_name or "").strip()
    role = (ai_role or "").strip()
    if not name and not role:
        return ("· AI 角色还没定：从上面用户写的想法里推断一个最合适的对手或搭档，"
                "再给他一个名字，写清他为什么和用户角色纠缠在一起。")
    lines = ["· AI 的角色名：%s" % (name or "（未填写，请自拟）"), "· AI 扮演的角色原文："]
    lines.append(role or "（未填写，请合理创作，但要贴合上面用户的原话）")
    state = (ai_state or "").strip()
    if state:
        lines.append("· 他的当前状态（剧情里已经发生了，必须保留）：" + state[:120])
    return "\n".join(lines)


# 一次只写一个字段，输出短、失败只重试这一项
FIELD_TASKS = {
    "user_name": "给「我扮演的角色」起一个名字：2~4 个汉字，贴合身份与时代。只输出名字，不要解释。",
    "user_role": ("把「我扮演的角色」写成 150~220 字的角色卡：外貌、性格、处境、"
                  "在意什么、说话习惯。"),
    "ai_name": "给 AI 角色起一个名字：2~4 个汉字，贴合用户的想法。只输出名字，不要解释。",
    "world": ("把用户写的背景补全成 180~260 字的世界观：时代、地点、规则与禁忌、当前局势。"
              "已有背景里的事实全部保留，但要用连贯的写法重新组织成一段，"
              "不要先照抄一遍再补一段。"),
    "opening": ("写一段 150~220 字的开场：第二人称「你」，让 AI 角色登场，"
                "与用户角色产生第一段互动，结尾留出接话余地。"),
    "lore": ("写一条世界书条目，用来在剧情提到它时给模型补设定。"
             "第一行只写触发关键词，2~4 个词，用中文逗号隔开（比如「银月酒馆,城南」），"
             "第二行起写 60~120 字的设定：是什么、谁管着、有什么规矩或秘密。"
             "不要写成角色卡，不要和已有条目重复。"),
}


def build_field_prompt(field, name="", user_name="", ai_name="", user_role="",
                       ai_role="", world="", rules="", characters=None,
                       opening="", lore=None, mode="", ai_state=""):
    if field == "character":
        who = (name or "").strip() or "AI 角色"
        task = ("把「%s」写成 120~200 字的角色卡：外貌、性格、说话方式与口头禅、"
                "和用户角色的关系与张力。用户写过的这个人设要合并进结果，不要另起一段重复。" % who)
        if not name.strip():
            task += "\n这个角色还没有名字：第一行写「名字：XXX」，第二行起再写设定。"
    else:
        task = FIELD_TASKS.get(field) or FIELD_TASKS["world"]
    return render(
        "field.md",
        task=task,
        others_block=_others_block(characters),
        user_name=user_name or "（未填写）",
        ai_block=_ai_block(ai_name, ai_role, ai_state),
        user_role=(user_role or "").strip() or "（未填写，请合理创作）",
        world=(world or "").strip() or "（未填写，请合理创作）",
        extra=_extra_block(rules, user_name, ai_name),
        state_block=_state_block(opening, lore, mode),
    )





def build_summary_prompt(prev_summary, text, summary_chars=400):
    head = ""
    if prev_summary:
        head = "【已有前情提要】\n%s\n" % prev_summary
    return render("summary.md", head=head, text=text, summary_chars=summary_chars)


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
