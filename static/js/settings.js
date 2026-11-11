/** 设定弹窗：角色卡 / 世界观 / 开场白，含 AI 两步扩写。 */
import { api, streamSSE, StreamEventError } from './api.js';
import { state } from './state.js';
import {
  $, toast, renderMessages, syncAll, focusComposer, renderCharacterEditor,
} from './ui.js';
import {
  loadSessions, newRun, refreshCurrentSession, loadStories, switchSideView,
} from './sessions.js';

/* 单项重生成：记下改之前的取值，「撤销」就还原它 */
const fieldUndo = new Map();
let askTarget = null;

let busySave = false;
let busyEnhance = false;
let busyOpening = false;
let draftChars = [];
let draftLore = [];
let draftMode = 'normal';
let editingStory = null;   // 非空 = 这次改的是故事本身，而不是某一局对话

function renderLoreList() {
  const box = $('#lore-list');
  if (!box) return;
  box.innerHTML = '';
  if (!draftLore.length) {
    const empty = document.createElement('div');
    empty.className = 'lore-empty';
    empty.textContent = '还没有条目。例：关键词「某酒馆」，内容写它的规矩、老板、暗号。';
    box.appendChild(empty);
    return;
  }
  draftLore.forEach((item, i) => {
    const card = document.createElement('div');
    card.className = 'lore-item';

    const keys = document.createElement('input');
    keys.className = 'lore-keys';
    keys.placeholder = '关键词，逗号分隔：某酒馆,酒馆';
    keys.value = item.keys || '';
    keys.oninput = () => { draftLore[i].keys = keys.value; };

    const body = document.createElement('textarea');
    body.className = 'lore-body';
    body.placeholder = '这段设定：只写事实，一小段就够';
    body.value = item.content || '';
    body.oninput = () => { draftLore[i].content = body.value; };

    const head = document.createElement('div');
    head.className = 'lore-head';
    const ai = document.createElement('button');
    ai.type = 'button';
    ai.className = 'field-ai';
    ai.textContent = '✦ AI';
    ai.title = '让 AI 按当前设定写这一条';
    ai.onclick = () => openAsk({
      field: 'lore',
      label: '世界书第 ' + (i + 1) + ' 条',
      read: () => (draftLore[i].content || ''),
      write: (value) => {
        const card2 = draftLore[i];
        if (!card2) return;
        const cut = value.indexOf('\n');
        const first = cut >= 0 ? value.slice(0, cut) : value;
        if (!(card2.keys || '').trim() && /[,，]|^[^。，,]{2,12}$/.test(first.trim())) {
          card2.keys = first.replace(/^关键词[:：]\s*/, '').replace(/[；。]/g, ',').trim();
          card2.content = cut >= 0 ? value.slice(cut + 1).trim() : first;
        } else {
          card2.content = value;
        }
        renderLoreList();
      },
      key: 'lore' + i,
      index: i,
    });

    const undo = document.createElement('button');
    undo.type = 'button';
    undo.className = 'lore-undo field-undo hidden';
    undo.textContent = '↺';
    undo.title = '撤销这一条的改动';
    undo.dataset.undoSlot = 'lore' + i;

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'lore-del';
    del.textContent = '✕';
    del.title = '删除这一条';
    del.onclick = () => { draftLore.splice(i, 1); renderLoreList(); };

    head.append(ai, undo, del);
    card.append(head, keys, body);
    box.appendChild(card);
  });
  syncUndoButtons();
}

export function addLoreEntry() {
  draftLore.push({ keys: '', content: '' });
  renderLoreList();
  const box = $('#lore-list');
  const last = box && box.querySelector('.lore-item:last-child .lore-keys');
  if (last) last.focus();
}

export function setDraftMode(value) {
  draftMode = value === 'rp' ? 'rp' : 'normal';
  syncModeSeg();
  syncCharLabel();
}

function syncModeSeg() {
  const seg = $('#f-mode');
  if (!seg) return;
  for (const btn of seg.querySelectorAll('button')) {
    btn.classList.toggle('active', btn.dataset.modeValue === draftMode);
  }
}

const val = (sel) => ($(sel).value || '').trim();

function setStatus(text, kind = '') {
  const el = $('#enhance-status');
  if (!el) return;
  el.textContent = text;
  el.className = kind;
}

/**
 * 设定弹窗。不带参数 = 改「当前这一局对话」的设定；
 * 传 storyId = 改「故事本身」的设定，跟任何一局对话都无关。
 */
