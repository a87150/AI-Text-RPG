# 架构

## 一句话

Flask 提供 JSON/SSE 接口，前端是零构建的原生 ES modules，所有状态存在 data/ 的 JSON 文件里。没有数据库、没有缓存层、没有消息队列。

## 分层

    templates/index.html        单页外壳，只有 DOM 结构
    static/css/app.css          全部样式，含移动端抽屉
    static/js/*.js              前端模块（原生 ES modules，浏览器直接加载）
            │  fetch / SSE
    rp/api.py                   HTTP 路由层：参数校验、加锁、拼 SSE 响应
    rp/llm.py                   与模型通信：流式解析、重试、剥离思考与状态残渣
    rp/prompts/                 提示词：system 怎么拼、状态格式、规则文案
    rp/characters.py            回合解析：把模型输出拆成旁白/台词/状态/新角色
    rp/context.py               上下文长度计算与压缩
    rp/storage.py               会话 JSON 读写 + 每会话一把锁
    rp/stories.py               故事 JSON 读写、建局时快照给会话
    rp/snapshots.py             存档点
    rp/covers.py                封面：上传落盘、缩略图、后台生图任务
    rp/backup.py                data/ 打包成 zip
    rp/config.py                常量、模型配置、写作参数、档位、持久化

依赖方向是单向的：api 依赖下面所有模块，下面的模块只依赖 config 和 llm，没有循环。

## 一次对话的完整流程

用户在输入框按回车，chat.js 走 POST /api/chat（SSE）：

1. **加锁**：try_acquire_busy(sid)。同一会话同时只能有一轮在生成，第二个请求拿 409。
2. **落用户消息**：load_session → 追加 user 消息 → save_session。首次发言顺便用它当标题。
3. **确定场面**：读状态里的在场 / 离场名单，把角色卡分成「在场完整展开」「不在场只列名字」两组。
   圆桌式之下不再由代码指定发言人——next_speaker() 恒返回空串，谁开口由模型按剧情决定。
4. **拼提示词**：build_messages(sess, speaker) 生成 system + 历史，最后一条 user 末尾补一行状态格式提醒。
5. **调模型**：completion_events() 逐块吐出 thinking/delta，失败自动重试（最多 MAX_CHAT_ATTEMPTS），太短也当失败。
6. **解析**：parse_turn(text, speaker, names) 把输出拆成旁白、台词、状态、新角色、变化。
   角色扮演模式下每个【名字】段落各收一条进 lines；一般模式整段算旁白。
7. **落盘**：_commit_turn() 写消息（含 lines）、合并状态、合并事实账本、登记新角色、
   按在场名单给没登记过的人补建角色卡、轮次 +1、每 15 轮存快照。
8. **回传**：done 事件带上下文占用、角色表、状态，前端据此重绘。
9. **后台压缩**：background_compress(sid) 在线程里跑，不阻塞这次响应。

SSE 事件类型：speaker（这轮谁说话）、thinking（模型的思考，可折叠）、delta（正文增量）、retry（重试中，带次数）、result、final、done、error。

## 封面生成的一轮

路径与普通回复完全分开，它不走模型，也不写会话 JSON 的正文：

1. POST /api/stories/<id>/cover/gen 把提示词和 job id 丢进内存，开一个 daemon
   线程，**立即**返回 202。不等图。
2. 线程里埋头做：改工作流的提示词和种子 → POST ComfyUI /prompt → 轮询 /history/<id>
   直到有图 → 定位它落在 data/covers/ 的哪个文件 → 生成缩略图 → 写回故事的
   cover / cover_history → 把 job 标成 done。
3. 前端每 2 秒 GET /api/stories/<id>/cover/job ，看到 done 就换图。失败也从这个接口拿到
   错误文本。

关键约束：**工作流直接写进 data/covers/，不再从 ComfyUI 的 output 拉一份。**
两边各写一份曾经导致每次生成多一张废图。因为 Windows 上 ComfyUI 还没放句柄，
定位到的文件就地用，不重命名也不复制。

## 为什么要加锁

两件事会同时发生：用户连按回车，以及后台压缩线程正在写同一个会话文件。storage.py 给每个会话 id 一把 RLock，所有读改写都在这把锁里。写文件用「临时文件 + os.replace」，进程被杀也不会留下半个 JSON。

## 前端模块

    main.js         装配层：初始化、事件绑定、设置面板、档位与模型选择
    chat.js         发送、流式接收、失败重试
    ui.js           渲染：消息、状态卡、上下文面板、主题字号
    sessions.js     会话与故事列表、分页、视图切换、刷新后的位置记忆
    settings.js     故事设定表单、单项 AI 生成、开新局
    msgops.js       重写、编辑、分叉
    search.js       全文搜索面板
    cover.js        封面：大图弹窗、生成进度轮询、管理列表、只读态
    backup.js       备份面板
    api.js          统一请求封装与 SSE 解析
    state.js        全局状态对象

模块之间用 state 共享数据，用回调互相调用，没有框架也没有响应式系统。改一处要手动调对应的 render —— 这是刻意的：页面结构简单，引入框架的成本高于收益。

## 上下文压缩

context.py 按字符数估算占用（中文字符约 0.7 token）。两个闸门任一触发就压：字数超过 context_limit，或消息条数超过当前档位的历史条数。

压缩把最早的一批消息交给模型写成摘要，累加进 sess.summary，然后把游标 compressed_upto 往前推。**消息本身不删**——只推游标。这一点很重要：分叉、回溯、导出、全文搜索都还要读全文，删了就没了。摘要以「前情提要」的形式重新进入 system 提示。

保留区间从「压缩后保留最近消息数」起算，并对齐到一条 user 消息上，避免把一问一答切开。压缩在后台线程里做，用户感知不到停顿。

## 快照

snapshots.py 把整个会话 JSON 复制到 data/snapshots/<会话 id>/<时间戳>-<原因>.json。触发点：每 15 轮一次、每次改设定前一次。每个会话只留最近 10 份。回滚时整体替换；生成中会拒绝（409），以免覆盖正在写的回合。
