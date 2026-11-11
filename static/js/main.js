/** 装配层：初始化、事件绑定、模型 / 提供方选择与全局错误兜底。 */
import { api } from './api.js';
import { state } from './state.js';
import {
  $, toast, initTheme, toggleTheme, applyFont, initFont, renderMessages, syncAll,
  syncComposer, focusComposer, setConnectionBanner, modalOpen, renderContextDetail,
  saveDraftSoon, loadDraft,
  closeNav, toggleCharPop, closeCharPop, scrollToBottom,
} from './ui.js';
import {
  loadSessions, createSession, clearSession, compressSession, switchSideView,
  createStory, loadStories, openPickStory, closePickStory, selectStory,
  renderSessionList, renderStoryList, savedView, savedStory, savedSession,
  openSession,
} from './sessions.js';
import { refreshBackups, runBackup, saveBackupConf } from './backup.js';
import { sendFromInput, bindRetryEvents } from './chat.js';
import { bindMessageActions } from './msgops.js';
import { bindSearch } from './search.js';
import {
  openSettings, closeSettings, saveSettings, enhance, regenOpening,
  newRunFromSettings, setDraftMode, addLoreEntry, popIdea, closeIdea, startEnhance,
  openAsk, closeAsk, runAsk,
} from './settings.js';

let busyNew = false;
let clearingKey = false;

/* ------------------------------ 模型 ------------------------------ */
const MODEL_SEP = '::';

function providerById(id) {
  const all = state.allProviders || state.providers;
  return all.find((p) => p.id === id) || state.providers.find((p) => p.id === id) || null;
}

function buildModelOptions(select) {
  select.innerHTML = '';

  for (const p of state.providers) {
    const group = document.createElement('optgroup');
    group.label = p.label;   // 云端只有配置好之后才会出现在这里

    const list = Array.isArray(p.models) ? p.models : [];
    if (!list.length) {
      const opt = document.createElement('option');
      opt.value = p.id + MODEL_SEP;
      opt.textContent = p.kind === 'ollama' ? '（未获取到本地模型）' : '（未设置模型名）';
      opt.disabled = true;
      group.appendChild(opt);
    }
    for (const name of list) {
      const opt = document.createElement('option');
      opt.value = p.id + MODEL_SEP + name;
      opt.textContent = name;
      group.appendChild(opt);
    }
    select.appendChild(group);
  }

  const cur = state.current;
  if (cur) {
    const key = cur.provider + MODEL_SEP + cur.model;
    const exists = Array.from(select.options).some((o) => o.value === key);
    if (!exists) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = cur.model;
      select.appendChild(opt);
    }
    select.value = key;
    const p = providerById(cur.provider);
    select.title = '当前：' + (p ? p.label : cur.provider) + ' · ' + cur.model;
  } else {
    select.title = '没有可用模型';
  }
}

/** 侧栏与设置窗里的两个下拉保持同一份列表。 */
function fillModelSelect() {
  for (const id of ['#model-select', '#prefs-model']) {
    const select = $(id);
    if (select) buildModelOptions(select);
  }
}

async function loadModels(notify = true) {
  const select = $('#model-select');
  const refresh = $('#btn-refresh-models');
  if (select) select.disabled = true;
  if (refresh) refresh.disabled = true;
  try {
    const data = await api('/api/models');
    state.providers = data.providers || [];
    // /api/models 会跳过还没配好的云端提供方，但设置面板得能把它们列出来，
    // 否则 openai 这种没配 Key 的永远选不到，也就永远配不上。
    try {
      const all = await api('/api/providers');
      state.allProviders = Array.isArray(all) ? all : state.providers;
    } catch (e) {
      state.allProviders = state.providers;
    }
    state.current = data.current || null;
    state.model = state.current ? state.current.model : '';
    fillModelSelect();
    if (data.error && notify) toast(data.error, 'error');
  } catch (e) {
    setConnectionBanner(e.message);
    if (notify) toast('读取模型列表失败：' + e.message, 'error');
  } finally {
    if (refresh) refresh.disabled = false;
    if (select) select.disabled = false;
    syncAll();
  }
}