export async function openSettings(opts = {}) {
  if (busySave || busyEnhance || busyOpening) { toast('正在处理，请稍候', 'error'); return; }
  let s;
  if (opts.storyId) {
    try {
      s = await api('/api/stories/' + opts.storyId);
    } catch (e) {
      toast('读取故事失败：' + e.message, 'error');
      return;
    }
    editingStory = s.id;
    s.mode = (state.conf && state.conf.mode) || 'normal';
  } else {
    if (!state.session) { toast('先从「对话」里选一个，或去「故事」里挑一份设定', 'error'); return; }
    editingStory = null;
    s = state.session;
  }

  const heading = $('#settings-title');
  if (heading) heading.textContent = editingStory ? '故事设定' : '对话设定';
  const regen = $('#btn-regen-opening');
  if (regen) regen.classList.toggle('hidden', !!editingStory);
  // 故事设定常驻右侧；对话设定才用弹窗
  const inline = !!editingStory;
  mountSettingsForm(inline ? 'pane' : 'modal');
  // 面板显隐只由 switchSideView 决定：这里再动一次会和异步取数抢，切走了又冒出来
  const empty = $('#pane-empty');
  if (empty) empty.classList.toggle('hidden', inline);

  $('#f-story-title').value = s.title || '';
  $('#f-user-name').value = s.user_name || '';
  $('#f-user-role').value = s.user_role || '';
  $('#f-world').value = s.world || '';
  $('#f-rules').value = s.rules || '';
  $('#f-opening').value = s.opening || '';
  const limit = (state.conf && state.conf.writing && state.conf.writing.max_characters) || 4;
  const meta = $('#story-meta');
  if (meta) {
    meta.textContent = editingStory
      ? '改的是故事本身：保存后，之后用这份设定开的每一局都用新内容，已经开过的对话不受影响。'
      : '改的是这一局对话：保存只影响当前对话。想让以后的局都用上，去「故事」里改故事本身。';
  }
  draftMode = s.mode || 'normal';
  syncModeSeg();
  draftLore = JSON.parse(JSON.stringify(s.lore || []));
  renderLoreList();
  draftChars = JSON.parse(JSON.stringify(s.characters || []));
  if (!draftChars.length) {
    draftChars = [{ name: '', role: '', state: '', history: [] }];
  }
  renderCharacterList();
  setStatus('');
  if (editingStory) {
    $('#settings-modal').classList.add('hidden');
  } else {
    $('#settings-modal').classList.remove('hidden');
    setTimeout(() => { const el = $('#f-user-name'); if (el) el.focus(); }, 0);
  }
}

/* ------------------------------ 单项重生成 ------------------------------ */

/** 打开「只改这一项」浮层；target 说明生成结果写回哪里。 */
export function openAsk(target) {
  if (busyEnhance) { toast('正在生成，等这一项写完', 'error'); return; }
  askTarget = target;
  const pop = $('#ask-pop');
  if (!pop) return;
  const title = $('#ask-title');
  if (title) title.textContent = '只改这一项：' + (target.label || '这一项');
  const note = $('#ask-note');
  if (note) {
    note.value = '';
    note.placeholder = target.sample || '写下这次想怎么改，只重写这一项';
  }
  pop.classList.remove('hidden');
  if (note) note.focus();
}

export function closeAsk() {
  const pop = $('#ask-pop');
  if (pop) pop.classList.add('hidden');
  askTarget = null;
}

function undoButtonFor(key) {
  const row = document.querySelector('[data-undo-slot="' + key + '"]');
  return row;
}

/** 生成完把旧值存起来，字段旁边出现一个可以撤回的按钮。 */
function markUndo(key, oldValue, restore, label) {
  fieldUndo.set(key, { oldValue, restore, label });
  const slot = undoButtonFor(key);
  if (!slot) return;
  slot.classList.remove('hidden');
  slot.onclick = (e) => {
    e.preventDefault();
    const saved = fieldUndo.get(key);
    if (!saved) return;
    saved.restore(saved.oldValue);
    fieldUndo.delete(key);
    // 角色卡重渲染后按钮是新建的，按 key 再找一次
    const again = undoButtonFor(key);
    if (again) again.classList.add('hidden');
    setStatus('已撤回「' + (saved.label || '这一项') + '」', '');
  };
}

