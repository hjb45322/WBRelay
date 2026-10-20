# -*- coding: utf-8 -*-
"""OpenAI 兼容端点：/v1/models、/v1/responses、/v1/chat/completions 的转发与协议翻译。

重试边界（本轮发布修复的重点，规则见 docs/方案-开源发布阻断修复.md §3.6）：

  · **尚未向客户端输出任何字节**时，上游失败可安全切换到下一个供应商：
    连接/读取失败、读取超时、响应体超限、非 JSON / 并非可解析的流。
  · **一旦开始向客户端输出**（已发响应头或已写正文），后续上游错误
    **绝不切换** —— 只记录错误、收尾（流式补 `response.completed` / `[DONE]`）
    并按 `realm="supplier:<id>"` 记一条带 `error` 的用量。
"""
import json
import os
import time
import urllib.error
import urllib.request

from ..log import _log
from ..models import _natural_key
from ..net import close_quietly, open_upstream_request
from ..protocol import (_args_json, chat_to_response, chat_to_responses,
                        responses_to_chat, responses_to_chat_completion,
                        usage_to_chat)
from ..redact import redact
from ..routing import (_route_models_set, cooling_suppliers_for, pick_supplier,
                       route_has_model, supplier_candidates, supplier_cooled,
                       supplier_mark_cool)
from ..settings import cooldown_429_secs, cooldown_secs
from ..usage import (extract_effort, extract_usage_from_sse_line,
                     extract_usage_tokens, record_usage)
from ..validate import check_model_name

# 上游非流式响应体上限（防止畸形/恶意上游打满内存）
MAX_UPSTREAM_BODY = 32 * 1024 * 1024

# ---- 请求侧失败 vs 供应商侧失败（docs/方案-请求侧错误语义.md）----
# 这些状态码表示「上游在拒绝这一份请求」，不是供应商不可用：给它打冷却会把
# 「一次请求体超限」放大成「该供应商 60 秒内所有模型都不可用」（2026-10-19 实测：
# 上游 413 → 冷却 60s → 客户端只看到 503「供应商都在冷却」）。
REQUEST_SIDE_STATUSES = (400, 404, 413, 422)

# 上游按 body 上限「先拒后关」时，客户端只拿到 WinError 10053/10054，无法从异常类型
# 区分「上游拒收」与「网络抖动」。用「已经推了多少字节」做判据：8 MiB 是保守下限
# （实测该量级上游已在 token 层顶到 1.048M 上限，16 MiB 触发字节墙）。
LARGE_REQUEST_BODY = 8 * 1024 * 1024


def _http_reason(code, detail=""):
    """冷却原因文案：`HTTP 500 <正文>`；没有正文时**不能**重复一次状态码。

    旧实现 `"HTTP %d %s" % (code, detail or "HTTP %d" % code)` 在正文读不到时会拼出
    `HTTP 413 HTTP 413`（2026-10-19 线上 503 文案里就是这个）。
    """
    text = "HTTP %d" % code
    return text + ((" " + detail) if detail else "")


def _sent_bytes(exc):
    """取 `_supplier_send` 记在异常上的「已推送请求体字节数」；没有则 0。"""
    try:
        return int(getattr(exc, "wbrelay_body_bytes", 0) or 0)
    except Exception:
        return 0


def _looks_like_oversize_abort(exc, body_bytes):
    """发送阶段的连接中断是否应归为「上游按 body 上限先拒后关」。

    只看两类证据：体量已经大到 8 MiB 量级，且确实是连接被掐断（而不是超时、DNS、
    SSL 之类）。超时/其它网络异常仍按供应商故障处理。
    """
    if body_bytes < LARGE_REQUEST_BODY:
        return False
    kinds = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
    # urllib 有时把底层异常包进 URLError.reason，两层都看
    return isinstance(exc, kinds) or isinstance(getattr(exc, "reason", None), kinds)


