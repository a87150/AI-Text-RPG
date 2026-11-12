# -*- coding: utf-8 -*-
"""离线自检：不访问 Ollama / 云端，只验证存储、统计与配置的核心不变量。

用法：python check.py
"""
import json
import os
import re
import pathlib
import shutil
import tempfile
import urllib.parse
import zipfile

TMP_DIR = tempfile.mkdtemp(prefix="lt-check-")
os.environ["LT_DATA_DIR"] = TMP_DIR
# 备份也别落在项目里：以前每次跑检查都往真实 backups/ 塞一对包
os.environ["LT_BACKUP_DIR"] = os.path.join(TMP_DIR, "backups")

import lt  # noqa: E402  必须在设置数据目录之后导入
import threading  # noqa: E402
import rp.api as rp_api  # noqa: E402
import rp.characters as rp_characters  # noqa: E402
import rp.config as rp_config  # noqa: E402
import rp.context as rp_context  # noqa: E402
import rp.llm as rp_llm  # noqa: E402
import rp.metrics as rp_metrics  # noqa: E402
import rp.prompts as rp_prompts  # noqa: E402
import rp.storage as rp_storage  # noqa: E402
import rp.stories as rp_stories
import rp.covers as rp_covers  # noqa: E402
import rp.snapshots as rp_snapshots  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" :: " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


MODE = {"kind": "error"}
TURN_TEXT = (
    "【旁白】雨还在下，走廊的灯闪了一下。\n"
    "【甲】他把伞收起来，靠在门边。\n"
    "【庚】这是抢话，必须被丢掉。\n"
    '【新角色】{"name":"庚","role":"房东，耳朵很尖","relation":"住在楼下"}\n'
    '【变化】{"name":"甲","change":"开始怀疑有人跟踪"}'
)


TURN_LORE = TURN_TEXT + chr(10) +     '【新词条】{"keys":"旧钟楼","content":"城里的钟楼。"}'


def fake_completion_events(messages, base_temp=0.8, max_attempts=3, min_chars=0,
                           extra=None, validate=None, with_reasoning=False, **kw):
    if MODE["kind"] == "final":
        text = TURN_LORE if MODE.get("lore") else TURN_TEXT
        yield {"type": "final", "text": text, "attempts": 1}
        return
    yield {"type": "error", "attempts": 1, "message": "离线自检不调用模型", "raw": ""}


def fake_model_text(messages, temperature=0.8, **kw):
    """状态补全走的是 model_text 这条路，跟主生成不是同一个函数。

    不 patch 它，自检里每次落一回轮次都会真打一次外网。
    """
    return '{"changes":[]}'


