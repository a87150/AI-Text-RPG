# -*- coding: utf-8 -*-
"""入口：python lt.py 启动；应用本体在 rp/ 包里。

兼容旧引用：常用名字仍然可以从 lt 直接取到。
"""
import os

from rp.api import app  # noqa: F401
from rp.characters import (  # noqa: F401
    apply_change, ensure_characters, next_speaker, parse_turn,
    role_labels, upsert_character,
)
from rp.config import (  # noqa: F401
    DATA_DIR, DEFAULT_MODEL, KEEP_RECENT_MESSAGES, MAX_CHAT_ATTEMPTS,
    MAX_CONTEXT_CHARS, MAX_ENHANCE_ATTEMPTS, MAX_OPENING_ATTEMPTS,
    MIN_CHAT_CHARS, OLLAMA_HOST, PROVIDERS, SETTINGS_PATH,
)
from rp.context import background_compress, compress_session, context_chars, context_info  # noqa: F401
from rp.llm import completion_events, model_call  # noqa: F401
from rp.prompts import build_messages, build_system_prompt  # noqa: F401
from rp.storage import load_session, save_session  # noqa: F401

if __name__ == "__main__":
    from rp.backup import start_auto
    from rp.config import VERSION, LAN_IP, backup_conf

    start_auto(backup_conf)      # 定时备份：配置里没开就空转，开了一分钟内生效
    port = int(os.environ.get("PORT", "5000"))
    print("代面 %s 已启动：http://127.0.0.1:%d" % (VERSION, port))
    if LAN_IP:
        print("同一局域网：http://%s:%d" % (LAN_IP, port))
    print("Ollama：%s" % OLLAMA_HOST)
    # 绑 0.0.0.0：127.0.0.1 只收本机连接，手机和平板连不进来
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)