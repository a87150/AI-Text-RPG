# 「代面」全维度健康检查报告

**审计基线**：`HEAD = c21ea29`（`chore: rename the app to 代面`，137 commits，工作区干净）
**审计方式**：只读静态审查 + 只读命令实测；未修改任何源码与配置
**报告生成时间**：2026-11-11

> **基线漂移声明**：审计期间项目发生了一次重命名提交（`3a9d200` → `c21ea29`，产品名「灵枢」→「代面」）。全部结论已用当前 HEAD 重新仲裁；早期抓到的 `check.py` 崩溃是**已修复的中间态**，不计入问题清单。

---

## 一、执行摘要

**项目定位**：单机自用的 AI 文字 RPG / 小说写作应用。Flask 提供 41 个 HTTP 路由，前端为原生 ES Module + SSE 流式渲染，模型侧对接 Ollama 与任意 OpenAI 兼容端点，数据以 JSON 文件落在 `data/`，无数据库、无鉴权、仅绑 `127.0.0.1`。

**技术栈**：Python 3（实机 3.14，全局解释器，非 venv）· Flask 3.1.3 · requests 2.32.5 · 原生 JS（无构建步骤）· JSON 文件存储。

### 规模

| 部分 | 文件数 | 行数 |
|---|---|---|
| Python 业务代码（`rp/` + `lt.py`） | 24 | 5286 |
| `check.py` 自检脚本 | 1 | 2200 |
| JS（`static/js/`） | 10 | 4436 |
| CSS / HTML / 文档 | 1 / 1 / 8 | 1122 / 447 / 1089 |

运行数据：17 个会话、15 个故事模板、10 份备份。

### 项目结构概览（限深 3 层）

```
lt/
├── lt.py              入口：注册 rp.api.app，启动 Flask（127.0.0.1:5000）
├── check.py           唯一自检入口，2200 行，89 个编号分组，605 处 check()
├── requirements.txt   仅两行：flask>=3.1 / requests>=2.31
├── rp/                应用本体
│   ├── api.py         41 个路由（1496 行，同时含业务与持久化）
│   ├── characters.py  角色卡、状态块解析、事实账本、在场判定
│   ├── prompts/       system 提示词拼装 + 分档规则 .md 素材
│   ├── config.py      设置读写、提供方管理、写作参数边界
│   ├── llm.py         provider 调用、SSE 解析、thinking 剥离、重试
│   ├── storage.py     会话读写、分锁、忙标记、原子写
│   ├── context.py     上下文长度统计与后台压缩
│   ├── backup.py      data/ 打包备份、还原、定时线程
│   ├── snapshots.py   每会话最近 10 份快照
│   ├── stories.py     故事模板
│   └── metrics.py     结构化事件日志 + 本轮用量
├── static/js/         10 个模块（api/chat/ui/settings/sessions/main…）
├── templates/index.html
├── docs/              8 份文档：架构/功能/配置/提示词/数据/开发/设计/更新记录
├── data/              会话、故事、快照、settings.json（含明文 Key，已 gitignore）
├── backups/  logs/    备份包与按日 JSON 事件日志（均已 gitignore）
```

**关键外部依赖与配置项**：Ollama（默认 `OLLAMA_HOST`，本机）；任意 OpenAI 兼容 `base_url` + `api_key`（存 `data/settings.json`）；环境变量 `PORT`、`LT_DATA_DIR`、`LT_BACKUP_DIR`、`LT_LOG_DIR`（后三者供自检隔离用）。

### 总体健康度评分

| 维度 | 分数 | 一句话理由 |
|---|---|---|
| 功能性 | **4** | 41 路由全部落地、0 处 TODO、错误响应统一含 409 乐观锁；但地点状态在一条路径上永久丢失 |
| 代码质量 | **3** | 4 个函数集中 187 个分支点、SSE 桥接三套并存；模块切分与注释质量高于平均水平 |
| 测试 | **3** | 605 条断言 / 89 组是真实回归资产，但是 2125 行单函数、零 `assert`、pytest 收集 0 项 |
| 安全 | **3** | 路径穿越 / 注入 / XSS / 密钥外泄四类硬骨头全防住；缺 CSRF/Host/限额三件套，且依赖带 HIGH 级 CVE |
| 性能 | **4** | 实测会话列表 4.5 ms、108KB 单会话解析 0.5 ms；SSE 流式 + 空闲超时 + 压缩退避齐备 |
| 可维护性/文档 | **2** | 8 份文档覆盖面广，但 SSE 字段、路由表、回滚语义、分组表四处硬性不符实现 |
| 工程化 | **2** | 能构建能运行，但 CI / lint / 锁文件 / venv / LICENSE / CONTRIBUTING 全线缺失，依赖不可复现 |

**总分 3.0 / 5 —— 可用但需整改**。无 P0：未发现高危可利用漏洞、不可恢复的数据丢失或启动即失败的缺陷。

### Top 5 最紧急问题

1. **`urllib3 2.5.0` 命中 `CVE-2026-21441`（HIGH）**，且代码正走 `stream=True` 路径，恰好是该 CVE 的触发条件。
2. **无 CSRF / Origin / Host 三重防护**：任意网页可 `no-cors` 触发写接口，反复建备份把历史备份挤出 `KEEP_MAX=30` 后删除。
3. **`place` / `location` 键分裂**：模型独立更新的地点写进一个无人读取的键，界面、导出、下一轮提示全部拿不到。
4. **文档与实现四类硬性不符**：SSE 事件字段全错（照文档写客户端拿到 `undefined`）、路由表缺 6 条、回滚语义与实现相反、`check.py` 分组表停在 67 组（实际 89 组）。
5. **零 CI + 依赖未锁定**：两行 `>=` 声明在全局解释器上跑，同一份 `requirements.txt` 今日会装出另一套含新版 urllib3 的依赖树——"同一声明，两套安全姿态"。

### 阻塞项清单

| 项 | 状态 | 影响 |
|---|---|---|
| `pip-audit` / `safety` | 未安装（`ModuleNotFoundError`） | 漏洞结论降级为 OSV 官方 API 比对，无第二扫描器交叉验证 |
| 真实 HTTP 验证 | 未执行 | 只读约束下启动服务会写 `data/` 与 `settings.json`；CSRF/DNS rebinding/SSRF 均为**静态路径推演** |
| CVE 可利用性实证 | 未验证 | 仅完成"版本范围命中 + 代码路径命中"两段静态匹配，未构造恶意重定向端点验证 OOM |
| 覆盖率数值 | 未测 | `coverage` 未安装；"7/41 路由零 HTTP 覆盖"是字面量静态判定，不等于行覆盖率 |
| 前端行为测试 | 无 | 4436 行 JS 的自动化只有 `node --check` 语法检查 |
| 其他用户对 `settings.json` 可读性 | 未验证 | ACL 只列出 SYSTEM/Administrators/当前用户，未枚举其他账户 |

---

## 二、问题清单

