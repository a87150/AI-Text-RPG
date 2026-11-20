/** 视图层基元：DOM 工具、Markdown 转义、提示条、主题、输入区与消息渲染。 */
import { api } from './api.js';
import { state, fmtNumber } from './state.js';
import { loadSessions } from './sessions.js';
// 循环引用：settings 也 import 本模块，靠函数声明提升在运行期解析
import { openAsk } from './settings.js';

export const $ = (sel) => document.querySelector(sel);

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) => ESCAPES[c]);
}

/** 极简 Markdown：先整体转义，再只放行粗体/斜体/换行。 */
export function mdToHtml(s) {
  let h = escapeHtml(s);
  h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  h = h.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  h = h.replace(/\n/g, '<br>');
  return h;
}

let toastTimer = 0;
export function toast(msg, kind = '') {
  let el = $('#toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = 'show' + (kind ? ' ' + kind : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = kind ? kind : ''; }, 3200);
}

/** 只差这一点就认为用户还贴在底部，愿意跟着新消息走。 */
const STICK_PX = 80;

function nearBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight < STICK_PX;
}

function showJump(show) {
  const btn = $('#jump-latest');
  if (btn) btn.classList.toggle('hidden', !show);
}

/**
 * force=true：刚发完消息、刚切会话这种必须看到底部的情况。
 * 否则只在用户本来就贴底时才跟随，往上翻就出「新消息」浮标，别把人拽走。
 */
export function scrollToBottom(force = false) {
  const box = $('#messages');
  if (!box) return;
  if (!force && !nearBottom(box)) {
    showJump(true);
    return;
  }
  box.scrollTop = box.scrollHeight;
  showJump(false);
}

/* ------------------------------ 主题 ------------------------------ */
const THEME_KEY = 'rp-theme';

export function applyTheme(theme, persist = true) {
  const name = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = name;
  const btn = $('#btn-theme');
  if (btn) {
    btn.textContent = name === 'light' ? '☀' : '🌙';
    btn.title = name === 'light' ? '切换到暗色模式' : '切换到亮色模式';
  }
  if (persist) {
    try { localStorage.setItem(THEME_KEY, name); } catch (e) { /* ignore */ }
  }
}

export function initTheme() {
  let saved = '';
  try { saved = localStorage.getItem(THEME_KEY) || ''; } catch (e) { /* ignore */ }
  const prefersLight = window.matchMedia
    && window.matchMedia('(prefers-color-scheme: light)').matches;
  applyTheme(saved || (prefersLight ? 'light' : 'dark'), false);
}

export function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');
}

const FONT_KEY = 'rp-font';
const FONT_SIZES = ['sm', 'md', 'lg', 'xl'];

export function applyFont(size, persist = true) {
  const name = FONT_SIZES.includes(size) ? size : 'md';
  document.documentElement.dataset.font = name;
  const sel = $('#prefs-font');
  if (sel) sel.value = name;
  if (persist) {
    try { localStorage.setItem(FONT_KEY, name); } catch (e) { /* ignore */ }
  }
}

export function initFont() {
  let saved = '';
  try { saved = localStorage.getItem(FONT_KEY) || ''; } catch (e) { /* ignore */ }
  applyFont(saved || 'md', false);
}

/* ------------------------------ 输入草稿 ------------------------------ */
// 切会话、刷新、分叉之后输入框被重建成空的，写了一半的话就没了。
// 按会话 id 存本地，输入时 debounce 500ms 写，进会话时读回来。
const DRAFT_PREFIX = 'rp-draft:';
let draftTimer = 0;

export function saveDraftSoon() {
  const el = $('#input');
  const sid = state.session && state.session.id;
  if (!el || !sid) return;
  clearTimeout(draftTimer);
  const text = el.value;
  draftTimer = setTimeout(() => {
    try {
      if (text.trim()) localStorage.setItem(DRAFT_PREFIX + sid, text);
      else localStorage.removeItem(DRAFT_PREFIX + sid);
    } catch (e) { /* 隐私模式存不下就算了 */ }
  }, 500);
}

/** 发送成功后要清掉：那句话已经进了对话记录，要重发直接点重试。
 * 顺手清掉定时器：输入后 500ms 内就发送时，那次尚未执行的写入会把旧文本又写回去。
 */
export function clearDraft(sid) {
  if (!sid) return;
  clearTimeout(draftTimer);
  try { localStorage.removeItem(DRAFT_PREFIX + sid); } catch (e) { /* ignore */ }
}

export function loadDraft(sid) {
  try {
    return (sid && localStorage.getItem(DRAFT_PREFIX + sid)) || '';
  } catch (e) {
    return '';
  }
}

/* ------------------------------ 焦点 ------------------------------ */
export function modalOpen() {
  for (const sel of ['#settings-modal', '#prefs-modal', '#ctx-modal']) {
    const m = $(sel);
    if (m && !m.classList.contains('hidden')) return true;
  }
  return false;
}

/**
 * 把光标送回输入框。
 * 设定弹窗打开时不抢焦点；输入框禁用（没选故事）时也不抢。
 */
export function focusComposer({ force = false } = {}) {
  const el = $('#input');
  if (!el || el.disabled) return;
  if (!force && modalOpen()) return;
  if (document.activeElement === el) return;
  el.focus({ preventScroll: true });
  const end = el.value.length;
  try { el.setSelectionRange(end, end); } catch (e) { /* ignore */ }
}

/* ------------------------------ 输入区同步 ------------------------------ */
/** 让 textarea 跟着内容长高。minPx 是底座，上限交给 CSS 的 max-height，
 * 超了就在框里自己滚，不会把整个页面撑开。 */
export function autoGrow(el, minPx = 0) {
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = Math.max(el.scrollHeight, minPx) + 'px';
}

export function syncComposer() {
  const input = $('#input');
  const btn = $('#btn-send');
  if (!input || !btn) return;

  const hasSession = !!state.session;
  const text = input.value.trim();

  input.disabled = !hasSession;
  input.placeholder = hasSession
    ? (state.busy ? 'AI 正在写…可以先打下一条，Enter 排队发送'
      : '以你角色的身份说话或行动…（Enter 发送，Shift+Enter 换行）')
    : '先新建或选择一个故事，才能开始对话';

  btn.disabled = !hasSession || state.busy || !text;
  btn.textContent = state.busy ? '生成中…' : '发送';

  paintAiStatus(hasSession);
  const count = $('#composer-count');
  if (count) count.textContent = text ? text.length + ' 字' : '';
  // 写长了就长高，不用在一条细缝里改；清空后自己缩回去
  autoGrow(input);
}

/**
 * 常驻状态栏：AI 生成状态不受消息区重建影响，永远可见。
 * 没有自定义状态时回落到快捷键提示。
 */
export function setAiStatus(text, kind = '') {
  state.aiStatus = { text: text || '', kind: kind || '' };
  paintAiStatus(true);
}

function paintAiStatus(hasSession) {
  const el = $('#ai-status');
  if (!el) return;
  const status = state.aiStatus || {};
  if (status.text) {
    el.textContent = status.text;
    el.className = status.kind || '';
    return;
  }
  el.textContent = hasSession
    ? (state.busy ? '正在生成回复…' : 'Enter 发送 · Shift+Enter 换行')
    : '还没有选择故事';
  el.className = hasSession ? '' : 'warn';
}

/** 角色卡片列表：设定弹窗与上下文弹窗共用。 */
export function renderCharacterCards(box, chars, emptyText) {
  if (!box) return;
  box.innerHTML = '';
  if (!chars || !chars.length) {
    const tip = document.createElement('div');
    tip.className = 'char-empty';
    tip.textContent = emptyText || '还没有角色。';
    box.appendChild(tip);
    return;
  }
  for (const c of chars) {
    const card = document.createElement('div');
    card.className = 'char-card';
    card.style.setProperty('--char-hue', hueOf(c.name));
    const name = document.createElement('div');
    name.className = 'cc-name';
    name.textContent = c.name || '（无名）';
    card.appendChild(name);
    if (c.role) {
      const role = document.createElement('div');
      role.className = 'cc-role';
      role.textContent = c.role;
      card.appendChild(role);
    }
    if (c.state) {
      const st = document.createElement('div');
      st.className = 'cc-state';
      st.textContent = '当前：' + c.state;
      card.appendChild(st);
    }
    box.appendChild(card);
  }
}

