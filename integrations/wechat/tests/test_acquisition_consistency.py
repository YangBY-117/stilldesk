"""Source generation checks must survive delayed or colliding file metadata."""
import os
from pathlib import Path

import pytest

from wechat_bridge import cipher
from test_cipher import KEY, encrypt, prepare, read_value, sqlite_pages, wal_bytes


def wal_path(path):
    return Path(str(path) + "-wal")


def wal_for(value):
    return wal_bytes([(2, 2, encrypt(sqlite_pages(value)[1], 2), None)])


def frozen_metadata(monkeypatch, *paths):
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
def test_same_metadata_same_header_change_during_acquisition_is_rejected(tmp_path, monkeypatch, target):
    source, _ = prepare(tmp_path, "old")
    journal = wal_path(source)
    journal.write_bytes(wal_for("old"))
    changed_path = source if target == "database" else journal
    original = changed_path.read_bytes()
    changed = (b"".join(encrypt(page, number) for number, page in enumerate(sqlite_pages("new"), 1))
               if target == "database" else wal_for("new"))
    head_size = cipher.PAGE_SIZE if target == "database" else cipher.WAL_HEADER_SIZE
    assert len(changed) == len(original) and changed[:head_size] == original[:head_size]
    frozen_metadata(monkeypatch, source, journal)
    signature = cipher._source_signature(source)
    original_open = Path.open
    invoked = []

    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, *args):
            block = self.stream.read(*args)
            if not invoked:
                invoked.append(True)
                replace_preserving_mtime(changed_path, changed)
            return block

    def open_file(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return Reader(stream) if path == source and mode == "rb" else stream

    monkeypatch.setattr(Path, "open", open_file)
    capture = tmp_path / "detached"
    capture.mkdir()
    with pytest.raises(cipher.SnapshotError, match="content changed|changed during"):
        cipher._acquire(source, capture, signature)
    assert invoked


@pytest.mark.parametrize("target", ["database", "wal"])
def test_cache_cannot_hide_same_metadata_same_header_changed_content(tmp_path, monkeypatch, target):
    source, destination = prepare(tmp_path, "old")
    journal = wal_path(source)
    if target == "wal":
        journal.write_bytes(wal_for("old"))
    changed_path = source if target == "database" else journal
    frozen_metadata(monkeypatch, changed_path)
    initial = cipher.snapshot(source, destination, KEY)
    assert cipher.snapshot(source, destination, KEY)["refreshed"] is False
    changed = (b"".join(encrypt(page, number) for number, page in enumerate(sqlite_pages("new"), 1))
               if target == "database" else wal_for("new"))
    replace_preserving_mtime(changed_path, changed)
    current = cipher.snapshot(source, destination, KEY)
    assert current["refreshed"] is True and read_value(destination) == "new"
    assert current["source_signature"] != initial["source_signature"]


@pytest.mark.parametrize("phase", ["observation", "copy", "private_digest", "private_backup"])
def test_growing_source_is_bounded_by_initial_size_plus_one(tmp_path, monkeypatch, phase):
    source, _ = prepare(tmp_path, "old")
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
            assert size >= 0
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
            cipher._copy_observed_file(source, tmp_path / "copy.db", signature["database"], cipher.PAGE_SIZE)
    assert sum(returned) == initial_size + 1


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
