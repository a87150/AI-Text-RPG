# -*- coding: utf-8 -*-
"""HTTP 路由。"""
import json
import os
import time
import uuid

import requests
from flask import Response, jsonify, render_template, request, send_file
from urllib.parse import quote as url_quote, urlsplit

from .characters import (
    advance_turn, apply_change, ensure_characters,
    merge_facts, merge_status, next_speaker, parse_turn, role_labels,
    looks_like_name, parse_cast, parse_lore_batch, split_cast_line,
    upsert_character, add_lore, parse_add_cast, refresh_states,
)
from . import backup
from . import metrics
from .llm import reasoning_supported
from .config import (
    DEFAULT_MODEL, DEFAULT_PROVIDER, MAX_CHAT_ATTEMPTS, MAX_ENHANCE_ATTEMPTS,
    MAX_OPENING_ATTEMPTS, MIN_CHAT_CHARS, MODES, PROVIDERS,
    REASONING_LEVELS, TIERS, TIER_LABELS, app, backup_conf, current_target,
    list_local_models, load_settings, mode_for, mode_of, provider_public,
    provider_usable, reasoning_effort, save_backup_conf, save_mode,
    save_provider, save_reasoning, save_tier, save_writing, set_current_model,
    CREATIVITY_LABELS, CREATIVITY_LEVELS, CREATIVITY_TEMP,
    MODE_DEFAULT, creativity, creativity_temp, delete_provider, provider_ids,
    reset_settings, save_creativity,
    tier, tier_context, tier_keep, tier_limits, writing, VERSION,
)
from .context import (
    background_compress, build_summary_prompt, compress_session, context_chars,
    context_info,
)
from .llm import completion_events, extract_json, sse, strip_thinking
from .prompts import (
    FIELD_TASKS, build_field_prompt, build_messages, build_system_prompt,
    clean_generated, ensure_original_kept,
)
from .stories import (
    apply_to, delete_story, list_stories, load_story, new_story, save_story,
    update_from, fill_names,
)
from .snapshots import list_snapshots, load_snapshot, snapshot
from .storage import (
    DEFAULT_TITLE, TEXT_FIELDS, StaleSession, get_session_lock, list_sessions,
    load_session, new_session, release_busy, save_session, session_path,
    try_acquire_busy,
)

load_settings()

def _str(data, key, limit=0):
    """取一个去空白的字符串入参。

    直接 (data.get(k) or "").strip() 遇到数字会抛 AttributeError，
    而 Flask 没有错误处理器时会返回 HTML 错误页，前端读不到 error 字段。
    类型不对当空处理，由各路由自己的必填判断去报 400。
    """
    val = data.get(key) if isinstance(data, dict) else None
    text = val.strip() if isinstance(val, str) else ""
    return text[:limit] if limit else text


# ============================== 入口守卫 ==============================
# 本机应用没有登录，一个跨站 <form method=post> 就能替用户改写设置、
# 反复建备份把历史包挤出保留份数。写请求只接受本机来源。
_SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))
_LOCAL_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))


@app.before_request
def _reject_cross_site():
    if request.method in _SAFE_METHODS:
        return None
    # 靠 Content-Type 判，不靠 Origin/Referer。跨站 <form> 本来就不发 Origin，
    # 再加一行 <meta name="referrer" content="no-referrer"> 就两个头都没了。
    # 而表单只能发 safelisted 的三种类型，发不出 application/json；
    # 真要发 JSON 就得过预检，而预检过不了。
    ctype = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return jsonify({"error": "拒绝非 JSON 写请求"}), 403
    # 有来源头的话再校一层，作为纵深
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if origin:
        host = (urlsplit(origin).hostname or "").lower()
        if host not in _LOCAL_HOSTS:
            return jsonify({"error": "拒绝跨站请求"}), 403
    return None


@app.after_request
def _security_headers(resp):
    # 不放 CSP：现有页面用了内联 style 和内联事件，上 CSP 会直接白屏。
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


# ============================== 路由 ==============================
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/config")
def api_config():
    provider, model = current_target()
    conf = writing()
    return jsonify({
        "provider": provider,
        "version": VERSION,
        "model": model,
        "mode": mode_of(),
        "writing": conf,
        "max_context_chars": conf["context_limit"],
        "keep_recent": conf["keep_recent"],
        "max_chat_attempts": MAX_CHAT_ATTEMPTS,
        "max_enhance_attempts": MAX_ENHANCE_ATTEMPTS,
        "reasoning_effort": reasoning_effort(),
        "reasoning_supported": reasoning_supported(),
        "reasoning_levels": REASONING_LEVELS,
        "tier": tier(),
        "tiers": TIERS,
        "tier_labels": TIER_LABELS,
        "tier_context": tier_context(),
        "tier_limits": tier_limits(),
        "tier_keep": tier_keep(),
        "creativity": creativity(),
        "creativities": CREATIVITY_LEVELS,
        "creativity_labels": CREATIVITY_LABELS,
        "creativity_temps": CREATIVITY_TEMP,
    })


@app.post("/api/writing")
def api_writing():
    data = request.get_json(silent=True) or {}
    conf = save_writing(data)
    if data.get("mode"):
        save_mode(data["mode"])
    if data.get("reasoning_effort"):
        save_reasoning(data["reasoning_effort"])
    if data.get("tier"):
        save_tier(data["tier"])
        # 换档会重写 context_limit / keep_recent / summary_chars。同一次请求里
        # 用户明确给过的值要压回去，不然「保存写作参数」里手调的字数白调。
        explicit = {k: data[k] for k in ("context_limit", "keep_recent", "summary_chars")
                    if k in data}
        if explicit:
            conf = save_writing(explicit)
    if data.get("creativity"):
        save_creativity(data["creativity"])
    return jsonify({"writing": conf, "mode": mode_of(),
                    "reasoning_effort": reasoning_effort(), "tier": tier(),
                    "creativity": creativity()})


@app.post("/api/writing/reset")
def api_writing_reset():
    return jsonify({"writing": reset_settings(), "mode": mode_of(),
                    "reasoning_effort": reasoning_effort(), "tier": tier(),
                    "creativity": creativity()})


@app.get("/api/search")
def api_search():
    """全文搜索：几千条消息直接扫 json 就够快，不需要索引。"""
    query = (request.args.get("q") or "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit") or 80), 200))
    except (TypeError, ValueError):
        limit = 80
    if len(query) < 2:
        return jsonify({"hits": [], "query": query})
    needle = query.lower()
    hits = []
    for item in list_sessions():
        sess = load_session(item["id"])
        if not sess:
            continue
        for index, msg in enumerate(sess.get("messages") or []):
            bits = [msg.get("content") or "", msg.get("narrator") or ""]
            for line in msg.get("lines") or []:
                bits.append((line.get("text") or ""))
            for text in bits:
                if not text or needle not in text.lower():
                    continue
                at = text.lower().index(needle)
                start = max(0, at - 40)
                snippet = text[start:start + 160].replace(chr(10), " ")
                hits.append({
                    "session_id": item["id"],
                    "title": item["title"],
                    "index": index,
                    "role": msg.get("role") or "",
                    "speaker": msg.get("speaker") or "",
                    "snippet": ("…" if start else "") + snippet,
                    "total": len(sess.get("messages") or []),
                })
                break
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    return jsonify({"hits": hits, "query": query, "truncated": len(hits) >= limit})


