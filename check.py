# -*- coding: utf-8 -*-
"""离线自检：不访问 Ollama / 云端，只验证存储、统计与配置的核心不变量。

用法：python check.py
"""
import json
import os
import shutil
import tempfile
import zipfile

TMP_DIR = tempfile.mkdtemp(prefix="lt-check-")
os.environ["LT_DATA_DIR"] = TMP_DIR

import lt  # noqa: E402  必须在设置数据目录之后导入
import rp.api as rp_api  # noqa: E402
import rp.characters as rp_characters  # noqa: E402
import rp.config as rp_config  # noqa: E402
import rp.context as rp_context  # noqa: E402
import rp.prompts as rp_prompts  # noqa: E402
import rp.storage as rp_storage  # noqa: E402
import rp.stories as rp_stories  # noqa: E402
import rp.snapshots as rp_snapshots  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" :: " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


MODE = {"kind": "error"}
TURN_TEXT = (
    "【旁白】雨还在下，走廊的灯闪了一下。\n"
    "【沈砚】他把伞收起来，靠在门边。\n"
    "【周惠兰】这是抢话，必须被丢掉。\n"
    '【新角色】{"name":"周惠兰","role":"房东，耳朵很尖","relation":"住在楼下"}\n'
    '【变化】{"name":"沈砚","change":"开始怀疑有人跟踪"}'
)


def fake_completion_events(messages, base_temp=0.8, max_attempts=3, min_chars=0,
                           extra=None, validate=None, with_reasoning=False, **kw):
    if MODE["kind"] == "final":
        yield {"type": "final", "text": TURN_TEXT, "attempts": 1}
        return
    yield {"type": "error", "attempts": 1, "message": "离线自检不调用模型", "raw": ""}


