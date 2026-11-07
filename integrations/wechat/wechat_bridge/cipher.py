"""Authenticated, read-only SQLCipher 4 database snapshots.

The source is never opened with SQLite or write access.  This module understands
the 4096-byte/80-byte-reserve SQLCipher layout used by the investigated client.
It is an unlocked, per-database capture, not a transaction across Weixin shards.

WAL format: https://www.sqlite.org/fileformat2.html#walformat
Crypto layout independently implemented from the SQLCipher 4 format described
in the pinned Apache-2.0 research reference; no upstream module is imported.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timezone
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SIZE = 4096
RESERVE_SIZE = 80
WAL_HEADER_SIZE = 32
WAL_FRAME_SIZE = 24 + PAGE_SIZE
SQLITE_HEADER = b"SQLite format 3\x00"
_CACHE_VERSION = 1
_LOCK_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


class SnapshotError(RuntimeError):
    """The current source could not produce a verified snapshot."""

    def __init__(self, message: str, *, code: str = "snapshot_validation_failed",
                 stage: str = "validation", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.retryable = retryable


class _SourceChanged(SnapshotError):
    def __init__(self, message: str):
        super().__init__(message, code="snapshot_unstable", stage="acquisition", retryable=True)


def _material(key: bytes, page1: bytes) -> tuple[bytes, bytes]:
    if not isinstance(key, bytes) or len(key) not in (32, 48):
        raise SnapshotError("SQLCipher raw key must contain 32 bytes or 32 key + 16 salt bytes")
    if len(page1) != PAGE_SIZE:
        raise SnapshotError("SQLCipher page 1 must contain exactly 4096 bytes")
    enc_key = key[:32]
    salt = key[32:] if len(key) == 48 else page1[:16]
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
    return enc_key, mac_key


def _valid_page(page: bytes, pgno: int, mac_key: bytes) -> bool:
    if len(page) != PAGE_SIZE or not 1 <= pgno <= 0xFFFFFFFE:
        return False
    start = 16 if pgno == 1 else 0
    computed = hmac.new(mac_key, page[start:-64], hashlib.sha512)
    computed.update(struct.pack("<I", pgno))
    return hmac.compare_digest(computed.digest(), page[-64:])


def verify_key(key: bytes, page1: bytes) -> bool:
    """Check page-1 authentication, without returning or persisting key material."""
    try:
        _, mac_key = _material(key, page1)
    except (SnapshotError, TypeError, ValueError):
        return False
    return _valid_page(page1, 1, mac_key)


def _decrypt_page(page: bytes, pgno: int, enc_key: bytes, mac_key: bytes) -> bytes:
    if not _valid_page(page, pgno, mac_key):
        raise SnapshotError(f"SQLCipher HMAC-SHA512 authentication failed on page {pgno}")
    start = 16 if pgno == 1 else 0
    iv = page[-RESERVE_SIZE:-64]
    decryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
    plaintext = decryptor.update(page[start:-RESERVE_SIZE]) + decryptor.finalize()
    return (SQLITE_HEADER if pgno == 1 else b"") + plaintext + bytes(RESERVE_SIZE)


def _wal_checksum(data: bytes, byteorder: str, seed: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """SQLite's rolling checksum; storage byte order is separately big-endian."""
    if len(data) % 8:
        raise SnapshotError("WAL checksum input must be a multiple of 8 bytes")
    s0, s1 = seed
    for x0, x1 in struct.iter_unpack(byteorder + "II", data):
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def _stat(path: Path) -> dict | None:
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return _stat_value(value)


def _stat_value(value: os.stat_result, *, from_handle: bool = False) -> dict:
    identity = {"size": value.st_size, "mtime_ns": value.st_mtime_ns,
                "device": value.st_dev, "inode": value.st_ino}
    # Windows Python 3.13 can expose creation time through stat().st_ctime
    # while fstat().st_ctime is NTFS change time. Birth time agrees across both
    # APIs; comparing ctime here would label every unchanged file as unstable.
    if os.name == "nt":
        identity["birthtime_ns"] = getattr(value, "st_birthtime_ns", None)
        if from_handle:
            # Keep the real handle change time across repeated observations to
            # notice in-place writes, including preallocated WAL reuse. Only
            # comparisons against legacy Path.stat() must omit this field.
            identity["handle_change_time_ns"] = value.st_ctime_ns
    else:
        identity["ctime_ns"] = value.st_ctime_ns
    return identity


def _handle_identity(handle: BinaryIO) -> dict:
    return _stat_value(os.fstat(handle.fileno()), from_handle=True)


