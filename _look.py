# -*- coding: utf-8 -*-
import json, glob, os

files = [(os.path.getmtime(p), p) for p in glob.glob("data/*.json") if not p.endswith("settings.json")]
files.sort(reverse=True)
s = json.load(open(files[0][1], encoding="utf-8"))
msgs = s.get("messages") or []
for i in (42, 51, 55, len(msgs) - 1):
    if i < 0 or i >= len(msgs):
        continue
    m = msgs[i]
    body = (m.get("narrator") or "") + "\n--line--\n" + (m.get("line") or "") + "\n--content--\n" + (m.get("content") or "")
    print("=" * 20, i, m.get("role"), "speaker=", m.get("speaker"))
    print(body.strip()[-420:])
    print("")