class _UpstreamRetryable(Exception):
    """「尚未向客户端输出」之前的上游失败 —— 可以安全切换下一个供应商。

    语义边界：只有在本对象被抛出时，调用方才允许改投下一家；
    一旦 `_sse_headers()` 已发出，任何错误都不再走这条路径。
    """


def _read_capped(resp, limit=MAX_UPSTREAM_BODY):
    """读上游响应体，超过上限抛 `_UpstreamRetryable`（多读 1 字节判定超限）。"""
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise _UpstreamRetryable("上游响应体超过上限 %d 字节" % limit)
    return data


def _parse_json(data):
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except Exception:
        return None


# 流终止事件按上游 wire protocol **分别**判定（第二轮发布修复）：
#   · Chat Completions：正常结束只认 `data: [DONE]`。
#     `chat.completion` / `chat.completion.chunk` 是 **chunk 的 object 值**，不是终止事件：
#     拿它当终止会把「普通 chunk 之后连接被截断」误判成正常结束。
#   · Responses：只认官方终止事件（completed / failed / incomplete / cancelled）。
# 一次透传里**一个终止事件都没见到**，说明上游连接被截断（客户端会收到半截回答），
# 必须记为错误 —— 否则截断看起来和正常结束一模一样。
CHAT_TERMINATORS = ("[DONE]",)
RESPONSES_TERMINATOR_TYPES = ("response.completed", "response.failed",
                              "response.incomplete", "response.cancelled")


def _sse_data_payload(line):
    """从一行 SSE 取 data 负载；不是 data 行返回 None。"""
    try:
        text = line.decode("utf-8", "replace").strip()
    except AttributeError:
        text = str(line).strip()
    if not text.startswith("data:"):
        return None
    return text[5:].strip()


def _saw_terminator(line, wire_api):
    """本行是否是「该 wire protocol 的合法终止事件」。"""
    payload = _sse_data_payload(line)
    if payload is None:
        return False
    if wire_api == "responses":
        try:
            obj = json.loads(payload)
        except Exception:
            return False
        return isinstance(obj, dict) and obj.get("type") in RESPONSES_TERMINATOR_TYPES
    return payload in CHAT_TERMINATORS


