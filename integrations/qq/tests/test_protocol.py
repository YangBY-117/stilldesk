import pytest

from qq_bridge.protocol import MAX_BLOB, WireError, decode_message, wire_fields
from conftest import body, field


def test_text_and_mixed_placeholder_do_not_expose_unknown_transport_bytes():
    blob = body("中文消息：明天开会") + field(40800, field(45002, 2)
               + field(45802, "PRIVATE_TOKEN_DO_NOT_RETURN") + field(45812, "pic.png"))
    result = decode_message(blob)
    assert "明天开会" in result.content
    assert "[图片]" in result.content
    assert result.type == "mixed"
    assert result.decode_status == "partial_unsupported"
    assert result.unsupported_fields == (45802,)
    assert result.image_paths == ("pic.png",)
    assert "PRIVATE_TOKEN" not in result.content


@pytest.mark.parametrize("blob", [b"\x80", b"\x00", b"\x0a\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff",
                                  b"\x0b", b"\x09short", b"\x0a\x05xx", b"x" * (MAX_BLOB + 1)],
                         ids=["varint", "zero-field", "overflow", "group-wire", "fixed", "length", "oversized"])
def test_malformed_wire_is_bounded_and_safe(blob):
    assert decode_message(blob).decode_status in {"malformed_protobuf", "unsupported_structure"}
    with pytest.raises(WireError):
        wire_fields(blob)


def test_application_json_and_reply_are_not_rendered_as_authority():
    blob = field(40800, field(45002, 10) + field(47901, "PRIVATE_TOKEN executable cmd"))
    result = decode_message(blob)
    assert result.content == "[应用消息]"
    assert result.decode_status == "partial_unsupported"
    assert "PRIVATE_TOKEN" not in result.content


def test_unknown_root_and_nonbytes_are_explicitly_unsupported():
    result = decode_message(field(9999, "PRIVATE_ROOT_TOKEN"))
    assert result.decode_status == "unsupported_structure"
    assert result.unsupported_fields == (9999,)
    assert "PRIVATE_ROOT_TOKEN" not in result.content
    assert decode_message("plaintext not protobuf").decode_status == "unsupported_format"


def test_wrong_known_text_wire_and_invalid_utf8_are_not_leaked():
    for value in (123, b"\xff\xfe"):
        result = decode_message(field(40800, field(45002, 1) + field(45101, value)))
        assert result.decode_status == "malformed_protobuf"


def test_empty_and_single_segment_are_supported():
    assert decode_message(b"").decode_status == "empty"
    assert decode_message(field(45002, 1) + field(45101, "直接存储")).content == "直接存储"
