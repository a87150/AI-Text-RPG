/** 侧栏故事列表与故事级操作（打开 / 删除 / 清空 / 压缩 / 同步）。 */
import { api } from './api.js';
import { state } from './state.js';
import { $, toast, renderMessages, syncAll, focusComposer, syncHeader, closeNav, loadDraft } from './ui.js';
// 循环引用（settings 也用 sessions 的函数）：函数声明会被提升，运行期调用没问题
import { openSettings } from './settings.js';
import { syncCoverThumb } from './cover.js';

export async function loadSessions() {
  try {
    state.sessions = await api('/api/sessions');
    renderSessionList();
    if (state.sideView === 'stories') renderStoryList();
  } catch (e) {
    renderSessionList();
    toast('读取对话列表失败：' + e.message, 'error');
  }
}

const PAGE_SIZE = 10;

/** 一页装几条要看列表有多高，固定条数会在还有大片空白时就翻页。 */
function pageSize(box, itemHeight = 66) {
  const h = box ? box.clientHeight : 0;
  const size = h ? Math.max(4, Math.floor(h / itemHeight)) : PAGE_SIZE;
  state.pageSize = size;
  return size;
}

/** 条目多了一页页翻，别把侧栏塞成一条长卷。 */
function renderPager(sel, total, page, onGo, size) {
  const box = $(sel);
  if (!box) return;
  const per = size || state.pageSize || PAGE_SIZE;
  const pages = Math.ceil(total / per);
  box.innerHTML = '';
  box.classList.toggle('hidden', pages <= 1);
  if (pages <= 1) return;
  const mk = (label, target, disabled, title) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.title = title;
    b.disabled = disabled;
    b.onclick = () => onGo(target);
    return b;
  };
  box.append(
    mk('‹', page - 1, page <= 0, '上一页'),
    Object.assign(document.createElement('span'), { textContent: (page + 1) + ' / ' + pages }),
    mk('›', page + 1, page >= pages - 1, '下一页'),
  );
}

export function renderSessionList() {
  const box = $('#session-list');
  if (!box) return;
  box.innerHTML = '';

  if (!state.sessions.length) {
    const empty = document.createElement('div');
    empty.className = 'session-empty';
    empty.textContent = '还没有对话，点上面开一局。';
    box.appendChild(empty);
    renderPager('#session-pager', 0, 0, () => {});
    return;
  }

  const size = pageSize(box);
  const pages = Math.max(1, Math.ceil(state.sessions.length / size));
  if (state.sessionPage >= pages) state.sessionPage = pages - 1;
  const visible = state.sessions.slice(state.sessionPage * size, (state.sessionPage + 1) * size);
  renderPager('#session-pager', state.sessions.length, state.sessionPage, (page) => {
    state.sessionPage = page;
    renderSessionList();
  });

  for (const s of visible) {
    const item = document.createElement('div');
    item.className = 'session-item'
      + (state.session && state.session.id === s.id ? ' active' : '');

    const body = document.createElement('div');
    body.className = 'si-body';
    const cover = coverOfStory(s.story_id);
    if (cover) body.appendChild(miniCover(cover));
    const title = document.createElement('div');
    title.className = 'si-title';
    title.textContent = s.title || '未命名故事';
    const meta = document.createElement('div');
    meta.className = 'si-meta';
    // 两种模式都显示角色数：一般模式也有配角，只是不用角色扮演的说话格式
    meta.textContent = (s.mode === 'rp' ? '扮演' : '一般')
      + ' · ' + ((s.char_count || 0) + ' 个角色')
      + ' · ' + ((s.message_count || 0) + ' 条')
      + (s.has_summary ? ' · 已压缩' : '');
    const text = document.createElement('div');
    text.className = 'si-text';
    text.append(title, meta);
    body.append(text);

    const del = document.createElement('button');
    del.className = 'si-del';
    del.type = 'button';
    del.textContent = '×';
    del.title = '删除这个故事';
    del.onclick = (e) => {
      e.stopPropagation();
      removeSession(s);
    };

    item.append(body, del);
    item.onclick = () => openSession(s.id);
    box.appendChild(item);
  }
}

