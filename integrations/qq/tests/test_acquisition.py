from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from qq_bridge import cipher
from test_cipher import KEY, QQ_HEADER, encrypt, prepare, read_value, sqlite_pages, wal_bytes


def wal_path(path: Path) -> Path:
    return Path(str(path) + "-wal")


def wal_for(value: str) -> bytes:
    return wal_bytes([(2, 2, encrypt(sqlite_pages(value)[1], 2), None)])


def on_database_read(monkeypatch, source: Path, action):
    """Inject a concurrent client change without running or inspecting QQ."""
    original_open = Path.open
    invoked = []

    class Reader:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, *args):
            data = self.handle.read(*args)
            if not invoked:
                invoked.append(True)
                action()
            return data

    def open_file(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        return Reader(handle) if path == source and mode == "rb" else handle

    monkeypatch.setattr(Path, "open", open_file)
    return invoked


def frozen_metadata(monkeypatch, *paths):
    """Model NTFS timestamps/identity that remain unchanged across a write."""
    original_stat, original_identity = cipher._stat, cipher._handle_identity
    stats, handles = {}, {}
    for path in paths:
        stats[path] = original_stat(path)
        with path.open("rb") as stream:
            handles[path] = original_identity(stream)
    monkeypatch.setattr(cipher, "_stat", lambda path: stats.get(path) if path in stats else original_stat(path))
    monkeypatch.setattr(cipher, "_handle_identity", lambda stream: handles.get(Path(stream.name))
                        if Path(stream.name) in handles else original_identity(stream))


def replace_preserving_mtime(path, payload):
    before = path.stat()
    path.write_bytes(payload)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


@pytest.mark.parametrize("target", ["database", "wal"])
def test_complete_content_detects_same_metadata_same_header_acquisition_write(tmp_path, monkeypatch, target):
    source, _ = prepare(tmp_path)
    journal = wal_path(source)
    journal.write_bytes(wal_for("old"))
    changed_path = source if target == "database" else journal
    original = changed_path.read_bytes()
    changed = (QQ_HEADER + b"".join(encrypt(page, number)
                                   for number, page in enumerate(sqlite_pages("new"), 1))
               if target == "database" else wal_for("new"))
    head_size = cipher.QQ_HEADER_SIZE + cipher.PAGE_SIZE if target == "database" else cipher.WAL_HEADER_SIZE
    assert len(changed) == len(original) and changed[:head_size] == original[:head_size]
    frozen_metadata(monkeypatch, source, journal)
    signature = cipher._source_signature(source)
    invoked = on_database_read(monkeypatch, source, lambda: replace_preserving_mtime(changed_path, changed))
    with pytest.raises(cipher.SnapshotError, match="content changed|changed during"):
        cipher._acquire_files(source, tmp_path / "copied.db", signature)
    assert invoked


@pytest.mark.parametrize("target", ["database", "wal"])
def test_cache_never_reuses_same_metadata_same_header_changed_content(tmp_path, monkeypatch, target):
    source, destination = prepare(tmp_path)
    journal = wal_path(source)
    if target == "wal":
        journal.write_bytes(wal_for("old"))
    changed_path = source if target == "database" else journal
    frozen_metadata(monkeypatch, changed_path)
    initial = cipher.snapshot(source, destination, KEY)
    assert cipher.snapshot(source, destination, KEY)["refreshed"] is False
    changed = (QQ_HEADER + b"".join(encrypt(page, number)
                                   for number, page in enumerate(sqlite_pages("new"), 1))
               if target == "database" else wal_for("new"))
    replace_preserving_mtime(changed_path, changed)
    current = cipher.snapshot(source, destination, KEY)
    assert current["refreshed"] is True and read_value(destination) == "new"
    assert current["source_signature"] != initial["source_signature"]


@pytest.mark.parametrize("phase", ["observation", "copy", "private_digest", "private_backup"])
def test_continuously_growing_source_is_rejected_after_initial_size_plus_one(tmp_path, monkeypatch, phase):
    source, _ = prepare(tmp_path)
    signature = cipher._source_signature(source)
    initial_size = source.stat().st_size
    frozen_metadata(monkeypatch, source)
    original_open = Path.open
    returned = []

    class GrowingReader:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, size=-1):
            assert size >= 0, "Source read must have an explicit finite bound"
            block = self.stream.read(size)
            returned.append(len(block))
            assert sum(returned) <= initial_size + 1, "Reader followed a growing EOF"
            with original_open(source, "ab") as writer:
                writer.write(b"grow")
            return block

    def open_file(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return GrowingReader(stream) if path == source and mode == "rb" else stream

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(cipher.SnapshotError, match="grew|changed"):
        if phase == "observation":
            cipher._source_signature(source)
        elif phase == "private_digest":
            cipher._file_digest(source)
        elif phase == "private_backup":
            cipher._backup_copy(source, tmp_path / "backup.db")
        else:
            cipher._acquire_files(source, tmp_path / "copy.db", signature)
    assert sum(returned) == initial_size + 1


def test_acquisition_copies_encrypted_db_and_wal_readonly_without_altering_sources(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path)
    source_wal = wal_path(source)
    source_wal.write_bytes(wal_for("committed"))
    signature = cipher._source_signature(source)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
              for path in (source, source_wal)}
    copied = tmp_path / "isolated-capture" / "nt_msg.db"
    copied.parent.mkdir()
    original_open = Path.open
    accesses = []

    def observed_open(path, mode="r", *args, **kwargs):
        if path in before:
            accesses.append((path, mode))
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed_open)
    started = datetime.now(timezone.utc)
    acquired_at = cipher._acquire_files(source, copied, signature)
    finished = datetime.now(timezone.utc)
    assert copied.read_bytes() == before[source][0]
    assert wal_path(copied).read_bytes() == before[source_wal][0]
    assert all(mode == "rb" for _, mode in accesses)
    for path, state in before.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns) == state
    parsed = datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
    assert started <= parsed <= finished


