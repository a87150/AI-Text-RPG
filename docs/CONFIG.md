# 可配参数

所有可配项都在 `rp/config.py`，界面上的输入框范围由同一张常量表决定；`check.py` 第 66 组会核对「常量 / 界面 min-max / 代码引用」三层是否一致，改错会直接报错。

## 写作参数（设置 → 写作）

存在 `settings.json` 的 `writing` 里，界面 id 与常量键一一对应。

| 键 | 默认 | 范围 | 作用 | 界面 |
| --- | --- | --- | --- | --- |
| `narrator_min` | 40 | 10 ~ 300 | 旁白段落最少字数 | `#w-narrator-min` |
| `narrator_max` | 100 | 20 ~ 600 | 旁白段落最多字数 | `#w-narrator-max` |
| `line_min` | 120 | 10 ~ 1200 | 每个角色一份，最少字数 | `#w-line-min` |
| `line_max` | 280 | 60 ~ 2000 | 每个角色一份，最多字数 | `#w-line-max` |
| `context_limit` | 8000 | 2000 ~ 60000 | 上下文预算（token） | `#w-context` |
| `keep_recent` | 16 | 4 ~ 60 | 压缩后保留最近多少条 | `#w-keep` |
| `max_characters` | 4 | 1 ~ 10 | 一局里最多几个角色 | `#w-chars` |
| `gen_role_min` | 0 | 0 ~ 800 | 角色卡最少字数，0 = 跟随档位 | `#w-gen-role-min` |
| `gen_role_max` | 0 | 0 ~ 1500 | 角色卡最多字数，0 = 跟随档位 | `#w-gen-role-max` |
| `gen_world_min` | 0 | 0 ~ 800 | 世界观最少字数，0 = 跟随档位 | `#w-gen-world-min` |
| `gen_world_max` | 0 | 0 ~ 2000 | 世界观最多字数，0 = 跟随档位 | `#w-gen-world-max` |
| `gen_open_min` | 0 | 0 ~ 800 | 开场白最少字数，0 = 跟随档位 | `#w-gen-open-min` |
| `gen_open_max` | 0 | 0 ~ 1500 | 开场白最多字数，0 = 跟随档位 | `#w-gen-open-max` |
| `summary_chars` | 400 | 150 ~ 2000 | 每段压缩摘要的字数上限 | `#w-summary` |

字数这四个值怎么落到 prompt 上，按模式分两种：

| 模式 | 旁白 | 每个角色 | 整轮上限 |
| --- | --- | --- | --- |
| 一般 `normal` | —— | —— | `narrator_max + line_max`（合成一段，不拆） |
| 角色扮演 `rp` | `narrator_min ~ narrator_max` | `line_min ~ line_max` 各一份 | `narrator_max + line_max × 人数` |

人数取角色表里的总数，不是只在场的。同一个区间还会写进最后一条 user 消息的尾巴，
内容和 rules 文件里的一致，不会出现文件说一套、尾巴说另一套。

这四个数是**区间**，不是目标值：低于下限算没写完，高于上限算替用户往下写。
程序不会按输入长短替你改这个区间；短输入的抑制靠规则文件里的文字提示
（`DENSITY_HINT`），不改数字。

`gen_role_*` / `gen_world_*` / `gen_open_*` 是「AI 补设定」三类各自的目标字数，
互不影响，填 0 就跟随模型档位。

跟随是有道理的：档位决定人设能注入多少字（`role_chars`），写超了注入时会被截断，
所以默认区间本来就贴着档位。三类按篇幅比例分：角色卡是基准，世界观 ×1.67、
开场白 ×1.33——世界观要铺时代地点规则，开场白要留出第一段互动，都比一张卡长。

| 档位 | 角色卡 | 世界观 | 开场白 |
|---|---|---|---|
| 初级 | 120~180 | 200~300 | 160~240 |
| 标准 | 240~360 | 400~600 | 320~480 |
| 高级 | 320~520 | 533~866 | 426~693 |

