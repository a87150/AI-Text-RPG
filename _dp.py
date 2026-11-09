import rp.prompts as P
s = {"world": "旧城，长年下雨。", "user_role": "夜班护士林晚，手上有一道旧疤。",
     "characters": [{"name": "沈砚", "role": "守灯人，三十出头，话少，眼神沉，右手常年揣在兜里。"},
                    {"name": "阿鸾", "role": "酒馆跑堂，嘴快，嘴甜心冷。"}],
     "status": {"location": "银月酒馆门廊", "time": "深夜", "relations": "互相试探", "items": "湿伞", "goal": "确认你来意"},
     "facts": [{"fact": "沈砚让林晚进了门", "state": "active", "turn": 1}],
     "mode": "rp"}
open("_prompt.txt", "w", encoding="utf-8").write(P.build_system_prompt(s, "沈砚"))
print("len", len(P.build_system_prompt(s, "沈砚")))
