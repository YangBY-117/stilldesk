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
