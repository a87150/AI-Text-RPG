# -*- coding: utf-8 -*-
"""模型调用：请求、SSE 解析、自动重试。"""
import json
import re

import requests

from .config import (
    REQUEST_TIMEOUT, RETRY_TEMP_STEP,
    current_target, provider_endpoint, provider_headers, reasoning_effort,
)

# 哪些提供方已经因为发了 reasoning_effort 而报错。
# 只存内存：重启后重探一次，比把探测结果写进配置文件安全。
_NO_REASONING = set()
# 不认 stream_options（拿不到用量）的提供方，同样只记内存
_NO_STREAM_USAGE = set()

def reasoning_supported():
    """当前提供方是否还应当发思考档位。供设置界面提示用。"""
    return current_target()[0] not in _NO_REASONING

# ============================== 模型调用 ==============================
def model_call(messages, temperature=0.8, stream=False, extra=None, reasoning=None):
    provider, model = current_target()
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
    }
    # 思考强度是 OpenAI 规范字段，云端和本地都该带上：
    # 以前只给 ollama 发，选了云端后这个设置就彻底失效了。
    # none 不发：规范里没这个值，传过去会被拒。
    level = (reasoning or reasoning_effort() or "").strip()
    if level and level != "none" and provider not in _NO_REASONING:
        payload["reasoning_effort"] = level
    # 流式默认不报用量，要主动要。不认这个参数的端点会 400，
    # 所以下面还有一次去掉重发。
    if stream and provider not in _NO_STREAM_USAGE:
        payload["stream_options"] = {"include_usage": True}
    if extra:
        payload.update(extra)

    def _post(body):
        r = requests.post(
            provider_endpoint(provider),
            json=body,
            stream=stream,
            headers=provider_headers(provider),
            timeout=REQUEST_TIMEOUT,
        )
        r.encoding = "utf-8"
        return r

    resp = _post(payload)
    # 服务端不认这个字段时会回 400。不能因为思考档位把正文请求带死，
    # 去掉重发一次，并记下来，下次直接不发。这就是能拿到的最强“探测”：
    # 没有任何云端提供方会告诉你它支持哪几个档位，但拒绝的方式是统一的。
    if resp.status_code == 400:
        low = (resp.text or "").lower()
        changed = False
        if "reasoning_effort" in payload and (
                "reasoning" in low or "unrecognized" in low or "unknown" in low or "extra" in low):
            payload.pop("reasoning_effort", None)
            _NO_REASONING.add(provider)
            changed = True
        if "stream_options" in payload and (
                "stream_options" in low or "unrecognized" in low
                or "unknown" in low or "extra" in low):
            payload.pop("stream_options", None)
            _NO_STREAM_USAGE.add(provider)
            changed = True
        if changed:
            resp = _post(payload)
    return resp