/** 可编辑的角色表：改名、改设定、清状态、删除、新增，都由用户决定。 */
export function renderCharacterEditor(box, list, onChange) {
  if (!box) return;
  box.innerHTML = '';

  list.forEach((c, i) => {
    const card = document.createElement('div');
    card.className = 'char-card editable';
    card.style.setProperty('--char-hue', hueOf(c.name));

    const head = document.createElement('div');
    head.className = 'cc-head';
    const idx = document.createElement('span');
    idx.className = 'cc-idx';
    idx.textContent = '角色 ' + (i + 1);
    // 只重写这一张卡：带上当前草稿里的名字，生成结果直接覆盖 role
    const ai = document.createElement('button');
    ai.type = 'button';
    ai.className = 'cc-ai field-ai';
    ai.textContent = '✦ AI';
    ai.title = '只重写这个角色';
    ai.onclick = () => {
      openAsk({
        field: 'character',
        name: (c.name || '').trim(),
        label: '角色「' + ((c.name || '').trim() || '未命名') + '」',
        read: () => c.role || '',
        write: (value) => { c.role = value; onChange(); },
        key: 'char' + i,
        index: i,
      });
    };

    const undo = document.createElement('button');
    undo.type = 'button';
    undo.className = 'cc-undo field-undo hidden';
    undo.textContent = '↺ 撤销';
    undo.dataset.undoSlot = 'char' + i;

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'cc-del';
    del.textContent = '移除';
    del.onclick = () => { list.splice(i, 1); onChange(); };
    head.append(idx, ai, undo, del);

    const name = document.createElement('input');
    name.className = 'cc-input';
    name.placeholder = '角色名';
    name.value = c.name || '';
    name.oninput = () => { c.name = name.value; };

    const role = document.createElement('textarea');
    role.className = 'cc-input cc-area';
    role.placeholder = '身份、性格、外貌、和你的关系…';
    role.value = c.role || '';

    // 超出档位上限的部分模型根本看不到，得让人知道，不能悄悄截掉
    const cap = ((state.conf && state.conf.tier_limits) || {}).role_chars || 0;
    const count = document.createElement('div');
    count.className = 'cc-count';
    const paint = () => {
      const n = (c.role || '').length;
      if (!cap) {
        count.textContent = n + ' 字 · 当前档位不限制长度';
        count.classList.remove('over');
        return;
      }
      const over = n > cap;
      count.textContent = n + ' 字 · 这一档只注入前 ' + cap + ' 字'
        + (over ? '，超出的 ' + (n - cap) + ' 字模型看不到' : '');
      count.classList.toggle('over', over);
    };
    role.oninput = () => { c.role = role.value; paint(); };
    paint();

    // 别叫 state：会遮蔽从 state.js 导入的全局状态，上面读档位时正好落在死区
    const stateField = document.createElement('input');
    stateField.className = 'cc-input';
    stateField.placeholder = '当前状态（可留空，AI 会随剧情更新）';
    stateField.value = c.state || '';
    stateField.oninput = () => { c.state = stateField.value; };

    // 音色跟着角色卡走：留空就退回设置里的音色表，不用为了换个声音去改全局
    const voice = document.createElement('select');
    voice.className = 'cc-input';
    const none = document.createElement('option');
    none.value = '';
    none.textContent = '音色：跟设置里的音色表';
    voice.appendChild(none);
    for (const v of ((state.conf && state.conf.speech_voices) || [])) {
      const o = document.createElement('option');
      o.value = v;
      // 带前缀，不然下拉收起后只显示一个英文名，看不出这行是音色
      o.textContent = '音色 · ' + v;
      voice.appendChild(o);
    }
    voice.value = c.voice || '';
    voice.onchange = () => { c.voice = voice.value; };

    // 语气也挂在卡上：同一个音色，说话方式跟着角色走
    const style = document.createElement('select');
    style.className = 'cc-input';
    const noStyle = document.createElement('option');
    noStyle.value = '';
    noStyle.textContent = '语气：平常';
    style.appendChild(noStyle);
    for (const nm of ((state.conf && state.conf.speech_styles) || [])) {
      const o = document.createElement('option');
      o.value = nm;
      o.textContent = '语气 · ' + nm;
      style.appendChild(o);
    }
    style.value = c.style || '';
    style.onchange = () => { c.style = style.value; };

    // 名字、音色、语气并排：各占一行会把卡撑得很长，三张卡就拉成一屏
    const says = document.createElement('div');
    says.className = 'cc-says';
    says.append(name, voice, style);

    card.append(head, says, role, count, stateField);
    box.appendChild(card);
  });

  const limit = (state.conf && state.conf.writing && state.conf.writing.max_characters) || 4;
  if (list.length < limit) {
    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'cc-add';
    add.textContent = '＋ 添加角色';
    add.onclick = () => {
      list.push({ name: '', role: '', state: '', history: [] });
      onChange();
    };
    box.appendChild(add);
  } else {
    const tip = document.createElement('div');
    tip.className = 'char-empty';
    tip.textContent = '最多 ' + limit + ' 个角色，想加新的先移除一个。';
    box.appendChild(tip);
  }
}

/** 上下文用量 / 压缩统计 / 在场角色，全部收进弹窗，标题栏不再堆字。 */
const SNAP_REASON = {
  compress: '压缩前', settings: '改设定前', turn: '每 15 轮', manual: '手动', restore: '回滚前',
};

async function loadSnapshots(sid, box) {
  let items = [];
  try {
    items = await api('/api/sessions/' + sid + '/snapshots');
  } catch (e) {
    box.textContent = '读取失败：' + e.message;
    return;
  }
  box.innerHTML = '';
  if (!items.length) {
    box.textContent = '还没有存档。压缩、改设定、每 15 轮会自动留一份。';
    return;
  }
  for (const snap of items) {
    const row = document.createElement('div');
    row.className = 'ctx-snap';
    const label = document.createElement('span');
    const when = new Date((snap.at || 0) * 1000);
    label.textContent = when.toLocaleString('zh-CN', { hour12: false })
      + ' · ' + (SNAP_REASON[snap.reason] || snap.reason);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '回到这里';
    btn.onclick = async () => {
      if (!confirm('回到这一版？当前进度会先自动存一份，回滚后也能再回来。')) return;
      btn.disabled = true;
      try {
        state.session = await api('/api/sessions/' + sid + '/snapshots/' + snap.name + '/restore',
          { method: 'POST' });
        renderMessages();
        syncAll();
        loadSessions();
        toast('已回滚到 ' + label.textContent, 'ok');
      } catch (e) {
        toast('回滚失败：' + e.message, 'error');
      } finally {
        btn.disabled = false;
      }
    };
    row.append(label, btn);
    box.appendChild(row);
  }
}

const SEG_LABEL = {
  rules: '写法', characters: '角色', user_role: '主角', summary: '前情',
  status: '处境', world: '世界观', language: '语言', identity: '身份', facts: '账本',
};
const SEG_COLOR = ['#7aa2f7', '#9ece6a', '#e0af68', '#bb9af7', '#7dcfff',
  '#f7768e', '#2ac3de', '#c0caf5', '#565f89'];

/** 改事实账本：加一条或删一条。成功后用服务端返回的完整账本替换本地视图。 */
async function editFacts(payload) {
  const s = state.session;
  if (!s || !s.id) return;
  try {
    const res = await api('/api/sessions/' + s.id + '/facts', { method: 'POST', body: payload });
    s.facts = res.facts || [];
    renderContextDetail();
  } catch (e) {
    toast('改账本失败：' + e.message, 'error');
  }
}