| ID | 级别 | 维度 | 位置 | 证据类型 | 问题 | 影响 | 修复建议 | 验收标准 |
|---|---|---|---|---|---|---|---|---|
| P1-01 | P1 | 安全 | `requirements.txt:2`→实装 `urllib3 2.5.0`；`rp/llm.py:48-53` | 命令输出 | `GHSA-38jv-5279-wg99` = `CVE-2026-21441` = `PYSEC-2026-1996`，范围 `1.22→2.6.3`，实装 2.5.0 受影响；CVSS `AV:N/AC:L/.../A:H`，CWE-409 | `stream=True` + 默认跟随重定向正是触发条件：恶意 `base_url` 返回重定向 + 压缩炸弹可致单进程 OOM | 锁 `urllib3>=2.6.3`（最新 2.8.0）；`pip-audit` 入 CI | `pip-audit -r requirements.txt` 报 0 漏洞；CI 每次提交执行 |
| P1-02 | P1 | 安全 | `rp/config.py:37-44`；`rp/api.py:107,489,966` | 代码 | 无 `before_request`/Origin/Referer 校验；写接口用 `get_json(silent=True) or {}`，无 JSON 体也照常执行 | 跨站 `<form method=POST>` 属 simple request，无需预检即可命中：可重置写作参数、反复 `POST /api/backups` 触发 `backup.py:86 prune()` 删历史备份 | 加 `before_request` 校验 Origin/Referer ∈ {空, `http://127.0.0.1:<port>`}；写路由强制 `application/json` 且非空 | `curl -X POST -H "Origin: http://evil.test" .../api/writing/reset` 返回 403；正常前端仍 200 |
| P1-03 | P1 | 安全 | `rp/config.py:37-44`；`lt.py:31` | 代码 | 绑定正确（确认无 `0.0.0.0`），但无 Host 白名单 / `TRUSTED_HOSTS` | DNS rebinding 可让 `evil.com` 视为同源，读取全部会话正文、备份列表与配置 | `app.config["TRUSTED_HOSTS"] = ["127.0.0.1","localhost"]`（Flask 3.1 原生） | `curl -H "Host: evil.test" .../api/sessions` 返回 400；正常 Host 仍 200 |
| P1-04 | P1 | 功能性 | `rp/characters.py:229-231` 写；`characters.py:409`、`api.py:258,445,658`、`ui.js:683,1103` 读 | 代码 | `refresh_states` 把模型输出的 `place` 写进 `st["place"]`，但 `STATUS_FIELDS` 与全部下游只认 `location`；`llm.py:218` 的泄漏正则也不含 `place` | 地点信息在 `refresh_states` 路径下永久丢失：界面不显示、导出缺失、下一轮读不回。`[[地点：X]]` 走 `MARK_KEYS`→`location` 能进，两条路径结果不一致 | `refresh_states` 统一用 `location`，`place` 作为别名映射进 `MARK_KEYS` 同款机制 | `grep -rn '"place"' rp/` 只出现在别名映射；新增一组"refresh_states 写的地点能被 ui/导出读到" |
| P1-05 | P1 | 可维护性 | `docs/DATA.md:166-170` ←→ `rp/llm.py:161,165`、`api.py:1137-1143` | 代码 | 文档写 `{"type":"delta","delta":...}`、`{"type":"thinking","delta":...}`、`{"type":"result","text":...}`；实现是 `delta.content`、`thinking.chars`、`result.data.text` | 照文档写客户端拿到 `undefined`，正文一片空白——外部使用者最易踩的一条 | 按实现重写 L163-171，补 `done` 的 `title/context/attempts/turn/status` | 文档字段名与 `check.py` 中 `"type": "delta"` 相关断言一致；新增"事件字段与文档同名"断言组 |
| P1-06 | P1 | 可维护性 | `docs/DATA.md:99-144`、`README.md:84` ←→ `rp/api.py:107,210,470,565,784,882` | 代码 | 文档自称"全部 HTTP 路由"，实缺 6 条：`writing/reset`、`DELETE providers/<pid>`、`export.md`、`sessions/import`、`lore/promote`、`commit_partial` | 导入导出（README 当卖点）与停止后草稿入库两条路径在接口文档里不存在 | 补 6 行；README:84 改为"主要路由"或补齐 | `grep -c "@app\." rp/api.py`（41）与文档路由行数一致 |
| P1-07 | P1 | 可维护性 | `docs/DATA.md:72-73` ←→ `rp/api.py:757-759,773-780,1312` | 代码 | 文档称回滚时"summary 保留""status 不对回滚"；实现恰恰是清空 summary、`compressed_upto=0`，并按每条消息的 `snap` 做 `_restore_snapshot` | 按文档实现分叉功能会直接复现 `3a9d200` 刚修好的"旧剧情还在上下文里" | 改 L72-73 为清空摘要 + 按 `snap` 恢复现场 | 文档两句与 `check.py` 组 78/90 断言一一对应 |
| P1-08 | P1 | 代码质量 | `rp/api.py`（1496 行）；`:223,246,289,390,708,733,1222,1261` | 代码 | 路由层同时承载业务状态机（`seed_opening`/`_rename_all`/`_bind_story`/`session_to_markdown`/`_rewind_to`/`_commit_turn`）与持久化（6 处直调 `save_session`/`save_story`）；4 个函数集中 187 分支点 | 改一条业务规则要动 HTTP 层；`api.py` 无法单测，自检只能靠 `test_client` 间接覆盖 | 抽 `rp/turns.py`（回合提交/回滚/开场）与 `rp/export_md.py`；`_bind_story` 下沉 `stories.py` | `api.py` < 700 行；`save_session`/`save_story` 只出现在路由内锁块；`check.py` 全绿 |
| P1-09 | P1 | 测试 | `check.py:72-2196` | 命令输出 | `main()` 跨越 2125 行单函数；`check(` 调用 605 次，`assert` **0 个**；`python -m pytest check.py --collect-only` → `no tests collected`，exit 5 | 失败后继续执行、无期望用例数守恒校验，某段被跳过不会使脚本失败；无法单跑某组、无 fixture/隔离/覆盖率 | 不必重写：按现有 89 个编号段切成 `group_NN()` 表驱动，加 `--only` 与段数守恒断言，再包一层 pytest | pytest 收集 ≥ 89 用例；`--only 66` 可单跑；删掉某段后脚本报失败而非静默变绿 |
| P1-10 | P1 | 工程化 | `requirements.txt:1-2` | 命令输出 | 两行 `>=`，无上限/无锁文件/无 hash/无 `.python-version`；实机为全局解释器（`IN_VENV False`，127 个包混居） | 构建不可复现；今日全新安装会得到 requests 2.34.2 / urllib3 2.8.0，与实装的 2.32.5 / 2.5.0 两套依赖树；`flask>=3.1` 放行 Flask 4.x | `pyproject.toml` + `uv lock`，由锁文件生成带 hash 的 `requirements.txt` | `pip install --require-hashes -r requirements.txt` 在干净机器装出逐字一致版本树 |
| P2-01 | P2 | 安全 | `rp/config.py:593`、`:410-415`；`rp/llm.py:48-56` | 代码 | `base_url` 完全用户可控，无 scheme/私网/归一化校验；`provider_endpoint` 再拼一次 `/chat/completions` | 可把出站 POST 指向 `127.0.0.1:任意端口` 或内网服务；`llm.py:153` 会回显状态码，构成内网探测 + 内网 POST 副作用 | 用 `urlparse` 校验 scheme∈{http,https}、拒私网/环回/链路本地；统一去尾路径 | 提交 `base_url=http://127.0.0.1:6379`、`file:///c:/` 均 400；合法端点正常 |
| P2-02 | P2 | 安全 | `rp/config.py:493-499,450-466`；`data/settings.json` | 命令输出 | Key 明文落盘（实测 custom1 长 51、custom2 长 46）；ACL 继承自用户目录未收紧 | 同机任意用户态进程可直读；误传 `data/` 即泄漏。**正面**：`/api/providers` 只下发 `has_key` 布尔，备份包内 Key 一律清空，git 全历史 0 命中 | 改用 DPAPI，或对 `settings.json` 设显式仅当前用户 ACL，Key 独立文件存 | `settings.json` 不再出现明文 Key；`/api/providers` 仍只返回 `has_key` |
| P2-03 | P2 | 安全 | `rp/api.py:1349`、`:1028` | 代码 | `(data.get("message") or "").strip()`——传 `{"message":123}` 时 `123.strip()` 抛 `AttributeError`；无 `@app.errorhandler` | 直接 500 且返回 Flask 默认 HTML 错误页，前端 `errorMessage()` 取不到 `error` 字段 | 抽 `as_str(v)` 辅助函数对每个入参做类型判定后返回 400 | `POST /api/chat {"message":123}` 返回 400 而非 500 |
| P2-04 | P2 | 安全 | `rp/api.py:315-317,601-602`；未配置 `MAX_CONTENT_LENGTH` | 代码 | `TEXT_FIELDS` 无长度上限；import 的 `characters` 不限数量（创建路径 `:329` 有 `[:max_characters]`） | 超大 JSON 整份进内存并落盘；单会话可撑到任意大，叠加下游 token 成本 | `MAX_CONTENT_LENGTH = 2MB`；按字段设上限（title 40 / role 400 / world 8000）；import 复用同一上限 | 2MB 以上请求体 413；`world` 超限被截断且响应可见 |
| P2-05 | P2 | 安全 | `rp/api.py:1436-1438` | 代码 | `metrics.note("status.parse_fail", raw=answer[-300:])` 把剧情原文写进磁盘日志 | 私密创作内容明文长期留在 `logs/`，`grep` 可检索；日志无保留期 | `raw` 改落 `raw_len`/哈希；需要复现时走开关式调试 | `logs/*.log` 不再出现正文子串 |
| P2-06 | P2 | 安全 | `rp/api.py:157-179`、`:114-155` | 代码 | `/api/models` 每次调用都打 Ollama；`/api/search` 每次全量扫所有会话，无频率上限 | 跨站 GET（simple request）可反复触发，造成 Ollama 负载与磁盘 IO | `/api/models` 加 30s 缓存；`/api/search` 加令牌桶 | 连打 50 次 `/api/models` 只触发 1 次 Ollama；超阈值返回 429 |
| P2-07 | P2 | 代码质量 | `rp/api.py:1087-1107` / `:1222-1244` / `:1177-1213,1425-1486` | 代码 | SSE 桥接三套实现；`headers={"Cache-Control":...}` 三处重复；`metrics.record` 只在其中一条路径执行 | 改 SSE 头或错误文案要同步 3 处；enhance 路径的用量从不入账 | `_stream_model` 增 `extra/validate/record_usage` 参数，enhance 改 `yield from`；抽 `_sse_response(gen)` | `mimetype="text/event-stream"` 只剩 1 处；enhance 路径出现 `chat.usage` |
| P2-08 | P2 | 代码质量 | `rp/api.py:644-649` vs `:318-329`；`:40` 硬编码 vs `characters.py:570 LORE_MAX` | 代码 | lore / characters 清洗逻辑两处逐字重复，上限 40 硬编码并存 | 改上限要动 3 处，漏改会造成"新建守 40、编辑守旧值" | 抽 `_clean_lore` / `_clean_characters`，上限引用 `LORE_MAX` | `grep -n "\[:40\]" rp/api.py` 为 0 |
| P2-09 | P2 | 代码质量 | `rp/config.py:332`、`characters.py:22`、`stories.py:104`、`snapshots.py:15` | 代码 | 4 处死代码全项目 0 引用：`_clamp`（`save_writing:354-364` 已手写同一套夹取）、`default_character`、`snapshot_from`、`_lock_reasons` | 读者误以为存在统一入口，改错地方 | 删除前三者，`default_character` 并入 `ensure_characters` | 四个名字在 `rp/` 中 0 命中，`check.py` 全绿 |
| P2-10 | P2 | 可维护性 | `docs/DEVELOPMENT.md:34-39` ←→ `check.py` | 命令输出 | 分组表停在 `65~67`，实际 89 组、编号 0-91（缺 7、60、61），68-91 共 24 组无文档 | 维护者不知道这些不变量已被守护；新增功能无编号可循 | 按实际分组重写表（68-91 逐条一行），或删表指向 `check.py` 段标题 | 文档组号范围与实际段标题集合一致 |
| P2-11 | P2 | 可维护性 | `docs/ARCHITECTURE.md:24,43` | 代码 | 文档称"下层只依赖 config 和 llm，没有循环"；实际 `context.py:6-14` 依赖 characters/metrics/prompts/snapshots/storage，`prompts` 与 `characters` 双向语义耦合。另列了从不 `yield` 的 `final` 事件 | 按文档做模块切分会撞上隐式耦合；客户端为不存在的事件写分支 | 按实测 `grep "^from \.\|^from \."` 重画依赖图；删 `final`，`result` 标注"仅 /api/enhance" | 依赖图与实测边一一对应；事件清单与 `grep "yield sse"` 一致 |
| P2-12 | P2 | 测试 | `rp/api.py:55,483,489,498,516,523,1024` | 命令输出 | 7/41 路由在 HTTP 层零覆盖：`GET /`、4 条备份 CRUD、`PUT /api/backup-config`、`POST /api/enhance`；`grep -i enhance check.py` → 0 命中 | `/api/enhance` 是 60+ 行含三段校验的逻辑，全部零覆盖；`send_file` 下载与 404/500 分支完全未触达 | 补 7 条用例，重点 `/api/enhance` 三种非法 `field`、备份路径穿越 404、`DELETE` 不存在文件 404 | 41/41 路由各至少 1 次 HTTP 调用 + 1 条错误分支断言 |
| P2-13 | P2 | 测试 | `check.py:1489-1544` | 代码 | 605 条断言中约 180 条靠匹配源码/模板字符串（如 `'saveDraftSoon' in _ui67`、`_html67.count('<details class="prefs-sec"') == 4`） | 双向误判：改结构会误报，删 DOM 挂点但留同名字符串仍 PASS；4436 行 JS 行为零自动化 | 源码字符串类断言收敛到一组 `check_frontend_contracts()`；3 条关键交互改用 `node --test` + jsdom 或已在环境的 playwright | 至少 1 个前端用例在删掉事件绑定时失败 |
| P2-14 | P2 | 工程化 | 全局 site-packages | 命令输出 | `python -c "import rp.api"` 报 `RequestsDependencyWarning: urllib3 (2.5.0) ... doesn't match a supported version!`，而 `pip check` 返回 0 | `pip check` 只校验元数据，抓不到运行时兼容性检查；"依赖是否真的兼容"在本环境不可证 | `uv venv .venv` + `uv sync`，CI 亦在干净 venv 内跑 | `import rp.api` 零 warning；`pip list` 仅含锁文件中的包 |
| P2-15 | P2 | 功能性 | `rp/metrics.py:23,60` | 代码 | `_pending` 是模块级全局 dict，`begin()` 调 `_pending.clear()` | 两会话并发聊天时 `chat.first_token` / `chat.done` 的耗时互相覆盖，指标串扰（不影响功能） | `_pending` 改为按 `sid` 的字典，或随会话锁走 | 并发两会话各发一轮，`chat.done.total_ms` 分别正确 |
| P2-16 | P2 | 功能性 | `rp/api.py:889-914` | 代码 | `commit_partial` 的 `load_session`→`append`→`save_session` 未包进会话锁；对比 `:1450-1455` 有锁 | `expect` 版本比对能兜住多数竞态，但与 `/api/chat` 提交并发时可能丢写 | 整段包进 `with get_session_lock(sid):` | 并发压测下不出现消息丢失 |
| P2-17 | P2 | 可维护性 | `README.md:25`、`docs/DEVELOPMENT.md:7` | 代码 | 写作 `PORT=8080 python lt.py`，但 `lt.py:28` 用 `os.environ.get("PORT")`；Windows 需 `$env:PORT=8080` | README 第一条"跑起来"在目标平台直接报 `PORT 不是可识别的命令` | 改为 `$env:PORT=8080; python lt.py   # PowerShell`，附 cmd 写法 | 按 README 在 PowerShell 复制粘贴能起服务 |
| P2-18 | P2 | 性能 | `rp/metrics.py:40-55` | 代码 | 日志按日追加，无轮转/保留期/清理 | 实测 `logs/2026-11-11.log` 已 408KB/天，长期运行无界增长 | 按大小或天数轮转，保留 N 天；用既有 `LT_LOG_DIR` 支持清理入口 | 连续运行 30 天后日志总量有上限 |
| P3-01 | P3 | 安全 | `rp/api.py:1406-1409` | 代码 | 500 响应体 `'保存消息失败：%s' % exc` 把内部异常串回显前端 | 路径与 JSON 结构细节外泄 | 对外固定文案，细节只进日志 | 500 响应 `error` 为固定字符串，不含路径 |
| P3-02 | P3 | 安全 | `static/js/search.js:125` | 代码 | `say('<div class="hint">搜索失败：' + (e.message \|\| e) + '</div>')`，`say()` 走 `innerHTML`（`:91`） | 当前 `/api/search` 不把用户输入拼进 `error`，**未验证可利用**；属 sink 层缺陷，未来任何回显改动会直接变 DOM XSS | 改 `textContent` 或 `escapeHtml` | `e.message` 含 `<img src=x onerror=alert(1)>` 时不执行 |
| P3-03 | P3 | 安全 | `rp/config.py:42-44` | 代码 | 无 `after_request` 注入安全头；`TEMPLATES_AUTO_RELOAD = True` 常开 | 无 CSP / `X-Content-Type-Options` / `Referrer-Policy`，缺 XSS 纵深 | `after_request` 统一注入三个头；自动重载在文档标注为开发便利 | 响应头出现 `X-Content-Type-Options: nosniff` |
| P3-04 | P3 | 代码质量 | `rp/api.py` 多处 | 代码 | `context_chars`+`context_info` 两行装饰重复 10 处；`'"error": "会话不存在"'` 重复 13 处；`with get_session_lock(sid)` 模板重复 6 处 | 改返回结构要动 10 个地方 | 抽 `_decorate(sess)` 与 `_locked_session(sid)` | 两行装饰只出现在 `_decorate` 内；`grep -c "会话不存在"` ≤ 2 |
| P3-05 | P3 | 代码质量 | `rp/api.py:20,32,36`、`characters.py:7`、`context.py:8`、`prompts/__init__.py:12`；`lt.py:9-21` | 代码 | 8 处未使用导入；`lt.py` 的 30 个 re-export 中 `check.py` 只用 8 个 | `api.py:36` 这类被自检打桩依赖的导入行有被 `ruff --fix` 清理的风险；`lt.py` 制造"包外还有公共 API"的假象 | 删未使用导入；`lt.py` 只留真用到的名字 | `python -W error -c "import lt"` 干净；无 `noqa: F401` 堆叠 |
| P3-06 | P3 | 代码质量 | `rp/api.py:258,445-447,658`；`characters.py:409` | 代码 | 状态字段集合 4 处重复硬编码，`STATUS_FIELDS` 未被复用 | 新增字段要改 4 处，漏一处就"模型写了但某视图不显示"——P1-04 就是同一模式的产物 | `characters.py` 出 `STATUS_FIELDS` + `FIELD_LABELS`，`api.py` 全部引用 | `grep -c '"location"' rp/api.py` 为 0 |
| P3-07 | P3 | 代码质量 | `rp/api.py:479` | 代码 | 用 `requests.utils.quote` 做 URL 编码，把 LLM 依赖带进路由层；`check.py:12` 用的是标准库 | `requests` 内部工具函数非稳定 API | `from urllib.parse import quote` | `grep "requests\." rp/api.py` 只剩异常类引用 |
| P3-08 | P3 | 工程化 | 缺失 `.ruff.toml`/`pyproject.toml`/`.pre-commit-config.yaml` | 命令输出 | `ruff check --isolated` → 188 项（57 可自动修）；其中 113 项 `UP031`、11 项 `UP009` 是风格项，真实可疑点 8 处 `F401` + `metrics.py:54 S110` | 无 lint 闸门；`except: pass` 会让事件日志悄悄少记，而这正是 `DEVELOPMENT.md:73-80` 指定的排障入口 | 最小配置 `select = ["F", "S110"]`，`--fix` 清 23 项 | `ruff check` 通过且纳入 CI |
| P3-09 | P3 | 工程化 | `.gitignore` | 代码 | 已正确忽略 `data/`（含 Key）、`backups/`、`logs/`、`__pycache__/`、`.venv/`；缺 `.pytest_cache/`、`.ruff_cache/`、`.coverage`、`htmlcov/` | 当前无实际泄漏；按 P1-09/P3-08 接入工具后会脏仓库 | 补 4 行 | 接入 pytest/ruff/coverage 后 `git status` 仍干净 |
| P3-10 | P3 | 工程化 | `rp/config.py:13`；无 tag | 命令输出 | `VERSION = "0.60"` 硬编码，`git tag` 为空，137 次提交无发布锚点 | 无法从任意 checkout 反查发布；重命名提交未升版本，`VERSION` 与 CHANGELOG 靠人工双写 | release 提交打 `v0.60` tag；或 `importlib.metadata` 读取 | `git describe --tags` 在 release 提交返回 `v0.60` |
| P3-11 | P3 | 可维护性 | `docs/CONFIG.md:9-18`、`README.md:68-76`、`docs/DATA.md:66,92,148` | 代码 | CONFIG 写作参数表漏 `auto_roles`/`language`（声称"id 与常量键一一对应"）；README 数据树缺 `logs/`、`backups/`；DATA.md 有衍字、settings 示例仍写 `every_hours`（实际 `every_minutes`）、`beforerestore`（实际 `before-restore`） | 行为可见项查不到；按示例写配置会走进迁移分支 | 逐条按实现修正 | 文档表 10 行 = `WRITING_DEFAULTS` 键数；示例字段与实际同名 |
| P3-12 | P3 | 代码质量 | `rp/snapshots.py:17` | 代码 | `import threading` 夹在常量与函数之间，其余 9 个模块都遵守"导入在文件头" | 无功能后果，破坏约定 | 移到文件头 | 所有模块导入均在前 15 行内 |
| P3-13 | P3 | 代码质量 | `rp/api.py:202,374,619`、`:1453-1455`、`:708` | 代码 | 路由函数名三种风格并存（`api_config` / `api_save_provider` / `api_set_model`）；`pid`/`sid`/`story_sid` 混用；`sess` 与 `s2` 指同一对象；`_state_snapshot` 与 `snapshots.snapshot` 语义不同但撞名 | 阅读成本与误改风险 | 同一资源同一风格；`s2` 改名 `fresh`；`_state_snapshot` 改名 `_capture_state` | 命名规范写入 `DEVELOPMENT.md` |