@app.get("/api/models")
def api_models():
    """各提供方及其模型；Ollama 掉线不影响云端模型的选择。"""
    provider, model = current_target()
    providers = []
    error = ""
    for pid in provider_ids():
        info = provider_public(pid)
        if info["kind"] != "ollama" and not provider_usable(pid):
            continue      # 还没配置的云端不进入选择列表
        if info["kind"] == "ollama":
            try:
                # 只报 Ollama 真有的模型。以前会把当前选中的模型名硬插进来，
                # 于是一个根本不存在的名字也能显示在本地列表里，选中就 404。
                info["models"] = list_local_models()
            except (requests.exceptions.RequestException, ValueError) as exc:
                error = "无法从 Ollama 获取本地模型：%s" % exc
        providers.append(info)
    return jsonify({
        "current": {"provider": provider, "model": model},
        "providers": providers,
        "error": error,
    })


@app.post("/api/model")
def api_set_model():
    data = request.get_json(silent=True) or {}
    provider = data.get("provider") or DEFAULT_PROVIDER
    if not provider_usable(provider):
        return jsonify({"error": "先在「云端模型」里配好 %s 的接口地址和 API Key"
                                 % provider_public(provider)["label"]}), 400
    try:
        provider, model = set_current_model(provider, data.get("model"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"provider": provider, "model": model})


@app.get("/api/providers")
def api_providers():
    return jsonify([provider_public(pid) for pid in provider_ids()])


@app.post("/api/providers/<pid>")
def api_save_provider(pid):
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(save_provider(pid, data))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/providers/<pid>")
def api_delete_provider(pid):
    try:
        return jsonify({"providers": delete_provider(pid), "items": provider_ids()})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/sessions")
def api_list_sessions():
    return jsonify(list_sessions())


def seed_opening(sess):
    """把设定里的开场白变成第一条消息；已经聊过就不动。"""
    chars = ensure_characters(sess)
    # 开场白里的 {主角} 换成真名；存的还是占位符，改名不用重写
    # 主角是用户扮演的角色，不是角色表第一位
    text = fill_names((sess.get("opening") or "").strip(),
                      (sess.get("user_name") or "").strip())
    if sess.get("messages") or not text:
        return
    use_rp = mode_for(sess) == "rp"
    turn = parse_turn(text, (chars[0]["name"] if chars else "") if use_rp else "",
                      [c["name"] for c in chars] if use_rp else None)
    if not (turn["line"] or turn["narrator"]):
        return
    if use_rp:
        msg = {"role": "assistant", "content": turn["line"], "speaker": turn["speaker"]}
        if turn["narrator"]:
            msg["narrator"] = turn["narrator"]
    else:
        msg = {"role": "assistant", "content": "", "narrator": turn["narrator"]}
    sess["messages"] = [msg]


def _rename_all(sess, mapping):
    """改了角色名，状态块里记着旧名的地方一起换掉。

    只改名字不动别的：不换的话模型看到“在场：甲”而角色表写着“顾辞”，当成两个人。
    """
    if not mapping:
        return
    st = sess.get("status") or {}
    for key in ("present", "away"):
        names = st.get(key)
        if isinstance(names, list):
            st[key] = [mapping.get(str(n).strip(), n) for n in names]
    for key in ("location", "time", "relations", "goal"):
        v = st.get(key)
        if isinstance(v, str) and v:
            for old, new in mapping.items():
                v = v.replace(old, new)
            st[key] = v
    items = st.get("items")
    if isinstance(items, dict):
        for old, new in mapping.items():
            if old in items and new not in items:
                items[new] = items.pop(old)
    sess["status"] = st
    # 标题里写了旧名也跟着换，但只换自动生成的那种
    title = sess.get("title") or ""
    if title:
        for old, new in mapping.items():
            title = title.replace(old, new)
        sess["title"] = title


def _renames(old_chars, new_chars):
    """按位置对比新旧角色表，找出被改掉的名字。"""
    m = {}
    for a, b in zip(old_chars or [], new_chars or []):
        an = (a.get("name") or "").strip() if isinstance(a, dict) else ""
        bn = (b.get("name") or "").strip() if isinstance(b, dict) else ""
        if an and bn and an != bn:
            m[an] = bn
    return m


def _bind_story(sess, story):
    """让会话记住自己从哪份设定来。有现成设定就只记 id，不回写内容。

    在对话里改设定改的是这一局，故事模板只有「故事」视图里那条 PUT 才能改。
    以前这里每次都 update_from + save_story，于是改对话等于改模板，
    下一局开出来也带着被污染的设定。只有一局全新的空对话才顺手建一份模板。
    """
    if story:
        sess["story_id"] = story["id"]
        return story
    story = new_story(sess.get("title") or DEFAULT_TITLE)
    update_from(story, sess)
    save_story(story)
    sess["story_id"] = story["id"]
    return story


@app.post("/api/sessions")
def api_create_session():
    data = request.get_json(silent=True) or {}
    story = load_story(data.get("story_id") or "")
    sess = new_session((data.get("title") or DEFAULT_TITLE).strip() or DEFAULT_TITLE,
                       mode_for({"mode": data.get("mode")}))
    if story:
        apply_to(sess, story)
        sess["mode"] = mode_for({"mode": data.get("mode")})
    for key in TEXT_FIELDS:
        if isinstance(data.get(key), str):
            sess[key] = data[key]
    if isinstance(data.get("lore"), list):
        sess["lore"] = [
            {"keys": str(e.get("keys") or "")[:120],
             "content": str(e.get("content") or "")[:2000]}
            for e in data["lore"] if isinstance(e, dict) and str(e.get("content") or "").strip()
        ][:40]
    if isinstance(data.get("characters"), list):
        sess["characters"] = [
            {"name": str(c.get("name") or ""), "role": str(c.get("role") or ""),
             "state": str(c.get("state") or ""), "history": c.get("history") or []}
            for c in data["characters"] if isinstance(c, dict)
        ][:writing()["max_characters"]]
    # 角色表可以是空的（用户没配就是没配），不能当它一定有人
    chars = ensure_characters(sess)
    sess["ai_name"] = chars[0]["name"] if chars else ""
    sess["ai_role"] = chars[0]["role"] if chars else ""
    _bind_story(sess, story)
    seed_opening(sess)
    save_session(sess)
    sess["context_chars"] = context_chars(sess)
    sess["context"] = context_info(sess)
    return jsonify(sess)


@app.get("/api/stories")
def api_stories():
    """故事列表；顺带带上每个故事开过几个对话。"""
    items = list_stories()
    counts = {}
    for s in list_sessions():
        key = s.get("story_id") or ""
        if key:
            counts[key] = counts.get(key, 0) + 1
    for item in items:
        item["runs"] = counts.get(item["id"], 0)
    return jsonify(items)


@app.get("/api/stories/<story_sid>")
def api_get_story(story_sid):
    """故事的完整设定，用来在设定弹窗里单独编辑它（不牵扯任何一局对话）。"""
    story = load_story(story_sid)
    if not story:
        return jsonify({"error": "故事不存在"}), 404
    return jsonify(story)