/** 面板重渲染后，把还有旧值可撤的字段按钮重新点亮。 */
export function syncUndoButtons() {
  for (const key of fieldUndo.keys()) {
    const slot = undoButtonFor(key);
    if (slot) {
      slot.classList.remove('hidden');
      slot.onclick = (e) => {
        e.preventDefault();
        const saved = fieldUndo.get(key);
        if (!saved) return;
        saved.restore(saved.oldValue);
        fieldUndo.delete(key);
        const again = undoButtonFor(key);
        if (again) again.classList.add('hidden');
        setStatus('已撤回「' + (saved.label || '这一项') + '」', '');
      };
    }
  }
}

export async function runAsk() {
  const target = askTarget;
  const note = val('#ask-note');
  if (!target) return;
  if (!note) { setStatus('先写一句你想怎么改', 'error'); return; }
  closeAsk();
  busyEnhance = true;
  const btn = $('#btn-ask-start');
  let text = '';
  let err = '';
  try {
    await streamSSE('/api/enhance', {
      field: target.field,
      name: target.name || '',
      user_name: val('#f-user-name'),
      user_role: val('#f-user-role'),
      world: val('#f-world'),
      characters: draftChars
        .map((c) => ({ name: (c.name || '').trim(), role: (c.role || '').trim(),
                       state: (c.state || '').trim() }))
        .filter((c) => c.name || c.role),
      rules: note,   // 这次的要求就是这一项的写作要求
      opening: val('#f-opening'),
      lore: draftLore.map((e) => ({ keys: e.keys || '', content: e.content || '' })),
      mode: draftMode,
    }, (ev) => {
      if (ev.type === 'result') text = (ev.data && ev.data.text) || '';
      else if (ev.type === 'error') err = ev.message || '生成失败';
      else if (ev.type === 'thinking') setStatus('「' + (target.label || '') + '」思考中…');
      else if (ev.type === 'delta') setStatus('「' + (target.label || '') + '」生成中…');
    });
  } catch (e) {
    err = e.message;
  } finally {
    busyEnhance = false;
    if (btn) btn.disabled = false;
  }
  if (err) { setStatus('生成失败：' + err, 'error'); return; }
  const body = (text || '').trim();
  if (!body) { setStatus('这一项没生成出内容，再试一次', 'error'); return; }
  let finalText = body;
  if (target.field === 'character') {
    const named = body.match(/^名字[:：]\s*(\S{1,8})/);
    if (named) {
      const card = draftChars[target.index];
      if (card && !(card.name || '').trim()) {
        card.name = named[1];
        renderCharacterList();
      }
      finalText = body.split('\n').slice(1).join('\n').trim() || body;
    }
  }
  const before = target.read();
  target.write(finalText);
  markUndo(target.key, before, target.write, target.label);
  setStatus('「' + (target.label || '') + '」已重写，不满意可以点旁边的「撤销」', 'ok');
}

const MODAL_HOST = '#settings-modal .modal-box';

/** 设定表单只有一份，按场景搬进弹窗或右侧面板，省得维护两套。 */
function mountSettingsForm(where) {
  const form = $('#settings-form');
  if (!form) return;
  const host = where === 'pane' ? $('#story-pane') : $(MODAL_HOST);
  if (host && form.parentElement !== host) host.appendChild(form);
  form.classList.remove('hidden');   // 删过故事后表单被藏起来，换位置时得复原
}

/**
 * 角色表编辑器：AI 会在剧情中往里加人和改状态，但增删改的最终决定权在用户手上。
 * 打开弹窗时复制一份草稿，保存时才写回，取消就等于什么都不改。
 */
function renderCharacterList() {
  renderCharacterEditor($('#char-list'), draftChars, () => {
    renderCharacterList();
    setStatus('角色表已改动，记得点「保存」生效');
  });
  syncCharLabel();
  syncUndoButtons();
}

/** 标题写实际情况：两种模式下这些角色各自怎么用、名额还剩几个。 */
function syncCharLabel() {
  const el = $('#char-label');
  if (!el) return;
  const limit = (state.conf && state.conf.writing && state.conf.writing.max_characters) || 4;
  const used = draftChars.filter((c) => (c.name || '').trim()).length;
  const quota = used >= limit ? '已满 ' + used + '/' + limit : '已 ' + used + '/' + limit + '，还能加 ' + (limit - used) + ' 个';
  const how = draftMode === 'rp'
    ? '角色扮演：每轮只轮一位发言'
    : '一般模式：系统写全场，不轮流';
  el.textContent = 'AI 角色 · ' + how + ' · ' + quota;
}