---

## 三、分维度详细分析

### 1. 功能性（4/5）

**现状**：41 个路由全部有实现，全项目 `TODO`/`FIXME`/`NotImplemented` **0 处**（唯一命中是 `prompts/__init__.py:611` 提示词里的示例词"XXX"）。错误响应高度统一：`jsonify({"error": "中文提示"}), 4xx`，并按语义区分 400/404/409/500（如 `api.py:542` "这一轮还在生成，等它写完再回滚" 409）。并发控制分两层：`try_acquire_busy` 忙标记 → 409，`StaleSession` 乐观锁 → 409 带 `conflict: True`。数据一致性做得比预期扎实：

- 原子写：`storage.py:101-106`、`stories.py:56-60`、`config.py:462-466` 全部走"独立临时名 + `os.replace`"，注释明确解释了为什么每次用 `uuid` 后缀避免并发写坏同一 `.tmp`。
- 不可逆操作先留退路：`context.py:84 snapshot(sess, "compress")`；`backup.py:167 create("before-restore")`。
- 失败退避：`context.py:148-155` `_COMPRESS_RETRY_SECS = 300`，避免每轮白调一次模型。

**问题**：P1-04（`place`/`location` 键分裂，地点状态在有独立状态更新时丢失）、P2-15（`metrics._pending` 全局串扰）、P2-16（`commit_partial` 读-改-写不在锁内）。