/** 故事视图：一份设定 + 它开过的所有对话。 */
export async function loadStories() {
  try {
    state.stories = await api('/api/stories');
  } catch (e) {
    state.stories = [];
    toast('读取故事列表失败：' + e.message, 'error');
  }
  renderStoryList();
}

function runsOf(storyId) {
  return (state.sessions || []).filter((s) => s.story_id === storyId);
}

const PICK_PER_PAGE = 8;

/** 对话视图的「＋」：先挑一份故事当模板，再开一局。 */
export async function openPickStory() {
  if (state.busy) { toast('正在生成回复，稍后再开新对话', 'error'); return; }
  await loadStories();
  state.pickPage = 0;
  renderPickList();
  $('#pick-blank').onclick = () => startRun('');
  $('#pick-story-modal').classList.remove('hidden');
}

function renderPickList() {
  const box = $('#pick-list');
  if (!box) return;
  box.innerHTML = '';
  const stories = state.stories || [];
  const pages = Math.max(1, Math.ceil(stories.length / PICK_PER_PAGE));
  if (state.pickPage >= pages) state.pickPage = pages - 1;

  if (!stories.length) {
    const empty = document.createElement('div');
    empty.className = 'pick-empty';
    empty.textContent = '还没有故事设定。先去「故事」里建一份，或者从空白开始。';
    box.appendChild(empty);
  }

  const start = state.pickPage * PICK_PER_PAGE;
  for (const story of stories.slice(start, start + PICK_PER_PAGE)) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'pick-item';
    const body = document.createElement('div');
    body.className = 'pi-body';
    const title = document.createElement('b');
    title.textContent = story.title || '未命名故事';
    title.title = title.textContent;
    const meta = document.createElement('span');
    meta.textContent = story.character_count + ' 个角色 · 开过 ' + (story.runs || 0) + ' 局';
    body.append(title, meta);
    const go = document.createElement('i');
    go.className = 'pi-go';
    go.textContent = '›';
    item.append(body, go);
    item.onclick = () => startRun(story.id);
    box.appendChild(item);
  }

  renderPager('#pick-pager', stories.length, state.pickPage, (page) => {
    state.pickPage = page;
    renderPickList();
  }, PICK_PER_PAGE);
}

export function closePickStory() {
  const el = $('#pick-story-modal');
  if (el) el.classList.add('hidden');
}

async function startRun(storyId) {
  try {
    const created = await newRun(storyId || '');
    state.session = created;
    state.aiStatus = { text: '', kind: '' };
    closePickStory();
    switchSideView('sessions');
    renderMessages();
    syncAll();
    await loadSessions();
    await loadStories();
    setTimeout(() => focusComposer({ force: true }), 0);
  } catch (e) {
    toast('开新对话失败：' + e.message, 'error');
  }
}

/** 故事列表里的「＋」：新建一份空设定，随后直接进设定弹窗填。 */
export async function createStory() {
  const story = await api('/api/stories', { method: 'POST', body: {} });
  await loadStories();
  return story;
}

async function removeStory(story) {
  const runs = runsOf(story.id).length;
  const hint = runs
    ? '它已经开过 ' + runs + ' 局对话，删掉只影响设定，对话本身会留着。'
    : '这份设定会被删掉。';
  if (!confirm('确定删除故事《' + (story.title || '未命名') + '》？' + hint)) return;
  try {
    await api('/api/stories/' + story.id, { method: 'DELETE' });
    await loadStories();
    if (state.selectedStory === story.id) {
      // 删掉的正是右侧正在编辑的那份，面板得退回空态，不能继续显示已删内容
      state.selectedStory = '';
      const empty = $('#pane-empty');
      if (empty) empty.classList.remove('hidden');
      const form = $('#settings-form');
      if (form && form.parentElement && form.parentElement.id === 'story-pane') {
        form.classList.add('hidden');
      }
    }
    renderStoryList();
    toast('故事已删除', 'ok');
  } catch (e) {
    toast('删除失败：' + e.message, 'error');
  }
}