def model_text(messages, temperature=0.8, reasoning=None):
    resp = model_call(messages, temperature=temperature, stream=False, reasoning=reasoning)
    if resp.status_code != 200:
        raise RuntimeError("Ollama 返回 %s：%s" % (resp.status_code, resp.text[:300]))
    data = resp.json()
    msg = data["choices"][0]["message"]
    # 有些本地模型把正文整段写进 reasoning，content 是空的
    content = (msg.get("content") or "").strip() \
        or (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    return strip_thinking(content)

def iter_sse_parts(resp, usage=None):
    """逐块解析 OpenAI 兼容流，产出 (content, reasoning)。

    usage 传一个 dict 进来就把服务端报的用量装回去。这个块往往
    在最后一个 chunk（choices 为空），所以要在跳过 choices 之前取。
    """
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
        u = obj.get("usage")
        if isinstance(u, dict) and u and usage is not None:
            usage.update(u)
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
        usage = {}
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
            for content, reasoning in iter_sse_parts(resp, usage):
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
        # 量长度要剥掉状态行：二十字正文配一百字 [[在场：…]]，
        # 按原文长度算就合格了，短回复被放过去。这里只影响判定，
        # 不动 last_raw——下游 parse_turn 还要从里面读状态
        if min_chars and len(strip_status_leaks(last_raw)) < min_chars:
            last_error = "输出过短"
            continue
        if validate:
            problem = validate(last_raw)
            if problem:
                last_error = problem
                continue
        yield {"type": "final", "text": last_raw, "attempts": attempts_used,
               "usage": usage}
        return

    yield {"type": "error", "attempts": attempts_used,
           "message": "已自动重试 %d 次仍失败：%s" % (attempts_used, last_error),
           "raw": last_raw[:800]}

def sse(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)

# 模型有时候不写 <thinking> 标签，直接把分析过程当正文交上来：
# 「用户想让我写一个故事背景……让我想想需要什么……等等，重新读一下」。
# 这类输出以第一人称分析开头，最后才给结果。
_THINK_HEAD = re.compile(
    r"^\s*(?:用户想让我|用户想要|用户希望|我需要|让我想想|让我再|我们先|首先我"
    r"|好的[，,]\s*(?:我|让我|先)|嗯[，,]\s*(?:我|让)|等等[，,]|重新读一下|再读一遍)",
    re.M)

def strip_thinking(text):
    if not text:
        return ""
    t = THINK_RE.sub("", text).strip()
    # 推理字段常在正文前带一段分隔线，去掉
    t = re.sub(r"^(?:[-*_]{3,}[ \t]*\n+)+", "", t).strip()
    if not _THINK_HEAD.match(t):
        return t
    # 命中分析口吻：找最后一个「X：」式标题，取它后面那段当正文。
    # 找不到标题就不猜，原样返回，交给 validate 判长度和口吻
    marks = list(re.finditer(r"(?:故事背景|背景|世界观|设定正文)\s*[:：]", t, re.M))
    if marks:
        return t[marks[-1].end():].strip()
    return t

# 模型常把状态写成裸 JSON 而不是带标签，或者标签只写了一半；这些都会漏进正文
_LEAK_BLOCK = re.compile(r"<StatusBlock>.*?(?:</StatusBlock>|$)", re.S | re.I)
_LEAK_TAG = re.compile(r"</?StatusBlock>", re.I)
_LEAK_JSON = re.compile(
    r'\{(?:[^{}]|\{[^{}]*\})*"(?:location|time|relations|items|goal|facts'
    r'|地点|时间|关系|物品|目标|事实)"(?:[^{}]|\{[^{}]*\})*\}',
    re.S | re.I,
)
# 正文后面紧跟的 [[地点：…]]：_LEAK_LINE 是整行锚定的，挤在同一行就吃不到。
# 只认带状态键的，正文里偶然出现的其他方括号不动（键名跟 characters.MARK_KEYS 一致，
# 那边是从这里导入的，不能反过来）。
_LEAK_MARK = re.compile(
    r"[ \t]*\[\[(?:在场|离场|退场|地点|位置|场景|时间|关系|人物|物品|携带|目标|意图|事实)"
    r"[：:][^\[\]]{0,400}\]\][ \t]*"
)
# 状态块前后常带的分隔线与空字段名，本身没有意义
_LEAK_LINE = re.compile(
    r"^[ 	]*(?:-{2,}|={2,}|新事实[：:]?|事实[：:]?|状态[：:]?|当前状态[：:]?"
    r"|旁白[：:]?|\[\[[^\]]*\]\])[ 	]*$",
    re.M,
)
def loose_status_json(text):
    """正文里没带标签的裸状态 JSON，能捞就捞一段出来。"""
    match = _LEAK_JSON.search(text or "")
    return match.group(0) if match else ""

def strip_status_leaks(text):
    """把漏进正文的状态残渣清掉：标签、裸 JSON、围栏、多余分隔行。"""
    body = text or ""
    if not body:
        return ""
    body = _LEAK_BLOCK.sub("", body)
    body = _LEAK_TAG.sub("", body)
    body = _LEAK_JSON.sub("", body)
    body = _LEAK_MARK.sub("", body)
    body = body.replace("\x60\x60\x60json", "").replace("\x60\x60\x60", "")
    body = _LEAK_LINE.sub("", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()

def extract_json_all(text):
    """一段里可能连着写好几个对象（模型一次加两个人，中间只有换行），全都捞出来。

    extract_json 是从第一个 { 取到最后一个 }，两个对象连写就整段解析失败，
    结果一个都拿不到。
    """
    if not text:
        return []
    t = strip_thinking(text)
    t = re.sub(r"\x60{3}(?:json|JSON)?", "", t)
    decoder = json.JSONDecoder()
    out, i = [], 0
    while i < len(t):
        start = t.find("{", i)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(t[start:])
        except ValueError:
            i = start + 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = start + end
    return out

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