**改进建议**：先修 P1-04 —— 它是唯一影响用户可见结果的缺陷，且修法极小（`characters.py:229-231` 改读写入 `location`）。验收：`grep -rn '"place"' rp/` 只剩别名映射；补一组断言"`refresh_states` 写的地点能被 `ui.js` 渲染与导出读到"。P2-15/16 可一并处理，两者都在会话锁语义内。

### 2. 代码质量（3/5）

**现状**：模块切分清晰（`storage`/`config`/`llm`/`characters`/`prompts`/`context`/`backup`/`snapshots`/`stories`/`metrics` 十一个职责单元），注释写"为什么"而非"做什么"——`storage.py:88` 解释锁的适用边界、`context.py:149` 解释为什么失败要歇五分钟、`backup.py:153-156` 解释还原的两个不能省的步骤。这种注释密度在个人项目里罕见。

**问题**：`api.py` 是唯一的复杂度黑洞。按 AST 统计，4 个函数集中 187 个分支点：`api_chat`（151 行/50 分支）、`api_enhance`（125/54）、`api_update_session`（87/46）、`_commit_turn`（82/42）。`characters.py` 亦有 `parse_turn`（101/51）、`refresh_states`（83/51）。此外 SSE 桥接三套并存（P2-07）、清洗逻辑重复（P2-08）、4 处死代码（P2-09）、状态字段集合 4 处硬编码（P3-06）。