/** 设置面板的折叠状态：点标题开合，记到 localStorage，刷新后照旧。 */
const PREFS_KEY = 'lt.prefs';
const PREFS_OPEN = { mode: true, model: true };   // 头一次打开时先展开这两节

function bindPrefsSections() {
  const secs = [...document.querySelectorAll('#prefs-modal details[data-sec]')];
  if (!secs.length) return;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(PREFS_KEY) || 'null'); } catch { saved = null; }
  for (const el of secs) {
    // 先定初始状态再挂监听，免得初始化自己写一遍存储
    const want = saved && typeof saved[el.dataset.sec] === 'boolean'
      ? saved[el.dataset.sec]
      : !!PREFS_OPEN[el.dataset.sec];
    el.open = want;
    el.addEventListener('toggle', () => {
      const map = {};
      for (const s of secs) map[s.dataset.sec] = s.open;
      try { localStorage.setItem(PREFS_KEY, JSON.stringify(map)); } catch { /* 隐私模式写不进去就算了 */ }
      if (el.dataset.sec === 'backup' && el.open) refreshBackups();
    });
  }
}

async function onModelChange(select) {
  select = select || $('#model-select');
  if (!select) return;
  const raw = select.value || '';
  const idx = raw.indexOf(MODEL_SEP);
  if (idx < 0) return;
  const provider = raw.slice(0, idx);
  const model = raw.slice(idx + MODEL_SEP.length);

  if (!model) {
    toast('先在设置的「云端模型」里填一个模型名', 'error');
    openPrefs();
    return;
  }
  const info = providerById(provider);
  if (info && info.kind === 'cloud' && !info.has_key) {
    toast(info.label + ' 还没有配置 API Key', 'error');
    openPrefs();
    return;
  }

  const previous = state.current;
  select.disabled = true;
  try {
    const result = await api('/api/model', { method: 'POST', body: { provider, model } });
    state.current = { provider: result.provider, model: result.model };
    state.model = result.model;
    toast('已切换模型：' + result.model, 'ok');
  } catch (e) {
    state.current = previous;
    toast('切换模型失败：' + e.message, 'error');
  } finally {
    select.disabled = false;      // 否则切过一次下拉就永久变灰
    fillModelSelect();
    syncAll();
  }
}

/* ------------------------------ 云端模型设置 ------------------------------ */
function setProviderStatus(text, kind = '') {
  const el = $('#p-status');
  if (!el) return;
  el.textContent = text;
  el.className = kind;
}

const NEW_PROVIDER = '__new__';

function fillProviderForm() {
  const pid = $('#p-provider').value;
  const info = providerById(pid);
  const isNew = pid === NEW_PROVIDER;
  const local = !!info && info.kind === 'ollama';

  $('#p-label-field').hidden = !isNew && !(info && info.custom);
  $('#p-label').value = isNew ? '' : ((info && info.custom && info.label) || '');
  $('#p-base').value = isNew || !info ? '' : info.base_url;
  $('#p-base').disabled = local;
  $('#p-key').value = '';
  $('#p-key').disabled = local;
  $('#p-key').placeholder = isNew || !info ? 'sk-…'
    : (local ? '本地模型不需要 Key'
      : (info.has_key ? '已保存，留空表示不修改' : 'sk-…'));
  // 本地模型列表由 Ollama 自己报，手填一个云端模型名只会 404
  $('#p-models').value = isNew || !info || local ? '' : (info.models || []).join(', ');
  $('#p-models').disabled = local;
  $('#p-models-hint').textContent = local ? '本地模型自动读取，不能手填。' : '';
  const del = $('#p-del');
  if (del) del.hidden = !(info && info.custom);
  setProviderStatus(local ? '本地 Ollama：模型列表自动读取，无需配置。' : '');
}

function nextCustomId() {
  const used = new Set(state.providers.map((p) => p.id));
  for (let i = 1; i < 100; i += 1) {
    const id = 'custom' + i;
    if (!used.has(id)) return id;
  }
  return 'custom' + Date.now();
}

function fillProviderSelect() {
  const select = $('#p-provider');
  if (!select) return;
  select.innerHTML = '';
  for (const p of (state.allProviders || state.providers)) {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.label;
    select.appendChild(opt);
  }
  const add = document.createElement('option');
  add.value = NEW_PROVIDER;
  add.textContent = '＋ 新增提供方…';
  select.appendChild(add);
  const preferred = state.current && providerById(state.current.provider)
    ? state.current.provider
    : (state.providers.some((p) => p.id === 'deepseek') ? 'deepseek' : '');
  if (preferred) select.value = preferred;
}

