/** 备份面板：手动打包、下载、删除，以及定时开关。 */
import { api } from './api.js';
import { state } from './state.js';
import { $, toast } from './ui.js';

const fmtSize = (n) => (n > 1024 * 1024
  ? (n / 1024 / 1024).toFixed(1) + ' MB'
  : Math.max(1, Math.round(n / 1024)) + ' KB');

const fmtTime = (at) => new Date((at || 0) * 1000)
  .toLocaleString('zh-CN', { hour12: false });

function setMsg(text, kind = '') {
  const el = $('#backup-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'backup-msg' + (kind ? ' ' + kind : '');
}

export async function refreshBackups() {
  const box = $('#backup-list');
  if (!box) return;
  box.textContent = '读取中…';
  try {
    const data = await api('/api/backups');
    const conf = data.conf || {};
    $('#backup-enabled').checked = !!conf.enabled;
    $('#backup-hours').value = conf.every_hours || 24;
    const items = data.items || [];
    box.innerHTML = '';
    if (!items.length) {
      box.textContent = '还没有备份。点上面「立即备份」打一个包。';
      return;
    }
    const head = document.createElement('div');
    head.className = 'backup-hint';
    head.textContent = '共 ' + items.length + ' 份，只保留最近 30 份。存在 ' + (data.dir || 'backups/');
    box.appendChild(head);
    for (const item of items) {
      const row = document.createElement('div');
      row.className = 'backup-item';
      const name = document.createElement('span');
      name.className = 'bi-name';
      name.textContent = fmtTime(item.at);
      name.title = item.name;
      const size = document.createElement('b');
      size.textContent = fmtSize(item.size);
      const get = document.createElement('a');
      get.className = 'bi-btn';
      get.textContent = '下载';
      get.href = '/api/backups/' + encodeURIComponent(item.name);
      get.download = item.name;
      const redo = document.createElement('button');
      redo.type = 'button';
      redo.className = 'bi-btn bi-restore';
      redo.textContent = '还原';
      redo.title = '用这份备份覆盖当前数据';
      redo.onclick = async () => {
        if (!confirm('用这份备份覆盖当前所有对话和设定？\n\n还原前会自动打一份保险包，点错了还能退回来。')) return;
        redo.disabled = true;
        setMsg('正在还原…');
        try {
          const data = await api('/api/backups/' + encodeURIComponent(item.name) + '/restore',
            { method: 'POST' });
          const r = data.result || {};
          setMsg('已还原 ' + (r.files || 0) + ' 个文件，页面即将刷新…', 'ok');
          // 内存里的会话已经过期了，只能整体刷新
          setTimeout(() => location.reload(), 900);
        } catch (e) {
          setMsg('还原失败：' + e.message, 'error');
          redo.disabled = false;
        }
      };

      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'bi-btn bi-del';
      del.textContent = '删除';
      del.onclick = async () => {
        if (!confirm('删掉这份备份？')) return;
        try {
          await api('/api/backups/' + encodeURIComponent(item.name), { method: 'DELETE' });
          refreshBackups();
        } catch (e) {
          toast('删除失败：' + e.message, 'error');
        }
      };
      row.append(name, size, get, redo, del);
      box.appendChild(row);
    }
  } catch (e) {
    box.textContent = '读取失败：' + e.message;
  }
}

export async function runBackup() {
  const btn = $('#btn-backup-now');
  btn.disabled = true;
  setMsg('正在打包…');
  try {
    // 数据多的时候打包会久一点，比通用接口的 20 秒宽一些
    const resp = await fetch('/api/backups', { method: 'POST' });
    if (!resp.ok) {
      const info = await resp.json().catch(() => ({}));
      throw new Error(info.error || ('HTTP ' + resp.status));
    }
    const data = await resp.json();
    const item = data.item || {};
    setMsg('已生成 ' + (item.files || 0) + ' 个文件的包（' + fmtSize(item.size || 0) + '）', 'ok');
    refreshBackups();
  } catch (e) {
    setMsg('备份失败：' + e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

export async function saveBackupConf() {
  const enabled = $('#backup-enabled').checked;
  const every = parseInt($('#backup-hours').value, 10) || 24;
  try {
    const conf = await api('/api/backup-config', {
      method: 'PUT',
      body: { enabled, every_hours: every },
    });
    $('#backup-hours').value = conf.every_hours;
    setMsg(conf.enabled
      ? '已开启：每 ' + conf.every_hours + ' 小时自动备份一次'
      : '已关闭定时备份', 'ok');
  } catch (e) {
    setMsg('保存失败：' + e.message, 'error');
  }
}
