/** 故事封面：右上角小图 + 居中弹窗，上传 / 生图 / 撤销。 */
import { api } from './api.js';
import { state } from './state.js';
import { $, toast } from './ui.js';

let busy = false;

function storyId() {
  // 对话里没选中任何故事，封面属于这局用的那份设定
  if (state.session && state.session.story_id) return state.session.story_id;
  if (state.session && state.session.title) {
    const hit = (state.stories || []).find((x) => x.title === state.session.title);
    if (hit) return hit.id;
  }
  return state.selectedStory || '';
}

function currentStory() {
  return (state.stories || []).find((s) => s.id === storyId()) || null;
}

/** 对话中只读：改封面是改设定本身，不能在已开局里改 */
function readonlyHere() {
  return !!(state.session && state.session.id);
}

function urlOf(name) {
  return name ? '/covers/' + encodeURIComponent(name) : '';
}

/** 标题右边那个小框。故事列表里的微缩图也走这同一份数据。 */
export function syncCoverThumb() {
  const story = currentStory();
  const name = (story && story.cover) || '';
  const img = $('#cover-thumb');
  const empty = $('#cover-empty');
  const slot = $('#btn-cover');
  if (slot) {
    slot.disabled = !story || readonlyHere();
    slot.title = readonlyHere()
      ? '封面属于设定，去「故事」里改'
      : '封面图：上传或生成';
  }
  if (img) {
    if (name) {
      // 这里是 192x264 的预览位，小图（160px）撑不住，会糊。
      // 160px 只给列表里那个 48px 格子用。
      img.src = urlOf(name);
      img.classList.remove('hidden');
    } else {
      img.removeAttribute('src');
      img.classList.add('hidden');
    }
  }
  if (empty) empty.classList.toggle('hidden', !!name);
}