/** 列表里的小图。没封面的话调用方不会调它。 */
function miniCover(name) {
  const img = document.createElement('img');
  img.className = 'si-cover';
  img.src = '/covers/' + encodeURIComponent(name);
  img.alt = '';
  img.loading = 'lazy';
  return img;
}

/** 一局对话没自己的封面，用它所属故事的。 */
function coverOfStory(storyId) {
  if (!storyId) return '';
  const story = (state.stories || []).find((x) => x.id === storyId);
  return (story && story.cover) || '';
}

export function renderStoryList() {
  const box = $('#story-list');
  if (!box) return;
  box.innerHTML = '';

  const stories = state.stories || [];
  if (!stories.length) {
    const empty = document.createElement('div');
    empty.className = 'session-empty';
    empty.textContent = '还没有故事设定，点上面新建一份。';
    box.appendChild(empty);
    renderPager('#story-pager', 0, 0, () => {});
    return;
  }

  const size = pageSize(box);
  const pages = Math.max(1, Math.ceil(stories.length / size));
  if (state.storyPage >= pages) state.storyPage = pages - 1;
  renderPager('#story-pager', stories.length, state.storyPage, (page) => {
    state.storyPage = page;
    renderStoryList();
  });
  const visible = stories.slice(state.storyPage * size, (state.storyPage + 1) * size);

  for (const story of visible) {
    const runs = runsOf(story.id);
    const item = document.createElement('div');
    item.className = 'story-item'
      + (state.selectedStory === story.id ? ' active' : '');

    const head = document.createElement('div');
    head.className = 'si-body';
    // 有封面就在标题前面放张小图；没有就不占位，行高不会参差
    if (story.cover) head.appendChild(miniCover(story.cover));
    const title = document.createElement('div');
    title.className = 'si-title';
    title.textContent = story.title || '未命名故事';
    const meta = document.createElement('div');
    meta.className = 'si-meta';
    meta.textContent = runs.length + ' 局 · ' + story.character_count + ' 个角色';
    const text = document.createElement('div');
    text.className = 'si-text';
    text.append(title, meta);
    head.append(text);

    item.onclick = () => selectStory(story.id);

    const del = document.createElement('button');
    del.className = 'si-del';
    del.type = 'button';
    del.textContent = '×';
    del.title = '删除这份故事设定';
    del.onclick = (e) => {
      e.stopPropagation();
      removeStory(story);
    };

    item.append(head, del);
    box.appendChild(item);
  }
}

const VIEW_KEY = 'lt.view';
const STORY_KEY = 'lt.story';
const SESSION_KEY = 'lt.session';

function remember(key, value) {
  try { localStorage.setItem(key, value || ''); } catch (e) { /* 隐私模式下忽略 */ }
}

function recall(key) {
  try { return localStorage.getItem(key) || ''; } catch (e) { return ''; }
}

/** 刷新后要回到原来那一页：视图、选中的故事、打开的对话都记着。 */
export function savedView() {
  return recall(VIEW_KEY) === 'sessions' ? 'sessions' : 'stories';
}

export function savedStory() {
  return recall(STORY_KEY);
}

export function savedSession() {
  return recall(SESSION_KEY);
}

export function switchSideView(view) {
  state.sideView = view === 'stories' ? 'stories' : 'sessions';
  remember(VIEW_KEY, state.sideView);
  const storyView = state.sideView === 'stories';
  const storyBlock = $('#story-block');
  const sessionBlock = $('#session-block');
  if (storyBlock) storyBlock.classList.toggle('hidden', !storyView);
  if (sessionBlock) sessionBlock.classList.toggle('hidden', storyView);
  const seg = $('#side-seg');
  if (seg) {
    for (const btn of seg.querySelectorAll('button')) {
      btn.classList.toggle('active', btn.dataset.view === state.sideView);
    }
  }
  const pane = $('#story-pane');
  if (pane) pane.classList.toggle('hidden', !storyView);
  document.body.classList.toggle('story-view', storyView);
  if (storyView) {
    loadStories();
    // 表单可能还挂在弹窗里（上一轮在对话视图打开过设定），切回来要搬进面板
    if (state.selectedStory) openSettings({ storyId: state.selectedStory });
  }
  syncHeader();
}