def test_same_length_database_write_during_acquisition_is_rejected(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path)
    signature = cipher._source_signature(source)
    changed = QQ_HEADER + b"".join(encrypt(page, number)
                                   for number, page in enumerate(sqlite_pages("new"), 1))
    assert len(changed) == source.stat().st_size
    invoked = on_database_read(monkeypatch, source, lambda: source.write_bytes(changed))
    copied = tmp_path / "copied.db"
    with pytest.raises(cipher.SnapshotError, match="changed|unstable"):
        cipher._acquire_files(source, copied, signature)
    assert invoked


@pytest.mark.parametrize("kind", ["appeared", "disappeared", "identity-replaced"])
def test_wal_generation_and_file_identity_changes_during_acquisition_are_rejected(tmp_path, monkeypatch, kind):
    source, destination = prepare(tmp_path)
    original_wal = wal_path(source)
    if kind != "appeared":
        original_wal.write_bytes(wal_for("old generation"))
    signature = cipher._source_signature(source)

    def mutate():
        if kind == "appeared":
            original_wal.write_bytes(wal_for("new commit"))
        elif kind == "disappeared":
            original_wal.unlink()
        else:
            replacement = tmp_path / "wal-replacement"
            replacement.write_bytes(original_wal.read_bytes())
            os.replace(replacement, original_wal)

    invoked = on_database_read(monkeypatch, source, mutate)
    with pytest.raises((cipher.SnapshotError, OSError)):
        cipher._acquire_files(source, tmp_path / "copied.db", signature)
    assert invoked


def test_validation_uses_acquired_view_when_real_source_has_a_later_commit(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path)
    signature = cipher._source_signature(source)
    original_capture = cipher._capture
    original_acquire = cipher._acquire_files
    captures, acquisitions = [], []

    def acquire(actual, copied, source_signature):
        acquired_at = original_acquire(actual, copied, source_signature)
        acquisitions.append((copied, acquired_at))
        return acquired_at

    def capture(copied, temporary, key, copied_signature):
        assert copied != source
        assert copied.read_bytes() == source.read_bytes()
        assert copied_signature["path"] == str(copied)
        captures.append(copied)
        wal_path(source).write_bytes(wal_for("later commit"))
        return original_capture(copied, temporary, key, copied_signature)

    monkeypatch.setattr(cipher, "_acquire_files", acquire)
    monkeypatch.setattr(cipher, "_capture", capture)
    result = cipher.snapshot(source, destination, KEY)
    assert result["validated"] is True and result["refreshed"] is True
    assert read_value(destination) == "old"
    assert len(captures) == len(acquisitions) == 1
    assert result["source_signature"] == signature
    assert result["captured_at"] == acquisitions[0][1]
    assert cipher._source_signature(source) != signature
    assert not acquisitions[0][0].exists()
    monkeypatch.setattr(cipher, "_capture", original_capture)
    following = cipher.snapshot(source, destination, KEY)
    assert following["refreshed"] is True
    assert read_value(destination) == "later commit"
    assert following["source_signature"] != result["source_signature"]


