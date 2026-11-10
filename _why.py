# -*- coding: utf-8 -*-
import json, glob, os

files = [(os.path.getmtime(p), p) for p in glob.glob("data/*.json") if not p.endswith("settings.json")]
files.sort(reverse=True)
st = json.load(open("data/settings.json", encoding="utf-8")) if os.path.exists("data/settings.json") else {}
w = st.get("writing") or {}
print("设置里的角色上限:", w.get("max_characters"))
for mt, p in files:
    s = json.load(open(p, encoding="utf-8"))
    msgs = s.get("messages") or []
    print("")
    print("=== %s | 角色 %d 个: %s" % ((s.get("title") or "?")[:24], len(s.get("characters") or []),
                                     [c.get("name") for c in (s.get("characters") or [])]))
    print("    pending_character:", json.dumps(s.get("pending_character"), ensure_ascii=False))
    hits = []
    for i, m in enumerate(msgs):
        body = (m.get("narrator") or "") + (m.get("line") or "") + (m.get("content") or "")
        if "新角色" in body or "[[" in body:
            tag = []
            if "新角色" in body: tag.append("新角色")
            if "[[" in body: tag.append("状态行")
            hits.append((i, m.get("role"), ",".join(tag)))
    print("   最近 6 条里带标记的:", hits[-6:] if hits else "一条都没有")
    if msgs:
        last = msgs[-1]
        body = (last.get("narrator") or "") + (last.get("line") or "") + (last.get("content") or "")
        print("   最后一条尾巴:", repr(body.strip()[-160:]))
