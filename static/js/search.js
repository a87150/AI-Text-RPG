import { api } from './api.js';
import { state } from './state.js';
import { $, toast } from './ui.js';
import { openSession } from './sessions.js';

let timer = 0;
let rows = [];
let active = -1;
let seq = 0;          // 每次查询递增：慢的那次回来时不许覆盖新的结果

function close() {
  const modal = $('#search-modal');
  if (modal) modal.classList.add('hidden');
}

/** 把命中词包成 <mark>，大小写不敏感；其余部分照原样成文本节点。 */
function paint(text, q) {
  const frag = document.createDocumentFragment();
  const t = text || '';
  const needle = (q || '').toLowerCase();
  if (!needle) {
    frag.appendChild(document.createTextNode(t));
    return frag;
  }
  const lower = t.toLowerCase();
  let i = 0;
  while (i < t.length) {
    const at = lower.indexOf(needle, i);
    if (at < 0) {
      frag.appendChild(document.createTextNode(t.slice(i)));
      break;
    }
    if (at > i) frag.appendChild(document.createTextNode(t.slice(i, at)));
    const mark = document.createElement('mark');
    mark.textContent = t.slice(at, at + needle.length);
    frag.appendChild(mark);
    i = at + needle.length;
  }
  return frag;
}

async function jump(hit) {
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
}

function hitRow(hit, q) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'search-hit';
  const head = document.createElement('div');
  head.className = 'search-hit-head';
  const title = document.createElement('span');
  title.className = 'search-hit-title';
  title.textContent = hit.title || '未命名';
  const pos = document.createElement('span');
  pos.className = 'search-hit-pos';
  pos.textContent = '第 ' + (hit.index + 1) + ' / ' + hit.total + ' 条'
    + (hit.role === 'user' ? ' · 你' : (hit.speaker ? ' · ' + hit.speaker : ''));
  head.append(title, pos);
  const body = document.createElement('div');
  body.className = 'search-hit-body';
  body.appendChild(paint(hit.snippet || '', q));
  row.append(head, body);
  row.onclick = () => jump(hit);
  return row;
}

function setActive(n) {
  if (!rows.length) return;
  active = (n + rows.length) % rows.length;
  rows.forEach((r, i) => r.classList.toggle('active', i === active));
  rows[active].scrollIntoView({ block: 'nearest' });
}

/** 占位提示：只写文字。不走 innerHTML 是因为下面的失败文案带 e.message，
 *  它可能含服务端回显的任意字符串。 */
function say(text) {
  const list = $('#search-hits');
  const count = $('#search-count');
  if (count) count.textContent = '';
  if (list) {
    list.innerHTML = '';
    const row = document.createElement('div');
    row.className = 'hint';
    row.textContent = text;
    list.appendChild(row);
  }
  rows = [];
  active = -1;
}

async function runQuery(text) {
  const q = (text || '').trim();
  if (q.length < 2) {
    say('至少输入两个字。');
    return;
  }
  const mine = ++seq;
  const count = $('#search-count');
  if (count) count.textContent = '搜索中…';
  try {
    const got = await api('/api/search?q=' + encodeURIComponent(q));
    if (mine !== seq) return;
    const hits = got.hits || [];
    if (!hits.length) {
      say('没有命中。');
      return;
    }
    const list = $('#search-hits');
    if (!list) return;
    list.innerHTML = '';
    rows = hits.map((hit) => {
      const row = hitRow(hit, q);
      list.appendChild(row);
      return row;
    });
    if (count) count.textContent = hits.length + ' 处' + (got.truncated ? '（已截断）' : '');
    setActive(0);
  } catch (e) {
    if (mine !== seq) return;
    say('搜索失败：' + (e.message || e));
  }
}

export function openSearch() {
  const modal = $('#search-modal');
  const input = $('#search-input');
  if (!modal || !input) return;
  modal.classList.remove('hidden');
  input.value = '';
  say('输入关键词，扫的是所有会话的全文。');
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
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActive(active + 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActive(active - 1);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        // 已经有结果就直接跳过去，没有才重新查
        if (rows.length && active >= 0) rows[active].click();
        else { clearTimeout(timer); runQuery(input.value); }
      } else if (e.key === 'Escape') {
        close();
      }
    });
  }
  const modal = $('#search-modal');
  if (modal) modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
}