def _path_matches_handle(path: Path, handle_stat: dict) -> bool:
    path_stat = _stat(path)
    return path_stat == {name: value for name, value in handle_stat.items() if name != "handle_change_time_ns"}


def _observe_file(path: Path, head_size: int, *, required: bool = False) -> dict | None:
    try:
        with path.open("rb") as handle:
            before = _handle_identity(handle)
            head = handle.read(head_size)
            after = _handle_identity(handle)
    except FileNotFoundError:
        if required:
            raise SnapshotError(f"Source database does not exist: {path}") from None
        return None
    if before != after or not _path_matches_handle(path, after):
        raise _SourceChanged(f"Source changed while observing {path.name}")
    return {"stat": before, "head_sha256": hashlib.sha256(head).hexdigest()}


def _source_signature(source: Path) -> dict:
    journal = Path(str(source) + "-journal")
    journal_stat = _stat(journal)
    if journal_stat and journal_stat["size"]:
        raise SnapshotError("Non-empty rollback journal detected; only stable SQLCipher/WAL captures are supported")
    return {"path": str(source), "database": _observe_file(source, PAGE_SIZE, required=True),
            "wal": _observe_file(Path(str(source) + "-wal"), WAL_HEADER_SIZE),
            "journal": journal_stat}


def _check_open_file(handle: BinaryIO, expected: dict | None, label: str) -> None:
    if expected is None or _handle_identity(handle) != expected["stat"]:
        raise _SourceChanged(f"{label} changed before capture")


def _copy_observed_file(source: Path, destination: Path, expected: dict,
                        head_size: int) -> None:
    """Copy a bounded file while checking its original handle and path identity."""
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        _check_open_file(incoming, expected, source.name)
        head = incoming.read(head_size)
        if hashlib.sha256(head).hexdigest() != expected["head_sha256"]:
            raise _SourceChanged(f"{source.name} header changed before acquisition")
        incoming.seek(0)
        remaining = expected["stat"]["size"]
        while remaining:
            block = incoming.read(min(1024 * 1024, remaining))
            if not block:
                raise _SourceChanged(f"{source.name} shortened during acquisition")
            outgoing.write(block)
            remaining -= len(block)
        _check_open_file(incoming, expected, source.name)
        if not _path_matches_handle(source, expected["stat"]):
            raise _SourceChanged(f"{source.name} was replaced during acquisition")
        incoming.seek(0)
        if hashlib.sha256(incoming.read(head_size)).hexdigest() != expected["head_sha256"]:
            raise _SourceChanged(f"{source.name} header changed during acquisition")


def _acquire(source: Path, directory: Path, signature: dict) -> tuple[Path, dict, str]:
    """Detach encrypted inputs before expensive decryption and validation.

    The original DB/WAL pair must stay unchanged only through this bounded
    copy. This acquires no writer lock and cannot establish a global transaction.
    """
    captured = directory / "source.db"
    _copy_observed_file(source, captured, signature["database"], PAGE_SIZE)
    if signature["wal"] is not None:
        try:
            _copy_observed_file(Path(str(source) + "-wal"), Path(str(captured) + "-wal"),
                                signature["wal"], WAL_HEADER_SIZE)
        except FileNotFoundError:
            raise _SourceChanged("WAL disappeared during acquisition") from None
    if _source_signature(source) != signature:
        raise _SourceChanged("Database/WAL changed during encrypted acquisition")
    acquired_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return captured, _source_signature(captured), acquired_at


def _parse_wal_header(header: bytes) -> tuple[str, bytes, tuple[int, int]]:
    if len(header) != WAL_HEADER_SIZE:
        raise SnapshotError("WAL header is incomplete (expected 32 bytes)")
    magic, version, page_size = struct.unpack_from(">III", header)
    if magic not in (0x377F0682, 0x377F0683):
        raise SnapshotError(f"Unsupported WAL magic 0x{magic:08x}; expected SQLite/WCDB WAL")
    if version != 3007000 or page_size != PAGE_SIZE:
        raise SnapshotError(f"Unsupported WAL version/page size: {version}/{page_size}")
    order = "<" if magic == 0x377F0682 else ">"
    checksum = _wal_checksum(header[:24], order)
    if checksum != struct.unpack_from(">II", header, 24):
        raise SnapshotError("WAL header checksum mismatch (SQLite WAL format); source may be changing or use an unsupported WCDB variant")
    return order, header[16:24], checksum