def main():
    # 路由模块用的是自己命名空间里的名字，patch 在这一层才生效
    rp_api.completion_events = fake_completion_events
    lt.completion_events = fake_completion_events
    client = lt.app.test_client()

    # 0. 模式与可配参数
    check("默认是一般模式", rp_config.mode_of() == "normal", rp_config.mode_of())
    check("默认角色上限是 4 个",
          rp_config.writing()["max_characters"] == 4, rp_config.writing())
    rp_config.save_writing({"max_characters": 99, "line_min": 5, "keep_recent": 999},
                           persist=False)
    clamped = rp_config.writing()
    check("写作参数越界被夹紧",
          clamped["max_characters"] == 8 and clamped["line_min"] == 40
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
        "ai_name": "沈砚", "ai_role": "沉默的警察",
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
    parsed = lt.parse_turn(TURN_TEXT, "沈砚")
    check("旁白与台词分开", parsed["narrator"].startswith("雨还在下")
          and parsed["line"].startswith("他把伞"), parsed)
    check("越权发言被丢弃", "抢话" not in parsed["line"], parsed["line"])
    check("新角色与变化被解析",
          parsed["new_character"]["name"] == "周惠兰"
          and parsed["updates"][0]["name"] == "沈砚", parsed)
    check("无标记输出整段归给发言人",
          lt.parse_turn("他点了点头。", "沈砚")["line"] == "他点了点头。")

    story = client.post("/api/sessions", json={}).get_json()
    story_sid = story["id"]
    client.put("/api/sessions/" + story_sid, json={"ai_name": "沈砚", "ai_role": "沉默的警察"})
    MODE["kind"] = "final"

    client.post("/api/chat", json={"session_id": story_sid, "message": "我回来了"}).get_data(as_text=True)
    after_turn = client.get("/api/sessions/" + story_sid).get_json()
    last = after_turn["messages"][-1]
    check("落盘的助手消息带说话人", last["speaker"] == "沈砚", last)
    check("落盘的助手消息带旁白", last.get("narrator", "").startswith("雨还在下"), last)
    check("落盘内容不含抢话段落", "抢话" not in last["content"], last["content"])
    names = [c["name"] for c in after_turn["characters"]]
    check("新角色已加入角色表", "周惠兰" in names, names)
    shen = [c for c in after_turn["characters"] if c["name"] == "沈砚"][0]
    check("角色设定随进展更新", shen["state"] == "开始怀疑有人跟踪", shen)
    check("回合数推进", after_turn["turn"] == 1, after_turn["turn"])

    # 用户对角色表有最终决定权
    edited = client.put("/api/sessions/" + story_sid, json={
        "characters": [
            {"name": "沈砚", "role": "刑警"},
            {"name": "周惠兰", "role": "房东"},
            {"name": "阿岚", "role": "线人"},
        ]
    }).get_json()
    check("用户可以新增角色", [c["name"] for c in edited["characters"]]
          == ["沈砚", "周惠兰", "阿岚"], edited["characters"])

    removed = client.put("/api/sessions/" + story_sid, json={
        "characters": [{"name": "沈砚", "role": "刑警"}]
    }).get_json()
    check("用户可以删除角色",
          [c["name"] for c in removed["characters"]] == ["沈砚"], removed["characters"])

    kept = client.put("/api/sessions/" + story_sid, json={
        "characters": [{"name": "沈砚", "role": "刑警，旧城区长大"}]
    }).get_json()
    check("用户改的人设被保留",
          kept["characters"][0]["role"] == "刑警，旧城区长大", kept["characters"])

    # AI 引入同名角色时，不能覆盖用户写的人设
    lt.upsert_character(kept, {"name": "沈砚", "role": "其实是鬼", "change": "伤了一条腿"})
    check("AI 不会改写用户的人设",
          kept["characters"][0]["role"] == "刑警，旧城区长大", kept["characters"][0])
    check("AI 只推进状态",
          kept["characters"][0]["state"] == "伤了一条腿", kept["characters"][0])

    # 第二回合必须换人说话
    speaker2 = lt.next_speaker(after_turn)
    check("第二回合轮到另一个角色", speaker2 == "周惠兰", speaker2)
    check("角色数不超过上限", len(after_turn["characters"]) <= lt.MAX_CHARACTERS)

    # 8. 模式切换：一般模式由系统叙述，不分配发言人
    rp_config.save_mode("normal", persist=False)
    plain = client.post("/api/sessions", json={}).get_json()
    plain_sid = plain["id"]
    client.put("/api/sessions/" + plain_sid, json={
        "ai_name": "沈砚", "ai_role": "沉默的警察", "world": "雨夜小城",
        "characters": [{"name": "沈砚", "role": "沉默的警察"},
                       {"name": "周惠兰", "role": "房东"}],
    })
    plain_sess = client.get("/api/sessions/" + plain_sid).get_json()
    check("一般模式没有发言人",
          rp_characters.next_speaker(plain_sess) == "",
          rp_characters.next_speaker(plain_sess))
    turn = rp_characters.parse_turn("雨还在下。\n【沈砚】他把伞收起来。", "")
    check("一般模式整段算叙述",
          bool(turn["narrator"]) and not turn["line"], turn)

    MODE["kind"] = "final"
    plain_resp = client.post("/api/chat", json={"session_id": plain_sid, "message": "我推门进去。"})
    plain_body = plain_resp.get_data(as_text=True)   # 流式响应必须读完才会真正生成
    psess = client.get("/api/sessions/" + plain_sid).get_json()
    last_plain = psess["messages"][-1]
    check("一般模式落盘为系统消息",
          last_plain["role"] == "assistant" and not last_plain.get("speaker")
          and bool(last_plain.get("narrator")),
          (last_plain, plain_body[:160]))

    rules_plain = rp_prompts.build_rules("林晚", "normal", "沈砚、周惠兰")
    check("一般模式提示词是叙述者口吻",
          "叙述者" in rules_plain and "本回合发言人" not in rules_plain,
          rules_plain[:80])
    rules_rp = rp_prompts.build_rules("林晚", "rp", "沈砚")
    check("角色扮演提示词要求轮流发言",
          "本回合发言人" in rules_rp or "被指定的那一个角色" in rules_rp,
          rules_rp[:80])
    rp_config.save_mode("rp", persist=False)

    # 9. 模式固定在会话上；故事能复用开多局
    rp_config.save_mode("normal", persist=False)
    fresh = client.post("/api/sessions", json={}).get_json()
    check("新对话带自己的模式", fresh["mode"] == "normal", fresh.get("mode"))
    check("新对话自动绑定一个故事", bool(fresh.get("story_id")), fresh.get("story_id"))

    saved = client.put("/api/sessions/" + fresh["id"], json={
        "title": "雨夜旧城", "user_name": "林晚", "user_role": "记者",
        "world": "雨夜", "opening": "雨落下来。",
        "characters": [{"name": "沈砚", "role": "守灯人"},
                       {"name": "周惠兰", "role": "房东"}],
        "mode": "rp",
    }).get_json()
    check("模式改为扮演后固定在这个对话",
          saved["mode"] == "rp" and rp_characters.next_speaker(saved) == "沈砚",
          saved.get("mode"))
    check("第一个角色就是 AI 角色",
          saved["ai_name"] == "沈砚" and saved["ai_role"] == "守灯人",
          (saved.get("ai_name"), saved.get("ai_role")))

    second = client.post("/api/sessions", json={"story_id": saved["story_id"]}).get_json()
    check("同一故事开新局沿用设定",
          [c["name"] for c in second["characters"]] == ["沈砚", "周惠兰"]
          and second["world"] == "雨夜" and second["user_name"] == "林晚",
          second.get("characters"))
    check("新局只带开场，不带旧对话",
          [m["role"] for m in second["messages"]] == ["assistant"],
          second.get("messages"))
    check("新局用自己的模式", second["mode"] == "normal", second.get("mode"))
    stories = client.get("/api/stories").get_json()
    mine = [s for s in stories if s["id"] == saved["story_id"]]
    check("故事列表记录开过几局", bool(mine) and mine[0]["runs"] == 2, stories)

    # 10. 状态面板：剥离、解析、注入
    status_text = ('【旁白】雨还在下。\n【沈砚】他收起伞。\n'
                   '<StatusBlock>{"location":"酒馆","time":"深夜",'
                   '"relations":"互相试探","items":["伞","旧钥匙"],'
                   '"goal":"问清来意"}</StatusBlock>')
    st_turn = rp_characters.parse_turn(status_text, "沈砚")
    check("状态块从正文里剥离",
          "StatusBlock" not in st_turn["line"]
          and "StatusBlock" not in st_turn["narrator"], st_turn)
    check("状态块解析成结构化数据",
          st_turn["status"]["location"] == "酒馆"
          and st_turn["status"]["items"] == "伞，旧钥匙"
          and st_turn["status"]["goal"] == "问清来意", st_turn["status"])

    with_status = dict(saved)
    with_status["status"] = st_turn["status"]
    prompt = rp_prompts.build_system_prompt(with_status, "沈砚")
    check("上一轮状态注入下一轮提示词",
          "【当前状态" in prompt and "问清来意" in prompt, prompt[-160:])

    # 11. 消息控制权：重写、就地编辑、分叉
    edit_sess = client.post("/api/sessions", json={}).get_json()
    esid = edit_sess["id"]
    raw = rp_storage.load_session(esid)
    raw["messages"] = [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "第一答", "speaker": "沈砚"},
        {"role": "user", "content": "第二句"},
        {"role": "assistant", "content": "第二答", "speaker": "沈砚"},
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
        {"keys": "银月酒馆,酒馆", "content": "银月酒馆的老板只收旧币。"},
        {"keys": "遗迹", "content": "遗迹在城北，夜里会浮起来。"},
    ]
    lore_sess["messages"] = [{"role": "user", "content": "我推开酒馆的门"}]
    hit_msgs = rp_prompts.build_messages(lore_sess, "沈砚")
    check("提到关键词才注入世界书",
          "银月酒馆的老板只收旧币" in hit_msgs[0]["content"]
          and "遗迹在城北" not in hit_msgs[0]["content"],
          hit_msgs[0]["content"][:80])
    lore_sess["messages"] = [{"role": "user", "content": "外面还在下雨"}]
    calm_msgs = rp_prompts.build_messages(lore_sess, "沈砚")
    check("没提到就不塞世界书",
          all("银月酒馆的老板只收旧币" not in (m.get("content") or "") for m in calm_msgs),
          len(calm_msgs))

    saved_lore = client.put("/api/sessions/" + fresh["id"], json={
        "lore": [{"keys": "酒馆", "content": "老板只收旧币。"}],
    }).get_json()
    check("世界书会随设定保存", saved_lore["lore"][0]["content"] == "老板只收旧币。",
          saved_lore.get("lore"))
    story_after = client.get("/api/sessions/" + fresh["id"]).get_json()["story"]
    check("世界书同步进故事模板",
          (story_after or {}).get("lore") and story_after["lore"][0]["keys"] == "酒馆",
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
        "character", name="沈砚", user_role="夜班护士",
        characters=[{"name": "沈砚", "role": "跑网约车的"},
                    {"name": "周惠兰", "role": "房东"}])
    check("角色卡要求照着名字写并给出字数",
          "沈砚" in named and "120~200" in named)
    check("其它角色一并列出", "周惠兰" in named and "必须原样保留" in named)

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
        {"name": "沈砚", "role": "守灯人，话少"},
        {"name": "周惠兰", "role": "房东，爱管闲事"},
    ]
    normal_prompt = rp_prompts.build_system_prompt(normal_sess, "")
    check("一般模式也拿到完整角色卡",
          "守灯人，话少" in normal_prompt and "房东，爱管闲事" in normal_prompt)
    check("一般模式不指定发言人", "本回合发言人" not in normal_prompt)
    check("一般模式要求按人设写所有角色",
          "角色按各自的人设" in normal_prompt and "不必轮流" in normal_prompt)

    rp_sess = dict(normal_sess, mode="rp")
    rp_prompt = rp_prompts.build_system_prompt(rp_sess, "周惠兰")
    check("两种模式读的是同一份角色表",
          "房东，爱管闲事" in rp_prompt and "本回合发言人" in rp_prompt)

    # 15. 思考强度（Ollama 的 reasoning_effort）
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
             "林晚，27 岁，赛博朋克城市的夜班护士，习惯把疲惫藏在利落的手脚里。\n\n"
             "林晚今年 27 岁，是赛博朋克城市的夜班护士，习惯把疲惫藏在利落的手脚里。\n\n"
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
        "title": "雨夜旧城", "user_name": "林晚", "world": "旧城雨季。",
        "characters": [{"name": "沈砚", "role": "守灯人"}],
    }).get_json()
    check("故事设定能单独保存",
          saved.get("world") == "旧城雨季。" and saved["characters"][0]["name"] == "沈砚",
          str(saved)[:60])
    check("故事自己派生 AI 角色名", saved.get("ai_name") == "沈砚", saved.get("ai_name"))
    check("故事改动已落盘",
          client.get("/api/stories/" + story["id"]).get_json().get("user_name") == "林晚")
    check("删除故事返回成功",
          client.delete("/api/stories/" + story["id"]).get_json().get("ok") is True)

    # 18. 对话里新出现的角色回流成故事的长期角色
    story2 = client.post("/api/stories", json={}).get_json()
    sess2 = client.post("/api/sessions", json={"story_id": story2["id"]}).get_json()
    live = rp_storage.load_session(sess2["id"])
    live["characters"] = [{"name": "沈砚", "role": "守灯人", "state": "受伤", "history": ["第 3 幕"]}]
    added = rp_stories.promote_characters(live, 4)
    check("新角色会回流进故事", added == ["沈砚"], str(added))
    saved_story = client.get("/api/stories/" + story2["id"]).get_json()
    cards = saved_story.get("characters") or []
    shen = next((c for c in cards if c.get("name") == "沈砚"), None)
    check("故事里能查到长期角色", shen is not None, str(cards)[:80])
    check("回流不带这一局的状态",
          shen is not None and shen["state"] == "" and shen["history"] == [])
    check("再回流一次不会重复",
          rp_stories.promote_characters(live, 4) == [])

    # 19. 人设锚定：聊久了把角色核心重插到近处
    anchor_sess = rp_storage.new_session("锚定测试")
    anchor_sess["characters"] = [{"name": "沈砚", "role": "守灯人，话少，不主动开口",
                                  "state": "", "history": []}]
    anchor_sess["turn"] = 15
    msgs = rp_prompts.build_messages(anchor_sess)
    check("第 15 轮会重插人设锚定",
          any("人设锚定" in m["content"] for m in msgs), str(len(msgs)))
    anchor_sess["turn"] = 14
    check("非整数轮不插锚定",
          not any("人设锚定" in m["content"] for m in rp_prompts.build_messages(anchor_sess)))
    anchored_msgs = rp_prompts.build_messages({**anchor_sess, "turn": 15})
    check("锚定贴在最近几轮之前",
          [i for i, m in enumerate(anchored_msgs) if "人设锚定" in m["content"]][0] > 0)

    # 20. 上下文检查器：能看见这一轮发了什么
    dbg_sess = rp_storage.new_session("检查器测试")
    dbg_sess["user_role"] = "夜班护士"
    dbg_sess["characters"] = [{"name": "沈砚", "role": "守灯人", "state": "", "history": []}]
    dbg_sess["lore"] = [{"keys": "银月酒馆", "content": "城南的酒馆，老板欠沈砚一条命。"}]
    dbg_sess["messages"] = [{"role": "user", "content": "去银月酒馆看看"}]
    dbg = rp_prompts.build_context_debug(dbg_sess)
    check("检查器给出 system 全文", "旁白与扮演规则" in dbg["system"])
    check("检查器标出命中的世界书",
          bool(dbg["lore"]) and dbg["lore"][0]["keys"] == "银月酒馆", str(dbg["lore"])[:50])
    check("检查器给出 token 估算", dbg["tokens"] > 0)
    check("上下文接口带 debug", "debug" in rp_context.context_info(dbg_sess))

    # 21. 事实账本：旧说法让位，不双写
    book_sess = {"turn": 3}
    rp_characters.merge_facts(book_sess, ["沈砚拿走了钥匙"], turn=3)
    rp_characters.merge_facts(book_sess, ["钥匙被沈砚拿走了"], turn=4)
    live = rp_characters.active_facts(book_sess)
    check("账本只留最新一条", len(live) == 1 and live[0]["turn"] == 4, str(live))
    check("旧事实标记为已失效",
          any(f["state"] == "resolved" for f in book_sess["facts"]))
    check("账本写进 system prompt",
          "已经定下的事" in rp_prompts.build_system_prompt(book_sess))
    parsed = rp_characters.parse_turn(
        '<StatusBlock>{"facts": ["两人约好三天后见"]}</StatusBlock>【沈砚】嗯。', "沈砚")
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
    saved = rp_config.save_backup_conf({"enabled": True, "every_hours": 6})
    check("定时备份能开启", saved["enabled"] is True and saved["every_hours"] == 6, str(saved))
    check("间隔超出范围会被夹住",
          rp_config.save_backup_conf({"every_hours": 99999})["every_hours"] <= 720)
    rp_config.save_backup_conf({"enabled": False, "every_hours": 24})
    now = 1_700_000_000
    check("关着就不该跑", rp_backup.due({"enabled": False, "every_hours": 1}, 0, now) is False)
    check("开着且到期才跑",
          rp_backup.due({"enabled": True, "every_hours": 2}, now - 3 * 3600, now) is True)
    check("没到期不重复跑",
          rp_backup.due({"enabled": True, "every_hours": 2}, now - 3600, now) is False)

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
