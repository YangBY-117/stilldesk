"""Synthetic outgoing-media preparation; never reads private Weixin data or sends."""
from dataclasses import replace
from contextlib import contextmanager
from io import BytesIO
import hashlib
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

from PIL import Image
import pytest
import zstandard

from wechat_bridge import outgoing
from wechat_bridge.outgoing import OutgoingError, inspect_media, receipt_matches


def picture(kind="PNG", size=(12, 8)):
    output = BytesIO()
    Image.new("RGB", size, "cornflowerblue").save(output, format=kind)
    return output.getvalue()


def write(tmp_path, name="report.docx", data=b"synthetic document"):
    target = tmp_path / name
    target.write_bytes(data)
    return target


def assert_code(code, callback):
    with pytest.raises(OutgoingError) as caught:
        callback()
    assert caught.value.code == code


def test_preview_is_read_only_and_preserves_basename_without_paths(tmp_path):
    source = write(tmp_path, "作业说明.docx")
    before = set(tmp_path.iterdir())
    prepared = inspect_media(str(source), "file")
    assert prepared.path == prepared.source_path == source
    assert prepared.name == source.name
    assert prepared.size == source.stat().st_size
    assert prepared.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert prepared.md5 == hashlib.md5(source.read_bytes()).hexdigest()
    assert set(tmp_path.iterdir()) == before
    assert set(prepared.public()) == {"kind", "name", "size", "sha256", "md5", "metadata"}
    assert str(tmp_path) not in str(prepared.public())


@pytest.mark.parametrize("value", ["relative.docx", "C:relative.docx", r"\\server\share\file.docx",
                                    r"\\?\C:\file.docx", r"\\.\NUL", "//server/file"])
def test_relative_network_and_device_paths_are_rejected(value):
    with pytest.raises(OutgoingError):
        inspect_media(value, "file")


@pytest.mark.parametrize("name", ["CON.docx", "com¹.txt", "LPT9", "trailing. ", "trailing.",
                                   "bad\x01.docx", "bad\x7f.docx", "file.docx:stream"])
def test_unsafe_windows_names_are_rejected_before_open(tmp_path, name):
    with pytest.raises(OutgoingError):
        inspect_media(str(tmp_path / name), "file")


def test_missing_directory_empty_and_parent_traversal_are_rejected(tmp_path):
    for path in (tmp_path / "missing.docx", tmp_path, write(tmp_path, data=b""),
                 tmp_path / "sub" / ".." / "anything.docx"):
        with pytest.raises(OutgoingError):
            inspect_media(str(path), "file")


@pytest.mark.parametrize("kind,cap", [("file", "MAX_FILE_BYTES"), ("video", "MAX_FILE_BYTES"),
                                    ("image", "MAX_IMAGE_BYTES")])
def test_size_limits_fail_before_decoding(tmp_path, monkeypatch, kind, cap):
    source = write(tmp_path, "large.bin", b"a" * 129)
    monkeypatch.setattr(outgoing, cap, 128)
    assert_code("outgoing_file_too_large", lambda: inspect_media(str(source), kind))


def test_parent_reparse_component_is_rejected(tmp_path, monkeypatch):
    folder = tmp_path / "folder"
    folder.mkdir()
    source = write(folder)
    original = outgoing._is_reparse
    monkeypatch.setattr(outgoing, "_is_reparse", lambda path: path == folder or original(path))
    assert_code("unsafe_outgoing_path", lambda: inspect_media(str(source), "file"))


def test_actual_symlink_is_rejected(tmp_path):
    source = write(tmp_path)
    link = tmp_path / "link.docx"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("Host does not permit synthetic symlink creation")
    assert_code("unsafe_outgoing_path", lambda: inspect_media(str(link), "file"))


def test_source_mutation_during_hash_is_rejected(tmp_path, monkeypatch):
    source = write(tmp_path)
    original = outgoing._open_read

    class MutatingReader:
        def __init__(self, stream):
            self.stream = stream
            self.changed = False

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            data = self.stream.read(size)
            if data and not self.changed:
                source.write_bytes(b"changed while hashing")
                self.changed = True
            return data

    @contextmanager
    def changed(path):
        with original(path) as stream:
            yield MutatingReader(stream)

    monkeypatch.setattr(outgoing, "_open_read", changed)
    assert_code("outgoing_source_changed", lambda: inspect_media(str(source), "file"))