export function renderContextDetail() {
  const box = $('#ctx-detail');
  if (!box) return;
  const s = state.session;
  box.innerHTML = '';
  if (!s) {
    box.innerHTML = '<div class="char-empty">还没有选择故事。</div>';
    return;
  }
  const c = s.context || { used: s.context_tokens || 0, limit: 0 };
  const used = c.used || 0;
  const limit = c.limit || 0;
  const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const comp = c.compress || {};
  const st = c.stream || {};
  const d = c.debug || null;

  const mkRow = (k, v) => {
    const r = document.createElement('div');
    r.className = 'ctx-row';
    const a = document.createElement('span');
    a.textContent = k;
    const b = document.createElement('b');
    b.textContent = v;
    r.append(a, b);
    return r;
  };
  const mkSub = (text) => {
    const el = document.createElement('div');
    el.className = 'ctx-sub';
    el.textContent = text;
    return el;
  };
  const mkNote = (text) => {
    const el = document.createElement('div');
    el.className = 'ctx-note';
    el.textContent = text;
    return el;
  };

  // 一、上下文占用：会话数据 + system 模板，就是这一轮真正要发出去的量
  const head = document.createElement('div');
  head.className = 'ctx-head';
  head.innerHTML = '<b>' + fmtNumber(used) + '</b> / ' + fmtNumber(limit)
    + ' token<span class="ctx-pct">' + pct + '%</span>';
  const meter = document.createElement('div');
  // 到八成就会压缩，所以八成既是警告线，也是实际能用到的天花板
  meter.className = 'ctx-meter' + (pct >= 95 ? ' full' : (pct >= 80 ? ' warn' : ''));
  const fill = document.createElement('i');
  fill.style.width = pct + '%';
  meter.appendChild(fill);
  box.append(head, meter,
    mkNote('会话数据加 system 模板一起算，到八成自动压缩。'));

  // 二、用量与耗时一块：模型报的数和落盘的累计放一起，对得上才说明没算错。
  // 首字等待卡 prefill、生成速度卡 decode，分开看才知道慢在哪一段
  const inTok = (st.usage && st.usage.prompt_tokens) || 0;
  const outTok = (st.usage && st.usage.completion_tokens) || 0;
  const tot = st.total || {};
  const ttft = st.first_token_ms || 0;
  const totalMs = st.total_ms || 0;
  const decodeMs = st.decode_ms || (totalMs > ttft ? totalMs - ttft : 0);
  const speed = (decodeMs > 0 && outTok) ? Math.round(outTok / (decodeMs / 1000)) : 0;
  // 费用按人民币估，拿不到单价就只在前面标一下，不写 0——0 会被当成免费
  // 没配单价和"这轮是免费的"是两件事，分开说——之前一律显示横杠，
  // 于是模型名对不上价目表时会一直挂着 0，谁也不知道是没算还是没花钱
  // 每个模型自带币种：国内厂商人民币，OpenAI 这类美元原价，不做汇率折算
  // 符号表以后端为准：写死 'USD' ? '$' : '¥' 的话，加第三种币会一律显示 ¥
  const signOf = (cur) => ((state.conf && state.conf.currency_sign) || {})[cur]
    || ((state.conf && state.conf.currency_sign) || {}).CNY || '';
  const money = (cost, cur) => {
    if (cost === null || cost === undefined) return '未配单价';
    const v = cost.total || 0;
    const sign = signOf(cur);
    if (!v) return sign + '0';
    // 不到一角时多给几位小数，否则便宜的轮次全是 0.00
    return sign + (v >= 0.1 ? v.toFixed(2) : v.toFixed(4));
  };
  // 跨币种就并排写出来，不做汇算：中途从 deepseek 换到 gpt 时两边都算数
  const moneyBook = (book) => {
    const keys = Object.keys(book || {}).sort();
    if (!keys.length) return '—';
    return keys.map((k) => money({ total: book[k] }, k)).join(' + ');
  };
  // 累计费用是按轮加出来的，高峰时段那一轮本身就贵，所以在标签上标一下现在的档
  const peakNow = st.cost && st.cost.peak;
  const core = document.createElement('div');
  core.className = 'ctx-rows';
  // 四个数成对排：上轮 / 累计，各一对 token 和费用。顺序固定，扫一眼就能上下比
  // 顺序固定成这样：先耗时三项，再上轮的两个数，再累计的两个数，最后才是会话结构。
  // 上轮和累计挨着放，方便一眼横着比
  core.appendChild(mkRow('首字等待', ttft ? fmtNumber(ttft) + ' ms' : '—'));
  core.appendChild(mkRow('生成速度', speed
    ? speed + ' token/s（' + fmtNumber(outTok) + ' 出 / ' + fmtNumber(decodeMs) + ' ms）' : '—'));
  core.appendChild(mkRow('上轮总耗时', totalMs ? fmtNumber(totalMs) + ' ms' : '—'));
  core.appendChild(mkRow('上轮 tok', inTok
    ? '入 ' + fmtNumber(inTok) + ' · 出 ' + fmtNumber(outTok)
    : '还没发过'));
  core.appendChild(mkRow('上轮费用' + (st.cost && st.cost.peak ? '（高峰）' : ''),
    money(st.cost, st.cost && st.cost.currency)));
  core.appendChild(mkRow('累计 tok', tot.calls
    ? '入 ' + fmtNumber(tot.prompt_tokens) + ' · 出 ' + fmtNumber(tot.completion_tokens)
      + ' · ' + tot.calls + ' 次' : '—'));
  core.appendChild(mkRow('累计费用', moneyBook(tot.cost_by_cur)));
  box.appendChild(core);

  // 四、system prompt 构成：占比条只回答一个问题——哪一块在膨胀
  const parts = (d && d.system_parts) || {};
  const segs = Object.keys(parts)
    .filter((k) => parts[k] > 0)
    .map((k) => ({ k: k, v: parts[k], name: SEG_LABEL[k] || k }))
    .sort((a, b) => b.v - a.v);
  if (segs.length) {
    const sysTotal = segs.reduce((n, x) => n + x.v, 0) || 1;
    box.appendChild(mkSub('系统提示词构成 · ' + fmtNumber(sysTotal) + ' token'));
    const bar = document.createElement('div');
    bar.className = 'ctx-seg';
    const legend = document.createElement('div');
    legend.className = 'ctx-seg-legend';
    segs.forEach((seg, i) => {
      const color = SEG_COLOR[i % SEG_COLOR.length];
      const piece = document.createElement('i');
      piece.style.width = (seg.v / sysTotal * 100) + '%';
      piece.style.background = color;
      piece.title = seg.name + ' ' + fmtNumber(seg.v) + ' token';
      bar.appendChild(piece);
      const item = document.createElement('span');
      const dot = document.createElement('i');
      dot.className = 'ctx-seg-dot';
      dot.style.background = color;
      const nm = document.createElement('b');
      nm.textContent = seg.name;
      item.append(dot, nm, document.createTextNode(' ' + fmtNumber(seg.v)));
      legend.appendChild(item);
    });
    box.append(bar, legend);
    const top = segs[0];
    const topPct = Math.round((top.v / sysTotal) * 100);
    if (topPct >= 40) {
      box.appendChild(mkNote('最大一块是「' + top.name + '」，占 ' + topPct
        + '%。想压缩 token，从这里下手最有效。'));
    }
  }

  // 五、策略与状态：偶尔看一眼，不看也不影响聊天
  if (d) {
    // 手改过就用会话里的完整账本，否则用本轮实际注入的那份。
    // 只列 active：resolved 是被取代的旧说法，混进来只会看着像重复
    const facts = (s.facts || d.facts || [])
      .filter((f) => (f.state || 'active') === 'active');
    box.appendChild(mkSub('策略'));
    const pol = document.createElement('div');
    pol.className = 'ctx-rows';
    pol.appendChild(mkRow('世界书命中', (d.lore || []).length
      ? (d.lore || []).length + ' 条 / ' + fmtNumber(d.lore_tokens) + ' token' : '无'));
    pol.appendChild(mkRow('事实账本', facts.length
      ? facts.length + ' 条 / ' + fmtNumber(d.facts_tokens) + ' token' : '无'));
    pol.appendChild(mkRow('人设锚定', d.anchored
      ? '本轮已重插角色核心' : '每 ' + (d.anchor_every || 15) + ' 轮重插一次'));
    box.appendChild(pol);

    // 六、会话结构与落盘：接着上面那块用量往下排，不再单独折一层。
    // 「前情提要」不放这——系统提示词构成里的「前情」就是它，说两遍只会让人对不上数
    core.appendChild(mkRow('消息', (c.messages || 0) + ' 条'));
    core.appendChild(mkRow('保留上限', (c.keep_recent || 0) + ' 条'));
    core.appendChild(mkRow('压缩', comp.times ? comp.times + ' 次' : '未压缩'));
    core.appendChild(mkRow('已折叠', comp.times
      ? comp.dropped + ' 条 / ' + fmtNumber(comp.dropped_tokens) + ' token' : '—'));
    core.appendChild(mkRow('写盘 / 读盘', c.io
      ? Math.round(c.io.save_ms || 0) + ' / ' + Math.round(c.io.load_ms || 0) + ' ms' : '—'));
    core.appendChild(mkRow('会话文件', c.io && c.io.bytes
      ? fmtNumber(Math.round(c.io.bytes / 1024)) + ' KB' : '—'));
    const hit = (st.usage && (st.usage.prompt_cache_hit_tokens
      || (st.usage.prompt_tokens_details || {}).cached_tokens)) || 0;
    core.appendChild(mkRow('缓存命中', hit
      ? fmtNumber(hit) + ' token（' + Math.round(hit / (inTok || 1) * 100) + '%）' : '无'));

    if ((d.lore || []).length) {
      box.appendChild(mkSub('命中的世界书'));
      const list = document.createElement('div');
      list.className = 'ctx-lore';
      for (const hit2 of d.lore) {
        const row = document.createElement('div');
        row.className = 'ctx-lore-row';
        const key = document.createElement('code');
        key.textContent = hit2.keys || '';
        const body = document.createElement('span');
        body.textContent = hit2.content || '';
        row.append(key, body);
        list.appendChild(row);
      }
      box.appendChild(list);
    }

    if (facts.length) {
      box.appendChild(mkSub('事实账本（最近 ' + facts.length + ' 条）'));
      const list = document.createElement('div');
      list.className = 'ctx-lore';
      for (const f of facts) {
        const row = document.createElement('div');
        row.className = 'ctx-lore-row';
        const key = document.createElement('code');
        key.textContent = (f.subject || '?') + ' · 第 ' + (f.turn || 0) + ' 轮';
        const body = document.createElement('span');
        body.textContent = f.fact || '';
        const del = document.createElement('button');
        del.className = 'ctx-fact-del';
        del.type = 'button';
        del.textContent = '×';
        del.title = '从账本里删掉这条';
        // 两段式：先点成「确定删除」，再点才真删，三秒不点自己收回。
        // 一行只有一个 ×，手指一滑就删掉一条设定，比点两次麻烦得多
        del.onclick = () => {
          if (del.dataset.armed) {
            editFacts({ action: 'delete', turn: f.turn, fact: f.fact });
            return;
          }
          del.dataset.armed = '1';
          del.textContent = '确定删除';
          del.classList.add('armed');
          del._disarm = setTimeout(() => {
            delete del.dataset.armed;
            del.textContent = '×';
            del.classList.remove('armed');
          }, 3000);
        };
        row.append(key, body, del);
        list.appendChild(row);
      }
      box.appendChild(list);
    }

    // 手填一条：模型没记住的、或你想让它记住的，都从这里进账本
    const add = document.createElement('div');
    add.className = 'ctx-fact-add';
    const input = document.createElement('input');
    input.type = 'text';
    input.maxLength = 120;
    input.placeholder = '手动记一条，比如「她左手腕有疤」';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '记下';
    const submit = () => {
      const text = input.value.trim();
      if (text) editFacts({ action: 'add', fact: text });
    };
    btn.onclick = submit;
    input.onkeydown = (e) => { if (e.key === 'Enter') submit(); };
    add.append(input, btn);
    box.appendChild(add);

    const details = document.createElement('details');
    details.className = 'ctx-system';
    const sum = document.createElement('summary');
    sum.textContent = '看这一轮的完整 system prompt';
    const pre = document.createElement('pre');
    pre.textContent = d.system || '';
    details.append(sum, pre);
    box.appendChild(details);
  }

  if (s.id) {
    box.appendChild(mkSub('历史版本（回滚用）'));
    const list = document.createElement('div');
    list.className = 'ctx-snaps';
    list.id = 'ctx-snaps';
    list.textContent = '读取中…';
    box.appendChild(list);
    loadSnapshots(s.id, list);
  }
}
/* ------------------------------ 顶部信息 ------------------------------ */
export function syncHeader() {
  // 故事视图里没有"当前对话"，这些操作只对对话有意义
  const storyView = state.sideView === 'stories';
  const on = !!state.session && !storyView;
  for (const id of ['btn-compress', 'btn-clear', 'btn-context', 'btn-export']) {
    const el = $('#' + id);
    if (!el) continue;
    el.disabled = !on || state.busy;
    // 没选对话时按钮留着一个灰壳只会显得脏，直接收起来
    el.classList.toggle('hidden', !on);
  }
  // 设定按钮只属于对话：故事页的设定就在右侧常驻，不需要入口
  const settingsBtn = $('#btn-settings');
  if (settingsBtn) {
    const inStories = state.sideView === 'stories';
    settingsBtn.classList.toggle('hidden', inStories);
    settingsBtn.disabled = inStories || !state.session || state.busy;
  }
  for (const id of ['btn-new-story', 'btn-new-session']) {
    const el = $('#' + id);
    if (el) el.disabled = state.busy;
  }
  const title = $('#chat-title');
  if (title) {
    if (storyView) {
      const picked = (state.stories || []).find((s) => s.id === state.selectedStory);
      title.textContent = picked ? (picked.title || '未命名故事') : '故事设定';
    } else {
      title.textContent = state.session ? (state.session.title || '未命名故事') : '未选择故事';
      if (state.session) {
        // 条数贴着标题走，跟左侧列表一致
        const sess = state.session;
        const n = sess.context ? sess.context.messages : (sess.messages || []).length;
        const count = document.createElement('span');
        count.className = 'chat-count';
        count.textContent = (n || 0) + ' 条';
        title.appendChild(count);
      }
    }
  }
  updateSub();
}

