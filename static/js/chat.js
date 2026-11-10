/** 发送消息、流式接收、发送状态与失败处理。 */
import { streamSSE, StreamEventError } from './api.js';
import { state } from './state.js';
import {
  $, toast, buildMessage, buildTurn, renderTurn, renderStreaming,
  setAiStatus, appendRetryHint, renderMessages, updateStatusCard, askCharacterPick,
  scrollToBottom, syncAll, syncComposer, focusComposer, mdToHtml, escapeHtml, updateSub,
  clearDraft,
} from './ui.js';
import { loadSessions, refreshCurrentSession } from './sessions.js';
import { api } from './api.js';

const TICK_MS = 1000;          // 状态栏按整秒走
const WAIT_MS = 240000;        // 单次等待上限 240 秒
const STATUS_KEEP_MS = 4000;   // 「完成」状态保留时长

const secondsSince = (start) => Math.floor((Date.now() - start) / 1000);

function clearStatusSoon() {
  setTimeout(() => {
    if (state.busy) return;
    state.aiStatus = { text: '', kind: '' };
    syncComposer();
  }, STATUS_KEEP_MS);
}

/**
 * 发之前先跟物品账本对一下：输入里提到的东西如果记在别人名下，先问一句。
 * 事中拦一下，比事后让模型把剧情圆回来便宜得多。
 */
function itemConflict(text) {
  const sess = state.session;
  if (!sess) return null;
  const items = (sess.status && sess.status.items) || {};
  // 旧数据里 items 可能是一串文字（没写名字的公共物品），那种分不出归属，不拦
  if (!items || typeof items !== 'object' || Array.isArray(items)) return null;
  const me = sess.user_name || '我';
  const split = (bag) => (Array.isArray(bag) ? bag : String(bag || '').split(/[、,，;；]/))
    .map((n) => String(n).trim())
    .filter(Boolean);
  // 用户说的「我」就是主角，所以先看主角自己名下有没有。
  // 主角也有一份的东西不算冲突，否则拿着自己的东西还要被拦一下。
  const mine = new Set(split(items[me]));
  const found = new Map();
  for (const owner of Object.keys(items)) {
    if (owner === me) continue;
    for (const name of split(items[owner])) {
      // 单字（伞、刀）太容易撞上普通用词，只认两个字符以上的
      if (name.length < 2 || !text.includes(name)) continue;
      if (mine.has(name)) continue;
      if (!found.has(name)) found.set(name, []);
      if (!found.get(name).includes(owner)) found.get(name).push(owner);
    }
  }
  if (!found.size) return null;
  const [item, owners] = found.entries().next().value;
  return { item, owners };
}

let conflictPassed = '';

function hideConflict() {
  const tip = $('#conflict-tip');
  if (tip) { tip.classList.add('hidden'); tip.innerHTML = ''; }
}

function showConflict(hit, text) {
  const tip = $('#conflict-tip');
  if (!tip) return;
  tip.innerHTML = '';
  const msg = document.createElement('span');
  msg.textContent = '⚠ 「' + hit.item + '」账本上记在' + hit.owners.join('、')
    + '名下，你自己这边没有。这条消息说你手上有它。';
  const fix = document.createElement('button');
  fix.type = 'button';
  fix.textContent = '仍然发送';
  fix.onclick = () => {
    conflictPassed = text;
    hideConflict();
    sendFromInput();
  };
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'ghost';
  cancel.textContent = '改一下';
  cancel.onclick = () => { hideConflict(); focusComposer({ force: true }); };
  tip.append(msg, fix, cancel);
  tip.classList.remove('hidden');
}

/** 输入框 → 校验 → 发送。 */
export async function sendFromInput() {
  const input = $('#input');
  if (!state.session) {
    toast('先新建或选择一个故事', 'error');
    focusComposer();
    return;
  }
  if (state.busy) {
    toast('正在生成回复，稍候再发下一句', 'error');
    focusComposer();
    return;
  }
  const text = (input.value || '').trim();
  if (!text) {
    toast('先说点什么吧');
    focusComposer({ force: true });
    return;
  }
  if (conflictPassed !== text) {
    const hit = itemConflict(text);
    if (hit) { showConflict(hit, text); return; }
  }
  conflictPassed = '';
  hideConflict();
  input.value = '';
  clearDraft(state.session.id);
  syncComposer();
  await sendMessage(text, { isRetry: false });
}

export async function retryLast(text) {
  if (!state.session || state.busy) return;
  if (!text) return;
  // 只有「上一条就是这条没答上的用户消息」才算重试；
  // 否则后端会真的新增一条，界面也必须把它渲染出来
  const msgs = state.session.messages || [];
  const last = msgs[msgs.length - 1];
  const pending = !!last && last.role === 'user'
    && (last.content || '').trim() === text.trim();
  await sendMessage(text, { isRetry: pending });
}

