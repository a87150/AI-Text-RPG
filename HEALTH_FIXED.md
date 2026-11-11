# HEALTH_REPORT 修复对照表

基线：`HEAD = dcf5ad3`。报告共 40 条（P1×10、P2×18、P3×13 减重叠），修 22 条，其余 18 条按判断不动，逐条写明理由。

## 已修（22）

| ID | 级别 | 改了什么 | 怎么验证的 |
|---|---|---|---|
| P1-01 | P1 | `requirements.txt` 加 `urllib3>=2.6.3` | 版本下限写入文件，`check.py` 组 93 断言 |
| P1-02 | P1 | `rp/api.py` 加 `before_request`，写请求的 Origin/Referer 必须是本机 | 实测 `curl -X POST -H "Origin: http://evil.test" /api/writing/reset` → **403** |
| P1-03 | P1 | `rp/config.py` 设 `TRUSTED_HOSTS` | 实测 `Host: evil.test` → **400** |
| P1-04 | P1 | `refresh_states` 的地点写进 `location`，不再写 `place`；读时拿 `location` 兜底 `place` | 组 81 断言改判 `location`；实测旧数据 `place:"厨房"` 仍能读到 |
| P1-05 | P1 | `docs/DATA.md` 的 SSE 字段按实现重写：`delta.content`、`thinking.chars`、`done` 真实字段 | 组 93 断言文档里出现 `"content": "正文增量"` |
| P1-06 | P1 | 路由表补 6 条（`commit_partial`/`DELETE providers`/`writing/reset`/`export.md`/`sessions/import`/`lore/promote`） | 文档 41 行 = 代码 41 条，组 93 断言相等 |
| P1-07 | P1 | 回滚语义按实现改写：清空 summary、按 `snap` 恢复场景、lore 按轮次过滤 | 与组 90 的 8 条断言一一对应 |
| P1-10 | P1 | `flask>=3.1,<4` / `requests>=2.32.5,<3`，不让大版本漂 | 文件本身 |
| P2-03 | P2 | 新增 `_str(data, key)`，17 处 `(data.get(x) or "").strip()` 全换；传数字不再炸 | 实测 `POST /api/chat {"message":123}` → **400**（原来 500） |
| P2-04 | P2 | `MAX_CONTENT_LENGTH = 2MB` | 配置生效，超限由 Flask 返回 413 |
| P2-05 | P2 | `status.parse_fail` 只记 `raw_len` + 末 40 字（去换行），不再写 300 字正文 | 组 93 断言 `raw=answer[` 不存在 |
| P2-09 | P2 | 删 `_clamp`、`snapshot_from`、`_lock_reasons` 三处死代码 | 全库 0 命中，`check.py` 全绿 |
| P2-15 | P2 | `metrics._pending` 改按 `sid` 分槽，两个会话同时聊天不再互相覆盖首字耗时 | `begin/first_token/end` 按 sid 存取 |
| P2-16 | P2 | `commit_partial` 整段包进 `get_session_lock` | 读-改-写不再和 `/api/chat` 交错 |
| P2-17 | P2 | README 的换端口写法改成 PowerShell `$env:PORT=8080; python lt.py`，另给 cmd 写法 | 粘贴可用 |
| P2-18 | P2 | 日志超过 14 天在进程首次写日志时清一次 | `KEEP_DAYS = 14`，跑不了失败也吞掉 |
| P3-01 | P3 | 500 不再回显异常串，改固定文案 + `session.save_fail` 进日志 | 组 93 断言 |
| P3-02 | P3 | `search.js` 的 `say()` 从 `innerHTML` 改成 `textContent` | `node --check` 通过；失败文案不再拼 HTML |
| P3-03 | P3 | `after_request` 注入 `X-Content-Type-Options: nosniff` 与 `Referrer-Policy: no-referrer` | 实测响应头里读到了 `nosniff` |
| P3-07 | P3 | `requests.utils.quote` → `urllib.parse.quote` | 路由层不再依赖 requests 内部工具函数 |
| P3-09 | P3 | `.gitignore` 补 `.pytest_cache/`、`.ruff_cache/`、`.coverage`、`htmlcov/` | 文件本身 |
| P3-12 | P3 | `snapshots.py` 的 `import threading` 挪到文件头 | 导入都在前 15 行内 |

## 判断后不动（18）

