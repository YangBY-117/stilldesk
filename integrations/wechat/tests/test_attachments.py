"""Synthetic attachment tests; no user's files or message data are read."""
import hashlib
import json
from pathlib import Path

import pytest
import zstandard

from wechat_bridge.attachments import (
    AttachmentInfo, AttachmentUnavailable, LocalAttachments, parse_attachment,
)


def card(name="report.pdf", data=b"document", *, md5=True, size=True):
    digest = f"<md5>{hashlib.md5(data).hexdigest()}</md5>" if md5 else ""
    length = f"<totallen>{len(data)}</totallen>" if size else ""
    return (f"<msg><appmsg><title>{name}</title><type>6</type>{digest}<appattach>"
            f"{length}<fileext>pdf</fileext><cdnattachurl>PRIVATE_CDN_TOKEN</cdnattachurl>"
            "<aeskey>PRIVATE_AES_KEY</aeskey></appattach></appmsg></msg>")


def setup(tmp_path):
    account = tmp_path / "account"
    state = tmp_path / "state"
    state.mkdir()
    (account / "msg/file/2026-10").mkdir(parents=True)
    return account, state, LocalAttachments(account, state)


def write_file(account, month, name, data):
    directory = account / "msg/file" / month
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(data)
    return target


def test_parse_compressed_card_and_keep_credentials_private():
    info = parse_attachment(b"invalid", zstandard.ZstdCompressor().compress(card().encode()))
    assert info.name == "report.pdf"
    assert info.size == 8
    assert info.private_fields["aeskey"] == "PRIVATE_AES_KEY"
    assert info.public()["transport_available"] is True
    assert "PRIVATE" not in repr(info)
    assert "PRIVATE" not in json.dumps(info.public())
    assert parse_attachment("<msg><appmsg><type>5</type></appmsg></msg>") is None
    assert parse_attachment('<!DOCTYPE msg [<!ENTITY x "secret">]><msg/>') is None


@pytest.mark.parametrize("sender", ["wxid_synthetic", "buptxf", "account-with.dots_2"])
def test_group_sender_prefix_is_bounded_and_parsed(sender):
    raw = sender + ":\n<?xml version='1.0'?>" + card()
    assert parse_attachment(raw).name == "report.pdf"
    assert parse_attachment(zstandard.ZstdCompressor().compress(raw.encode())).size == 8


def test_group_prefix_does_not_enable_arbitrary_prose_or_entities():
    assert parse_attachment("x" * 257 + ":\n" + card()) is None
    assert parse_attachment("arbitrary preceding prose:\n" + card()) is None
    assert parse_attachment("wxid_x:\nplain text " + card()) is None
    assert parse_attachment('wxid_x:\n<!DOCTYPE msg [<!ENTITY x "secret">]>' + card()) is None


@pytest.mark.parametrize("name", ["../secret.pdf", r"..\secret.pdf", "C:secret.pdf", "NUL.pdf", ".."])
def test_reject_pathlike_or_reserved_filenames(name):
    with pytest.raises(AttachmentUnavailable) as caught:
        parse_attachment(card(name))
    assert caught.value.code == "invalid_attachment_metadata"


def test_validated_export_preserves_source_and_is_idempotent(tmp_path):
    account, state, files = setup(tmp_path)
    data = b"original document bytes"
    source = write_file(account, "2026-10", "report.pdf", data)
    write_file(account, "2026-09", "report.pdf", b"unrelated document")
    info = parse_attachment(card(data=data))
    result = files.get({"id": "message/message_0.db/Msg_a/7"}, info)
    assert Path(result["path"]).is_relative_to(state / "downloads")
    assert Path(result["path"]).read_bytes() == source.read_bytes() == data
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["already_exported"] is False
    assert files.get({"id": "message/message_0.db/Msg_a/7"}, info)["already_exported"] is True
    assert not list((state / "downloads").rglob(".part-*"))
    assert "PRIVATE" not in json.dumps(result)


def test_same_name_same_size_wrong_hash_is_not_accepted(tmp_path):
    account, _, files = setup(tmp_path)
    write_file(account, "2026-10", "report.pdf", b"wrong!!!")
    with pytest.raises(AttachmentUnavailable) as caught:
        files.locate({"id": "7"}, parse_attachment(card(data=b"document")))
    assert caught.value.code == "attachment_not_cached"


def test_without_md5_different_same_size_candidates_are_ambiguous(tmp_path):
    account, _, files = setup(tmp_path)
    write_file(account, "2026-10", "report.pdf", b"document")
    write_file(account, "2026-09", "report.pdf", b"wrong!!!")
    with pytest.raises(AttachmentUnavailable) as caught:
        files.locate({"id": "7"}, parse_attachment(card(md5=False)))
    assert caught.value.code == "attachment_ambiguous"


