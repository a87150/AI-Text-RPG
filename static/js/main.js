/** 装配层：初始化、事件绑定、模型 / 提供方选择与全局错误兜底。 */
import { api } from './api.js';
import { state } from './state.js';
import {
  $, toast, initTheme, toggleTheme, applyFont, initFont, renderMessages, syncAll,
  syncComposer, focusComposer, setConnectionBanner, modalOpen, renderContextDetail,
  closeNav, toggleCharPop, closeCharPop,
} from './ui.js';
import {
  loadSessions, createSession, clearSession, compressSession, switchSideView,
  createStory, loadStories, openPickStory, closePickStory, selectStory,
  renderSessionList, renderStoryList, savedView, savedStory, savedSession,
  openSession,
} from './sessions.js';
import { openBackup, closeBackup, runBackup, saveBackupConf } from './backup.js';
import { sendFromInput, bindRetryEvents } from './chat.js';
import { bindMessageActions } from './msgops.js';
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
  return state.providers.find((p) => p.id === id) || null;
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

function fillProviderForm() {
  const pid = $('#p-provider').value;
  const info = providerById(pid);
  $('#p-base').value = info ? info.base_url : '';
  $('#p-key').value = '';
  $('#p-key').disabled = !!info && info.kind === 'ollama';
  $('#p-key').placeholder = !info ? 'sk-…'
    : (info.kind === 'ollama' ? '本地模型不需要 Key'
      : (info.has_key ? '已保存，留空表示不修改' : 'sk-…'));
  $('#p-models').value = info ? (info.models || []).join(', ') : '';
  setProviderStatus(info && info.kind === 'ollama'
    ? '本地 Ollama：模型列表自动读取，无需配置。'
    : '');
}

function fillProviderSelect() {
  const select = $('#p-provider');
  if (!select) return;
  select.innerHTML = '';
  for (const p of state.providers) {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.label;
    select.appendChild(opt);
  }
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
    ['#w-chars', 'max_characters'],
  ]) {
    const el = $(id);
    if (el) el.value = w[key] == null ? '' : w[key];
  }
  const font = $('#prefs-font');
  if (font) font.value = localStorage.getItem(FONT_KEY) || 'md';
  syncReasoningSeg();
  syncTierSeg();
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
    if (lim.role_chars) bits.push('人设每条 ' + lim.role_chars + ' 字');
    if (lim.facts) bits.push('账本 ' + lim.facts + ' 条');
    if (lim.anchor_every) bits.push('每 ' + lim.anchor_every + ' 轮重插人设');
    hint.textContent = base + (bits.length ? '（' + bits.join(' · ') + '）' : '');
  }
}

async function saveWriting() {
  // 空着的项不提交：后端收到 0 会把参数压到下限
  const body = {};
  for (const [id, key] of [
    ['#w-narrator-min', 'narrator_min'], ['#w-narrator-max', 'narrator_max'],
    ['#w-line-min', 'line_min'], ['#w-line-max', 'line_max'],
    ['#w-context', 'context_limit'], ['#w-keep', 'keep_recent'],
    ['#w-chars', 'max_characters'],
  ]) {
    const el = $(id);
    const raw = el ? String(el.value || '').trim() : '';
    const value = Number(raw);
    if (raw !== '' && Number.isFinite(value)) body[key] = value;
  }
  body.mode = currentMode();
  body.reasoning_effort = currentReasoning();
  body.tier = currentTier();
  try {
    const res = await api('/api/writing', { method: 'POST', body });
    state.conf = Object.assign({}, state.conf, {
      writing: res.writing || state.conf.writing,
      mode: res.mode || currentMode(),
      reasoning_effort: res.reasoning_effort || currentReasoning(),
      tier: res.tier || currentTier(),
    });
    fillWritingForm();
    syncModeSeg();
    syncTierSeg();
    toast('设置已保存', 'ok');
  } catch (e) {
    toast('保存失败：' + e.message, 'error');
  }
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
  const pid = $('#p-provider').value;
  if (!pid) return;
  const btn = $('#p-save');
  btn.disabled = true;
  setProviderStatus('保存中…');
  try {
    const body = {
      base_url: ($('#p-base').value || '').trim(),
      models: ($('#p-models').value || '').trim(),
    };
    const key = ($('#p-key').value || '').trim();
    if (key) body.api_key = key;
    if (clearingKey) body.clear_key = true;
    await api('/api/providers/' + pid, { method: 'POST', body });
    await loadModels(false);
    fillProviderForm();
    setProviderStatus('✅ 已保存', 'ok');
    toast('云端模型设置已保存', 'ok');
  } catch (e) {
    setProviderStatus('❌ ' + e.message, 'error');
  } finally {
    clearingKey = false;
    btn.disabled = false;
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
    setConnectionBanner('');
    await loadModels(false);
  } catch (e) {
    setConnectionBanner('后端不可用：' + e.message);
  } finally {
    if (btn) btn.disabled = false;
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
  $('#btn-open-backup').onclick = openBackup;
  $('#btn-backup-now').onclick = runBackup;
  $('#btn-backup-save').onclick = saveBackupConf;
  $('#btn-backup-close').onclick = closeBackup;
  $('#backup-modal').addEventListener('click', (e) => {
    if (e.target.id === 'backup-modal') closeBackup();
  });
  $('#pick-cancel').onclick = closePickStory;
  $('#pick-story-modal').addEventListener('click', (e) => {
    if (e.target.id === 'pick-story-modal') closePickStory();   // 点背景也能退
  });
  $('#btn-new-story').onclick = newStory;
  $('#btn-new-session').onclick = () => openPickStory();
  $('#btn-send').onclick = sendFromInput;
  $('#btn-settings').onclick = () => openSettings();
  $('#btn-clear').onclick = clearSession;
  $('#btn-compress').onclick = () => compressSession($('#btn-compress'));
  $('#btn-theme').onclick = () => { toggleTheme(); syncThemeSeg(); };
  $('#btn-context').onclick = openContext;
  $('#btn-ctx-close').onclick = closeContext;
  $('#btn-refresh-models').onclick = () => loadModels(true);
  $('#btn-prefs').onclick = openPrefs;
  $('#prefs-close').onclick = closePrefs;
  $('#prefs-save').onclick = saveWriting;
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
  $('#p-clear-key').onclick = clearProviderKey;

  // 点按钮时不让它抢走输入框焦点 —— 这是「发完消息要重新点输入框」的根源
  for (const btn of document.querySelectorAll('button')) {
    btn.addEventListener('mousedown', (e) => e.preventDefault());
  }

  $('#input').addEventListener('input', syncComposer);
  $('#input').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
    e.preventDefault();
    sendFromInput();
  });

  $('#settings-modal').addEventListener('click', (e) => {
    if (e.target.id === 'settings-modal') closeSettings();
  });
  $('#prefs-modal').addEventListener('click', (e) => {
    if (e.target.id === 'prefs-modal') closePrefs();
  });
  $('#ctx-modal').addEventListener('click', (e) => {
    if (e.target.id === 'ctx-modal') closeContext();
  });

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