def _apply_wal(wal: BinaryIO, output: BinaryIO, enc_key: bytes, mac_key: bytes) -> dict:
    size = os.fstat(wal.fileno()).st_size
    coverage = {"wal_present": True, "wal_current_generation_frames": 0,
                "wal_committed_frames": 0, "wal_uncommitted_frames_ignored": 0,
                "wal_stale_frames_ignored": 0, "wal_incomplete_tail_bytes_ignored": 0,
                "wal_checksum": "sqlite_rolling_checksum_over_stored_encrypted_pages"}
    if size == 0:
        return coverage
    header = wal.read(WAL_HEADER_SIZE)
    order, salts, checksum = _parse_wal_header(header)
    frame_count, tail = divmod(size - WAL_HEADER_SIZE, WAL_FRAME_SIZE)
    pending: dict[int, int] = {}
    last_commit = 0
    for index in range(1, frame_count + 1):
        frame_offset = wal.tell()
        frame_header = wal.read(24)
        page = wal.read(PAGE_SIZE)
        if len(frame_header) != 24 or len(page) != PAGE_SIZE:
            raise _SourceChanged(f"WAL frame {index} became incomplete during capture")
        # The first stale frame terminates the valid prefix. Never skip a stale
        # frame and resume at a later, superficially matching frame.
        if frame_header[8:16] != salts:
            coverage["wal_stale_frames_ignored"] = frame_count - index + 1
            break
        pgno, commit_size = struct.unpack_from(">II", frame_header)
        if not 1 <= pgno <= 0xFFFFFFFE or commit_size > 0xFFFFFFFE:
            raise SnapshotError(f"WAL frame {index} contains an invalid page number/database size")
        checksum = _wal_checksum(frame_header[:8], order, checksum)
        checksum = _wal_checksum(page, order, checksum)
        if checksum != struct.unpack_from(">II", frame_header, 16):
            raise SnapshotError(f"WAL frame {index} checksum mismatch (stored encrypted-page checksum); no fresh snapshot published. Unsupported WCDB checksum variants are not guessed")
        if not _valid_page(page, pgno, mac_key):
            raise SnapshotError(f"WAL frame {index}, page {pgno}: SQLCipher HMAC-SHA512 authentication failed")
        coverage["wal_current_generation_frames"] = index
        pending[pgno] = frame_offset + 24
        if commit_size:
            cursor = wal.tell()
            for page_number, offset in pending.items():
                # Frames past the committed size are discarded by truncation.
                if page_number <= commit_size:
                    wal.seek(offset)
                    decoded = _decrypt_page(wal.read(PAGE_SIZE), page_number, enc_key, mac_key)
                    output.seek((page_number - 1) * PAGE_SIZE)
                    output.write(decoded)
            output.truncate(commit_size * PAGE_SIZE)
            pending.clear()
            last_commit = index
            coverage["committed_database_pages"] = commit_size
            wal.seek(cursor)
    coverage["wal_committed_frames"] = last_commit
    coverage["wal_uncommitted_frames_ignored"] = coverage["wal_current_generation_frames"] - last_commit
    coverage["wal_incomplete_tail_bytes_ignored"] = tail
    # Detect reset/reuse even if a filesystem timestamp did not advance.
    wal.seek(0)
    if wal.read(WAL_HEADER_SIZE) != header:
        raise _SourceChanged("WAL header changed during capture")
    return coverage