export function updateSub() {
  const el = $('#chat-sub');
  if (!el) return;
  if (state.sideView === 'stories') {
    el.textContent = '故事 = 原始模板，改完保存后新开的对话都会继承它';
    return;
  }
  const s = state.session;
  if (!s) { el.textContent = ''; return; }
  el.textContent = '';
  const put = (text) => el.appendChild(document.createTextNode(text));
  const chars = s.characters || [];
  const status = s.status || {};
  const present = status.present || [];
  const away = status.away || [];
  const me = s.user_name || '我';
  // 主角永远不算离场：他去哪儿由他自己决定，不由这份名单判定
  const isLeft = (name) => name === me ? false
    : (away.includes(name) || (present.length > 0 && !present.includes(name)));
  const chip = (name) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'char-chip' + (isLeft(name) ? ' off' : '') + (name === me ? ' me' : '');
    btn.dataset.name = name;
    btn.title = name === me ? '点开看自己的当前状态' : '点开看这个角色的当前状态';
    btn.textContent = name;
    return btn;
  };
  const group = (label, list) => {
    if (!list.length) return;
    put(label);
    for (const c of list) {
      el.appendChild(chip(c.name || c));
      put(' ');
    }
  };

  // 主角单独一格，在场/不在场按状态分两组，不再用「（离场）」这种尾巴
  put('主角：');
  el.appendChild(chip(me));
  const others = chars.filter((c) => c.name !== me);
  group(' 在场：', others.filter((c) => !isLeft(c.name)));
  group(' 不在场：', others.filter((c) => isLeft(c.name)));
  if (!chars.length) put(' AI = ' + (s.ai_name || 'AI'));

  // 当前状态单独占一行：地点、时间、关系、目标、物品都记在状态块里，
  // 挤在同一行会把上面的在场名单顶散
  const flat = (v) => {
    if (Array.isArray(v)) return v.filter(Boolean).join('、');
    if (v && typeof v === 'object') return Object.keys(v).filter((k) => k && v[k])
      .map((k) => k + '：' + v[k]).join('；') || (v[''] || '');
    return String(v || '').trim();
  };
  const facts = [];
  // 物品不在这一行：谁的归谁，去角色弹层里看
  for (const [key, label] of [['location', '地点'], ['time', '时间'],
    ['relations', '关系'], ['goal', '目标']]) {
    const value = flat(status[key]);
    if (value) facts.push([label, value]);
  }
  if (!facts.length) return;
  const line = document.createElement('div');
  line.className = 'sub-state';
  const icon = document.createElement('span');
  icon.className = 'sub-icon';
  icon.textContent = '🧭';
  line.appendChild(icon);
  facts.forEach(([k, v], i) => {
    if (i) {
      const dot = document.createElement('span');
      dot.className = 'sub-sep';
      dot.textContent = ' · ';
      line.appendChild(dot);
    }
    const key = document.createElement('b');
    key.className = 'sub-key';
    key.textContent = k + ' ';
    line.appendChild(key);
    line.appendChild(document.createTextNode(v));
  });
  el.appendChild(line);
}