/** 生成中把发送按钮换成「停止」：写歪了不该等它吐完 600 字。 */
function showStopButton(onStop) {
  const send = $('#btn-send');
  const stop = $('#btn-stop');
  if (send) send.classList.add('hidden');
  if (!stop) return;
  stop.classList.remove('hidden');
  stop.onclick = () => {
    stop.disabled = true;
    stop.textContent = '停止中…';
    onStop();
  };
  stop.disabled = false;
  stop.textContent = '■ 停止';
}

function hideStopButton() {
  const send = $('#btn-send');
  const stop = $('#btn-stop');
  if (stop) {
    stop.classList.add('hidden');
    stop.disabled = false;
    stop.textContent = '■ 停止';
  }
  if (send) send.classList.remove('hidden');
}

/** 停下来的半段内容：留下来还是扔掉，用户自己定。 */
function showDraftActions(turnBox, shown, speaker, isPlain, originalText) {
  const bar = document.createElement('div');
  bar.className = 'draft-bar';
  const tip = document.createElement('span');
  tip.className = 'draft-tip';
  tip.textContent = '已停止 · 这段是草稿';
  const keep = document.createElement('button');
  keep.type = 'button';
  keep.className = 'draft-keep';
  keep.textContent = '保留并继续';
  keep.onclick = async () => {
    keep.disabled = true;
    try {
      await api('/api/sessions/' + state.session.id + '/commit_partial',
        { method: 'POST', body: { content: shown, speaker: speaker || '' } });
      bar.remove();
      await refreshCurrentSession();
      renderMessages();
      updateSub();
      toast('草稿已存下，可以接着写', 'ok');
    } catch (e) {
      keep.disabled = false;
      toast('存不下草稿：' + e.message, 'error');
    }
  };
  const drop = document.createElement('button');
  drop.type = 'button';
  drop.className = 'draft-drop';
  drop.textContent = '丢弃重写';
  drop.onclick = () => {
    bar.remove();
    if (turnBox) turnBox.remove();
    const input = $('#input');
    if (input && !input.value.trim()) {
      input.value = originalText;
      syncComposer();
    }
    toast('已丢弃，内容放回输入框，改完再发', 'ok');
  };
  bar.append(tip, keep, drop);
  if (turnBox) turnBox.appendChild(bar);
}

