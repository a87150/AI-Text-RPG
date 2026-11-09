# -*- coding: utf-8 -*-
"""模型调用：请求、SSE 解析、自动重试。"""
import json
import re

import requests

from .config import (
    MAX_CHAT_ATTEMPTS, OLLAMA_API_URL, REQUEST_TIMEOUT, RETRY_TEMP_STEP,
    current_target, provider_endpoint, provider_headers, reasoning_effort,
)

# ============================== 模型调用 ==============================
def model_call(messages, temperature=0.8, stream=False, extra=None):
    provider, model = current_target()
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
    }
    if provider == "ollama":
        # 支持思考的本地模型不给档位会一直想，显式压到设置里的强度
        payload["reasoning_effort"] = reasoning_effort()
    if extra:
        payload.update(extra)
    resp = requests.post(
        provider_endpoint(provider),
        json=payload,
        stream=stream,
        headers=provider_headers(provider),
        timeout=REQUEST_TIMEOUT,
    )
    resp.encoding = "utf-8"
    return resp


def model_text(messages, temperature=0.8):
    resp = model_call(messages, temperature=temperature, stream=False)
    if resp.status_code != 200:
        raise RuntimeError("Ollama 返回 %s：%s" % (resp.status_code, resp.text[:300]))
    data = resp.json()
    msg = data["choices"][0]["message"]
    # 有些本地模型把正文整段写进 reasoning，content 是空的
    content = (msg.get("content") or "").strip() \
        or (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    return strip_thinking(content)


def iter_sse_parts(resp):
    """逐块解析 OpenAI 兼容流，产出 (content, reasoning)。"""
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content") or ""
        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
        if content or reasoning:
            yield content, reasoning


def completion_events(messages, base_temp, max_attempts, min_chars=0,
                      extra=None, validate=None, with_reasoning=False):
    """流式生成，质量不达标自动重试（chat / opening / enhance 共用）。

    事件与 SSE 同构：thinking / delta / retry / final / error。
    最后一个事件必定是 final（成功）或 error（失败）。
    """
    last_error = "未知错误"
    last_raw = ""
    attempts_used = 0

    for attempt in range(1, max_attempts + 1):
        attempts_used = attempt
        if attempt > 1:
            yield {"type": "retry", "attempt": attempt,
                   "max": max_attempts, "reason": last_error}
        collected = []
        reasonings = []
        think_chars = 0
        content_chars = 0
        try:
            resp = model_call(
                messages,
                temperature=min(1.5, base_temp + RETRY_TEMP_STEP * (attempt - 1)),
                stream=True,
                extra=extra,
            )
            if resp.status_code != 200:
                last_error = "接口返回 %s" % resp.status_code
                continue
            for content, reasoning in iter_sse_parts(resp):
                if reasoning:
                    reasonings.append(reasoning)
                    if with_reasoning:
                        think_chars += len(reasoning)
                        yield {"type": "thinking", "chars": think_chars}
                if content:
                    collected.append(content)
                    content_chars += len(content)
                    yield {"type": "delta", "content": content, "chars": content_chars}
        except RuntimeError as exc:
            # 地址 / Key 没配好：重试没有意义，直接报给用户
            yield {"type": "error", "attempts": attempt,
                   "message": "模型配置有误：%s" % exc, "raw": ""}
            return
        except requests.exceptions.RequestException as exc:
            last_error = "连接失败：%s" % exc
            continue

        last_raw = strip_thinking("".join(collected)).strip()
        if not last_raw:
            # content 为空时用 reasoning 兜底：部分模型只往这里写正文
            last_raw = strip_thinking("".join(reasonings)).strip()
        if not last_raw:
            last_error = "模型返回空内容"
            continue
        if min_chars and len(last_raw) < min_chars:
            last_error = "输出过短"
            continue
        if validate:
            problem = validate(last_raw)
            if problem:
                last_error = problem
                continue
        yield {"type": "final", "text": last_raw, "attempts": attempts_used}
        return

    yield {"type": "error", "attempts": attempts_used,
           "message": "已自动重试 %d 次仍失败：%s" % (attempts_used, last_error),
           "raw": last_raw[:800]}


def sse(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)


def strip_thinking(text):
    if not text:
        return ""
    t = THINK_RE.sub("", text).strip()
    # 推理字段常在正文前带一段分隔线，去掉
    return re.sub(r"^(?:[-*_]{3,}[ \t]*\n+)+", "", t).strip()


def extract_json(text):
    if not text:
        return None
    t = strip_thinking(text)
    t = re.sub(r"^```(?:json|JSON)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None
    raw = t[start:end + 1]
    for candidate in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None
