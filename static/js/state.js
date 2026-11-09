/**
 * 全局可变状态。刻意保持扁平：模块直接读写它，
 * 改完状态后调用 ui.syncComposer()/syncHeader() 同步视图。
 */
export const state = {
  session: null,      // 当前故事（后端会话对象）
  sessions: [],       // 侧栏：对话列表
  stories: [],        // 侧栏：故事（设定）列表
  sideView: 'stories',   // 侧栏当前视图：stories | sessions
  selectedStory: '',     // 故事视图里正在编辑的那份
  pageSize: 10,
  pickPage: 0,
  storyPage: 0,          // 故事列表当前页
  sessionPage: 0,        // 对话列表当前页
  busy: false,        // 是否正在生成回复
  aiStatus: { text: '', kind: '' },  // 常驻状态栏：构思中 / 重试 / 失败 / 完成
  model: '',          // 当前模型名
  current: null,      // { provider, model }
  providers: [],      // [{ id, label, kind, base_url, models, has_key }]
  conf: null,         // /api/config 原样：模型 + 写作参数
};

/** 千分位数字，用于上下文统计展示。 */
export const fmtNumber = (n) => Number(n || 0).toLocaleString('zh-CN');