async function sendMessage(text, { isRetry }) {
  const s = state.session;
  if (!s.ai_role && !s.world) {
    toast('先点右上角「设定」，把角色和背景写一下', 'error');
    return;
  }

  const box = $('#messages');
  const empty = box && box.querySelector('.empty');
  if (empty) empty.remove();
  const oldHint = box && box.querySelector('.retry-hint');
  if (oldHint) oldHint.remove();

  const userLabel = s.user_name || '我';
  const aiLabel = s.ai_name || 'AI';
  if (!isRetry && box) box.appendChild(buildMessage('user', text, userLabel).el);

  // 生成中不露角色名：只有旁白块 + 一个带省略号和秒数的气泡
  const turnBox = document.createElement('div');
  turnBox.className = 'turn';
  if (box) box.appendChild(turnBox);
  let speaker = '';
  let current = renderStreaming(turnBox, '', aiLabel, 0);
  scrollToBottom(true);

  state.busy = true;
  state.stopped = false;
  syncAll();
  focusComposer({ force: true });

  const startedAt = Date.now();
  let acc = '';
  let longestAcc = '';
  let gotDone = false;
  let success = false;
  let draftShown = false;

  // 一般模式整段都是叙述，直接进气泡，不再拆旁白块
  const isPlain = !!(state.conf && state.conf.mode === 'normal');
  // 秒数只出现在气泡里，底部状态栏不重复读秒
  const repaint = () => {
    if (gotDone) return;
    current = renderStreaming(
      turnBox, acc, speaker || aiLabel, secondsSince(startedAt), isPlain,
    ) || current;
  };
  const ticker = setInterval(repaint, TICK_MS);
  setAiStatus('正在生成…', 'warn');
  const stopper = new AbortController();
  state.abortStream = stopper;
  showStopButton(async () => {
    state.stopped = true;
    stopper.abort(new DOMException('用户停止', 'AbortError'));
  });

  try {
    await streamSSE(
      '/api/chat',
      { session_id: s.id, message: text, retry: !!isRetry },
      (ev) => {
        if (ev.type === 'speaker') {
          speaker = ev.name || '';
          repaint();

        } else if (ev.type === 'delta') {
          acc += ev.content || '';
          repaint();
          scrollToBottom(true);

        } else if (ev.type === 'retry') {
          if (acc.length > longestAcc.length) longestAcc = acc;
          acc = '';
          repaint();
          const reason = ev.reason || '';
          if (current) current.bubble.innerHTML = '<span class="pending">第 ' + ev.attempt + '/' + ev.max
            + ' 次重试：' + escapeHtml(reason) + '…</span>'
            + '<span class="tick">' + secondsSince(startedAt) + 's</span>';
          setAiStatus('第 ' + ev.attempt + '/' + ev.max + ' 次重试：' + reason, 'warn');

        } else if (ev.type === 'error') {
          throw new StreamEventError(ev.message || '生成失败');

        } else if (ev.type === 'done') {
          gotDone = true;
          if (ev.title) s.title = ev.title;
          const turn = ev.turn || (acc ? (isPlain
            ? { narrator: acc, speaker: '', line: '' }
            : { narrator: '', speaker: speaker || aiLabel, line: longestAcc || acc }) : null);
          if (turn && (turn.line || turn.narrator || (turn.lines || []).length)) {
            // 完成后重排：先画外音，再按发言顺序排角色气泡
            current = renderTurn(turnBox, turn) || current;
            if (!s.messages) s.messages = [];
            const msg = { role: 'assistant', content: turn.line || '' };
            if (turn.narrator) msg.narrator = turn.narrator;
            if (turn.speaker) msg.speaker = turn.speaker;
            if ((turn.lines || []).length) msg.lines = turn.lines;
            s.messages.push(msg);
          }
          if (ev.characters) s.characters = ev.characters;
          if (ev.pending_character && ev.pending_character.name) {
            const pending = ev.pending_character;
            askCharacterPick(pending, s.characters || []).then(async (drop) => {
              try {
                const r = await api('/api/sessions/' + s.id + '/character-pick', {
                  method: 'POST', body: { drop },
                });
                s.characters = r.characters;
                renderMessages();
                syncAll();
                toast(drop ? ('换掉了 ' + drop + '，' + r.added + ' 进来了') : '这次没登记新角色', 'ok');
              } catch (e) {
                toast('调整角色失败：' + e.message, 'error');
              }
            });
          }
          if (ev.status) { s.status = ev.status; updateStatusCard(ev.status); }
          s.context_chars = ev.context_chars;
          if (ev.context) s.context = ev.context;
          updateSub();
          setAiStatus('已完成', 'ok');
          clearStatusSoon();
          if (ev.attempts && ev.attempts > 1) {
            toast('已在自动重试 ' + (ev.attempts - 1) + ' 次后成功', 'ok');
          }
        }
      },
      { idleTimeout: WAIT_MS, signal: stopper.signal }
    );

    if (!gotDone) throw new Error('连接提前中断，未收到完整回复');
    success = true;

  } catch (e) {
    const stopped = state.stopped;
    const shown = longestAcc || acc;
    current = renderStreaming(turnBox, shown, speaker || aiLabel, 0, isPlain) || current;
    if (stopped) {
      // 停下来的内容不当失败：留着当草稿，让用户决定要还是不要
      setAiStatus('已停止', 'warn');
      if (shown.trim()) {
        draftShown = true;
        showDraftActions(turnBox, shown, speaker, isPlain, text);
      }
    } else {
      const msg = (e && e.message) ? e.message : String(e);
      if (current) {
        current.bubble.innerHTML = (shown ? mdToHtml(shown) + '<br>' : '')
          + '<span class="err">⚠ ' + escapeHtml(msg) + '</span>';
      }
      setAiStatus('生成失败：' + msg, 'error');
      toast('生成失败：' + msg, 'error');
    }

  } finally {
    state.abortStream = null;
    hideStopButton();
    clearInterval(ticker);
    if (current && current.bubble) current.bubble.classList.remove('typing');
    state.busy = false;
    syncAll();

    // 光标回输入框：按钮在发送时被禁用过，浏览器会把它变成 body 焦点
    focusComposer({ force: document.activeElement === document.body });
    requestAnimationFrame(() => focusComposer());
    setTimeout(() => focusComposer(), 60);

    // 停止也算没答上，只是把话说清楚，别让人以为是模型挂了
    if (!success && !(draftShown)) appendRetryHint(text, state.stopped);

    (async () => {
      const before = (s.messages || []).length;
      const fresh = await refreshCurrentSession();
      if (fresh) {
        const changed = (fresh.messages || []).length !== before
          || (fresh.summary || '') !== (s.summary || '');
        updateSub();
        if (changed && !state.busy) renderMessages();
      }
      loadSessions();
    })();
  }
}

/** 顶部「重试上一条」卡片与重试事件。 */
export function bindRetryEvents() {
  document.addEventListener('rp:retry-last', (e) => {
    const text = e.detail && e.detail.text;
    retryLast(text);
  });
}