/** 角色满了：问用户是踢掉一个换新的，还是干脆不要这个新角色。 */
export function askCharacterPick(pending, chars) {
  return new Promise((resolve) => {
    const modal = document.createElement('div');
    modal.className = 'modal';
    const box = document.createElement('div');
    box.className = 'modal-box narrow';
    const head = document.createElement('h2');
    head.textContent = '角色满了';
    const tip = document.createElement('p');
    tip.className = 'hint';
    tip.textContent = '要加「' + (pending.name || '新角色') + '」'
      + (pending.role ? '（' + pending.role.slice(0, 30) + '）' : '')
      + '，得先腾个位置。点一个人换掉，或者这次先不登记。';
    const list = document.createElement('div');
    list.className = 'pick-list';
    for (const c of chars) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'pick-item';
      b.textContent = c.name + (c.role ? ' · ' + c.role.slice(0, 24) : '');
      b.onclick = () => { modal.remove(); resolve(c.name); };
      list.appendChild(b);
    }
    const actions = document.createElement('div');
    actions.className = 'modal-actions';
    const skip = document.createElement('button');
    skip.type = 'button';
    skip.className = 'ghost';
    skip.textContent = '不要新角色';
    skip.onclick = () => { modal.remove(); resolve(''); };
    const spacer = document.createElement('span');
    spacer.className = 'spacer';
    actions.append(skip, spacer);
    box.append(head, tip, list, actions);
    modal.appendChild(box);
    document.body.appendChild(modal);
  });
}

/** 点顶栏的角色名，看这个角色此刻是什么状态。 */
export function toggleCharPop(name, anchor) {
  const pop = $('#char-pop');
  const s = state.session;
  if (!pop || !s) return;
  if (!pop.classList.contains('hidden') && pop.dataset.for === name) {
    pop.classList.add('hidden');
    return;
  }
  const me = s.user_name || '我';
  let c = (s.characters || []).find((x) => x.name === name);
  // 主角不在角色表里，但他也有状态要看
  if (!c && name === me) c = { name: me, role: '', state: '', history: [] };
  if (!c) return;

  const status = s.status || {};
  const rows = [];
  // 长设定不适合塞弹层，看状态就够
  // 物品按人分的时候各看各的；还是一条整串就标成公共的，至少别让人以为丢了
  let carried = '';
  if (status.items && typeof status.items === 'object') {
    carried = status.items[name] || '';
  } else if (typeof status.items === 'string' && status.items.trim()) {
    carried = status.items.trim() + '（公共）';
  }
  if (carried) rows.push(['物品', carried]);
  // 状况只要一条：state 就是当前状态。history 只在它缺失时兜底，
  // 一起读会把同一句话重复一遍
  // 不能叫 state：模块顶部导入了 state（当前会话），同名会在 TDZ 里把上面的 state.session 弄崩
  const nowText = String(c.state || '').trim()
    || String((c.history || []).slice(-1)[0] || '').trim();
  rows.push(['状况', nowText || '还没记下来，模型写了【变化】之后这里会有内容']);
  if (name === me) rows.unshift(['身份', '你这一局演的就是 TA']);

  pop.textContent = '';
  pop.dataset.for = name;
  const head = document.createElement('div');
  head.className = 'cp-head';
  head.textContent = c.name;
  pop.appendChild(head);
  for (const [label, value] of rows) {
    const row = document.createElement('div');
    row.className = 'cp-row';
    const k = document.createElement('b');
    k.textContent = label;
    const v = document.createElement('span');
    v.textContent = value;
    row.append(k, v);
    pop.appendChild(row);
  }
  const status2 = s.status || {};
  const isMe = name === (s.user_name || '我');
  const left = isMe ? false
    : ((status2.away || []).includes(name)
      || ((status2.present || []).length > 0 && !(status2.present || []).includes(name)));
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'cp-act';
  toggle.textContent = left ? '标回在场' : '标记离场';
  toggle.onclick = () => setCharScene(name, left).catch((e) => {
    toast('改不动：' + e.message, 'error');
  });
  if (!isMe) pop.appendChild(toggle);   // 主角没有「离场」这回事
  pop.classList.remove('hidden');

  const box = anchor ? anchor.getBoundingClientRect() : null;
  if (box) {
    const left = Math.min(box.left, window.innerWidth - 320);
    pop.style.left = Math.max(12, left) + 'px';
    pop.style.top = (box.bottom + 8) + 'px';
  }
}

/** 模型判错在场名单时，让用户自己改。 */
export async function setCharScene(name, present) {
  const s = state.session;
  if (!s) return;
  const st = s.status || {};
  const on = new Set(st.present || []);
  const off = new Set(st.away || []);
  if (present) { on.add(name); off.delete(name); } else { off.add(name); on.delete(name); }
  state.session = await api('/api/sessions/' + s.id, {
    method: 'PUT',
    body: { status: { present: [...on], away: [...off] } },
  });
  closeCharPop();
  syncAll();
  await loadSessions();
}

export function closeCharPop() {
  const pop = $('#char-pop');
  if (pop) pop.classList.add('hidden');
}

/** 窄屏抽屉：选中东西或点遮罩就收回。 */
export function closeNav() {
  document.body.classList.remove('nav-open');
}

export function setConnectionBanner(message) {
  const banner = $('#conn-banner');
  const text = $('#conn-text');
  if (!banner || !text) return;
  if (!message) { banner.classList.add('hidden'); return; }
  text.textContent = message;
  banner.classList.remove('hidden');
}

export function syncAll() {
  syncHeader();
  syncComposer();
  renderContextDetail();
  const select = $('#model-select');
  if (select) select.disabled = state.busy;
}

/* ------------------------------ 消息渲染 ------------------------------ */
const SEG_RE = /【([^】]{1,24})】/g;

/** 把一次模型输出切成「旁白 / 各角色」的段落，流式与落盘共用同一套规则。 */
export function splitSegments(text) {
  const src = text || '';
  const marks = [];
  SEG_RE.lastIndex = 0;
  let m;
  while ((m = SEG_RE.exec(src))) {
    marks.push({ tag: m[1].trim(), start: m.index, bodyStart: m.index + m[0].length });
  }
  if (!marks.length) return [{ tag: '', body: src.trim() }];
  // 第一个【】之前那段是旁白，之前整段被丢掉了：流式时开头一直空着，收尾才补上
  const out = [];
  const head = src.slice(0, marks[0].start).trim();
  if (head) out.push({ tag: '', body: head });
  marks.forEach((mk, i) => out.push({
    tag: mk.tag,
    body: src.slice(mk.bodyStart, i + 1 < marks.length ? marks[i + 1].start : src.length).trim(),
  }));
  return out;
}

const META_TAGS = ['新角色', '新增角色', '角色', '变化', '状态', '设定变化'];
const NARR_TAGS = ['旁白', '叙述', '系统'];

/**
 * 拆出旁白与每个角色的发言。
 * 圆桌之后一轮里会有好几个人说话，所以这里返回列表而不是单条台词；
 * 【新角色】【变化】这些是给后端看的，不渲染。
 */
export function pickTurn(text, speaker) {
  const out = { narrator: '', line: '', lines: [] };
  const narrators = [];
  for (const seg of splitSegments(text)) {
    const body = seg.body || '';
    if (!body) continue;
    if (NARR_TAGS.includes(seg.tag)) narrators.push(body);
    else if (META_TAGS.includes(seg.tag)) continue;
    else if (!seg.tag) {
      // 没有标签的段落：rp 模式下第一段算旁白，其余接在后面继续当旁白
      narrators.push(body);
    } else {
      out.lines.push({ speaker: seg.tag, text: body });
    }
  }
  out.narrator = narrators.join('\n\n');
  // 兼容只认 line 的老调用
  out.line = out.lines.length ? out.lines[0].text : '';
  return out;
}

/** 旁白。顶部的小标签写故事名（不是「系统」），一眼看出这段属于哪个故事。
 *  生成中和生成完之后必须长得一样，所以流式和落盘都走这里。
 */
