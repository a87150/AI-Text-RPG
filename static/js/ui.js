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

export function scrollToBottom() {
  const box = $('#messages');
  if (box) box.scrollTop = box.scrollHeight;
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
  const sel = $('#font-select');
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

    card.append(head, name, role, count, stateField);
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

export function renderContextDetail() {
  const box = $('#ctx-detail');
  if (!box) return;
  const s = state.session;
  box.innerHTML = '';
  if (!s) {
    box.innerHTML = '<div class="char-empty">还没有选择故事。</div>';
    return;
  }
  const c = s.context || { used: s.context_chars || 0, limit: 0 };
  const used = c.used || 0;
  const limit = c.limit || 0;
  const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const comp = c.compress || {};

  const head = document.createElement('div');
  head.className = 'ctx-head';
  head.innerHTML = '<b>' + fmtNumber(used) + '</b> / ' + fmtNumber(limit)
    + ' 字<span class="ctx-pct">' + pct + '%</span>';

  const meter = document.createElement('div');
  meter.className = 'ctx-meter' + (pct >= 90 ? ' full' : (pct >= 70 ? ' warn' : ''));
  const fill = document.createElement('i');
  fill.style.width = pct + '%';
  meter.appendChild(fill);

  const rows = document.createElement('div');
  rows.className = 'ctx-rows';
  const items = [
    ['消息', (c.messages || 0) + ' 条'],
    ['压缩', comp.times ? comp.times + ' 次' : '未压缩'],
    ['已折叠', comp.times
      ? comp.dropped + ' 条 / ' + fmtNumber(comp.dropped_chars) + ' 字' : '—'],
    ['保留最近', (c.keep_recent || 0) + ' 条'],
    ['前情提要', c.summary_chars ? fmtNumber(c.summary_chars) + ' 字' : '无'],
  ];
  for (const [k, v] of items) {
    const row = document.createElement('div');
    row.className = 'ctx-row';
    const label = document.createElement('span');
    label.textContent = k;
    const value = document.createElement('b');
    value.textContent = v;
    row.append(label, value);
    rows.appendChild(row);
  }

  box.append(head, meter, rows);

  const d = c.debug || null;
  if (d && (d.system || (d.lore || []).length || (d.facts || []).length)) {
    const facts = d.facts || [];
    const extra = document.createElement('div');
    extra.className = 'ctx-rows';
    for (const [k, v] of [
      ['本轮发给模型', fmtNumber(d.chars ? d.chars.total : 0) + ' 字 · 约 ' + fmtNumber(d.tokens || 0) + ' token'],
      ['世界书命中', (d.lore || []).length ? (d.lore || []).length + ' 条 / ' + fmtNumber(d.lore_chars) + ' 字' : '无'],
      ['事实账本', facts.length ? facts.length + ' 条 / ' + fmtNumber(d.facts_chars) + ' 字' : '无'],
      ['人设锚定', d.anchored ? '本轮已重插角色核心' : '每 ' + (d.anchor_every || 15) + ' 轮重插一次'],
    ]) {
      const row = document.createElement('div');
      row.className = 'ctx-row';
      const label = document.createElement('span');
      label.textContent = k;
      const value = document.createElement('b');
      value.textContent = v;
      row.append(label, value);
      extra.appendChild(row);
    }
    box.appendChild(extra);

    if ((d.lore || []).length) {
      const sub = document.createElement('div');
      sub.className = 'ctx-sub';
      sub.textContent = '命中的世界书';
      box.appendChild(sub);
      const list = document.createElement('div');
      list.className = 'ctx-lore';
      for (const hit of d.lore) {
        const row = document.createElement('div');
        row.className = 'ctx-lore-row';
        const key = document.createElement('code');
        key.textContent = hit.keys || '';
        const body = document.createElement('span');
        body.textContent = hit.content || '';
        row.append(key, body);
        list.appendChild(row);
      }
      box.appendChild(list);
    }

    if (facts.length) {
      const sub = document.createElement('div');
      sub.className = 'ctx-sub';
      sub.textContent = '事实账本（最近 ' + facts.length + ' 条）';
      box.appendChild(sub);
      const list = document.createElement('div');
      list.className = 'ctx-lore';
      for (const f of facts) {
        const row = document.createElement('div');
        row.className = 'ctx-lore-row';
        const key = document.createElement('code');
        key.textContent = (f.subject || '?') + ' · 第 ' + (f.turn || 0) + ' 轮';
        const body = document.createElement('span');
        body.textContent = f.fact || '';
        row.append(key, body);
        list.appendChild(row);
      }
      box.appendChild(list);
    }

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
    const sub = document.createElement('div');
    sub.className = 'ctx-sub';
    sub.textContent = '历史版本（回滚用）';
    box.appendChild(sub);
    const list = document.createElement('div');
    list.className = 'ctx-snaps';
    list.id = 'ctx-snaps';
    list.textContent = '读取中…';
    box.appendChild(list);
    loadSnapshots(s.id, list);
  }

  const chars = s.characters || [];
  if (chars.length) {
    const sub = document.createElement('div');
    sub.className = 'ctx-sub';
    sub.textContent = '在场角色 · ' + chars.length + ' 人';
    box.appendChild(sub);
    const list = document.createElement('div');
    list.className = 'char-list';
    renderCharacterCards(list, chars);
    box.appendChild(list);
  }
}

/* ------------------------------ 顶部信息 ------------------------------ */
export function syncHeader() {
  // 故事视图里没有"当前对话"，这些操作只对对话有意义
  const storyView = state.sideView === 'stories';
  const on = !!state.session && !storyView;
  for (const id of ['btn-settings', 'btn-compress', 'btn-clear', 'btn-context']) {
    const el = $('#' + id);
    if (!el) continue;
    el.disabled = !on || state.busy;
    // 没选对话时按钮留着一个灰壳只会显得脏，直接收起来
    el.classList.toggle('hidden', !on);
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
  put('我 = ' + (s.user_name || '我') + ' · ');

  const chars = s.characters || [];
  const status = s.status || {};
  const present = status.present || [];
  const away = status.away || [];
  if (!chars.length) {
    put('AI = ' + (s.ai_name || 'AI'));
  } else {
    put('在场 ');
    for (const c of chars) {
      const left = away.includes(c.name)
        || (present.length > 0 && !present.includes(c.name));
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'char-chip' + (left ? ' off' : '');
      btn.dataset.name = c.name;
      btn.title = '点开看这个角色的当前状态';
      btn.textContent = c.name + (left ? '（离场）' : '');
      el.appendChild(btn);
      put(' ');
    }
  }
  put('· ' + ((s.context ? s.context.messages : (s.messages || []).length) || 0) + ' 条');
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
  const c = (s.characters || []).find((x) => x.name === name);
  if (!c) return;

  const status = s.status || {};
  const rows = [];
  if (c.role) rows.push(['设定', c.role]);
  if (c.state) rows.push(['此刻', c.state]);
  if (c.history && c.history.length) {
    rows.push(['近况', c.history.slice(-3).join('；')]);
  }
  const scene = [status.time, status.location].filter(Boolean).join(' · ');
  if (scene) rows.push(['场景', scene + '（全场共用）']);
  if (!rows.length) rows.push(['状态', '还没有记录，聊几轮之后这里会有内容']);

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
  const left = (status2.away || []).includes(name)
    || ((status2.present || []).length > 0 && !(status2.present || []).includes(name));
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'cp-act';
  toggle.textContent = left ? '标回在场' : '标记离场';
  toggle.onclick = () => setCharScene(name, left).catch((e) => {
    toast('改不动：' + e.message, 'error');
  });
  pop.appendChild(toggle);
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
  return marks.map((mk, i) => ({
    tag: mk.tag,
    body: src.slice(mk.bodyStart, i + 1 < marks.length ? marks[i + 1].start : src.length).trim(),
  }));
}

/** 只保留旁白和本轮发言人的台词：别人抢话的段落、新角色/变化指令都不显示。 */
export function pickTurn(text, speaker) {
  const out = { narrator: '', line: '' };
  const narrators = [];
  const lines = [];
  for (const seg of splitSegments(text)) {
    if (seg.tag === '旁白' || seg.tag === '叙述' || seg.tag === '系统') {
      if (seg.body) narrators.push(seg.body);
    } else if (seg.tag === speaker) {
      if (seg.body) lines.push(seg.body);
    } else if (!seg.tag && seg.body) {
      (speaker ? lines : narrators).push(seg.body);
    }
  }
  out.narrator = narrators.join('\n\n');
  out.line = lines.join('\n\n');
  return out;
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

/** 一个 AI 回合 = 旁白 + 一个角色气泡 */
export function buildTurn(narrator, speaker, line) {
  const el = document.createElement('div');
  el.className = 'turn';
  if (narrator) el.appendChild(narratorNode(narrator));
  const msg = buildMessage('assistant', line, speaker);
  el.appendChild(msg.el);
  return { el, msg };
}

/** 落盘后的最终内容，按服务端解析结果重绘（丢弃抢话段落）。 */
export function renderTurn(container, turn) {
  if (!container) return null;
  container.innerHTML = '';
  const isSystem = !turn.speaker;
  if (turn.narrator) container.appendChild(narratorNode(turn.narrator, isSystem ? '系统' : ''));
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
  if (!plain && picked.narrator) container.appendChild(narratorNode(picked.narrator));

  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.style.setProperty('--char-hue', hueOf(speaker || ''));

  const bubble = document.createElement('div');
  bubble.className = 'bubble typing';
  const shown = plain ? (text || '') : picked.line;
  const body = document.createElement('span');
  if (shown) body.innerHTML = mdToHtml(shown);
  else body.innerHTML = '<span class="dots">⋯</span>';
  const tick = document.createElement('span');
  tick.className = 'tick';
  tick.textContent = seconds + 's';
  bubble.append(body, tick);

  el.appendChild(bubble);
  container.appendChild(el);
  return { el, bubble };
}

/** 名字 → 稳定的色相，让每个角色有自己的颜色。 */
export function hueOf(name) {
  let h = 0;
  const s = name || '';
  for (let i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) % 360;
  return h;
}

export function buildMessage(role, content, name) {
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

  el.append(who, bubble);
  return { el, who, bubble };
}

/** 每条消息右下角的操作条。只给自己写的话提供编辑，改 AI 的话没有意义。 */
function toolsNode(role) {
  const acts = role === 'user'
    ? [['edit', '✎', '编辑这句话'], ['rewrite', '↻', '重发这句'], ['branch', '⧉', '从这里分叉']]
    : [['rewrite', '↻', '删掉这条及之后重新生成'], ['branch', '⧉', '从这里分叉']];
  const bar = document.createElement('div');
  bar.className = 'msg-tools';
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

const STATUS_LABELS = [
  ['location', '地点'], ['time', '时间'], ['relations', '关系'],
  ['goal', '目标'], ['items', '物品'],
];

/** 每轮结束时模型给的状态：位置 / 时间 / 关系 / 目标 / 物品。 */
export function statusNode(status) {
  if (!status) return null;
  const rows = STATUS_LABELS.filter(([key]) => status[key]);
  if (!rows.length) return null;
  const el = document.createElement('div');
  el.className = 'status-card';
  const head = document.createElement('div');
  head.className = 'st-head';
  head.textContent = '🧭 当前状态';
  el.appendChild(head);
  for (const [key, label] of rows) {
    const row = document.createElement('div');
    row.className = 'st-row';
    const k = document.createElement('span');
    k.className = 'st-k';
    k.textContent = label;
    const v = document.createElement('span');
    v.className = 'st-v';
    v.textContent = status[key];
    row.append(k, v);
    el.appendChild(row);
  }
  return el;
}

/** 生成结束后只换状态卡，不重建整个消息列表。 */
export function updateStatusCard(status) {
  const box = $('#messages');
  if (!box) return;
  const old = box.querySelector('.status-card');
  if (old) old.remove();
  const node = statusNode(status || {});
  if (!node) return;
  const summary = box.querySelector('.summary-card');
  if (summary && summary.nextSibling) box.insertBefore(node, summary.nextSibling);
  else box.insertBefore(node, box.firstChild);
}

function summaryNode(summary) {
  const card = document.createElement('div');
  card.className = 'summary-card';
  card.innerHTML = '<div class="sc-title">📖 前情提要（更早的剧情已压缩）</div>'
    + '<div class="sc-body">' + mdToHtml(summary) + '</div>';
  return card;
}

/** 上一条是用户消息 → 说明 AI 没答上，给出重试入口。 */
export function appendRetryHint(lastContent) {
  const box = $('#messages');
  if (!box) return null;
  const wrap = document.createElement('div');
  wrap.className = 'retry-hint';
  wrap.innerHTML = '<span>⚠ AI 尚未回复（自动重试仍失败）。</span>'
    + '<button type="button">重试上一条</button>';
  wrap.querySelector('button').onclick = () => {
    wrap.remove();
    document.dispatchEvent(new CustomEvent('rp:retry-last', { detail: { text: lastContent } }));
  };
  box.appendChild(wrap);
  scrollToBottom();
  return wrap;
}

export function renderMessages() {
  const box = $('#messages');
  if (!box) return;
  box.innerHTML = '';

  const s = state.session;
  if (!s) {
    box.appendChild(emptyNode('还没有选择对话。点左上角的「＋ 新对话」开一局，或者在「故事」里先做一份设定。'));
    return;
  }

  if (s.summary) box.appendChild(summaryNode(s.summary));
  const st = statusNode(s.status);
  if (st) box.appendChild(st);

  const msgs = s.messages || [];
  if (!msgs.length) {
    box.appendChild(emptyNode('故事还没开始。点右上角「设定」写下双方人设与故事背景，'
      + '让 AI 帮你完善并生成开场白，就可以开演了。'));
    return;
  }

  const userLabel = s.user_name || '我';
  msgs.forEach((m, index) => {
    let host;
    if (m.role === 'user') {
      host = buildMessage('user', m.content, userLabel).el;
    } else if (m.speaker) {
      host = buildTurn(m.narrator || '', m.speaker, m.content).el;
    } else {
      // 一般模式：系统叙述，没有角色名
      host = document.createElement('div');
      host.className = 'turn';
      host.appendChild(narratorNode(m.narrator || m.content || '', '系统'));
    }
    host.dataset.index = index;
    host.appendChild(toolsNode(m.role === 'user' ? 'user' : 'assistant'));
    box.appendChild(host);
  });

  const last = msgs[msgs.length - 1];
  if (last && last.role === 'user') appendRetryHint(last.content || '');
  scrollToBottom();
}
