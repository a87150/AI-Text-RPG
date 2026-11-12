# 数据格式与接口

## 目录

    data/
    ├── <会话 id>.json          一局对话
    ├── stories/<故事 id>.json   故事模板
    ├── snapshots/<会话 id>/     存档点，每个会话留 10 份
    └── settings.json          模型、Key、参数、档位

会话 id 是 12 位十六进制串，由后端生成。

## 会话 JSON

    {
      "id": "298b5362d9b3",
      "title": "雨夜",
      "created": 1794259817,
      "turn": 7,
      "mode": "rp",
      "story_id": "65af52ef42e0",
      "user_name": "我",
      "user_role": "夜班护士，手上有一道旧疤。",
      "world": "旧城，常年下雨。",
      "rules": "用户自己加的额外要求",
      "opening": "开场那一段正文",
      "summary": "更早剧情的压缩摘要",
      "compressed_upto": 12,
      "pending_character": null,
      "characters": [
        {"name": "甲", "role": "守灯人，话少，记仇。", "state": "此刻的状态", "history": ["最近的变化"]}
      ],
      "status": {
        "present": ["甲"], "away": ["丙"],
        "location": "某酒馆", "time": "深夜",
        "relations": "戒备", "items": {"甲": "湿伞、铜钥匙"}, "goal": "等他先开口"
      },
      "facts": [
        {"fact": "钥匙到了甲手里", "state": "active", "turn": 3, "subject": "钥匙"}
      ],
      "messages": [
        {"role": "user", "content": "我推门进去。"},
        {"role": "assistant", "narrator": "旁白",
         "lines": [{"speaker": "甲", "text": "台词"}, {"speaker": "乙", "text": "另一句"}]}
      ],
      "lore": [{"keys": "某酒馆", "content": "城南的酒馆，老板欠甲一条命。"}]
    }

character 的 state 和 history 由模型每轮的「【变化】」维护；第 0 个角色决定 ai_name 和 ai_role。

一般模式下 assistant 消息只有 narrator。角色扮演模式下多一个 lines 数组，每个开口的角色一条
{"speaker", "text"}，按发言顺序排；渲染时一人一个气泡。旧的单发言人数据用 speaker + content，
读取时两种都兼容。

status.items 是 {人名: "东西、东西"}；模型写成没有归属的一串时退回字符串，界面上标「（公共）」。

compressed_upto 是压缩游标：小于它的消息已经进了 summary。消息本身不删，所以分叉、导出、全文搜索
都还能读全文。

pending_character 是角色数满时暂存的新人 {"name", "role"}，等用户在弹窗里决定换谁进来。

## 分叉

POST /api/sessions/<sid>/branch {"index": n}

深拷贝原会话，换新 id，标题加「（分支）」，messages 只留 [:n+1]，原会话不动。含 n 那一条，所以两种落点都对：在你的话上分叉，新会话等一条回复；在 AI 的话上分叉，接着往下写。

分叉点之后的东西不能带过去，否则新对话会"剧透"：

- turn 退回到分叉那条消息记录的轮次（每条 assistant 消息带 turn 字段）
- facts 只留 turn 严格小于该轮次的（facts 的 turn 记在本轮开始前，比消息的 turn 小 1）
- lore 只留 turn 小于该轮次的；没有 turn 字段的老条目保留
- summary 清空、compressed_upto 归零：摘要写的是已经被撤销的剧情，留着模型会接着它往下写
- status 与 characters[].state 按消息快照恢复。每条 assistant 消息带一份 snap（当时的 status 与每人 state），
  回滚取剩下消息里最新的那份；一份都没有就清空。不这么做的话，场景和状态都是增量叠加的，重发后会停在
  被撤销那一轮

## 故事 JSON

结构和会话基本一致，只是没有 messages、status、facts、summary。它在「开新局」时被复制成新会话的初始设定。

## settings.json

    {
      "provider": "ollama",
      "model": "dmtx/qwen3.5-9b-abliterated:latest",
      "providers": {
        "deepseek": {"base_url": "...", "api_key": "sk-..."}
      },
      "writing": {"narrator_min": 40, "line_max": 280, "context_limit": 8000},
      "writing_version": 1,
      "mode": "rp",
      "reasoning": "medium",
      "tier": "basic",
      "backup": {"enabled": false, "every_hours": 24}
    }

写文件时先写临时文件再 os.replace，断电不会留下半个 JSON。备份打包时 api_key 会被清空。

## HTTP 接口

