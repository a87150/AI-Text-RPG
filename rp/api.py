# -*- coding: utf-8 -*-
"""HTTP 路由。"""
import json
import os
import threading
import time
import uuid

import requests
from flask import Response, jsonify, render_template, request, send_file

from .characters import (
    MAX_CHARACTERS, advance_turn, apply_change, ensure_characters,
    merge_facts, merge_status, next_speaker, parse_turn, role_labels,
    upsert_character,
)
from . import backup
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
    tier, tier_context, tier_limits, writing,
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
    update_from, promote_characters,
)
from .snapshots import list_snapshots, load_snapshot, snapshot
from .storage import (
    DEFAULT_TITLE, TEXT_FIELDS, get_session_lock, list_sessions,
    load_session, new_session, release_busy, save_session, session_path,
    try_acquire_busy,
)

load_settings()

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
        "model": model,
        "mode": mode_of(),
        "writing": conf,
        "max_context_chars": conf["context_limit"],
        "keep_recent": conf["keep_recent"],
        "max_chat_attempts": MAX_CHAT_ATTEMPTS,
        "max_enhance_attempts": MAX_ENHANCE_ATTEMPTS,
        "reasoning_effort": reasoning_effort(),
        "reasoning_levels": REASONING_LEVELS,
        "tier": tier(),
        "tiers": TIERS,
        "tier_labels": TIER_LABELS,
        "tier_context": tier_context(),
        "tier_limits": tier_limits(),
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
    text = (sess.get("opening") or "").strip()
    if sess.get("messages") or not text:
        return
    chars = ensure_characters(sess)
    use_rp = mode_for(sess) == "rp"
    turn = parse_turn(text, chars[0]["name"] if use_rp else "")
    if not (turn["line"] or turn["narrator"]):
        return
    if use_rp:
        msg = {"role": "assistant", "content": turn["line"], "speaker": turn["speaker"]}
        if turn["narrator"]:
            msg["narrator"] = turn["narrator"]
    else:
        msg = {"role": "assistant", "content": "", "narrator": turn["narrator"]}
    sess["messages"] = [msg]


def _bind_story(sess, story):
    """把会话的设定同步进故事模板，保证「设定」面板编辑的就是故事本身。"""
    if not story:
        story = new_story(sess.get("title") or DEFAULT_TITLE)
        sess["story_id"] = story["id"]
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
    chars = ensure_characters(sess)
    sess["ai_name"] = chars[0]["name"]
    sess["ai_role"] = chars[0]["role"]
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
    story = new_story((data.get("title") or "").strip() or DEFAULT_TITLE)
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


@app.put("/api/sessions/<sid>")
def api_update_session(sid):
    data = request.get_json(silent=True) or {}
    with get_session_lock(sid):
        sess = load_session(sid)
        if not sess:
            return jsonify({"error": "会话不存在"}), 404
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
            # 角色表是唯一来源：第一个角色就是「AI 角色」，兼容旧字段
            sess["ai_name"] = chars[0]["name"]
            sess["ai_role"] = chars[0]["role"]
            if sess.get("title") in ("", DEFAULT_TITLE, "新对话"):
                sess["title"] = "与%s的故事" % chars[0]["name"]
        elif len(chars) == 1:
            # 只有一个角色时，设定面板里的名字/人设就是它的
            name = (sess.get("ai_name") or "").strip()
            if name:
                chars[0]["name"] = name[:24]
            if (sess.get("ai_role") or "").strip():
                chars[0]["role"] = sess["ai_role"].strip()[:400]
        seed_opening(sess)
        _bind_story(sess, load_story(sess.get("story_id") or ""))
        save_session(sess)
        sess["context_chars"] = context_chars(sess)
        sess["context"] = context_info(sess)
        return jsonify(sess)


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
        sess["messages"] = msgs[:index]
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
    cloned["messages"] = msgs[:index + 1]

    # 深拷贝会把"此刻"的状态和账本一起带过去，分叉点之前没发生过的事就成了剧透。
    # 轮次退回到分叉点，账本按轮次过滤；老数据没有 turn 字段就保持原样。
    mark = None
    for m in reversed(cloned["messages"]):
        if isinstance(m.get("turn"), int):
            mark = m["turn"]
            break
    if mark is not None:
        cloned["turn"] = mark
        # facts 的 turn 记在本轮开始前，比消息的 turn 小 1，所以用严格小于
        cloned["facts"] = [
            f for f in (src.get("facts") or [])
            if isinstance(f, dict) and int(f.get("turn") or 0) < mark
        ]
    cloned["created_at"] = now
    cloned["updated_at"] = now
    save_session(cloned)
    cloned["context_chars"] = context_chars(cloned)
    cloned["context"] = context_info(cloned)
    return jsonify(cloned)