/* ------------------------------ 设置窗：外观与写作 ------------------------------ */
function currentTheme() {
  return document.documentElement.getAttribute('data-theme') || 'dark';
}

const MODE_HINTS = {
  normal: '一般模式：系统用旁白口吻叙述场景与角色反应。只影响之后新建的对话。',
  rp: '角色扮演：按角色表轮流发言，每轮只有一位角色说话。只影响之后新建的对话。',
};

function currentMode() {
  return (state.conf && state.conf.mode) || 'normal';
}

function syncModeSeg() {
  const seg = $('#prefs-mode');
  const mode = currentMode();
  if (seg) {
    for (const btn of seg.querySelectorAll('button')) {
      btn.classList.toggle('active', btn.dataset.modeValue === mode);
    }
  }
  const hint = $('#mode-hint');
  if (hint) hint.textContent = MODE_HINTS[mode] || '';
}

function syncThemeSeg() {
  const seg = $('#prefs-theme');
  if (!seg) return;
  for (const btn of seg.querySelectorAll('button')) {
    btn.classList.toggle('active', btn.dataset.themeValue === currentTheme());
  }
}

const FONT_KEY = 'rp-font';

function fillWritingForm() {
  const w = (state.conf && state.conf.writing) || {};
  for (const [id, key] of [
    ['#w-narrator-min', 'narrator_min'], ['#w-narrator-max', 'narrator_max'],
    ['#w-line-min', 'line_min'], ['#w-line-max', 'line_max'],
    ['#w-context', 'context_limit'], ['#w-keep', 'keep_recent'],
    ['#w-chars', 'max_characters'], ['#w-summary', 'summary_chars'],
    ['#w-roles', 'auto_roles'],
  ]) {
    const el = $(id);
    if (el) el.value = w[key] == null ? '' : w[key];
  }
  const font = $('#prefs-font');
  if (font) font.value = localStorage.getItem(FONT_KEY) || 'md';
  syncReasoningSeg();
  syncTierSeg();
  syncCreativitySeg();
}

function currentReasoning() {
  return (state.conf && state.conf.reasoning_effort) || 'medium';
}

function syncReasoningSeg() {
  const seg = $('#reasoning-seg');
  if (!seg) return;
  for (const btn of seg.querySelectorAll('button')) {
    btn.classList.toggle('active', btn.dataset.level === currentReasoning());
  }
}

function currentCreativity() {
  return (state.conf && state.conf.creativity) || 'standard';
}

function syncCreativitySeg() {
  const seg = $('#creativity-seg');
  if (!seg) return;
  for (const btn of seg.querySelectorAll('button')) {
    btn.classList.toggle('active', btn.dataset.creativity === currentCreativity());
  }
  const hint = $('#creativity-hint');
  const temps = (state.conf && state.conf.creativity_temps) || {};
  const temp = temps[currentCreativity()];
  if (hint && temp) {
    const base = (state.conf.creativity_labels || {})[currentCreativity()] || '';
    hint.textContent = base + '（温度 ' + temp + '）';
  }
}

function currentTier() {
  return (state.conf && state.conf.tier) || 'basic';
}

function syncTierSeg() {
  const seg = $('#tier-seg');
  if (!seg) return;
  for (const btn of seg.querySelectorAll('button')) {
    btn.classList.toggle('active', btn.dataset.tier === currentTier());
  }
  const hint = $('#tier-hint');
  if (hint) {
    const labels = (state.conf && state.conf.tier_labels) || {};
    const budget = (state.conf && state.conf.tier_context) || 0;
    const base = labels[currentTier()] || '初级给单行标记加两条示例，高级直接要结构化 JSON。';
    const lim = (state.conf && state.conf.tier_limits) || {};
    const bits = [];
    if (budget) bits.push('上下文 ' + budget + ' 字');
    if (lim.history_msgs) bits.push('历史 ' + lim.history_msgs + ' 条');
    const keep = (state.conf && state.conf.tier_keep) || 0;
    if (keep) bits.push('压缩后留 ' + keep + ' 条');
    if (lim.role_chars) bits.push('人设每条 ' + lim.role_chars + ' 字');
    if (lim.facts) bits.push('账本 ' + lim.facts + ' 条');
    if (lim.anchor_every) bits.push('每 ' + lim.anchor_every + ' 轮重插人设');
    hint.textContent = base + (bits.length ? '（' + bits.join(' · ') + '）' : '');
  }
}

