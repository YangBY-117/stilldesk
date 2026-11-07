"""Candidate exclusion needs affirmative non-file evidence, not parse failure."""
import json

import pytest
import zstandard

from wechat_bridge.attachments import (
    AttachmentUnavailable, MAX_XML_BYTES, classify_attachment_candidate, parse_attachment,
)


def file_card(name="report.pptx", size="8", digest="a" * 32):
    return (f"<msg><appmsg><type>6</type><title>{name}</title><md5>{digest}</md5>"
            f"<appattach><totallen>{size}</totallen><fileext>pptx</fileext>"
            "<cdnattachurl>PRIVATE_CDN_SENTINEL</cdnattachurl>"
            "<aeskey>PRIVATE_AES_SENTINEL</aeskey></appattach></appmsg></msg>")


def non_file(subtype=57, nested=""):
    return (f"<msg><appmsg><type>{subtype}</type><title>Regarding report.pptx</title>"
            f"<refermsg><content>{nested}</content></refermsg><appattach/>"
            "</appmsg></msg>")


def test_actual_shape_compressed_quote_with_empty_and_opaque_auxiliary():
    raw = zstandard.ZstdCompressor().compress(non_file().encode())
    result = classify_attachment_candidate(raw, "", b"\x08\x96\xff\x00\x80\x01\x00")
    assert result.kind == "known_non_file"
    assert result.subtype == 57
    assert result.attachment is None
    assert parse_attachment(raw, "", b"\xff") is None


@pytest.mark.parametrize("packed", [
    b"\x08\x96<\xff\x00\x80",
    b"\x08\x02<\x00",
    b"\x08<msg>\xff",
    b"\xff<msg>\x80",
])
def test_incidental_angle_bracket_in_packed_auxiliary_does_not_block_quote(packed):
    result = classify_attachment_candidate(non_file(), packed)
    assert result.kind == "known_non_file" and result.subtype == 57
    assert classify_attachment_candidate(packed).kind == "unknown"


@pytest.mark.parametrize("raw", [
    b"<msg>\xff",
    b"\xef\xbb\xbf<msg>\xff",
    b"wxid_x:\n<msg>\xff",
    b" \twxid_x:\r\n  <msg>\xff",
    b"human prose before <msg>\xff",
    "human prose before <msg><appmsg><type>6</type></appmsg></msg>",
    zstandard.ZstdCompressor().compress(b"\x08\x02<\x00"),
])
def test_xml_shaped_or_compressed_damage_remains_unknown(raw):
    result = classify_attachment_candidate(non_file(), raw)
    assert result.kind == "unknown"
    assert result.reason != "opaque_auxiliary"


@pytest.mark.parametrize("subtype", [5, 57])
@pytest.mark.parametrize("standalone", [False, True])
def test_known_non_file_types_are_explicit_and_outer_only(subtype, standalone):
    raw = non_file(subtype)
    if standalone:
        raw = raw.removeprefix("<msg>").removesuffix("</msg>")
    result = classify_attachment_candidate(raw)
    assert result.kind == "known_non_file" and result.subtype == subtype


@pytest.mark.parametrize("nested", [file_card(), "&lt;appmsg&gt;&lt;type&gt;6&lt;/type&gt;&lt;/appmsg&gt;"])
def test_quote_embedded_file_is_not_an_outer_file(nested):
    result = classify_attachment_candidate(non_file(nested=nested))
    assert result.kind == "known_non_file" and result.subtype == 57
    assert parse_attachment(non_file(nested=nested)) is None


def test_file_fallback_and_credentials_remain_private():
    raw = zstandard.ZstdCompressor().compress(file_card().encode())
    info = parse_attachment(b"invalid", raw)
    result = classify_attachment_candidate(b"invalid", raw)
    assert result.kind == "file" and result.subtype == 6
    assert result.attachment == info
    assert info.name == "report.pptx"
    assert "PRIVATE" not in repr(result)
    assert "PRIVATE" not in json.dumps(info.public())


def test_consistent_duplicate_representations_remain_usable():
    result = classify_attachment_candidate(file_card(), file_card())
    assert result.kind == "file"
    assert classify_attachment_candidate(non_file(), non_file()).kind == "known_non_file"