def test_filename_alone_is_insufficient(tmp_path):
    account, _, files = setup(tmp_path)
    write_file(account, "2026-10", "report.pdf", b"document")
    with pytest.raises(AttachmentUnavailable) as caught:
        files.locate({"id": "7"}, parse_attachment(card(md5=False, size=False)))
    assert caught.value.code == "attachment_identity_unavailable"


def test_only_month_directories_are_eligible(tmp_path):
    account, _, files = setup(tmp_path)
    write_file(account, "other", "report.pdf", b"document")
    with pytest.raises(AttachmentUnavailable) as caught:
        files.locate({"id": "7"}, parse_attachment(card()))
    assert caught.value.code == "attachment_not_cached"


def test_reparse_candidate_is_rejected_without_reading_target(tmp_path, monkeypatch):
    from wechat_bridge import attachments
    account, _, files = setup(tmp_path)
    source = write_file(account, "2026-10", "report.pdf", b"document")
    original = attachments._is_reparse
    monkeypatch.setattr(attachments, "_is_reparse", lambda path: path == source or original(path))
    with pytest.raises(AttachmentUnavailable) as caught:
        files.locate({"id": "7"}, parse_attachment(card()))
    assert caught.value.code == "unsafe_attachment_path"


def test_conflicting_export_is_preserved(tmp_path):
    account, state, files = setup(tmp_path)
    write_file(account, "2026-10", "report.pdf", b"document")
    folder = state / "downloads" / hashlib.sha256(b"7").hexdigest()
    folder.mkdir(parents=True)
    destination = folder / "report.pdf"
    destination.write_bytes(b"do not overwrite")
    with pytest.raises(AttachmentUnavailable) as caught:
        files.get({"id": "7"}, parse_attachment(card()))
    assert caught.value.code == "attachment_export_conflict"
    assert destination.read_bytes() == b"do not overwrite"
    assert not list(folder.glob(".part-*"))


def test_oversize_or_invalid_metadata_is_rejected():
    with pytest.raises(AttachmentUnavailable) as caught:
        parse_attachment(card().replace("<totallen>8</totallen>", "<totallen>999999999</totallen>"))
    assert caught.value.code == "attachment_too_large"
    with pytest.raises(AttachmentUnavailable):
        parse_attachment(card().replace("<totallen>8</totallen>", "<totallen>-1</totallen>"))


def test_compressed_xml_bomb_is_bounded():
    raw = b"<msg>" + b"x" * 2_000_001 + b"</msg>"
    assert parse_attachment(zstandard.ZstdCompressor().compress(raw)) is None


def test_same_size_mutation_after_selection_never_publishes(tmp_path, monkeypatch):
    from wechat_bridge import attachments
    account, state, files = setup(tmp_path)
    path = write_file(account, "2026-10", "report.pdf", b"document")
    original_open = attachments._open_read
    calls = 0

    def changed_before_copy(candidate, boundary):
        nonlocal calls
        if candidate == path:
            calls += 1
            if calls == 2:
                path.write_bytes(b"changed!")
        return original_open(candidate, boundary)

    monkeypatch.setattr(attachments, "_open_read", changed_before_copy)
    with pytest.raises(AttachmentUnavailable) as caught:
        files.get({"id": "7"}, parse_attachment(card(md5=False)))
    assert caught.value.code == "attachment_source_changed"
    assert not list((state / "downloads").rglob("report.pdf"))
    assert not list((state / "downloads").rglob(".part-*"))


def test_identical_copies_are_not_ambiguous(tmp_path):
    account, _, files = setup(tmp_path)
    write_file(account, "2026-10", "report.pdf", b"document")
    write_file(account, "2026-09", "report.pdf", b"document")
    assert files.locate({"id": "7"}, parse_attachment(card(md5=False))).is_file()


def staged_file(state, data=b"document"):
    folder = state / "download-staging" / ("a" * 32)
    folder.mkdir(parents=True)
    path = folder / "report.pdf"
    path.write_bytes(data)
    return path


def test_import_download_verifies_and_preserves_staging_with_repeat(tmp_path):
    _, state, files = setup(tmp_path)
    path = staged_file(state)
    info = parse_attachment(card())
    first = files.import_download({"id": "7"}, info, path)
    second = files.import_download({"id": "7"}, info, path)
    assert Path(first["path"]).read_bytes() == path.read_bytes() == b"document"
    assert first["source"] == "verified_client_download"
    assert first["already_exported"] is False
    assert second["already_exported"] is True
    assert "PRIVATE" not in json.dumps([first, second])