const SAVE_LABELS = { mode: '模式', model: '模型设置', writing: '写作参数' };

/** 分节保存共用一条路径：表单里的值本来就是一份，分开提交只会多出两条出错分支。 */
async function saveSection(btn) {
  const sec = (btn && btn.dataset.save) || 'writing';
  const label = SAVE_LABELS[sec] || '设置';
  const note = document.querySelector('.prefs-note[data-note="' + sec + '"]');
  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
  const ok = await saveWriting(true);
  if (btn) { btn.disabled = false; btn.textContent = '保存' + label; }
  if (note) {
    note.textContent = ok ? label + '已保存' : '保存失败，检查刚才的数字有没有填错';
    note.className = 'prefs-note ' + (ok ? 'ok' : 'bad');
  }
}

async function resetAll() {
  if (!confirm('把模式、档位、思考强度、创意程度、写作字数都恢复默认？\n模型选择和云端 Key 不动。')) return;
  try {
    await api('/api/writing/reset', { method: 'POST' });
    state.conf = await api('/api/config');
    applyFont('md');
    fillWritingForm();
    syncModeSeg();
    syncTierSeg();
    syncReasoningSeg();
    syncCreativitySeg();
    toast('已恢复默认', 'ok');
  } catch (e) {
    toast('恢复默认失败：' + e.message, 'error');
  }
}

async function saveWriting(quiet = false) {
  // 空着的项不提交：后端收到 0 会把参数压到下限
  const body = {};
  for (const [id, key] of [
    ['#w-narrator-min', 'narrator_min'], ['#w-narrator-max', 'narrator_max'],
    ['#w-line-min', 'line_min'], ['#w-line-max', 'line_max'],
    ['#w-context', 'context_limit'], ['#w-keep', 'keep_recent'],
    ['#w-chars', 'max_characters'], ['#w-summary', 'summary_chars'],
    ['#w-roles', 'auto_roles'],
  ]) {
    const el = $(id);
    const raw = el ? String(el.value || '').trim() : '';
    const value = Number(raw);
    if (raw !== '' && Number.isFinite(value)) body[key] = value;
  }
  body.mode = currentMode();
  body.reasoning_effort = currentReasoning();
  body.tier = currentTier();
  body.creativity = currentCreativity();
  try {
    const res = await api('/api/writing', { method: 'POST', body });
    state.conf = Object.assign({}, state.conf, {
      writing: res.writing || state.conf.writing,
      mode: res.mode || currentMode(),
      reasoning_effort: res.reasoning_effort || currentReasoning(),
      tier: res.tier || currentTier(),
      creativity: res.creativity || currentCreativity(),
    });
    fillWritingForm();
    syncModeSeg();
    syncTierSeg();
    syncReasoningSeg();
    syncCreativitySeg();
    if (!quiet) toast('设置已保存', 'ok');
    return true;
  } catch (e) {
    if (!quiet) toast('保存失败：' + e.message, 'error');
    return false;
  }
}

/**
 * 点遮罩关闭。不能只看 e.target：从弹窗里按下再滑到遮罩上松手，click 会派发给
 * 两者的共同祖先（也就是遮罩），拖动选字、滑一下就整窗消失。得按下和松开都
 * 在遮罩上、且没移动超过一点，才算真的点了遮罩。
 */
function bindBackdropClose(modalId, close) {
  const el = document.getElementById(modalId);
  if (!el) return;
  let onBackdrop = false;
  let downX = 0;
  let downY = 0;
  el.addEventListener('pointerdown', (e) => {
    onBackdrop = e.target === el;
    downX = e.clientX;
    downY = e.clientY;
  });
  el.addEventListener('click', (e) => {
    const moved = Math.hypot(e.clientX - downX, e.clientY - downY) > 8;
    const hit = e.target === el && onBackdrop && !moved;
    onBackdrop = false;
    if (hit) close();
  });
}