def main():
    # 路由模块用的是自己命名空间里的名字，patch 在这一层才生效
    rp_api.completion_events = fake_completion_events
    lt.completion_events = fake_completion_events
    rp_characters.model_text = fake_model_text
    client = lt.app.test_client()
    # 写接口只收 application/json（防跨站表单）。测试里嵌一个默认头，
    # 省得三四十处 client.delete(...) / client.post(url) 逐个补 json。
    client.environ_base = {"CONTENT_TYPE": "application/json"}

    # 0. 模式与可配参数
    check("默认是一般模式", rp_config.mode_of() == "normal", rp_config.mode_of())
    check("默认角色上限是 4 个",
          rp_config.writing()["max_characters"] == 4, rp_config.writing())
    rp_config.save_writing({"max_characters": 99, "line_min": 5, "keep_recent": 999},
                           persist=False)
    clamped = rp_config.writing()
    check("写作参数越界被夹紧",
          clamped["max_characters"] == 10 and clamped["line_min"] == 40
          and clamped["keep_recent"] == 60, clamped)
    rp_config.save_writing({"max_characters": 4, "line_min": 120, "keep_recent": 16},
                           persist=False)

    # 后续用例都走角色扮演路径
    rp_config.save_mode("rp", persist=False)

    # 1. 会话创建与上下文统计
    created = client.post("/api/sessions", json={}).get_json()
    sid = created["id"]
    check("创建会话返回完整上下文统计",
          created["context"]["limit"] == lt.MAX_CONTEXT_CHARS
          and created["context"]["keep_recent"] == lt.KEEP_RECENT_MESSAGES,
          created.get("context"))

    client.put("/api/sessions/" + sid, json={
        "ai_name": "甲", "ai_role": "沉默的警察",
        "world": "雨夜小城", "opening": "雨水敲打着窗。",
    })
    sess = client.get("/api/sessions/" + sid).get_json()
    check("开场白落成第一条助手消息",
          [m["role"] for m in sess["messages"]] == ["assistant"],
          sess["messages"])
    check("上下文用量与实际内容一致",
          sess["context"]["used"] == lt.context_chars(sess),
          (sess["context"]["used"], lt.context_chars(sess)))

    # 2. 回归：消息列表为空时，首条用户消息必须落盘
    empty = client.post("/api/sessions", json={}).get_json()
    empty_sid = empty["id"]
    check("新会话消息为空", empty["messages"] == [], empty["messages"])
    resp = client.post("/api/chat", json={"session_id": empty_sid, "message": "第一句"})
    body = resp.get_data(as_text=True)
    check("模型失败时仍返回 error 事件", '"type": "error"' in body, body[:200])
    after = client.get("/api/sessions/" + empty_sid).get_json()
    check("首条用户消息不丢失",
          [m["content"] for m in after["messages"]] == ["第一句"],
          after["messages"])
    check("失败后保留重试入口所需的消息", after["messages"][-1]["role"] == "user")

    # 3. 重试同一句不重复插入
    client.post("/api/chat", json={"session_id": empty_sid, "message": "第一句", "retry": True})
    after2 = client.get("/api/sessions/" + empty_sid).get_json()
    check("重试不重复插入用户消息",
          [m["content"] for m in after2["messages"]].count("第一句") == 1,
          after2["messages"])

    # 4. 模型配置：校验与密钥不外泄
    listed = [p["id"] for p in client.get("/api/models").get_json()["providers"]]
    check("未配置的云端不出现在可选模型里",
          listed == ["ollama"] or "deepseek" not in listed, listed)
    blocked = client.post("/api/model", json={"provider": "deepseek", "model": "deepseek-chat"})
    check("切到未配置的云端被拒绝", blocked.status_code == 400, blocked.get_json())

    ok = client.post("/api/model", json={"provider": "ollama", "model": "qwen3.5:9b"})
    check("切换本地模型成功", ok.status_code == 200, ok.get_json())
    bad = client.post("/api/model", json={"provider": "nope", "model": "x"})
    check("未知提供方被拒绝", bad.status_code == 400, bad.get_json())

    saved = client.post("/api/providers/deepseek", json={
        "base_url": "https://api.deepseek.com/v1",
        "models": "deepseek-chat,deepseek-reasoner",
        "api_key": "sk-offline-test",
    }).get_json()
    check("云端提供方保存后标记已配置", saved["has_key"] is True, saved)
    check("接口不回显 API Key", "sk-offline-test" not in json.dumps(saved), saved)

    providers = client.get("/api/providers").get_json()
    deepseek = [p for p in providers if p["id"] == "deepseek"][0]
    check("提供方列表不泄露 Key", "sk-offline-test" not in json.dumps(providers))
    check("云端模型名已保存", "deepseek-chat" in deepseek["models"], deepseek)

    listed2 = [p["id"] for p in client.get("/api/models").get_json()["providers"]]
    check("配置后云端进入可选列表", "deepseek" in listed2, listed2)

    client.post("/api/providers/deepseek", json={"clear_key": True})
    listed3 = [p["id"] for p in client.get("/api/models").get_json()["providers"]]
    check("清除 Key 后云端退出可选列表", "deepseek" not in listed3, listed3)

    # 5. 清空会话重置压缩统计
    client.post("/api/sessions/" + sid + "/clear")
    cleared = client.get("/api/sessions/" + sid).get_json()
    check("清空后消息与前情提要归零",
          cleared["messages"] == [] and cleared["summary"] == "",
          cleared["messages"])

    # 6. 旁白 / 单角色发言 / 轮换 / 新角色 / 设定演进
    parsed = lt.parse_turn(TURN_TEXT, "甲")
    check("旁白与台词分开", parsed["narrator"].startswith("雨还在下")
          and parsed["line"].startswith("他把伞"), parsed)
    check("越权发言被丢弃", "抢话" not in parsed["line"], parsed["line"])
    check("新角色与变化被解析",
          parsed["new_character"]["name"] == "庚"
          and parsed["updates"][0]["name"] == "甲", parsed)
    check("无标记输出整段归给发言人",
          lt.parse_turn("他点了点头。", "甲")["line"] == "他点了点头。")

    story = client.post("/api/sessions", json={}).get_json()
    story_sid = story["id"]
    client.put("/api/sessions/" + story_sid, json={"ai_name": "甲", "ai_role": "沉默的警察"})
    MODE["kind"] = "final"

    client.post("/api/chat", json={"session_id": story_sid, "message": "我回来了"}).get_data(as_text=True)
    after_turn = client.get("/api/sessions/" + story_sid).get_json()
    last = after_turn["messages"][-1]
    check("落盘的助手消息带说话人", last["speaker"] == "甲", last)
    check("落盘的助手消息带旁白", last.get("narrator", "").startswith("雨还在下"), last)
    check("落盘内容不含抢话段落", "抢话" not in last["content"], last["content"])
    names = [c["name"] for c in after_turn["characters"]]
    check("新角色已加入角色表", "庚" in names, names)
    shen = [c for c in after_turn["characters"] if c["name"] == "甲"][0]
    check("角色设定随进展更新", shen["state"] == "开始怀疑有人跟踪", shen)
    check("回合数推进", after_turn["turn"] == 1, after_turn["turn"])

    # 用户对角色表有最终决定权
    edited = client.put("/api/sessions/" + story_sid, json={
        "characters": [
            {"name": "甲", "role": "刑警"},
            {"name": "庚", "role": "房东"},
            {"name": "阿岚", "role": "线人"},
        ]
    }).get_json()
    check("用户可以新增角色", [c["name"] for c in edited["characters"]]
          == ["甲", "庚", "阿岚"], edited["characters"])

    removed = client.put("/api/sessions/" + story_sid, json={
        "characters": [{"name": "甲", "role": "刑警"}]
    }).get_json()
    check("用户可以删除角色",
          [c["name"] for c in removed["characters"]] == ["甲"], removed["characters"])

    kept = client.put("/api/sessions/" + story_sid, json={
        "characters": [{"name": "甲", "role": "刑警，旧城区长大"}]
    }).get_json()
    check("用户改的人设被保留",
          kept["characters"][0]["role"] == "刑警，旧城区长大", kept["characters"])

    # AI 引入同名角色时，不能覆盖用户写的人设
    lt.upsert_character(kept, {"name": "甲", "role": "其实是鬼", "change": "伤了一条腿"})
    check("AI 不会改写用户的人设",
          kept["characters"][0]["role"] == "刑警，旧城区长大", kept["characters"][0])
    check("AI 只推进状态",
          kept["characters"][0]["state"] == "伤了一条腿", kept["characters"][0])

    # 第二回合必须换人说话
    # 圆桌式后不再由代码指定单一发言人
    check("不再按轮次指定发言人", lt.next_speaker(after_turn) == "",
          lt.next_speaker(after_turn))
    check("角色数不超过上限", len(after_turn["characters"]) <= rp_config.writing()["max_characters"])

    # 8. 模式切换：一般模式由系统叙述，不分配发言人
    rp_config.save_mode("normal", persist=False)
    plain = client.post("/api/sessions", json={}).get_json()
    plain_sid = plain["id"]
    client.put("/api/sessions/" + plain_sid, json={
        "ai_name": "甲", "ai_role": "沉默的警察", "world": "雨夜小城",
        "characters": [{"name": "甲", "role": "沉默的警察"},
                       {"name": "庚", "role": "房东"}],
    })
    plain_sess = client.get("/api/sessions/" + plain_sid).get_json()
    check("一般模式没有发言人",
          rp_characters.next_speaker(plain_sess) == "",
          rp_characters.next_speaker(plain_sess))
    turn = rp_characters.parse_turn("雨还在下。\n【甲】他把伞收起来。", "")
    check("一般模式整段算叙述",
          turn["narrator"].startswith("雨还在下") and not turn["lines"], turn)

    MODE["kind"] = "final"
    plain_resp = client.post("/api/chat", json={"session_id": plain_sid, "message": "我推门进去。"})
    plain_body = plain_resp.get_data(as_text=True)   # 流式响应必须读完才会真正生成
    psess = client.get("/api/sessions/" + plain_sid).get_json()
    last_plain = psess["messages"][-1]
    check("一般模式落盘为系统消息",
          last_plain["role"] == "assistant" and not last_plain.get("speaker")
          and bool(last_plain.get("narrator")),
          (last_plain, plain_body[:160]))

    rules_plain = rp_prompts.build_rules("我", "normal", "甲、庚")
    check("一般模式提示词是叙述者口吻",
          "叙述者" in rules_plain and "你这一轮演的就是" not in rules_plain,
          rules_plain[:80])
    rules_rp = rp_prompts.build_rules("我", "rp", "甲")
    check("角色扮演提示词要求在场角色各自发言",
          "各写一段" in rules_rp and "旁白" in rules_rp, rules_rp[:90])
    rp_config.save_mode("rp", persist=False)

    # 9. 模式固定在会话上；故事能复用开多局
    rp_config.save_mode("normal", persist=False)
    fresh = client.post("/api/sessions", json={}).get_json()
    check("新对话带自己的模式", fresh["mode"] == "normal", fresh.get("mode"))
    check("新对话自动绑定一个故事", bool(fresh.get("story_id")), fresh.get("story_id"))

    saved = client.put("/api/sessions/" + fresh["id"], json={
        "title": "雨夜旧城", "user_name": "我", "user_role": "记者",
        "world": "雨夜", "opening": "雨落下来。",
        "characters": [{"name": "甲", "role": "守灯人"},
                       {"name": "庚", "role": "房东"}],
        "mode": "rp",
    }).get_json()
    check("模式改为扮演后固定在这个对话",
          saved["mode"] == "rp", saved.get("mode"))
    check("第一个角色就是 AI 角色",
          saved["ai_name"] == "甲" and saved["ai_role"] == "守灯人",
          (saved.get("ai_name"), saved.get("ai_role")))

    # 模板是独立一层：想让新局带上设定，得显式改模板，改对话不会写回去
    client.put("/api/stories/" + saved["story_id"], json={
        "title": saved["title"], "user_name": saved["user_name"],
        "user_role": saved["user_role"], "world": saved["world"],
        "rules": saved["rules"], "opening": saved["opening"],
        "characters": saved["characters"],
    })
    second = client.post("/api/sessions", json={"story_id": saved["story_id"]}).get_json()
    check("同一故事开新局沿用设定",
          [c["name"] for c in second["characters"]] == ["甲", "庚"]
          and second["world"] == "雨夜" and second["user_name"] == "我",
          second.get("characters"))
    check("新局只带开场，不带旧对话",
          [m["role"] for m in second["messages"]] == ["assistant"],
          second.get("messages"))
    check("新局用自己的模式", second["mode"] == "normal", second.get("mode"))
    stories = client.get("/api/stories").get_json()
    mine = [s for s in stories if s["id"] == saved["story_id"]]
    check("故事列表记录开过几局", bool(mine) and mine[0]["runs"] == 2, stories)

    # 10. 状态面板：剥离、解析、注入
    status_text = ('【旁白】雨还在下。\n【甲】他收起伞。\n'
                   '<StatusBlock>{"location":"酒馆","time":"深夜",'
                   '"relations":"互相试探","items":["伞","旧钥匙"],'
                   '"goal":"问清来意"}</StatusBlock>')
    st_turn = rp_characters.parse_turn(status_text, "甲")
    check("状态块从正文里剥离",
          "StatusBlock" not in st_turn["line"]
          and "StatusBlock" not in st_turn["narrator"], st_turn)
    check("状态块解析成结构化数据",
          st_turn["status"]["location"] == "酒馆"
          and st_turn["status"]["items"] == "伞，旧钥匙"
          and st_turn["status"]["goal"] == "问清来意", st_turn["status"])

    with_status = dict(saved)
    with_status["status"] = st_turn["status"]
    prompt = rp_prompts.build_system_prompt(with_status, "甲")
    check("上一轮状态注入下一轮提示词",
          "【此刻的处境】" in prompt and "问清来意" in prompt, prompt[-160:])

    # 11. 消息控制权：重写、就地编辑、分叉
    edit_sess = client.post("/api/sessions", json={}).get_json()
    esid = edit_sess["id"]
    raw = rp_storage.load_session(esid)
    raw["messages"] = [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "第一答", "speaker": "甲"},
        {"role": "user", "content": "第二句"},
        {"role": "assistant", "content": "第二答", "speaker": "甲"},
    ]
    rp_storage.save_session(raw)

    rewound = client.post("/api/sessions/" + esid + "/rewind",
                          json={"index": 3}).get_json()
    check("重写会砍掉这条及之后", 
          [m["content"] for m in rewound["messages"]] == ["第一句", "第一答", "第二句"],
          [m["content"] for m in rewound["messages"]])

    edited = client.put("/api/sessions/" + esid + "/message",
                        json={"index": 0, "content": "改过的第一句"}).get_json()
    check("消息可以就地编辑",
          edited["messages"][0]["content"] == "改过的第一句", edited["messages"][0])

    forked = client.post("/api/sessions/" + esid + "/branch", json={"index": 1}).get_json()
    check("分叉出独立的新对话",
          forked["id"] != esid and len(forked["messages"]) == 2
          and forked["messages"][1]["content"] == "第一答",
          [m["content"] for m in forked["messages"]])
    check("分叉带上设定且不动原对话",
          bool(forked.get("story_id"))
          and len(client.get("/api/sessions/" + esid).get_json()["messages"]) == 3,
          forked.get("story_id"))

    # 12. 世界书：只注入命中关键词的条目
    lore_sess = dict(saved)
    lore_sess["lore"] = [
        {"keys": "某酒馆,酒馆", "content": "某酒馆的老板只收旧币。"},
        {"keys": "遗迹", "content": "遗迹在城北，夜里会浮起来。"},
    ]
    lore_sess["messages"] = [{"role": "user", "content": "我推开酒馆的门"}]
    hit_msgs = rp_prompts.build_messages(lore_sess, "甲")
    check("提到关键词才注入世界书",
          "某酒馆的老板只收旧币" in hit_msgs[0]["content"]
          and "遗迹在城北" not in hit_msgs[0]["content"],
          hit_msgs[0]["content"][:80])
    lore_sess["messages"] = [{"role": "user", "content": "外面还在下雨"}]
    calm_msgs = rp_prompts.build_messages(lore_sess, "甲")
    check("没提到就不塞世界书",
          all("某酒馆的老板只收旧币" not in (m.get("content") or "") for m in calm_msgs),
          len(calm_msgs))

    saved_lore = client.put("/api/sessions/" + fresh["id"], json={
        "lore": [{"keys": "酒馆", "content": "老板只收旧币。"}],
    }).get_json()
    check("世界书会随设定保存", saved_lore["lore"][0]["content"] == "老板只收旧币。",
          saved_lore.get("lore"))
    story_after = client.get("/api/sessions/" + fresh["id"]).get_json()["story"]
    # 以前这里会把对话的世界书也写进故事模板；现在故事只能在故事视图里改
    check("世界书不再同步进故事模板",
          not (story_after or {}).get("lore"),
          (story_after or {}).get("lore"))

    # 13. 设定扩写：一次一个字段，贴住用户原话、不丢其它角色
    solo = rp_prompts.build_field_prompt(
        "user_role", user_role="我是实习生，想和上司搞暧昧，日常向", rules="别搞悬疑")
    check("单字段提示词只要一个部分",
          "这次只写一个部分" in solo and '{"' not in solo)
    check("用户原话整句进提示词",
          "我是实习生，想和上司搞暧昧，日常向" in solo and "别搞悬疑" in solo)
    check("没写 AI 角色时让模型自己推断",
          "AI 角色还没定" in solo, solo[-160:])

    named = rp_prompts.build_field_prompt(
        "character", name="甲", user_role="夜班护士",
        characters=[{"name": "甲", "role": "跑网约车的"},
                    {"name": "庚", "role": "房东"}])
    check("角色卡要求照着名字写并给出字数",
          "甲" in named and "只写一个部分" in named, named[:80])
    check("其它角色一并列出", "庚" in named and "必须原样保留" in named)

    name_prompt = rp_prompts.build_field_prompt("ai_name", user_role="夜班护士")
    check("起名任务只要名字", "只输出名字" in name_prompt)

    world_prompt = rp_prompts.build_field_prompt("world", world="雨夜旧城")
    check("世界观要求保留已有事实但重新组织",
          "雨夜旧城" in world_prompt and "不要先照抄一遍再补一段" in world_prompt)

    # 简述是底稿：生成结果要吸收它，而不是把原文再贴一遍
    brief = "我是赛博朋克城市的夜班护士，值完班天刚亮，累得不想说话"
    absorbed = ("赛博朋克城市的凌晨，你刚结束十二小时的夜班，护士服袖口沾着消毒水味。"
                "你二十七岁，说话简短，不主动开口。")
    merged = rp_prompts.ensure_original_kept(brief, absorbed)
    check("改写吸收了简述就整段替换",
          merged == absorbed and merged.count("夜班护士") <= 1, merged[:40])
    check("生成本身已含简述时不会贴两遍",
          rp_prompts.ensure_original_kept(brief, brief + "\n\n你不是本地人。").count("夜班护士") == 1)
    weak = rp_prompts.ensure_original_kept(brief, "一个年轻女人，在城市的凌晨下班。")
    check("模型漏了简述才补回原文", brief in weak and weak.count("夜班护士") == 1)

    # 14. 一般模式同样用得上整份角色表（只是由系统发言）
    normal_sess = dict(saved)
    normal_sess["mode"] = "normal"
    normal_sess["characters"] = [
        {"name": "甲", "role": "守灯人，话少"},
        {"name": "庚", "role": "房东，爱管闲事"},
    ]
    normal_prompt = rp_prompts.build_system_prompt(normal_sess, "")
    check("一般模式也拿到完整角色卡",
          "守灯人，话少" in normal_prompt and "房东，爱管闲事" in normal_prompt)
    check("一般模式不指定发言人", "你这一轮演的就是" not in normal_prompt)
    check("一般模式要求按人设写所有角色",
          "角色按各自的人设" in normal_prompt and "不必轮流" in normal_prompt)

    rp_sess = dict(normal_sess, mode="rp")
    rp_prompt = rp_prompts.build_system_prompt(rp_sess, "庚")
    check("两种模式读的是同一份角色表",
          "房东，爱管闲事" in rp_prompt and "你这一轮演的就是：庚" in rp_prompt)
    check("RP 模式先钉死身份",
          rp_prompt.startswith("你这一轮就是【庚】本人"), rp_prompt[:30])

    # 15. 思考强度（Ollama 的 reasoning_effort）
    check("/api/config 下发是否支持思考强度",
          "reasoning_supported" in client.get("/api/config").get_json())
    check("默认思考强度是中等", rp_config.reasoning_effort() == "medium",
          rp_config.reasoning_effort())
    rp_config.save_reasoning("high", persist=False)
    check("可以改成其它档位", rp_config.reasoning_effort() == "high")
    rp_config.save_reasoning("bogus", persist=False)
    check("非法档位被忽略", rp_config.reasoning_effort() == "high")
    rp_config.save_reasoning("medium", persist=False)
    check("配置接口带出思考强度",
          client.get("/api/config").get_json()["reasoning_effort"] == "medium")
    saved_conf = client.post("/api/writing", json={"reasoning_effort": "low"}).get_json()
    check("设置窗能改思考强度", saved_conf["reasoning_effort"] == "low",
          saved_conf.get("reasoning_effort"))
    rp_config.save_reasoning("medium", persist=False)

    # 16. 补全只输出正文：去前言、去标题、去重复、整段覆盖简述
    dirty = ("好的，以下是为你生成的角色设定：\n\n**角色卡：**\n\n"
             "我，27 岁，赛博朋克城市的夜班护士，习惯把疲惫藏在利落的手脚里。\n\n"
             "我今年 27 岁，是赛博朋克城市的夜班护士，习惯把疲惫藏在利落的手脚里。\n\n"
             "说话简短，不主动开口。")
    cleaned = rp_prompts.clean_generated(dirty)
    check("补全不带开场客套", not cleaned.startswith("好的"), cleaned[:16])
    check("补全不带小标题与 Markdown",
          "角色卡" not in cleaned and "**" not in cleaned, cleaned[:40])
    check("补全不留重复段落", cleaned.count("夜班护士") == 1, cleaned)
    check("补全保留正文内容", "说话简短" in cleaned)

    brief = "我是赛博朋克城市的夜班护士"
    check("简述已被吸收时整段覆盖",
          rp_prompts.ensure_original_kept(brief, cleaned) == cleaned)
    gap = rp_prompts.ensure_original_kept(brief, "一个住在城南的年轻女人。")
    check("完全没吸收才补回原文", brief in gap)

    # 17. 故事能单独建、单独改，不牵动任何一局对话
    story = client.post("/api/stories", json={}).get_json()
    check("能单独建一份故事", bool(story.get("id")), str(story)[:40])
    saved = client.put("/api/stories/" + story["id"], json={
        "title": "雨夜旧城", "user_name": "我", "world": "旧城雨季。",
        "characters": [{"name": "甲", "role": "守灯人"}],
    }).get_json()
    check("故事设定能单独保存",
          saved.get("world") == "旧城雨季。" and saved["characters"][0]["name"] == "甲",
          str(saved)[:60])
    check("故事自己派生 AI 角色名", saved.get("ai_name") == "甲", saved.get("ai_name"))
    check("故事改动已落盘",
          client.get("/api/stories/" + story["id"]).get_json().get("user_name") == "我")
    check("删除故事返回成功",
          client.delete("/api/stories/" + story["id"]).get_json().get("ok") is True)

    # 18. 对话和故事的设定互相独立：建局时快照一份，之后各走各的
    story2 = client.post("/api/stories", json={}).get_json()
    sess2 = client.post("/api/sessions", json={"story_id": story2["id"]}).get_json()
    client.put("/api/sessions/" + sess2["id"], json={
        "characters": [{"name": "甲", "role": "守灯人", "state": "受伤"}],
    })
    cards_after = client.get("/api/stories/" + story2["id"]).get_json().get("characters") or []
    check("对话里加角色不动故事模板",
          not any((x.get("name") or "") == "甲" for x in cards_after), str(cards_after)[:80])
    check("对话里改设定不动故事世界观",
          (client.get("/api/stories/" + story2["id"]).get_json().get("world") or "") != "对话里的世界")
    client.put("/api/stories/" + story2["id"], json={"world": "故事里的世界"})
    check("改故事不影响已建的对话",
          (rp_storage.load_session(sess2["id"]).get("world") or "") != "故事里的世界")
    check("promote_characters 已移除", not hasattr(rp_stories, "promote_characters"))

    # 19. 人设锚定：聊久了把角色核心重插到近处
    anchor_sess = rp_storage.new_session("锚定测试")
    anchor_sess["characters"] = [{"name": "甲", "role": "守灯人，话少，不主动开口",
                                  "state": "", "history": []}]
    # 间隔由档位决定，这里按当前档位的间隔来验
    every = rp_config.tier_limits()["anchor_every"]
    anchor_sess["turn"] = every
    msgs = rp_prompts.build_messages(anchor_sess)
    check("到锚定间隔会重插人设锚定",
          any("人设锚定" in m["content"] for m in msgs), str(len(msgs)))
    anchor_sess["turn"] = every - 1
    check("没到的轮次不插锚定",
          not any("人设锚定" in m["content"] for m in rp_prompts.build_messages(anchor_sess)))
    anchored_msgs = rp_prompts.build_messages({**anchor_sess, "turn": every})
    check("锚定贴在最近几轮之前",
          [i for i, m in enumerate(anchored_msgs) if "人设锚定" in m["content"]][0] > 0)

    # 20. 上下文检查器：能看见这一轮发了什么
    dbg_sess = rp_storage.new_session("检查器测试")
    dbg_sess["user_role"] = "夜班护士"
    dbg_sess["characters"] = [{"name": "甲", "role": "守灯人", "state": "", "history": []}]
    dbg_sess["lore"] = [{"keys": "某酒馆", "content": "城南的酒馆，老板欠甲一条命。"}]
    dbg_sess["messages"] = [{"role": "user", "content": "去某酒馆看看"}]
    dbg = rp_prompts.build_context_debug(dbg_sess)
    check("检查器给出 system 全文", "【写法】" in dbg["system"])
    check("检查器标出命中的世界书",
          bool(dbg["lore"]) and dbg["lore"][0]["keys"] == "某酒馆", str(dbg["lore"])[:50])
    check("检查器给出 token 估算", dbg["tokens"] > 0)
    # 中文在 Qwen 词表里约 0.6 个 token/字，不是一字一个；
    # 但也不能像旧公式那样把标点当英文算，会低估一成
    _zh = "她站在门口，风从背后吹过来。"
    _t_zh = rp_prompts._tokens(_zh)
    check("中文不按一字一 token 估", _t_zh < len(_zh) * 0.85, str(_t_zh))
    check("中文估算也不至于过低", _t_zh >= len(_zh) * 0.5, str(_t_zh))
    check("中文标点比英文标点贵",
          rp_prompts._tokens("。。。。") > rp_prompts._tokens("...."),
          str(rp_prompts._tokens("。。。。")) + " vs " + str(rp_prompts._tokens("....")))
    check("上下文接口带 debug", "debug" in rp_context.context_info(dbg_sess))

    # 21. 事实账本：旧说法让位，不双写
    book_sess = {"turn": 3}
    rp_characters.merge_facts(book_sess, ["甲拿走了钥匙"], turn=3)
    rp_characters.merge_facts(book_sess, ["钥匙被甲拿走了"], turn=4)
    live = rp_characters.active_facts(book_sess)
    check("账本只留最新一条", len(live) == 1 and live[0]["turn"] == 4, str(live))
    check("旧事实标记为已失效",
          any(f["state"] == "resolved" for f in book_sess["facts"]))
    check("账本写进 system prompt",
          "已经发生" in rp_prompts.build_system_prompt(book_sess))
    parsed = rp_characters.parse_turn(
        '<StatusBlock>{"facts": ["两人约好三天后见"]}</StatusBlock>【甲】嗯。', "甲")
    check("状态块能声明事实", parsed["facts"] == ["两人约好三天后见"], str(parsed["facts"]))
    check("事实不会混进状态字段", "facts" not in (parsed["status"] or {}))

    # 22. 快照：能存能列能回滚
    snap_sess = rp_storage.new_session("快照测试")
    rp_storage.save_session(snap_sess)
    name = rp_snapshots.snapshot(snap_sess, "settings")
    check("快照能写下来", bool(name), str(name))
    items = rp_snapshots.list_snapshots(snap_sess["id"])
    check("快照能列出来", any(i["name"] == name for i in items), str(items)[:60])
    check("快照能读回来",
          (rp_snapshots.load_snapshot(snap_sess["id"], name) or {}).get("id") == snap_sess["id"])
    for _ in range(12):
        rp_snapshots.snapshot(snap_sess, "turn")
    check("只保留最近 10 份",
          len(rp_snapshots.list_snapshots(snap_sess["id"])) <= 10)
    check("非法名字读不到",
          rp_snapshots.load_snapshot(snap_sess["id"], "../settings.json") is None)

    # 23. 轮次与快照节奏：0 轮不该触发存档
    tick = rp_storage.new_session("节拍测试")
    rp_characters.advance_turn(tick)
    check("轮次从 0 开始累加", tick["turn"] == 1, str(tick["turn"]))
    check("第一轮不触发每 15 轮存档", tick["turn"] % 15 != 0)

    # 24. 备份：能打包、抹掉密钥、只认自己的文件名
    import rp.backup as rp_backup
    made = rp_backup.create("manual")
    check("备份能生成", bool(made.get("name")) and made.get("files", 0) > 0, str(made)[:80])
    zpath = rp_backup.path_of(made["name"])
    check("备份文件存在", bool(zpath))
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        check("包里有 settings.json", "data/settings.json" in names)
        packed = json.loads(zf.read("data/settings.json").decode("utf-8"))
        keys = [(v or {}).get("api_key") for v in (packed.get("providers") or {}).values()]
        check("包里的 API Key 被抹掉", all(not k for k in keys), str(keys)[:60])
        check("其他设置还在包里", "writing" in packed)
    check("路径穿越读不到东西", rp_backup.path_of("../settings.json") is None)
    check("备份列表含刚才那份",
          any(x["name"] == made["name"] for x in rp_backup.listing()))
    rp_backup.delete(made["name"])
    check("删除后再查就没了",
          not any(x["name"] == made["name"] for x in rp_backup.listing()))

    # 25. 定时备份配置：开了能存，越界值夹回范围
    saved = rp_config.save_backup_conf({"enabled": True, "every_minutes": 90})
    check("定时备份能开启", saved["enabled"] is True and saved["every_minutes"] == 90, str(saved))
    check("间隔超出范围会被夹住",
          rp_config.save_backup_conf({"every_minutes": 999999})["every_minutes"] <= 20160)
    check("短于 5 分钟会被抬回来",
          rp_config.save_backup_conf({"every_minutes": 1})["every_minutes"] >= 5)
    rp_config.save_backup_conf({"enabled": False, "every_minutes": 1440})
    now = 1_700_000_000
    check("关着就不该跑", rp_backup.due({"enabled": False, "every_minutes": 60}, 0, now) is False)
    check("开着且到期才跑",
          rp_backup.due({"enabled": True, "every_minutes": 120}, now - 3 * 3600, now) is True)
    check("没到期不重复跑",
          rp_backup.due({"enabled": True, "every_minutes": 120}, now - 3600, now) is False)

    # 26. 状态行三种写法都要接住，正文里不许留残渣
    NL = chr(10)
    body, status = rp_characters.extract_status(NL.join([
        "他把门让开。",
        "[[地点：门廊|时间：深夜|关系：试探|物品：湿伞|目标：等他开口|事实：钥匙易主]]",
    ]))
    check("单行状态行解析出全部字段",
          status and status.get("location") == "门廊" and status.get("facts") == ["钥匙易主"],
          str(status))
    check("单行状态行不进正文", "|" not in body and "[[" not in body, repr(body))
    body, status = rp_characters.extract_status(NL.join([
        "他把你让进门里。",
        "--",
        chr(123) + chr(34) + "location" + chr(34) + ":" + chr(34) + "门廊" + chr(34)
        + "," + chr(34) + "time" + chr(34) + ":" + chr(34) + "深夜" + chr(34) + chr(125),
        "新事实：",
    ]))
    check("裸 JSON 也能捞出状态", status and status.get("location") == "门廊", str(status))
    check("裸 JSON 不留在正文", "location" not in body and chr(123) not in body, repr(body))
    check("分隔线与空字段名被清掉", "--" not in body and "新事实" not in body, repr(body))
    body, status = rp_characters.extract_status(
        "<StatusBlock>" + chr(123) + chr(34) + "location" + chr(34) + ":" + chr(34)
        + "柜台" + chr(34) + chr(125) + "</StatusBlock>正文")
    check("旧 XML 写法仍然认", status and status.get("location") == "柜台", str(status))
    check("纯正文不受影响",
          rp_characters.extract_status("就是一段普通正文。") == ("就是一段普通正文。", None))

    # 27. 设定生成要去掉英文解释行
    cleaned = rp_prompts.clean_generated(NL.join([
        "Here is the setting:", "", "夜班护士我。", "She is calm.", "手上有疤。",
    ]))
    check("生成设定里没有英文行", "Here is" not in cleaned and "She is" not in cleaned, repr(cleaned))
    check("中文正文保留下来", "夜班护士我" in cleaned and "手上有疤" in cleaned, repr(cleaned))

    # 28. 状态格式提醒贴在最后一条 user 上，不能只在 system 里
    tail_sess = rp_storage.new_session()
    tail_sess["messages"] = [
        {"role": "user", "content": "我推门进去。", "id": "t1"},
        {"role": "assistant", "content": "他抬头。", "narrator": "", "id": "t2"},
    ]
    built = rp_prompts.build_messages(tail_sess)
    last_user = [m for m in built if m["role"] == "user"][-1]["content"]
    check("提醒写进了最后一条用户消息", "[[地点" in last_user, repr(last_user[-40:]))

    # 29. 模型能力档位：格式随之变化，非法值被忽略
    base_block = None
    for name in rp_config.TIERS:
        rp_config.save_tier(name, persist=False)
        block = rp_prompts.status_prompt()
        if name == "basic":
            base_block = block
            check("初级档给单行标记和多条示例", block.count("[[") >= 3, str(block.count("[[")))
        elif name == "standard":
            check("标准档示例更少", block.count("[[") == 1, str(block.count("[[")))
        else:
            check("高级档要结构化 JSON", "StatusBlock" in block and "location" in block)
    rp_config.save_tier("basic", persist=False)
    check("档位文案各不相同", len({rp_config.TIER_LABELS[t] for t in rp_config.TIERS}) == 3)
    rp_config.save_tier("nonsense", persist=False)
    check("非法档位被忽略", rp_config.tier() == "basic", rp_config.tier())
    check("配置接口带出档位", client.get("/api/config").get_json()["tier"] == "basic")

    # 30. 在场 / 离场：解析、分组、发言权、状态累积
    scene_status = {"present": ["甲"], "away": ["丙"]}
    scene_chars = [{"name": "甲", "role": "守灯人"},
                   {"name": "乙", "role": "跑堂"},
                   {"name": "丙", "role": "账房"}]
    on, off = rp_characters.scene_split(scene_chars, scene_status)
    check("按在场名单分组", [c["name"] for c in on] == ["甲"]
          and "丙" in [c["name"] for c in off], str((on, off)))
    check("没标过就全员在场",
          len(rp_characters.scene_split(scene_chars, {})[0]) == 3)
    scene_sess = {"turn": 3, "mode": "rp", "characters": scene_chars, "status": scene_status}
    check("圆桌式不再指定单一发言人", rp_characters.next_speaker(scene_sess) == "")   # 在场分组见下面两条
    block = rp_prompts.build_character_block(scene_chars, "甲", scene_status)
    check("离场角色写进提示但标注不要出现",
          "丙" in block and "不在这一场" in block, block[-80:])
    merged = rp_characters.merge_status(
        {"location": "酒馆", "present": ["甲"], "away": ["丙"]}, {"goal": "试探"})
    check("状态是累积的，没写到的字段不丢",
          merged.get("location") == "酒馆" and merged.get("goal") == "试探"
          and merged.get("away") == ["丙"], str(merged))
    check("状态行里写在场也能解析",
          (rp_characters.extract_status("正文。\n[[在场：甲,乙|离场：丙|地点：酒馆]]")[1] or {})
          .get("present") == ["甲", "乙"])
    check("JSON 里写在场也能解析",
          (rp_characters.extract_status(
              "正文。<StatusBlock>" + chr(123) + chr(34) + "present" + chr(34) + ":[" + chr(34)
              + "甲" + chr(34) + "]," + chr(34) + "away" + chr(34) + ":[" + chr(34)
              + "丙" + chr(34) + "]" + chr(125) + "</StatusBlock>")[1] or {})
          .get("away") == ["丙"])

    # 31. 只允许改自己的话，改 AI 的台词会被拒
    edit_sess2 = client.post("/api/sessions", json={}).get_json()
    rp_storage.save_session(dict(
        rp_storage.load_session(edit_sess2["id"]),
        messages=[{"role": "user", "content": "我说的"},
                  {"role": "assistant", "content": "它说的", "speaker": "甲"}]))
    ok = client.put("/api/sessions/%s/message" % edit_sess2["id"],
                    json={"index": 0, "text": "改自己的话"})
    check("能改自己的消息", ok.status_code == 200, str(ok.status_code))
    bad = client.put("/api/sessions/%s/message" % edit_sess2["id"],
                     json={"index": 1, "text": "想改 AI 的"})
    check("不能改 AI 的消息", bad.status_code == 400, str(bad.status_code))
    check("AI 的正文没被动过",
          (rp_storage.load_session(edit_sess2["id"])["messages"][1].get("speaker")) == "甲")

    # 32. 档位决定注入策略：历史条数、人设长度、账本条数、锚定间隔
    def _fat_sess():
        return {"turn": 8, "mode": "rp", "user_name": "甲",
                "characters": [{"name": "甲", "role": "守灯人，话少。" * 30}],
                "facts": [{"fact": "事实%d" % i, "state": "active", "turn": i} for i in range(20)],
                "messages": [{"role": "user", "content": "u%d" % i} if i % 2 == 0
                             else {"role": "assistant", "content": "a%d" % i} for i in range(40)]}

    rp_config.save_tier("basic", persist=False)
    msgs = rp_prompts.build_messages(_fat_sess(), "甲")
    hist = [m for m in msgs if m["role"] != "system"]
    check("初级只带最近 16 条历史", len(hist) == 16, str(len(hist)))
    check("历史不从 assistant 开头", msgs[-16]["role"] == "user", msgs[-16]["role"])
    _cut = rp_prompts.clip("守灯人，话少。" * 30, 200)
    check("初级每条人设截到 200 字左右", _cut.__len__() <= 240, str(len(_cut)))
    check("截断断在句号上", _cut.startswith("守灯人") and _cut.rstrip("…")[-1] in "。！？", _cut[-26:])
    check("截断后告诉模型这是节选",
          "节选" in rp_prompts.build_character_block(
              [{"name": "甲", "role": "守灯人，话少。" * 40}], "甲"))
    body = rp_prompts.build_system_prompt(_fat_sess(), "甲")
    check("初级只注入 6 条账本",
          body.count("· 事实") == 6, str(body.count("· 事实")))
    check("初级第 8 轮就重插人设",
          any("人设锚定" in m["content"] for m in msgs))

    rp_config.save_tier("advanced", persist=False)
    msgs2 = rp_prompts.build_messages(_fat_sess(), "甲")
    hist2 = [m for m in msgs2 if m["role"] != "system"]
    check("高级不砍历史", len(hist2) == 40, str(len(hist2)))
    body2 = rp_prompts.build_system_prompt(_fat_sess(), "甲")
    check("高级注入 20 条账本",
          body2.count("· 事实") == 20, str(body2.count("· 事实")))
    check("高级不截人设",
          rp_prompts.clip("守灯人，话少。" * 30, rp_config.tier_limits()["role_chars"]).__len__() > 150)
    check("高级第 8 轮不重插",
          not any("人设锚定" in m["content"] for m in msgs2))
    check("重试次数随档位", rp_config.tier_limits()["attempts"] == 2)
    rp_config.save_tier("basic", persist=False)
    check("配置接口带出完整策略",
          client.get("/api/config").get_json()["tier_limits"]["history_msgs"] == 16)

    # 33. 设置面板是折叠节，备份并进来了，不再有第二个弹窗
    _html = (pathlib.Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    # 默认模式那节已去掉：模式在建局和各自的「设定」里切
    check("设置分成四个折叠节", _html.count('<details class="prefs-sec"') == 4,
          str(_html.count('<details class="prefs-sec"')))
    check("每节都有标题和内容",
          _html.count("<summary>") == 4 and _html.count('class="prefs-body"') == 4)
    check("不再有默认模式选择", 'data-sec="mode"' not in _html and 'id="prefs-mode"' not in _html)
    check("设置里有正文语言选择", 'id="language-seg"' in _html)
    check("语言只给中英两个", _html.count('data-lang=') == 2)
    check("云端控件并在模型节里",
          'data-sec="cloud"' not in _html and 'id="p-provider"' in _html)
    _bkjs = (pathlib.Path(__file__).parent / "static" / "js" / "backup.js").read_text(encoding="utf-8")
    check("每个备份都能还原", "bi-restore" in _bkjs and "/restore" in _bkjs)
    check("备份弹窗已删掉", 'id="backup-modal"' not in _html)
    check("备份控件搬进设置", 'data-sec="backup"' in _html and 'id="btn-backup-now"' in _html)
    check("不再有「打开备份面板」按钮", 'id="btn-open-backup"' not in _html)
    _main = (pathlib.Path(__file__).parent / "static" / "js" / "main.js").read_text(encoding="utf-8")
    check("折叠状态存 localStorage", "lt.prefs" in _main and "PREFS_KEY" in _main)
    check("展开备份节会去取列表", "refreshBackups" in _main)

    # 34. 规则文案也按档位分：初级短且硬，高级才谈写法
    def _rule_body(level):
        rp_config.save_tier(level, persist=False)
        return rp_prompts.build_rules("我", "rp", "甲")

    body_basic = _rule_body("basic").replace(rp_prompts.status_prompt(), "")
    body_std = _rule_body("standard").replace(rp_prompts.status_prompt(), "")
    body_adv = _rule_body("advanced").replace(rp_prompts.status_prompt(), "")
    check("规则正文随档位变长", len(body_basic) < len(body_std) < len(body_adv),
          "%d < %d < %d" % (len(body_basic), len(body_std), len(body_adv)))
    check("初级不谈文风", "文风" not in body_basic and "文风" in body_std and "节奏" in body_adv)
    check("高级才讲留白与感官", "留白" in body_adv and "留白" not in body_basic)
    check("三档都保留不能替用户说话",
          all("不要代替" in b or "绝不代替" in b for b in (body_basic, body_std, body_adv)))
    for _lv in ("basic", "standard", "advanced"):
        rp_config.save_tier(_lv, persist=False)
        _full = rp_prompts.build_rules("我", "rp", "甲")
        check("第 %s 档带状态说明" % _lv, rp_prompts.status_prompt()[:20] in _full)
    rp_config.save_tier("basic", persist=False)

    # 35. 分叉要把轮次和账本退回分叉点，别把没发生的事带过去
    br = client.post("/api/sessions", json={}).get_json()
    br_sess = rp_storage.load_session(br["id"])
    br_sess["turn"] = 9
    br_sess["status"] = {"location": "后巷"}
    br_sess["facts"] = [
        {"fact": "第三轮的旧事", "state": "active", "turn": 2},
        {"fact": "第九轮的新事", "state": "active", "turn": 8},
    ]
    br_sess["messages"] = [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "回应", "speaker": "甲", "turn": 3},
        {"role": "user", "content": "第二句"},
        {"role": "assistant", "content": "后来的回应", "speaker": "甲", "turn": 9},
    ]
    rp_storage.save_session(br_sess)
    cut = client.post("/api/sessions/%s/branch" % br["id"], json={"index": 1}).get_json()
    check("分叉只保留到那一条", len(cut["messages"]) == 2, str(len(cut["messages"])))
    check("轮次退回分叉点", cut["turn"] == 3, str(cut["turn"]))
    check("账本只留分叉之前的事实",
          [f["fact"] for f in cut["facts"]] == ["第三轮的旧事"],
          str(cut["facts"]))
    check("原会话不受影响", len(rp_storage.load_session(br["id"])["messages"]) == 4)
    check("分叉标题带标记", cut["title"].endswith("（分支）"), cut["title"])
    check("分叉换新 id", cut["id"] != br["id"])

    # 36. 备份能还原，且 Key 不会被包里的空值冲掉
    import rp.backup as rp_backup
    made_restore = client.post("/api/sessions", json={}).get_json()
    client.put("/api/sessions/%s" % made_restore["id"], json={"title": "还原标记"})
    settings_path = os.path.join(rp_config.DATA_DIR, "settings.json")
    with open(settings_path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("providers", {})["deepseek"] = {
        "base_url": "https://api.deepseek.com/v1", "api_key": "sk-keep-me", "models": "deepseek-chat"}
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    item = rp_backup.create("manual")
    pack = rp_backup.path_of(item["name"])
    with zipfile.ZipFile(pack) as zf:
        packed = json.loads(zf.read("data/settings.json").decode("utf-8"))
    check("包里不含 API Key",
          packed["providers"]["deepseek"]["api_key"] == "", repr(packed["providers"]["deepseek"]))

    client.delete("/api/sessions/%s" % made_restore["id"])
    check("还原前会话已删掉", not rp_storage.load_session(made_restore["id"]))

    res = client.post("/api/backups/%s/restore" % item["name"]).get_json()["result"]
    check("还原写回了文件", res["files"] > 0, str(res))
    check("还原留了保险包", "beforerestore" in res["guard"].replace("-", ""), res["guard"])
    check("会话回来了", bool(rp_storage.load_session(made_restore["id"])))
    with open(settings_path, encoding="utf-8") as f:
        after = json.load(f)
    check("还原后 Key 被补回来",
          after["providers"]["deepseek"]["api_key"] == "sk-keep-me",
          after["providers"]["deepseek"].get("api_key", "")[:8])
    check("还原后配置重新读进内存", res["keys"] >= 0)

    # 路径穿越的包名进不来
    check("备份名挡住 ../", rp_backup.path_of("../settings.json") is None)
    bad = client.post("/api/backups/%s/restore" % "..%2F..%2Fetc")
    check("非法备份名还原被拒", bad.status_code in (404, 400), str(bad.status_code))

    # 37. 创意程度：温度 + 写法倾向，两样都随档位走
    check("创意档位有三个", list(rp_config.CREATIVITY_LEVELS) == ["standard", "creative", "wild"])
    _temps = [rp_config.creativity_temp(x) for x in rp_config.CREATIVITY_LEVELS]
    check("温度逐档变高", _temps == sorted(_temps) and len(set(_temps)) == 3, str(_temps))
    check("发散档不超过重试上限", rp_config.creativity_temp("wild") <= 1.2)

    def _tail(level):
        rp_config.save_creativity(level, persist=False)
        sess = {"turn": 1, "mode": "rp", "user_name": "甲",
                "characters": [{"name": "甲", "role": "守灯人"}],
                "messages": [{"role": "user", "content": "我推门进去。"}]}
        return rp_prompts.build_messages(sess, "甲")[-1]["content"]

    check("标准档不提换角度", "别写第一反应" not in _tail("standard"))
    check("创意档要求换一个接法", "别写第一反应" in _tail("creative"))
    _wild = _tail("wild")
    check("发散档允许节外生枝", "节外生枝" in _wild)
    check("发散档仍然不许替用户决定", "不能替用户决定" in _wild)
    rp_config.save_creativity("standard", persist=False)
    check("创意档落盘再读回",
          (client.post("/api/writing", json={"creativity": "creative"}).get_json()["creativity"])
          == "creative")
    client.post("/api/writing", json={"creativity": "standard"})
    check("配置接口带出三个档位",
          client.get("/api/config").get_json()["creativities"] == ["standard", "creative", "wild"])
    check("非法创意档被忽略",
          client.post("/api/writing", json={"creativity": "insane"}).get_json()["creativity"]
          == "standard")

    # 38. 设置分节保存 + 还原默认
    client.post("/api/writing", json={"narrator_min": 77, "narrator_max": 88,
                                      "creativity": "wild", "reasoning_effort": "high",
                                      "tier": "advanced", "mode": "rp"})
    _before = client.get("/api/config").get_json()
    check("还原前是改过的值", _before["writing"]["narrator_min"] == 77, str(_before["writing"]["narrator_min"]))

    _reset = client.post("/api/writing/reset").get_json()
    check("还原默认返回默认写作参数", _reset["writing"]["narrator_min"] == rp_config.WRITING_DEFAULTS["narrator_min"],
          str(_reset["writing"]["narrator_min"]))
    check("还原默认把模式换回默认", _reset["mode"] == rp_config.MODE_DEFAULT, _reset["mode"])
    check("还原默认把档位换回默认", _reset["tier"] == rp_config.TIER_DEFAULT, _reset["tier"])
    check("还原默认把思考强度换回默认", _reset["reasoning_effort"] == rp_config.REASONING_DEFAULT,
          _reset["reasoning_effort"])
    check("还原默认把创意程度换回默认", _reset["creativity"] == rp_config.CREATIVITY_DEFAULT,
          _reset["creativity"])
    _after = client.get("/api/config").get_json()
    check("还原默认后上下文跟着档位", _after["writing"]["context_limit"] == rp_config.TIER_LIMITS["basic"]["context"],
          str(_after["writing"]["context_limit"]))
    check("还原默认不动模型选择",
          _after["model"] == _before["model"] and _after["provider"] == _before["provider"],
          _after["model"])

    # 弹窗结构：底部不再有统一保存，三节各有自己的保存按钮
    _html = (pathlib.Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    check("底部统一保存按钮已删除", 'id="prefs-save"' not in _html)
    check("底部换成还原默认", 'id="prefs-reset"' in _html)
    check("两节各有保存按钮", _html.count('class="prefs-save"') == 2,
          str(_html.count('class="prefs-save"')))
    check("保存按钮分别标注", all('data-save="%s"' % k in _html for k in ("model", "writing")))
    check("外观节没有多余保存按钮", 'data-save="look"' not in _html)

    # 遮罩关闭必须同时看按下点，否则从弹窗里拖到遮罩上松手会整窗消失
    _main = (pathlib.Path(__file__).parent / "static" / "js" / "main.js").read_text(encoding="utf-8")
    check("遮罩关闭收成一个函数", "function bindBackdropClose" in _main)
    check("三个弹窗都走同一条路径", _main.count("bindBackdropClose(") == 4,
          str(_main.count("bindBackdropClose(")))
    check("按下点也参与判定", "e.target === el && onBackdrop && !moved" in _main)
    check("拖动超过阈值不算点击", "> 8" in _main.split("function bindBackdropClose")[1][:600])

    # 39. 提供方：内置不可删、可自建、ollama 不吃手填模型名、错配会被挡
    check("内置提供方只有三个", list(rp_config.PROVIDERS) == ["ollama", "deepseek", "openai"])
    check("内置的不带 custom 标记", rp_config.provider_public("deepseek")["custom"] is False)

    _ok = client.post("/api/providers/custom1", json={
        "label": "中转站", "base_url": "https://relay.example/v1",
        "api_key": "sk-test", "models": "a/b, c/d"})
    check("能新建自定义提供方", _ok.status_code == 200, _ok.get_data(as_text=True)[:120])
    _made = _ok.get_json()
    check("自定义带 custom 标记", _made["custom"] is True)
    check("自定义进得了提供方列表", "custom1" in rp_config.provider_ids(), str(rp_config.provider_ids()))
    check("自定义算可用", rp_config.provider_usable("custom1"))
    check("自定义端点拼对", rp_config.provider_endpoint("custom1") == "https://relay.example/v1/chat/completions",
          rp_config.provider_endpoint("custom1"))
    check("自定义下模型能选中", rp_config.set_current_model("custom1", "a/b") == ("custom1", "a/b"))

    _noname = client.post("/api/providers/custom9", json={"base_url": "https://x/v1"})
    check("没有名字的新提供方被拒", _noname.status_code == 400, str(_noname.status_code))

    check("内置提供方不能删", client.delete("/api/providers/deepseek").status_code == 400)
    check("自定义能删", client.delete("/api/providers/custom1").status_code == 200)
    check("删完就消失", "custom1" not in rp_config.provider_ids())
    check("删掉在用的提供方会退回本地",
          rp_config.current_target()[0] == rp_config.DEFAULT_PROVIDER, str(rp_config.current_target()))

    # ollama 的模型名不接受手填：填了会把云端模型名混进本地列表，选中就 404
    rp_config.save_provider("ollama", {"models": "deepseek-flash", "base_url": "http://evil/v1"})
    _local = rp_config.provider_public("ollama")
    check("本地模型名手填被丢弃", _local["models"] == [], str(_local["models"]))
    check("本地地址不接受覆盖", "evil" not in _local["base_url"], _local["base_url"])

    # provider 与 model 错配要被挡住，否则请求会打到错误端点然后 404
    client.post("/api/providers/custom2", json={"label": "X", "base_url": "https://x/v1",
                                                "api_key": "k", "models": "only-this"})
    _bad = client.post("/api/model", json={"provider": "custom2", "model": "别的模型"})
    check("错配的模型名被拒", _bad.status_code == 400, str(_bad.status_code))
    check("配对的模型名能设", client.post("/api/model", json={"provider": "custom2", "model": "only-this"}).status_code == 200)
    client.post("/api/model", json={"provider": "ollama", "model": "dmtx/qwen3.5-9b-abliterated:latest"})
    client.delete("/api/providers/custom2")

    # 重启后自定义提供方还在
    rp_config.save_provider("custom3", {"label": "留存检查", "base_url": "https://keep/v1",
                                        "api_key": "k2", "models": "m1"})
    rp_config.load_settings()
    check("重新加载后自定义提供方还在", "custom3" in rp_config.provider_ids())
    check("名字也读回来了", rp_config.provider_public("custom3")["label"] == "留存检查")
    client.delete("/api/providers/custom3")

    # 界面结构
    _html2 = (pathlib.Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    check("有起名输入框", 'id="p-label"' in _html2)
    check("有删除按钮", 'id="p-del"' in _html2)
    _main2 = (pathlib.Path(__file__).parent / "static" / "js" / "main.js").read_text(encoding="utf-8")
    check("下拉里有新增项", "新增提供方" in _main2)
    check("设置面板用全量提供方列表", "state.allProviders || state.providers" in _main2)

    # 40. 回合平衡（跟着用户输入密度走）+ 时间线注入
    def _sess(text, time=""):
        st = {"time": time} if time else {}
        return {"turn": 3, "mode": "rp", "user_name": "我",
                "characters": [{"name": "甲", "role": "守灯人"}],
                "status": st, "messages": [{"role": "user", "content": text}]}

    check("极短输入判成 thin", rp_prompts.input_density(_sess("我点头。"))[0] == "thin")
    check("长段动作判成 thick",
          rp_prompts.input_density(_sess("我推开门，抬手挡了一下灯，转身把包扔在椅子上，走过去拉开窗帘。"))[0] == "thick")
    check("中等输入判成 normal",
          rp_prompts.input_density(_sess("我把外套挂在门边，问他今晚还走不走。"))[0] == "normal")
    _thin = rp_prompts.input_density(_sess("我点头。"))[1]
    _thick = rp_prompts.input_density(_sess("我推开门，抬手挡了一下灯，转身把包扔在椅子上，走过去拉开窗帘。"))[1]
    check("极短时目标字数抬高", _thin[1] > _thick[1], "%s vs %s" % (_thin, _thick))
    check("长段时目标字数收紧", _thick[1] < _thin[1] and _thick[1] <= 400, str(_thick))
    check("上限不再随极短输入暴涨",
          rp_prompts.input_density(_sess("我点头。"))[1][1] <= 460,
          str(rp_prompts.input_density(_sess("我点头。"))[1]))

    def _tail_of(sess):
        return rp_prompts.build_messages(sess, "甲")[-1]["content"]

    check("极短时要求撑起场景", "别跟着变短" in _tail_of(_sess("我点头。")))
    check("长段时要求不复述", "不要复述我刚写过的动作" in _tail_of(_sess("我推开门，抬手挡了一下灯，转身把包扔在椅子上，走过去拉开窗帘。")))
    check("中等时不多加节奏要求", "别跟着变短" not in _tail_of(_sess("我把外套挂在门边，问他今晚还走不走。")))

    check("没有时间就不提时间", "上一轮结束时是" not in _tail_of(_sess("我点头。")))
    check("有时间就把上一轮的时间写进去", "上一轮结束时是「深夜」" in _tail_of(_sess("我点头。", "深夜")))
    check("要求时间自然推进", "自然往下走" in _tail_of(_sess("我点头。", "深夜")))
    check("提醒补过渡", "交代清过了多久" in _tail_of(_sess("我点头。", "深夜")))

    # 刷新页面不该把还在生成的会话判成"没答上"
    _ui = (pathlib.Path(__file__).parent / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    check("刷新后会先看后端忙不忙", "state.session.busy" in _ui)
    check("忙的时候显示正在写", "AI 正在写这一轮" in _ui)
    check("会盯着会话等它写完", "setInterval" in _ui and "sess.busy" in _ui)
    check("角色弹层用状态不用设定", "['设定', c.role]" not in _ui)
    check("顶栏带场景和时间", "['location', '地点']" in _ui and "['time', '时间']" in _ui)
    _css = (pathlib.Path(__file__).parent / "static" / "css" / "app.css").read_text(encoding="utf-8")
    check("hidden 属性不会被 display 盖掉", "[hidden] { display: none !important; }" in _css)

    # 41. 流式停止（草稿入库）+ Markdown 导出
    _sp = client.post("/api/sessions", json={}).get_json()
    client.put("/api/sessions/%s" % _sp["id"], json={
        "title": "导出检查", "mode": "rp", "user_name": "我",
        "characters": [{"name": "甲", "role": "守灯人", "state": "靠在门边"}],
        "world": "海边有座孤塔。"})
    rp_storage.save_session(dict(rp_storage.load_session(_sp["id"]), messages=[
        {"role": "user", "content": "我推门进去。"},
        {"role": "assistant", "content": "你来了。", "speaker": "甲", "narrator": "门轴响了一声。"},
    ], status={"location": "灯塔二层", "time": "深夜", "present": ["甲"]},
        facts=[{"fact": "钥匙在抽屉里"}], turn=2))

    _r = client.post("/api/sessions/%s/commit_partial" % _sp["id"],
                     json={"content": "他没抬头，只把灯芯挑亮了一点。", "speaker": "甲"})
    check("草稿能入库", _r.status_code == 200, str(_r.status_code))
    _msgs = _r.get_json()["messages"]
    check("草稿带 partial 标记", _msgs[-1].get("partial") is True)
    check("草稿带说话人", _msgs[-1].get("speaker") == "甲")
    check("草稿进了历史", len(_msgs) == 3, str(len(_msgs)))
    check("空草稿被拒",
          client.post("/api/sessions/%s/commit_partial" % _sp["id"], json={"content": "   "}).status_code == 400)

    _md = client.get("/api/sessions/%s/export.md" % _sp["id"])
    check("导出返回 200", _md.status_code == 200)
    check("导出是 markdown", "markdown" in _md.headers.get("Content-Type", ""),
          _md.headers.get("Content-Type"))
    check("导出带附件名", "filename*=UTF-8" in _md.headers.get("Content-Disposition", ""),
          _md.headers.get("Content-Disposition"))
    _text = _md.get_data(as_text=True)
    check("导出有标题", _text.startswith("# 导出检查"), _text[:20])
    check("用户消息渲染成引用", "> **我**" in _text)
    check("旁白单独成行", "*门轴响了一声。*" in _text)
    check("角色言行带名字", "**甲**：你来了。" in _text)
    check("草稿有标注", "停止后保留的草稿" in _text)
    check("附录有状态", "## 结尾时的状态" in _text and "灯塔二层" in _text)
    check("附录有账本", "## 事实账本" in _text and "钥匙在抽屉里" in _text)
    check("导出不存在的会话 404", client.get("/api/sessions/nope/export.md").status_code == 404)
    client.delete("/api/sessions/%s" % _sp["id"])

    # 前端结构：停止按钮换掉发送、草稿两条出路、导出入口
    _h = (pathlib.Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    check("有停止按钮", 'id="btn-stop"' in _h)
    check("有导出按钮", 'id="btn-export"' in _h)
    _c = (pathlib.Path(__file__).parent / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    check("生成中把发送换成停止", "showStopButton" in _c and "hideStopButton" in _c)
    check("停止走 AbortController", "stopper.abort" in _c)
    check("草稿能保留", "commit_partial" in _c and "draft-keep" in _c)
    check("草稿能丢弃重写", "draft-drop" in _c)
    check("停止时不谎报模型失败", "你停止了这一轮" in
          (pathlib.Path(__file__).parent / "static" / "js" / "ui.js").read_text(encoding="utf-8"))
    _api = (pathlib.Path(__file__).parent / "static" / "js" / "api.js").read_text(encoding="utf-8")
    check("streamSSE 接受外部中止信号", "signal }" in _api or "signal)" in _api)

    # 42. 一般模式必须把 AI 自己写的旁白喂回上下文（否则每轮都从头起笔）
    _nm = {
        "title": "一般模式", "mode": "normal", "user_name": "我",
        "characters": [{"name": "甲", "role": "守灯人"}],
        "status": {"present": ["甲"], "location": "灯塔"},
        "turn": 3,
        "messages": [
            {"role": "user", "content": "我推门进去。"},
            {"role": "assistant", "content": "", "narrator": "门轴响了一声，屋里只有一盏灯。"},
            {"role": "user", "content": "我问他为什么不开灯。"},
            {"role": "assistant", "content": "", "narrator": "他没回答，只是把灯芯挑亮了些。"},
        ],
    }
    _bm = rp_prompts.build_messages(_nm, None)
    _asst = [m for m in _bm if m["role"] == "assistant"]
    check("一般模式不丢 AI 历史", len(_asst) == 2, "assistant 条数 %d" % len(_asst))
    check("旁白按 assistant 原样回放",
          any("门轴响了一声" in m["content"] for m in _asst))
    check("两次旁白都在",
          any("灯芯挑亮" in m["content"] for m in _asst))
    _pairs = [(a["role"], b["role"]) for a, b in zip(_bm, _bm[1:])]
    check("没有连续两条 user 挨着", ("user", "user") not in _pairs)

    # 43. 从这里重写要连轮次和账本一起退，否则 turn 会飘到几十轮
    _rw = client.post("/api/sessions", json={}).get_json()
    _rs = rp_storage.load_session(_rw["id"])
    _rs["messages"] = [
        {"role": "user", "content": "一", "turn": 0},
        {"role": "assistant", "content": "回一", "speaker": "甲", "turn": 1},
        {"role": "user", "content": "二", "turn": 1},
        {"role": "assistant", "content": "回二", "speaker": "甲", "turn": 2},
        {"role": "user", "content": "三", "turn": 2},
        {"role": "assistant", "content": "回三", "speaker": "甲", "turn": 9},
        {"role": "user", "content": "四", "turn": 9},
    ]
    _rs["turn"] = 9
    _rs["facts"] = [
        {"turn": 0, "fact": "第一件事", "state": "active"},
        {"turn": 5, "fact": "后来的事", "state": "active"},
    ]
    rp_storage.save_session(_rs)
    _back = client.post("/api/sessions/%s/rewind" % _rw["id"], json={"index": 2}).get_json()
    check("重写把轮次退回去", _back["turn"] == 1, str(_back["turn"]))
    check("重写把消息截断", len(_back["messages"]) == 2, str(len(_back["messages"])))
    check("重写退掉分叉点之后的账本",
          [f["fact"] for f in _back["facts"]] == ["第一件事"],
          str([f["fact"] for f in _back["facts"]]))
    check("重写不删分叉点之前的事", len(_back["facts"]) >= 1)
    _br = client.post("/api/sessions/%s/branch" % _rw["id"], json={"index": 1}).get_json()
    check("分叉同样退回轮次", _br["turn"] == 1, str(_br["turn"]))
    check("分叉同样过滤账本", [f["fact"] for f in _br["facts"]] == ["第一件事"],
          str([f["fact"] for f in _br["facts"]]))
    client.delete("/api/sessions/%s" % _rw["id"])
    client.delete("/api/sessions/%s" % _br["id"])

    # 44. 账本注入不再写"第几轮"：模型不知道轮次，写个虚高的数字只会让它时间感错乱
    _fs = {"title": "账本", "mode": "rp", "user_name": "我", "turn": 3,
           "facts": [{"turn": 28, "fact": "钥匙在抽屉里", "state": "active"}]}
    _fm = rp_prompts.build_messages(_fs, None)
    _ftext = chr(10).join(m["content"] for m in _fm)
    check("账本注入不带轮次", "第 28 轮" not in _ftext)
    check("账本事实本身还在", "钥匙在抽屉里" in _ftext)

    # 45. 改了自己的话：记下 edited、问要不要重生成
    _es = client.post("/api/sessions", json={}).get_json()
    _esess = rp_storage.load_session(_es["id"])
    _esess["messages"] = [
        {"role": "user", "content": "我推门进去。"},
        {"role": "assistant", "content": "你来了。", "speaker": "甲"},
    ]
    rp_storage.save_session(_esess)
    _e1 = client.put("/api/sessions/%s/message" % _es["id"],
                     json={"index": 0, "content": "我推门进去，风跟着灌了进来。"}).get_json()
    check("改过内容就打 edited 标记", _e1["messages"][0].get("edited") is True)
    check("内容确实换了", _e1["messages"][0]["content"] == "我推门进去，风跟着灌了进来。")
    check("后面的回复没被删", len(_e1["messages"]) == 2)
    _e2 = client.put("/api/sessions/%s/message" % _es["id"],
                     json={"index": 0, "content": "我推门进去，风跟着灌了进来。"}).get_json()
    check("原样再存不会重复动", _e2["messages"][0].get("edited") is True)
    check("还是只有一条用户消息",
          sum(1 for m in _e2["messages"] if m["role"] == "user") == 1)
    client.delete("/api/sessions/%s" % _es["id"])

    _edjs = (pathlib.Path(__file__).parent / "static" / "js" / "msgops.js").read_text(encoding="utf-8")
    check("内容变了才问要不要重生成", "text !== original && hasAfter(index)" in _edjs)
    check("问的时候给两个出路", "ask-yes" in _edjs and "就这样" in _edjs)
    check("点重新生成走回退重写", "rewrite(index)" in _edjs)
    _uijs = (pathlib.Path(__file__).parent / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    check("消息能渲染已编辑标", "edited-tag" in _uijs and "m.edited" in _uijs)

    # 46. 压缩要有两个闸门：字符超限、或者条数超了历史能带的量
    rp_config.save_tier("basic", persist=False)
    _calls = []
    _orig_model_text = rp_context.model_text
    rp_context.model_text = lambda m, **kw: (_calls.append(1), "提要：两人在灯塔里过了一夜。")[1]
    try:
        _cs = rp_storage.new_session()
        _cs["mode"] = "normal"
        _cmsgs = []
        for _i in range(10):
            _cmsgs.append({"role": "user", "content": "第%d句：" % _i + "我往里走了一步。" * 6,
                           "turn": _i})
            _cmsgs.append({"role": "assistant", "content": "",
                           "narrator": "第%d段：" % _i + "灯影在墙上晃。" * 6, "turn": _i + 1})
        _cs["messages"] = _cmsgs
        _cs["turn"] = 10
        rp_storage.save_session(_cs)
        _chars = rp_context.context_chars(_cs)
        check("短消息几十条时字符远没到上限", _chars < rp_config.writing()["context_limit"],
              str(_chars))
        check("条数超了历史能带的量", len(_cmsgs) > rp_config.tier_limits()["history_msgs"])
        _ok = rp_context.compress_session(_cs["id"])
        check("条数超限也触发压缩", _ok is True)
        check("压缩确实调了一次模型", len(_calls) == 1, str(len(_calls)))
        _after = rp_storage.load_session(_cs["id"])
        check("压缩一条消息都不删", len(_after["messages"]) == len(_cmsgs),
              "%d vs %d" % (len(_after["messages"]), len(_cmsgs)))
        check("压缩只推游标", int(_after.get("compressed_upto") or 0) > 0,
              str(_after.get("compressed_upto")))
        _histafter = [m for m in rp_prompts.build_messages(_after, None) if m["role"] != "system"]
        check("游标之后的历史条数够带满",
              len(_histafter) >= rp_config.tier_limits()["history_msgs"] - 1,
              "%d" % len(_histafter))
        check("压缩后留下摘要", bool((_after.get("summary") or "").strip()))
        check("游标落在用户发言上",
              _after["messages"][int(_after["compressed_upto"])].get("role") == "user",
              _after["messages"][int(_after["compressed_upto"])].get("role"))
        check("压缩后字符变少", rp_context.context_chars(_after) < _chars)
        check("刚压完不会重复压", rp_context.compress_session(_cs["id"]) is False)
        check("没有白调第二次模型", len(_calls) == 1, str(len(_calls)))
        # 摘要要进 system 提示，否则等于没留
        _built = rp_prompts.build_messages(_after, None)
        check("摘要进了上下文", "两人在灯塔里过了一夜" in _built[0]["content"])
        _p = rp_storage.session_path(_cs["id"])
        if _p:
            os.remove(_p)
    finally:
        rp_context.model_text = _orig_model_text

    # 47. 压缩失败不能每轮都重试，得有退避
    _ctxsrc = (pathlib.Path(__file__).parent / "rp" / "context.py").read_text(encoding="utf-8")
    check("压缩失败有退避", "_COMPRESS_RETRY_SECS" in _ctxsrc and "_compress_fail_at" in _ctxsrc)
    check("压缩切点对齐用户发言", 'messages[split].get("role") != "user"' in _ctxsrc)

    # 48. 重写砍进摘要范围时，游标要退，保证重写点前面还留着原文
    _rs2 = client.post("/api/sessions", json={}).get_json()
    _sv = rp_storage.load_session(_rs2["id"])
    _sv["messages"] = [{"role": "user" if i % 2 == 0 else "assistant",
                        "content": "m%d" % i, "turn": i} for i in range(20)]
    _sv["turn"] = 20
    _sv["compressed_upto"] = 12
    _sv["summary"] = "更早的提要"
    rp_storage.save_session(_sv)
    _rb = client.post("/api/sessions/%s/rewind" % _rs2["id"], json={"index": 6}).get_json()
    check("重写砍进摘要范围时游标退回", int(_rb.get("compressed_upto") or 0) == 0,
          str(_rb.get("compressed_upto")))
    _rb2 = client.post("/api/sessions/%s/rewind" % _rs2["id"], json={"index": 18}).get_json()
    check("重写点在摘要范围外时游标不动", int(_rb2.get("compressed_upto") or 0) == 0,
          str(_rb2.get("compressed_upto")))
    client.delete("/api/sessions/%s" % _rs2["id"])

    # 49. 角色弹窗只留状态和近况；删除提供方按钮改成图标
    _uijs2 = (pathlib.Path(__file__).parent / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    _pop = _uijs2[_uijs2.index("function toggleCharPop"):]
    _pop = _pop[:_pop.index("function ", 40)]
    check("角色弹窗不写时间", "status.time" not in _pop)
    check("角色弹窗仍写状态", "c.state" in _pop)
    _html2 = (pathlib.Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    check("删除按钮是图标", 'class="del-btn"' in _html2 and 'id="p-del"' in _html2)
    check("删除按钮有可读名称", 'aria-label="删除这个自定义提供方"' in _html2)
    check("删除按钮不再是文字按钮", ">删除</button>" not in _html2)
    _css2 = (pathlib.Path(__file__).parent / "static" / "css" / "app.css").read_text(encoding="utf-8")
    check("删除按钮有危险色悬停", ".del-btn:hover" in _css2 and "var(--danger)" in _css2)
    check("删除按钮有键盘焦点样式", ".del-btn:focus-visible" in _css2)

    # 50. 切档位要连「压缩后留几条」一起换
    for _t, _want in (("basic", 16), ("standard", 40), ("advanced", 60)):
        client.post("/api/writing", json={"tier": _t})
        _cf = client.get("/api/config").get_json()
        check("切到%s档后保留条数跟着变" % _t,
              _cf["writing"]["keep_recent"] == _want,
              "%d vs %d" % (_cf["writing"]["keep_recent"], _want))
        check("%s档的 tier_keep 一致" % _t, _cf["tier_keep"] == _want, str(_cf["tier_keep"]))
        check("%s档上下文预算也跟着换" % _t,
              _cf["writing"]["context_limit"] == rp_config.TIER_LIMITS[_t]["context"],
              str(_cf["writing"]["context_limit"]))
    # 面板显示的值要等于压缩真正会留的值
    _pv = client.post("/api/sessions", json={}).get_json()
    client.post("/api/writing", json={"tier": "advanced"})
    _ci = client.get("/api/sessions/%s" % _pv["id"]).get_json()["context"]
    check("面板的保留条数等于实际生效值", _ci["keep_recent"] == 60, str(_ci["keep_recent"]))
    client.delete("/api/sessions/%s" % _pv["id"])
    client.post("/api/writing", json={"tier": "basic"})

    # 51. 顶栏那一行显示当前状态
    _uijs3 = (pathlib.Path(__file__).parent / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    check("顶栏画当前状态", "sub-state" in _uijs3 and "🧭" in _uijs3)
    for _f, _lab in (("location", "地点"), ("time", "时间"), ("relations", "关系"),
                     ("goal", "目标")):
        check("顶栏带%s" % _lab, "['%s', '%s']" % (_f, _lab) in _uijs3)
    # 物品归人，不再挤在状态行里
    check("顶栏不带物品", "['items', '物品']" not in _uijs3)
    check("数组型字段会拼成顿号", "Array.isArray(v)" in _uijs3)

    # 52. 四个字数设置合起来决定一轮的总量
    _ws = {"turn": 1, "mode": "rp", "user_name": "我",
           "characters": [{"name": "甲", "role": "守灯人"}],
           "status": {}, "messages": [{"role": "user", "content": "我点头。"}]}
    _w = rp_config.writing()
    _total = _w["narrator_max"] + _w["line_max"]
    for _t in ("basic", "standard", "advanced"):
        rp_config.save_tier(_t, persist=False)
        _bd = rp_prompts.build_system_prompt(_ws, "甲")
        check("%s档的规则给出合计上限" % _t, ("不超过 %d 字" % _total) in _bd)
    rp_config.save_tier("basic", persist=False)
    _nb = rp_prompts.build_system_prompt(dict(_ws, mode="normal"), None)
    _lo = _w["narrator_min"] + _w["line_min"]
    check("一般模式用总量而不是只算台词",
          ("%d~%d 字" % (_lo, _total)) in _nb, _nb[_nb.find("每次"):][:40])
    # 改设置，规则和尾巴都要跟着变
    rp_config.save_writing({"line_max": 120}, persist=False)
    _bd2 = rp_prompts.build_system_prompt(_ws, "甲")
    check("调小台词上限后合计上限跟着降",
          ("不超过 %d 字" % (_w["narrator_max"] + 120)) in _bd2)
    _tl = rp_prompts.build_messages(_ws, "甲")[-1]["content"]
    _tl_hi = rp_prompts.input_density(_ws)[1][1]
    check("尾巴里的上限也跟着降", ("最多 %d 字" % _tl_hi) in _tl, _tl[-120:])
    check("尾巴上限是密度缩放后的值", _tl_hi >= _w["narrator_max"] + 120, str(_tl_hi))
    rp_config.save_writing({"line_max": _w["line_max"]}, persist=False)

    # 53. 一般模式也要认【新角色】【变化】，这两块以前被整段跳过
    _NL = chr(10)
    _raw = ("雨点砸在窗户上。" + _NL + "【新角色】" + '{"name": "丁", "role": "杂货铺女儿"}'
            + _NL + "【变化】" + '{"name": "乙", "change": "开始招呼新人"}'
            + _NL + "[[地点：客厅|时间：深夜]]")
    _n = rp_characters.parse_turn(_raw, None)
    check("一般模式能认出新角色", (_n["new_character"] or {}).get("name") == "丁",
          str(_n["new_character"]))
    check("一般模式能认出变化", len(_n["updates"]) == 1 and _n["updates"][0]["name"] == "乙",
          str(_n["updates"]))
    check("一般模式也能读到状态行", _n["status"].get("location") == "客厅", str(_n["status"]))
    check("新角色那行不留在正文里", '"name"' not in _n["narrator"] and "新角色" not in _n["narrator"],
          _n["narrator"])
    check("第一个标签之前的正文没被吞", _n["narrator"].startswith("雨点砸在窗户上"), _n["narrator"])
    _n2 = rp_characters.parse_turn("他念了一句【别过来】，转身走了。", None)
    check("正文里普通的方括号不吞内容", "别过来" in _n2["narrator"], _n2["narrator"])
    _n3 = rp_characters.parse_turn("雨还在下。【新角色】{坏掉的 json", None)
    check("写坏的新角色不吞正文", "雨还在下" in _n3["narrator"], _n3["narrator"])

    # 54. 高级档也要提醒写状态行，不然聊久了状态就不更新了
    rp_config.save_tier("advanced", persist=False)
    _ts = {"turn": 2, "mode": "normal", "user_name": "我", "characters": [],
           "status": {}, "messages": [{"role": "user", "content": "我把外套挂在门边。"}]}
    _adv_msgs = rp_prompts.build_messages(_ts, None)
    _nb2 = rp_prompts.build_system_prompt(_ts, None)
    check("高级档尾巴提醒写状态行", "<StatusBlock>" in _adv_msgs[-1]["content"])
    # 两套格式并存时模型每轮都收到互相矛盾的要求，跟 system 里的必须一致
    check("高级档 system 与尾巴提醒格式一致",
          ("<StatusBlock>" in _nb2) == ("<StatusBlock>" in _adv_msgs[-1]["content"]))
    check("一般模式规则给了新角色格式", "【新角色】" in _nb2)
    check("一般模式规则给了人数上限", ("全场最多 %d 人" % rp_config.writing()["max_characters"]) in _nb2)
    rp_config.save_tier("basic", persist=False)

    # 55. 设置面板的底部按钮固定
    _html3 = (pathlib.Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    check("设置面板有独立滚动区", 'class="prefs-scroll"' in _html3)
    check("底部按钮在滚动区之外",
          _html3.index('class="prefs-scroll"') < _html3.index('id="prefs-reset"')
          and _html3.rindex("</div>", 0, _html3.index('id="prefs-reset"')) > _html3.index('class="prefs-scroll"'))
    _css2 = (pathlib.Path(__file__).parent / "static" / "css" / "app.css").read_text(encoding="utf-8")
    check("滚动区自己滚不带动底部", ".prefs-scroll" in _css2 and "flex: 1 1 auto" in _css2)
    check("底部按钮不参与滚动", "#prefs-modal .modal-actions" in _css2 and "flex: none" in _css2)
    check("高度按父容器算不写 vh", "max-height: 100%" in _css2)

    # 56. 旁白/叙述的样子：正常字，只留一道线做区分，收尾时不换脸
    _css3 = (pathlib.Path(__file__).parent / "static" / "css" / "app.css").read_text(encoding="utf-8")
    _ui4 = (pathlib.Path(__file__).parent / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    _narr_css = _css3[_css3.index(".narrator {"):][:400]
    check("旁白不用灰字", "--text-dim" not in _narr_css and "color: var(--text)" in _narr_css, _narr_css[:120])
    check("旁白不用小字号", "font-size: var(--msg-fs)" in _narr_css and "msg-fs) - 1px" not in _narr_css)
    check("旁白留一道左边线做区分", "border-left: 2px solid" in _narr_css)
    check("旁白标签的样式还在", ".narr-tag" in _css3)
    check("标签内容来自故事名", "storyName()" in _ui4 and "label.textContent = tag" in _ui4)
    check("一般模式的流式也画成旁白块",
          "if (plain) {" in _ui4 and "const el = narratorNode(text || '')" in _ui4)
    check("流式和落盘共用同一套旁白结构",
          _ui4.count("narratorNode(") >= 4)

    # 57. 角色上限由设置决定，最高 10 个；一般模式也要真的写进角色表
    _cs = rp_storage.new_session()
    _cs["mode"] = "normal"
    _cs["characters"] = [{"name": "甲", "role": "主角", "state": "", "history": []}]
    rp_config.save_writing({"max_characters": 10}, persist=False)
    _NL2 = chr(10)
    for _i in range(1, 10):
        _t2 = rp_characters.parse_turn(
            "雨还在下。" + _NL2 + "【新角色】" + ('{"name": "角色%d", "role": "路人"}' % _i), None)
        rp_characters.upsert_character(_cs, _t2["new_character"])
    check("角色上限 10 能招满", len(_cs["characters"]) == 10, str(len(_cs["characters"])))
    check("第 11 个超出上限被拒",
          rp_characters.upsert_character(_cs, {"name": "第十一个"}) is None)
    check("调小上限不会踢掉已有角色", len(_cs["characters"]) == 10)
    rp_config.save_writing({"max_characters": 4}, persist=False)
    check("调小上限后不再收新人",
          rp_characters.upsert_character(_cs, {"name": "再来一个"}) is None)
    check("上限范围是 1 到 10", rp_config.WRITING_BOUNDS["max_characters"] == (1, 10),
          str(rp_config.WRITING_BOUNDS["max_characters"]))
    _html4 = (pathlib.Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    check("界面写的是角色上限不是同时在场",
          "角色上限" in _html4 and "同时在场角色上限" not in _html4)
    check("输入框 max 跟着改成 10", 'id="w-chars" type="number" min="1" max="10"' in _html4)

    # 58. 物品绑定角色 + 主角也在场上
    _src1, _st1 = rp_characters._take_mark_line("[[物品：乙=纱裙、皮鞭；甲=手机]]")
    check("物品能按人分开",
          _st1.get("items") == {"乙": "纱裙、皮鞭", "甲": "手机"}, str(_st1))
    _src2, _st2 = rp_characters._take_mark_line("[[物品：一把湿伞]]")
    check("不写名字还是公共物品", _st2.get("items") == "一把湿伞", str(_st2))
    _src3, _st3 = rp_characters._take_mark_line("[[物品：乙＝纱裙；公共的钥匙]]")
    check("全角等号也认", (_st3.get("items") or {}).get("乙") == "纱裙", str(_st3))
    _merged = rp_characters.merge_status({"items": {"乙": "纱裙", "甲": "手机"}},
                                         {"items": {"乙": "纱裙碎了"}})
    check("物品合并不冲掉别人",
          _merged["items"] == {"乙": "纱裙碎了", "甲": "手机"}, str(_merged))

    # 59. 角色满了要问用户，不能静默丢掉
    _full = rp_storage.new_session()
    _full["mode"] = "normal"
    rp_config.save_writing({"max_characters": 2}, persist=False)
    _full["characters"] = [{"name": "甲", "role": "", "state": "", "history": []},
                           {"name": "乙", "role": "", "state": "", "history": []}]
    _r = rp_characters.upsert_character(_full, {"name": "丙", "role": "新人"})
    check("满了先不登记", _r is None)
    check("但记下了待定角色", (_full.get("pending_character") or {}).get("name") == "丙",
          str(_full.get("pending_character")))
    rp_config.save_writing({"max_characters": 4}, persist=False)
    _apijs = (pathlib.Path(__file__).parent / "rp" / "api.py").read_text(encoding="utf-8")
    check("有让用户取舍的接口", "/character-pick" in _apijs)
    _chjs = (pathlib.Path(__file__).parent / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    _uijs2 = (pathlib.Path(__file__).parent / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    check("前端会弹选择框", "askCharacterPick" in _chjs and "export function askCharacterPick" in _uijs2)
    check("选择框能选不要新角色", "不要新角色" in _uijs2)
    check("主角单独成格，不再混进在场名单", "put('主角：')" in _uijs2)
    check("主角也有自己的状态弹层", "if (!c && name === me)" in _uijs2)

    # 62. 圆桌式：一轮旁白 + 在场角色各自发言
    _round = ("夜深了，客厅只剩一盏灯。" + chr(10) + chr(10)
              + "【乙】她回过头：「你还没睡。」" + chr(10) + chr(10)
              + "【丙】她把膝盖抱得更紧了。")
    _r6 = rp_characters.parse_turn(_round, "", ["乙", "丙"])
    check("旁白和发言分开",
          _r6["narrator"].startswith("夜深") and len(_r6["lines"]) == 2, str(_r6["lines"]))
    check("每个在场角色各一条", [x["speaker"] for x in _r6["lines"]] == ["乙", "丙"],
          str(_r6["lines"]))
    check("认不出的名字不吞内容", "路人" not in _r6["narrator"])

    _r7 = rp_characters.parse_turn("开头。" + chr(10) + "【路人甲】他说了一句。", "", ["乙"])
    check("临时路人也能露脸", len(_r7["lines"]) == 1 and _r7["narrator"] == "开头。", str(_r7))
    check("rp 不再按轮次指定发言人", rp_characters.next_speaker({"mode": "rp", "turn": 3,
          "characters": [{"name": "A", "role": "", "state": "", "history": []}]}) == "")
    _rules_rp = rp_prompts.build_rules("我", "rp", "乙")
    check("规则要求先旁白再逐人发言",
          "旁白" in _rules_rp and "各写一段" in _rules_rp, _rules_rp[:90])

    # 63. 角色卡统一一条「状况」；物品只在他自己的弹层里
    _ui63 = (pathlib.Path(__file__).parent / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    check("人物只留一条状况", "['近况'" not in _ui63)
    check("所有角色都有状况行", "rows.push(['状况'" in _ui63)
    check("状况不再和近况重复", "t !== c.state" in _ui63)
    check("顶栏那行不再列物品", "['items', '物品']" not in _ui63)
    check("物品进角色弹层", "status.items[name]" in _ui63)

    # 64. 备份份数可调，上限 30
    check("备份份数有默认", rp_config.BACKUP_DEFAULTS.get("keep") == 30)
    check("备份份数有上界", rp_config.BACKUP_BOUNDS.get("keep") == (1, 30))
    _bk = rp_config.save_backup_conf({"keep": 99})
    check("超过 30 会被压回 30", _bk["keep"] == 30, str(_bk))
    rp_config.save_backup_conf({"keep": 30})
    _bjs = (pathlib.Path(__file__).parent / "static" / "js" / "backup.js").read_text(encoding="utf-8")
    check("界面上能填份数", "#backup-keep" in _bjs)
    _h64 = (pathlib.Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    check("输入框上限是 30", 'id="backup-keep" min="1" max="30"' in _h64)

    # 65. 角色弹层能点开，且不许用 state 当局部变量名（会撞上模块级 state，TDZ 直接崩）
    _ui65 = (pathlib.Path(__file__).parent / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    _pop = _ui65[_ui65.index("export function toggleCharPop"):]
    _tail = chr(10) + "def "
    _pop = _pop[:_pop.index(_tail)] if _tail in _pop else _pop
    check("弹层里没有叫 state 的局部变量", "const state =" not in _pop and "let state =" not in _pop)
    check("状况行始终存在", "['状况'" in _pop)
    check("只要状况，不要额外的最近一行", "lastSaid" not in _pop and "['最近'" not in _pop)
    check("在场名单会剔掉「无」这类占位", "_clean_names" in rp_characters.__dict__ or "def _clean_names" in
          (pathlib.Path(__file__).parent / "rp" / "characters.py").read_text(encoding="utf-8"))
    check("在场名单不再被角色数硬截断",
          'names = _clean_names(value)' in (pathlib.Path(__file__).parent / "rp" / "characters.py").read_text(encoding="utf-8"))

    # 66. 四层对账：常量 / 界面 min-max / 磁盘现值 / 代码引用，任何一层对不上就报
    _html66 = (pathlib.Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    _UI_IDS = {
        "narrator_min": "w-narrator-min", "narrator_max": "w-narrator-max",
        "line_min": "w-line-min", "line_max": "w-line-max",
        "context_limit": "w-context", "keep_recent": "w-keep",
        "max_characters": "w-chars", "summary_chars": "w-summary",
        "every_minutes": "backup-minutes", "keep": "backup-keep",
    }
    _src66 = "".join(f.read_text(encoding="utf-8") for f in
                     sorted((pathlib.Path(__file__).parent / "rp").rglob("*.py")))
    for _k, _ui in _UI_IDS.items():
        _bounds = rp_config.WRITING_BOUNDS.get(_k) or rp_config.BACKUP_BOUNDS.get(_k)
        _tag = re.search(r'<input[^>]*id="%s"[^>]*>' % re.escape(_ui), _html66)
        check("界面有 %s 的输入框" % _k, bool(_tag), _ui)
        if _tag:
            _lo = re.search(r'min="(\d+)"', _tag.group(0))
            _hi = re.search(r'max="(\d+)"', _tag.group(0))
            check("界面 %s 的下限和常量一致" % _k,
                  _lo and int(_lo.group(1)) == _bounds[0],
                  "%s vs %s" % (_lo.group(1) if _lo else "缺", _bounds))
            check("界面 %s 的上限和常量一致" % _k,
                  _hi and int(_hi.group(1)) == _bounds[1],
                  "%s vs %s" % (_hi.group(1) if _hi else "缺", _bounds))
        check("配置键 %s 真的被用到" % _k, _src66.count('"' + _k + '"') >= 2,
              "只出现 %d 次" % _src66.count('"' + _k + '"'))
    # 档位里的每个键也得有人用，别留死配置
    for _k in ("context", "history_msgs", "role_chars", "facts", "anchor_every", "attempts"):
        check("档位键 %s 有人用" % _k, _src66.count('["%s"]' % _k) >= 1)

    # 67. 全文搜索 / 草稿 / 浮标 / 冲突提示
    _s67 = rp_storage.new_session(title="搜索测试")
    _s67["messages"] = [{"role": "user", "content": "我把青瓷瓶放回架子上了"},
                        {"role": "assistant", "narrator": "青瓷瓶在架子上晃了一下"}]
    rp_storage.save_session(_s67)
    _hit = client.get("/api/search?q=" + urllib.parse.quote("青瓷瓶")).get_json()
    check("全文搜索能命中正文", bool(_hit["hits"]), str(_hit)[:80])
    check("搜索带出轮次和片段",
          _hit["hits"] and _hit["hits"][0]["index"] == 0 and "青瓷瓶" in _hit["hits"][0]["snippet"])
    check("少于两个字不搜", client.get("/api/search?q=a").get_json()["hits"] == [])
    _ui67 = (pathlib.Path(__file__).parent / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    check("草稿按会话 id 存本地", "rp-draft:" in _ui67 and "saveDraftSoon" in _ui67)
    check("草稿写入是 debounce 的", "clearTimeout(draftTimer)" in _ui67 and "}, 500)" in _ui67)
    _html67 = (pathlib.Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    check("有「新消息」浮标", 'id="jump-latest"' in _html67)
    check("有冲突提示挂点", 'id="conflict-tip"' in _html67)
    _chat67 = (pathlib.Path(__file__).parent / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    check("发送前查物品归属", "function itemConflict" in _chat67 and "conflictPassed" in _chat67)
    check("发送后清掉草稿",
          "clearDraft(state.session.id)" in _chat67 and "export function clearDraft" in _ui67)
    check("清草稿时顺手取消尚未执行的写入",
          "clearTimeout(draftTimer);" in _ui67[_ui67.index("export function clearDraft"):][:400])
    check("单字物品不参与比对", "name.length < 2" in _chat67)
    check("冲突判定先看主角名下有没有",
          "const mine = new Set(split(items[me]))" in _chat67 and "if (mine.has(name)) continue;" in _chat67)
    check("同一件东西记在多人名下时全列出来",
          "found.set(name, [])" in _chat67 and "owners.join" in _chat67 and "hit.owner " not in _chat67)

    # 68. 数据层：乐观锁、半截幂等、取消标志、物品与事实分工
    _s68 = rp_storage.new_session(title="冲突测试")
    rp_storage.save_session(_s68)
    _stamp = rp_storage.load_session(_s68["id"])["updated_at"]
    try:
        rp_storage.save_session(dict(_s68), expect=_stamp - 5)
        check("旧版本写入会被拦下", False, "没抛 StaleSession")
    except rp_storage.StaleSession:
        check("旧版本写入会被拦下", True)
    _again = rp_storage.load_session(_s68["id"])
    check("正常版本能写",
          rp_storage.save_session(dict(_again), expect=_again["updated_at"]) is None)
    check("updated_at 是数值不是字符串",
          isinstance(rp_storage.current_stamp(_s68["id"]), (int, float)))

    # 半截入库不能和完整回复同时存两条
    _p68 = client.post("/api/sessions", json={}).get_json()
    _full = "他把钥匙放在桌上，转身走了。雨还在下。"
    client.post("/api/sessions/%s/commit_partial" % _p68["id"],
                json={"content": _full})
    _m68 = rp_storage.load_session(_p68["id"])["messages"]
    check("半截能入库", len(_m68) == 1 and _m68[0]["partial"])
    client.post("/api/sessions/%s/commit_partial" % _p68["id"],
                json={"content": "他把钥匙放在桌上，转身走了。"})
    _m68b = rp_storage.load_session(_p68["id"])["messages"]
    check("已存过的半截不重复插入", len(_m68b) == 1, str(len(_m68b)))

    _api68 = (pathlib.Path(__file__).parent / "rp" / "api.py").read_text(encoding="utf-8")
    check("断开时会置取消标志", "except GeneratorExit" in _api68 and "cancelled[\u0022flag\u0022] = True" in _api68)
    check("写盘前检查取消标志", _api68.count("if cancelled[\u0022flag\u0022]:") >= 2)
    _st68 = (pathlib.Path(__file__).parent / "rp" / "prompts" / "status.md").read_text(encoding="utf-8")
    check("物品与事实分工写进提示词",
          "物品只管「此刻谁手里有什么」" in _st68 and "反例" in _st68)

    # 69. 导入与耗时可见
    _src69 = client.post("/api/sessions", json={}).get_json()
    client.post("/api/sessions/%s/commit_partial" % _src69["id"],
                json={"content": "他把伞收了。"})
    _dump69 = rp_storage.load_session(_src69["id"])
    _imp69 = client.post("/api/sessions/import", json=_dump69)
    check("导入返回 200", _imp69.status_code == 200, _imp69.status_code)
    _new69 = _imp69.get_json()
    check("导入换了新 id", _new69.get("id") != _src69["id"])
    check("导入把消息带过来了", _new69.get("messages") == 1, _new69)
    _back69 = rp_storage.load_session(_new69["id"])
    check("导入的内容一致", _back69["messages"][0]["content"] == "他把伞收了。")
    check("导入疯话文件被拒",
          client.post("/api/sessions/import", json={}).status_code == 400)
    check("导入不是对象也被拒",
          client.post("/api/sessions/import", json=[1, 2]).status_code == 400)

    _ctx69 = rp_context.context_info(rp_storage.load_session(_new69["id"]))
    check("上下文信息带读写耗时",
          isinstance(_ctx69.get("io"), dict) and "save_ms" in _ctx69["io"])

    # 70. 续写的三个接缝都得在（上一次只改了函数体，签名没改，点发送直接抛 ReferenceError）
    _chat70 = (pathlib.Path(__file__).parent / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    _sig70 = [l for l in _chat70.split(chr(10)) if "async function sendMessage" in l]
    check("sendMessage 有且只有一个定义", len(_sig70) == 1, len(_sig70))
    check("形参里声明了 isContinue", bool(_sig70) and "isContinue" in _sig70[0])
    check("形参里声明了 isRetry", bool(_sig70) and "isRetry" in _sig70[0])
    check("续写请求体真的带了 continue", "continue: !!isContinue" in _chat70)
    check("续写走本地拼接而不新增一条",
          "tail.narrator = joinText(" in _chat70 and "return prev + extra" in _chat70)
    check("续写不画用户气泡", "!isRetry && !isContinue" in _chat70)
    _ui70 = (pathlib.Path(__file__).parent / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    check("只有最后一条回复能续写", "index === msgs.length - 1" in _ui70)
    check("操作条有 continue 动作", "'continue'" in _ui70)
    _mo70 = (pathlib.Path(__file__).parent / "static" / "js" / "msgops.js").read_text(encoding="utf-8")
    check("continue 动作真的被接住", "act === 'continue'" in _mo70)
    # 历史回放要把一轮里的多个气泡一起交给 buildTurn，
    # 少传 lines 就只剩第一个人的台词
    check("历史消息把 lines 也传给了",
          "m.content, m.lines)" in _ui70)

    # 71. 续写拼接、分叉不动原会话、并发写
    _j71 = rp_api._join_text
    check("续写：句号尾不重复",
          _j71("他走了。", "他走了。雨还在下。") == "他走了。雨还在下。",
          _j71("他走了。", "他走了。雨还在下。"))
    check("续写：逗号尾不重复",
          _j71("他停下，", "他停下，又控不住了。") == "他停下，又控不住了。")
    check("续写：省略号尾不重复",
          _j71("他张了张嘴……", "……什么也没说。") == "他张了张嘴……什么也没说。")
    check("续写：不重复时原样拼",
          _j71("他走了。", "雨还在下。") == "他走了。雨还在下。")
    check("续写：空尾不动", _j71("他走了。", "") == "他走了。")
    check("续写：前面空就直接用后面", _j71("", "雨。") == "雨。")

    # 分叉不能动原会话的字节
    _b71 = client.post("/api/sessions", json={}).get_json()
    client.post("/api/sessions/%s/commit_partial" % _b71["id"],
                json={"content": "第一段。"})
    _path71 = rp_storage.session_path(_b71["id"])
    _before71 = pathlib.Path(_path71).read_bytes()
    _copy71 = client.post("/api/sessions/%s/branch" % _b71["id"], json={"index": 0})
    check("分叉返回 200", _copy71.status_code == 200, _copy71.status_code)
    check("分叉后原会话字节不变",
          pathlib.Path(_path71).read_bytes() == _before71)
    check("分叉出来的是新 id",
          (_copy71.get_json() or {}).get("id") not in (None, _b71["id"]))

    # 两个线程同时持旧版本写：只能成一个
    _c71 = client.post("/api/sessions", json={}).get_json()
    _stamp71 = rp_storage.load_session(_c71["id"])["updated_at"]
    _codes = []
    def _race():
        r = client.put("/api/sessions/%s" % _c71["id"],
                       json={"world": "同时写", "updated_at": _stamp71})
        _codes.append(r.status_code)
    _ts = [threading.Thread(target=_race) for _ in range(2)]
    [_t.start() for _t in _ts]
    [_t.join() for _t in _ts]
    check("并发写只成一个，另一个被拦",
          sorted(_codes) == [200, 409], _codes)

    # 72. 故事模板不能被对话改掉
    _st72 = client.post("/api/stories", json={"title": "模板72"}).get_json()
    client.put("/api/stories/%s" % _st72["id"], json={
        "title": "模板72",
        "world": "旧城，常年下雨。",
        "rules": "不用感叹号。",
        "characters": [{"name": "甲", "role": "守灯人。"}],
    })
    _sp72 = pathlib.Path(rp_stories.story_path(_st72["id"]))
    _before72 = _sp72.read_bytes()
    _run72 = client.post("/api/sessions", json={"story_id": _st72["id"]}).get_json()
    check("从故事开局拿到模板设定",
          _run72.get("world") == "旧城，常年下雨。", _run72.get("world"))
    client.put("/api/sessions/%s" % _run72["id"], json={
        "title": "这局自己的名字",
        "world": "这局自己的世界。",
        "characters": [{"name": "甲", "role": "守灯人。"},
                        {"name": "乙", "role": "新来的。"}],
    })
    check("在对话里改设定，故事字节不变",
          _sp72.read_bytes() == _before72)
    check("对话自己的设定真的改了",
          rp_storage.load_session(_run72["id"]).get("world") == "这局自己的世界。")
    _run72b = client.post("/api/sessions", json={"story_id": _st72["id"]}).get_json()
    check("改过一局之后，新局仍拿到模板原样",
          _run72b.get("world") == "旧城，常年下雨。"
          and [c["name"] for c in _run72b.get("characters") or []] == ["甲"],
          (_run72b.get("world"), _run72b.get("characters")))
    _n72 = len(rp_stories.list_stories())
    _blank72 = client.post("/api/sessions", json={}).get_json()
    check("空对话绑一份模板、不多建",
          bool(_blank72.get("story_id")) and len(rp_stories.list_stories()) == _n72 + 1,
          (bool(_blank72.get("story_id")), _n72, len(rp_stories.list_stories())))
    check("建空对话也没碰原模板", _sp72.read_bytes() == _before72)

    # 73. 设定里的 {主角} 占位符
    check("占位符换成真名",
          rp_stories.fill_names("{主角}推开门。", "甲") == "甲推开门。")
    check("没给名字就原样",
          rp_stories.fill_names("{主角}推开门。", "") == "{主角}推开门。")
    check("裸的主角不动",
          rp_stories.fill_names("这个故事的主角很安静。", "甲")
          == "这个故事的主角很安静。")
    check("混用时只换带花括号的",
          rp_stories.fill_names("{主角}是主角。", "甲") == "甲是主角。")

    _ph = client.post("/api/sessions", json={}).get_json()
    client.put("/api/sessions/" + _ph["id"], json={
        "user_name": "甲",
        "world": "{主角}是守灯人，{主角}怕黑。",
        "rules": "不要让{主角}轻易妥协。",
        "characters": [{"name": "丙", "role": "守灯人。"}],
    })
    _phs = rp_storage.load_session(_ph["id"])
    _sys = rp_prompts.build_system_prompt(_phs)
    check("系统提示里世界观已替换", "甲是守灯人，甲怕黑。" in _sys)
    # 额外要求是写设定用的，不该跟着故事推进跑
    check("额外要求不进故事提示", "不要让甲轻易妥协。" not in _sys)

    # 单字名（甲、乙）在中文里和普通字撞得太狠，不能见字就替换
    check("单字名不误伤正文",
          "甲板" in rp_prompts.build_field_prompt(
              "world", user_name="甲", user_role="他站在甲板上。", world="一座城"))
    check("多字名照常换成占位符",
          "{主角}站着" in rp_prompts.build_field_prompt(
              "world", user_name="林晚", user_role="林晚站着。", world="一座城"))
    # 生成角色卡时主角同样只能是占位符，否则跟 field.md 第七条打架
    check("写角色卡时主角也是占位符",
          "必须叫「甲」" not in rp_prompts.build_field_prompt(
              "character", name="丙", user_name="甲", user_role="甲站着。"))
    # 摘要和事实已经拼在 system 里，重复加一遍会虚报用量
    _cd = rp_prompts.build_context_debug({
        "user_name": "甲", "summary": "很久以前" * 30,
        "facts": [{"fact": "某件事", "state": "active"}], "messages": [],
    })
    check("上下文总量不重复计摘要和事实",
          _cd["chars"]["total"] == len(_cd["system"]) + _cd["lore_chars"],
          str(_cd["chars"]))
    check("状态提示写明事实上限", "一轮最多三条" in rp_prompts.status_prompt())
    check("写设定时不把占位符当真名",
          "必须叫「{主角}」" not in rp_prompts.build_locked_block("{主角}", ""))
    check("真名场景照旧钉死", "必须叫「甲」" in rp_prompts.build_locked_block("甲", ""))
    # 生成某个名字时，不能把那个名字锁住
    check("生成 AI 名字时不锁 AI 名字",
          "必须叫" not in rp_prompts.build_field_prompt("ai_name", ai_name="林晚"))
    check("生成主角名字时不锁主角名字",
          "必须叫" not in rp_prompts.build_field_prompt("user_name", user_name="甲"))
    check("写其他字段时仍锁住 AI 名字",
          "必须叫「林晚」" in rp_prompts.build_field_prompt(
              "world", world="一个城", user_name="甲", ai_name="林晚"))
    check("额外要求仍在写设定时生效",
          rp_prompts._extra_block("不要写血腥。", "", "")
          == "\n\n· 额外要求：\n不要写血腥。")
    check("AI 角色按真名出现，不被占位符吃掉", "丙" in _sys)
    check("存下来的仍是占位符",
          _phs["world"] == "{主角}是守灯人，{主角}怕黑。", _phs.get("world"))
    check("没定主角名就不替换",
          "{主角}" in rp_prompts.build_system_prompt(
              {"world": "{主角}是守灯人。", "user_name": ""}))

    client.put("/api/sessions/" + _ph["id"], json={"user_name": "顾辞"})
    _phs2 = rp_storage.load_session(_ph["id"])
    _sys2 = rp_prompts.build_system_prompt(_phs2)
    check("改主角名后同一份设定自动跟上",
          "顾辞是守灯人，顾辞怕黑。" in _sys2)
    check("改主角名不动存下来的设定", _phs2["world"] == _phs["world"])
    check("改 AI 角色名不影响占位符",
          "顾辞是守灯人" in rp_prompts.build_system_prompt(
              dict(_phs2, characters=[{"name": "丁", "role": "守灯人。"}])))
    check("按位置找出被改掉的名字",
          rp_api._renames([{"name": "甲"}], [{"name": "顾辞"}]) == {"甲": "顾辞"})
    # 74. 生成设定时主角给占位符
    _fp = rp_prompts.build_field_prompt("world", user_name="甲", ai_name="丙",
                                          ai_role="丙是守夜的人。", world="旧城。")
    check("生成设定时主角换成占位符", "{主角}" in _fp)
    check("生成设定时不给主角真名", "甲" not in _fp)
    check("生成设定时 AI 角色照给真名", "丙是守夜的人" in _fp)
    check("生成提示词里有占位符规则",
          "7. 「我扮演的角色」" in _fp)
    _fpc = rp_prompts.build_field_prompt("character", name="丙", ai_name="丙", ai_role="x")
    check("单张角色卡保留真名", "丙" in _fpc)
    # 75. 认人：解析与生成项
    _c1 = rp_characters.parse_cast("甲｜身份一" + chr(10)
                                   + "- 乙：身份二" + chr(10)
                                   + "无：没用" + chr(10)
                                   + "众人｜不是人")
    check("认人跳过占位词",
          [x["name"] for x in _c1] == ["甲", "乙"], str(_c1))
    check("认人能切出设定", _c1[0]["role"] == "身份一", str(_c1))
    check("认人项在生成表里", "cast" in rp_prompts.FIELD_TASKS)
    _c2 = rp_characters.parse_cast("{主角}｜占位符" + chr(10)
                                   + "主角｜不是人名")
    check("认人不把占位符当人", _c2 == [], str(_c2))
    _c3 = rp_characters.parse_cast("「丙」｜带书名号")
    check("认人去掉书名号", _c3 and _c3[0]["name"] == "丙", str(_c3))

    # 76. 生成时附带的名单行：剥离与收人
    _b1, _n1 = rp_characters.split_cast_line("甲推开门。" + chr(10) + chr(10)
                                              + "角色：乙、丙")
    check("名单行被剥掉", _b1 == "甲推开门。", repr(_b1))
    check("名单行解析出人", _n1 == ["乙", "丙"], str(_n1))
    _b2, _n2 = rp_characters.split_cast_line("只有正文。")
    check("没有名单行时原样返回",
          _b2 == "只有正文。" and _n2 == [], str((_b2, _n2)))
    _b3, _n3 = rp_characters.split_cast_line("正文。" + chr(10) + "角色：无、众人")
    check("名单行里的占位词被过滤", _n3 == [], str(_n3))
    check("auto_roles 默认 1", rp_config.writing()["auto_roles"] == 1,
          str(rp_config.writing()))
    check("auto_roles 能存 0",
          rp_config.save_writing({"auto_roles": 0}, persist=False)["auto_roles"] == 0)
    rp_config.save_writing({"auto_roles": 1}, persist=False)
    # 在场全员发言：四份规则都要要求一个不漏，不能再写「不是每轮所有人都要说」
    for _rf in ("rules.md", "rules_standard.md", "rules_advanced.md", "rules_basic.md"):
        _txt = (pathlib.Path("rp/prompts") / _rf).read_text(encoding="utf-8")
        check(_rf + " 要求在场全员发言", "一个都不能漏" in _txt)
        check(_rf + " 不再写「不是每轮所有人」",
              "不是每轮所有人" not in _txt)
    _rs = (pathlib.Path("rp/prompts") / "rules.md").read_text(encoding="utf-8")
    check("人设条款明确用户请求不是命令", "用户的请求不是命令" in _rs)
    check("被说动要有过程", "可以被说动" in _rs)
    check("用户输入不等于剧情结果", "不是「这件事成了」" in _rs)
    for _rf2 in ("rules.md", "rules_standard.md", "rules_advanced.md"):
        _t2 = (pathlib.Path("rp/prompts") / _rf2).read_text(encoding="utf-8")
        check(_rf2 + " 要求先过五条判定", "能力与资源" in _t2 and "铺垫" in _t2)
        check(_rf2 + " 不准替用户抄答案", "抄答案" in _t2)
    # 旁白只写背景和局面，角色细节归各自气泡
    for _rf3 in ("rules.md", "rules_standard.md", "rules_advanced.md", "rules_basic.md"):
        _t3 = (pathlib.Path("rp/prompts") / _rf3).read_text(encoding="utf-8")
        check(_rf3 + " 旁白不写角色细节", "不写任何具体角色的台词" in _t3)
        check(_rf3 + " 每人一轮只一段", "一轮只起一段" in _t3)
        check(_rf3 + " 旁白不是画外音", "画外音" in _t3)
    _nm2 = (pathlib.Path("rp/prompts") / "normal.md").read_text(encoding="utf-8")
    check("normal.md 也要先判定再写", "不是「已经成了」" in _nm2)

    # 77. 状态标记挤在正文同一行时也要剥掉
    _b77, _s77 = rp_characters._take_mark_line(
        "甲笑了。[[地点：某酒馆|时间：深夜]]说完就走")
    check("行内状态被剥掉、正文留着",
          _b77 == "甲笑了。说完就走", repr(_b77))
    check("行内状态被解析出来",
          _s77.get("location") == "某酒馆" and _s77.get("time") == "深夜",
          str(_s77))
    _b77b, _s77b = rp_characters._take_mark_line("[[地点：某酒馆|时间：深夜]]")
    check("独占一行照旧能剥",
          _b77b == "" and _s77b.get("location") == "某酒馆", repr(_b77b))
    _b77c, _s77c = rp_characters._take_mark_line("甲说[[这不是状态]]完了")
    check("不是状态键的方括号不动",
          _b77c == "甲说[[这不是状态]]完了" and _s77c == {},
          repr(_b77c))
    check("兜底清理也不误伤",
          rp_characters.strip_status_leaks("甲说[[这不是状态]]完了")
          == "甲说[[这不是状态]]完了")
    check("兜底清理行内残渣",
          rp_characters.strip_status_leaks("甲笑了。[[地点：某酒馆]]说完就走")
          == "甲笑了。说完就走")

    # 78. 重新生成要连摘要一起退，否则旧剧情还在上下文里
    _rw = rp_storage.new_session("重写测试")
    _rw["messages"] = [
        {"role": "user", "content": "甲推门。", "turn": 1},
        {"role": "assistant", "content": "乙抬头。", "turn": 1},
        {"role": "user", "content": "甲坐下。", "turn": 2},
        {"role": "assistant", "content": "乙递茶。", "turn": 2},
    ]
    _rw["summary"] = "旧摘要：甲打赢了乙。"
    _rw["compressed_upto"] = 2
    rp_api._rewind_to(_rw, 3)
    check("重写后摘要清空", _rw["summary"] == "", repr(_rw.get("summary")))
    check("重写后压缩游标归零",
          _rw["compressed_upto"] == 0, str(_rw.get("compressed_upto")))
    check("重写后消息截到重写点",
          len(_rw["messages"]) == 3, str(len(_rw["messages"])))
    check("重写后轮次跟着退", _rw["turn"] == 2, str(_rw.get("turn")))

    # 79. 对话里长出来的词条只进这一局，手动才进故事
    _L, _R = "【", "】"
    _turn = rp_characters.parse_turn(
        "他推开门。" + chr(10) + _L + "新词条" + _R
        + '{"keys":"旧钟楼,钟楼","content":"城里最高的建筑。"}')
    check("新词条被解析出来", len(_turn["lore"]) == 1, str(_turn["lore"]))
    check("标签不进正文", _turn["narrator"] == "他推开门。", repr(_turn["narrator"]))
    _book = {"lore": []}
    check("词条写进这一局", rp_characters.add_lore(_book, _turn["lore"][0]) is True)
    check("同关键词不重复加",
          rp_characters.add_lore(_book, _turn["lore"][0]) is False and len(_book["lore"]) == 1)

    story3 = client.post("/api/stories", json={}).get_json()
    sess3 = client.post("/api/sessions", json={"story_id": story3["id"]}).get_json()
    live3 = rp_storage.load_session(sess3["id"])
    live3["lore"] = [{"keys": "旧钟楼", "content": "城里最高的建筑。"}]
    rp_storage.save_session(live3)
    check("对话里的词条不动故事模板",
          not (client.get("/api/stories/" + story3["id"]).get_json().get("lore") or []))
    got3 = client.post("/api/sessions/" + sess3["id"] + "/lore/promote",
                       json={"keys": "旧钟楼"}).get_json()
    check("手动存进故事成功", got3.get("added") == 1, str(got3)[:80])
    check("故事里查得到这一条",
          any((e.get("keys") or "") == "旧钟楼"
              for e in (client.get("/api/stories/" + story3["id"]).get_json().get("lore") or [])))
    again3 = client.post("/api/sessions/" + sess3["id"] + "/lore/promote",
                         json={"all": True}).get_json()
    check("再存一次不重复加", again3.get("added") == 0, str(again3)[:80])

    # 80. 用户点名要加人：直接建卡，不等模型写【新角色】
    _s80 = {"characters": [{"name": "甲", "role": "守灯人"}]}
    _c80 = rp_characters.parse_add_cast("加入角色：乙")
    check("点名加人能认出名字", _c80 == ["乙"], str(_c80))
    check("没点名就不猜", rp_characters.parse_add_cast("你好呀") == [])
    check("带「叫」字也能剥",
          rp_characters.parse_add_cast("加个角色叫丙") == ["丙"],
          str(rp_characters.parse_add_cast("加个角色叫丙")))
    for _nm in _c80:
        rp_characters.upsert_character(_s80, {"name": _nm})
    check("点名后角色表里真有了",
          [c["name"] for c in _s80["characters"]] == ["甲", "乙"],
          str([c["name"] for c in _s80["characters"]]))

    # 81. 状态补全：独立一次调用把正文里的处境写回角色卡
    _orig_text = rp_characters.model_text
    def _fake(messages, temperature=0.8, **kw):
        return '{"changes":[{"name":"甲","state":"被按在椅子上"},' \
               '{"name":"不存在的人","state":"x"}]}'
    _s81 = {"characters": [{"name": "甲", "role": "", "state": "", "history": []}]}
    try:
        rp_characters.model_text = _fake
        _n81 = rp_characters.refresh_states(_s81, "甲被按在椅子上。")
        check("状态补全写进角色卡",
              _n81 == 1 and _s81["characters"][0]["state"] == "被按在椅子上",
              repr(_s81["characters"][0]["state"]))
        check("名单外的人不会被建卡",
              len(_s81["characters"]) == 1, str(len(_s81["characters"])))
        check("状态变化记进履历",
              _s81["characters"][0]["history"] == ["被按在椅子上"],
              str(_s81["characters"][0]["history"]))
        check("同一个状态不重复追加",
              rp_characters.refresh_states(_s81, "甲被按在椅子上。") == 0)
        def _boom(messages, temperature=0.8, **kw):
            raise RuntimeError("接口挂了")
        rp_characters.model_text = _boom
        check("模型失败不影响上一轮",
              rp_characters.refresh_states(_s81, "甲起身。") == 0)

        # 在场名单要跟着改，不能一直全员在场
        def _scene(messages, temperature=0.8, **kw):
            return ('{"changes":[],"present":["甲","不存在的人"],'
                    '"away":["乙"],"place":"某城·酒馆"}')
        _s81b = {"characters": [{"name": "甲", "state": "", "history": []},
                                {"name": "乙", "state": "", "history": []}],
                 "status": {"present": ["甲", "乙"], "away": [], "place": ""},
                 "user_name": "我"}
        rp_characters.model_text = _scene
        rp_characters.refresh_states(_s81b, "甲转身走进酒馆，乙留在街上。")
        _st81 = _s81b["status"]
        check("在场名单被更新", _st81.get("present") == ["我", "甲"], str(_st81))
        check("离场名单被更新", _st81.get("away") == ["乙"], str(_st81))
        check("地点被更新", _st81.get("location") == "某城·酒馆", str(_st81))
        check("名单外的人不会混进在场", "不存在的人" not in _st81.get("present", []))
        rp_characters.model_text = _fake
        rp_characters.refresh_states(_s81b, "甲坐下。")
        check("模型没给场景时不清空名单", _s81b["status"].get("present") == ["我", "甲"],
              str(_s81b["status"]))
    finally:
        rp_characters.model_text = _orig_text

    # 82. 续写不能新增一条：之前 add_lore 的插入把 return 吞进了 for 循环
    sid82 = client.post("/api/sessions", json={}).get_json()["id"]
    client.put("/api/sessions/" + sid82, json={"ai_name": "甲", "ai_role": "警察"})
    _live82 = rp_storage.load_session(sid82)
    _live82["messages"] = [
        {"role": "user", "content": "他推门。", "turn": 1},
        {"role": "assistant", "content": "", "narrator": "雨还在下。", "turn": 1},
    ]
    rp_storage.save_session(_live82)
    MODE["kind"] = "final"
    MODE["lore"] = True          # 这一轮还要长出一条词条
    try:
        # 必须把响应体读完：SSE 是生成器，不读就不执行，落库也不会发生
        client.post("/api/chat", json={"session_id": sid82, "continue": True}).get_data(as_text=True)
        _after82 = rp_storage.load_session(sid82)
        check("续写不新增消息",
              len(_after82["messages"]) == 2, str(len(_after82["messages"])))
        check("续写拼到原来那条上",
              _after82["messages"][-1]["role"] == "assistant", str(_after82["messages"][-1])[:60])
        check("续写的正文真拼上了",
              len(_after82["messages"][-1].get("narrator") or "") > len("雨还在下。"),
              repr((_after82["messages"][-1].get("narrator") or "")[:60]))
        check("续写那一轮也能长词条",
              any((e.get("keys") or "") == "旧钟楼" for e in (_after82.get("lore") or [])),
              str(_after82.get("lore")))
    finally:
        MODE["lore"] = False

    # 83. 思考强度要真的发出去：以前只给 ollama 发，选了云端就彻底失效
    _cap = {}
    class _FakeResp:
        status_code = 200
        text = "ok"
        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}
    _orig_post = rp_llm.requests.post
    def _fake_post(url, json=None, stream=False, headers=None, timeout=None):
        _cap.clear()
        _cap.update(json or {})
        return _FakeResp()
    rp_llm.requests.post = _fake_post
    try:
        _orig_provider = rp_config._model_state.get("provider")
        _orig_custom = rp_config._provider_overrides.get("custom1")
        rp_config._provider_overrides["custom1"] = {"label": "x", "base_url": "http://x/v1", "api_key": "k"}
        rp_config._model_state["provider"] = "custom1"
        rp_config._model_state.setdefault("model", "x")
        rp_config.save_reasoning("high", persist=False)
        rp_llm.model_text([{"role": "user", "content": "hi"}])
        check("云端也会带上思考强度", _cap.get("reasoning_effort") == "high", str(_cap.get("reasoning_effort")))
        rp_llm.model_text([{"role": "user", "content": "hi"}], reasoning="low")
        check("单次调用可以压低强度", _cap.get("reasoning_effort") == "low")
        rp_config.save_reasoning("none", persist=False)
        rp_llm.model_text([{"role": "user", "content": "hi"}])
        check("none 不发这个字段", "reasoning_effort" not in _cap, str(list(_cap)))
        rp_config.save_reasoning("high", persist=False)

        # 服务端回 400 不认这个字段：要自动去掉重发，不能把正文请求带死
        _seen = []
        class _RejectResp:
            status_code = 400
            text = '{"error":"Unrecognized request argument: reasoning_effort"}'
            def json(self):
                return {}
        def _reject_first(url, json=None, stream=False, headers=None, timeout=None):
            body = dict(json or {})
            _seen.append("reasoning_effort" in body)
            if len(_seen) == 1:
                return _RejectResp()
            return _FakeResp()
        rp_llm.requests.post = _reject_first
        rp_llm._NO_REASONING.discard("custom1")
        _r83 = rp_llm.model_text([{"role": "user", "content": "hi"}])
        check("拒绝后自动去掉字段重发", _seen == [True, False], str(_seen))
        check("降级后照常拿到正文", _r83 == "ok", repr(_r83))
        check("降级结果被记住", rp_llm.reasoning_supported() is False)
        rp_llm.model_text([{"role": "user", "content": "hi"}])
        check("下次直接不发", _seen[-1] is False, str(_seen))
        rp_llm._NO_REASONING.discard("custom1")
        rp_llm.requests.post = _fake_post

        rp_config.save_reasoning("medium", persist=False)
        if _orig_provider:
            rp_config._model_state["provider"] = _orig_provider
        if _orig_custom is None:
            rp_config._provider_overrides.pop("custom1", None)
        else:
            rp_config._provider_overrides["custom1"] = _orig_custom
    finally:
        rp_llm.requests.post = _orig_post

    # 84. 同一个角色一轮里连着写了两段：要并回去，不能占两个气泡
    _dupe = rp_characters.parse_turn(
        "【甲】一句话。\n\n【甲】又补一句。\n\n【乙】插一句。",
        "", ["甲", "乙", "丙"])
    _spk = [l["speaker"] for l in _dupe["lines"]]
    check("连着的同名段贴回一起", _spk == ["甲", "乙"], str(_spk))
    check("拆开的两句都还在",
          "一句话" in _dupe["lines"][0]["text"] and "又补一句" in _dupe["lines"][0]["text"],
          _dupe["lines"][0]["text"])

    # 85. 模型忘了写【】：最后一个人后面的叙述不能塞进他嘴里
    _tl = rp_characters.parse_turn(
        "【乙】你好。\n\n【丙】嗯。\n\n"
        "乙没接丙的话，目光扫过屋子。",
        "", ["甲", "乙", "丙"])
    check("漏标签的尾巴不跟着最后一个人", _tl["lines"][-1]["text"] == "嗯。", repr(_tl["lines"][-1]["text"]))
    check("尾巴被当作旁白保留", "乙没接丙的话" in _tl["narrator"], _tl["narrator"])
    _tl2 = rp_characters.parse_turn(
        "【乙】你好。\n\n她低下头，声音变轻：「别问了。」",
        "", ["甲", "乙", "丙"])
    check("同一人分段说话不拆", _tl2["lines"][0]["text"].count("别问了") == 1 and "你好" in _tl2["lines"][0]["text"],
          repr(_tl2["lines"][0]["text"]))

    # 86. 模型把别人的段塞进最后一个角色里：主角的、第三人的都要拆出来
    _mx = rp_characters.parse_turn(
        "【乙】她低头。\n\n甲没接话，扫了一眼。\n\n"
        "【丙心里一沉：完了。她笑了笑：「行」",
        "", ["乙", "丙"])
    _names86 = [x["speaker"] for x in _mx["lines"]]
    check("主角的段不算最后那个人的", "扫了一眼" not in _mx["lines"][0]["text"],
          _mx["lines"][0]["text"])
    check("漏写右括号也能认出是谁", _names86 == ["乙", "丙"], str(_names86))
    check("同一人不占两个气泡", len({x["speaker"] for x in _mx["lines"]}) == len(_mx["lines"]))

    # 87. 语言设置：默认中文，能切英文，提示词里真的换了
    check("默认中文", rp_config.writing().get("language") == "zh", str(rp_config.writing().get("language")))
    check("中文提示里有中文要求", "正文一律用简体中文" in rp_prompts.LANGUAGE_LINES["zh"])
    check("英文提示里有英文要求", "Write everything in English" in rp_prompts.LANGUAGE_LINES["en"])
    _lw = rp_config.save_writing({"language": "en"}, persist=False)
    check("能切到英文", rp_config.writing()["language"] == "en", str(rp_config.writing()["language"]))
    check("保存后回包也是英文", _lw.get("language") == "en", str(_lw.get("language")))
    rp_config.save_writing({"language": "zz"}, persist=False)
    check("乱写的语言不生效", rp_config.writing()["language"] == "en")
    rp_config.save_writing({"language": "zh"}, persist=False)
    check("切回中文", rp_config.writing()["language"] == "zh")

    # 故事列表不再显示主角姓名
    _sj = (pathlib.Path(__file__).parent / "static" / "js" / "sessions.js").read_text(encoding="utf-8")
    check("故事列表没有主角姓名", "story.user_name" not in _sj)
    check("故事列表还显示局数和角色数", "' 局 · '" in _sj and "character_count" in _sj)

    # 88. 角色表可以真的是空的：没配就没配，不能删了又长回来
    _z = client.post("/api/sessions", json={"mode": "rp"}).get_json()
    check("新建时没配角色就是空表", _z.get("characters") == [], str(_z.get("characters")))
    _z2 = client.put("/api/sessions/" + _z["id"], json={"characters": []}).get_json()
    check("把人全删了也能存", _z2.get("characters") == [], str(_z2.get("characters")))
    _z3 = client.get("/api/sessions/" + _z["id"]).get_json()
    check("重读不会凭空长出一个", _z3.get("characters") == [], str(_z3.get("characters")))
    check("空表时也没有 AI 名字", not (_z3.get("ai_name") or "").strip(), str(_z3.get("ai_name")))
    # 但用户自己填了名字，就该建一个
    _z4 = client.put("/api/sessions/" + _z["id"], json={"ai_name": "丙", "ai_role": "守门人"}).get_json()
    check("填了名字就建", [c["name"] for c in _z4.get("characters") or []] == ["丙"],
          str([c["name"] for c in _z4.get("characters") or []]))
    client.delete("/api/sessions/" + _z["id"])

    # 对话列表：两种模式都显示角色数与条数
    _sl = (pathlib.Path(__file__).parent / "static" / "js" / "sessions.js").read_text(encoding="utf-8")
    check("列表显示角色数", "char_count || 0) + ' 个角色'" in _sl)
    check("列表不再分模式显示不同字段",
          "s.ai_name" not in _sl.split("meta.textContent")[1].split(";")[0])

    # 顶栏按钮图标 + 文字；导入导出走中间弹窗
    _tb = _html[_html.index('class="actions"'):_html.index('</header>')]
    for _bid in ("btn-search", "btn-context", "btn-settings", "btn-compress",
                 "btn-clear", "btn-io", "btn-theme"):
        _seg = _tb[_tb.index('id="%s"' % _bid):]
        _seg = _seg[:_seg.index("</button>")]
        check(_bid + " 带文字", "<i>" in _seg and len(_seg.split("</i>")[-1].strip()) > 0,
              _seg)
    # 对话页面的设定入口：按钮在，且挂了点击
    check("对话有设定入口", 'id="btn-settings"' in _html)
    _mj = (pathlib.Path(__file__).parent / "static" / "js" / "main.js").read_text(encoding="utf-8")
    check("设定按钮已绑定", "btn-settings" in _mj and "openSettings()" in _mj)
    # 设定按钮只在对话视图出现，故事页不该有入口
    _uj = (pathlib.Path(__file__).parent / "static" / "js" / "ui.js").read_text(encoding="utf-8")
    _sj = (pathlib.Path(__file__).parent / "static" / "js" / "sessions.js").read_text(encoding="utf-8")
    check("设定按钮跟着视图隐藏",
          "settingsBtn.classList.toggle('hidden', inStories)" in _uj)
    # 用之前必须先声明。上一版把 const 删了又留下使用，
    # syncHeader 报 ReferenceError，界面误报后端不可用。
    check("设定按钮先声明后使用",
          _uj.index("const settingsBtn = $('#btn-settings')") < _uj.index("if (settingsBtn) {"))

    # 生成完必须立刻重绘。之前只在 finally 的异步回调里重绘，
    # 而那个回调还有 if (fresh) 前置条件，请求一开小差就永远不画，
    # 流式期间那个临时容器没有工具栏，于是要刷新才能看到。
    _cj = (pathlib.Path(__file__).parent / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    check("生成完立刻重绘", "if (state.session === s) renderMessages();" in _cj)
    # 只贴 DOM 不进数组的话，一重绘刚发的话就没了
    check("发消息时本地数组也要有这条",
          "s.messages.push({ role: 'user', content: text });" in _cj)
    # 编辑态退出时先摘掉输入框，否则 renderMessages 的守卫会把它自己拦下
    _mj = (pathlib.Path(__file__).parent / "static" / "js" / "msgops.js").read_text(encoding="utf-8")
    check("编辑保存先退出编辑态", "closeEdit();" + chr(10) + "      renderMessages();" in _mj)
    # 切局时 state.session 已经换了，守卫还按旧条件跳过的话，消息区会停在上一局
    check("编辑守卫只拦同一局的重绘",
          "editing.dataset.sid === ((state.session && state.session.id)" in _uj)
    check("编辑框记下所属会话", "area.dataset.sid" in _mj)
    # 编辑框要看得清 200 字：底座按宽度算，内容多了自已长，上限在 CSS 里截
    check("编辑框自底座算 200 字", "Math.ceil(200 / perLine)" in _mj)
    check("编辑框输入时自已长", "area.addEventListener('input', grow)" in _mj)
    check("编辑框关闭时卸掉 resize 监听", "window.removeEventListener('resize', grow)" in _mj)
    _cs = (pathlib.Path(__file__).parent / "static" / "css" / "app.css").read_text(encoding="utf-8")
    check("编辑框有高度上限", "max-height: 60vh" in _cs, "")
    check("编辑框不再手动拖大", "resize: none" in _cs, "")
    check("底部输入框也跟着长高", "autoGrow(input);" in _uj)
    check("编辑时这条消息占满宽度", ".msg.editing" in _cs)
    check("编辑结束撤掉占满", "classList.remove" in _mj)
    # AI 消息宽度是内容撑的，78% 只是上限；要一样宽只能实测，不能按百分比设
    check("编辑框宽度照抄 AI 回复的 min(80%, 760px)", "min(80%, 760px)" in _cs)
    check("编辑态不再被 .msg 的 78% 放开", "max-width: 100%" not in _cs.split(".msg.editing")[1].split("}")[0])
    # 之前整行替换把 padding 和圆角一起删了，输入框变直角
    _composer = _cs.split(".composer textarea")[1].split("}")[0]
    check("底部输入框有圆角", "border-radius: var(--radius)" in _composer)
    check("底部输入框有内边距", "padding: 12px 14px" in _composer)
    check("停止按钮圆角和其他控件一致", "height: 76px; border-radius: var(--radius)" in _cs)
    # 手机上 toolbar 不换行会把按钮压成竖排，整条撑到 800 多像素高
    check("手机顶部栏拆两行", ".topbar { flex-wrap: wrap;" in _cs)
    check("手机按钮组横向滚动不竖排", "overflow-x: auto" in _cs and "white-space: nowrap" in _cs)
    _sj = (pathlib.Path(__file__).parent / "static" / "js" / "sessions.js").read_text(encoding="utf-8")
    # 条目实际 76px 高加 4px 间距，按 66 算会多塞几条：溢出出了滚动条却还没到翻页
    check("每页条数按真实条目高度算", "itemHeight = 80" in _sj)
    check("算页数时扣掉标题和翻页条", "for (const el of host.children)" in _sj)
    check("静态文件不缓存", rp_config.app.config["SEND_FILE_MAX_AGE_DEFAULT"] == 0)
    # 查进度失败、任务丢失这两条路不放锁的话，封面功能会永久静默失效
    _cv = (pathlib.Path(__file__).parent / "static" / "js" / "cover.js").read_text(encoding="utf-8")
    check("封面轮询出错也要放锁", _cv.count("busy = false;") >= 10, str(_cv.count("busy = false;")))
    check("取消也要退出编辑态", "const restore = () => { closeEdit(); renderMessages(); };;" not in _mj and "closeEdit(); renderMessages();" in _mj)
    check("重绘排在网络校准之前",
          _cj.index("if (state.session === s) renderMessages();")
          < _cj.index("const fresh = await refreshCurrentSession();"))
    # 切故事只该发一次设定请求
    check("切故事不再重复请求", "await openSettings({ storyId })" not in _sj)

    check("切视图时也算一次", "setBtn.classList.toggle('hidden', storyView)" in _sj)
    check("导入导出是弹窗", 'id="io-modal"' in _html and 'id="io-menu"' not in _html)
    check("弹窗里两个操作都在",
          'id="btn-export"' in _html and 'id="btn-import-session"' in _html)
    check("弹窗有关闭按钮", 'id="io-close"' in _html)

    # 89. 用量与费用：能拿到真实 usage 就记真实的，拿不到就不猜
    _u = {}
    class _Sse:
        def iter_lines(self, decode_unicode=True):
            for line in [
                'data: {"choices":[{"delta":{"content":"你好"}}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":1200,'
                '"completion_tokens":480,"total_tokens":1680,'
                '"prompt_cache_hit_tokens":900}}',
                'data: [DONE]',
            ]:
                yield line
    _parts = list(rp_llm.iter_sse_parts(_Sse(), _u))
    check("流里能拿到用量", _u.get("prompt_tokens") == 1200, str(_u))
    check("正文结果不受影响", _parts == [("你好", "")], str(_parts))

    _cost = rp_config.estimate_cost("deepseek-chat", {
        "prompt_tokens": 1000, "completion_tokens": 500, "prompt_cache_hit_tokens": 600})
    check("费用按命中折算", _cost and abs(_cost["in"] - 0.000108) < 1e-9,
          str(_cost))
    check("未知模型不乱报价", rp_config.estimate_cost("mystery-model", {}) is None)

    rp_metrics.record({"prompt_tokens": 100, "completion_tokens": 50}, "deepseek-chat")
    _snap = rp_metrics.snapshot()
    check("面板能读到本轮用量", _snap["usage"]["prompt_tokens"] == 100, str(_snap["usage"]))
    check("面板能读到累计", _snap["total"]["calls"] >= 1, str(_snap["total"]))

    # 90. 重发回滚：消息之外的符生状态也要一起退
    _rw = {
        "turn": 3,
        "messages": [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b", "turn": 1,
             "snap": {"status": {"present": ["甲"]}, "states": {"甲": "站着"}}},
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d", "turn": 2,
             "snap": {"status": {"present": ["甲", "乙"]}, "states": {"甲": "坐下", "乙": "进门"}}},
            {"role": "user", "content": "e"},
            {"role": "assistant", "content": "f", "turn": 3,
             "snap": {"status": {"present": ["乙"]}, "states": {"甲": "离开", "乙": "守着"}}},
        ],
        "status": {"present": ["乙"]},
        "characters": [{"name": "甲", "state": "离开"}, {"name": "乙", "state": "守着"}],
        "facts": [{"turn": 0, "fact": "旧事"}, {"turn": 1, "fact": "中间事"}, {"turn": 2, "fact": "最后一件"}],
        "lore": [{"keys": "旧", "content": "x", "turn": 0}, {"keys": "新", "content": "y", "turn": 2}],
        "summary": "旧摘要",
        "compressed_upto": 5,
    }
    # 退到第二轮回复之前（index=4）
    rp_api._rewind_to(_rw, 4)
    check("回滚：消息截到重发点", len(_rw["messages"]) == 4, str(len(_rw["messages"])))
    check("回滚：轮次跟着退", _rw["turn"] == 2, str(_rw["turn"]))
    check("回滚：未来的事实清掉",
          [f["fact"] for f in _rw["facts"]] == ["旧事", "中间事"], str(_rw["facts"]))
    check("回滚：未来的词条清掉",
          [e["keys"] for e in _rw["lore"]] == ["旧"], str(_rw["lore"]))
    check("回滚：场景恢复到那一轮",
          _rw["status"].get("present") == ["甲", "乙"], str(_rw["status"]))
    check("回滚：角色状态恢复",
          [c["state"] for c in _rw["characters"]] == ["坐下", "进门"],
          str([c["state"] for c in _rw["characters"]]))
    check("回滚：摘要不留作废剧情", _rw["summary"] == "")

    # 退到开头：第一轮的事实也要没，不能因为 turn=0 就绕过去
    _rw2 = {
        "turn": 1,
        "messages": [{"role": "user", "content": "a"},
                     {"role": "assistant", "content": "b", "turn": 1}],
        "facts": [{"turn": 0, "fact": "第一轮的事"}],
        "characters": [{"name": "甲", "state": "旧状态"}],
        "status": {"present": ["甲"]},
    }
    rp_api._rewind_to(_rw2, 0)
    check("回滚到开头：事实归零", _rw2["facts"] == [], str(_rw2["facts"]))
    check("回滚到开头：场景不留残影",
          _rw2["status"] == {} and _rw2["characters"][0]["state"] == "",
          str((_rw2["status"], _rw2["characters"])))

    # 91. 产品名一处不漏：标签页、侧栏品牌、启动日志、README 标题
    _tpl = (pathlib.Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    check("标签页标题是代面", "<title>代面 · RP Chat</title>" in _tpl)
    check("侧栏品牌是代面", "🎭 代面<span class=\"ver\"" in _tpl)
    check("旧名不残留", "灵枢" not in _tpl)

    # 92. 一键启动脚本：该跳的跳，不该跳的不能跳
    _ps = pathlib.Path(__file__).parent / "start.ps1"
    check("启动脚本在根目录", _ps.exists())
    _pst = _ps.read_text(encoding="utf-8-sig")
    check("脚本用 venv", "-m venv" in _pst and ".venv" in _pst)
    check("脚本记住依赖指纹", ".requirements.hash" in _pst)
    check("指纹不依赖 Get-FileHash（部分机器没这个模块）",
          "(Get-FileHash" not in _pst and "SHA256" in _pst)
    check("启动脚本带着中文，读时要 UTF-8 BOM",
          _ps.read_bytes()[:3] == b"\xef\xbb\xbf")
    check("启动脚本不传 py -3",
          "'-3'" not in _pst)

    # 93. 安全基线：跨站、Host、体积、CVE 下限
    _api = (pathlib.Path(__file__).parent / "rp" / "api.py").read_text(encoding="utf-8")
    _cfg = (pathlib.Path(__file__).parent / "rp" / "config.py").read_text(encoding="utf-8")
    _req = (pathlib.Path(__file__).parent / "requirements.txt").read_text(encoding="utf-8")
    check("写请求有跨站守卫", "before_request" in _api and "拒绝跨站请求" in _api)
    check("Host 白名单已配", "TRUSTED_HOSTS" in _cfg)
    check("请求体有上限", "MAX_CONTENT_LENGTH" in _cfg)
    check("安全响应头已注入", "X-Content-Type-Options" in _api)
    check("urllib3 下限跟上 CVE", "urllib3>=2.6.3" in _req)
    # 光看 requirements 是假绿：声明改了、环境没动，自检也会过。
    # CVE-2026-21441 的触发条件就在 llm.py 的 stream=True 里。
    import urllib3 as _urllib3
    _uv = tuple(int(x) for x in _urllib3.__version__.split(".")[:3])
    check("urllib3 实装版本已过 CVE 线", _uv >= (2, 6, 3), _urllib3.__version__)
    check("入参不再直接 .strip()", '(data.get("message") or "").strip()' not in _api)
    check("日志不写剧情原文", "raw=answer[" not in _api)

    # 地点只走 location：写进 place 等于丢到无人读的键
    _ch = (pathlib.Path(__file__).parent / "rp" / "characters.py").read_text(encoding="utf-8")
    check("地点统一写 location", 'st["place"]' not in _ch and 'st["location"] = place' in _ch)

    # 文档与实现的路由数对得上
    _doc = (pathlib.Path(__file__).parent / "docs" / "DATA.md").read_text(encoding="utf-8")
    _routes = len([1 for ln in _api.split(chr(10))
                   if ln.startswith(("@app.get", "@app.post", "@app.put", "@app.delete"))])
    _rows = len([1 for ln in _doc.split(chr(10))
                 if ln.startswith("    GET ") or ln.startswith("    POST ")
                 or ln.startswith("    PUT ") or ln.startswith("    DELETE ")])
    check("路由表与实现同数", _routes == _rows, "%d vs %d" % (_routes, _rows))
    check("SSE 文档写对字段名", '"content": "正文增量"' in _doc)

    # 94. 前端模块：调用了但从未定义的函数。
    # syncThemeSeg 就是这么漏的：三处调用、零处定义，而它在
    # openPrefs() 里，抛错位置恰好在 remove('hidden') 之前——设置弹窗永远开不了。
    # node --check 只看语法，看不出这个。
    import re as _re
    _js_dir = pathlib.Path(__file__).parent / "static" / "js"
    _keywords = {
        "if", "for", "while", "switch", "catch", "return", "typeof", "function", "parseInt",
        "parseFloat", "isNaN", "String", "Number", "Boolean", "Array", "Object", "JSON",
        "Math", "Date", "Promise", "Map", "Set", "console", "fetch", "setTimeout",
        "clearTimeout", "setInterval", "clearInterval", "requestAnimationFrame",
        "encodeURIComponent", "decodeURIComponent", "alert", "confirm", "await", "new",
        "super", "do", "else", "try", "throw", "delete", "void", "in", "of", "case",
        "async", "import", "export", "default", "from", "class", "extends", "yield", "this",
    }
    for _p in sorted(_js_dir.glob("*.js")):
        _src = _p.read_text(encoding="utf-8")
        _def = set(_keywords)
        for _m in _re.finditer(r"(?:^|\n)\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)", _src):
            _def.add(_m.group(1))
        for _m in _re.finditer(r"(?:^|\n)\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=", _src):
            _def.add(_m.group(1))
        for _m in _re.finditer(r"import\s*\{([^}]+)\}\s*from", _src):
            for _part in _m.group(1).split(","):
                _n = _part.strip().split(" as ").pop().strip()
                if _n:
                    _def.add(_n)
        for _m in _re.finditer(r"function\s*\(([^)]*)\)", _src):
            for _arg in _m.group(1).split(","):
                _n = _arg.strip().split("=")[0].strip()
                if _n:
                    _def.add(_n)
        for _m in _re.finditer(r"\(\s*([A-Za-z0-9_$,\s]*)\s*\)\s*=>", _src):
            for _arg in _m.group(1).split(","):
                _n = _arg.strip()
                if _n:
                    _def.add(_n)
        for _m in _re.finditer(r"catch\s*\(\s*([A-Za-z0-9_$]+)", _src):
            _def.add(_m.group(1))
        _called = set(_re.findall(r"(?:^|[^.A-Za-z0-9_$])([a-z][A-Za-z0-9_$]*)\s*\(", _src))
        # 前三个是方法名或解构回调参数，不是未定义函数
        _skip = {"constructor", "onEvent", "onStop", "onGo", "onChange", "close", "remove", "syncHeader"}
        _miss = sorted(_n for _n in _called if _n not in _def and _n not in _skip)
        check("前端无未定义调用：" + _p.name, not _miss, ", ".join(_miss))

    # 摘要字数跟档位走：它要被压掉的量约束，不能是固定值
    for _lv in ("basic", "standard", "advanced"):
        _lim = rp_config.TIER_LIMITS[_lv]
        check("档位 %s 定义了摘要字数" % _lv, "summary" in _lim, str(_lim.get("summary")))
    _sz = [rp_config.TIER_LIMITS[k]["summary"] for k in ("basic", "standard", "advanced")]
    check("摘要字数随档位递增", _sz == sorted(_sz) and len(set(_sz)) == 3, str(_sz))
    check("默认摘要字数与初级档一致",
          rp_config.WRITING_DEFAULTS["summary_chars"] == rp_config.TIER_LIMITS["basic"]["summary"])
    # 5% 是经验值：3% 在高级档上是 800 字压三万，细节掉得太多；
    # 大了又挤占每轮预算（摘要是每轮都注入的）。
    for _lv in ("basic", "standard", "advanced"):
        _lim = rp_config.TIER_LIMITS[_lv]
        _ratio = _lim["summary"] / _lim["context"]
        check("档位 %s 摘要占上下文 5%%" % _lv,
              abs(_ratio - 0.05) < 0.005, "%.3f" % _ratio)
        check("档位 %s 摘要在合法范围内" % _lv,
              rp_config.WRITING_BOUNDS["summary_chars"][0] <= _lim["summary"]
              <= rp_config.WRITING_BOUNDS["summary_chars"][1], str(_lim["summary"]))

    # 95. busy 要能被读到：界面刷新后靠它分辨"还在写"还是"没答上"
    _bs = client.post("/api/sessions", json={}).get_json()
    _bsid = _bs["id"]
    check("平时 busy 是假", client.get("/api/sessions/" + _bsid).get_json().get("busy") is False)
    rp_storage.try_acquire_busy(_bsid)
    check("生成中 busy 是真", client.get("/api/sessions/" + _bsid).get_json().get("busy") is True)
    check("生成中不让回滚快照",
          client.post("/api/sessions/" + _bsid + "/snapshots/x/restore").status_code == 409)
    rp_storage.release_busy(_bsid)
    check("写完 busy 变回假", client.get("/api/sessions/" + _bsid).get_json().get("busy") is False)
    check("busy 不落盘", "busy" not in (rp_storage.load_session(_bsid) or {}))
    client.delete("/api/sessions/" + _bsid)

    # 96. 封面：路径靠 BASE_DIR 推、可撤销、拒绝路径穿越
    from rp import covers as _cov
    check("封面目录在数据目录下", _cov.COVERS_DIR.startswith(rp_config.DATA_DIR))
    # "算出来的"就是：它必须等于 DATA_DIR 拼 covers。写死盘符的话，
    # 换机器或换 LT_DATA_DIR 后就会指到一个不存在的地方。
    check("封面路径由数据目录推出",
          _cov.COVERS_DIR == os.path.join(rp_config.DATA_DIR, "covers"))
    check("封面名拒绝路径穿越", _cov.exists("../settings.json") is False)
    check("封面名拒绝空串", _cov.exists("") is False)

    _st = rp_stories.new_story("测试")
    check("新故事带封面字段", _st.get("cover") == "" and _st.get("cover_history") == [])
    rp_stories.set_cover(_st, "a.png", "p1")
    rp_stories.set_cover(_st, "b.png", "p2")
    check("换封面时旧图进历史", [h["name"] for h in _st["cover_history"]] == ["a.png"])
    check("封面提示词随图存", _st.get("cover_prompt") == "p2")
    rp_stories.undo_cover(_st)
    check("撤销回到上一张", _st.get("cover") == "a.png")
    check("撤销连提示词一起还原", _st.get("cover_prompt") == "p1")
    rp_stories.undo_cover(_st)
    check("没历史时撤销清空而不报错", _st.get("cover") == "")

    _blank = rp_stories.new_story("")
    check("空故事拼不出提示词", rp_stories.cover_prompt_from(_blank) == "")
    _rich = rp_stories.new_story("雨夜")
    _rich["world"] = "一座下了三天雨的小城。"
    _rich["characters"] = [{"name": "甲", "role": "摆渡人"}]
    _pp = rp_stories.cover_prompt_from(_rich)
    check("有设定时拼得出提示词", _pp and "甲" in _pp)
    check("提示词不会拼出句号加顿号", "。、" not in _pp)
    check("封面总长有上限", len(_pp) < 320, str(len(_pp)))

    # 97. 生图不能阻塞请求：开一张图要几十秒，路由里等它就是超时
    check("封面生图有后台任务入口", hasattr(rp_covers, "start_job"))
    check("封面进度可查", hasattr(rp_covers, "job_of"))
    check("未知任务返回 None", rp_covers.job_of("no-such-story") is None)
    check("生图超时默认不低于 120 秒", rp_covers.GEN_TIMEOUT >= 120, str(rp_covers.GEN_TIMEOUT))
    check("生图超时可配置",
          "LT_COVER_TIMEOUT" in (pathlib.Path(__file__).parent / "rp" / "covers.py").read_text(encoding="utf-8"))
    check("进度接口已公开", "/cover/job" in _api)

    # 98. 删故事要连带删它的封面，否则 covers 里全是孤儿图
    _ds = rp_stories.new_story("删图测试")
    rp_stories.save_story(_ds)
    _dn1, _dn2 = "zz_chk_live.jpg", "zz_chk_hist.jpg"
    for _n in (_dn1, _dn2):
        (_cov.COVERS_DIR and open(os.path.join(_cov.COVERS_DIR, _n), "wb").write(b"x"))
    rp_stories.set_cover(_ds, _dn1, "p1")
    rp_stories.set_cover(_ds, _dn2, "p2")
    rp_stories.save_story(_ds)
    check("删前两张图都在", _cov.exists(_dn1) and _cov.exists(_dn2))
    rp_stories.delete_story(_ds["id"])
    check("删故事后当前封面也没了", not _cov.exists(_dn2))
    check("删故事后历史封面也没了", not _cov.exists(_dn1))

    # 撤销不能只弹栈：被撤掉的那张要从磁盘上消失，否则越撤越多孤儿图
    _us = rp_stories.new_story("撤销测试")
    rp_stories.save_story(_us)
    _un = ["zz_undo_1.jpg", "zz_undo_2.jpg"]
    for _n in _un:
        open(os.path.join(_cov.COVERS_DIR, _n), "wb").write(b"x")
        rp_stories.set_cover(_us, _n, "p")
        rp_stories.save_story(_us)
    check("两张都在盘上", all(_cov.exists(_n) for _n in _un))
    rp_stories.undo_cover(_us)
    check("撤一步删一张", not _cov.exists(_un[1]) and _cov.exists(_un[0]))
    rp_stories.undo_cover(_us)
    check("撤到底也清干净", not _cov.exists(_un[0]))
    rp_stories.delete_story(_us["id"])

    # 文件被手删了也不能把坏引用回给前端
    check("不存在的封面当没封面", rp_stories._live_cover("nope.jpg") == "")
    check("真存在的封面照旧返回", rp_stories._live_cover(_dn1) == "")

    # 99. 缩略图要真的是另一张小图，不能拿原图当缩略图
    check("缩略图目录在封面目录下", _cov.THUMBS_DIR.startswith(_cov.COVERS_DIR))
    check("缩略图尺寸不超过屏幕需求的三倍", _cov.THUMB_SIZE <= 200, str(_cov.THUMB_SIZE))
    _tn = "zz_thumb_src.png"
    _tp = os.path.join(_cov.COVERS_DIR, _tn)
    try:
        from PIL import Image
        Image.new("RGB", (832, 1216), (30, 40, 60)).save(_tp, "PNG")
        _okimg = True
    except Exception:
        _okimg = False
    if _okimg:
        _full_bytes = os.path.getsize(_tp)
        check("能生成缩略图", _cov.make_thumb(_tn))
        _th = _cov.thumb_of(_tn)
        check("缩略图是 jpeg", _th.endswith(".jpg"), _th)
        _thb = os.path.getsize(_cov.thumb_path(_th))
        check("缩略图明显比原图小", _thb * 5 < _full_bytes, "%d vs %d" % (_thb, _full_bytes))
        with Image.open(_cov.thumb_path(_th)) as _im:
            check("缩略图真的被缩了", max(_im.size) <= _cov.THUMB_SIZE, str(_im.size))
        check("缩略图地址形状对", _cov.url_thumb(_tn).startswith("/covers/thumbs/"))
        check("不存在的缩略图不放行", _cov.is_thumb("nope.jpg") is False)
        check("缩略图名拒绝路径", _cov.is_thumb("../x.jpg") is False)
        _cov.drop(_tn)
        check("删原图连带删缩略图", not _cov.is_thumb(_th) and not _cov.exists(_tn))

    # 100. 封面管理：列出来、选回去、删得掉
    _ms = rp_stories.new_story("管理测试")
    rp_stories.save_story(_ms)
    _mn = ["zz_m1.jpg", "zz_m2.jpg", "zz_m3.jpg"]
    for _n in _mn:
        open(os.path.join(_cov.COVERS_DIR, _n), "wb").write(b"x")
        rp_stories.set_cover(_ms, _n, "p_" + _n)
    check("管理列表带当前那张", [i["name"] for i in rp_stories.cover_list(_ms)][0] == _mn[2])
    check("管理列表长度对", len(rp_stories.cover_list(_ms)) == 3)
    rp_stories.pick_cover(_ms, _mn[0])
    check("选回去就变当前", _ms["cover"] == _mn[0])
    check("选回去不丢原来那张", _mn[2] in [h["name"] for h in _ms["cover_history"]])
    rp_stories.forget_cover(_ms, _mn[1])
    check("删掉就不在列表里", _mn[1] not in [i["name"] for i in rp_stories.cover_list(_ms)])
    check("删掉连图一起没", not _cov.exists(_mn[1]))
    rp_stories.forget_cover(_ms, _ms["cover"])
    check("删掉当前就顶上一张", _ms["cover"] == _mn[2], _ms["cover"])
    rp_stories.delete_story(_ms["id"])

    # 101. 一次生成只能落一份图：ComfyUI 写在哪就用哪张，不再拄一份
    check("生图文件前缀已固定", isinstance(_cov.SAVE_PREFIX, str) and _cov.SAVE_PREFIX)
    check("custom_path 指向封面目录",
          '"custom_path"] = COVERS_DIR' in (pathlib.Path(__file__).parent / "rp" / "covers.py").read_text(encoding="utf-8"))
    _pf = _cov.SAVE_PREFIX + "_zz_probe.jpg"
    _pp = os.path.join(_cov.COVERS_DIR, _pf)
    open(_pp, "wb").write(b"x")
    check("能认出 ComfyUI 已写好的那张", _cov._written({"filename": _pf}) == _pp)
    check("名字对不上也能按前缀找到", _cov._written({"filename": "wrong.jpg"}) == _pp)
    os.unlink(_pp)
    check("真没图时不乱拿", _cov._written({"filename": "nope.jpg"}) is None)

    shutil.rmtree(TMP_DIR, ignore_errors=True)
    if FAILED:
        print("")
        print("%d 项失败" % len(FAILED))
        return 1
    print("")
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