@app.put("/api/sessions/<sid>/message")
def api_edit_message(sid):
    """就地改一条消息的文本。"""
    data = request.get_json(silent=True) or {}
    text = (data.get("content") or "").strip()
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
        msg["content"] = text
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
    field = (data.get("field") or "").strip()
    name = (data.get("name") or "").strip()

    user_role = (data.get("user_role") or "").strip()
    ai_role = (data.get("ai_role") or "").strip()
    world = (data.get("world") or "").strip()
    rules = (data.get("rules") or "").strip()
    user_name = (data.get("user_name") or "").strip()
    ai_name = (data.get("ai_name") or "").strip()
    characters = data.get("characters")
    if not isinstance(characters, list):
        characters = []
    opening = (data.get("opening") or "").strip()
    lore = data.get("lore")
    if not isinstance(lore, list):
        lore = []
    mode = (data.get("mode") or "").strip()

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
    min_len = 1 if is_name else 40
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

        yield sse({
            "type": "result",
            "field": field,
            "name": name,
            "attempts": parsed.get("attempts", 1),
            "data": {"field": field, "name": name, "text": text,
                     "attempts": parsed.get("attempts", 1)},
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
    first_speaker = next_speaker(sess)
    ask = ("请写出这场角色扮演的开场：先来一小段旁白，再让【%s】登场说第一段话。"
           % first_speaker) if use_rp else (
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

            turn = parse_turn(text, first_speaker)
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
        if ev["type"] == "final":
            return ev["text"], ev["attempts"]
        if ev["type"] == "error":
            yield sse({"type": "error", "message": ev["message"] + "。请稍后手动重试。"})
            return None, 0
        yield sse(ev)
    yield sse({"type": "error", "message": "生成失败，请稍后手动重试。"})
    return None, 0


def _commit_turn(s2, turn, use_rp):
    """一轮生成之后要落盘的所有副作用：消息、状态、账本、角色、轮次、存档。"""
    if use_rp:
        msg = {"role": "assistant", "content": turn["line"], "speaker": turn["speaker"]}
        if turn["narrator"]:
            msg["narrator"] = turn["narrator"]
    else:
        # 系统叙述：正文放 narrator，前端渲染成旁白块
        msg = {"role": "assistant", "content": "", "narrator": turn["narrator"]}
    s2.setdefault("messages", []).append(msg)
    if turn.get("status"):
        s2["status"] = merge_status(s2.get("status"), turn["status"])
    merge_facts(s2, turn.get("facts"), s2.get("turn"))
    if turn["new_character"]:
        upsert_character(s2, turn["new_character"])
        promote_characters(s2, writing()["max_characters"])
    for upd in turn["updates"]:
        apply_change(s2, upd)
    advance_turn(s2)                  # 一般模式也要数轮次，锚定和存档都靠它
    turn_no = int(s2.get("turn") or 0)
    msg["turn"] = turn_no             # 分叉要靠它把账本和轮次退回去
    if turn_no > 0 and turn_no % 15 == 0:
        snapshot(s2, "turn")
    save_session(s2)
    return msg


@app.post("/api/chat")
def api_chat():
    data = request.get_json(silent=True) or {}
    sid = data.get("session_id")
    text = (data.get("message") or "").strip()
    is_retry = bool(data.get("retry"))

    if not text:
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
            last_is_same_user = (
                bool(msgs)
                and msgs[-1].get("role") == "user"
                and msgs[-1].get("content") == text
            )
            if not (is_retry and last_is_same_user):
                msgs.append({"role": "user", "content": text})
            if sess.get("title") in ("", DEFAULT_TITLE, "新对话"):
                sess["title"] = text[:16]
            speaker = next_speaker(sess)      # 发言权由代码分配，模型只能写这一位
            save_session(sess)
            messages = build_messages(sess, speaker)
    except LookupError as exc:
        release_busy(sid)
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        release_busy(sid)
        app.logger.exception("追加用户消息失败：%s", exc)
        return jsonify({"error": "保存消息失败：%s" % exc}), 500

    # 前端不再传温度，改成创意档位；显式传的 temperature 仍然优先（兼容旧页面）
    try:
        temperature = float(data.get("temperature", creativity_temp()))
    except (TypeError, ValueError):
        temperature = creativity_temp()
    temperature = max(0.0, min(1.5, temperature))

    def generate():
        use_rp = sess_mode == "rp"
        try:
            # 先告诉前端这轮由谁发言，流式渲染才能丢掉别人抢话的段落
            yield sse({"type": "speaker", "name": speaker})
            answer, attempts_used = yield from _stream_model(
                messages, speaker, temperature, tier_limits()["attempts"], use_rp)
            if answer is None:
                return

            turn = parse_turn(answer, speaker)
            if use_rp and not turn["line"]:
                yield sse({"type": "error",
                           "message": "模型没有给出【%s】的台词，请重试。" % speaker})
                return
            if not use_rp and not turn["narrator"]:
                yield sse({"type": "error", "message": "模型没有给出内容，请重试。"})
                return

            with get_session_lock(sid):
                s2 = load_session(sid)
                if s2 is not None:
                    _commit_turn(s2, turn, use_rp)
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
            })
        finally:
            release_busy(sid)

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
