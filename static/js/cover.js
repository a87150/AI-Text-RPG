/** 故事封面：右上角小图 + 居中弹窗，上传 / 生图 / 撤销。 */
import { api } from './api.js';
import { state } from './state.js';
import { $, toast } from './ui.js';

let busy = false;

function storyId() {
  return state.selectedStory || '';
}

function currentStory() {
  return (state.stories || []).find((s) => s.id === storyId()) || null;
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
  if (slot) slot.disabled = !story;
  if (img) {
    if (name) {
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
  document.dispatchEvent(new CustomEvent('rp:stories-changed'));
}

export function openCover() {
  const story = currentStory();
  if (!story) {
    toast('先选一份故事设定', 'error');
    return;
  }
  const prompt = story.cover_prompt || '';
  const input = $('#cover-prompt');
  if (input) input.value = prompt;
  syncStage(story.cover || '');
  syncUndo();
  setMsg('');
  $('#cover-modal').classList.remove('hidden');
  $('#cover-prompt').focus();
}

export function closeCover() {
  const m = $('#cover-modal');
  if (m) m.classList.add('hidden');
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

async function generate() {
  if (busy) return;
  const prompt = ($('#cover-prompt') && $('#cover-prompt').value.trim()) || '';
  busy = true;
  const btn = $('#cover-gen');
  if (btn) btn.disabled = true;
  setMsg('正在生成，可能要几十秒…');
  try {
    const data = await api('/api/stories/' + storyId() + '/cover/gen', {
      method: 'POST',
      body: prompt ? { prompt } : {},
    });
    afterChange(data);
    setMsg('生成好了，不满意可以撤销重来', 'ok');
  } catch (e) {
    setMsg(e.message || '生成失败', 'error');
  } finally {
    busy = false;
    if (btn) btn.disabled = false;
    syncUndo();
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
  const rm = $('#cover-remove');
  if (rm) {
    rm.onclick = async () => {
      if (busy) return;
      busy = true;
      try {
        const data = await api('/api/stories/' + storyId() + '/cover/undo', { method: 'POST', body: {} });
        afterChange(data);
        setMsg('封面已去掉', 'ok');
      } catch (e) {
        setMsg(e.message || '去不掉', 'error');
      } finally {
        busy = false;
        syncUndo();
      }
    };
  }
}