/** 旁白标签上写的故事名：优先故事模板的标题，没有就用这局对话的标题。 */
function storyName() {
  const s = state.session || {};
  const story = (state.stories || []).find((x) => x.id === s.story_id);
  return (story && story.title) || s.title || '故事';
}

export function narratorNode(text, tag = '') {
  const el = document.createElement('div');
  el.className = 'narrator';
  if (tag) {
    const label = document.createElement('span');
    label.className = 'narr-tag';
    label.textContent = tag;
    el.appendChild(label);
  }
  const body = document.createElement('div');
  body.className = 'narr-body';
  body.innerHTML = mdToHtml(text);
  el.appendChild(body);
  return el;
}

/** 一个 AI 回合 = 旁白 + 若干角色气泡 */
export function buildTurn(narrator, speaker, line, lines) {
  const el = document.createElement('div');
  el.className = 'turn';
  // 旁白块一律带上故事名：多角色模式以前只有一般模式才有这个标签
  if (narrator) el.appendChild(narratorNode(narrator, storyName()));
  const many = (lines || []).filter((x) => x && x.text);
  if (many.length) {
    let last = null;
    for (const item of many) {
      last = buildMessage('assistant', item.text, item.speaker || 'AI');
      el.appendChild(last.el);
    }
    return { el, msg: last };
  }
  const msg = buildMessage('assistant', line, speaker);
  el.appendChild(msg.el);
  return { el, msg };
}

/** 落盘后的最终内容，按服务端解析结果重绘。 */
export function renderTurn(container, turn) {
  if (!container) return null;
  container.innerHTML = '';
  if (turn.narrator) container.appendChild(narratorNode(turn.narrator, storyName()));
  const lines = (turn.lines || []).filter((x) => x && x.text);
  if (lines.length) {
    // 圆桌式：旁白之后一人一个气泡，按发言顺序排
    let last = null;
    for (const item of lines) {
      last = buildMessage('assistant', item.text, item.speaker || 'AI');
      container.appendChild(last.el);
    }
    return last;
  }
  if (!turn.line) return null;          // 一般模式只有叙述，没有角色气泡
  const msg = buildMessage('assistant', turn.line, turn.speaker || 'AI');
  container.appendChild(msg.el);
  return msg;
}

/**
 * 生成中：不显示角色名与头像，只有一个气泡（省略号 + 秒数）。
 * 旁白先出现，台词随后接上，顺序固定为「画外音 → 角色言行」。
 */
export function renderStreaming(container, text, speaker, seconds = 0, plain = false) {
  if (!container) return null;
  const picked = pickTurn(text, speaker);
  container.innerHTML = '';
  if (plain) {
    // 一般模式整段都是叙述：流式也直接画成旁白块。
    // 以前流式走气泡、收尾换成旁白块，一收尾整个框就换了个样子。
    const el = narratorNode(text || '');
    const body = el.querySelector('.narr-body');
    if (!text) body.innerHTML = '<span class="dots">⋯</span>';
    const tick = document.createElement('span');
    tick.className = 'tick';
    tick.textContent = seconds + 's';
    body.appendChild(tick);
    container.appendChild(el);
    return { el, bubble: body };
  }
  if (picked.narrator) container.appendChild(narratorNode(picked.narrator));

  // 圆桌式：边流边按【名字】切气泡，收尾时结构不变，不会整块换样子
  let last = null;
  for (const item of picked.lines) {
    last = buildMessage('assistant', item.text, item.speaker);
    last.el.classList.add('typing');
    last.bubble.classList.add('typing');
    container.appendChild(last.el);
  }
  if (!last) {
    // 还没念到任何人的名字：先给一个空的气泡占位
    last = buildMessage('assistant', '', speaker || '');
    last.bubble.innerHTML = '<span class="dots">⋯</span>';
    container.appendChild(last.el);
  }
  const tick = document.createElement('span');
  tick.className = 'tick';
  tick.textContent = seconds + 's';
  last.bubble.appendChild(tick);
  return { el: last.el, bubble: last.bubble };
}

/** 名字 → 稳定的色相，让每个角色有自己的颜色。 */
export function hueOf(name) {
  let h = 0;
  const s = name || '';
  for (let i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) % 360;
  return h;
}

let _sayAudio = null;
let _speakingBtn = null;
// 每轮朗读一个号，新一轮把旧轮顶掉；没它的话连点两下会两个循环各播各的
let _speakRun = 0;
// 合成期间点的暂停，存着等真开播时生效
let _speakHold = false;
// 边生成边读：模型一个字一个字吐，按句切好丢进队列，念完一句接下一句。
// 队列串行是刻意的：几段声音叠在一起没法听。
const TTS_QUEUE = [];
let ttsBusy = false;
// 队列里连着一串都念不出来是常态（配置错、显存满），只提示第一次，
// 成功念出一句再重新武装，免得刷满一屏
let ttsWarned = false;
let ttsOn = false;
let ttsBuf = '';
// 播当前段时把后面两段先合成好，轮到时直接放。
// 长消息慢就慢在串行等 HTTP，预取几段基本上就被播放时间盖住了。
const PREFETCH = 2;

/** 把 markdown 变成能念的干净文字：代码块和状态标记跳过，否则会念出一串星号。 */
function speakable(text) {
  return String(text || '')
    .replace(/\x60\x60\x60[\s\S]*?\x60\x60\x60/g, ' ')
    .replace(/\[\[[\s\S]*?\]\]/g, ' ')
    .replace(/[*_\x60#>|~]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function stopSpeak() {
  if (!_sayAudio) return;
  try { _sayAudio.pause(); } catch (e) { /* 已经停了 */ }
  URL.revokeObjectURL(_sayAudio.src);
  _sayAudio = null;
}

/** 正在读时再点一下就暂停，再点继续。返回 true 表示这一下被它吃掉了。 */
/**
 * 点一下就切状态：在播就暂停/继续，还在合成就记下暂停意图。
 * 返回 true 表示这一下被吃掉了，不能再起一轮。
 * 之前只看 _sayAudio，合成那几秒里它还是空的，于是连点两下就播两遍。
 */
function toggleSpeakPause() {
  if (!_speakRun) return false;
  if (!_sayAudio) {
    // 还在等第一段合成：切暂停意图，真正开播时生效
    _speakHold = !_speakHold;
    if (_speakingBtn) _speakingBtn.classList.toggle('paused', _speakHold);
    return true;
  }
  if (_sayAudio.paused) {
    _speakHold = false;
    _sayAudio.play().catch(() => {});
    if (_speakingBtn) _speakingBtn.classList.remove('paused');
  } else {
    _speakHold = true;
    try { _sayAudio.pause(); } catch (e) { return false; }
    if (_speakingBtn) _speakingBtn.classList.add('paused');
  }
  return true;
}

async function fetchSpeech(text, role, voice, style) {
  const res = await fetch('/api/speech', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, role: role || '', voice: voice || '', style: style || '' }),
  });
  if (!res.ok) {
    const info = await res.json().catch(() => ({}));
    throw new Error(info.error || ('HTTP ' + res.status));
  }
  return URL.createObjectURL(await res.blob());
}

// 不再在这里 stopSpeak()：那会把上一段连带暂停状态一起打掉。
function playUrl(url) {
  return new Promise((resolve, reject) => {
    const audio = new Audio(url);
    _sayAudio = audio;
    if (_speakHold) {
      // 用户在合成期间就点了暂停，先别出声
      try { audio.pause(); } catch (e) { /* 还没开始就算了 */ }
    }
    audio.onended = () => { URL.revokeObjectURL(url); if (_sayAudio === audio) _sayAudio = null; resolve(); };
    audio.onerror = () => { URL.revokeObjectURL(url); if (_sayAudio === audio) _sayAudio = null; reject(new Error('播放器报错')); };
    audio.play().catch(reject);
    if (_speakHold) { try { audio.pause(); } catch (e) { /* 同上 */ } }
  });
}

async function pumpSpeech() {
  if (ttsBusy) return;
  ttsBusy = true;
  try {
    while (TTS_QUEUE.length) {
      const body = TTS_QUEUE.shift();
      try {
        await playUrl(await fetchSpeech(body));
        ttsWarned = false;
      } catch (e) {
        // 一句念不出来不该把整条流停掉，但也不能只在控制台说一声：
        // 用户的感受就是「点了没反应」
        console.error('朗读失败', e);
        if (!ttsWarned) {
          ttsWarned = true;
          toast('朗读失败：' + ((e && e.message) || e), 'error');
        }
      }
    }
  } finally {
    ttsBusy = false;
  }
}