def _capture(source: Path, temporary: Path, key: bytes, signature: dict) -> dict:
    coverage = {"scope": "single_database_committed_wal_view", "global_atomic": False,
                "source_read_only": True, "source_lock_acquired": False,
                "stability": "unchanged_file_identity_size_timestamps_and_headers_before_after_capture",
                "freshness": "latest_authenticated_commit_in_observed_stable_files",
                "limitations": ["No SQLite read lock or wal-index end mark is acquired",
                                "Separate databases are captured independently",
                                "No claim of global transaction atomicity or messages absent from local files"],
                "wal_present": False, "wal_current_generation_frames": 0,
                "wal_committed_frames": 0, "wal_uncommitted_frames_ignored": 0,
                "wal_stale_frames_ignored": 0, "wal_incomplete_tail_bytes_ignored": 0}
    with source.open("rb") as database, temporary.open("w+b") as output:
        _check_open_file(database, signature["database"], "Database")
        size = os.fstat(database.fileno()).st_size
        if not size or size % PAGE_SIZE:
            raise SnapshotError(f"Database length {size} is not a nonzero multiple of 4096")
        first = database.read(PAGE_SIZE)
        enc_key, mac_key = _material(key, first)
        if not _valid_page(first, 1, mac_key):
            raise SnapshotError("SQLCipher page 1 HMAC-SHA512 failed; key or source format does not match")
        database.seek(0)
        for pgno in range(1, size // PAGE_SIZE + 1):
            output.write(_decrypt_page(database.read(PAGE_SIZE), pgno, enc_key, mac_key))
        coverage["database_pages_hmac_verified"] = size // PAGE_SIZE
        if signature["wal"] is not None:
            wal_path = Path(str(source) + "-wal")
            try:
                with wal_path.open("rb") as wal:
                    _check_open_file(wal, signature["wal"], "WAL")
                    coverage.update(_apply_wal(wal, output, enc_key, mac_key))
                    _check_open_file(wal, signature["wal"], "WAL")
            except FileNotFoundError:
                raise _SourceChanged("WAL disappeared during capture") from None
        _check_open_file(database, signature["database"], "Database")
        output.seek(0)
        page1 = bytearray(output.read(PAGE_SIZE))
        if len(page1) != PAGE_SIZE or page1[16:18] != struct.pack(">H", PAGE_SIZE) or page1[20] != RESERVE_SIZE:
            raise SnapshotError("Decrypted SQLite header does not match 4096-byte pages / 80 reserved bytes")
        # The detached copy has no WAL. Make its logical page count authoritative
        # even when the source header was older than the last WAL commit.
        output.seek(0, os.SEEK_END)
        final_pages = output.tell() // PAGE_SIZE
        page1[18:20] = b"\x01\x01"
        page1[28:32] = struct.pack(">I", final_pages)
        page1[92:96] = page1[24:28]
        output.seek(0)
        output.write(page1)
        output.flush()
        os.fsync(output.fileno())
    coverage["snapshot_database_pages"] = final_pages
    coverage["normalized_local_header"] = ["SQLite magic", "detached rollback read/write version", "committed page count"]
    return coverage


def _quick_check(path: Path) -> None:
    uri = path.as_uri() + "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as exc:
        raise SnapshotError(f"Decrypted snapshot SQLite quick_check failed: {exc}") from exc
    if rows != [("ok",)]:
        detail = "; ".join(str(row[0]) for row in rows[:5])
        raise SnapshotError(f"Decrypted snapshot SQLite quick_check failed: {detail}")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _cache_mac(metadata: dict, key: bytes) -> str:
    cache_key = hmac.new(key, b"wechat-agent-bridge authenticated snapshot cache v1", hashlib.sha256).digest()
    return hmac.new(cache_key, _canonical(metadata), hashlib.sha256).hexdigest()


def _metadata_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".snapshot.json")


def _public(metadata: dict, refreshed: bool) -> dict:
    return {name: metadata[name] for name in ("path", "captured_at", "wal_frames", "validated", "source_signature", "coverage")} | {"refreshed": refreshed}


def _trusted_cache(destination: Path, key: bytes, signature: dict) -> dict | None:
    try:
        metadata_bytes = _metadata_path(destination).read_bytes()
        metadata = json.loads(metadata_bytes)
        stored_mac = metadata.pop("cache_mac")
        if not isinstance(stored_mac, str) or not hmac.compare_digest(stored_mac, _cache_mac(metadata, key)):
            return None
        if (metadata.get("version") != _CACHE_VERSION or metadata.get("path") != str(destination)
                or metadata.get("source_signature") != signature or metadata.get("validated") is not True):
            return None
        if metadata.get("database_sha256") != _file_digest(destination):
            return None
        if _metadata_path(destination).read_bytes() != metadata_bytes:
            return None
        return _public(metadata, False)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _new_temp(destination: Path, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="." + destination.name + "-", suffix=suffix, dir=destination.parent)
    os.close(descriptor)
    return Path(name)


def _publish(temporary: Path, destination: Path, metadata: dict, key: bytes) -> None:
    metadata = metadata | {"cache_mac": _cache_mac(metadata, key)}
    stamp = _metadata_path(destination)
    stamp_temp = _new_temp(destination, ".json.tmp")
    backup: Path | None = None
    replaced = False
    try:
        with stamp_temp.open("wb") as handle:
            handle.write(_canonical(metadata))
            handle.flush()
            os.fsync(handle.fileno())
        # Keep the old destination recoverable until both publications succeed.
        # A hard link does not mutate its bytes or double its disk usage.
        if destination.exists():
            backup = _new_temp(destination, ".previous.tmp")
            backup.unlink()
            try:
                os.link(destination, backup)
            except OSError:
                # Some filesystems do not support hard links.
                with destination.open("rb") as src, backup.open("wb") as dst:
                    while block := src.read(1024 * 1024):
                        dst.write(block)
        os.replace(temporary, destination)
        replaced = True
        os.replace(stamp_temp, stamp)
    except BaseException:
        if replaced:
            if backup is not None:
                os.replace(backup, destination)
            else:
                destination.unlink(missing_ok=True)
        raise
    finally:
        stamp_temp.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)


