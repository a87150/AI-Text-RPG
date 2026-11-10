/** 消息级操作：重写、就地编辑、从这里分叉。 */
import { api } from './api.js';
import { state } from './state.js';
import { continueLast } from './chat.js';
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

/** 这条后面还有别人的话吗？没有就不用问，重生成也无从谈起。 */
function hasAfter(index) {
  return messagesOf().length > index + 1;
}

/** 改了内容之后问一句：后面的回复是照着旧话写的，要不要重来。 */
function askRegenerate(index) {
  const host = document.querySelector('#messages [data-index="' + index + '"]');
  if (!host || host.querySelector('.edit-ask')) return;
  const bar = document.createElement('div');
  bar.className = 'edit-ask';
  const tip = document.createElement('span');
  tip.textContent = '已保存。后面的回复还是按原话写的。';
  const yes = document.createElement('button');
  yes.type = 'button';
  yes.className = 'ask-yes';
  yes.textContent = '重新生成';
  const no = document.createElement('button');
  no.type = 'button';
  no.textContent = '就这样';
  yes.onclick = () => {
    bar.remove();
    rewrite(index);
  };
  no.onclick = () => bar.remove();
  bar.append(tip, yes, no);
  host.appendChild(bar);
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
      if (text !== original && hasAfter(index)) askRegenerate(index);
      else toast('已保存这条消息', 'ok');
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
  else if (act === 'continue') continueLast();
  });
}