设置面板里留空的框会用 placeholder 显示跟随档位时的实际值，切档位能看出差别。
填了数字就盖过档位，只影响填的那一类。

切能力档位时 `context_limit` 会被该档位的默认值覆盖——档位就是靠这个定义预算的。手改之后又切了档位，改的会被冲掉。反过来，档位抬高之后，之前被压缩掉的会话如果在新预算里装得下，游标会自动退回 0、摘要是清掉的（`_reclaim_context`），原文重新进上下文。

## 能力档位（`TIER_LIMITS`）

档位不是界面参数，是 `config.py` 里的一张表，切档位等于查表。

| 项 | 初级 `basic` | 标准 `standard` | 高级 `advanced` | 为什么 |
| --- | --- | --- | --- | --- |
| `context` | 8000 | 16000 | 32000 | 窗口决定能塞多少 |
| `history_msgs` | 16 | 40 | 0（不限） | 长历史对小模型是纯噪音 |
| `role_chars` | 200 | 400 | 0（不截断） | 设定越长越容易串味 |
| `facts` | 6 | 12 | 24 | 账本是连贯性的关键，但读多了会抄 |
| `anchor_every` | 8 | 15 | 24 | 小模型更健忘，要更勤地重插人设 |
| `attempts` | 3 | 3 | 2 | 格式容易坏的模型多给一次 |

`0` 是「不限」而不是「零」：`history_msgs=0` 表示不按条数裁，`role_chars=0` 表示不截断。界面上这两处会显示「当前档位不限制长度」而不是「0 字」。

截断不会拦腰砍字，`clip()` 优先断在句号、分号、换行上。

## 备份（设置 → 备份）

| 键 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `enabled` | false | — | 是否定时备份 |
| `every_minutes` | 1440 | 5 ~ 20160 | 每隔多少分钟一次（5 分钟 ~ 14 天） |
| `keep` | 30 | 1 ~ 30 | 最多留几份，超出删最旧的 |

旧版本存的是 `every_hours`，读到会自动乘 60 迁移过来。定时线程每 60 秒重读一次配置，改完不用重启。

## 模型与提供方

`settings.json` 里 `provider` / `model` 记当前选择，`providers` 存自定义提供方：

```json
{
  "provider": "custom1",
  "model": "uncensored",
  "providers": {
    "custom1": {
      "label": "apiiasemcensura",
      "base_url": "https://api.apiiasemcensura.com/v1",
      "api_key": "sk-...",
      "models": ["uncensored"]
    }
  }
}
```

Ollama 是内置提供方，不需要存 Key；模型列表来自 `GET /api/models` 实时查询本地 Ollama，不在列表里的名字不会显示（以免选中一个不存在模型就 404）。

**关于写盘**：`persist_settings()` 会把内存里的整份配置写回磁盘。命令行小脚本（`python -c`）从不调用 `load_settings()`，内存里全是默认值，直接写会把用户真实的 providers、Key、写作参数一起抹掉。所以 `config.py` 有一个 `_loaded` 标记：没加载过的进程只写它真正改过的那项，其余沿用磁盘原值。**新增任何写 settings.json 的代码路径，必须走 `persist_settings()`，不要自己 `os.replace`。**

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `PORT` | 监听端口，默认 5000 |
| `LT_DATA_DIR` | 数据目录，默认 `data/`；`check.py` 用它指到临时目录 |
| `LT_BACKUP_DIR` | 备份目录，默认 `backups/`；`check.py` 同样指到临时目录 |
| `LT_LOG_DIR` | 事件日志目录，默认 `logs/`；已进 .gitignore |
| `LT_COMFY` | ComfyUI 地址，默认 `http://127.0.0.1:8188` |
| `LT_COVER_TIMEOUT` | 封面生图超时秒数，默认 180，下限 30 |

## 前端本地存储

| key | 内容 |
| --- | --- |
| `rp-theme` | 亮色 / 暗色 |
| `rp-font` | 字号档位 |
| `rp-draft:<会话 id>` | 该会话未发送的输入草稿 |
| 视图与上次停留的会话 | 刷新后回到原位置 |