@app.post("/api/stories")
def api_create_story():
    data = request.get_json(silent=True) or {}
    story = new_story(_str(data, "title") or DEFAULT_TITLE)
    save_story(story)
    return jsonify(story), 201


@app.put("/api/stories/<story_sid>")
def api_update_story(story_sid):
    story = load_story(story_sid)
    if not story:
        return jsonify({"error": "故事不存在"}), 404
    data = request.get_json(silent=True) or {}
    update_from(story, data)
    save_story(story)
    return jsonify(story)


@app.delete("/api/stories/<story_sid>")
def api_delete_story(story_sid):
    return jsonify({"ok": delete_story(story_sid)})


# ============================== 备份 ==============================
def session_to_markdown(sess):
    """把一局对话排成可读的 Markdown：状态块当引用，账本当附录。

    这是给人的版本，不是给程序还原用的——备份 zip 那份才是。
    """
    lines = ["# %s" % (sess.get("title") or "未命名故事"), ""]
    meta = []
    if sess.get("user_name"):
        meta.append("我 = %s" % sess["user_name"])
    chars = sess.get("characters") or []
    if chars:
        meta.append("角色 = %s" % "、".join(c.get("name") or "?" for c in chars))
    if sess.get("turn"):
        meta.append("%s 轮" % sess["turn"])
    if meta:
        lines += ["*%s*" % " · ".join(meta), ""]

    if chars:
        lines += ["## 角色", ""]
        for c in chars:
            head = "- **%s**" % (c.get("name") or "?")
            if c.get("scene"):
                head += "（%s）" % c["scene"]
            lines.append(head)
            if c.get("role"):
                lines.append("  %s" % c["role"].replace("\n", " ").strip())
            if c.get("state"):
                lines.append("  状态：%s" % c["state"])
        lines.append("")

    if sess.get("world"):
        lines += ["## 背景", "", sess["world"].strip(), ""]

    lines += ["## 正文", ""]
    for m in sess.get("messages") or []:
        content = (m.get("content") or "").strip()
        narrator = (m.get("narrator") or "").strip()
        if m.get("role") == "user":
            who = sess.get("user_name") or "我"
            lines += ["> **%s**" % who, ">", "> " + content.replace("\n", "\n> "), ""]
            continue
        if narrator:
            lines += ["*%s*" % narrator, ""]
        if content:
            who = (m.get("speaker") or "").strip()
            if who:
                lines += ["**%s**：%s" % (who, content), ""]
            else:
                lines += [content, ""]
        if m.get("partial"):
            lines += ["> （这条是停止后保留的草稿）", ""]

    status = sess.get("status") or {}
    if status:
        lines += ["## 结尾时的状态", ""]
        for key, label in (("location", "场景"), ("time", "时间"),
                           ("present", "在场"), ("away", "离场"),
                           ("goal", "目标"), ("items", "物品"), ("relations", "关系")):
            value = status.get(key)
            if not value:
                continue
            if isinstance(value, list):
                value = "、".join(str(v) for v in value)
            lines.append("- %s：%s" % (label, value))
        lines.append("")

    facts = sess.get("facts") or []
    if facts:
        lines += ["## 事实账本", ""]
        for f in facts:
            text = f.get("text") if isinstance(f, dict) else str(f)
            if text:
                lines.append("- %s" % text)
        lines.append("")

    if sess.get("summary"):
        lines += ["## 前情提要", "", sess["summary"].strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


@app.get("/api/sessions/<sid>/export.md")
def api_export_markdown(sid):
    sess = load_session(sid)
    if not sess:
        return jsonify({"error": "会话不存在"}), 404
    text = session_to_markdown(sess)
    name = "".join(ch for ch in (sess.get("title") or "story")
                   if ch.strip() and ch not in '\\/:*?"<>|') or "story"
    resp = app.response_class(text, mimetype="text/markdown; charset=utf-8")
    resp.headers["Content-Disposition"] = "attachment; filename*=UTF-8''%s.md" % url_quote(name)
    return resp


@app.get("/api/backups")
def api_backups():
    return jsonify({"items": backup.listing(), "conf": backup_conf(),
                    "dir": backup.BACKUP_DIR})


@app.post("/api/backups")
def api_backup_create():
    try:
        result = backup.create("manual")
    except OSError as exc:
        return jsonify({"error": "备份失败：%s" % exc}), 500
    return jsonify({"item": result, "items": backup.listing()})


@app.get("/api/backups/<name>")
def api_backup_download(name):
    full = backup.path_of(name)
    if not full:
        return jsonify({"error": "备份不存在"}), 404
    return send_file(full, as_attachment=True, download_name=name)


@app.post("/api/backups/<name>/restore")
def api_backup_restore(name):
    result = backup.restore(name)
    if not result:
        return jsonify({"error": "备份不存在或已损坏"}), 404
    # 配置在启动时读进内存的，还原了 settings.json 得重新读一次才生效
    load_settings()
    return jsonify({"result": result, "items": backup.listing()})


@app.delete("/api/backups/<name>")
def api_backup_delete(name):
    if not backup.delete(name):
        return jsonify({"error": "备份不存在"}), 404
    return jsonify({"items": backup.listing()})


@app.put("/api/backup-config")
def api_backup_config():
    data = request.get_json(silent=True) or {}
    return jsonify(save_backup_conf(data))


@app.get("/api/sessions/<sid>/snapshots")
def api_snapshots(sid):
    if not load_session(sid):
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(list_snapshots(sid))


@app.post("/api/sessions/<sid>/snapshots/<name>/restore")
def api_restore_snapshot(sid, name):
    current = load_session(sid)
    if not current:
        return jsonify({"error": "会话不存在"}), 404
    if current.get("busy"):
        return jsonify({"error": "这一轮还在生成，等它写完再回滚"}), 409
    saved = load_snapshot(sid, name)
    if not saved:
        return jsonify({"error": "快照不存在"}), 404
    snapshot(current, "restore")     # 回滚前先留一份，回滚本身也能再回滚
    saved["id"] = sid
    save_session(saved)
    return jsonify(load_session(sid))


@app.get("/api/sessions/<sid>")
def api_get_session(sid):
    sess = load_session(sid)
    if not sess:
        return jsonify({"error": "会话不存在"}), 404
    sess["context_chars"] = context_chars(sess)
    sess["context"] = context_info(sess)
    if sess.get("story_id"):
        sess["story"] = load_story(sess["story_id"])
    sess["status"] = sess.get("status") or {}
    return jsonify(sess)


@app.post("/api/sessions/import")
def api_import_session():
    """把导出出去的会话 JSON 装回来。既与导出对称，也是换机器时的底。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "不是一份会话文件"}), 400
    msgs = data.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return jsonify({"error": "这份文件里没有对话记录"}), 400
    if len(msgs) > 20000:
        return jsonify({"error": "对话太长，导入不了"}), 400

    clean = []
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        item = {"role": m["role"]}
        for key in ("content", "narrator", "speaker"):
            if isinstance(m.get(key), str):
                item[key] = m[key]
        if isinstance(m.get("lines"), list):
            item["lines"] = [{"speaker": str(x.get("speaker") or ""),
                              "text": str(x.get("text") or "")}
                             for x in m["lines"] if isinstance(x, dict)]
        if isinstance(m.get("turn"), int):
            item["turn"] = m["turn"]
        clean.append(item)
    if not clean:
        return jsonify({"error": "这份文件里没有可用的消息"}), 400

    sess = new_session(title=(str(data.get("title") or "导入的对话"))[:40] + "（导入）")
    for key in TEXT_FIELDS:
        if isinstance(data.get(key), str):
            sess[key] = data[key]
    if data.get("mode") in MODES:
        sess["mode"] = data["mode"]
    if isinstance(data.get("characters"), list):
        sess["characters"] = [c for c in data["characters"] if isinstance(c, dict) and c.get("name")]
    if isinstance(data.get("status"), dict):
        sess["status"] = data["status"]
    if isinstance(data.get("facts"), list):
        sess["facts"] = [f for f in data["facts"] if isinstance(f, dict)][:200]
    if isinstance(data.get("lore"), list):
        sess["lore"] = [e for e in data["lore"] if isinstance(e, dict)][:40]
    if isinstance(data.get("summary"), str):
        sess["summary"] = data["summary"]
    sess["messages"] = clean
    sess["turn"] = len([m for m in clean if m["role"] == "assistant"])
    save_session(sess)
    return jsonify({"id": sess["id"], "title": sess["title"],
                    "messages": len(clean), "characters": len(sess.get("characters") or [])})


@app.put("/api/sessions/<sid>")
def api_update_session(sid):
    data = request.get_json(silent=True) or {}
    expect = data.get("updated_at")
    with get_session_lock(sid):
        sess = load_session(sid)
        if not sess:
            return jsonify({"error": "会话不存在"}), 404
        # 别的标签页已经写过了就不能整份覆盖，否则对面刚聊的内容无声消失
        if expect is not None and sess.get("updated_at") is not None:
            try:
                if abs(float(sess["updated_at"]) - float(expect)) > 1e-6:
                    metrics.note("session.stale", sid=sid, expected=expect,
                                 actual=sess["updated_at"], path="update_session")
                    return jsonify({"error": "这个对话在别处更新过，请重新打开再改",
                                    "conflict": True}), 409
            except (TypeError, ValueError):
                pass
        snapshot(sess, "settings")     # 改设定前留底，改坏了能退回来
        for key in TEXT_FIELDS:
            if isinstance(data.get(key), str):
                sess[key] = data[key]
        if sess.get("title") in ("", DEFAULT_TITLE, "新对话") and sess.get("ai_name"):
            sess["title"] = "与%s的故事" % sess["ai_name"]
        if data.get("mode") in MODES:
            sess["mode"] = data["mode"]
        if isinstance(data.get("lore"), list):
            sess["lore"] = [
                {"keys": str(e.get("keys") or "")[:120],
                 "content": str(e.get("content") or "")[:2000]}
                for e in data["lore"] if isinstance(e, dict) and str(e.get("content") or "").strip()
            ][:40]
        wanted = data.get("status")
        if isinstance(wanted, dict):
            # 模型判断不准时，用户手动纠正在场名单：只认这几个字段
            patch = {}
            for key in ("present", "away"):
                names = wanted.get(key)
                if isinstance(names, list):
                    patch[key] = [str(n).strip()[:24] for n in names if str(n).strip()]
            for key in ("location", "time", "relations", "items", "goal"):
                if isinstance(wanted.get(key), str) and wanted[key].strip():
                    patch[key] = wanted[key].strip()[:160]
            if patch:
                sess["status"] = merge_status(sess.get("status"), patch)
        custom_chars = isinstance(data.get("characters"), list)
        _old_chars = [dict(c) for c in (sess.get("characters") or []) if isinstance(c, dict)]
        if custom_chars:
            # 用户手工编辑/删除/新增的角色表优先，随后由 ensure_characters 归一化
            sess["characters"] = [
                {
                    "name": str(c.get("name") or ""),
                    "role": str(c.get("role") or ""),
                    "state": str(c.get("state") or ""),
                    "history": c.get("history") or [],
                }
                for c in data["characters"] if isinstance(c, dict)
            ][:writing()["max_characters"]]
        chars = ensure_characters(sess)
        if custom_chars:
            # 角色表是唯一来源：第一个角色就是「AI 角色」，兼容旧字段。
            # 空表也要能存：用户把人全删了就是不想有人
            sess["ai_name"] = chars[0]["name"] if chars else ""
            sess["ai_role"] = chars[0]["role"] if chars else ""
            _rename_all(sess, _renames(_old_chars, sess["characters"]))
            if chars and sess.get("title") in ("", DEFAULT_TITLE, "新对话"):
                sess["title"] = "与%s的故事" % chars[0]["name"]
        elif len(chars) <= 1:
            # 设定面板里填的名字/人设就是那个角色的。
            # 一个都没有但填了名字：那是用户自己写的，建一个。
            # 名字也没填：就是不想有人，不要凭空造
            name = (sess.get("ai_name") or "").strip()
            role = (sess.get("ai_role") or "").strip()
            if not chars:
                if name:
                    chars = [{"name": name[:24], "role": role[:400],
                              "state": "", "history": []}]
                    sess["characters"] = chars
            else:
                if name:
                    chars[0]["name"] = name[:24]
                if role:
                    chars[0]["role"] = role[:400]
        seed_opening(sess)
        save_session(sess)
        sess["context_chars"] = context_chars(sess)
        sess["context"] = context_info(sess)
        return jsonify(sess)


def _state_snapshot(sess):
    """当下这一眼看得到的现场：场景格 + 每个人的状态一句。

    存得很小（几百字节），但没它就无法回滚：场景和状态都是在旧值上叠加的，
    只删消息的话它们会停在被撤销那一轮的样子，模型就以为那件事还发生着。
    """
    states = {}
    for c in sess.get("characters") or []:
        if isinstance(c, dict) and (c.get("name") or "").strip():
            states[c["name"]] = c.get("state") or ""
    st = sess.get("status")
    return {"status": dict(st) if isinstance(st, dict) else {}, "states": states}


def _restore_snapshot(sess, snap):
    """把现场恢复成快照里的样子；没快照就清空，宁可空着也不能留错的。"""
    snap = snap if isinstance(snap, dict) else {}
    st = snap.get("status")
    sess["status"] = dict(st) if isinstance(st, dict) else {}
    states = snap.get("states") if isinstance(snap.get("states"), dict) else {}
    for c in sess.get("characters") or []:
        if isinstance(c, dict):
            c["state"] = states.get(c.get("name") or "", "")


def _rewind_to(sess, index):
    """把会话退回到 index 之前，轮次和账本一起退。

    只删消息不退轮次是个隐蔽的污染源：重写几次之后 turn 会飘到几十，
    而上下文里只剩几轮内容。模型看到的账本写着「第 28 轮：xxx」，
    实际它手上的历史只有八轮，时间感就散了；锚定和快照的频率也跟着错。
    """
    msgs = sess.get("messages") or []
    mark = 0
    for m in reversed(msgs[:index]):
        if isinstance(m.get("turn"), int):
            mark = m["turn"]
            break
    sess["messages"] = msgs[:index]
    sess["turn"] = mark
    # 游标把剩下的消息也盖住了就退回去，保证重写点前面还留着一截原文：
    # 只剩摘要的话，模型接不上刚才的语气和细节
    _cur = int(sess.get("compressed_upto") or 0)
    _buff = writing()["keep_recent"]
    if _cur > index - _buff:
        sess["compressed_upto"] = max(0, index - _buff)
    # 摘要是被删那批消息压出来的：游标退回去之后，它们会重新进上下文，
    # 而旧摘要里还记着已经撤销的情节。原文都在 messages 里，
    # 直接清掉让后台按剩下的内容重算——不清的话模型会接着一段作废剧情往下写。
    if sess.get("summary"):
        sess["summary"] = ""
        sess["compressed_upto"] = 0
    facts = sess.get("facts")
    if isinstance(facts, list):
        # 账本的 turn 记在 advance_turn 之前，比消息的 turn 小 1，所以用严格小于。
        # 不能给 mark 保底成 1：退到开头时 mark=0，那保底会把第一轮
        # （已经被撤销）的事实全留下来，模型就以为那件事还在。
        sess["facts"] = [f for f in facts
                         if isinstance(f, dict) and int(f.get("turn") or 0) < mark]
    lore = sess.get("lore")
    if isinstance(lore, list):
        # 老存档里的词条没有 turn，不能当成 0 一起删掉
        sess["lore"] = [e for e in lore
                        if not isinstance(e, dict) or "turn" not in e
                        or int(e.get("turn") or 0) < mark]
    # 场景和角色状态回到剩下那一轮的现场
    left = sess.get("messages") or []
    snap = None
    for m in reversed(left):
        if isinstance(m.get("snap"), dict):
            snap = m["snap"]
            break
    _restore_snapshot(sess, snap)
    return sess


@app.post("/api/sessions/<sid>/lore/promote")
def api_promote_lore(sid):
    """把这一局攒的词条存进故事模板。

    只在你点的时候发生，不会自动回写。只增不改：模板里已有同名 keys 的跳过。
    """
    data = request.get_json(silent=True) or {}
    keys = _str(data, "keys")
    everything = bool(data.get("all"))
    with get_session_lock(sid):
        sess = load_session(sid)
        if not sess:
            return jsonify({"error": "会话不存在"}), 404
        story = load_story(sess.get("story_id") or "")
        if not story:
            return jsonify({"error": "这一局没有关联的故事模板"}), 404
        pool = [e for e in (sess.get("lore") or []) if isinstance(e, dict)]
        raw = data.get("entries")
        if isinstance(raw, list):
            # 面板传过来的草稿优先：用户可能改了还没点保存
            picked = [e for e in raw
                      if isinstance(e, dict) and str(e.get("keys") or "").strip()]
        elif everything:
            picked = pool
        else:
            picked = [e for e in pool if (e.get("keys") or "").strip() == keys]
        if not picked:
            return jsonify({"error": "没找到要存的词条"}), 404
        cards = story.get("lore")
        if not isinstance(cards, list):
            cards = story["lore"] = []
        known = {(c.get("keys") or "").strip().lower()
                 for c in cards if isinstance(c, dict)}
        added = 0
        for e in picked:
            k = (e.get("keys") or "").strip()
            if not k or k.lower() in known:
                continue
            cards.append({"keys": k[:120], "content": str(e.get("content") or "")[:2000]})
            known.add(k.lower())
            added += 1
        if added:
            save_story(story)
        return jsonify({"ok": True, "added": added, "story_lore": cards})


@app.post("/api/sessions/<sid>/rewind")
def api_rewind(sid):
    """删掉 index 起的消息，用来重写。ponytail: 前情提要不动，压缩是单向的。"""
    data = request.get_json(silent=True) or {}
    try:
        index = int(data.get("index"))
    except (TypeError, ValueError):
        return jsonify({"error": "索引不合法"}), 400
    with get_session_lock(sid):
        sess = load_session(sid)
        if not sess:
            return jsonify({"error": "会话不存在"}), 404
        msgs = sess.get("messages") or []
        if index < 0 or index > len(msgs):
            return jsonify({"error": "索引超出范围"}), 400
        _rewind_to(sess, index)
        save_session(sess)
        sess["context_chars"] = context_chars(sess)
        sess["context"] = context_info(sess)
        return jsonify(sess)


@app.post("/api/sessions/<sid>/branch")
def api_branch(sid):
    """把会话在 index 处截断后复制成新的一局，原对话不受影响。"""
    data = request.get_json(silent=True) or {}
    try:
        index = int(data.get("index"))
    except (TypeError, ValueError):
        return jsonify({"error": "索引不合法"}), 400
    src = load_session(sid)
    if not src:
        return jsonify({"error": "会话不存在"}), 404
    msgs = src.get("messages") or []
    if index < 0 or index > len(msgs):
        return jsonify({"error": "索引超出范围"}), 400

    now = time.time()
    cloned = json.loads(json.dumps(src))
    cloned["id"] = uuid.uuid4().hex[:12]
    cloned["title"] = ((src.get("title") or DEFAULT_TITLE) + "（分支）")[:40]
    # 深拷贝会把「此刻」的状态和账本一起带过去，分叉点之后的事就成了剧透。
    # 和「从这里重写」共用同一套回滚，免得两处规则漂移
    _rewind_to(cloned, index + 1)
    cloned["created_at"] = now
    cloned["updated_at"] = now
    save_session(cloned)
    cloned["context_chars"] = context_chars(cloned)
    cloned["context"] = context_info(cloned)
    return jsonify(cloned)


@app.post("/api/sessions/<sid>/commit_partial")
def api_commit_partial(sid):
    """把用户中途停下来的半段内容当正式回复存下。

    停止时后端那一轮已经被掐断，不会自己写盘，所以这里补一条；
    之后用户接着写，上下文里就有这段。
    """
    data = request.get_json(silent=True) or {}
    content = _str(data, "content")
    if not content:
        return jsonify({"error": "没有内容可存"}), 400
    speaker = _str(data, "speaker")
    # 整段进锁：读-改-写分开做的话，这里添的草稿会和
    # /api/chat 提交的完整回复互相覆盖。
    with get_session_lock(sid):
        sess = load_session(sid)
        if not sess:
            return jsonify({"error": "会话不存在"}), 404
        # 赛跑：用户点停止的同时后端也可能把完整回复写进去了。
        # 已经有一条 assistant 把这段包含进去了就不再重复存，否则一轮会变成两条。
        msgs = sess.setdefault("messages", [])
        last = msgs[-1] if msgs else None
        if last and last.get("role") == "assistant":
            existing = (last.get("content") or "").strip()
            if existing and (existing.startswith(content) or content.startswith(existing)):
                out = dict(sess)
                out["context_chars"] = context_chars(sess)
                out["context"] = context_info(sess)
                return jsonify(out)
        msg = {"role": "assistant", "content": content, "partial": True}
        if speaker:
            msg["speaker"] = speaker
        msg["turn"] = sess.get("turn") or 0
        msgs.append(msg)
        try:
            save_session(sess, expect=data.get("updated_at"))
        except StaleSession as exc:
            metrics.note("session.stale", sid=sid, expected=exc.expected, actual=exc.actual, path="commit_partial")
            return jsonify({"error": "这个对话在别处更新过，已重新读取",
                            "conflict": True}), 409
        out = dict(sess)
        out["context_chars"] = context_chars(sess)
        out["context"] = context_info(sess)
        return jsonify(out)


@app.put("/api/sessions/<sid>/message")
def api_edit_message(sid):
    """就地改一条消息的文本。"""
    data = request.get_json(silent=True) or {}
    text = _str(data, "content")
    try:
        index = int(data.get("index"))
    except (TypeError, ValueError):
        return jsonify({"error": "索引不合法"}), 400
    with get_session_lock(sid):
        sess = load_session(sid)
        if not sess:
            return jsonify({"error": "会话不存在"}), 404
        msgs = sess.get("messages") or []
        if index < 0 or index >= len(msgs):
            return jsonify({"error": "索引超出范围"}), 400
        msg = msgs[index]
        if msg.get("role") != "user":
            # 只让用户改自己说过的话；改 AI 的台词既不自然，也会让状态对不上
            return jsonify({"error": "只能修改自己发的消息"}), 400
        if text != (msg.get("content") or ""):
            msg["content"] = text
            msg["edited"] = True    # 界面上留个记号：后面的回复是对着旧话写的
        save_session(sess)
        sess["context_chars"] = context_chars(sess)
        sess["context"] = context_info(sess)
        return jsonify(sess)


@app.delete("/api/sessions/<sid>")
def api_delete_session(sid):
    path = session_path(sid)
    if path and os.path.isfile(path):
        with get_session_lock(sid):
            try:
                os.remove(path)
            except OSError:
                pass
    return jsonify({"ok": True})


@app.post("/api/sessions/<sid>/clear")
def api_clear_session(sid):
    with get_session_lock(sid):
        sess = load_session(sid)
        if not sess:
            return jsonify({"error": "会话不存在"}), 404
        sess["messages"] = []
        sess["summary"] = ""
        sess.pop("compress", None)  # 清空对话后压缩统计也归零
        save_session(sess)
    return jsonify({"ok": True, "context_chars": context_chars(sess),
                    "context": context_info(sess)})


@app.post("/api/sessions/<sid>/character-pick")
def api_character_pick(sid):
    """角色满了以后用户的选择：drop 传名字就踢掉那个人换新的，传空就不要新角色。"""
    data = request.get_json(silent=True) or {}
    drop = _str(data, "drop")
    with get_session_lock(sid):
        sess = load_session(sid)
        if not sess:
            return jsonify({"error": "会话不存在"}), 404
        pending = sess.get("pending_character") or {}
        name = (pending.get("name") or "").strip()
        if not name:
            return jsonify({"error": "没有待定角色"}), 400
        if drop:
            sess["characters"] = [c for c in (sess.get("characters") or [])
                                  if (c.get("name") or "") != drop]
            sess.pop("pending_character", None)
            upsert_character(sess, pending)
        else:
            sess.pop("pending_character", None)
        save_session(sess)
        chars = ensure_characters(sess)
    return jsonify({"ok": True, "characters": chars,
                    "dropped": drop or "", "added": name if drop else ""})


@app.post("/api/sessions/<sid>/compress")
def api_compress_session(sid):
    if not try_acquire_busy(sid):
        return jsonify({"error": "该会话正在生成或压缩中，请稍候"}), 409
    try:
        ok = compress_session(sid, force=True)
        sess = load_session(sid)
    finally:
        release_busy(sid)
    return jsonify({
        "ok": ok,
        "summary": (sess or {}).get("summary", ""),
        "message_count": len((sess or {}).get("messages") or []),
        "context_chars": context_chars(sess) if sess else 0,
        "context": context_info(sess),
    })


@app.post("/api/enhance")
def api_enhance():
    """一次只生成一个字段：输出短、失败只重试这一项。"""
    data = request.get_json(silent=True) or {}
    field = _str(data, "field")
    name = _str(data, "name")

    user_role = _str(data, "user_role")
    ai_role = _str(data, "ai_role")
    world = _str(data, "world")
    rules = _str(data, "rules")
    user_name = _str(data, "user_name")
    ai_name = _str(data, "ai_name")
    characters = data.get("characters")
    if not isinstance(characters, list):
        characters = []
    opening = _str(data, "opening")
    lore = data.get("lore")
    if not isinstance(lore, list):
        lore = []
    mode = _str(data, "mode")

    if field == "ai_role":      # 老前端调用的名字，统一成 character
        field = "character"
        name = name or ai_name
    if field not in FIELD_TASKS and field != "character":
        return jsonify({"error": "未知的生成项"}), 400
    if field in ("user_role", "world", "opening", "lore") and not (
            user_role or ai_role or world or characters or lore or rules):
        return jsonify({"error": "先写一点初始想法，比如身份或场景"}), 400

    # 当前这个角色已经推进到的状态也要一并给模型看
    ai_state = ""
    for c in characters:
        if not isinstance(c, dict):
            continue
        cname = str(c.get("name") or "").strip()
        if name and cname != name:
            continue
        ai_state = str(c.get("state") or "").strip()
        if ai_state:
            break

    prompt = build_field_prompt(
        field, name=name, user_name=user_name, ai_name=ai_name, user_role=user_role,
        ai_role=ai_role, world=world, rules=rules, characters=characters,
        opening=opening, lore=lore, mode=mode, ai_state=ai_state,
    )

    is_name = field in ("user_name", "ai_name")
    min_len = 1 if is_name else (3 if field in ("cast", "lore_batch") else 40)
    parsed = {}

    def validate(text):
        body = strip_thinking(text or "").strip()
        if is_name:
            body = body.strip('"“”「」『』').splitlines()[0].strip() if body else ""
            body = body[:8]
        if len(body) < min_len:
            return "内容太短（%d 字）" % len(body)
        parsed["text"] = body
        return None

    def generate():
        for ev in completion_events(
            [{"role": "user", "content": prompt}],
            # 设定整理要贴着用户原话走，温度压住发散
            base_temp=0.45,
            max_attempts=MAX_ENHANCE_ATTEMPTS,
            extra={"max_tokens": 800},
            validate=validate,
            with_reasoning=True,
        ):
            if ev["type"] == "final":
                parsed["attempts"] = ev["attempts"]
                break
            if ev["type"] == "error":
                yield sse({
                    "type": "error",
                    "message": ev["message"] + "。这一项可以单独重试。",
                    "raw": ev.get("raw") or "",
                })
                return
            yield sse(ev)

        text = parsed.get("text") or ""
        if not is_name:
            text = clean_generated(text)
        if not text:
            yield sse({"type": "error", "message": "这一项没生成出内容，请重试。"})
            return
        if not is_name:
            # 原文硬保护：用户写的那句必须还在
            keep = user_role if field == "user_role" else world if field == "world" else ""
            if field == "character" and name:
                keep = next((str(c.get("role") or "") for c in characters
                             if str(c.get("name") or "").strip() == name), "")
            if keep:
                text = ensure_original_kept(keep, text)

        cast_names = []
        if field in ("world", "opening"):
            # 名单行是给代码看的，剥掉再存正文
            text, cast_names = split_cast_line(text)
        payload = {"field": field, "name": name, "text": text,
                   "attempts": parsed.get("attempts", 1)}
        if field == "cast":
            # 名单直接解析成结构，前端拿去并入角色表
            payload["cast"] = parse_cast(text)
        elif cast_names:
            payload["cast"] = [{"name": n, "role": ""} for n in cast_names]
        if field == "lore_batch":
            payload["lore"] = parse_lore_batch(text)
        yield sse({
            "type": "result",
            "field": field,
            "name": name,
            "attempts": parsed.get("attempts", 1),
            "data": payload,
        })

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/sessions/<sid>/opening")
def api_opening(sid):
    if not try_acquire_busy(sid):
        return jsonify({"error": "该会话正在生成中，请稍候"}), 409

    sess = load_session(sid)
    if not sess:
        release_busy(sid)
        return jsonify({"error": "会话不存在"}), 404
    if sess.get("messages"):
        release_busy(sid)
        return jsonify({"error": "当前对话已有内容，请先「清空」再重新生成开场"}), 400

    use_rp = mode_for(sess) == "rp"
    chars = ensure_characters(sess)
    first_speaker = next_speaker(sess)
    ask = ("请写出这场角色扮演的开场：先来一小段旁白，再让在场角色各自说一句。"
           ) if use_rp else (
           "请写出这段故事的开场：用旁白口吻交代场景与气氛，"
           "让在场角色自然出现并做出反应，200~300 字。")
    messages = [
        {"role": "system", "content": build_system_prompt(sess, first_speaker)},
        {"role": "user", "content": ask},
    ]

    def generate():
        try:
            yield sse({"type": "speaker", "name": first_speaker})
            text, attempts_used = yield from _stream_model(
                messages, first_speaker, 0.9, MAX_OPENING_ATTEMPTS, use_rp)
            if not text:
                return

            turn = parse_turn(text, first_speaker, [c["name"] for c in chars] if use_rp else None)
            body = turn["line"] if use_rp else turn["narrator"]
            if not body:
                yield sse({"type": "error", "message": "开场没有可用内容，请重试。"})
                return

            with get_session_lock(sid):
                s2 = load_session(sid)
                if s2 and not s2.get("messages"):
                    s2["messages"] = []       # 先清空，_commit_turn 往里塞第一条
                    _commit_turn(s2, turn, use_rp)
                    s2["opening"] = body
                    save_session(s2)
                    sess["messages"] = s2["messages"]
                    sess["opening"] = turn["line"]
                    sess["characters"] = s2.get("characters")
                    sess["turn"] = s2.get("turn")

            yield sse({
                "type": "done",
                "attempts": attempts_used,
                "context_chars": context_chars(sess),
                "context": context_info(sess),
                "message_count": len(sess.get("messages") or []),
                "turn": turn,
                "characters": ensure_characters(sess),
            })
        finally:
            release_busy(sid)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _stream_model(messages, speaker, temperature, max_attempts, use_rp):
    """把 completion_events 包成 SSE 事件流；成功时返回 (正文, 尝试次数)。

    用 yield from 的返回值传结果，调用方不用再写一遍同样的循环。
    """
    for ev in completion_events(
        messages, base_temp=temperature, max_attempts=max_attempts,
        min_chars=MIN_CHAT_CHARS,
        validate=lambda t: None if (not use_rp or "【" in t)
        else "缺少【旁白】/【角色名】格式",
    ):
        if ev["type"] == "delta":
            metrics.first_token()      # 用户感知的等待到此结束
        if ev["type"] == "final":
            # 服务端报多少就记多少；报不出来就是空字典，面板显示—
            metrics.record(ev.get("usage") or {}, current_target()[1])
            return ev["text"], ev["attempts"]
        if ev["type"] == "error":
            yield sse({"type": "error", "message": ev["message"] + "。请稍后手动重试。"})
            return None, 0
        yield sse(ev)
    yield sse({"type": "error", "message": "生成失败，请稍后手动重试。"})
    return None, 0


def _join_text(prev, extra):
    """续写的拼接：模型常把上一句的末尾重复一遍再往下写，能对上就切掉。"""
    if not extra:
        return prev
    if not prev:
        return extra
    for n in (6, 4, 2, 1):
        head = extra[:n]
        if head and prev.endswith(head):
            extra = extra[n:]
            break
    return prev + extra


def _commit_turn(s2, turn, use_rp, resume=False):
    """一轮生成之后要落盘的所有副作用：消息、状态、账本、角色、轮次、存档。

    resume=True 表示这是上一条回复的延续：内容拼到尾上，不新增消息，
    也不推进轮次——那还是同一轮。
    """
    msgs = s2.setdefault("messages", [])
    if resume and msgs and msgs[-1].get("role") == "assistant":
        tail = msgs[-1]
        if use_rp:
            tail["content"] = _join_text(tail.get("content") or "", turn["line"])
            if turn["narrator"]:
                tail["narrator"] = _join_text(tail.get("narrator") or "", turn["narrator"])
            if turn.get("lines"):
                merged = list(tail.get("lines") or [])
                for line in turn["lines"]:
                    if merged and merged[-1].get("speaker") == line.get("speaker"):
                        merged[-1] = {"speaker": line["speaker"],
                                      "text": _join_text(merged[-1].get("text") or "", line.get("text") or "")}
                    else:
                        merged.append(dict(line))
                tail["lines"] = merged
        else:
            tail["narrator"] = _join_text(tail.get("narrator") or "", turn["narrator"])
        tail.pop("partial", None)
        if turn.get("status"):
            s2["status"] = merge_status(s2.get("status"), turn["status"])
        merge_facts(s2, turn.get("facts"), s2.get("turn"))
        # 对话里新长出来的词条只进这一局的世界书，不碰故事模板
        for entry in turn.get("lore") or []:
            add_lore(s2, entry, s2.get("turn"))
        tail["snap"] = _state_snapshot(s2)      # 续写也得把现场刷新
        save_session(s2)
        return tail
    if use_rp:
        msg = {"role": "assistant", "content": turn["line"], "speaker": turn["speaker"]}
        if turn["narrator"]:
            msg["narrator"] = turn["narrator"]
        if turn.get("lines"):
            msg["lines"] = turn["lines"]      # 一轮里每人一段，前端按这个排
    else:
        # 系统叙述：正文放 narrator，前端渲染成旁白块
        msg = {"role": "assistant", "content": "", "narrator": turn["narrator"]}
    s2.setdefault("messages", []).append(msg)
    if turn.get("status"):
        s2["status"] = merge_status(s2.get("status"), turn["status"])
    merge_facts(s2, turn.get("facts"), s2.get("turn"))
    for entry in turn.get("lore") or []:
        add_lore(s2, entry, s2.get("turn"))
    # 状态和角色当前情形都是增量更新的，光删消息它们还停在被撤销
    # 那一轮；每条回复存一份现场，重发时退到哪就恢复到哪。
    msg["snap"] = _state_snapshot(s2)
    # 一轮里模型可能一次加好几个人（【新角色】后面连写几行 JSON）
    fresh = turn.get("new_characters") or ([turn["new_character"]] if turn["new_character"] else [])
    for item in fresh:
        upsert_character(s2, item)
    # 模型常把新人物只写进在场名单、不给【新角色】标签，光等标签永远等不到。
    # 名单里出现不认识的名字就补一张空卡，人设留白，等模型下一轮再补【变化】。
    me, _ai = role_labels(s2)
    st = s2.get("status") or {}
    known = {(c.get("name") or "").strip() for c in ensure_characters(s2)}
    for raw in list(st.get("present") or []) + list(st.get("away") or []):
        nm = str(raw or "").strip()
        if nm == me or nm in known or not looks_like_name(nm):
            continue
        known.add(nm)
        fresh.append({"name": nm})
        upsert_character(s2, {"name": nm})
    for upd in turn["updates"]:
        apply_change(s2, upd)
    advance_turn(s2)                  # 一般模式也要数轮次，锚定和存档都靠它
    turn_no = int(s2.get("turn") or 0)
    msg["turn"] = turn_no             # 分叉要靠它把账本和轮次退回去
    if turn_no > 0 and turn_no % 15 == 0:
        snapshot(s2, "turn")
    # 正文写完了，再单独过一次角色状态。
    # 指望模型在正文里自己补【变化】靠不住：29 个角色里只有 7 个有 state。
    # 续写跳过：那只是一截增量，拿它判状态会误判。失败就当没发生。
    if not resume:
        refresh_states(s2, (turn.get("narrator") or "") + chr(10) + (turn.get("line") or ""))
    save_session(s2)
    return msg


@app.post("/api/chat")
def api_chat():
    data = request.get_json(silent=True) or {}
    sid = data.get("session_id")
    text = _str(data, "message")
    is_retry = bool(data.get("retry"))
    is_continue = bool(data.get("continue"))

    if not text and not is_continue:
        return jsonify({"error": "消息不能为空"}), 400
    if not session_path(sid):
        return jsonify({"error": "非法的会话 ID"}), 400

    if not try_acquire_busy(sid):
        return jsonify({"error": "上一条消息还在生成中，请稍候再试"}), 409

    sess_mode = mode_of()
    try:
        with get_session_lock(sid):
            sess = load_session(sid)
            if not sess:
                raise LookupError("会话不存在")
            sess_mode = mode_for(sess)
            # 不能用 sess.get("messages") or []：空列表是 falsy，
            # 会返回临时列表，append 进去的第一条消息永远落不了盘
            msgs = sess.setdefault("messages", [])
            if not isinstance(msgs, list):
                msgs = sess["messages"] = []
            # 续写：不新增 user 消息，把已经写出的半截当成待接的上文
            resume_from = ""
            if is_continue and msgs and msgs[-1].get("role") == "assistant":
                resume_from = (msgs[-1].get("content") or msgs[-1].get("narrator") or "").strip()
            last_is_same_user = (
                bool(msgs)
                and msgs[-1].get("role") == "user"
                and msgs[-1].get("content") == text
            )
            if is_continue:
                if not resume_from:
                    release_busy(sid)
                    return jsonify({"error": "上一条不是没写完的回复，无从接起"}), 400
            elif not (is_retry and last_is_same_user):
                msgs.append({"role": "user", "content": text})
            # 用户在这句里点了名要加人：直接建卡，不等模型写【新角色】
            for _nm in parse_add_cast(text):
                upsert_character(sess, {"name": _nm})
            if sess.get("title") in ("", DEFAULT_TITLE, "新对话"):
                sess["title"] = text[:16]
            speaker = next_speaker(sess)      # 现在恒为空：一轮里在场角色各自发言
            cast = [c["name"] for c in ensure_characters(sess)]
            save_session(sess)
            messages = build_messages(sess, speaker)
            if resume_from:
                messages.append({"role": "assistant", "content": resume_from})
                messages.append({"role": "user", "content": (
                    "上一条回复被截断了。从它停下的那个字接着写，"
                    "不要重复已经写过的内容，不要重新开头，不要加任何说明。"),
                })
    except LookupError as exc:
        release_busy(sid)
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        release_busy(sid)
        app.logger.exception("追加用户消息失败：%s", exc)
        # 异常串里带着路径和 JSON 结构，只进日志不进响应
        metrics.note("session.save_fail", sid=sid, error=str(exc)[:300])
        return jsonify({"error": "保存消息失败，详情见后端日志"}), 500

    # 前端不再传温度，改成创意档位；显式传的 temperature 仍然优先（兼容旧页面）
    try:
        temperature = float(data.get("temperature", creativity_temp()))
    except (TypeError, ValueError):
        temperature = creativity_temp()
    temperature = max(0.0, min(1.5, temperature))

    _prompt_chars = sum(len(m.get("content") or "") for m in messages)
    _system_chars = sum(len(m.get("content") or "") for m in messages
                        if m.get("role") == "system")
    metrics.begin(sid, _prompt_chars, _system_chars, len(messages))

    cancelled = {"flag": False}      # 客户端断开时置位，写盘前必须看一眼

    def generate():
        use_rp = sess_mode == "rp"
        try:
            # 先告诉前端这轮由谁发言，流式渲染才能丢掉别人抢话的段落
            yield sse({"type": "speaker", "name": speaker})
            answer, attempts_used = yield from _stream_model(
                messages, speaker, temperature, tier_limits()["attempts"], use_rp)
            if answer is None:
                return

            turn = parse_turn(answer, speaker, cast if use_rp else None)
            if not turn.get("status") and ("[[" in answer or "【地点" in answer):
                # 只记长度和指纹：原文是私人剧情，落进日志就是长期可 grep
                metrics.note("status.parse_fail", sid=sid, mode="rp" if use_rp else "normal",
                             raw_len=len(answer), raw_tail=answer[-40:].replace(chr(10), " "))
            if use_rp and not (turn["lines"] or turn["narrator"]):
                yield sse({"type": "error",
                           "message": "模型这一轮没写出内容（旁白和角色发言都空），请重试。"})
                return
            if not use_rp and not turn["narrator"]:
                yield sse({"type": "error", "message": "模型没有给出内容，请重试。"})
                return

            if cancelled["flag"]:
                metrics.note("stream.cancel", sid=sid, stage="before_commit")
                return                # 用户已经点了停止，不要把没要的东西写进去
            with get_session_lock(sid):
                if cancelled["flag"]:
                    return
                s2 = load_session(sid)
                if s2 is not None:
                    _commit_turn(s2, turn, use_rp, resume=bool(resume_from))
                    context = context_chars(s2)
                    count = len(s2.get("messages") or [])
                    characters = ensure_characters(s2)
                else:
                    context, count, characters = 0, 0, []

            yield sse({
                "type": "done",
                "title": (s2 or {}).get("title", ""),
                "context_chars": context,
                "context": context_info(s2),
                "message_count": count,
                "attempts": attempts_used,
                "turn": turn,
                "status": (s2 or {}).get("status") or {},
                "characters": characters,
                "pending_character": (s2 or {}).get("pending_character"),
            })
        except GeneratorExit:
            # 客户端 abort：Flask 会 close 这个 generator。标记之后必须 re-raise，
            # 否则 Python 会报 "generator ignored GeneratorExit"。
            cancelled["flag"] = True
            raise
        finally:
            release_busy(sid)

        if cancelled["flag"]:
            metrics.end(sid, ok=False, cancelled=True)
            return
        metrics.end(sid, ok=True, attempts=attempts_used)
        background_compress(sid)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
