from __future__ import annotations

from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import struct
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from qq_bridge import cipher


KEY = bytes(range(32))
SALT = bytes(range(64, 80))
PAGE = 4096
USABLE = PAGE - 80
CRYPT_USABLE = PAGE - 48
QQ_HEADER = (b"SQLite header 3\0QQ_NT DB\0HMAC_SHA1\0").ljust(1024, b"\0")


def varint(value: int) -> bytes:
    parts = [value & 0x7F]
    while value > 0x7F:
        value >>= 7
        parts.append(value & 0x7F)
    parts.reverse()
    return bytes(value | (0x80 if index < len(parts) - 1 else 0)
                 for index, value in enumerate(parts))


def record(fields: list[tuple[int, bytes]]) -> bytes:
    serials = b"".join(varint(kind) for kind, _ in fields)
    size = len(serials) + 1
    while size != len(serials) + len(varint(size)):
        size = len(serials) + len(varint(size))
    return varint(size) + serials + b"".join(value for _, value in fields)


def leaf(cell: bytes, *, first: bool = False) -> bytearray:
    page = bytearray(PAGE)
    offset = 100 if first else 0
    start = USABLE - len(cell)
    page[offset] = 13
    struct.pack_into(">HHH", page, offset + 1, 0, 1, start)
    struct.pack_into(">H", page, offset + 8, start)
    page[start:USABLE] = cell
    return page


def sqlite_pages(value: str = "old") -> list[bytes]:
    """Independent real SQLite pages with QQ's logical 80-byte reserve.

    SQLite's payload reserve differs from the 48-byte crypto trailer. Overflow
    pages deliberately exercise ordinary non-B-tree pages as well.
    """
    sql = b"CREATE TABLE t(value TEXT)"
    schema = record([(23, b"table"), (15, b"t"), (15, b"t"), (1, b"\x02"),
                     (2 * len(sql) + 13, sql)])
    first = leaf(varint(len(schema)) + b"\x01" + schema, first=True)
    text = value.encode("utf-8")
    payload = record([(2 * len(text) + 13, text)])
    local = len(payload)
    if local > USABLE - 35:
        minimum = (USABLE - 12) * 32 // 255 - 23
        local = minimum + (len(payload) - minimum) % (USABLE - 4)
        if local > USABLE - 35:
            local = minimum
    cell = varint(len(payload)) + b"\x01" + payload[:local]
    overflow = []
    if local != len(payload):
        cell += struct.pack(">I", 3)
        rest = payload[local:]
        chunks = [rest[index:index + USABLE - 4]
                  for index in range(0, len(rest), USABLE - 4)]
        for index, chunk in enumerate(chunks):
            following = index + 4 if index + 1 < len(chunks) else 0
            overflow.append((struct.pack(">I", following) + chunk).ljust(PAGE, b"\0"))
    pages = [first, leaf(cell), *overflow]
    first[:16] = b"SQLite format 3\0"
    struct.pack_into(">H", first, 16, PAGE)
    first[18:24] = bytes((1, 1, 80, 64, 32, 32))
    for offset, value in ((24, 1), (28, len(pages)), (40, 1), (44, 4),
                          (56, 1), (92, 1), (96, 3050004)):
        struct.pack_into(">I", first, offset, value)
    return [bytes(page) for page in pages]


def encrypt(page: bytes, pgno: int, *, key: bytes = KEY,
            salt: bytes = SALT) -> bytes:
    iv = hashlib.sha256(struct.pack("<I", pgno) + page).digest()[:16]
    start = 16 if pgno == 1 else 0
    encoder = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encoded = encoder.update(page[start:CRYPT_USABLE]) + encoder.finalize()
    prefix = (salt if pgno == 1 else b"") + encoded + iv
    mac_key = hashlib.pbkdf2_hmac("sha512", key,
                                bytes(value ^ 0x3A for value in salt), 2, 32)
    tag = hmac.new(mac_key, prefix[start:] + struct.pack("<I", pgno),
                   hashlib.sha1).digest()
    return prefix + tag + bytes(12)


