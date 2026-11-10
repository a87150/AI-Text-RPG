import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9431; const API = 'http://127.0.0.1:5000';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const dir = mkdtempSync(join(tmpdir(), 'lt-ui56-'));
const proc = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run', '--window-size=1400,900',
  '--remote-debugging-port=' + PORT, '--user-data-dir=' + dir, API + '/'], { stdio: 'ignore' });
async function findPage() {
  for (let i = 0; i < 40; i += 1) {
    try {
      const list = await (await fetch('http://127.0.0.1:' + PORT + '/json/list')).json();
      const pages = list.filter((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      const p = pages.find((t) => (t.url || '').includes('127.0.0.1:5000')) || pages[0];
      if (p) return p;
    } catch { /* wait */ }
    await sleep(300);
  }
  throw new Error('no page');
}
const page = await findPage();
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
let seq = 0; const pending = new Map(); const errors = [];
ws.addEventListener('message', (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === 'Runtime.exceptionThrown') {
    const d = m.params.exceptionDetails;
    errors.push(String((d.exception && (d.exception.description || d.exception.value)) || d.text || '?').split('\n')[0]);
  }
});
function send(method, params = {}) {
  seq += 1; const id = seq;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((res, rej) => pending.set(id, (m) => (m.error ? rej(new Error(m.error.message)) : res(m.result))));
}
async function js(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) return 'ERR ' + String(r.exceptionDetails.exception && r.exceptionDetails.exception.description).split('\n')[0];
  return r.result.value;
}
const out = [];
const check = (n, ok, extra = '') => out.push((ok ? 'PASS ' : 'FAIL ') + n + (ok ? '' : ' :: ' + extra));
await send('Runtime.enable');
await sleep(3500);
await js("(function(){var b=document.querySelector('[data-view=sessions]');if(b)b.click();return !!b;})()");
await sleep(1000);
await js("(function(){var it=document.querySelector('.session-item');if(it)it.click();return !!it;})()");
await sleep(2800);
const sub = await js("document.querySelector('#chat-sub').textContent");
out.push('INFO 顶栏: ' + sub);
check('写的是本人→', sub.indexOf('本人→') === 0 || sub.indexOf('本人→') >= 0, sub);
check('不再有「我 = 」', sub.indexOf('我 = ') < 0, sub);
const meCount = (sub.match(/谭谈/g) || []).length;
check('主角名字只出现一次', meCount <= 1, sub);
check('主角那一格能点', (await js("(function(){var b=document.querySelector('#chat-sub .char-chip.me');return b ? b.dataset.name : '';})()")) !== '');
const meName = await js("(function(){var b=document.querySelector('#chat-sub .char-chip.me');return b ? b.dataset.name : '';})()");
await js("(function(){var b=document.querySelector('#chat-sub .char-chip.me');if(b)b.click();return !!b;})()");
await sleep(600);
const popShown = await js("(function(){var p=document.querySelector('#char-pop');return p && !p.classList.contains('hidden') ? p.textContent.slice(0,40) : 'NO';})()");
out.push('INFO 点开主角: ' + popShown);
check('点主角能弹出状态', popShown !== 'NO', popShown);
check('弹层标题是主角名', popShown.indexOf(meName) >= 0, popShown);
await js("(function(){var p=document.querySelector('#char-pop');if(p)p.classList.add('hidden');return 1;})()");
writeFileSync('_sub2.png', Buffer.from((await send('Page.captureScreenshot', { format: 'png' })).data, 'base64'));
check('没有 JS 异常', errors.length === 0, errors.join(' | '));
console.log(out.join('\n'));
ws.close(); proc.kill();
process.exit(0);