class ApiV1Mixin:
    def _v1_index(self):
        """`GET /v1`：把网关地址贴进浏览器时给出模型清单 + 端点提示。

        OpenAI 官方的 `/v1` 也是 404，但"把本机网关地址贴进浏览器看一眼"是最常见的自检动作，
        回一片 404 JSON 会让人误以为拉取不了；这里直接回模型清单，顺带列出可用端点。
        """
        names = sorted(_route_models_set(), key=_natural_key)
        return self._json(200, {
            "object": "list",
            "count": len(names),
            "data": [{"id": n, "object": "model"} for n in names],
            "endpoints": {
                "models": "/v1/models",
                "chat": "/v1/chat/completions",
                "responses": "/v1/responses",
            },
            "note": "本机 OpenAI 兼容网关；鉴权默认关闭，开启后需 Authorization: Bearer <key>",
        })

    def _v1_models(self):
        # 只返回能路由的模型：路由表并集（拉取显示范围 ∩ 组级启用）
        names = sorted(_route_models_set(), key=_natural_key)
        return self._json(200, {"object": "list",
                                "data": [{"id": n, "object": "model"} for n in names]})

    @staticmethod
    def _close_quietly(resp):
        close_quietly(resp)

    def _supplier_send(self, path, payload, supplier):
        wire = supplier.get("wire_api", "responses")
        client_is_responses = path in ("/v1/responses", "/responses")
        base = supplier["base_url"].rstrip("/")
        if wire == "responses":
            # 协议矩阵第 4 象限：客户端 chat × 上游 responses → 翻译请求体
            # （见 docs/方案-OpenCode接管修复.md §三）
            url = base + "/responses"
            body = payload if client_is_responses else chat_to_responses(payload)
        else:
            url = base + "/chat/completions"
            body = payload if not client_is_responses else responses_to_chat(payload)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": "Bearer " + supplier["api_key"],
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if payload.get("stream") else "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })
        try:
            resp = open_upstream_request(req, supplier.get("timeout_sec", 120))
        except Exception as exc:
            # 把「这次到底推了多少字节」带给上层：body 超限时上游会先拒后关，
            # 异常本身（10053/10054）看不出是拒收还是网络抖动，只有体量能佐证。
            try:
                exc.wbrelay_body_bytes = len(data)
            except Exception:
                pass
            raise
        return resp, body, len(data)

    @staticmethod
    def _upstream_error_body(exc):
        """读取上游错误正文（截断 + 脱敏后才会进日志/响应）。"""
        try:
            raw = exc.read(600)
            text = raw.decode("utf-8", "replace").strip()
        except Exception:
            text = ""
        finally:
            close_quietly(exc)
        return redact(text, 300)

    def _reject_oversize(self, supplier, model, effort, body_bytes, note, detail, t_start):
        """上游拒收本次请求体（413 / 大 body 被掐断）：不冷却、不再切换，直接回 413。

        为什么不能当供应商故障：这是对「这一份请求体」的判决，供应商本身是健康的。
        旧实现会给它打 60s 冷却，于是下一次请求连尝试都没有，客户端只看到 503，
        真实原因（会话太大）被彻底掩盖（见 docs/方案-请求侧错误语义.md §1.3）。
        """
        name = supplier.get("name") or supplier.get("id")
        try:
            mib = float(body_bytes) / (1024.0 * 1024.0)
        except Exception:
            mib = 0.0
        if body_bytes >= 1024 * 1024:
            size_note = "请求体约 %.1f MiB" % mib
        elif body_bytes:
            size_note = "请求体 %d 字节" % body_bytes
        else:
            size_note = "请求体过大"
        message = ("上游（%s）拒绝了本次请求：%s，%s。"
                   "请精简会话历史或新建会话后重试；该供应商未被标记为故障。"
                   % (name, size_note, note))
        _log("[WBRelay] supplier %s 拒收请求体（不冷却、不再切换）：%s；%s%s"
             % (supplier.get("id"), size_note, note,
                ("；上游说明：" + detail) if (detail and detail not in note) else ""))
        record_usage({"model": model, "error": True, "status": 413, "effort": effort,
                      "message": redact(message, 300),
                      "elapsed_ms": int((time.time() - t_start) * 1000),
                      "realm": "none"})
        return self._err(413, message, "invalid_request_error")

    def _supplier_dispatch(self, path, payload, first):
        model = str(payload.get("model") or "")
        want_stream = bool(payload.get("stream"))
        effort = extract_effort(payload)
        t_start = time.time()
        # 顺序 = settings.suppliers 数组顺序（供应商顺序即优先级）；冷却中的跳过
        ordered = [first] + [s for s in supplier_candidates(model) if s["id"] != first["id"]]
        last_status, last_error = 502, ""
        for s in ordered:
            sid = s["id"]
            if supplier_cooled(sid):
                continue
            try:
                resp, _sent_body, sent_bytes = self._supplier_send(path, payload, s)
            except urllib.error.HTTPError as exc:
                detail = self._upstream_error_body(exc)
                last_status = exc.code
                last_error = detail or ""
                if exc.code == 413:
                    # 请求体被上游拒收：不冷却、不再切换（换一家也是同样的体），
                    # 直接把原因交给客户端，让它去精简会话。
                    return self._reject_oversize(s, model, effort, _sent_bytes(exc),
                                                 _http_reason(413, detail), detail, t_start)
                if exc.code in REQUEST_SIDE_STATUSES:
                    # 请求本身不合规（参数/路径/语义）：供应商仍然健康，不打冷却；
                    # 仍按核心语义「失败必切换」试下一家。
                    _log("[WBRelay] supplier %s 拒绝了本次请求（请求侧错误，不冷却）：%s -> next"
                         % (sid, _http_reason(exc.code, last_error)))
                    continue
                supplier_mark_cool(sid, cooldown_429_secs() if exc.code == 429 else cooldown_secs(),
                                   reason=_http_reason(exc.code, last_error))
                _log("[WBRelay] supplier %s failed: %s -> next"
                     % (sid, _http_reason(exc.code, last_error)))
                continue
            except Exception as exc:
                body_bytes = _sent_bytes(exc)
                if getattr(exc, "wbrelay_local_connect", False):
                    # 本机 ready 队列没有在请求发送前拿到连接：这是本地
                    # TCP/队列问题，不是供应商故障，不能把它放大成 60s
                    # 供应商冷却；仍按“失败必切换”尝试下一家。
                    last_status = 503
                    last_error = redact(exc, 300)
                    _log("[WBRelay] supplier %s 本地 ready 连接不可用（不冷却）：%s -> next"
                         % (sid, last_error))
                    continue
                if _looks_like_oversize_abort(exc, body_bytes):
                    # 已经推了 8 MiB 量级的请求体才被掐断：几乎必然是上游按 body 上限
                    # 先拒后关（Windows 上表现为 10053/10054），不是供应商故障。
                    return self._reject_oversize(s, model, effort, body_bytes,
                                                 "上传请求体时连接被上游断开"
                                                 "（极可能按请求体上限拒收）",
                                                 redact(exc, 200), t_start)
                last_status = 502
                last_error = redact(exc, 300)
                supplier_mark_cool(sid, cooldown_secs(), reason=last_error)
                _log("[WBRelay] supplier %s failed: %s -> next" % (sid, last_error))
                continue
            try:
                # 注意：这里返回前可能已经把响应头发给客户端；
                # 之后的错误一律由 _supplier_output 内部消化，不再回到本循环。
                return self._supplier_output(path, payload, s, resp, sent_bytes,
                                             t_start, model, want_stream, effort)
            except _UpstreamRetryable as exc:
                # 尚未向客户端输出 → 允许切换下一个供应商
                self._close_quietly(resp)
                last_status = 502
                last_error = redact(exc, 300)
                supplier_mark_cool(sid, cooldown_secs(), reason=last_error)
                _log("[WBRelay] supplier %s failed（未开始输出，可重试）: %s -> next" % (sid, last_error))
                continue
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # 客户端主动断开：不再切换、不再写
                self._close_quietly(resp)
                return
        record_usage({"model": model, "error": True, "status": last_status,
                      "effort": effort,
                      "message": "all suppliers failed: %s" % redact(last_error, 300),
                      "elapsed_ms": int((time.time() - t_start) * 1000),
                      "realm": "supplier:all_failed"})
        return self._err(last_status, "all suppliers failed: %s" % redact(last_error, 300),
                         "server_error" if last_status >= 500 else "invalid_request_error")

    def _supplier_output(self, path, payload, supplier, resp, sent_bytes, t_start,
                         model, want_stream, effort=None):
        wire = supplier.get("wire_api", "responses")
        sid = supplier["id"]
        client_is_responses = path in ("/v1/responses", "/responses")
        try:
            if wire == "responses":
                if not client_is_responses:
                    # 协议矩阵第 4 象限：把 responses 响应/流翻译回 chat
                    if want_stream:
                        self._sse_headers()
                        self._responses_stream_to_chat(resp, model, t_start, sid, effort)
                        return
                    data = _read_capped(resp)          # 未输出 → 失败可重试
                    obj = _parse_json(data)
                    if not isinstance(obj, dict):
                        raise _UpstreamRetryable("上游返回非 JSON 响应")
                    usage = obj.get("usage")
                    record_usage(dict({"model": model, "stream": False, "realm": "supplier:" + sid,
                                       "effort": effort,
                                       "elapsed_ms": int((time.time() - t_start) * 1000)},
                                      **extract_usage_tokens(usage)))
                    return self._json(200, responses_to_chat_completion(obj, model))
                if want_stream:
                    return self._pipe_sse(resp, model, t_start, sid, effort, wire)
                data = _read_capped(resp)              # 未输出 → 失败可重试
                obj = _parse_json(data)
                if not isinstance(obj, dict):
                    raise _UpstreamRetryable("上游返回非 JSON 响应")
                usage = obj.get("usage")
                record_usage(dict({"model": model, "stream": False, "realm": "supplier:" + sid,
                                   "effort": effort,
                                   "elapsed_ms": int((time.time() - t_start) * 1000)},
                                  **extract_usage_tokens(usage)))
                return self._json(200, obj)
            # wire = chat
            if not client_is_responses:
                if want_stream:
                    return self._pipe_sse(resp, model, t_start, sid, effort, wire)
                data = _read_capped(resp)
                obj = _parse_json(data)
                if not isinstance(obj, dict):
                    raise _UpstreamRetryable("上游返回非 JSON 响应")
                usage = obj.get("usage")
                record_usage(dict({"model": model, "stream": False, "realm": "supplier:" + sid,
                                   "effort": effort,
                                   "elapsed_ms": int((time.time() - t_start) * 1000)},
                                  **extract_usage_tokens(usage)))
                return self._json(200, obj)
            # chat 上游 + 客户端要 responses：逐 chunk 翻译（真流式，不等上游结束）
            if want_stream:
                self._sse_headers()
                self._chat_stream_to_responses(resp, model, t_start, sid, effort)
                return
            chat_obj = self._aggregate_chat_stream(resp)   # 未输出 → 失败可重试
            if not chat_obj:
                raise _UpstreamRetryable("上游未返回可解析的 chat 流")
            result = chat_to_response(chat_obj, model)
            record_usage(dict({"model": model, "stream": False, "realm": "supplier:" + sid,
                               "elapsed_ms": int((time.time() - t_start) * 1000)},
                              **extract_usage_tokens(chat_obj.get("usage"))))
            return self._json(200, result)
        finally:
            self._close_quietly(resp)

    def _pipe_sse(self, resp, model, t_start, sid, effort, wire_api):
        """流式透传上游 SSE。

        **先发响应头**：此后上游出错不再切换供应商（切换会把两家内容拼给同一客户端），
        只在日志里记一条脱敏错误，并按供应商 realm 记一条 error 用量。
        """
        self._sse_headers()
        first_ms = None
        captured_usage = None
        ended = False
        error = ""
        try:
            for chunk in resp:
                if first_ms is None:
                    first_ms = int((time.time() - t_start) * 1000)
                self.wfile.write(chunk)
                self.wfile.flush()
                if not ended and _saw_terminator(chunk, wire_api):
                    ended = True
                u = extract_usage_from_sse_line(chunk)
                if u is not None:
                    captured_usage = u
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            raise                     # 客户端断开：交给 _supplier_dispatch 收尾
        except Exception as exc:
            error = redact(exc, 200)
            _log("[WBRelay] supplier %s 流式传输中断（已开始输出，不再切换供应商）：%s"
                 % (sid, error))
        if not ended and not error:
            # 连接正常结束但没见到该协议的终止事件 → 上游把流截断了
            error = ("上游 %s 流在未发送终止事件的情况下结束（连接被截断）"
                     % ("Responses" if wire_api == "responses" else "Chat"))
        extra = extract_usage_tokens(captured_usage) if captured_usage else {}
        row = dict({"model": model, "stream": True, "realm": "supplier:" + sid,
                    "effort": effort,
                    "elapsed_ms": int((time.time() - t_start) * 1000),
                    "ttft_ms": first_ms}, **extra)
        if error:
            row["error"] = True
            row["status"] = 502
            row["message"] = "stream aborted after headers: " + error
        record_usage(row)

    def _aggregate_chat_stream(self, resp):
        """把 chat SSE 收集成完整对象（非流式用）。"""
        chat_obj = {}
        try:
            for line in resp:
                if not line or line == b"\n" or line == b"\r\n":
                    continue
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue
                payload = text[5:].strip()
                if payload == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                except Exception:
                    continue
                if chunk.get("object") == "chat.completion":
                    chat_obj = chunk
                    continue
                chat_obj.setdefault("id", chunk.get("id"))
                chat_obj.setdefault("model", chunk.get("model"))
                choices = chat_obj.setdefault("choices", [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": None}])
                ch = choices[0]
                delta = chunk.get("choices") or [{}]
                d0 = delta[0].get("delta") or {} if delta else {}
                if d0.get("content"):
                    ch["message"]["content"] += d0["content"]
                if d0.get("role"):
                    ch["message"]["role"] = d0["role"]
                if d0.get("tool_calls"):
                    for tc in d0["tool_calls"]:
                        ch["message"].setdefault("tool_calls", []).append(tc)
                if delta and delta[0].get("finish_reason"):
                    ch["finish_reason"] = delta[0]["finish_reason"]
                if chunk.get("usage"):
                    chat_obj["usage"] = chunk["usage"]
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            raise
        except Exception as exc:
            # 未向客户端输出任何内容 → 可重试下一家
            raise _UpstreamRetryable("读取上游 chat 流失败：%s" % redact(exc, 200))
        return chat_obj

    def _chat_stream_to_responses(self, resp, model, t_start, sid, effort):
        """chat SSE 逐 chunk 翻译为 responses SSE（真流式，不等上游结束；上游中断则发 completed 收尾）。"""
        rid = "resp_" + os.urandom(12).hex()

        def emit(event, obj):
            self.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(obj, ensure_ascii=False)))
                             .encode("utf-8"))
            self.wfile.flush()

        emit("response.created", {"type": "response.created", "response": {
            "id": rid, "object": "response", "status": "in_progress",
            "model": model, "output": [], "usage": None}})
        first_ms = None
        text_parts = []
        usage = None
        error = ""
        done_seen = False
        try:
            for raw in resp:
                if first_ms is None:
                    first_ms = int((time.time() - t_start) * 1000)
                if not raw or raw in (b"\n", b"\r\n"):
                    continue
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done_seen = True
                    break
                try:
                    chunk = json.loads(payload)
                except Exception:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                d0 = (choices[0] or {}).get("delta") or {} if choices else {}
                t = d0.get("content")
                if t:
                    text_parts.append(t)
                    emit("response.output_text.delta", {"type": "response.output_text.delta",
                                                        "delta": t, "item_id": "it_1",
                                                        "output_index": 0, "content_index": 0})
                r = d0.get("reasoning_content")
                if r:
                    emit("response.reasoning_summary_text.delta",
                         {"type": "response.reasoning_summary_text.delta",
                          "delta": r, "item_id": "it_1", "output_index": 0, "content_index": 0})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            raise
        except Exception as exc:
            # 已经发出 response.created —— 不再切换供应商，只记录后正常收尾
            error = redact(exc, 200)
            _log("[WBRelay] supplier %s chat→responses 流式中断（已开始输出，不再切换供应商）：%s"
                 % (sid, error))
        if not done_seen and not error:
            error = "上游 chat 流未发送 [DONE] 即结束（连接被截断）"
        result = {
            "id": rid, "object": "response", "status": "completed", "model": model,
            "output": [{"id": "msg_1", "type": "message", "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "".join(text_parts),
                                     "annotations": []}]}],
            "usage": extract_usage_tokens(usage) if usage else None,
        }
        emit("response.completed", {"type": "response.completed", "response": result})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        extra = extract_usage_tokens(usage) if usage else {}
        row = dict({"model": model, "stream": True, "realm": "supplier:" + sid,
                    "effort": effort, "elapsed_ms": int((time.time() - t_start) * 1000),
                    "ttft_ms": first_ms}, **extra)
        if error:
            row["error"] = True
            row["status"] = 502
            row["message"] = "stream aborted after headers: " + error
        record_usage(row)

    def _responses_stream_to_chat(self, resp, model, t_start, sid, effort):
        """responses SSE 逐事件翻译为 chat SSE（协议矩阵第 4 象限）。

        收尾策略与 `_chat_stream_to_responses` 对齐：上游缺终止事件时仍补终块 + [DONE]，
        但用量记 `error=True` —— 截断必须在看板上看得见，而不是伪装成正常结束。
        """
        cid = "chatcmpl-" + os.urandom(8).hex()
        created = int(time.time())

        def emit(delta=None, finish=None):
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": model,
                     "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
            self.wfile.write(("data: %s\n\n" % json.dumps(chunk, ensure_ascii=False)).encode("utf-8"))
            self.wfile.flush()

        def emit_usage(usage):
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": model, "choices": [], "usage": usage}
            self.wfile.write(("data: %s\n\n" % json.dumps(chunk, ensure_ascii=False)).encode("utf-8"))
            self.wfile.flush()

        first_ms = None
        usage = None
        error = ""
        finished = False
        role_sent = False
        tool_index = {}
        tool_calls = 0
        sent_args = set()
        try:
            for raw in resp:
                if first_ms is None:
                    first_ms = int((time.time() - t_start) * 1000)
                if not raw or raw in (b"\n", b"\r\n"):
                    continue
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    evt = json.loads(payload)
                except Exception:
                    continue
                if not isinstance(evt, dict):
                    continue
                etype = evt.get("type")
                if etype in ("response.created", "response.in_progress"):
                    continue
                if not role_sent:
                    role_sent = True
                    emit({"role": "assistant", "content": ""})
                if etype == "response.output_item.added":
                    item = evt.get("item") if isinstance(evt.get("item"), dict) else {}
                    if item.get("type") == "function_call":
                        idx = tool_calls
                        tool_calls += 1
                        key = str(item.get("id") or item.get("call_id") or idx)
                        tool_index[key] = idx
                        # 两个 id 都登记：上游在不同事件里可能用 id 或 call_id 指代同一个调用
                        if item.get("call_id"):
                            tool_index[str(item["call_id"])] = idx
                        if item.get("id"):
                            tool_index[str(item["id"])] = idx
                        # 上游可能**一次性**把参数给全（不发 ...arguments.delta）——
                        # 早先这里无条件写 ""，会让这类上游（例：space-bunny 流式）参数整段丢失。
                        initial = _args_json(item.get("arguments"))
                        emit({"tool_calls": [{
                            "index": idx,
                            "id": str(item.get("call_id") or item.get("id") or ""),
                            "type": "function",
                            "function": {"name": str(item.get("name") or ""),
                                         "arguments": initial}}]})
                        if initial:
                            sent_args.add(idx)
                    continue
                if etype == "response.output_text.delta":
                    delta = evt.get("delta")
                    if delta:
                        emit({"content": str(delta)})
                    continue
                if etype in ("response.reasoning_summary_text.delta",
                             "response.reasoning_text.delta"):
                    delta = evt.get("delta")
                    if delta:
                        emit({"reasoning_content": str(delta)})
                    continue
                if etype == "response.output_item.done":
                    item = evt.get("item") if isinstance(evt.get("item"), dict) else {}
                    if item.get("type") == "function_call":
                        key = str(item.get("id") or item.get("call_id") or "")
                        idx = tool_index.get(key)
                        if idx is not None and idx not in sent_args:
                            args = _args_json(item.get("arguments"))
                            if args:
                                sent_args.add(idx)
                                emit({"tool_calls": [{"index": idx,
                                                      "function": {"arguments": args}}]})
                    continue
                if etype == "response.function_call_arguments.delta":
                    idx = tool_index.get(str(evt.get("item_id") or ""), 0)
                    delta = str(evt.get("delta") or "")
                    if delta:
                        sent_args.add(idx)
                        emit({"tool_calls": [{"index": idx,
                                              "function": {"arguments": delta}}]})
                    continue
                if etype == "response.completed":
                    resp_obj = evt.get("response") if isinstance(evt.get("response"), dict) else {}
                    usage = resp_obj.get("usage") or evt.get("usage")
                    finished = True
                    continue
                if etype in ("response.failed", "response.incomplete", "response.cancelled",
                             "response.error", "error"):
                    detail = evt.get("error") or evt.get("message")
                    if detail is None:
                        resp_obj = evt.get("response") if isinstance(evt.get("response"), dict) else {}
                        detail = resp_obj.get("error")
                    error = (redact(detail, 200) if isinstance(detail, str)
                             else redact(json.dumps(detail, ensure_ascii=False), 200)) \
                        if detail else etype
                    finished = True
                    continue
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            raise
        except Exception as exc:
            # 已经发出响应头 —— 不再切换供应商，只记录后收尾
            error = redact(exc, 200)
            _log("[WBRelay] supplier %s responses→chat 流式中断（已开始输出，不再切换供应商）：%s"
                 % (sid, error))
        if not finished and not error:
            error = "上游 responses 流未发送终止事件即结束（连接被截断）"
        emit({}, "tool_calls" if tool_calls else "stop")
        chat_usage = usage_to_chat(usage) if usage else None
        if chat_usage:
            emit_usage(chat_usage)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        extra = extract_usage_tokens(usage) if usage else {}
        row = dict({"model": model, "stream": True, "realm": "supplier:" + sid,
                    "effort": effort, "elapsed_ms": int((time.time() - t_start) * 1000),
                    "ttft_ms": first_ms}, **extra)
        if error:
            row["error"] = True
            row["status"] = 502
            row["message"] = "stream aborted after headers: " + error
        record_usage(row)

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    # ---- 路由入口 ----
    def _dispatch(self, path, payload):
        model, err = check_model_name(payload.get("model"))
        if err:
            return self._err(400, err, "invalid_request_error")
        if not model:
            return self._err(400, "model 不能为空", "invalid_request_error")
        effort = extract_effort(payload)
        if not route_has_model(model):
            record_usage({"model": model, "error": True, "status": 503,
                          "effort": effort,
                          "message": "model '%s' 未启用或无供应商可路由" % model,
                          "realm": "none"})
            return self._err(503, "model '%s' 未启用 - 请在看板模型配置中启用" % model,
                             "server_error")
        first = pick_supplier(model)
        if first is None:
            cooling = cooling_suppliers_for(model)
            if cooling:
                # 供应商是启用的，只是刚失败过正在冷却 —— 必须与「没启用」区分开
                detail = []
                for c in cooling[:3]:
                    tip = "，最近失败：%s" % c["reason"] if c.get("reason") else ""
                    detail.append("%s（剩余 %ds%s）" % (c["name"], c["remaining"], tip))
                if len(cooling) > 3:
                    detail.append("等 %d 家" % (len(cooling) - 3))
                msg = ("no usable supplier for model '%s' - 供应商都在冷却：%s"
                       % (model, "；".join(detail)))
                record_usage({"model": model, "error": True, "status": 503,
                              "effort": effort,
                              "message": "all suppliers cooling for model '%s'" % model,
                              "realm": "none"})
            else:
                msg = ("no usable supplier for model '%s' - 请在看板检查供应商是否启用" % model)
                record_usage({"model": model, "error": True, "status": 503,
                              "effort": effort,
                              "message": "no usable supplier for model '%s'" % model,
                              "realm": "none"})
            return self._err(503, msg, "server_error")
        return self._supplier_dispatch(path, payload, first)