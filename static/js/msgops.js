/** 消息级操作：重写、就地编辑、从这里分叉。 */
import { api } from './api.js';
import { state } from './state.js';
import { $, toast, renderMessages, syncAll } from './ui.js';
import { loadSessions, refreshCurrentSession } from './sessions.js';
import { retryLast } from './chat.js';

function messagesOf() {
  return (state.session && state.session.messages) || [];
}

/** 找一个位置之前最近的那条用户发言。 */
function userTextBefore(index) {
  const msgs = messagesOf();
  for (let i = index - 1; i >= 0; i -= 1) {
    if (msgs[i].role === 'user') return msgs[i].content || '';
  }
  return '';
}

async function rewrite(index) {
  if (!state.session) return;
  if (state.busy) { toast('正在生成，稍后再重写', 'error'); return; }
  const msg = messagesOf()[index];
  if (!msg) return;
  const text = msg.role === 'user' ? (msg.content || '') : userTextBefore(index);
  if (!text) { toast('这条前面没有可以重发的用户消息', 'error'); return; }
  try {
    await api('/api/sessions/' + state.session.id + '/rewind', {
      method: 'POST', body: { index },
    });
    await refreshCurrentSession();
    renderMessages();
    await retryLast(text);
  } catch (e) {
    toast('重写失败：' + e.message, 'error');
  }
}

async function branch(index) {
  if (!state.session) return;
  if (state.busy) { toast('正在生成，稍后再分叉', 'error'); return; }
  try {
    const created = await api('/api/sessions/' + state.session.id + '/branch', {
      method: 'POST', body: { index },
    });
    state.session = created;
    renderMessages();
    syncAll();
    await loadSessions();
    toast('已从这里分出一条新的故事线', 'ok');
  } catch (e) {
    toast('分叉失败：' + e.message, 'error');
  }
}

function startEdit(index, host) {
  const msg = messagesOf()[index];
  if (!msg) return;
  const bubble = host.querySelector('.bubble') || host.querySelector('.narr-body');
  if (!bubble || host.querySelector('.edit-area')) return;
  const original = msg.content || msg.narrator || '';

  const area = document.createElement('textarea');
  area.className = 'edit-area';
  area.value = original;

  const bar = document.createElement('div');
  bar.className = 'edit-bar';
  const save = document.createElement('button');
  save.type = 'button';
  save.textContent = '保存';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.textContent = '取消';

  const restore = () => { renderMessages(); };
  cancel.onclick = restore;
  save.onclick = async () => {
    const text = (area.value || '').trim();
    if (!text) { toast('不能存成空内容', 'error'); return; }
    save.disabled = true;
    try {
      await api('/api/sessions/' + state.session.id + '/message', {
        method: 'PUT', body: { index, content: text },
      });
      await refreshCurrentSession();
      renderMessages();
      toast('已保存这条消息', 'ok');
    } catch (e) {
      save.disabled = false;
      toast('保存失败：' + e.message, 'error');
    }
  };

  bar.append(save, cancel);
  bubble.innerHTML = '';
  bubble.append(area, bar);
  area.focus();
  area.setSelectionRange(area.value.length, area.value.length);
}

export function bindMessageActions() {
  const box = $('#messages');
  if (!box) return;
  box.addEventListener('click', (e) => {
    const btn = e.target.closest('.msg-tools button[data-act]');
    if (!btn) return;
    const host = btn.closest('[data-index]');
    if (!host) return;
    const index = Number(host.dataset.index);
    if (Number.isNaN(index)) return;
    const act = btn.dataset.act;
    if (act === 'rewrite') rewrite(index);
    else if (act === 'branch') branch(index);
    else if (act === 'edit') startEdit(index, host);
  });
}
