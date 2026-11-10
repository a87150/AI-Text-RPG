import pathlib
p = pathlib.Path("check.py")
s = p.read_text(encoding="utf-8")
anchor = "    shutil.rmtree(TMP_DIR, ignore_errors=True)"
block = '''    # 52. 四个字数设置合起来决定一轮的总量
    _ws = {"turn": 1, "mode": "rp", "user_name": "我",
           "characters": [{"name": "沈砚", "role": "守灯人"}],
           "status": {}, "messages": [{"role": "user", "content": "我点头。"}]}
    _w = rp_config.writing()
    _total = _w["narrator_max"] + _w["line_max"]
    for _t in ("basic", "standard", "advanced"):
        rp_config.save_tier(_t, persist=False)
        _bd = rp_prompts.build_system_prompt(_ws, "沈砚")
        check("%s档的规则给出合计上限" % _t, ("不超过 %d 字" % _total) in _bd)
    rp_config.save_tier("basic", persist=False)
    _nb = rp_prompts.build_system_prompt(dict(_ws, mode="normal"), None)
    _lo = _w["narrator_min"] + _w["line_min"]
    check("一般模式用总量而不是只算台词",
          ("%d~%d 字" % (_lo, _total)) in _nb, _nb[_nb.find("每次"):][:40])
    # 改设置，规则和尾巴都要跟着变
    rp_config.save_writing({"line_max": 120}, persist=False)
    _bd2 = rp_prompts.build_system_prompt(_ws, "沈砚")
    check("调小台词上限后合计上限跟着降",
          ("不超过 %d 字" % (_w["narrator_max"] + 120)) in _bd2)
    _tl = rp_prompts.build_messages(_ws, "沈砚")[-1]["content"]
    check("尾巴里的上限也跟着降", ("最多 %d 字" % (_w["narrator_max"] + 120)) in _tl, _tl[-80:])
    rp_config.save_writing({"line_max": _w["line_max"]}, persist=False)

'''
assert anchor in s
s = s.replace(anchor, block + anchor, 1)
p.write_text(s, encoding="utf-8")
print("ok")