function setMsg(text, kind) {
  const el = $('#cover-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'cover-msg' + (kind ? ' ' + kind : '');
}

function syncStage(name) {
  const img = $('#cover-stage-img');
  const empty = $('#cover-stage-empty');
  if (img) {
    if (name) {
      img.src = urlOf(name) + '?t=' + Date.now();   // 同名文件换内容时不至于拿缓存
      img.classList.remove('hidden');
    } else {
      img.removeAttribute('src');
      img.classList.add('hidden');
    }
  }
  if (empty) empty.classList.toggle('hidden', !!name);
}

function syncUndo() {
  const story = currentStory();
  const left = (story && story.cover_history_count) || 0;
  const btn = $('#cover-undo');
  if (btn) {
    btn.disabled = busy || !left;
    btn.title = left ? '回到上一张（还有 ' + left + ' 张）' : '没有可回退的图';
  }
}

/** 封面变了之后，列表和标题框都要跟着变。 */
function afterChange(payload) {
  const story = currentStory();
  if (story) {
    story.cover = payload.cover || '';
    if (typeof payload.cover_prompt === 'string') story.cover_prompt = payload.cover_prompt;
    if (typeof payload.left === 'number') story.cover_history_count = payload.left;
  }
  syncStage(payload.cover || '');
  syncCoverThumb();
  if (managing) loadGrid();
  document.dispatchEvent(new CustomEvent('rp:stories-changed'));
}

export function openCover() {
  const story = currentStory();
  if (!story) {
    toast('先选一份故事设定', 'error');
    return;
  }
  if (readonlyHere()) {
    toast('封面属于设定本身，去「故事」里换', 'error');
    return;
  }
  const prompt = story.cover_prompt || '';
  const input = $('#cover-prompt');
  if (input) input.value = prompt;
  syncStage(story.cover || '');
  syncUndo();
  setMsg('');
  $('#cover-modal').classList.remove('hidden');
  // 上次没生完就把窗关了：进来就接着看进度
  (async () => {
    try {
      const data = await api('/api/stories/' + story.id + '/cover/job');
      if (data.job && data.job.state === 'running') {
        busy = true;
        showGenerating(data.job.started);
        watchJob();
      } else if (data.job && data.job.state === 'done') {
        syncUndo();
      }
    } catch (e) { /* 查不到就当没任务 */ }
  })();
  $('#cover-prompt').focus();
}

export function closeCover() {
  const m = $('#cover-modal');
  if (m) m.classList.add('hidden');
  // 关窗不影响后台生成，但没必要继续打接口
  stopPoll();
  stopTicking();
  closeManage();
  busy = false;
}

async function upload(file) {
  if (!file) return;
  if (file.size > 12 * 1024 * 1024) {
    setMsg('这张太大（超过 12MB）', 'error');
    return;
  }
  const fd = new FormData();
  fd.append('file', file);
  busy = true;
  setMsg('正在存…');
  try {
    // 写接口只收 JSON；上传是唯一的例外，靠这个自定义头过守卫
    const resp = await fetch('/api/stories/' + storyId() + '/cover', {
      method: 'POST',
      headers: { 'X-Cover-Upload': '1' },
      body: fd,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
    afterChange(data);
    setMsg('已换上', 'ok');
  } catch (e) {
    setMsg('存不下：' + (e.message || e), 'error');
  } finally {
    busy = false;
    syncUndo();
  }
}

let pollTimer = 0;

function stopPoll() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = 0;
  }
}

/** 生成中：把舞台换成占位，按钮全禁掉，旁边跑秒数。 */
function showGenerating(startedAt) {
  const img = $('#cover-stage-img');
  const empty = $('#cover-stage-empty');
  if (img) {
    img.removeAttribute('src');
    img.classList.add('hidden');
  }
  if (empty) {
    empty.classList.remove('hidden');
    empty.textContent = '生成中…';
  }
  for (const id of ['#cover-gen', '#cover-pick', '#cover-undo', '#cover-manage']) {
    const b = $(id);
    if (b) b.disabled = true;
  }
  const t0 = startedAt || Date.now() / 1000;
  clearInterval(tickTimer);
  tickTimer = setInterval(() => {
    const secs = Math.max(0, Math.round(Date.now() / 1000 - t0));
    setMsg('生成中，已经 ' + secs + ' 秒…');
  }, 1000);
}

function stopTicking() {
  if (tickTimer) {
    clearInterval(tickTimer);
    tickTimer = 0;
  }
  for (const id of ['#cover-gen', '#cover-pick', '#cover-manage']) {
    const b = $(id);
    if (b) b.disabled = false;
  }
}

let tickTimer = 0;

/** 轮询进度。跑完就自己收工，把图换上。 */
function watchJob() {
  const sid = storyId();
  stopPoll();
  pollTimer = setTimeout(async () => {
    if (storyId() !== sid) { stopTicking(); stopPoll(); return; }
    let data;
    try {
      data = await api('/api/stories/' + sid + '/cover/job');
    } catch (e) {
      stopTicking();
      setMsg('查不到进度：' + (e.message || e), 'error');
      return;
    }
    const job = data.job;
    if (!job) {
      stopTicking();
      setMsg('任务没了，可能服务重启过', 'error');
      return;
    }
    if (job.state === 'running') {
      const secs = data.elapsed !== undefined ? data.elapsed : Math.round(Date.now() / 1000 - job.started);
      setMsg('生成中，已经 ' + Math.round(secs) + ' 秒…');
      pollTimer = setTimeout(watchJob, 2000);
      return;
    }
    // 任务结束了，把生成中的锁解掉，否则撤销按钮一直是灰的
    busy = false;
    stopTicking();
    if (job.state === 'error') {
      syncStage((currentStory() && currentStory().cover) || '');
      setMsg(job.error || '生成失败', 'error');
      syncUndo();
      return;
    }
    afterChange({ cover: job.cover, cover_prompt: job.prompt, left: job.history_count });
    setMsg('生成好了，不满意可以撤销重来', 'ok');
    syncUndo();
  }, 1200);
}

async function generate() {
  if (busy) return;
  const prompt = ($('#cover-prompt') && $('#cover-prompt').value.trim()) || '';
  busy = true;
  showGenerating();
  try {
    const data = await api('/api/stories/' + storyId() + '/cover/gen', {
      method: 'POST',
      body: prompt ? { prompt } : {},
    });
    watchJob();
    if (data.job && data.job.state === 'running') {
      setMsg('提交了，正在生成…');
    }
  } catch (e) {
    busy = false;
    stopTicking();
    syncStage((currentStory() && currentStory().cover) || '');
    setMsg(e.message || '生成失败', 'error');
  }
}

async function undo() {
  if (busy) return;
  busy = true;
  setMsg('正在退回…');
  try {
    const data = await api('/api/stories/' + storyId() + '/cover/undo', { method: 'POST', body: {} });
    afterChange(data);
    setMsg(data.cover ? '已退回上一张' : '已经退到没有封面', 'ok');
  } catch (e) {
    setMsg(e.message || '撤不回', 'error');
  } finally {
    busy = false;
    syncUndo();
  }
}

export function bindCover() {
  const open = $('#btn-cover');
  if (open) open.onclick = openCover;
  const close = $('#cover-close');
  if (close) close.onclick = closeCover;
  const modal = $('#cover-modal');
  if (modal) {
    modal.addEventListener('click', (e) => {
      if (e.target.id === 'cover-modal') closeCover();
    });
  }
  const pick = $('#cover-pick');
  const file = $('#cover-file');
  if (pick && file) {
    pick.onclick = () => file.click();
    file.onchange = () => {
      const f = file.files && file.files[0];
      file.value = '';           // 同一张图连选两次也要触发
      upload(f);
    };
  }
  const gen = $('#cover-gen');
  if (gen) gen.onclick = generate;
  const back = $('#cover-undo');
  if (back) back.onclick = undo;
  const mg = $('#cover-manage');
  if (mg) mg.onclick = () => openManage();
}

let managing = false;

function renderGrid(items) {
  const box = $('#cover-grid');
  if (!box) return;
  box.innerHTML = '';
  if (!items || !items.length) {
    const empty = document.createElement('div');
    empty.className = 'cover-grid-empty';
    empty.textContent = '还没有图。上面生成一张，或者从本地选一张。';
    box.appendChild(empty);
    return;
  }
  for (const it of items) {
    const cell = document.createElement('div');
    cell.className = 'cover-cell' + (it.current ? ' active' : '');

    const img = document.createElement('img');
    img.className = 'cover-cell-img';
    img.alt = '';
    img.loading = 'lazy';
    img.src = it.thumb || it.url;
    img.onerror = () => {
      if (it.url && img.src.indexOf('/thumbs/') !== -1) {
        img.onerror = null;
        img.src = it.url;
      }
    };
    img.title = it.prompt || it.name;
    if (!it.current) img.onclick = () => pick(it.name);
    cell.appendChild(img);

    if (it.current) {
      const mark = document.createElement('span');
      mark.className = 'cover-cell-mark';
      mark.textContent = '正在用';
      cell.appendChild(mark);
    } else {
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'cover-cell-del';
      del.textContent = '×';
      del.title = '彻底删掉这张';
      // 两段式：第一次点变成“确认删”，三秒内再点才真删。比弹系统确认框快，
      // 也不会把鼠标从格子里拉走。
      let armed = 0;
      del.onclick = (e) => {
        e.stopPropagation();
        if (!armed) {
          armed = setTimeout(() => {
            armed = 0;
            del.classList.remove('armed');
            del.textContent = '×';
          }, 3000);
          del.classList.add('armed');
          del.textContent = '确认删？';
          return;
        }
        clearTimeout(armed);
        armed = 0;
        del.classList.remove('armed');
        del.textContent = '×';
        drop(it.name);
      };
      cell.appendChild(del);
    }
    box.appendChild(cell);
  }
}

async function loadGrid() {
  try {
    const data = await api('/api/stories/' + storyId() + '/cover/list');
    renderGrid(data.items || []);
  } catch (e) {
    const box = $('#cover-grid');
    if (box) box.innerHTML = '<div class="cover-grid-empty">读不到图列表</div>';
  }
}

function openManage() {
  const panel = $('#cover-manage-panel');
  if (!panel) return;
  managing = panel.classList.contains('hidden');
  panel.classList.toggle('hidden', !managing);
  if (managing) loadGrid();
}

function closeManage() {
  const panel = $('#cover-manage-panel');
  if (panel) panel.classList.add('hidden');
  managing = false;
}

async function pick(name) {
  if (busy) return;
  busy = true;
  setMsg('正在换…');
  try {
    const data = await api('/api/stories/' + storyId() + '/cover/pick', {
      method: 'POST', body: { name },
    });
    afterChange(data);
    renderGrid(data.items || []);
    setMsg('已换成这张', 'ok');
  } catch (e) {
    setMsg(e.message || '换不了', 'error');
  } finally {
    busy = false;
    syncUndo();
  }
}

async function drop(name) {
  if (busy) return;
  busy = true;
  setMsg('正在删…');
  try {
    const data = await api('/api/stories/' + storyId() + '/cover/drop', {
      method: 'POST', body: { name },
    });
    afterChange(data);
    renderGrid(data.items || []);
    setMsg('已删掉', 'ok');
  } catch (e) {
    setMsg(e.message || '删不掉', 'error');
  } finally {
    busy = false;
    syncUndo();
  }
}