/** 点故事 = 在右侧打开它的设定。 */
export async function selectStory(storyId) {
  state.selectedStory = storyId;
  remember(STORY_KEY, storyId);
  renderStoryList();
  switchSideView('stories');
  closeNav();
  const empty = $('#pane-empty');
  if (empty) empty.classList.add('hidden');
  // 表单的搬移由 switchSideView 负责，这里不再重复加载
  await openSettings({ storyId });
  syncCoverThumb();
}

export async function openSession(id) {
  if (state.busy) { toast('正在生成回复，稍后再切换故事', 'error'); return; }
  try {
    state.session = await api('/api/sessions/' + id);
    state.aiStatus = { text: '', kind: '' };
    remember(SESSION_KEY, state.session.id);
    $('#settings-modal').classList.add('hidden');
    closeNav();
    renderSessionList();
    if (state.sideView === 'stories') renderStoryList();
    $('#input').value = loadDraft(state.session.id);
    renderMessages();
    syncAll();
    focusComposer({ force: true });
  } catch (e) {
    toast('打开失败：' + e.message, 'error');
  }
}

export async function createSession() {
  return api('/api/sessions', { method: 'POST', body: {} });
}

/** 用同一个故事的设定再开一局：设定照抄，对话从零开始。 */
export async function newRun(storyId, mode) {
  const body = { story_id: storyId };
  if (mode) body.mode = mode;
  const created = await api('/api/sessions', { method: 'POST', body });
  remember(SESSION_KEY, created.id);   // 刷新后要回到这一局
  return created;
}

async function removeSession(s) {
  if (state.busy) { toast('正在生成回复，稍后再删除', 'error'); return; }
  if (!confirm('确定删除《' + (s.title || '未命名故事') + '》？不可恢复。')) return;
  try {
    await api('/api/sessions/' + s.id, { method: 'DELETE' });
    if (state.session && state.session.id === s.id) {
      state.session = null;
      remember(SESSION_KEY, '');
      renderMessages();
    }
    syncAll();
    await loadSessions();
    focusComposer();
  } catch (e) {
    toast('删除失败：' + e.message, 'error');
  }
}

export async function clearSession() {
  if (!state.session) return;
  if (state.busy) { toast('正在生成回复，稍候再清空', 'error'); return; }
  if (!confirm('清空当前对话？剧情记录会被删除（设定保留）。')) return;
  try {
    await api('/api/sessions/' + state.session.id + '/clear', { method: 'POST' });
    await refreshCurrentSession();
    renderMessages();
    syncAll();
    toast('已清空', 'ok');
    focusComposer();
  } catch (e) {
    toast('清空失败：' + e.message, 'error');
  }
}

export async function compressSession(btn) {
  if (!state.session) return;
  if (state.busy) return;
  if (btn) btn.disabled = true;
  toast('正在压缩上下文…');
  try {
    const r = await api('/api/sessions/' + state.session.id + '/compress', { method: 'POST' });
    await refreshCurrentSession();
    renderMessages();
    syncAll();
    toast(r.ok ? '已压缩，早期剧情折叠为前情提要' : '消息还不多，暂时不用压缩', 'ok');
  } catch (e) {
    toast('压缩失败：' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** 静默拉取当前故事最新状态（标题、上下文、消息、前情提要）。 */
export async function refreshCurrentSession() {
  if (!state.session) return null;
  try {
    const fresh = await api('/api/sessions/' + state.session.id);
    state.session = fresh;
    return fresh;
  } catch (e) {
    return null;
  }
}