def test_image_identity_dimensions_and_extension_are_checked(tmp_path):
    source = write(tmp_path, "photo.png", picture())
    prepared = inspect_media(str(source), "image")
    assert prepared.metadata == {"format": "PNG", "width": 12, "height": 8, "animated": False}
    wrong = write(tmp_path, "wrong.jpg", source.read_bytes())
    assert_code("outgoing_format_mismatch", lambda: inspect_media(str(wrong), "image"))
    unsupported = write(tmp_path, "photo.webp", picture("WEBP"))
    assert_code("unsupported_outgoing_image", lambda: inspect_media(str(unsupported), "image"))


def test_truncated_and_excessive_pixel_images_are_rejected(tmp_path, monkeypatch):
    source = write(tmp_path, "broken.jpg", picture("JPEG")[:-20])
    assert_code("invalid_outgoing_image", lambda: inspect_media(str(source), "image"))
    source = write(tmp_path, "large.png", picture())
    monkeypatch.setattr(outgoing, "MAX_IMAGE_PIXELS", 90)
    assert_code("outgoing_image_too_large", lambda: inspect_media(str(source), "image"))


def mp4_header():
    return (24).to_bytes(4, "big") + b"ftypisom" + b"\x00" * 4 + b"isomiso2"


def test_mp4_probe_is_bounded_has_no_shell_and_keeps_private_logs_out(tmp_path, monkeypatch):
    source = write(tmp_path, "video.mp4", mp4_header())
    calls = []

    def probe(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"\x00\x00\x00")

    monkeypatch.setattr(outgoing.subprocess, "run", probe)
    prepared = inspect_media(str(source), "video")
    assert prepared.metadata == {"format": "MP4", "first_frame_decoded": True}
    args, kwargs = calls[0]
    assert "-nostdin" in args and "-protocol_whitelist" in args
    assert args[args.index("-protocol_whitelist") + 1] == "file,pipe"
    assert args[args.index("-i") + 1] == str(source)
    assert kwargs["timeout"] == 15
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert not kwargs.get("shell")


