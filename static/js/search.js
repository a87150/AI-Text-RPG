import { api } from './api.js';
import { state } from './state.js';
import { $, toast } from './ui.js';
import { openSession } from './sessions.js';

let timer = 0;

function close() {
  const modal = $('#search-modal');
  if (modal) modal.classList.add('hidden');
}

function hitRow(hit) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'search-hit';
  const head = document.createElement('div');
  head.className = 'search-hit-head';
  head.textContent = hit.title + ' · 第 ' + (hit.index + 1) + ' / ' + hit.total + ' 条'
    + (hit.role === 'user' ? ' · 你' : '');
  const body = document.createElement('div');
  body.className = 'search-hit-body';
  body.textContent = hit.snippet;
  row.append(head, body);
  row.onclick = async () => {
    close();
    if (!state.session || state.session.id !== hit.session_id) {
      await openSession(hit.session_id);
    }
    const node = document.querySelector('#messages [data-index="' + hit.index + '"]');
    if (!node) {
      toast('这条在更早的位置，已经被压缩掉了', 'error');
      return;
    }
    node.scrollIntoView({ block: 'center', behavior: 'smooth' });
    node.classList.add('search-flash');
    setTimeout(() => node.classList.remove('search-flash'), 1600);
  };
  return row;
}

async function runQuery(text) {
  const list = $('#search-hits');
  if (!list) return;
  const q = (text || '').trim();
  if (q.length < 2) {
    list.innerHTML = '<div class="hint">至少输入两个字。</div>';
    return;
  }
  try {
    const got = await api('/api/search?q=' + encodeURIComponent(q));
    const hits = got.hits || [];
    if (!hits.length) {
      list.innerHTML = '<div class="hint">没有命中。</div>';
      return;
    }
    list.innerHTML = '';
    for (const hit of hits) list.appendChild(hitRow(hit));
  } catch (e) {
    list.innerHTML = '<div class="hint">搜索失败：' + (e.message || e) + '</div>';
  }
}

export function openSearch() {
  const modal = $('#search-modal');
  const input = $('#search-input');
  if (!modal || !input) return;
  modal.classList.remove('hidden');
  input.value = '';
  const list = $('#search-hits');
  if (list) list.innerHTML = '<div class="hint">输入关键词，扫的是所有会话的全文。</div>';
  input.focus();
}

export function bindSearch() {
  const btn = $('#btn-search');
  if (btn) btn.onclick = openSearch;
  const closeBtn = $('#btn-search-close');
  if (closeBtn) closeBtn.onclick = close;
  const input = $('#search-input');
  if (input) {
    input.addEventListener('input', () => {
      clearTimeout(timer);
      const text = input.value;
      timer = setTimeout(() => runQuery(text), 250);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { clearTimeout(timer); runQuery(input.value); }
      if (e.key === 'Escape') close();
    });
  }
  const modal = $('#search-modal');
  if (modal) modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
}