@pytest.mark.parametrize("data", [b"partial", b"wrong!!!"])
def test_import_rejects_partial_and_wrong_hash_without_publishing(tmp_path, data):
    _, state, files = setup(tmp_path)
    path = staged_file(state, data)
    with pytest.raises(AttachmentUnavailable) as caught:
        files.import_download({"id": "7"}, parse_attachment(card()), path)
    assert caught.value.code == "attachment_download_unverified"
    assert path.read_bytes() == data
    assert not (state / "downloads").exists()


def test_import_rejects_outside_staging_and_missing_identity(tmp_path):
    account, state, files = setup(tmp_path)
    outside = write_file(account, "2026-10", "report.pdf", b"document")
    with pytest.raises(AttachmentUnavailable) as caught:
        files.import_download({"id": "7"}, parse_attachment(card()), outside)
    assert caught.value.code == "unsafe_attachment_path"
    path = staged_file(state)
    with pytest.raises(AttachmentUnavailable) as caught:
        files.import_download({"id": "7"}, parse_attachment(card(md5=False)), path)
    assert caught.value.code == "attachment_identity_unavailable"


def test_import_rejects_staging_reparse_component(tmp_path, monkeypatch):
    from wechat_bridge import attachments
    _, state, files = setup(tmp_path)
    path = staged_file(state)
    original = attachments._is_reparse
    monkeypatch.setattr(
        attachments, "_is_reparse", lambda item: item == path.parent or original(item)
    )
    with pytest.raises(AttachmentUnavailable) as caught:
        files.import_download({"id": "7"}, parse_attachment(card()), path)
    assert caught.value.code == "unsafe_attachment_path"


def test_import_source_mutation_during_copy_is_not_published(tmp_path, monkeypatch):
    from wechat_bridge import attachments
    _, state, files = setup(tmp_path)
    path = staged_file(state)
    original_open = attachments._open_read
    calls = 0

    def mutate_before_second_open(candidate, boundary):
        nonlocal calls
        if candidate == path:
            calls += 1
            if calls == 2:
                path.write_bytes(b"changed!")
        return original_open(candidate, boundary)

    monkeypatch.setattr(attachments, "_open_read", mutate_before_second_open)
    with pytest.raises(AttachmentUnavailable) as caught:
        files.import_download({"id": "7"}, parse_attachment(card()), path)
    assert caught.value.code == "attachment_source_changed"
    assert not list((state / "downloads").rglob("report.pdf"))
    assert not list((state / "downloads").rglob(".part-*"))


def test_weixin_numbered_download_variant_requires_verified_hash(tmp_path):
    account, state, files = setup(tmp_path)
    data = b"document"
    write_file(account, "2026-10", "report.pdf", b"wrong!!!")
    renamed = write_file(account, "2026-10", "report(1).pdf", data)
    info = parse_attachment(card())
    assert files.locate({"id": "7"}, info) == renamed
    result = files.get({"id": "7"}, info)
    assert Path(result["path"]).name == "report.pdf"
    assert Path(result["path"]).read_bytes() == data
    assert renamed.read_bytes() == data
    assert Path(result["path"]).is_relative_to(state / "downloads")


def test_numbered_download_variant_with_wrong_hash_is_rejected(tmp_path):
    account, _, files = setup(tmp_path)
    write_file(account, "2026-10", "report(1).pdf", b"wrong!!!")
    with pytest.raises(AttachmentUnavailable) as caught:
        files.locate({"id": "7"}, parse_attachment(card()))
    assert caught.value.code == "attachment_not_cached"


def test_multiple_numbered_variants_with_same_content_are_not_ambiguous(tmp_path):
    account, _, files = setup(tmp_path)
    one = write_file(account, "2026-10", "report(1).pdf", b"document")
    two = write_file(account, "2026-10", "report(2).pdf", b"document")
    assert files.locate({"id": "7"}, parse_attachment(card())) in (one, two)


def test_numbered_download_variant_is_not_used_without_expected_hash(tmp_path):
    account, _, files = setup(tmp_path)
    write_file(account, "2026-10", "report(1).pdf", b"document")
    with pytest.raises(AttachmentUnavailable) as caught:
        files.locate({"id": "7"}, parse_attachment(card(md5=False)))
    assert caught.value.code == "attachment_not_cached"


@pytest.mark.parametrize("name", [
    "report(0).pdf", "report(01).pdf", "report(-1).pdf", "report(1a).pdf",
    "report (1).pdf", "report_1.pdf", "report(1).pdf.bak", "other(1).pdf",
])
def test_other_similar_names_are_not_download_variants(tmp_path, name):
    account, _, files = setup(tmp_path)
    write_file(account, "2026-10", name, b"document")
    with pytest.raises(AttachmentUnavailable) as caught:
        files.locate({"id": "7"}, parse_attachment(card()))
    assert caught.value.code == "attachment_not_cached"
