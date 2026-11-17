# -*- coding: utf-8 -*-
"""vox 常驻合成进程（跑在 3.13 下）。

vox serve 的 /v1/tts 只认预先注册的风格名，内置发音人只有 default，
没法按请求给一句自然语言风格。自己拿 Python 接口就能传 instruct，
所以这里接管合成：stdin 一行一个请求，stdout 一行一个结果。

请求：{"op":"synth","text":..,"speaker":..,"instruct":..,"out":..,"language":..}
      {"op":"ping"}  {"op":"close"}
"""
import sys, os, json, traceback

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    model = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "models", "qwen3-custom.tmod")
    device = sys.argv[2] if len(sys.argv) > 2 else "auto"
    threads = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    import vox
    engine = vox.Engine(model, device=device, threads=threads)
    cache = {}

    builtin = {str(x).lower() for x in engine.speakers}
    default_speaker = "vivian" if "vivian" in builtin else sorted(builtin)[0]

    def voice_of(speaker, instruct):
        # speaker 必须是内置发音人；传上来的可能是角色名，那属于上层的映射，
        # 到这儿还没对上就是没配音色，退回默认，别让整句念不出来
        name = str(speaker or "").strip().lower()
        if name not in builtin:
            name = default_speaker
        instruct = str(instruct or "").strip() or None
        key = (name, instruct)
        v = cache.get(key)
        if v is None:
            v = engine.speaker_voice(name, instruct)
            cache[key] = v
        return v

    print(json.dumps({"ok": True, "ready": True, "speakers": list(engine.speakers),
                      "sample_rate": engine.sample_rate}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            print(json.dumps({"ok": False, "error": "请求不是合法 json"}), flush=True)
            continue
        op = req.get("op") or "synth"
        if op == "close":
            print(json.dumps({"ok": True}), flush=True)
            break
        if op == "ping":
            print(json.dumps({"ok": True}), flush=True)
            continue
        try:
            v = voice_of(req.get("speaker"), req.get("instruct"))
            audio = engine.synthesize(req.get("text") or "", v)
            out = req["out"]
            vox.save_wav(out, audio, engine.sample_rate)
            print(json.dumps({"ok": True, "out": out, "frames": int(len(audio))}), flush=True)
        except Exception as e:
            tb = traceback.format_exc()
            print(json.dumps({"ok": False,
                              "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
                              "trace": tb[-1200:]}), flush=True)

if __name__ == "__main__":
    main()