function openPrefs() {
  fillProviderSelect();
  fillProviderForm();
  fillModelSelect();
  fillWritingForm();
  syncModeSeg();
  syncThemeSeg();
  $('#prefs-modal').classList.remove('hidden');
}

function closePrefs() {
  $('#prefs-modal').classList.add('hidden');
  setTimeout(() => focusComposer({ force: true }), 0);
}

async function saveProviderForm() {
  let pid = $('#p-provider').value;
  if (!pid) return;
  const isNew = pid === NEW_PROVIDER;
  if (isNew) pid = nextCustomId();
  const btn = $('#p-save');
  btn.disabled = true;
  setProviderStatus('保存中…');
  try {
    const body = {
      base_url: ($('#p-base').value || '').trim(),
      models: ($('#p-models').value || '').trim(),
    };
    if (isNew || (providerById(pid) || {}).custom) body.label = ($('#p-label').value || '').trim();
    if (isNew && !body.label) throw new Error('先给它起个名字');
    const key = ($('#p-key').value || '').trim();
    if (key) body.api_key = key;
    if (clearingKey) body.clear_key = true;
    await api('/api/providers/' + pid, { method: 'POST', body });
    await loadModels(false);
    fillProviderSelect();
    $('#p-provider').value = pid;
    fillProviderForm();
    setProviderStatus('✅ 已保存', 'ok');
    toast(isNew ? '新提供方已加上' : '云端模型设置已保存', 'ok');
  } catch (e) {
    setProviderStatus('❌ ' + e.message, 'error');
  } finally {
    clearingKey = false;
    btn.disabled = false;
  }
}

async function removeProvider() {
  const pid = $('#p-provider').value;
  const info = providerById(pid);
  if (!info || !info.custom) return;
  if (!confirm('删掉提供方「' + info.label + '」？它的地址和 Key 都会一起清掉。')) return;
  try {
    await api('/api/providers/' + pid, { method: 'DELETE' });
    await loadModels(false);
    fillProviderSelect();
    fillProviderForm();
    toast('已删除 ' + info.label, 'ok');
  } catch (e) {
    setProviderStatus('❌ ' + e.message, 'error');
  }
}

async function clearProviderKey() {
  const pid = $('#p-provider').value;
  const info = providerById(pid);
  if (!pid || !info || !info.has_key) {
    setProviderStatus('这个提供方还没有保存 Key');
    return;
  }
  clearingKey = true;
  try {
    await saveProviderForm();
    setProviderStatus('已清除保存的 Key', 'ok');
  } finally {
    clearingKey = false;
  }
}

/* ------------------------------ 新建 ------------------------------ */
/** 故事列表顶上的「＋」：建一份空设定，直接在右侧打开它。 */
async function newStory() {
  if (busyNew || state.busy) return;
  busyNew = true;
  const btn = $('#btn-new-story');
  if (btn) btn.disabled = true;
  try {
    switchSideView('stories');
    const story = await createStory();
    await selectStory(story.id);
  } catch (e) {
    toast('新建故事失败：' + e.message, 'error');
  } finally {
    busyNew = false;
    if (btn) btn.disabled = false;
  }
}