`_clamp`（`config.py:332`）值得单说：它内部完整实现了 `WRITING_BOUNDS` 夹取，而 `save_writing:354-364` 又手写了一遍同样的逻辑，`_clamp` 从未被调用。这是"重构做了一半"的典型痕迹——读代码的人会以为存在统一入口。

**改进建议**：按 P1-08 拆分，优先级为 `api_chat` → `_commit_turn` → `api_enhance` → `api_update_session`。逐个函数的抽分边界已在问题清单给出行号区间。验收：`api.py` < 700 行，且 `save_session`/`save_story`/`load_session` 只出现在路由内的锁块中；每次拆分后 `python check.py` 必须全绿（605 条断言是拆分的安全网，这也是它的主要价值）。

### 3. 测试（3/5）

**现状**：这是本项目最矛盾的一块。`check.py` 2200 行、89 个编号分组、605 次 `check()` 调用，**实测可运行**：`python check.py` → `EXIT=0`、`ALL CHECKS PASSED`，耗时约 1 秒。隔离设计专业：`LT_DATA_DIR`/`LT_BACKUP_DIR`/`LT_LOG_DIR` 三个环境变量把产物导向 `tempfile.mkdtemp`，实测跑完 `data/` 与 `backups/` 无新增文件。模型调用 6 个点全部打桩（`completion_events`、`model_text`、`requests.post`），不发真实网络请求。

更重要的是这些断言**锚定真实历史缺陷**：组 78 断言"回滚要连摘要一起退"、组 90 断言"回滚到开头场景不留残影"、组 82 断言 `add_lore` 不把 `return` 吞进 `for`——每一条都对应一次真实 bug 的修复。

**问题**：形态是 2125 行单函数脚本而非测试套件（P1-09）。零 `assert`，`check()` 只打印并 `append` 到 `FAILED`（`check.py:38-39`），失败后继续执行；全文只在 `:2192` 用到 `len(FAILED)`，**没有"期望用例总数"守恒校验**——某一段被跳过或循环没进，脚本依然变绿。`pytest` 收集 0 项。约 180 条断言靠匹配源码字符串（P2-13），改结构就误报。7/41 路由零 HTTP 覆盖（P2-12）。前端 4436 行 JS 的唯一自动化是 `node --check` 语法检查。

**改进建议**：不重写、不引入框架。最小动作：把 `main()` 按 89 个编号段切成 `group_NN()` 表驱动 + 末尾"段数守恒"断言 + `--only` 参数；这三件事做完，"静默变绿"这一最危险的性质就消除了。验收：删掉任意一段后脚本必须报失败。

### 4. 安全（3/5）

**现状**：四类"硬骨头"全部防住，且是在正确的位置防的。