def checksum(data: bytes, order: str,
             state: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    values = struct.unpack(order + str(len(data) // 4) + "I", data)
    a, b = state
    for index in range(0, len(values), 2):
        a = (a + values[index] + b) % (2 ** 32)
        b = (b + values[index + 1] + a) % (2 ** 32)
    return a, b


def wal_bytes(frames: list[tuple[int, int, bytes, bytes | None]],
              *, big_endian: bool = False) -> bytes:
    order = ">" if big_endian else "<"
    salts = b"wal-salt"
    prefix = struct.pack(">IIII", 0x377F0683 if big_endian else 0x377F0682,
                         3007000, PAGE, 7) + salts
    state = checksum(prefix, order)
    result = bytearray(prefix + struct.pack(">II", *state))
    for pgno, commit_size, page, override in frames:
        leading = struct.pack(">II", pgno, commit_size)
        state = checksum(leading + page, order, state)
        result += leading + (override or salts) + struct.pack(">II", *state) + page
    return bytes(result)


def prepare(tmp_path: Path, value: str = "old") -> tuple[Path, Path]:
    root = tmp_path / "account" / "nt_qq" / "nt_db"
    root.mkdir(parents=True, exist_ok=True)
    source = root / "nt_msg.db"
    source.write_bytes(QQ_HEADER + b"".join(
        encrypt(page, number) for number, page in enumerate(sqlite_pages(value), 1)))
    return source, tmp_path / "private-cache" / "nt_msg.db"


def read_value(path: Path) -> str:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        return db.execute("SELECT value FROM t").fetchone()[0]


@pytest.mark.parametrize("raw_with_salt", [False, True])
def test_authenticated_snapshot_is_queryable_without_modifying_source(tmp_path, raw_with_salt):
    source, destination = prepare(tmp_path, "合成日程样本")
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    key = KEY + SALT if raw_with_salt else KEY
    page1 = source.read_bytes()[1024:1024 + PAGE]
    assert cipher.verify_key(key, page1)
    result = cipher.snapshot(source, destination, key)
    assert result["validated"] and result["refreshed"]
    assert result["captured_at"].endswith("Z")
    assert result["coverage"]["global_atomic"] is False
    assert result["coverage"]["database_pages_hmac_verified"] == 2
    assert read_value(destination) == "合成日程样本"
    assert destination.read_bytes()[20] == 80
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    cached = cipher.snapshot(source, destination, key)
    assert not cached["refreshed"]
    assert cached["captured_at"] == result["captured_at"]


@pytest.mark.parametrize("key", [b"", KEY[:-1], KEY + b"extra", bytes([255]) * 32,
                                 KEY + bytes(16), None, "secret"])
def test_invalid_key_or_salt_fails_verification(key):
    assert not cipher.verify_key(key, encrypt(sqlite_pages()[0], 1))


@pytest.mark.parametrize("offset", [100, PAGE - 48, PAGE - 32])
def test_ciphertext_iv_or_mac_tampering_is_rejected(tmp_path, offset):
    source, destination = prepare(tmp_path)
    data = bytearray(source.read_bytes())
    data[1024 + offset] ^= 1
    source.write_bytes(data)
    assert not cipher.verify_key(KEY, bytes(data[1024:1024 + PAGE]))
    with pytest.raises(cipher.SnapshotError, match="page 1 HMAC"):
        cipher.snapshot(source, destination, KEY)
    assert not destination.exists()


@pytest.mark.parametrize("length", [0, PAGE - 1, PAGE + 1])
def test_page_length_is_checked(length):
    assert not cipher.verify_key(KEY, bytes(length))


def test_explicit_keysalt_cannot_mask_changed_source_salt(tmp_path):
    source, destination = prepare(tmp_path)
    data = bytearray(source.read_bytes())
    data[1024] ^= 1
    source.write_bytes(data)
    assert not cipher.verify_key(KEY + SALT, bytes(data[1024:1024 + PAGE]))
    with pytest.raises(cipher.SnapshotError, match="salt does not match"):
        cipher.snapshot(source, destination, KEY + SALT)
    assert not destination.exists()


@pytest.mark.parametrize("change", ["ciphertext", "mac", "wrong-key"])
def test_failed_refresh_never_returns_previous_cache_as_fresh(tmp_path, change):
    source, destination = prepare(tmp_path)
    cipher.snapshot(source, destination, KEY)
    old = destination.read_bytes()
    stamp = Path(str(destination) + ".snapshot.json")
    old_stamp = stamp.read_bytes()
    key = KEY
    if change == "wrong-key":
        key = bytes([255]) * 32
    else:
        data = bytearray(source.read_bytes())
        offset = 100 if change == "ciphertext" else PAGE - 32
        data[1024 + PAGE + offset] ^= 1
        source.write_bytes(data)
    with pytest.raises(cipher.SnapshotError, match="HMAC|authentication"):
        cipher.snapshot(source, destination, key)
    assert destination.read_bytes() == old
    assert stamp.read_bytes() == old_stamp


@pytest.mark.parametrize("big_endian", [False, True])
def test_wal_uses_last_commit_and_excludes_uncommitted_tail(tmp_path, big_endian):
    source, destination = prepare(tmp_path)
    committed = encrypt(sqlite_pages("committed")[1], 2)
    pending = encrypt(sqlite_pages("pending")[1], 2)
    wal = Path(str(source) + "-wal")
    wal.write_bytes(wal_bytes([(2, 2, committed, None), (2, 0, pending, None)],
                              big_endian=big_endian))
    before = wal.read_bytes()
    result = cipher.snapshot(source, destination, KEY)
    assert read_value(destination) == "committed"
    assert result["wal_frames"] == 1
    assert result["coverage"]["wal_current_generation_frames"] == 2
    assert result["coverage"]["wal_uncommitted_frames_ignored"] == 1
    assert wal.read_bytes() == before


def test_uncommitted_wal_does_not_change_database(tmp_path):
    source, destination = prepare(tmp_path)
    Path(str(source) + "-wal").write_bytes(wal_bytes([
        (2, 0, encrypt(sqlite_pages("pending")[1], 2), None)]))
    result = cipher.snapshot(source, destination, KEY)
    assert read_value(destination) == "old"
    assert result["wal_frames"] == 0
    assert result["coverage"]["wal_uncommitted_frames_ignored"] == 1


def test_stale_wal_generation_terminates_prefix_and_never_resumes(tmp_path):
    source, destination = prepare(tmp_path)
    frames = [(2, 2, encrypt(sqlite_pages("current")[1], 2), None),
              (2, 2, encrypt(sqlite_pages("stale")[1], 2), b"bad-salt"),
              (2, 2, encrypt(sqlite_pages("must not apply")[1], 2), None)]
    Path(str(source) + "-wal").write_bytes(wal_bytes(frames))
    result = cipher.snapshot(source, destination, KEY)
    assert read_value(destination) == "current"
    assert result["wal_frames"] == 1
    assert result["coverage"]["wal_stale_frames_ignored"] == 2


def test_wal_commit_can_truncate_database_with_overflow_pages(tmp_path):
    source, destination = prepare(tmp_path, "x" * 9000)
    cipher.snapshot(source, destination, KEY)
    assert read_value(destination) == "x" * 9000
    assert destination.stat().st_size > 2 * PAGE
    small = sqlite_pages("shrunk")
    Path(str(source) + "-wal").write_bytes(wal_bytes([
        (2, 0, encrypt(small[1], 2), None), (1, 2, encrypt(small[0], 1), None)]))
    result = cipher.snapshot(source, destination, KEY)
    assert result["wal_frames"] == 2
    assert destination.stat().st_size == 2 * PAGE
    assert read_value(destination) == "shrunk"


@pytest.mark.parametrize("kind", ["header", "checksum", "hmac"])
def test_wal_corruption_preserves_previous_verified_cache(tmp_path, kind):
    source, destination = prepare(tmp_path)
    cipher.snapshot(source, destination, KEY)
    old = destination.read_bytes()
    page = bytearray(encrypt(sqlite_pages("new")[1], 2))
    if kind == "hmac":
        page[-32] ^= 1
    data = bytearray(wal_bytes([(2, 2, bytes(page), None)]))
    if kind == "header":
        data[31] ^= 1
    elif kind == "checksum":
        data[32 + 23] ^= 1
    Path(str(source) + "-wal").write_bytes(data)
    with pytest.raises(cipher.SnapshotError, match="HMAC-SHA1" if kind == "hmac" else "checksum mismatch"):
        cipher.snapshot(source, destination, KEY)
    assert destination.read_bytes() == old


@pytest.mark.parametrize("invalid", [(0, 2), (0xFFFFFFFF, 2), (2, 0xFFFFFFFF)])
def test_wal_invalid_page_or_commit_size_is_rejected(tmp_path, invalid):
    source, destination = prepare(tmp_path)
    page = encrypt(sqlite_pages()[1], 2)
    Path(str(source) + "-wal").write_bytes(wal_bytes([(*invalid, page, None)]))
    with pytest.raises(cipher.SnapshotError, match="invalid page number/database size"):
        cipher.snapshot(source, destination, KEY)


@pytest.mark.parametrize("tail", [b"partial frame", bytes(24 + 2000)])
def test_incomplete_wal_tail_is_reported_and_not_applied(tmp_path, tail):
    source, destination = prepare(tmp_path)
    page = encrypt(sqlite_pages("committed")[1], 2)
    Path(str(source) + "-wal").write_bytes(wal_bytes([(2, 2, page, None)]) + tail)
    result = cipher.snapshot(source, destination, KEY)
    assert read_value(destination) == "committed"
    assert result["coverage"]["wal_incomplete_tail_bytes_ignored"] == len(tail)


@pytest.mark.parametrize("length", [1, 16, 31])
def test_truncated_wal_header_fails_closed(tmp_path, length):
    source, destination = prepare(tmp_path)
    Path(str(source) + "-wal").write_bytes(bytes(length))
    with pytest.raises(cipher.SnapshotError, match="WAL header is incomplete"):
        cipher.snapshot(source, destination, KEY)
    assert not destination.exists()


@pytest.mark.parametrize("change", ["metadata", "snapshot", "source-identity"])
def test_cache_authentication_content_and_source_identity_are_required(tmp_path, change):
    source, destination = prepare(tmp_path)
    cipher.snapshot(source, destination, KEY)
    stamp = Path(str(destination) + ".snapshot.json")
    if change == "metadata":
        data = json.loads(stamp.read_text())
        data["captured_at"] = "forged"
        stamp.write_text(json.dumps(data))
    elif change == "snapshot":
        data = bytearray(destination.read_bytes())
        data[150] ^= 1
        destination.write_bytes(data)
    else:
        replacement = source.with_suffix(".replacement")
        replacement.write_bytes(source.read_bytes())
        os.replace(replacement, source)
    assert cipher.snapshot(source, destination, KEY)["refreshed"] is True
    assert read_value(destination) == "old"


def test_source_changes_have_bounded_retries_and_preserve_cache(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path)
    cipher.snapshot(source, destination, KEY)
    old = destination.read_bytes()
    original = cipher._source_signature
    calls = 0

    def changing(path):
        nonlocal calls
        signature = original(path)
        if path == source:
            calls += 1
            return signature | {"sequence": calls}
        return signature

    monkeypatch.setattr(cipher, "_source_signature", changing)
    with pytest.raises(cipher.SnapshotError, match="after 3 bounded attempts"):
        cipher.snapshot(source, destination, KEY)
    assert calls == 6
    assert destination.read_bytes() == old


@pytest.mark.parametrize("change", ["header", "logical-reserve", "btree"])
def test_authenticated_but_wrong_qq_or_sqlite_format_is_not_published(tmp_path, change):
    source, destination = prepare(tmp_path)
    pages = [bytearray(page) for page in sqlite_pages()]
    header = QQ_HEADER
    if change == "header":
        header = bytes(1024)
    elif change == "logical-reserve":
        pages[0][20] = 48
    else:
        pages[1][0] = 255
    source.write_bytes(header + b"".join(encrypt(bytes(page), index)
                                         for index, page in enumerate(pages, 1)))
    with pytest.raises(cipher.SnapshotError, match="custom header|80 reserved|quick_check failed"):
        cipher.snapshot(source, destination, KEY)
    assert not destination.exists()


def test_snapshot_rejects_source_directory_and_hardlink_alias(tmp_path):
    source, destination = prepare(tmp_path)
    for target in (source, source.parent / "decrypted.db", source.parent.parent / "other.db"):
        with pytest.raises(cipher.SnapshotError, match="outside the source data directory"):
            cipher.snapshot(source, target, KEY)
    alias = tmp_path / "source-hardlink.db"
    try:
        os.link(source, alias)
    except OSError:
        pytest.skip("filesystem does not support hard links")
    with pytest.raises(cipher.SnapshotError, match="aliases the source"):
        cipher.snapshot(source, alias, KEY)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_destination_with_sqlite_sidecars_is_rejected(tmp_path, suffix):
    source, destination = prepare(tmp_path)
    destination.parent.mkdir()
    Path(str(destination) + suffix).write_bytes(b"sidecar")
    with pytest.raises(cipher.SnapshotError, match="sidecar"):
        cipher.snapshot(source, destination, KEY)


def test_nonempty_source_journal_is_rejected(tmp_path):
    source, destination = prepare(tmp_path)
    Path(str(source) + "-journal").write_bytes(b"hot")
    with pytest.raises(cipher.SnapshotError, match="rollback journal"):
        cipher.snapshot(source, destination, KEY)


def test_metadata_publish_failure_restores_previous_destination(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path)
    cipher.snapshot(source, destination, KEY)
    old = destination.read_bytes()
    stamp = Path(str(destination) + ".snapshot.json")
    old_stamp = stamp.read_bytes()
    prepare(tmp_path, "new")
    original = cipher.os.replace

    def fail_stamp(src, dst):
        if str(dst).endswith(".snapshot.json"):
            raise OSError("simulated metadata publication failure")
        return original(src, dst)

    monkeypatch.setattr(cipher.os, "replace", fail_stamp)
    with pytest.raises(cipher.SnapshotError, match="simulated metadata publication failure"):
        cipher.snapshot(source, destination, KEY)
    assert destination.read_bytes() == old
    assert stamp.read_bytes() == old_stamp


def test_checksum_matches_wal_written_by_standard_sqlite(tmp_path):
    path = tmp_path / "real.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA page_size=4096")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t(value TEXT)")
        db.commit()
        raw = Path(str(path) + "-wal").read_bytes()
        order, _, state = cipher._parse_wal_header(raw[:32])
        state = cipher._wal_checksum(raw[32:40], order, state)
        state = cipher._wal_checksum(raw[56:56 + PAGE], order, state)
        assert state == struct.unpack_from(">II", raw, 48)


@pytest.mark.skipif(os.name != "nt", reason="Windows stat/fstat identity regression")
def test_windows_identity_uses_birthtime_and_real_handle_change_time():
    common = dict(st_size=8192, st_mtime_ns=200, st_dev=1, st_ino=2, st_birthtime_ns=100)
    path = SimpleNamespace(**common, st_ctime_ns=100)
    handle = SimpleNamespace(**common, st_ctime_ns=300)
    identity = cipher._stat_value(handle, from_handle=True)
    assert identity["handle_change_time_ns"] == 300
    assert cipher._stat_value(path) == {key: value for key, value in identity.items()
                                        if key != "handle_change_time_ns"}
    changed = SimpleNamespace(**common, st_ctime_ns=301)
    assert cipher._stat_value(changed, from_handle=True) != identity
