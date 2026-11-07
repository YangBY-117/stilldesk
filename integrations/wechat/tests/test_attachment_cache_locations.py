"""Account/chat-scoped local caches never require a Weixin UI download."""
import hashlib
from pathlib import Path

import pytest

from wechat_bridge import attachments
from wechat_bridge.attachments import AttachmentInfo, AttachmentUnavailable, LocalAttachments


CHAT = "synthetic-files@chatroom"
MESSAGE = {"id": "message/message_0.db/Msg_synthetic/7", "chat": CHAT}
DATA = b"verified attachment contents"


@pytest.fixture
def cache(tmp_path):
    account, state = tmp_path / "account", tmp_path / "state"
    account.mkdir(); state.mkdir()
    return account, state, LocalAttachments(account, state)


def identity(data=DATA, *, md5=True):
    return AttachmentInfo("report.pdf", len(data), "pdf", hashlib.md5(data).hexdigest() if md5 else None)


def temporary_file(account, *, chat=CHAT, month="2026-11", name="report.pdf", data=DATA):
    digest = chat[5:] if chat.startswith("hash:") else hashlib.md5(chat.encode()).hexdigest()
    path = account / "temp/RWTemp" / month / digest / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def ordinary_file(account, *, month="2026-11", data=DATA):
    path = account / "msg/file" / month / "report.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_same_chat_rwtemp_verified_export_without_msg_file_directory(cache):
    account, state, local = cache
    source = temporary_file(account)
    assert not (account / "msg/file").exists()
    assert local.locate(MESSAGE, identity()) == source
    result = local.get(MESSAGE, identity())
    assert result["source"] == "verified_local_weixin_temporary_file"
    assert result["md5"] == hashlib.md5(DATA).hexdigest()
    assert result["sha256"] == hashlib.sha256(DATA).hexdigest()
    assert Path(result["path"]).read_bytes() == source.read_bytes() == DATA
    assert Path(result["path"]).is_relative_to(state / "downloads")


def test_rwtemp_requires_matching_content_digest(cache):
    account, _, local = cache
    temporary_file(account, data=b"x" * len(DATA))
    with pytest.raises(AttachmentUnavailable) as error:
        local.locate(MESSAGE, identity())
    assert error.value.code == "attachment_not_cached"


def test_other_chat_rwtemp_is_never_used_even_when_name_size_and_hash_match(cache):
    account, _, local = cache
    temporary_file(account, chat="other-chat@chatroom")
    with pytest.raises(AttachmentUnavailable) as error:
        local.locate(MESSAGE, identity())
    assert error.value.code == "attachment_not_cached"


def test_unresolved_chat_hash_uses_exact_rwtemp_folder(cache):
    account, _, local = cache
    source = temporary_file(account)
    unresolved = {**MESSAGE, "chat": "hash:" + hashlib.md5(CHAT.encode()).hexdigest()}
    assert local.locate(unresolved, identity()) == source


def test_rwtemp_is_not_searched_without_chat_identity(cache):
    account, _, local = cache
    temporary_file(account)
    with pytest.raises(AttachmentUnavailable) as error:
        local.locate({"id": MESSAGE["id"]}, identity())
    assert error.value.code == "attachment_not_cached"


def test_nonmonth_rwtemp_folder_is_not_a_source(cache):
    account, _, local = cache
    temporary_file(account, month="current")
    with pytest.raises(AttachmentUnavailable) as error:
        local.locate(MESSAGE, identity())
    assert error.value.code == "attachment_not_cached"


def test_numbered_rwtemp_filename_requires_expected_hash(cache):
    account, _, local = cache
    source = temporary_file(account, name="report(1).pdf")
    with pytest.raises(AttachmentUnavailable) as error:
        local.locate(MESSAGE, identity(md5=False))
    assert error.value.code == "attachment_not_cached"
    assert local.locate(MESSAGE, identity()) == source
    source.write_bytes(b"x" * len(DATA))
    with pytest.raises(AttachmentUnavailable) as error:
        local.locate(MESSAGE, identity())
    assert error.value.code == "attachment_not_cached"


def test_private_export_is_reusable_after_original_cache_cleanup(cache):
    account, _, local = cache
    source = ordinary_file(account)
    first = local.get(MESSAGE, identity())
    source.unlink()
    source.parent.rmdir(); source.parent.parent.rmdir()
    second = local.get(MESSAGE, identity())
    assert second["source"] == "verified_bridge_export"
    assert second["already_exported"] is True
    assert second["path"] == first["path"]
    assert second["sha256"] == first["sha256"]
    assert Path(second["path"]).read_bytes() == DATA
    assert not list(local.downloads.rglob(".part-*"))