export function closeSettings() {
  if (busySave) { toast('正在保存，请稍候', 'error'); return; }
  closeIdea();
  if (editingStory) {
    // 右侧面板里点「取消」= 丢掉这次改动，重新读一遍故事
    const id = editingStory;
    openSettings({ storyId: id });
    toast('已还原成保存时的内容', 'ok');
    return;
  }
  $('#settings-modal').classList.add('hidden');
  setTimeout(() => focusComposer({ force: true }), 0);
}

export async function saveSettings() {
  if ((!state.session && !editingStory) || busySave) return;
  busySave = true;
  const btn = $('#btn-save');
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '保存中…';

  const characters = draftChars
    .map((c) => ({
      name: (c.name || '').trim(),
      role: (c.role || '').trim(),
      state: (c.state || '').trim(),
      history: c.history || [],
    }))
    .filter((c) => c.name);

  // 以界面上高亮的按钮为准，避免状态没同步上就保存
  const activeMode = $('#f-mode button.active');
  const payload = {
    title: val('#f-story-title'),
    mode: activeMode ? activeMode.dataset.modeValue : draftMode,
    user_name: val('#f-user-name'),
    user_role: val('#f-user-role'),
    world: val('#f-world'),
    rules: val('#f-rules'),
    opening: val('#f-opening'),
    characters,
    lore: draftLore
      .map((e) => ({ keys: (e.keys || '').trim(), content: (e.content || '').trim() }))
      .filter((e) => e.content),
    // 角色表是唯一来源，第一个角色就是 AI 角色
    ai_name: characters.length ? characters[0].name : '',
    ai_role: characters.length ? characters[0].role : '',
  };

  try {
    if (editingStory) {
      // 故事本身：只写 stories/<id>.json，不碰任何一局对话
      await api('/api/stories/' + editingStory, { method: 'PUT', body: payload });
      await loadStories();
      toast('故事设定已保存', 'ok');
      return;
    }
    // 带上拿着的版本号：别的标签页这期间写过，就不能整份覆盖掉
    state.session = await api('/api/sessions/' + state.session.id, {
      method: 'PUT',
      body: { ...payload, updated_at: state.session && state.session.updated_at },
    });
    $('#settings-modal').classList.add('hidden');
    renderMessages();
    syncAll();
    await loadSessions();
    await loadStories();
    toast('设定已保存', 'ok');
    setTimeout(() => focusComposer({ force: true }), 0);
  } catch (e) {
    if (e.status === 409) {
      toast('这个对话在别处更新过，已重新读取，请确认后再存', 'error');
      await refreshCurrentSession();
      renderMessages();
      syncAll();
      return;
    }
    toast('保存失败：' + e.message, 'error');
  } finally {
    busySave = false;
    btn.disabled = false;
    btn.textContent = original || '保存';
  }
}

const STEP_LABEL = {
  user_name: '我的角色名',
  user_role: '我的角色设定',
  ai_name: 'AI 角色名',
  world: '世界观',
  opening: '开场',
};

/** 每次取最新的界面内容：后一项要用到前一项刚生成的结果。 */
function enhanceContext() {
  const first = (draftChars[0] || {});
  return {
    user_name: val('#f-user-name'),
    user_role: val('#f-user-role'),
    ai_name: (first.name || '').trim(),
    ai_role: (first.role || '').trim(),
    world: val('#f-world'),
    rules: val('#f-rules'),
    opening: val('#f-opening'),
    mode: draftMode,
    lore: draftLore.map((e) => ({ keys: e.keys || '', content: e.content || '' })),
    characters: draftChars
      .map((c) => ({ name: (c.name || '').trim(), role: (c.role || '').trim(),
                     state: (c.state || '').trim() }))
      .filter((c) => c.name || c.role),
  };
}

function stepLabel(step) {
  if (step.field === 'character') return '角色「' + ((draftChars[step.index] || {}).name || '未命名') + '」';
  return STEP_LABEL[step.field] || step.field;
}

/** 勾选状态：默认全选，没记录的项视为勾上。 */
let pickOn = null;

function pickKey(step) {
  return step.field === 'character' ? 'c' + step.index : step.field;
}