def _safe_paths(source: Path, destination: Path) -> tuple[Path, Path]:
    source = source.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve()
    data_root = next((parent for parent in source.parents if parent.name.lower() == "db_storage"), source.parent)
    if destination == source or destination.is_relative_to(data_root):
        raise SnapshotError("Snapshot destination must be outside the source data directory")
    if destination.exists() and os.path.samefile(source, destination):
        raise SnapshotError("Snapshot destination aliases the source database")
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(destination) + suffix).exists():
            raise SnapshotError(f"Snapshot destination has a SQLite {suffix} sidecar; use a separate immutable cache path")
    return source, destination


def snapshot(source: Path, destination: Path, key: bytes) -> dict:
    """Build or reuse an authenticated stable per-database SQLite view.

    Current-generation WAL corruption is an error; the previous verified cache
    survives and is never returned as if fresh.  At most three captures are
    attempted when normal source mutation is detected. Source opens use ``rb``.
    Encrypted inputs are copied first; later source writes cannot invalidate an
    already acquired view. Freshness refers to acquisition, not return time.
    """
    if not isinstance(key, bytes) or len(key) not in (32, 48):
        raise SnapshotError("SQLCipher raw key must contain 32 or 48 bytes")
    source, destination = _safe_paths(Path(source), Path(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK_GUARD:
        lock = _LOCKS.setdefault(os.path.normcase(str(destination)), threading.Lock())
    with lock:
        last_change: Exception | None = None
        for attempt in range(3):
            temporary: Path | None = None
            signature: dict | None = None
            acquired = False
            stage = "acquisition"
            try:
                signature = _source_signature(source)
                cached = _trusted_cache(destination, key, signature)
                if cached is not None:
                    if _source_signature(source) != signature:
                        raise _SourceChanged("Source changed while validating its cache")
                    return cached
                temporary = _new_temp(destination, ".db.tmp")
                with tempfile.TemporaryDirectory(prefix="." + destination.name + "-",
                                                 suffix=".capture", dir=destination.parent) as folder:
                    captured, captured_signature, captured_at = _acquire(source, Path(folder), signature)
                    acquired = True
                    stage = "validation"
                    coverage = _capture(captured, temporary, key, captured_signature)
                    if _source_signature(captured) != captured_signature:
                        raise SnapshotError("Detached encrypted inputs changed during validation")
                coverage.update({
                    "acquisition": "bounded_read_only_encrypted_database_and_wal_copy",
                    "stability": "unchanged_source_identity_size_timestamps_and_headers_through_acquisition",
                    "freshness": "latest_authenticated_commit_in_acquired_files_not_at_return_time",
                    "source_changes_after_acquisition_allowed": True,
                })
                _quick_check(temporary)
                metadata = {"version": _CACHE_VERSION, "path": str(destination),
                            "captured_at": captured_at,
                            "wal_frames": coverage["wal_committed_frames"], "validated": True,
                            "source_signature": signature, "coverage": coverage,
                            "database_sha256": _file_digest(temporary)}
                stage = "publish"
                _publish(temporary, destination, metadata, key)
                return _public(metadata, True)
            except (OSError, SnapshotError) as exc:
                changed = not acquired and isinstance(exc, _SourceChanged)
                if not acquired and not changed and signature is not None:
                    try:
                        changed = _source_signature(source) != signature
                    except _SourceChanged:
                        changed = True
                if not changed:
                    if isinstance(exc, SnapshotError):
                        if isinstance(exc, _SourceChanged):
                            # A detached private input changed; this is not a
                            # normal live-source race and must not be retried.
                            raise SnapshotError(str(exc), stage=stage) from exc
                        exc.stage = stage
                        raise
                    raise SnapshotError(f"Cannot capture source database: {exc}",
                                        code="snapshot_io_error", stage=stage) from exc
                last_change = exc
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        raise SnapshotError(f"Source remained unstable after 3 bounded attempts; previous cache retained: {last_change}",
                            code="snapshot_unstable", stage="acquisition", retryable=True) from last_change