function enqueueSpeech(text) {
  const body = speakable(text);
  if (!body) return;
  TTS_QUEUE.push(body);
  pumpSpeech();
}

const SENT_END = /[\u3002\uff01\uff1f!?\u2026\n]/;
// \u592a\u77ed\u4e00\u53e5\u4f1a\u5ff5\u6210\u4e00\u4e2a\u4e2a\u8bcd\uff0c\u542c\u7740\u65ad\uff1b\u6512\u591f\u8fd9\u4e48\u591a\u5b57\u518d\u9001
const MIN_CHUNK = 6;

export function setStreamSpeech(on) {
  ttsOn = !!on;
  if (!ttsOn) { ttsBuf = ''; TTS_QUEUE.length = 0; }
}

/** \u6bcf\u6536\u5230\u4e00\u6bb5 delta \u5c31\u5582\u8fdb\u6765\uff0c\u5b83\u81ea\u5df1\u6309\u53e5\u5207\u3002 */
export function feedStream(text) {
  if (!ttsOn) return;
  ttsBuf += text || '';
  let last = -1;
  for (let i = 0; i < ttsBuf.length; i++) {
    if (SENT_END.test(ttsBuf[i])) last = i;
  }
  if (last < MIN_CHUNK) return;
  enqueueSpeech(ttsBuf.slice(0, last + 1));
  ttsBuf = ttsBuf.slice(last + 1);
}

/** \u8fd9\u4e00\u8f6e\u751f\u6210\u7ed3\u675f\uff1a\u5269\u4e0b\u7684\u5c3e\u5df4\u4e0d\u7ba1\u6709\u6ca1\u6709\u53e5\u53f7\u90fd\u5ff5\u5b8c\u3002 */
export function flushStream() {
  const rest = ttsBuf;
  ttsBuf = '';
  if (ttsOn && rest.trim()) enqueueSpeech(rest);
}

export function stopSpeaking() {
  TTS_QUEUE.length = 0;
  ttsBuf = '';
  stopSpeak();
}

/** \u6c14\u6ce1\u4e0a\u90a3\u4e2a \U0001F50A\uff1a\u6574\u6bb5\u4e00\u6b21\u8bf7\u6c42\uff0c\u4e0d\u5206\u6bb5\u3002 */
/**
 * 旁白里夹的直接引语。结构化数据里没有这一层，只能从文本切。
 * 中文双引号、日式直角引号、英文双引号都认。
 */
const QUOTE_RE = /[\u201c\u300c]([^\u201d\u300d]*)[\u201d\u300d]|"([^"]*)"/g;

/** 一段旁白切成叙述/引述两种，没引号就原样一段。 */
function splitQuotes(text) {
  const body = String(text || '');
  const out = [];
  let last = 0;
  QUOTE_RE.lastIndex = 0;
  let m;
  while ((m = QUOTE_RE.exec(body)) !== null) {
    if (m.index > last) out.push({ quote: false, text: body.slice(last, m.index) });
    const said = m[1] != null ? m[1] : (m[2] || '');
    if (said.trim()) out.push({ quote: true, text: said });
    last = m.index + m[0].length;
  }
  if (last < body.length) out.push({ quote: false, text: body.slice(last) });
  return out.length ? out : [{ quote: false, text: body }];
}

/**
 * 一条消息拆成配音片段，返回 [{role, text}]，role 拿去查音色表。
 *
 * 多角色那条路是白给的：后端已经把 lines 里的 speaker 分好了。
 * 一般模式只有旁白，旁白里人物说的话靠引号切，角色名给「引述」。
 */
/** 查这个角色卡上配的风格；没配过返回空串，就是平常的语气。 */
export function styleOf(name) {
  const key = String(name || '').trim();
  if (!key) return '';
  const chars = (state.session && state.session.characters) || [];
  const hit = chars.find((c) => c && String(c.name || '').trim() === key);
  return (hit && hit.style) || '';
}

/** 查这个角色卡上配的音色；没配过返回空串，让它退回设置里的音色表。 */
export function voiceOf(name) {
  const key = String(name || '').trim();
  if (!key) return '';
  const chars = (state.session && state.session.characters) || [];
  const hit = chars.find((c) => c && String(c.name || '').trim() === key);
  return (hit && hit.voice) || '';
}

export function segmentsOf(msg, userLabel) {
  const out = [];
  const push = (role, text, voice, style) => {
    const body = speakable(text);
    if (body) out.push({ role, text: body, voice: voice || '', style: style || '' });
  };
  if (!msg) return out;
  if (msg.role === 'user') {
    // 我念的词跟着「我的角色」那张卡走，没设过就退回全局音色表
    const mine = (state.session && state.session.user_voice) || '';
    const myStyle = (state.session && state.session.user_style) || '';
    push(userLabel || '\u6211', msg.content, mine, myStyle);
    return out;
  }
  const lines = (msg.lines || []).filter((x) => x && x.text);
  if (lines.length) {
    // 旁白没有角色卡，留空让它走全局音色表
    if (msg.narrator) push('\u65c1\u767d', msg.narrator, '', '');
    for (const it of lines) {
      push(it.speaker || 'AI', it.text, voiceOf(it.speaker), styleOf(it.speaker));
    }
    return out;
  }
  for (const piece of splitQuotes(msg.narrator || msg.content || '')) {
    push(piece.quote ? '\u5f15\u8ff0' : '\u65c1\u767d', piece.text);
  }
  return out;
}

/** 逐段念：一段一个请求，串行播。正在播时再点一下就暂停。 */
/**
 * 把一段长文字按句号再切一层。每小句单独发请求，预取才能真正提前。
 * 只切长的：太碎的话每句都要一次请求，反而更慢。
 */
const SPLIT_AT = 24;
function splitLong(text) {
  const body = String(text || '');
  if (body.length <= SPLIT_AT * 1.5) return [body];
  const parts = body.match(/[^\u3002\uff01\uff1f\uff1b\n]+[\u3002\uff01\uff1f\uff1b\n]?/g) || [body];
  const out = [];
  let buf = '';
  for (const part of parts) {
    buf += part;
    if (buf.length >= SPLIT_AT) { out.push(buf.trim()); buf = ''; }
  }
  if (buf.trim()) out.push(buf.trim());
  return out.filter(Boolean);
}

export async function speakSegments(segments, btn) {
  // 同一个按钮再点一下：不管是在播还是还在合成，都当暂停/继续处理
  if (btn && btn === _speakingBtn && toggleSpeakPause()) return;

  const flat = [];
  for (const seg of (segments || [])) {
    if (!seg || !seg.text) continue;
    for (const piece of splitLong(seg.text)) {
      flat.push({ role: seg.role, text: piece, voice: seg.voice, style: seg.style });
    }
  }
  const list = flat;
  if (!list.length) { toast('这段没什么可以念的。'); return; }

  // 新一轮：把上一轮的循环顶掉，否则两个循环会各播各的
  const myRun = ++_speakRun;
  _speakHold = false;
  stopSpeak();
  const pending = new Map();
  const ask = (i) => {
    if (!pending.has(i)) {
      pending.set(i, fetchSpeech(list[i].text, list[i].role, list[i].voice, list[i].style));
    }
    return pending.get(i);
  };

  const prevBtn = _speakingBtn;
  if (prevBtn && prevBtn !== btn) prevBtn.classList.remove('speaking', 'paused');
  _speakingBtn = btn || null;
  if (btn) { btn.classList.add('speaking'); btn.classList.remove('paused'); }

  try {
    for (let i = 0; i < list.length; i += 1) {
      if (myRun !== _speakRun) return;
      for (let k = i + 1; k <= i + PREFETCH && k < list.length; k += 1) ask(k);
      const url = await ask(i);
      if (myRun !== _speakRun) { URL.revokeObjectURL(url); return; }
      await playUrl(url);
    }
  } catch (e) {
    if (myRun === _speakRun) {
      toast('念不出来：' + (e && e.message ? e.message : e), 'error');
    }
  } finally {
    for (const pr of pending.values()) {
      pr.then((u) => { try { URL.revokeObjectURL(u); } catch (e) { /* 已经没了 */ } })
        .catch(() => {});
    }
    pending.clear();
    if (myRun === _speakRun) {
      if (btn) btn.classList.remove('speaking', 'paused');
      _speakingBtn = null;
      _speakRun = 0;
      _speakHold = false;
    }
  }
}

