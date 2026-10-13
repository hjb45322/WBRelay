# -*- coding: utf-8 -*-
"""OpenAI 两种协议之间的转换（responses <-> chat/completions）。"""
import json
import os
import time

# ---------------------------------------------------------------------------
# 协议翻译（responses <-> chat，简版够用）
# ---------------------------------------------------------------------------
def _args_json(value):
    """把工具调用参数统一成**字符串**。

    OpenAI(chat) 与 Responses 都要求 `arguments` 是 JSON 文本：
      · 标准客户端给字符串 → 原样返回；
      · 部分客户端 / harness 会把**已解析的 dict / list** 直接塞进来 —— 必须重新序列化，
        否则上游收到对象会把整条工具调用判废（实测：模型丢掉工具上下文、直接改口答）；
      · None → 空串（等价于"无参数调用"）。
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    return str(value)

def responses_to_chat(payload):
    chat = {
        "model": payload.get("model"),
        "stream": bool(payload.get("stream")),
    }
    msgs = []
    for item in payload.get("input") or []:
        if isinstance(item, str):
            msgs.append({"role": "user", "content": item})
        elif isinstance(item, dict) and item.get("type") == "message":
            role = item.get("role") or "user"
            content = ""
            for c in item.get("content") or []:
                if isinstance(c, str):
                    content += c
                elif isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text"):
                    content += str(c.get("text") or "")
            msgs.append({"role": role, "content": content})
    if payload.get("instructions"):
        msgs.insert(0, {"role": "system", "content": payload["instructions"]})
    if not msgs:
        msgs = [{"role": "user", "content": ""}]
    chat["messages"] = msgs
    if payload.get("tools"):
        chat["tools"] = payload["tools"]
    if payload.get("tool_choice") is not None:
        chat["tool_choice"] = payload["tool_choice"]
    if payload.get("parallel_tool_calls") is not None:
        chat["parallel_tool_calls"] = payload["parallel_tool_calls"]
    return chat


def chat_to_response(chat_obj, model):
    out = []
    for ch in chat_obj.get("choices") or []:
        m = ch.get("message") or {}
        if m.get("content"):
            out.append({"type": "message", "role": m.get("role", "assistant"),
                        "content": [{"type": "output_text", "text": m["content"]}]})
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            out.append({"type": "function_call", "name": fn.get("name"),
                        "arguments": _args_json(fn.get("arguments"))})
    return {
        "id": "resp_" + os.urandom(12).hex(), "object": "response",
        "status": "completed", "model": model, "output": out,
        "usage": chat_obj.get("usage"), "created_at": int(time.time()),
    }


# ---------------------------------------------------------------------------
# 反向翻译：chat 客户端 × responses 上游（见 docs/方案-OpenCode接管修复.md）
# ---------------------------------------------------------------------------
def _chat_text_of(content):
    """chat 的 content（字符串 / 分片数组）→ (纯文本, 图片 URL 列表)。"""
    if isinstance(content, str):
        return content, []
    texts = []
    images = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                texts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("text", "input_text", "output_text"):
                texts.append(str(part.get("text") or ""))
            elif ptype in ("image_url", "input_image"):
                url = part.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                if url is None:
                    url = part.get("url")
                if url:
                    images.append(str(url))
    return "".join(texts), images


def _tool_choice_to_responses(choice):
    """chat 的 tool_choice → responses 的 tool_choice。认不出的形态原样透传。"""
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict):
        fn = choice.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return {"type": "function", "name": str(fn["name"])}
    return choice


def chat_to_responses(payload):
    """Chat Completions 请求 → Responses 请求（客户端 chat × 上游 responses）。

    映射范围：文本 / 图片 / system → instructions / 工具定义 / 工具调用与工具结果 /
    推理档位 / 采样参数 / 输出上限。

    Responses 协议里**没有**对应位置的字段（frequency_penalty、presence_penalty、
    stop、logprobs、stream_options 等）一律显式丢弃 —— 硬塞会被上游按非法参数拒绝。
    """
    out = {"model": payload.get("model"), "stream": bool(payload.get("stream"))}
    instructions = []
    items = []
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        raw = msg.get("content")
        if role in ("system", "developer"):
            text, _ = _chat_text_of(raw)
            if text:
                instructions.append(text)
            continue
        if role == "tool":
            text, _ = _chat_text_of(raw)
            items.append({"type": "function_call_output",
                          "call_id": str(msg.get("tool_call_id") or ""),
                          "output": text})
            continue
        text, images = _chat_text_of(raw)
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                items.append({"type": "function_call",
                              "call_id": str(tc.get("id") or ""),
                              "name": str(fn.get("name") or ""),
                              "arguments": _args_json(fn.get("arguments"))})
        content = []
        if text:
            content.append({"type": "output_text" if role == "assistant" else "input_text",
                            "text": text})
        for url in images:
            content.append({"type": "input_image", "image_url": url})
        if content:
            items.append({"type": "message", "role": role, "content": content})
    if instructions:
        out["instructions"] = "\n\n".join(instructions)
    if not items:
        items = [{"type": "message", "role": "user",
                  "content": [{"type": "input_text", "text": ""}]}]
    out["input"] = items

    tools = []
    for t in payload.get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else None
        if fn is None:
            tools.append(t)                    # 已是 responses 形态，原样带过
            continue
        entry = {"type": "function", "name": str(fn.get("name") or ""),
                 "parameters": fn.get("parameters") or {"type": "object", "properties": {}}}
        if fn.get("description"):
            entry["description"] = str(fn["description"])
        if fn.get("strict") is not None:
            entry["strict"] = bool(fn["strict"])
        tools.append(entry)
    if tools:
        out["tools"] = tools
    if payload.get("tool_choice") is not None:
        out["tool_choice"] = _tool_choice_to_responses(payload["tool_choice"])
    if payload.get("parallel_tool_calls") is not None:
        out["parallel_tool_calls"] = bool(payload["parallel_tool_calls"])
    if payload.get("max_completion_tokens") is not None:
        out["max_output_tokens"] = payload["max_completion_tokens"]
    elif payload.get("max_tokens") is not None:
        out["max_output_tokens"] = payload["max_tokens"]
    for key in ("temperature", "top_p", "seed", "user"):
        if payload.get(key) is not None:
            out[key] = payload[key]
    effort = payload.get("reasoning_effort")
    if effort is None:
        rsn = payload.get("reasoning")
        if isinstance(rsn, dict):
            effort = rsn.get("effort")
        elif isinstance(rsn, str):
            effort = rsn
    if effort:
        out["reasoning"] = {"effort": str(effort)}
    return out


def usage_to_chat(usage):
    """responses usage → chat usage（字段名不同、数值同源）。无有效字段返回 None。"""
    if not isinstance(usage, dict):
        return None
    out = {}
    prompt = usage.get("input_tokens")
    if prompt is None:
        prompt = usage.get("prompt_tokens")
    if prompt is not None:
        out["prompt_tokens"] = prompt
    completion = usage.get("output_tokens")
    if completion is None:
        completion = usage.get("completion_tokens")
    if completion is not None:
        out["completion_tokens"] = completion
    total = usage.get("total_tokens")
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)
    if total is not None:
        out["total_tokens"] = total
    details = usage.get("input_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        out["prompt_tokens_details"] = {"cached_tokens": details["cached_tokens"]}
    details = usage.get("output_tokens_details")
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        out["completion_tokens_details"] = {"reasoning_tokens": details["reasoning_tokens"]}
    return out or None


def responses_to_chat_completion(obj, model=None):
    """Responses 响应对象 → Chat Completions 响应对象（非流式）。"""
    text = []
    tool_calls = []
    for item in obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    text.append(str(c.get("text") or ""))
        elif itype == "function_call":
            tool_calls.append({"id": str(item.get("call_id") or item.get("id") or ""),
                               "type": "function",
                               "function": {"name": str(item.get("name") or ""),
                                            "arguments": _args_json(item.get("arguments"))}})
    message = {"role": "assistant", "content": "".join(text) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    out = {"id": obj.get("id") or ("chatcmpl-" + os.urandom(8).hex()),
           "object": "chat.completion",
           "created": int(obj.get("created_at") or time.time()),
           "model": model or obj.get("model") or "",
           "choices": [{"index": 0, "message": message,
                        "finish_reason": "tool_calls" if tool_calls else "stop"}]}
    usage = usage_to_chat(obj.get("usage"))
    if usage:
        out["usage"] = usage
    return out


# ---------------------------------------------------------------------------
# 用量
# ---------------------------------------------------------------------------