def test_invalid_video_magic_never_starts_decoder_and_failed_probe_rejects(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(outgoing.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    source = write(tmp_path, "video.mp4", b"pretend-video")
    assert_code("unsupported_outgoing_video", lambda: inspect_media(str(source), "video"))
    assert calls == []
    source.write_bytes(mp4_header())
    monkeypatch.setattr(outgoing.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout=b""))
    assert_code("invalid_outgoing_video", lambda: inspect_media(str(source), "video"))


def test_video_probe_mutation_invalidates_metadata(tmp_path, monkeypatch):
    source = write(tmp_path, "video.mp4", mp4_header())

    def probe(*args, **kwargs):
        source.write_bytes(mp4_header() + b"changed")
        return SimpleNamespace(returncode=0, stdout=b"\x00\x00\x00")

    monkeypatch.setattr(outgoing.subprocess, "run", probe)
    assert_code("outgoing_source_changed", lambda: inspect_media(str(source), "video"))


def test_staging_copies_verified_bytes_preserves_basename_and_detects_stale_copy(tmp_path):
    source = write(tmp_path)
    prepared = inspect_media(str(source), "file")
    state = tmp_path / "state"
    state.mkdir()
    staged = prepared.stage(state, "synthetic_request_1")
    assert staged.path != source and staged.source_path == source
    assert staged.path.name == source.name
    assert staged.path.parent.parent == state / "outgoing"
    assert staged.path.read_bytes() == source.read_bytes()
    assert staged.validate_staged() is True
    staged.path.write_bytes(b"modified staged copy")
    assert_code("outgoing_staging_changed", staged.validate_staged)


def test_changed_source_never_stages_and_invalid_request_never_writes(tmp_path):
    source = write(tmp_path)
    prepared = inspect_media(str(source), "file")
    state = tmp_path / "state"
    state.mkdir()
    assert_code("invalid_outgoing_request_id", lambda: prepared.stage(state, "short"))
    assert list(state.iterdir()) == []
    original_time = source.stat().st_mtime_ns
    source.write_bytes(b"x" * prepared.size)
    os.utime(source, ns=(source.stat().st_atime_ns, original_time))
    assert_code("outgoing_source_changed", lambda: prepared.stage(state, "synthetic_request_2"))
    assert list(state.iterdir()) == []


def test_staging_and_validation_reject_reparse_directory(tmp_path, monkeypatch):
    source = write(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    prepared = inspect_media(str(source), "file")
    staged = prepared.stage(state, "synthetic_request_3")
    original = outgoing._is_reparse
    monkeypatch.setattr(outgoing, "_is_reparse", lambda path:
                        path == staged.path.parent or original(path))
    assert_code("unsafe_outgoing_path", staged.validate_staged)


def test_replaced_staged_file_is_rejected_even_with_identical_bytes(tmp_path):
    source = write(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    staged = inspect_media(str(source), "file").stage(state, "synthetic_request_replace")
    original = staged.path.stat()
    replacement = staged.path.with_suffix(".replacement")
    replacement.write_bytes(staged.path.read_bytes())
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    replacement.replace(staged.path)
    assert_code("outgoing_staging_changed", staged.validate_staged)


def test_staging_parent_reparse_is_rejected_before_copy(tmp_path, monkeypatch):
    source = write(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / "outgoing").mkdir()
    prepared = inspect_media(str(source), "file")
    original = outgoing._is_reparse
    monkeypatch.setattr(outgoing, "_is_reparse", lambda path:
                        path == state / "outgoing" or original(path))
    assert_code("unsafe_outgoing_path", lambda: prepared.stage(state, "synthetic_request_parent"))
    assert list((state / "outgoing").iterdir()) == []


def make_staged(tmp_path):
    source = write(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    return source, inspect_media(str(source), "file").stage(state, "synthetic_lease_request")


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing is mandatory; POSIX is advisory only")
def test_staged_lease_denies_write_delete_replace_and_preserves_source_sharing(tmp_path):
    source, staged = make_staged(tmp_path)
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"replacement")
    with staged.lease_staged() as held:
        assert held is staged and staged.validate_staged()
        for mutation in (lambda: staged.path.write_bytes(b"changed"),
                         staged.path.unlink, lambda: replacement.replace(staged.path)):
            with pytest.raises(OSError):
                mutation()
        assert staged.path.read_bytes() == b"synthetic document"
        # The original source is deliberately outside the staged lease.
        source.write_bytes(b"changed original source")
        assert staged.validate_staged()
    staged.path.write_bytes(b"writable once lease ends")
    assert_code("outgoing_staging_changed", staged.validate_staged)


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows mandatory file sharing")
def test_staged_lease_rejects_an_already_open_writer_before_yield(tmp_path):
    _, staged = make_staged(tmp_path)
    with staged.path.open("r+b"):
        with pytest.raises(OutgoingError) as caught:
            with staged.lease_staged():
                pytest.fail("A staged file with an existing writer received a lease")
    assert caught.value.code == "outgoing_file_unavailable"


def test_staged_lease_rejects_unstaged_or_changed_copy_before_yield(tmp_path):
    source = write(tmp_path)
    prepared = inspect_media(str(source), "file")
    with pytest.raises(OutgoingError) as caught:
        with prepared.lease_staged():
            pytest.fail("An unstaged file received a lease")
    assert caught.value.code == "outgoing_not_staged"
    state = tmp_path / "state"
    state.mkdir()
    staged = prepared.stage(state, "synthetic_lease_changed")
    staged.path.write_bytes(b"changed before lease")
    with pytest.raises(OutgoingError) as caught:
        with staged.lease_staged():
            pytest.fail("Changed staged bytes received a lease")
    assert caught.value.code == "outgoing_staging_changed"


def test_staged_lease_releases_on_body_error_without_rewriting_exception(tmp_path):
    _, staged = make_staged(tmp_path)
    failure = OSError("synthetic caller failure")
    with pytest.raises(OSError) as caught:
        with staged.lease_staged():
            raise failure
    assert caught.value is failure
    staged.path.write_bytes(b"lease released")


def test_staged_lease_revalidates_on_normal_exit(tmp_path, monkeypatch):
    _, staged = make_staged(tmp_path)
    original = outgoing.PreparedMedia.validate_staged
    checks = []

    def validate(self):
        checks.append(self.path)
        return original(self)

    monkeypatch.setattr(outgoing.PreparedMedia, "validate_staged", validate)
    with staged.lease_staged():
        assert len(checks) == 1
    assert len(checks) == 2


def test_source_mutation_during_copy_is_never_published(tmp_path, monkeypatch):
    source = write(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    prepared = inspect_media(str(source), "file")
    original = outgoing._fingerprint

    def changed(path, cap, **kwargs):
        if kwargs.get("output") is not None:
            source.write_bytes(b"changed before copy")
        return original(path, cap, **kwargs)

    monkeypatch.setattr(outgoing, "_fingerprint", changed)
    assert_code("outgoing_source_changed", lambda: prepared.stage(state, "synthetic_request_4"))


def file_card(prepared, *, name=None, size=None, md5=None):
    return ("<msg><appmsg><type>6</type>"
            f"<title>{prepared.name if name is None else name}</title>"
            f"<md5>{prepared.md5 if md5 is None else md5}</md5><appattach>"
            f"<totallen>{prepared.size if size is None else size}</totallen>"
            "<aeskey>PRIVATE_AES_KEY</aeskey></appattach></appmsg></msg>")


def test_file_receipt_requires_name_size_and_semantic_md5(tmp_path):
    prepared = inspect_media(str(write(tmp_path)), "file")
    assert receipt_matches("file", (file_card(prepared),), prepared)
    for kwargs in ({"name": "other.docx"}, {"size": prepared.size + 1}, {"md5": "f" * 32}, {"md5": ""}):
        assert not receipt_matches("file", (file_card(prepared, **kwargs),), prepared)
    assert not receipt_matches("file", (f"<msg><aeskey>{prepared.md5}</aeskey></msg>",), prepared)


@pytest.mark.parametrize("kind,tag,keys", [("image", "img", ("md5", "origimgmd5")),
                                        ("video", "videomsg", ("md5", "newmd5"))])
def test_media_receipts_parse_only_semantic_hashes_and_support_zstd(tmp_path, kind, tag, keys):
    prepared = replace(inspect_media(str(write(tmp_path)), "file"), kind=kind)
    for key in keys:
        raw = f'<msg><{tag} {key}="{prepared.md5.upper()}" aeskey="PRIVATE_TOKEN"/></msg>'
        assert receipt_matches(kind, (raw,), prepared)
        assert receipt_matches(kind, (zstandard.ZstdCompressor().compress(raw.encode()),), prepared)
    for raw in (f'<msg><{tag} aeskey="{prepared.md5}" cdnthumburl="{prepared.md5}"/></msg>',
                f'<msg><{tag} md5="{"f" * 32}"/></msg>',
                f'<msg><other md5="{prepared.md5}"/></msg>',
                f'<msg><appmsg><{tag} md5="{prepared.md5}"/></appmsg></msg>',
                f'<msg><{tag} md5="{prepared.md5}ff"/></msg>', "not XML"):
        assert not receipt_matches(kind, (raw,), prepared)
    assert not receipt_matches("file", (raw,), prepared)


def test_media_receipt_rejects_xml_entities_oversized_and_arbitrary_prefix(tmp_path):
    prepared = replace(inspect_media(str(write(tmp_path)), "file"), kind="image")
    card = f'<msg><img md5="{prepared.md5}"/></msg>'
    assert receipt_matches("image", ("synthetic_sender:\n" + card,), prepared)
    for raw in ("preceding arbitrary prose " + card,
                '<!DOCTYPE msg [<!ENTITY x "secret">]>' + card,
                b"<msg>" + b"x" * (outgoing.MAX_XML_BYTES + 1) + b"</msg>",
                zstandard.ZstdCompressor().compress(b"x" * (outgoing.MAX_XML_BYTES + 1))):
        assert not receipt_matches("image", (raw,), prepared)