def test_successful_validation_does_not_require_reopening_live_source(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path)
    original_acquire = cipher._acquire_files
    original_open = Path.open
    acquired = []

    def acquire(actual, copied, signature):
        timestamp = original_acquire(actual, copied, signature)
        acquired.append(True)
        return timestamp

    def reject_late_live_open(path, *args, **kwargs):
        if acquired and path in (source, wal_path(source)):
            raise AssertionError("live source reopened during independent validation")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(cipher, "_acquire_files", acquire)
    monkeypatch.setattr(Path, "open", reject_late_live_open)
    result = cipher.snapshot(source, destination, KEY)
    assert result["validated"] is True
    assert read_value(destination) == "old"


@pytest.mark.parametrize("failure", [None, "acquisition", "validation", "publication"])
def test_private_copies_and_their_sidecars_are_cleaned_on_every_exit(tmp_path, monkeypatch, failure):
    source, destination = prepare(tmp_path)
    wal_path(source).write_bytes(wal_for("committed"))
    original_acquire = cipher._acquire_files
    original_capture = cipher._capture
    original_replace = cipher.os.replace
    copies = []

    def acquire(actual, copied, signature):
        copies.append(copied)
        timestamp = original_acquire(actual, copied, signature)
        assert copied.exists() and wal_path(copied).exists()
        if failure == "acquisition":
            raise cipher._SourceChanged("synthetic acquisition changed")
        return timestamp

    def capture(*args, **kwargs):
        if failure == "validation":
            raise cipher.SnapshotError("synthetic copied page authentication failure")
        return original_capture(*args, **kwargs)

    def replace(src, dst):
        if failure == "publication" and str(dst).endswith(".snapshot.json"):
            raise OSError("synthetic publication failure")
        return original_replace(src, dst)

    monkeypatch.setattr(cipher, "_acquire_files", acquire)
    monkeypatch.setattr(cipher, "_capture", capture)
    monkeypatch.setattr(cipher.os, "replace", replace)
    if failure:
        with pytest.raises(cipher.SnapshotError, match="synthetic|bounded attempts"):
            cipher.snapshot(source, destination, KEY)
        assert not destination.exists()
    else:
        assert cipher.snapshot(source, destination, KEY)["validated"] is True
        assert read_value(destination) == "committed"
    assert copies
    assert len(copies) == (3 if failure == "acquisition" else 1)
    for copied in copies:
        assert not copied.exists()
        assert not wal_path(copied).exists()
        assert not Path(str(copied) + "-shm").exists()
        assert not Path(str(copied) + "-journal").exists()
    assert all(path.name in {destination.name, destination.name + ".snapshot.json"}
               for path in destination.parent.iterdir())


def test_capture_timestamp_is_acquisition_time_not_validation_completion(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path)
    original_acquire = cipher._acquire_files
    acquired_at = "2026-10-20T00:00:00Z"

    def acquire(actual, copied, signature):
        original_acquire(actual, copied, signature)
        return acquired_at

    monkeypatch.setattr(cipher, "_acquire_files", acquire)
    result = cipher.snapshot(source, destination, KEY)
    assert result["captured_at"] == acquired_at


def test_unstable_backup_fallback_never_replaces_previous_cache_or_stamp(tmp_path, monkeypatch):
    destination = tmp_path / "private.db"
    temporary = tmp_path / "new.db"
    destination.write_bytes(b"old snapshot")
    temporary.write_bytes(b"new snapshot")
    stamp = cipher._metadata_path(destination)
    stamp.write_bytes(b"old metadata")
    original_open = Path.open

    class GrowingReader:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, size=-1):
            assert size >= 0
            block = self.stream.read(size)
            with original_open(destination, "ab") as writer:
                writer.write(b"grow")
            return block

    def open_file(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return GrowingReader(stream) if path == destination and mode == "rb" else stream

    def no_hardlinks(*args):
        raise OSError("synthetic filesystem without hard links")

    monkeypatch.setattr(cipher.os, "link", no_hardlinks)
    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(cipher.SnapshotError, match="changed"):
        cipher._publish(temporary, destination, {}, KEY)
    monkeypatch.setattr(Path, "open", original_open)
    assert destination.read_bytes().startswith(b"old snapshot")
    assert stamp.read_bytes() == b"old metadata"
    assert temporary.read_bytes() == b"new snapshot"
    assert not list(tmp_path.glob("*.previous.tmp"))
    assert not list(tmp_path.glob("*.json.tmp"))
