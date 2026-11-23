# -*- coding: utf-8 -*-
"""入口：python lt.py 启动；应用本体在 rp/ 包里。

这里只保留 check.py 打补丁和取常量时真要用到的名字。以前是「常用名字都
从 lt 取一份」的兼容层，三十来个 re-export 里有二十多个已经没人引用——
留着会让人以为改了 rp/ 之后还要同步改这里。
新代码请直接从 rp.xxx 导入。
"""
import os

from rp.api import app  # noqa: F401  自检里 lt.app.test_client()
from rp.characters import next_speaker, parse_turn, upsert_character  # noqa: F401
from rp.config import (  # noqa: F401
    KEEP_RECENT_MESSAGES, MAX_CONTEXT_TOKENS, OLLAMA_HOST,
)
from rp.llm import completion_events  # noqa: F401  自检要把它换成假流

if __name__ == "__main__":
    from rp.backup import start_auto
    from rp.config import VERSION, LAN_IP, backup_conf

    start_auto(backup_conf)      # 定时备份：配置里没开就空转，开了一分钟内生效
    port = int(os.environ.get("PORT", "5000"))
    print("代面 %s 已启动：http://127.0.0.1:%d" % (VERSION, port))
    if LAN_IP:
        print("同一局域网：http://%s:%d" % (LAN_IP, port))
    else:
        # 静默跳过的话，启动日志看着完全正常，手机连不上时根本不会
        # 想到是绑定/白名单的问题
        print("还没取到局域网地址：手机暂时连不进来。网卡起来后会自动补上，不用重启。")
    print("Ollama：%s" % OLLAMA_HOST)
    # 绑 0.0.0.0：127.0.0.1 只收本机连接，手机和平板连不进来
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)