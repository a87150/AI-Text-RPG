# 开发与自检

## 跑起来

```bash
python lt.py            # 默认 5000
PORT=8080 python lt.py  # 换端口
```

没有构建步骤。改 `static/js/*.js`、`static/css/app.css`、`templates/index.html` 之后刷新浏览器即可；改 `rp/*.py` 要重启进程。

改前端后先做语法检查，别等浏览器报错：

```bash
node --check static/js/ui.js
```

## check.py

`python check.py` 是唯一的测试入口，不发网络请求，几十秒跑完，全绿才提交。它自己搭一个临时数据目录跑整套流程：

```python
TMP_DIR = tempfile.mkdtemp(prefix="lt-check-")
os.environ["LT_DATA_DIR"] = TMP_DIR
os.environ["LT_BACKUP_DIR"] = os.path.join(TMP_DIR, "backups")
```

两个环境变量都指到临时目录，**不会碰用户真实的 `data/` 和 `backups/`**。这一点是踩过坑才加的：以前每跑一次检查就往真实 `backups/` 里塞一对包，攒了 27 份。

测验按编号分组，用 `check("说明", 条件, 附加信息)` 断言。分组大致是：

| 组 | 覆盖 |
| --- | --- |
| 1 ~ 20 | 会话 CRUD、模式切换、角色解析、状态行三种写法 |
| 21 ~ 30 | 压缩、快照、备份、导出 |
| 31 ~ 50 | API 边界、错误路径、SSE 事件序列 |
| 51 ~ 60 | 前端约定（DOM 挂点、模块导出） |
| 62 ~ 64 | 圆桌式解析、角色卡单一状况、备份份数 |
| 65 ~ 67 | 角色弹层、四层对账、搜索与草稿与冲突检查 |
| 68 ~ 92 | 对话拆分、世界书、名字去重、回写禁止 |
| 93 ~ 101 | 封面生命周期、十轮回归、文档与路由对账、前端绑定 |

## 加功能的约定

**文档里不写用户故事的具体内容。** 例子一律用甲、乙、丙这种代号，或者“角色甲”；
地名写某城、某酒馆。代码注释、check.py 的测试文案、提示词示例同样适用。


**配置参数走四层**。新增一个可配项时，四样东西必须同时到位，否则 `check.py` 第 66 组会报：

1. `config.py` 里的默认值与范围（`WRITING_DEFAULTS` / `WRITING_BOUNDS` 或备份那张表）。
2. `templates/index.html` 里对应 `<input>` 的 `min` / `max` 与常量一致。
3. `check.py` 的 `UI_IDS` 映射表里登记这个键到 input id。
4. 代码里真的用它——只在定义处出现一次会被判成死配置。

**写设置只能走 `persist_settings()`**。它会判断 `_loaded`：没加载过配置的进程只写它改过的那项，其余沿用磁盘原值。任何自己 `os.replace` 写 `settings.json` 的新代码都会绕过这层保护，把用户的 Key 抹掉。

**用户交互式的写要带版本号**。`save_session(sess, expect=...)` 是乐观锁：磁盘上的 `updated_at`
和它对不上就抛 `StaleSession`。改设定、改消息、半截入库这三条路必须传，因为它们拿着
前端几分钟前读的对象去覆盖整份文件。后端自己在锁内 `load` 完立刻写的路径
不用传：竞争窗口已经被锁封住了，多一层校验只会带来假冲突。

**写盘前要看取消标志**。流式生成器被客户端 abort 时，Flask 会 close 它，在 `yield` 点抛
`GeneratorExit`。注意：**它只在 yield 点响应**，如果 abort 发生在一段纯同步代码中间（比如正在
序列化），那段代码会跑完。所以不能指望异常自己把活干停下，要明确置标志、
写盘前主动检查。

**数据层耗时已经在量**。`storage.IO_TIMING` 记录最近一次读写的毫秒数和文件大小，
在上下文面板里显示。加了什么字段导致会话变胖，开面板看一眼就知道。

**前端不要用模块级导入名当局部变量**。`state`、`config`、`api` 这些在模块顶部 import 过，函数里再写 `const state = ...` 会在 TDZ 里把前面用到的同名变量一起弄崩，而且报错信息指向的是函数第一行，很难看出是这个原因。

**物品与状态分开存**。`state` 是「此刻」，`facts` 是「发生过且不能反悔」，`items` 是「东西在谁手上」。三种东西的更新时机和合并规则都不同，别互相替代。

## 事件日志

`rp/metrics.py` 往 `logs/YYYY-MM-DD.log` 追加一行一个 JSON。出事时 grep 事件名：

    grep status.parse_fail logs/*.log      # 模型写了状态块但没解出来，带原文末尾 300 字
    grep session.stale logs/*.log          # 两个标签页撞车，带两边的 updated_at
    grep ledger.redundant logs/*.log       # 能从物品格推出来的事实有几条
    grep chat.first_token logs/*.log       # 首字耗时，看 prefill 走到哪了

全部事件：chat.request / chat.first_token / chat.done / stream.cancel /
session.stale / status.parse_fail / ledger.merge / ledger.redundant / snapshot.create。

写日志失败一律吞掉，不会因为磁盘满或权限把对话弄挂。日志不进版本库。

直接读最近一次的指标：`python -c "import rp.metrics as m; print(m.snapshot())"`。

**首字耗时才是真正的瓶颈**，不是写盘。写盘是 0～1ms，首字实测 2.3s。
以后判断“该不该动数据层”看写盘，判断“该不该动上下文策略”看首字。

## 调试

**看某次请求实际发了什么**：设置里有调试入口可以看拼好的 system 提示和历史（`build_context_debug`）。上下文面板（顶栏 📊）显示当前占用、压缩情况、在场角色。

**看模型原始输出**：`llm.py` 里 `completion_events()` 逐块吐事件，写歪的格式先过 `strip_status_leaks()` 清洗，再交给 `parse_turn()`。想知道模型到底写了什么，临时在 `parse_turn` 前面打一行 `repr(text)`。

**前端无头验证**：`node` 起一个 Edge headless，用 CDP 连上去点。设备是 `--headless=new --remote-debugging-port=PORT --user-data-dir=<临时目录>`，然后 `fetch http://127.0.0.1:PORT/json/list` 拿 WebSocket 地址。

写这类脚本有个坑：**别在单引号字符串里塞真实换行**。JS 里 `'a\nb'` 是转义序列没问题，但如果生成脚本时把 `\n` 写成了真换行，整个脚本会 `SyntaxError`。用 `String.fromCharCode(10)` 或 `JSON.stringify` 拼。

## 提交

一次改动一个提交，说清楚「为什么」而不是「改了什么」。消息用中文，标题一行说完，正文分点。

仓库里 `data/` 和 `backups/` 都在 `.gitignore` 里——里面有明文 API Key 和私人内容，不进版本库。