def test_private_export_is_scoped_to_exact_message_id(cache):
    account, _, local = cache
    source = ordinary_file(account)
    other = {**MESSAGE, "id": "different-message"}
    local.get(other, identity())
    source.unlink()
    with pytest.raises(AttachmentUnavailable) as error:
        local.locate(MESSAGE, identity())
    assert error.value.code == "attachment_not_cached"


def test_size_only_private_export_cannot_hide_new_conflicting_weixin_candidate(cache):
    account, _, local = cache
    ordinary_file(account)
    info = identity(md5=False)
    first = local.get(MESSAGE, info)
    ordinary_file(account, month="2026-10", data=b"x" * len(DATA))
    with pytest.raises(AttachmentUnavailable) as error:
        local.get(MESSAGE, info)
    assert error.value.code == "attachment_ambiguous"
    assert Path(first["path"]).read_bytes() == DATA


def test_size_only_private_export_is_not_reused_after_source_cleanup(cache):
    account, _, local = cache
    source = ordinary_file(account)
    info = identity(md5=False)
    first = local.get(MESSAGE, info)
    source.unlink()
    with pytest.raises(AttachmentUnavailable) as error:
        local.get(MESSAGE, info)
    assert error.value.code == "attachment_not_cached"
    assert Path(first["path"]).read_bytes() == DATA


def test_conflicting_private_export_is_preserved_even_when_weixin_copy_matches(cache):
    account, state, local = cache
    ordinary_file(account)
    folder = state / "downloads" / hashlib.sha256(MESSAGE["id"].encode()).hexdigest()
    folder.mkdir(parents=True)
    destination = folder / "report.pdf"
    destination.write_bytes(b"private file must remain")
    with pytest.raises(AttachmentUnavailable) as error:
        local.get(MESSAGE, identity())
    assert error.value.code == "attachment_export_conflict"
    assert destination.read_bytes() == b"private file must remain"
    assert not list(folder.glob(".part-*"))


@pytest.mark.parametrize("location", ["temporary_root", "month", "chat_folder", "file"])
def test_reparse_components_in_rwtemp_are_rejected_before_file_read(cache, monkeypatch, location):
    account, _, local = cache
    path = temporary_file(account)
    targets = {"temporary_root": account / "temp/RWTemp", "month": path.parent.parent,
               "chat_folder": path.parent, "file": path}
    original = attachments._is_reparse
    monkeypatch.setattr(attachments, "_is_reparse", lambda p: p == targets[location] or original(p))
    monkeypatch.setattr(attachments, "_digest", lambda *args: pytest.fail("must reject before reading bytes"))
    with pytest.raises(AttachmentUnavailable) as error:
        local.locate(MESSAGE, identity())
    assert error.value.code == "unsafe_attachment_path"


@pytest.mark.parametrize("location", ["downloads", "message_folder", "file"])
def test_reparse_components_in_private_export_are_rejected(cache, monkeypatch, location):
    account, state, local = cache
    ordinary_file(account)
    result = local.get(MESSAGE, identity())
    path = Path(result["path"])
    targets = {"downloads": state / "downloads", "message_folder": path.parent, "file": path}
    original = attachments._is_reparse
    monkeypatch.setattr(attachments, "_is_reparse", lambda p: p == targets[location] or original(p))
    monkeypatch.setattr(attachments, "_digest", lambda *args: pytest.fail("must reject before reading bytes"))
    with pytest.raises(AttachmentUnavailable) as error:
        local.locate(MESSAGE, identity())
    assert error.value.code == "unsafe_attachment_path"


def test_total_entry_limit_includes_both_normal_and_rwtemp_directories(cache, monkeypatch):
    account, _, local = cache
    ordinary_file(account)
    temporary_file(account, name="unrelated.txt", data=b"other")
    monkeypatch.setattr(attachments, "MAX_SCAN_ENTRIES", 1)
    with pytest.raises(AttachmentUnavailable) as error:
        local.locate(MESSAGE, identity())
    assert error.value.code == "attachment_scan_limit"


def test_candidate_limit_includes_rwtemp_matches(cache, monkeypatch):
    account, _, local = cache
    ordinary_file(account)
    temporary_file(account)
    monkeypatch.setattr(attachments, "MAX_CANDIDATES", 1)
    with pytest.raises(AttachmentUnavailable) as error:
        local.locate(MESSAGE, identity())
    assert error.value.code == "attachment_scan_limit"
