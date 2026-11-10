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
      "user_name": "林晚",
      "user_role": "夜班护士，手上有一道旧疤。",
      "world": "旧城，常年下雨。",
      "rules": "用户自己加的额外要求",
      "opening": "开场那一段正文",
      "summary": "更早剧情的压缩摘要",
      "characters": [
        {"name": "沈砚", "role": "守灯人，话少，记仇。", "state": "此刻的状态", "history": ["最近的变化"]}
      ],
      "status": {
        "present": ["沈砚"], "away": ["周掌柜"],
        "location": "银月酒馆", "time": "深夜",
        "relations": "戒备", "items": "湿伞", "goal": "等他先开口"
      },
      "facts": [
        {"fact": "钥匙到了沈砚手里", "state": "active", "turn": 3, "subject": "钥匙"}
      ],
      "messages": [
        {"role": "user", "content": "我推门进去。"},
        {"role": "assistant", "speaker": "沈砚", "content": "台词", "narrator": "旁白"}
      ],
      "lore": [{"keys": "银月酒馆", "content": "城南的酒馆，老板欠沈砚一条命。"}]
    }

character 的 state 和 history 由模型每轮的「【变化】」维护；第 0 个角色决定 ai_name 和 ai_role。

一般模式下 assistant 消息只有 narrator，content 为空；角色扮演模式两者都有。

## 分叉

POST /api/sessions/<sid>/branch {"index": n}

深拷贝原会话，换新 id，标题加「（分支）」，messages 只留 [:n+1]，原会话不动。含 n 那一这条，所以两种落点都对：在你的话上分叉，新会话等一条回复；在 AI 的话上分叉，接着往下写。

分叉点之后的东西不能带过去，否则新对话会"剧透"：

- turn 退回到分叉那条消息记录的轮次（每条 assistant 消息带 turn 字段）
- facts 只留 turn 小于该轮次的（facts 的 turn 记在本轮开始前，比消息的 turn 小 1）
- summary 保留，因为压缩掉的都是分叉点之前的内容
- status 与 characters[].state/history 不回滚。要精确回滚得给每条消息存完整快照，代价是整个会话文件翻几倍；而地点、在场这些会随剧情自然纠正，账本才是真正会造成矛盾的东西

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
    GET    /api/stories                   故事列表
    POST   /api/stories                   新建故事
    GET    /api/stories/<id>              读故事
    PUT    /api/stories/<id>              改故事
    DELETE /api/stories/<id>              删故事

消息控制：

    POST   /api/sessions/<sid>/rewind     从某条消息重写
    PUT    /api/sessions/<sid>/message    就地编辑某条消息
    POST   /api/sessions/<sid>/branch     从某条分叉出新会话

流式生成（SSE）：

    POST   /api/chat                      发一条消息并生成
    POST   /api/sessions/<sid>/opening    生成开场白
    POST   /api/enhance                   单项设定生成

配置与维护：

    GET    /api/config                    当前模型、模式、参数、档位
    POST   /api/writing                   保存写作参数 / 模式 / 思考强度 / 档位
    GET    /api/models                    本地可用模型
    POST   /api/model                     切换模型
    GET    /api/providers                 提供方列表（Key 不回显）
    POST   /api/providers/<pid>           保存提供方配置
    GET    /api/sessions/<sid>/snapshots  存档点列表
    POST   /api/sessions/<sid>/snapshots/<name>/restore   回滚到某个存档点
    GET    /api/backups                   备份列表与定时配置
    POST   /api/backups                    立即备份
    GET    /api/backups/<name>            下载备份
    POST   /api/backups/<name>/restore    还原这份备份
    DELETE /api/backups/<name>            删除备份
    PUT    /api/backup-config              定时备份开关

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

    {"type": "speaker", "name": "沈砚"}
    {"type": "thinking", "delta": "..."}
    {"type": "delta", "delta": "正文增量"}
    {"type": "retry", "attempt": 2, "max": 3, "reason": "内容太短"}
    {"type": "result", "text": "..."}
    {"type": "done", "message_count": 4, "context_chars": 1830, "characters": [...]}
    {"type": "error", "message": "生成失败"}
