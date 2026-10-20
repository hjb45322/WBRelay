# -*- coding: utf-8 -*-
"""HTTP 请求体帧解析：Content-Length / chunked 与明确的失败语义。

设计边界：
  · 本模块只依赖标准库，不读配置、不写日志、不接触网络；
  · 调用方负责把 PayloadError 映射成 HTTP 响应；
  · 失败时只携带「已读正文字节数」，绝不携带请求正文本身。
"""
import json


# chunk size 行 / trailer 行上限（含 CRLF）
MAX_CHUNK_LINE = 8192
# 零 chunk 后的 trailer 总量上限
MAX_TRAILER_BYTES = 8192
# chunk size 只允许有限位数，避免超长十六进制串消耗 CPU
MAX_CHUNK_SIZE_DIGITS = 16


class PayloadError(Exception):
    """请求体读取或解析失败，携带可直接映射为 HTTP 的语义。"""

    def __init__(self, status, message, bytes_read=0):
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)
        self.bytes_read = int(bytes_read)


def _headers_get_all(headers, name):
    values = []
    get_all = getattr(headers, "get_all", None)
    if get_all is not None:
        values = list(get_all(name) or [])
    else:
        value = headers.get(name)
        if value is not None:
            values = [value]
    return [str(v) for v in values]


def _parse_framing(headers):
    """返回 ('length', n) 或 ('chunked', None)；歧义/缺失直接抛 PayloadError。"""
    content_lengths = _headers_get_all(headers, "Content-Length")
    transfer_encodings = _headers_get_all(headers, "Transfer-Encoding")

    if content_lengths and transfer_encodings:
        raise PayloadError(400, "invalid request framing")
    if len(content_lengths) > 1:
        raise PayloadError(400, "invalid request framing")

    if content_lengths:
        text = content_lengths[0].strip()
        # 拒绝 +1 / 1,1 / 0x10 等非十进制写法，避免不同层对长度理解不一致
        if not text or not text.isdigit():
            raise PayloadError(400, "invalid request framing")
        return "length", int(text, 10)

    if transfer_encodings:
        if len(transfer_encodings) != 1:
            raise PayloadError(400, "invalid request framing")
        text = transfer_encodings[0].strip().lower()
        if text != "chunked":
            raise PayloadError(400, "invalid request framing")
        return "chunked", None

    raise PayloadError(411, "length required")


def _read_exact(stream, size, already_read=0):
    """严格读取 size 字节；EOF 提前出现时报 incomplete request body。"""
    parts = []
    remaining = int(size)
    actual = int(already_read)
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise PayloadError(400, "incomplete request body", bytes_read=actual)
        parts.append(chunk)
        got = len(chunk)
        remaining -= got
        actual += got
    return b"".join(parts), actual


def _read_crlf_line(stream, bytes_read):
    """读一条带 CRLF 的行；超长、无 CRLF 或 EOF 都视为 chunked 格式错误。"""
    line = stream.readline(MAX_CHUNK_LINE + 1)
    if not line:
        raise PayloadError(400, "incomplete request body", bytes_read=bytes_read)
    if len(line) > MAX_CHUNK_LINE or not line.endswith(b"\r\n"):
        raise PayloadError(400, "invalid chunked body", bytes_read=bytes_read)
    return line


def _read_trailers(stream, bytes_read):
    """读取零 chunk 后的 trailer，直到空行；总量有上限。"""
    trailer_bytes = 0
    while True:
        line = _read_crlf_line(stream, bytes_read)
        trailer_bytes += len(line)
        if trailer_bytes > MAX_TRAILER_BYTES:
            raise PayloadError(400, "invalid chunked body", bytes_read=bytes_read)
        if line == b"\r\n":
            return


def _read_chunked(stream, limit):
    parts = []
    total = 0
    while True:
        line = _read_crlf_line(stream, total)
        size_text = line[:-2].split(b";", 1)[0].strip()
        if (not size_text or len(size_text) > MAX_CHUNK_SIZE_DIGITS
                or any(c not in b"0123456789abcdefABCDEF" for c in size_text)):
            raise PayloadError(400, "invalid chunked body", bytes_read=total)
        size = int(size_text, 16)

        if total + size > limit:
            # 声明值就超限：不再读取 chunk 数据，立即让调用方关闭连接
            raise PayloadError(413, "payload too large", bytes_read=total)
        if size == 0:
            _read_trailers(stream, total)
            return b"".join(parts)

        data, total = _read_exact(stream, size, total)
        parts.append(data)
        ending = stream.read(2)
        if ending != b"\r\n":
            raise PayloadError(400, "invalid chunked body", bytes_read=total)


def _read_content_length(stream, length, limit):
    if length > limit:
        # 先拒后读：不把超大请求体拉进内存，也不尝试 drain
        raise PayloadError(413, "payload too large", bytes_read=0)
    raw, _actual = _read_exact(stream, length, 0)
    return raw


def read_json_payload(headers, stream, limit):
    """读取并解析 UTF-8 JSON 请求体。

    Content-Length: 0 或零长度 chunked 返回空 dict，保留现有「可选 JSON 正文」
    API 的调用习惯；真正的 JSON 语法错误仍返回 invalid json。
    """
    limit = int(limit)
    if limit <= 0:
        raise ValueError("limit must be positive")

    framing, length = _parse_framing(headers)
    if framing == "chunked":
        raw = _read_chunked(stream, limit)
    else:
        raw = _read_content_length(stream, length, limit)

    if not raw:
        return {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PayloadError(400, "invalid utf-8", bytes_read=len(raw))
    try:
        return json.loads(text)
    except Exception:
        raise PayloadError(400, "invalid json", bytes_read=len(raw))