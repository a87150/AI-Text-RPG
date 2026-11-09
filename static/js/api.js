/** 后端接口封装：统一的网络错误、超时与 SSE 解析。 */

const REQUEST_TIMEOUT = 20000;

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** 事件流里服务端主动报的业务错误，不应被当成「连接中断」再包一层。 */
export class StreamEventError extends Error {
  constructor(message) {
    super(message);
    this.name = 'StreamEventError';
  }
}

async function errorMessage(resp) {
  const data = await resp.json().catch(() => null);
  if (data && data.error) return data.error;
  if (resp.status === 404) return '接口不存在（404），后端版本可能不匹配';
  if (resp.status === 409) return '上一条消息还在生成中，请稍候再试';
  return '请求失败：HTTP ' + resp.status;
}

export async function api(path, { method = 'GET', body } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT);
  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    });
  } catch (e) {
    throw new ApiError(e && e.name === 'AbortError'
      ? '请求超时（' + REQUEST_TIMEOUT / 1000 + ' 秒），后端没有响应'
      : '无法连接后端服务，请确认 lt.py 正在运行');
  } finally {
    clearTimeout(timer);
  }
  if (!resp.ok) throw new ApiError(await errorMessage(resp), resp.status);
  return resp.json().catch(() => ({}));
}

/**
 * 以 SSE 方式读取流式接口。
 * onEvent 抛错会中断读取并把错误抛给调用方。
 */
export async function streamSSE(url, body, onEvent, { idleTimeout = 240000 } = {}) {
  const ctrl = new AbortController();
  let idleTimer = 0;
  let reader = null;

  const resetIdle = () => {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      try { ctrl.abort(new DOMException('idle timeout', 'TimeoutError')); } catch (e) { /* ignore */ }
    }, idleTimeout);
  };
  resetIdle();

  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      signal: ctrl.signal,
    });
    if (!resp.ok) throw new ApiError(await errorMessage(resp), resp.status);
    if (!resp.body) throw new ApiError('当前浏览器不支持流式读取');

    reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      resetIdle();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of chunk.split('\n')) {
          const s = line.trim();
          if (!s.startsWith('data:')) continue;
          let ev;
          try { ev = JSON.parse(s.slice(5).trim()); } catch (e) { continue; }
          onEvent(ev);
        }
      }
    }
  } catch (e) {
    if (e && (e.name === 'AbortError' || e.name === 'TimeoutError')) {
      throw new ApiError('超过 ' + Math.round(idleTimeout / 1000) + ' 秒没有新进度，已中止');
    }
    if (e instanceof ApiError || e instanceof StreamEventError) throw e;
    throw new ApiError('连接中断：' + ((e && e.message) || e));
  } finally {
    clearTimeout(idleTimer);
    if (reader) { try { reader.releaseLock(); } catch (e) { /* ignore */ } }
  }
}
