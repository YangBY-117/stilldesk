"""Small bounded protobuf-wire reader for QQNT message bodies.

Field names were checked against the MIT-licensed message.proto at
artiga033/ntdb_unwrap b1c5420a1957a833787b57f616243d4fa120526d.
This module independently implements only wire parsing and selected display
fields; it does not copy generated code or expose transport metadata.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_BLOB = 2_000_000
MAX_FIELDS = 4096
MAX_SEGMENTS = 512
MAX_TEXT = 200_000


class WireError(ValueError):
    """Malformed, unsupported, or oversized wire input."""


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise WireError("truncated varint")
        byte = data[pos]
        pos += 1
        if shift == 63 and byte > 1:
            raise WireError("varint overflow")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise WireError("varint overflow")


def wire_fields(data: bytes) -> dict[int, list[int | bytes]]:
    """Parse one message with strict length, count, field, and wire bounds."""
    if not isinstance(data, bytes) or len(data) > MAX_BLOB:
        raise WireError("invalid message size")
    result: dict[int, list[int | bytes]] = {}
    pos = 0
    count = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        field, wire = tag >> 3, tag & 7
        count += 1
        if not 0 < field < (1 << 29) or count > MAX_FIELDS:
            raise WireError("invalid field")
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire in (1, 5):
            length = 8 if wire == 1 else 4
            if pos + length > len(data):
                raise WireError("truncated fixed field")
            value = data[pos:pos + length]
            pos += length
        elif wire == 2:
            length, pos = _varint(data, pos)
            if length > MAX_BLOB or pos + length > len(data):
                raise WireError("truncated length field")
            value = data[pos:pos + length]
            pos += length
        else:
            raise WireError("unsupported wire type")
        result.setdefault(field, []).append(value)
    return result


def _text(fields: dict, field: int, limit: int = MAX_TEXT) -> str:
    values = fields.get(field, [])
    if not values:
        return ""
    value = values[0]
    if not isinstance(value, bytes):
        raise WireError("wrong text wire")
    try:
        text = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise WireError("invalid text") from error
    if len(text) > limit:
        raise WireError("oversized text")
    # Do not reinterpret chat text as terminal control sequences.
    return "".join(char for char in text if char >= " " or char in "\n\t").strip()


@dataclass(frozen=True)
class DecodedMessage:
    content: str
    type: str
    decode_status: str
    image_paths: tuple[str, ...] = ()
    unsupported_fields: tuple[int, ...] = ()


_KNOWN_SEGMENT_FIELDS = {45001, 45002, 45101, 45402, 45405, 45812,
                         47601, 47602, 47423, 40020, 40021}


def decode_message(value: object) -> DecodedMessage:
    """Render selected fields, with placeholders for every unhandled segment.

    Unknown bytes, image URLs, application JSON/XML, CDN tokens, and arbitrary
    binary fields never become message text or image-file candidates.
    """
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return DecodedMessage("[QQ 消息：内容格式未支持]", "unknown", "unsupported_format")
    try:
        data = bytes(value)
        root = wire_fields(data)
        if 40800 in root:
            segments = root[40800]
            unsupported = set(root) - {40800}
        elif 45002 in root:
            # Some clients store a single segment without the outer wrapper.
            segments = [data]
            unsupported = set()
        elif not root:
            return DecodedMessage("[QQ 消息：空正文]", "unknown", "empty")
        else:
            return DecodedMessage("[QQ 消息：正文结构未支持]", "unknown",
                                  "unsupported_structure", unsupported_fields=tuple(sorted(root)))
        if len(segments) > MAX_SEGMENTS:
            raise WireError("too many segments")
        parts: list[str] = []
        kinds: set[str] = set()
        images: list[str] = []
        partial = bool(unsupported)
        for segment in segments:
            if not isinstance(segment, bytes):
                raise WireError("wrong segment wire")
            fields = wire_fields(segment)
            types = fields.get(45002, [])
            if len(types) != 1 or not isinstance(types[0], int):
                raise WireError("missing segment type")
            kind = types[0]
            unsupported.update(set(fields) - _KNOWN_SEGMENT_FIELDS)
            if kind == 1:
                parts.append(_text(fields, 45101))
                kinds.add("text")
            elif kind == 2:
                parts.append("[图片]")
                kinds.add("image")
                path = _text(fields, 45812, 4096)
                if path:
                    images.append(path)
            elif kind == 3:
                name = _text(fields, 45402, 512)
                parts.append("[文件]" + (" " + name if name else ""))
                kinds.add("file")
            elif kind == 6:
                text = _text(fields, 47602, 512)
                parts.append("[表情]" + (" " + text if text else ""))
                kinds.add("emoji")
            elif kind == 7:
                parts.append("[回复：引用正文未解析]")
                kinds.add("reply")
                partial = True
            else:
                label = {8: "提示", 10: "应用消息", 21: "通话", 26: "动态消息"}.get(kind)
                parts.append(f"[{label or 'QQ 未支持消息类型 ' + str(kind)}]")
                kinds.add("other")
                partial = True
            if sum(map(len, parts)) > MAX_TEXT:
                raise WireError("oversized rendered message")
        content = "\n".join(part for part in parts if part) or "[QQ 消息：空正文]"
        status = "partial_unsupported" if partial or unsupported else "decoded"
        return DecodedMessage(content, next(iter(kinds)) if len(kinds) == 1 else "mixed",
                              status, tuple(dict.fromkeys(images)), tuple(sorted(unsupported)))
    except (WireError, TypeError, ValueError, OverflowError):
        return DecodedMessage("[QQ 消息：内容无法解码]", "unknown", "malformed_protobuf")