路径穿越：`storage.py:53-56` `SID_RE.fullmatch(r"[0-9a-f]{12}")` 通过后才拼路径；`stories.py:19-22` 同款；`snapshots.py:22-28` 用 `isalnum()/'-_'` 白名单过滤；`backup.py:110-115` `path_of()` 拒绝 `/`、`\` 且要求文件真实存在；`backup.py:177-187` Zip Slip 用 `normpath` + `startswith(root + sep)` + 强制 `data/` 前缀与 `.json` 后缀。

注入与反序列化：`eval`/`exec`/`subprocess`/`os.system`/`pickle`/`yaml.load`/`__import__`/`shell=True` 在 `rp/`、`lt.py`、`check.py` **全部 0 命中**，JSON 一律 `json.load`。

前端 XSS：`ui.js:10-22` 的 `mdToHtml` 采用正确顺序——先 `escapeHtml` 整体转义，再只放行 `**`/`*`/换行，不拼链接、不拼属性，因此单引号虽未转义也无逃逸面。32 处 `innerHTML` 中绝大多数是 `= ''` 或静态片段，动态拼接点全部经 `mdToHtml`/`escapeHtml`/`textContent`；搜索高亮走 `createTextNode` + `mark.textContent`。

密钥：`/api/providers` 只下发 `has_key` 布尔；备份包内 `api_key` 一律清空（`backup.py:28-38`）；`logs/*.log` 对 5 种密钥模式 **0 命中**；git 全历史无 `sk-*`，`data/settings.json` 从未入库。

**问题**：全部集中在"本机专用"这个假设的边界上。零鉴权本身合理（`lt.py:31` 确认只绑 `127.0.0.1`、`debug=False`），但缺 CSRF/Origin（P1-02）、Host 校验（P1-03）、请求体限额（P2-04）、速率限制（P2-06），让"本机专用"退化成"任意网页可写"。`base_url` 无校验构成 SSRF 面（P2-01）。依赖侧 `urllib3 2.5.0` 命中 HIGH 级 CVE（P1-01），且代码正走 `stream=True`。

**改进建议**：按性价比排序。第一步 P1-01（改一行版本约束，消除一个 HIGH）；第二步 P1-02+P1-03（约 20 行 `before_request`，同时解决 CSRF 与 rebinding，这是投入产出比最高的一处）；第三步 P2-03+P2-04（入参类型与限额）。验收标准见清单，均可 curl 复现。

### 5. 性能（4/5）

**实测**（只读，`import rp.storage` 后直接计时）：

| 指标 | 实测值 | 判定 |
|---|---|---|
| `list_sessions()`（17 会话，421.7 KB 总量） | **4.5 ms** | 线性外推 100 会话约 26 ms，不成瓶颈 |
| `load_session()` 单文件最大 108 KB | **0.5 ms** | JSON 解析成本可忽略 |
| `check.py` 全量 605 断言 | 约 1 秒 | 远快于文档所述"几十秒" |

**静态推断被实测否证的部分**：最初基于 `storage.py:150-161`（`os.listdir` + 对每个文件 `load_session` 全量解析）判断这是列表页瓶颈。实测推翻了该判断——17 个会话 4.5 ms，100 个会话也就 26 ms 量级。这条不成立，撤销。

**仍然成立的性能结论**（静态推断）：`/api/models`（`api.py:157-179`）每次调用都打 Ollama，无缓存；`/api/search`（`:114-155`）每次全量扫所有会话，无频率上限——两者都是跨站 GET 可反复触发的放大面（P2-06，主要按安全计）。日志无轮转（P2-18，实测单日已 408 KB）。

**并发与资源**：SSE 有 `idleTimeout = 240000`（`api.js:55`）与 `reader.releaseLock()`（:112）；后端请求有 `REQUEST_TIMEOUT` 与 `RETRY_TEMP_STEP`（`llm.py:9`）；后台压缩失败有 5 分钟退避（`context.py:149`）；`_busy_sessions` 防止同一会话并发写入。无连接/文件/线程泄漏迹象。

**改进建议**：真正的延迟在模型侧（首 token 时间），而 `metrics.py` 已经在记录 `first_token_ms` / `total_ms` / prefill 占比——**这个项目对性能的可观测性比它的性能本身更好**，不需要额外优化动作。只补 P2-06 的缓存与 P2-18 的日志轮转即可。

### 6. 可维护性与文档（2/5）

**现状**：8 份文档、1089 行，覆盖架构、功能、配置、提示词、数据格式、开发指南、角色设计、更新记录——这个文档完整度在个人项目里属于前列。`docs/CHANGELOG.md` 19 KB 手工维护，`DESIGN-CHARACTERS.md` 6998 B 记录设计决策。

**问题**：文档覆盖面广但追不上代码，且不符之处都是**硬性**的、会让读者写错代码的：

- P1-05：SSE 事件字段全错。文档 `{"type":"delta","delta":"..."}`，实现 `{"type":"delta","content":...,"chars":...}`。照文档写客户端，正文一片空白。
- P1-06：路由表缺 6 条，而 README:84 宣称"全部 HTTP 路由"。
- P1-07：分叉回滚语义与实现**相反**——文档说"summary 保留、status 不回滚"，实现是清空摘要并按每条消息的 `snap` 恢复现场。这正是 `3a9d200` 刚修掉的问题。
- P2-10：`check.py` 分组表停在 67 组，实际 89 组，缺的 68-91 恰好覆盖最近 20 个提交修复的缺陷。
- P2-11：ARCHITECTURE.md 的依赖方向描述与实际依赖边不符，并列出从不 `yield` 的 `final` 事件。
- P2-17：README 第一条"跑起来"用 `PORT=8080 python lt.py`，在 Windows PowerShell 上直接失败。

**可观测性反而是加分项**：`metrics.py` 输出一行一个 JSON 的结构化事件（`chat.request` / `chat.first_token` / `chat.done` / `chat.usage` / `session.stale` / `snapshot.create`），事件命名规范，`session.stale` 带 `expected`/`actual` 两边版本号便于定位竞态；`usage` 拿不到真实值时为 `{}` 而不是拿估算冒充；写日志失败一律吞掉，注释写明"观测不能反过来把主流程弄挂"。

**改进建议**：文档修正不需要架构改动，是纯粹的抄写工作，可以在一个下午内完成并全部纳入 `check.py` 断言（它已有 `_UI_IDS` 四层对账的现成模式）。验收：文档字段名与 `check.py` 断言一致，这样文档漂移会立刻让自检变红。

### 7. 工程化（2/5）

**现状**：能构建、能运行、有版本号（`VERSION = "0.60"`）、提交信息质量**高于多数商业仓库**——137 条中 135 条符合 `type: subject`，subject 精炼，body 详述"症状 → 根因 → 修法"。`.gitignore` 正确排除了含 Key 的 `data/`、`backups/`、`logs/`、`__pycache__`、`.venv`。

**问题**：全部自动化闸门缺失。逐项核实：`.github/workflows`、`Dockerfile`、`Makefile`、`pyproject.toml`、`setup.cfg`、`.ruff.toml`、`setup.py`、`tox.ini`、`.pre-commit-config.yaml`、`.editorconfig`、`LICENSE`、`CONTRIBUTING.md`、`tests/`、任何锁文件、`.python-version` —— **全部 MISSING**。

依赖不可复现是一级问题（P1-10）：`requirements.txt` 两行 `>=`，在全局解释器（127 个包混居）上跑。实测同一份声明今天全新解析会得到 requests 2.34.2 / urllib3 2.8.0，与实装的 2.32.5 / 2.5.0 分岔——**当前运行版本恰好是带 CVE 的那个，新装反而是安全的**。这是"依赖不锁"最直观的代价。

`docs/DEVELOPMENT.md:20` 写着"全绿才提交"，但没有 CI、没有 pre-commit，这句话纯靠自律。审计期间就观察到了它的后果：一个 `BASE_DIR` 未定义的改动进入了工作区（`check.py:2184`，已在 `c21ea29` 修复）。

**改进建议**：成本极低、收益最高的一组动作。`uv` 已在环境（0.13.5），`ruff` 已装（0.17.1）：

1. `pyproject.toml` + `uv lock` → 带 hash 的 `requirements.txt`（消除 P1-01 与 P1-10）
2. `.github/workflows/check.yml` 跑 `python check.py` + `ruff check`（消除 P1-10 的自律依赖）
3. 补 `LICENSE` 与 `CONTRIBUTING.md`（对外可见性所需）
4. 最小 ruff 配置 `select = ["F", "S110"]`，`--fix` 清 23 项

验收：故意注入 `BASE_DIR` 类错误能被 CI 在 60 秒内拦下。

---

## 四、改进路线图

### 立即（本周）—— P1，按依赖顺序

| 序 | 动作 | 映射 | 验收 |
|---|---|---|---|
| 1 | 锁 `urllib3>=2.6.3` 并 `pip install -U`，`pip-audit` 入流程 | P1-01 | `pip-audit -r requirements.txt` 报 0 漏洞 |
| 2 | 加 `before_request` 校验 Origin/Referer + `TRUSTED_HOSTS` | P1-02, P1-03 | `curl -H "Origin: http://evil.test"` → 403；`-H "Host: evil.test"` → 400 |
| 3 | 修 `refresh_states` 的 `place`→`location` | P1-04 | `grep -rn '"place"' rp/` 只剩别名映射；新增一组断言 |
| 4 | 重写 `DATA.md` 的 SSE 事件块、补 6 条路由、改正回滚语义 | P1-05, P1-06, P1-07 | 文档字段与 `check.py` 断言一致 |
| 5 | `pyproject.toml` + `uv lock` + `.github/workflows/check.yml` | P1-10 | 干净机器装出逐字一致版本树；CI 拦下注入错误 |

### 短期（1 个月）—— P1 结构性 + P2 高性价比

| 序 | 动作 | 映射 | 验收 |
|---|---|---|---|
| 6 | `main()` 切成 `group_NN()` 表驱动 + 段数守恒 + `--only` | P1-09 | pytest 收集 ≥ 89 用例；删段后报失败 |
| 7 | 抽 `rp/turns.py` 与 `rp/export_md.py`，`api.py` < 700 行 | P1-08 | `save_session` 只在路由锁块内出现 |
| 8 | 入参类型守卫 + `MAX_CONTENT_LENGTH` + 字段上限 | P2-03, P2-04 | `{"message":123}` → 400；2MB → 413 |
| 9 | `base_url` 校验（scheme + 私网拒绝 + 路径归一） | P2-01 | `http://127.0.0.1:6379` → 400 |
| 10 | 补 7 条零覆盖路由的 HTTP 用例 | P2-12 | 41/41 路由各 1 次调用 + 1 条错误分支 |
| 11 | `uv venv` 隔离 + 消除 `RequestsDependencyWarning` | P2-14 | `import rp.api` 零 warning |
| 12 | 重写 `DEVELOPMENT.md` 分组表、`ARCHITECTURE.md` 依赖图、README 端口写法 | P2-10, P2-11, P2-17 | 文档与实际一一对应 |

### 中期（1 季度）—— P2 剩余 + P3 排期

| 序 | 动作 | 映射 |
|---|---|---|
| 13 | 拆 `api_chat`/`_commit_turn`/`api_enhance`/`api_update_session` 四个热点函数 | P1-08 续 |
| 14 | SSE 桥接收敛为单一 `_stream_model` + `_sse_response` | P2-07 |
| 15 | 抽 `_clean_lore`/`_clean_characters`，统一 `STATUS_FIELDS` 与 `FIELD_LABELS` | P2-08, P3-06 |
| 16 | 前端引入 `node --test` + 3 条关键交互用例，源码字符串断言降级 | P2-13 |
| 17 | Key 改 DPAPI 或显式 ACL；日志去正文、加轮转 | P2-02, P2-05, P2-18 |
| 18 | `/api/models` 缓存、`/api/search` 限速；`metrics._pending` 按 sid 隔离 | P2-06, P2-15 |
| 19 | 删 4 处死代码、8 处未使用导入、`lt.py` re-export 瘦身；ruff 最小配置入 CI | P2-09, P3-05, P3-08 |
| 20 | 补安全响应头、异常串不外泄、`search.js:125` 改 `textContent` | P3-01, P3-02, P3-03 |
| 21 | 文档剩余修正 + `v0.60` tag + 命名规范写入 `DEVELOPMENT.md` | P3-10, P3-11, P3-12, P3-13 |

---

## 五、附录

### 已执行的检查命令与结果摘要

```
git log --oneline -8 / rev-list --count HEAD   → HEAD c21ea29，137 commits，单分支 main，无 tag
git status --short                              → 空（工作区干净）
python check.py                                 → EXIT=0，ALL CHECKS PASSED，约 1s
python -c "import rp.api"                       → IMPORT_OK，伴随 RequestsDependencyWarning(urllib3 2.5.0)
python -m pip check                             → No broken requirements found（抓不到上面的运行时告警）
python -m pip list                              → Flask 3.1.3 / requests 2.32.5 / urllib3 2.5.0 / Werkzeug 3.1.9
python -m pip_audit | python -m safety          → ModuleNotFoundError（均未安装，降级 OSV API）
python -m pytest check.py --collect-only -q     → no tests collected（exit 5）
python -m ruff check --isolated rp lt.py check.py --statistics → 188 errors，57 fixable
node --check（10 个 JS 文件）                    → SYNTAX_FAIL=0（前端唯一自动化）
AST 扫描（24 个 .py）                             → 5286 行；分支点 Top：api_chat 50 / api_enhance 54 /
                                                   parse_turn 51 / refresh_states 51 / _commit_turn 42 /
                                                   api_update_session 46 / session_to_markdown 38
Select-String "@app\.(get|post|put|delete)"      → 41 个路由；鉴权装饰器 0 命中
Select-String eval|exec|subprocess|pickle|yaml.load|shell=True → rp/、lt.py、check.py 全 0 命中
Select-String "innerHTML|insertAdjacentHTML|outerHTML|document.write" → 32 处（动态拼接点全部转义）
Select-String "sk-[A-Za-z0-9]{16,}|api_key|Bearer|token=" logs\*.log → 0 命中
Select-String "灵枢"（重命名残留）               → 仅 check.py:2187（故意的重命名断言）
curl api.osv.dev/v1/vulns/GHSA-38jv-5279-wg99   → HTTP 200，aliases 含 CVE-2026-21441 / PYSEC-2026-1996，
                                                   range [1.22 → 2.6.3)，severity HIGH，CWE-409
curl pypi.org/pypi/urllib3/json                 → 最新 2.8.0（2.6.3 之后已有 2.7.0、2.8.0）
实测 list_sessions()                            → 4.5 ms / 17 会话
实测 load_session(108KB)                        → 0.5 ms
```

### 未能覆盖的范围及原因

- **未启动服务做真实 HTTP 验证**：只读约束下启动 `lt.py` 会写 `data/` 与 `settings.json`。因此 CSRF、DNS rebinding、SSRF 三条结论全部基于**静态代码路径推演**，未做实证。
- **CVE 可利用性未实证**：仅完成"版本范围命中 + 代码路径命中"两段静态匹配，未构造恶意重定向端点验证 OOM。
- **无第二漏洞扫描器交叉验证**：`pip-audit` 与 `safety` 均未安装，结论单一来源（OSV 官方 API，已人工核对 `affected.ranges` 与实装版本）。
- **无覆盖率数据**：`coverage` 未安装。P2-12 的"7/41 路由零 HTTP 覆盖"是源码字面量静态判定，不等于行覆盖率。
- **前端行为零验证**：4436 行 JS 只有语法检查，无浏览器或 jsdom 执行。
- **`data/`、`backups/`、`logs/` 业务内容未阅读**：按要求只核对结构与敏感字段是否存在，不读用户创作正文（唯一例外是核对日志是否泄漏 Key 的模式匹配，输出只报命中数）。
- **其他 Windows 用户对 `settings.json` 的可读性未枚举**：ACL 只列出 SYSTEM/Administrators/当前用户。

### 本次审计的自我修正记录

保留以下记录以示结论可信度：

1. 最初基于 `storage.py:150-161` 静态推断"`list_sessions` 全量解析是列表页瓶颈"，实测 4.5 ms/17 会话后**撤销**该判断。
2. 曾怀疑 `api.js:105` 会把用户主动停止误报为"超过 240 秒没有新进度"，核查 `chat.js:371-381` 的 `state.stopped` 优先分支后**撤销**。
3. 一度用字面量匹配比对前后端路由，得出"18 条后端路由未被前端引用"——该方法未考虑前端字符串拼接构造动态路径，属**误报**，已弃用改为读 `api.js` 封装层。
4. 工程组报出的 P0（`check.py:2184` `NameError`）经验证是**已修复的中间态**：当前 HEAD 该行已用 `pathlib.Path(__file__).parent`，`BASE_DIR` 残留 0 处，`python check.py` 全绿。故不计入问题清单，改为在工程化维度说明"无 CI 导致此类改动可进入工作区"。

### 复现步骤（针对关键 P1）

```
# P1-01 依赖漏洞
python -c "import urllib3; print(urllib3.__version__)"   # 2.5.0（< 2.6.3）
curl -s https://api.osv.dev/v1/vulns/GHSA-38jv-5279-wg99 | findstr CVE-2026-21441

# P1-02 CSRF（需服务运行，本次未执行）
curl -X POST -H "Origin: http://evil.test" http://127.0.0.1:5000/api/writing/reset
# 预期当前：200（无防护）；修复后应为 403

# P1-04 地点键分裂
findstr /n "\"place\"" rp\characters.py        # 229-231 写入
findstr /n "STATUS_FIELDS" rp\characters.py    # 409 只含 location

# P1-09 测试形态
python -m pytest check.py --collect-only -q    # no tests collected
findstr /c:"assert " check.py                  # 0 命中
```

---

## 六、架构决策建议（数据库与依赖取舍）

本章回答两个选型问题：**要不要上数据库**、**要不要引入其它开源项目**。结论与依据均来自本次实测，与第四章的缺陷修复路线图互补（那章修问题，这章定方向）。

### 6.1 数据库：不需要，引入是负收益

三个实测数字直接否掉这个方向：

| 指标 | 实测值 | 说明 |
|---|---|---|
| 会话规模 | 17 个会话 / 421.7 KB | 单机个人使用量级 |
| `list_sessions()` 全量解析 | **4.5 ms** | 线性外推 100 会话约 26 ms |
| 最大单会话（108 KB）解析 | **0.5 ms** | JSON 解析成本可忽略 |

SQLite 的引入门槛是"并发写 + 复杂查询 + 数据量超内存"，本项目三个都不占。

更关键的是，换掉 JSON 会**毁掉现有的三个真实优势**：

1. 用户能直接打开 `data/<sid>.json` 手改剧情、修坏掉的一条消息；
2. 整个 `data/` 目录复制到另一台机器就是迁移，无需导出导入；
3. 备份就是一个 zip（`backup.py:50-89`），还原出错时从包里挑单个文件就能救回。

换成 SQLite 后这三项全部归零，还要额外引入迁移工具与 schema 管理。

它的数据一致性也不依赖数据库：`storage.py:31` 按会话分 `RLock`、`storage.py:101-106` 独立临时名 + `os.replace` 原子写、`StaleSession` 乐观锁（`storage.py:95-98`）挡住多标签页并发写、`backup.py:167` 还原前先打 `before-restore` 保险包。这套机制已经解决了单进程单用户下的全部一致性问题。

**触发迁移的阈值**（任一满足再考虑）：会话数到几千、需要跨会话的多条件结构化查询、需要多进程/多设备并发写、数据总量进入数百 MB 量级。

### 6.2 全文搜索：数据库当索引，不当存储

唯一将来会受益于索引的地方是 @@GET /api/search`（`api.py:114-155`），它现在每次全量扫所有会话文件。但 421 KB 规模下全量扫描是毫秒级，此时上倒排索引是过度设计。

真要上，正确做法是**旁路索引**：JSON 仍是唯一真相源，另建 `data/index.db` 只存 `sid + speaker + 正文摘要`，搜索走 SQLite FTS5，命中后用 sid 回 JSON 读全文。索引可随时删除重建，不承担数据风险；JSON 的可读可迁移优势全部保留。

建议在搜索耗时超过 100 ms 时再动。

### 6.3 不需要外部项目、但值得做的三项改进

以下三项本次审计新发现，**不需要任何新依赖**。

**（1）重试逻辑把两类失败混为一谈**（`rp/llm.py:136-155`）

`completion_events` 用同一个循环同时处理"内容太短要重试"与"接口返回非 200"，且 `continue` 前没有任何退避：

```python
if resp.status_code != 200:
    last_error = "接口返回 %s" % resp.status_code
    continue        # 立即重试，温度 +0.1
```

两个问题叠加：模型服务挂掉或返回 429 时，会在几毫秒内连打满 `MAX_CHAT_ATTEMPTS = 3` 次；而 5xx 属传输故障，提温度（`base_temp + RETRY_TEMP_STEP * (attempt - 1)`，`llm.py:149`）对它毫无意义——升温只对"生成质量不达标"有效。

修复：按状态码分流。非 200 时 `time.sleep(0.5 * 2 ** (attempt - 1))` 且**保持温度不变**；质量重试才升温。
验收：mock 一个返回 500 的端点，3 次重试总耗时应 > 1.5 s 而非瞬间完成，且三次温度相同。

**（2）HTTP 连接未显式释放**（`rp/llm.py:47-58`）

`resp = _post(payload)` 不是 context manager。正常路径读尽 body 后连接会回连接池，但客户端中断时 generator 被 close（`api.py:1474-1478` 已正确处理 `GeneratorExit`），`resp` 没有 `close()`，依赖 GC 回收。

修复：在 `_stream_model` 内套 `try/finally: resp.close()`。影响很小（本地、单用户、连接池会回收），属正确性问题而非性能问题。
**未验证**：未构造断连场景实测 CLOSE_WAIT 是否堆积，此条为静态推演。

**（3）`settings.json` 缺整体 schema 版本**（`rp/config.py`）

`writing_version` 的存在说明已有迁移意识（写作参数可演进），但 settings 整体没有版本字段。加一个 `settings_version`，将来改结构时才有据可依，否则只能靠键名嗅探判断。验收：`load_settings` 能按版本号分支处理旧格式。

### 6.4 值得引入的开源项目：只有四个，且都已在环境里

判据是"零安装成本 + 直接消除一类已证实风险"：

| 项目 | 状态 | 消除的风险 | 对应问题 |
|---|---|---|---|
| `uv` 0.13.5 | 已装 | 依赖不可复现；今日全新安装会装出 urllib3 2.8.0，与实装 2.5.0 分岔 | P1-10, P1-01 |
| `ruff` 0.17.1 | 已装 | `F401` 自动清理 `rp/api.py:36` 这类被打桩依赖的导入行，导致自检静默失效并真实联网 | P3-05, P3-08 |
| `pip-audit` | 需装一次 | CVE 检查无法固化，本次只能降级为手工比对 OSV | P1-01 |
| `pytest` | 需装一次 | 给 `check.py` 套"失败即停 + 可单跑"，本身不重写断言 | P1-09 |

### 6.5 明确建议不要引入的

| 候选 | 否决理由 |
|---|---|
| SQLAlchemy 或任何 ORM | 把可读可手改的 JSON 变成不可读库文件，换来用不到的查询能力 |
| FastAPI + Pydantic | 41 个只跑在 `127.0.0.1` 的路由不需要整套校验框架；`request.get_json` 加十几个 `isinstance` 即可 |
| 前端框架（Vue/React） | 4436 行原生 ES Module 已在跑，`mdToHtml` 的转义顺序还是对的；重写是净损失 |
| Alembic 等迁移工具 | 没有 schema 就没有迁移（若按 6.1 维持 JSON 存储） |

### 6.6 投入产出比排序

1. 拆 `api.py`（1496 行吞掉全部业务与持久化，是唯一阻碍单测的结构问题）
2. `check.py` 按 89 个编号段切分 + 段数守恒断言（消除最危险的"某段被跳过仍全绿"）
3. CI + 锁依赖（`DEVELOPMENT.md:20` 那句"全绿才提交"目前靠自律，审计期间已因此漏进过一个 `NameError`）
4. 重试加退避（6.3 第 1 项，真实稳定性问题）
5. 文档对齐 SSE 字段与路由表（纯抄写工作）

前四项都不需要新增任何第三方依赖。这个项目的真正短板不是"缺工具"，而是**没有闸门**——所以补工具的重点应是让已有的 605 条断言和 41 个路由无法被悄悄绕过，而不是引入更多能力。

---

## 审查结论

这是一个工程质量明显高于同规模个人项目的代码库——路径穿越、注入、XSS、密钥外泄四类高风险面全部在正确位置防住，原子写与乐观锁齐备，可观测性设计专业，605 条断言锚定真实历史缺陷。它的短板不在"写得对不对"，而在"没有闸门"：零 CI、依赖不锁、文档追不上代码、唯一测试是 2125 行单函数脚本。补齐这四项的投入产出比，远高于重构那 4 个热点函数。