| ID | 级别 | 不修的理由 |
|---|---|---|
| P1-08 | P1 | 把 `api.py` 拆成 `turns.py` + `export_md.py`。1496 行里同时住着路由和状态机，拆是对的，但这是纯位移，且要连带动 17 处调用点和全部 41 条路由的自检路径。**收益是阅读体验，风险是把一个能跑的东西拆坏**。等你哪天真要加功能被这块挡住时再拆，那时有具体需求兜底。 |
| P1-09 | P1 | `check.py` 切成 89 个 `group_NN()` + pytest。同理：605 条断言现在是真的在跑，改成表驱动要重排 2125 行。真要做的价值在"能单跑某组"，那是开发体验，不是正确性。 |
| P2-01 | P2 | 给 `base_url` 拒私网/环回。**这条修了会直接打断正常使用**：本地 Ollama 就是 `127.0.0.1:11434`，局域网模型机也是私网。报告把它当 SSRF，但威胁模型是"用户自己填自己的地址"，没有第三方能填。 |
| P2-02 | P2 | Key 改 DPAPI 加密。本机单用户自用，`data/` 已 gitignore，备份包里的 Key 已经清空，`/api/providers` 只回 `has_key`。加密要处理迁移和"换机后解不开"，收益低于代价。 |
| P2-06 | P2 | `/api/models` 缓存、`/api/search` 限速。这两条的攻击面是"跨站 GET 反复打"，P1-02 修完后跨站请求已经进不来；本机自用不存在第三方刷。 |
| P2-07 | P2 | 三套 SSE 桥接合并。真实的痛点只有一个：`/api/enhance` 的用量没进 `metrics`。但合并要动三条流式路径，是重构不是修 bug，先记着。 |
| P2-08 | P2 | lore / characters 清洗逻辑重复、上限 40 硬编码。重复是真的，但两处当前行为一致，不产生 bug。属于"下次改到这块时顺手抽"，不值得单独一轮。 |
| P2-10 | P2 | `docs/DEVELOPMENT.md` 分组表停在 67、实际 89 组。文档便宜，但这一份是给"要改自检的人"看的，当前只有我在改。**你要的话我五分钟补完**，不主动动是因为它会和下一次加组再次漂移。 |
| P2-11 | P2 | 重画依赖图。现在的图不准，但不影响任何行为；重画得先 `grep` 出全部 `from .` 边，属于好的做法、不是修复。 |
| P2-12 | P2 | 补 7 条零覆盖路由的用例。有价值，尤其 `/api/enhance`（60 行三段校验零覆盖）。但这是**加测试**不是修缺陷，且我改 `/api/enhance` 的入参处理时已顺带验证过一次。留作下一轮。 |
| P2-13 | P2 | 前端契约断言收敛 + 引入 jsdom。4436 行 JS 只有语法检查，确实薄。但引入前端测试栈是新增依赖 + 新增维护面，得你点头。 |
| P2-14 | P2 | 全局解释器混装。**`start.ps1` 已经解决**——现在跑起来就是 `.venv` 里的干净环境。 |
| P3-04 | P3 | 抽 `_decorate` / `_locked_session`。10 处重复是真的，但抽出来要改 10 个 return 点，改动面比重复本身大。 |
| P3-05 | P3 | 删未使用导入。**报告自己警告过**：`api.py:36` 那个导入被 `check.py` 打桩依赖，`ruff --fix` 清掉会让自检静默失效并真实联网。只删了 `check.py` 里一个确定无用的 `import re as _re`。 |
| P3-06 | P3 | `STATUS_FIELDS` 复用。P1-04 修完后地点已经统一，剩下的字段集合重复暂时不产生行为差异。 |
| P3-08 | P3 | 接 ruff 配置 + `--fix`。188 项里 124 项是 `UP031`/`UP009` 风格项，真实可疑 8 处。不接 CI 的话配了也没人跑。 |
| P3-10 | P3 | git tag。一条命令的事，但打哪个提交是发布决策，等你定版本节奏。 |
| P3-11 | P3 | `docs/CONFIG.md` 写作参数表漏 `auto_roles`/`language`、README 数据树缺 `logs/`。改了 DATA.md 里那处衍字，剩下的表和代码对不上的部分是**少了**不是**错了**，不影响按文档操作。 |

## 顺带发现（报告没提）

`_rewind_to` 里 `facts` 的过滤原本是 `turn < max(mark, 1)`，那个 `max(mark, 1)` 会让"退到开头"时保留第一轮的全部事实——正是被撤销那一轮产生的。已在 `3a9d200` 一并改成严格比较，这里记一笔因为报告只写了 surface 症状没点到根因。

## 复审后补修（3 条，2026-11-12）

上一版的 CSRF 守卫留了 no-referrer 绕过：跨站 `<form>` 本来就不发 `Origin`，只有 `Referer`；
攻击页加一行 `<meta name="referrer" content="no-referrer">` 两个头就都没了，`if not origin: return None`
直接放行。改成不依赖任何请求头：写方法必须 `Content-Type: application/json`，跨站表单发不出这个类型
（发得出就要过预检，预检过不了）。`Origin`/`Referer` 校验保留作为纵深。前端 `api.js` 全带这个头，
只有 `backup.js` 一处裸 `fetch` 补了头，`check.py` 的 client 用 `environ_base` 统一带上。

urllib3 那条之前是假绿：断言查的是 `requirements.txt` 文本，环境里还跑着 2.5.0。已 `pip install -U`
到 **2.8.0**，断言改成读 `urllib3.__version__` 比对 `(2, 6, 3)`。

`_r5.log` / `_r5.log.err` 确实被提交进去了（`.gitignore` 只盖 `_*.py`、`_*.txt`）。已 `git rm --cached`
并从磁盘删除；`.gitignore` 补 `_*.log`、`_*.log.*`。查过内容：只有 Flask 开发服务器访问日志和一个
会话 id，没有 Key、没有路径、没有正文。

复审提出的另一个知情项保留原样：`status.parse_fail` 记的 `raw_tail` 是 40 字剧情，仍是正文。
本地临时排障够用；哪天日志要当长期资产再换哈希。