export async function speakText(text, btn, role) {
  const body = speakable(text);
  if (!body) { toast('\u8fd9\u6bb5\u6ca1\u4ec0\u4e48\u53ef\u4ee5\u5ff5\u7684\u3002'); return; }
  await speakSegments([{ role: role || '\u65c1\u767d', text: body }], btn);
}

export function buildMessage(role, content, name, edited) {
  const el = document.createElement('div');
  el.className = 'msg ' + role;
  el.style.setProperty('--char-hue', hueOf(name));

  const who = document.createElement('div');
  who.className = 'who';
  if (role === 'assistant') {
    const avatar = document.createElement('i');
    avatar.className = 'avatar';
    avatar.textContent = (name || 'AI').trim().slice(0, 1);
    who.append(avatar);
  }
  const whoName = document.createElement('span');
  whoName.textContent = name;
  who.append(whoName);

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = mdToHtml(content);
  if (edited) {
    const tag = document.createElement('span');
    tag.className = 'edited-tag';
    tag.textContent = '已编辑';
    tag.title = '这条改过，之后的回复仍是按原话写的';
    bubble.appendChild(tag);
  }


  el.append(who, bubble);
  return { el, who, bubble };
}

/** 每条消息右下角的操作条。只给自己写的话提供编辑，改 AI 的话没有意义。 */
function toolsNode(role, isLast) {
  const acts = role === 'user'
    ? [['speak', '🔊', '念这一段'], ['edit', '✎', '编辑这句话'], ['rewrite', '↻', '重发这句'], ['branch', '⧉', '从这里分叉']]
    : [['speak', '🔊', '念这一段'], ['rewrite', '↻', '删掉这条及之后重新生成'], ['branch', '⧉', '从这里分叉']]
      .concat(isLast ? [['continue', '▶', '这条没写完，接着往下写']] : []);
  const bar = document.createElement('div');
  // 最后一条常驻显示：点完停止、或刚写完，鼠标都在下面输入区，
  // 全靠 hover 的话那排按钮就等于不存在，人会以为功能没了
  bar.className = isLast ? 'msg-tools always' : 'msg-tools';
  for (const [act, icon, title] of acts) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.dataset.act = act;
    btn.title = title;
    btn.textContent = icon;
    bar.appendChild(btn);
  }
  return bar;
}

function emptyNode(text) {
  const el = document.createElement('div');
  el.className = 'empty';
  el.textContent = text;
  return el;
}

/** 压缩摘要：更早的剧情折叠成这样，插在被折叠那截消息的后面。 */
function summaryNode(summary) {
  const card = document.createElement('div');
  card.className = 'summary-card';
  card.innerHTML = '<div class="sc-title">📖 前情提要（更早的剧情已压缩）</div>'
    + '<div class="sc-body">' + mdToHtml(summary) + '</div>';
  return card;
}

/** 上一条是用户消息 → 说明 AI 没答上，给出重试入口。 */
export function appendRetryHint(lastContent, stopped) {
  const box = $('#messages');
  if (!box) return null;
  const wrap = document.createElement('div');
  wrap.className = 'retry-hint';
  wrap.innerHTML = (stopped
    ? '<span>你停止了这一轮，内容没有存下来。</span>'
    : '<span>⚠ AI 尚未回复（自动重试仍失败）。</span>')
    + '<button type="button">重试上一条</button>';
  wrap.querySelector('button').onclick = () => {
    wrap.remove();
    document.dispatchEvent(new CustomEvent('rp:retry-last', { detail: { text: lastContent } }));
  };
  box.appendChild(wrap);
  scrollToBottom(true);
  return wrap;
}

export function renderMessages() {
  const box = $('#messages');
  if (!box) return;
  // 有人正在就地改消息就不重建：整表推倒重来会把那个
  // textarea 连同没保存的字一起抹掉。
  // 但只拦同一个会话的重绘：切局、删局时 state.session 已经换了，
  // 还按旧条件跳过，屏幕上就会一直是上一局的内容。
  const editing = box.querySelector('.edit-area');
  if (editing && editing.dataset.sid === ((state.session && state.session.id) || '')) return;
  // 用户往上翻着看时不抢滚动；只有本来就贴底才跟着底。
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.innerHTML = '';

  const s = state.session;
  if (!s) {
    box.appendChild(emptyNode('还没有选择对话。点左上角的「＋ 新对话」开一局，或者在「故事」里先做一份设定。'));
    return;
  }

  const msgs = s.messages || [];
  // 前情提要插在被折叠的那截消息后面，一眼看出它替代的是哪一段；
  // 还没压缩过（游标是 0）就照旧放最前面
  const ctx = s.context || {};
  const upto = Math.min((ctx.compress && ctx.compress.upto) || 0, msgs.length);
  if (s.summary && !upto) box.appendChild(summaryNode(s.summary));
  if (!msgs.length) {
    box.appendChild(emptyNode('故事还没开始。点右上角「设定」写下双方人设与故事背景，'
      + '让 AI 帮你完善并生成开场白，就可以开演了。'));
    return;
  }

  const userLabel = s.user_name || '我';
  msgs.forEach((m, index) => {
    if (s.summary && index === upto) box.appendChild(summaryNode(s.summary));
    let host;
    if (m.role === 'user') {
      host = buildMessage('user', m.content, userLabel, m.edited).el;
    } else if (m.speaker || (m.lines || []).length) {
      // 多角色一轮里每人一段，全在 lines 里；漏传就只剩第一个人的台词
      host = buildTurn(m.narrator || '', m.speaker, m.content, m.lines).el;
    } else {
      // 一般模式：系统叙述，没有角色名
      host = document.createElement('div');
      host.className = 'turn';
      host.appendChild(narratorNode(m.narrator || m.content || '', storyName()));
    }
    host.dataset.index = index;
    // 用户喊停留下的半截：不标出来的话，看着和写完的一轮一模一样
    if (m.partial) host.classList.add('is-partial');
    // 只有最后一条 AI 回复能续写：往前接就不是同一轮了
    host.appendChild(toolsNode(m.role === 'user' ? 'user' : 'assistant', index === msgs.length - 1));
    box.appendChild(host);
  });

  const last = msgs[msgs.length - 1];
  if (last && last.role === 'user') {
    // 后端还在跑的时候刷新页面，磁盘上最后一条当然是用户消息。
    // 这时候不能判成"没答上"，得等它写完自己回来。
    if (state.session && state.session.busy) appendBusyHint();
    else appendRetryHint(last.content || '');
  }
  if (atBottom || !box.scrollTop) scrollToBottom(true);
}

/** 刷新打断的是浏览器这头的流，后端照旧在生成，盯着会话等它落盘。 */
let busyTimer = null;

export function stopBusyWatch() {
  if (busyTimer) {
    clearInterval(busyTimer);
    busyTimer = null;
  }
}

export function appendBusyHint() {
  const box = $('#messages');
  if (!box) return null;
  // 重绘会把提示连节点一起清掉，光看 busyTimer 就以为"还在"，
  // 结果第二次重绘起这份提示永远挂不回来——刷新后既没有「正在写」
  // 也没有「没答上」，屏幕上只剩一条用户消息。认节点，不认计时器。
  const alive = box.querySelector('.retry-hint.busy');
  if (alive) return alive;
  stopBusyWatch();
  const wrap = document.createElement('div');
  wrap.className = 'retry-hint busy';
  wrap.innerHTML = '<span>✍ AI 正在写这一轮（页面刷新不会打断它，写完会自动出现）。</span>';
  box.appendChild(wrap);
  scrollToBottom(true);
  const sid = state.session && state.session.id;
  if (!sid) return wrap;
  busyTimer = setInterval(async () => {
    try {
      const sess = await api('/api/sessions/' + sid);
      if (!sess.busy) {
        stopBusyWatch();
        if (state.session && state.session.id === sid) {
          state.session = sess;
          renderMessages();
          syncAll();
        }
      }
    } catch (e) {
      stopBusyWatch();
    }
  }, 2500);
  return wrap;
}
