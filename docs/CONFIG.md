# 可配参数

所有可配项都在 `rp/config.py`，界面上的输入框范围由同一张常量表决定；`check.py` 第 66 组会核对「常量 / 界面 min-max / 代码引用」三层是否一致，改错会直接报错。

## 写作参数（设置 → 写作）

存在 `settings.json` 的 `writing` 里，界面 id 与常量键一一对应。

| 键 | 默认 | 范围 | 作用 | 界面 |
| --- | --- | --- | --- | --- |
| `narrator_min` | 40 | 10 ~ 300 | 旁白最少字数 | `#w-narrator-min` |
| `narrator_max` | 100 | 20 ~ 600 | 旁白最多字数 | `#w-narrator-max` |
| `line_min` | 120 | 40 ~ 1200 | 角色台词最少字数 | `#w-line-min` |
| `line_max` | 280 | 60 ~ 2000 | 角色台词最多字数 | `#w-line-max` |
| `context_limit` | 8000 | 2000 ~ 60000 | 上下文预算（字） | `#w-context` |
| `keep_recent` | 16 | 4 ~ 60 | 压缩后保留最近多少条 | `#w-keep` |
| `max_characters` | 4 | 1 ~ 10 | 一局里最多几个角色 | `#w-chars` |
| `summary_chars` | 400 | 150 ~ 1200 | 每段压缩摘要的字数上限 | `#w-summary` |

切能力档位时 `context_limit` 会被该档位的默认值覆盖——档位就是靠这个定义预算的。手改之后又切了档位，改的会被冲掉。

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

## 前端本地存储

| key | 内容 |
| --- | --- |
| `rp-theme` | 亮色 / 暗色 |
| `rp-font` | 字号档位 |
| `rp-draft:<会话 id>` | 该会话未发送的输入草稿 |
| 视图与上次停留的会话 | 刷新后回到原位置 |