async function refreshBackendStatus() {
  const btn = $('#btn-retry-conn');
  if (btn) btn.disabled = true;
  try {
    state.conf = await api('/api/config');
    const ver = $('#app-ver');
    if (ver) ver.textContent = state.conf.version ? 'v' + state.conf.version : '';
    setConnectionBanner('');
    await loadModels(false);
  } catch (e) {
    setConnectionBanner('后端不可用：' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** 把导出出去的会话 JSON 装回来：既是导出的对称，也是换机器时的底。 */
async function importSessionFile(e) {
  const input = e.target;
  const file = input.files && input.files[0];
  input.value = '';                     // 清掉选择，同一个文件能再选一次
  if (!file) return;
  try {
    const raw = await file.text();
    let data;
    try { data = JSON.parse(raw); } catch (err) {
      toast('这不是一份 JSON 文件', 'error');
      return;
    }
    const r = await api('/api/sessions/import', { method: 'POST', body: data });
    await loadSessions();
    switchSideView('sessions');
    await openSession(r.id);
    toast('已导入 ' + r.messages + ' 条消息、' + r.characters + ' 个角色', 'ok');
  } catch (err) {
    toast('导入失败：' + err.message, 'error');
  }
}

function openContext() {
  renderContextDetail();
  $('#ctx-modal').classList.remove('hidden');
}

function closeContext() {
  $('#ctx-modal').classList.add('hidden');
  setTimeout(() => focusComposer({ force: true }), 0);
}

/* ------------------------------ 绑定 ------------------------------ */
function bindUI() {
  // 侧栏高度变了，一页能装几条也跟着变
  let resizeTimer = 0;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      renderSessionList();
      renderStoryList();
    }, 200);
  });
  $('#btn-nav').onclick = () => document.body.classList.toggle('nav-open');
  $('#nav-mask').onclick = closeNav;
  $('#btn-backup-now').onclick = runBackup;
  $('#btn-backup-save').onclick = saveBackupConf;
  bindPrefsSections();
  $('#pick-cancel').onclick = closePickStory;
  $('#pick-story-modal').addEventListener('click', (e) => {
    if (e.target.id === 'pick-story-modal') closePickStory();   // 点背景也能退
  });
  $('#btn-new-story').onclick = newStory;
  $('#btn-new-session').onclick = () => openPickStory();
  $('#btn-import-session').onclick = () => $('#import-file').click();
  $('#import-file').onchange = importSessionFile;
  $('#btn-send').onclick = sendFromInput;
  $('#btn-settings').onclick = () => openSettings();
  $('#btn-clear').onclick = clearSession;
  $('#btn-export').onclick = () => {
    if (!state.session) return;
    const name = (state.session.title || 'story').replace(/[\\/:*?"<>|]/g, '');
    const a = document.createElement('a');
    a.href = '/api/sessions/' + state.session.id + '/export.md';
    a.download = name + '.md';
    a.click();
    toast('已导出 ' + name + '.md', 'ok');
  };
  $('#btn-compress').onclick = () => compressSession($('#btn-compress'));
  $('#btn-theme').onclick = () => { toggleTheme(); syncThemeSeg(); };
  $('#btn-context').onclick = openContext;
  $('#btn-ctx-close').onclick = closeContext;
  $('#btn-refresh-models').onclick = () => loadModels(true);
  $('#btn-prefs').onclick = openPrefs;
  $('#prefs-close').onclick = closePrefs;
  $('#prefs-reset').onclick = resetAll;
  for (const btn of document.querySelectorAll('#prefs-modal .prefs-save')) {
    btn.onclick = () => saveSection(btn);
  }
  $('#tier-seg').onclick = (e) => {
    const btn = e.target.closest('button[data-tier]');
    if (!btn) return;
    state.conf = Object.assign({}, state.conf, { tier: btn.dataset.tier });
    syncTierSeg();
    // 后端会按档位重置上下文预算，存完把新预算填回表单
    saveWriting().then(async () => {
      state.conf = await api('/api/config');
      fillWritingForm();
      syncTierSeg();
    });
  };
  $('#reasoning-seg').onclick = (e) => {
    const btn = e.target.closest('button[data-level]');
    if (!btn) return;
    state.conf = Object.assign({}, state.conf, { reasoning_effort: btn.dataset.level });
    syncReasoningSeg();
  };
  $('#creativity-seg').onclick = (e) => {
    const btn = e.target.closest('button[data-creativity]');
    if (!btn) return;
    state.conf = Object.assign({}, state.conf, { creativity: btn.dataset.creativity });
    syncCreativitySeg();
  };
  $('#prefs-refresh').onclick = () => loadModels(true);
  $('#prefs-font').onchange = (e) => applyFont(e.target.value);
  $('#prefs-model').onchange = (e) => onModelChange(e.target);
  $('#prefs-mode').onclick = (e) => {
    const btn = e.target.closest('button[data-mode-value]');
    if (!btn) return;
    state.conf = Object.assign({}, state.conf, { mode: btn.dataset.modeValue });
    syncModeSeg();
  };
  $('#prefs-theme').onclick = (e) => {
    const btn = e.target.closest('button[data-theme-value]');
    if (!btn || btn.dataset.themeValue === currentTheme()) return;
    toggleTheme();
    syncThemeSeg();
  };
  $('#btn-retry-conn').onclick = refreshBackendStatus;
  $('#model-select').onchange = (e) => onModelChange(e.target);

  $('#btn-enhance').onclick = () => popIdea();
  $('#btn-idea-start').onclick = startEnhance;
  $('#btn-idea-cancel').onclick = closeIdea;
  $('#btn-ask-cancel').onclick = closeAsk;
  $('#btn-ask-start').onclick = runAsk;
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.field-ai');
    if (!btn) return;
    e.preventDefault();
    const field = btn.dataset.field;
    const map = {
      user_role: { key: 'user_role', label: '我扮演的角色', sel: '#f-user-role' },
      world: { key: 'world', label: '故事背景与世界观', sel: '#f-world' },
      opening: { key: 'opening', label: '开场白', sel: '#f-opening' },
    };
    const item = map[field];
    if (!item) return;
    openAsk({
      field,
      label: item.label,
      read: () => ($(item.sel).value || ''),
      write: (text) => { $(item.sel).value = text; },
      key: item.key,
    });
  });
  $('#btn-regen-opening').onclick = regenOpening;
  $('#btn-new-run').onclick = newRunFromSettings;
  $('#chat-sub').onclick = (e) => {
    const chip = e.target.closest('.char-chip');
    if (chip) toggleCharPop(chip.dataset.name, chip);
  };
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#char-pop') && !e.target.closest('.char-chip')) closeCharPop();
  });
  $('#side-seg').onclick = (e) => {
    const btn = e.target.closest('button[data-view]');
    if (btn) switchSideView(btn.dataset.view);
  };
  $('#btn-add-lore').onclick = addLoreEntry;
  $('#f-mode').onclick = (e) => {
    const btn = e.target.closest('button[data-mode-value]');
    if (!btn) return;
    setDraftMode(btn.dataset.modeValue);
  };
  $('#btn-save').onclick = saveSettings;
  $('#btn-cancel').onclick = closeSettings;

  $('#p-provider').onchange = fillProviderForm;
  $('#p-save').onclick = saveProviderForm;
  $('#p-del').onclick = removeProvider;
  $('#p-clear-key').onclick = clearProviderKey;

  // 点按钮时不让它抢走输入框焦点 —— 这是「发完消息要重新点输入框」的根源
  for (const btn of document.querySelectorAll('button')) {
    btn.addEventListener('mousedown', (e) => e.preventDefault());
  }

  $('#jump-latest').onclick = () => scrollToBottom(true);