@pytest.mark.parametrize("other", [file_card(), non_file(5)])
def test_conflicting_raw_outer_types_are_unknown(other):
    result = classify_attachment_candidate(non_file(), other)
    assert result.kind == "unknown"
    assert result.reason == "conflicting_outer_types"


@pytest.mark.parametrize("other", [
    file_card("other.pptx"), file_card(size="9"), file_card(digest="b" * 32),
])
def test_conflicting_file_metadata_is_unknown(other):
    assert classify_attachment_candidate(file_card(), other).kind == "unknown"


@pytest.mark.parametrize("other", [
    "<msg><appmsg><type>6</type>",
    "prose before <msg><appmsg><type>6</type></appmsg></msg>",
    '<!DOCTYPE msg [<!ENTITY x "PRIVATE_ENTITY">]><msg/>',
    b"<msg>\xff</msg>",
    b"\x28\xb5\x2f\xfd",
    zstandard.ZstdCompressor().compress(file_card().encode())[:-1],
    zstandard.ZstdCompressor().compress(file_card().encode()) + b"trailing-data",
])
def test_unreadable_structured_alternative_does_not_allow_quote_exclusion(other):
    result = classify_attachment_candidate(non_file(), other)
    assert result.kind == "unknown"
    assert result.attachment is None
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize("raw", [
    "<msg><appmsg><type>57</type><type>6</type></appmsg></msg>",
    "<msg><appmsg><type>57</type><type>57</type></appmsg></msg>",
    "<msg><appmsg><type>57</type></appmsg><appmsg><type>6</type></appmsg></msg>",
    "<msg><appmsg><type><nested>57</nested></type></appmsg></msg>",
    '<msg xmlns="unverified"><appmsg><type>57</type></appmsg></msg>',
    "<wrapper><appmsg><type>57</type></appmsg></wrapper>",
    "<msg><refermsg><appmsg><type>6</type></appmsg></refermsg></msg>",
    "<msg><appmsg><type>57x</type></appmsg></msg>",
])
def test_ambiguous_or_unknown_outer_structure_is_not_non_file(raw):
    assert classify_attachment_candidate(raw).kind == "unknown"
    assert parse_attachment(raw) is None


@pytest.mark.parametrize("subtype", [0, 19, 999999])
def test_future_or_unverified_card_subtype_is_unknown(subtype):
    result = classify_attachment_candidate(non_file(subtype))
    assert result.kind == "unknown" and result.subtype == subtype
    assert result.reason == "unsupported_outer_type"


@pytest.mark.parametrize("compress", [False, True])
def test_oversized_alternative_is_unknown(compress):
    raw = b"<msg>" + b"x" * MAX_XML_BYTES + b"</msg>"
    if compress:
        raw = zstandard.ZstdCompressor().compress(raw)
    result = classify_attachment_candidate(non_file(), raw)
    assert result.kind == "unknown" and result.reason == "representation_too_large"


def test_unknown_content_size_zstd_is_bounded_and_complete():
    raw = zstandard.ZstdCompressor(write_content_size=False).compress(non_file().encode())
    assert classify_attachment_candidate(raw).kind == "known_non_file"
    assert classify_attachment_candidate(raw[:-1]).kind == "unknown"


def test_invalid_file_metadata_preserves_old_parser_exception_and_safe_classification():
    raw = file_card(size="-1")
    with pytest.raises(AttachmentUnavailable) as error:
        parse_attachment(raw)
    assert error.value.code == "invalid_attachment_metadata"
    result = classify_attachment_candidate(raw)
    assert result.kind == "unknown" and result.subtype == 6
    assert result.reason == "invalid_attachment_metadata"


def test_group_sender_prefix_is_shared_and_bounded():
    raw = "wxid_test:\n" + non_file()
    assert classify_attachment_candidate(raw).kind == "known_non_file"
    assert classify_attachment_candidate("x" * 257 + ":\n" + non_file()).kind == "unknown"


@pytest.mark.parametrize("raw", [None, "", b"", "plain auxiliary", b"\xff\x80\x01"])
def test_auxiliary_data_alone_is_never_non_file_evidence(raw):
    assert classify_attachment_candidate(raw).kind == "unknown"