会话与故事：

    GET    /                              首页
    GET    /api/sessions                  会话列表（只带元信息）
    POST   /api/sessions                  新建会话，可带 story_id
    GET    /api/sessions/<sid>            读完整会话
    PUT    /api/sessions/<sid>            改设定（会先存快照）
    DELETE /api/sessions/<sid>            删除
    POST   /api/sessions/<sid>/clear      清空消息
    POST   /api/sessions/<sid>/compress   手动触发压缩
    POST   /api/sessions/<sid>/character-pick   角色满时换人，落定 pending_character
    GET    /api/stories                   故事列表
    POST   /api/stories                   新建故事
    GET    /api/stories/<id>              读故事
    PUT    /api/stories/<id>              改故事
    DELETE /api/stories/<id>              删故事
    POST   /api/stories/<id>/cover        上传封面（multipart，必须带 X-Cover-Upload 头）
    POST   /api/stories/<id>/cover/gen    用 ComfyUI 生封面，可传 prompt / negative
    POST   /api/stories/<id>/cover/undo   撤回到上一张
    GET    /api/stories/<id>/cover/job    查一次生图进度（前端每两秒问一次）
    GET    /covers/thumbs/<name>          封面缩略图（列表用，160px）
    GET    /covers/<name>                 封面图本体

消息控制：

    POST   /api/sessions/<sid>/rewind     从某条消息重写
    PUT    /api/sessions/<sid>/message    就地编辑某条消息
    POST   /api/sessions/<sid>/branch     从某条分叉出新会话
    POST   /api/sessions/<sid>/commit_partial   把停下来的半段当正式回复存下

流式生成（SSE）：

    POST   /api/chat                      发一条消息并生成
    POST   /api/sessions/<sid>/opening    生成开场白
    POST   /api/enhance                   单项设定生成

配置与维护：

    GET    /api/search                    全文搜索，?q= 至少两个字，?limit= 上限 200
    GET    /api/config                    当前模型、模式、参数、档位
    POST   /api/writing                   保存写作参数 / 模式 / 思考强度 / 档位
    GET    /api/models                    本地可用模型
    POST   /api/model                     切换模型
    GET    /api/providers                 提供方列表（Key 不回显）
    POST   /api/providers/<pid>           保存提供方配置
    GET    /api/sessions/<sid>/snapshots  存档点列表
    POST   /api/sessions/<sid>/snapshots/<name>/restore   回滚到某个存档点
    DELETE /api/providers/<pid>           删掉一个自建提供方
    POST   /api/writing/reset             写作参数恢复默认
    GET    /api/sessions/<sid>/export.md  导出成 Markdown
    POST   /api/sessions/import           把导出的 JSON 装回来，开一份新会话
    POST   /api/sessions/<sid>/lore/promote   把这一局的词条固化到故事模板
    GET    /api/backups                   备份列表与定时配置
    POST   /api/backups                    立即备份
    GET    /api/backups/<name>            下载备份
    POST   /api/backups/<name>/restore    还原这份备份
    DELETE /api/backups/<name>            删除备份
    PUT    /api/backup-config              定时备份：enabled / every_minutes / keep

还原的三个要点：

1. 先把当前数据打一个 -beforerestore 的保险包，点错了还能退回来
2. 备份里的 api_key 是被抹掉的，写回前用还原前的 Key 补上，否则点一次还原云端 Key 就没了
3. 还原完调 load_settings() 重新读配置，不然磁盘上的 settings.json 回来了、进程里还是旧值

解压只接受 data/ 下、以 .json 结尾的条目，路径做 normpath 后必须落在 DATA_DIR 里，挡掉 zip slip。

## 错误约定

业务错误统一返回 {"error": "中文说明"}，前端直接显示。常见状态码：

    400  参数不合法
    404  会话/故事/备份不存在
    409  该会话正在生成中
    500  后端异常（写文件失败、模型调用失败）

## SSE 事件

    {"type": "speaker", "name": "甲"}
    {"type": "thinking", "chars": 128}
    {"type": "delta", "content": "正文增量", "chars": 42}
    {"type": "retry", "attempt": 2, "max": 3, "reason": "内容太短"}
    {"type": "done", "attempts": 1, "context_chars": 1830, "context": {...},
     "message_count": 4, "turn": 3, "characters": [...]}
    {"type": "error", "message": "生成失败", "attempts": 3, "raw": "..."}

delta 事件的字段名是 content，不叫 delta；thinking 只带累计字数 chars，不带原文。
final 是内部事件，不会出现在 SSE 里。

/api/enhance 走另一条流，事件只有这三种：

    {"type": "delta", "content": "增量", "chars": 12}
    {"type": "result", "field": "world", "name": "", "attempts": 1, "data": {...}}
    {"type": "error", "message": "生成失败", "attempts": 3, "raw": "..."}
