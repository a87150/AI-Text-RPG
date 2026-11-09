/** 发送消息、流式接收、发送状态与失败处理。 */
import { streamSSE, StreamEventError } from './api.js';
import { state } from './state.js';
import {
  $, toast, buildMessage, buildTurn, renderTurn, renderStreaming,
  setAiStatus, appendRetryHint, renderMessages, updateStatusCard,
  scrollToBottom, syncAll, syncComposer, focusComposer, mdToHtml, escapeHtml, updateSub,
} from './ui.js';
import { loadSessions, refreshCurrentSession } from './sessions.js';

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
  input.value = '';
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
  scrollToBottom();

  state.busy = true;
  syncAll();
  focusComposer({ force: true });

  const startedAt = Date.now();
  let acc = '';
  let longestAcc = '';
  let gotDone = false;
  let success = false;

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
          scrollToBottom();

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
          if (turn && (turn.line || turn.narrator)) {
            // 完成后重排：先画外音，再角色言行
            current = renderTurn(turnBox, turn) || current;
            if (!s.messages) s.messages = [];
            const msg = { role: 'assistant', content: turn.line || '' };
            if (turn.narrator) msg.narrator = turn.narrator;
            if (turn.speaker) msg.speaker = turn.speaker;
            s.messages.push(msg);
          }
          if (ev.characters) s.characters = ev.characters;
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
      { idleTimeout: WAIT_MS }
    );

    if (!gotDone) throw new Error('连接提前中断，未收到完整回复');
    success = true;

  } catch (e) {
    const msg = (e && e.message) ? e.message : String(e);
    const shown = longestAcc || acc;
    current = renderStreaming(turnBox, shown, speaker || aiLabel, 0, isPlain) || current;
    if (current) {
      current.bubble.innerHTML = (shown ? mdToHtml(shown) + '<br>' : '')
        + '<span class="err">⚠ ' + escapeHtml(msg) + '</span>';
    }
    setAiStatus('生成失败：' + msg, 'error');
    toast('生成失败：' + msg, 'error');

  } finally {
    clearInterval(ticker);
    if (current && current.bubble) current.bubble.classList.remove('typing');
    state.busy = false;
    syncAll();

    // 光标回输入框：按钮在发送时被禁用过，浏览器会把它变成 body 焦点
    focusComposer({ force: document.activeElement === document.body });
    requestAnimationFrame(() => focusComposer());
    setTimeout(() => focusComposer(), 60);

    if (!success) appendRetryHint(text);

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
