# -*- coding: utf-8 -*-
"""入口：python lt.py 启动；应用本体在 rp/ 包里。

兼容旧引用：常用名字仍然可以从 lt 直接取到。
"""
import os

from rp.api import app  # noqa: F401
from rp.characters import (  # noqa: F401
    MAX_CHARACTERS, apply_change, ensure_characters, next_speaker, parse_turn,
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
    port = int(os.environ.get("PORT", "5000"))
    print("角色扮演应用已启动：http://127.0.0.1:%d" % port)
    print("Ollama：%s" % OLLAMA_HOST)
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