/** 全部可生成的项，不看勾选。 */
function allSteps() {
  const steps = [];
  if (!val('#f-user-name').trim()) steps.push({ field: 'user_name' });
  steps.push({ field: 'user_role' });

  let usable = draftChars
    .map((c, index) => ({ c, index }))
    .filter(({ c }) => (c.name || '').trim() || (c.role || '').trim());
  if (!usable.length) {
    // 一张卡都没有就留一张空卡给生成用；别每调一次多插一张
    if (!draftChars.length) draftChars.push({ name: '', role: '', state: '', history: [] });
    usable = [{ c: draftChars[0], index: 0 }];
  }
  for (const { index } of usable) steps.push({ field: 'character', index });
  steps.push({ field: 'world' });
  steps.push({ field: 'opening' });
  // 认人排最后：前面几项写完再认，能连刚生成的人一起认出来
  steps.push({ field: 'cast' });
  return steps;
}

/** 按勾选组装：勾了就生成、生成就覆盖，取消勾才保住原文。 */
function buildSteps() {
  const all = allSteps();
  if (!pickOn) return all;
  return all.filter((s) => pickOn[pickKey(s)] !== false);
}

/** 把勾选项画进弹窗：一项一行，默认全勾。 */
function renderPick() {
  const box = $('#idea-pick');
  if (!box) return;
  pickOn = pickOn || {};
  box.innerHTML = '';
  for (const step of allSteps()) {
    const key = pickKey(step);
    if (pickOn[key] === undefined) pickOn[key] = true;
    const lab = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.dataset.pick = key;
    cb.checked = pickOn[key];
    cb.onchange = () => { pickOn[key] = cb.checked; };
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(' ' + stepLabel(step)));
    box.appendChild(lab);
  }
}

/** 生成结果里带了名单就补进角色表：只补缺的，不改已有角色的人设。 */
function addCast(data) {
  const found = (data && data.cast) || [];
  let added = 0;
  for (const person of found) {
    const name = (person.name || '').trim();
    if (!name) continue;
    const have = draftChars.find((c) => (c.name || '').trim() === name);
    if (have) {
      if (!(have.role || '').trim() && person.role) have.role = person.role;
      continue;
    }
    draftChars.push({ name, role: person.role || '', state: '', history: [] });
    added += 1;
  }
  if (added) renderCharacterList();
  return added;
}

function applyStep(step, data) {
  // 认人只往表里补人：不删人，也不改已有角色的人设
  if (step.field === 'cast') return addCast(data);
  const body = ((data && data.text) || '').trim();
  if (!body) return;
  if (step.field === 'user_name') { $('#f-user-name').value = body; return; }
  if (step.field === 'user_role') { $('#f-user-role').value = body; return; }
  // 世界观和开场白写完顺带收人：正文里提到的人自动进表
  if (step.field === 'world') { $('#f-world').value = body; addCast(data); return; }
  if (step.field === 'opening') { $('#f-opening').value = body; addCast(data); return; }

  const card = draftChars[step.index];
  if (!card) return;
  if (step.field === 'ai_name') { card.name = body; } else if (step.field === 'character') {
    let rest = body;
    const named = body.match(/^名字[:：]\s*(\S{1,8})/);
    if (named && !(card.name || '').trim()) {
      card.name = named[1];
      rest = body.split('\n').slice(1).join('\n').trim() || body;
    }
    card.role = rest;
  }
  renderCharacterList();
}

async function runStep(step, label, order, total) {
  const ctx = enhanceContext();
  const payload = {
    ...ctx,
    field: step.field,
    name: (draftChars[step.index] || {}).name || '',
  };
  const startedAt = Date.now();
  let result = null;

  await streamSSE('/api/enhance', payload, (ev) => {
    const sec = ((Date.now() - startedAt) / 1000).toFixed(0);
    const head = '(' + order + '/' + total + ') ' + label + ' · ';
    if (ev.type === 'thinking') setStatus(head + '思考中… ' + sec + 's');
    else if (ev.type === 'delta') setStatus(head + '生成中… ' + sec + 's');
    else if (ev.type === 'retry') setStatus(head + '第 ' + ev.attempt + '/' + ev.max + ' 次重试：' + ev.reason);
    else if (ev.type === 'result') result = ev.data;
    else if (ev.type === 'error') throw new StreamEventError(ev.message);
  }, { idleTimeout: 180000 });

  if (!result) throw new Error('没有拿到内容');
  return result;
}

let failedSteps = [];