$('#input').addEventListener('input', () => { syncComposer(); saveDraftSoon(); });
  $('#input').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
    e.preventDefault();
    sendFromInput();
  });

  bindBackdropClose('settings-modal', closeSettings);
  bindBackdropClose('prefs-modal', closePrefs);
  bindBackdropClose('ctx-modal', closeContext);

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !modalOpen()) return;
    if (!$('#prefs-modal').classList.contains('hidden')) closePrefs();
    else if (!$('#ctx-modal').classList.contains('hidden')) closeContext();
    else closeSettings();
  });

  window.addEventListener('unhandledrejection', (e) => {
    const reason = e.reason;
    if (reason && reason.name === 'ApiError') return; // 已由各调用点提示
    toast('出现未预期的错误：' + ((reason && reason.message) || reason), 'error');
  });
}

/* ------------------------------ 启动 ------------------------------ */
async function boot() {
  initTheme();
  initFont();
  bindRetryEvents();
  bindMessageActions();
  bindSearch();
  bindUI();
  await refreshBackendStatus();
  await loadSessions();
  await loadStories();
  // 上次停在故事页就回故事页，连选中的那份一起还原
  const view = savedView();
  if (view === 'stories') {
    const storyId = savedStory();
    if (storyId && (state.stories || []).some((s) => s.id === storyId)) {
      await selectStory(storyId);
    } else {
      switchSideView('stories');
    }
  } else {
    switchSideView('sessions');
    const sid = savedSession();
    if (sid && (state.sessions || []).some((s) => s.id === sid)) await openSession(sid);
  }
  renderMessages();
  syncAll();
  focusComposer({ force: true });
}

boot();