/** 只补上次没成功的项，不算第一次生成。 */
export function popIdea(forceFull = false) {
  if (forceFull) failedSteps = [];
  const pop = $('#idea-pop');
  if (!pop) return;
  renderPick();
  pop.classList.remove('hidden');
  const area = $('#f-rules');
  if (area) {
    area.value = val('#f-rules') || area.value || '';
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);
  }
}

export function closeIdea() {
  const pop = $('#idea-pop');
  if (pop) pop.classList.add('hidden');
}

/** 弹出层里点「开始生成」。 */
export async function startEnhance() {
  closeIdea();
  await enhance();
}

export async function enhance() {
  // 故事视图里没有"当前对话"，生成设定不该因为 state.session 为空就毫无反应
  if (busyEnhance) { setStatus('正在生成，等这一轮跑完再点', ''); return; }

  const ctx = enhanceContext();
  if (!ctx.user_role && !ctx.world && !ctx.characters.length && !val('#f-rules').trim()) {
    setStatus('至少写一点点初始想法，比如「我是实习生，AI 是我的上司，现代都市」。', 'error');
    return;
  }

  busyEnhance = true;
  const btn = $('#btn-enhance');
  btn.disabled = true;

  const all = buildSteps();
  // 上次失败的只补那几项，已经成功的不重跑
  const steps = failedSteps.length
    ? all.filter((s) => failedSteps.some((f) => f.field === s.field && f.index === s.index))
    : all;
  failedSteps = [];

  const done = [];
  const failed = [];
  for (let i = 0; i < steps.length; i += 1) {
    const step = steps[i];
    const label = stepLabel(step);
    try {
      const data = await runStep(step, label, i + 1, steps.length);
      applyStep(step, data);
      done.push(label);
    } catch (e) {
      failed.push({ step, label });
      setStatus('(' + (i + 1) + '/' + steps.length + ') ' + label + ' 没成功：' + (e.message || e), 'error');
    }
  }

  failedSteps = failed.map((f) => f.step);
  if (!failed.length) {
    setStatus('✅ 全部完成（共 ' + done.length + ' 项），可继续手动微调，然后点「保存」。', 'ok');
  } else {
    setStatus('完成 ' + done.length + ' 项，' + failed.length + ' 项没成功：'
      + failed.map((f) => f.label).join('、') + '。再点一次按钮只补这几项。', 'error');
  }

  busyEnhance = false;
  btn.disabled = false;
}

/** 保存设定，并用同一份设定新开一局。 */
export async function newRunFromSettings() {
  if ((!state.session && !editingStory) || busySave) return;
  const btn = $('#btn-new-run');
  if (btn) btn.disabled = true;
  try {
    const storyId = editingStory || (state.session && state.session.story_id);
    await saveSettings();
    const created = await newRun(storyId, draftMode);
    state.session = created;
    $('#settings-modal').classList.add('hidden');
    // 从故事视图点开新局：得真的跳到这一局，不能还停在故事设定面板上
    switchSideView('sessions');
    renderMessages();
    syncAll();
    await loadSessions();
    toast('已用这份设定开了新局', 'ok');
    setTimeout(() => focusComposer({ force: true }), 0);
  } catch (e) {
    toast('开新局失败：' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

export async function regenOpening() {
  if (!state.session || busyOpening) return;
  if ((state.session.messages || []).length > 0) {
    toast('当前对话已有内容，请先「清空」再重新生成开场', 'error');
    return;
  }

  busyOpening = true;
  const btn = $('#btn-regen-opening');
  const area = $('#f-opening');
  btn.disabled = true;
  area.value = '';
  setStatus('正在生成开场…');

  try {
    await streamSSE('/api/sessions/' + state.session.id + '/opening', {}, (ev) => {
      if (ev.type === 'delta') {
        area.value += ev.content || '';
        area.scrollTop = area.scrollHeight;
      } else if (ev.type === 'retry') {
        area.value = '';
        setStatus('第 ' + ev.attempt + '/' + ev.max + ' 次自动重试：' + ev.reason + '…');
      } else if (ev.type === 'error') {
        throw new StreamEventError(ev.message);
      }
    }, { idleTimeout: 240000 });

    setStatus('✅ 开场白已生成。【旁白】与【角色名】标记会在保存时自动拆成旁白和台词。', 'ok');
    await refreshCurrentSession();
    syncAll();
  } catch (e) {
    setStatus('❌ ' + e.message, 'error');
  } finally {
    busyOpening = false;
    btn.disabled = false;
  }
}
