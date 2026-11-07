from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import struct
from contextlib import closing
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_bridge import cipher


KEY = bytes(range(32))
SALT = bytes(range(64, 80))
PAGE = 4096
USABLE = PAGE - 80


def varint(value: int) -> bytes:
    parts = [value & 0x7F]
    while value > 0x7F:
        value >>= 7
        parts.append(value & 0x7F)
    parts.reverse()
    return bytes(part | (0x80 if i < len(parts) - 1 else 0) for i, part in enumerate(parts))


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
    """Small real SQLite fixture, including valid SQLCipher reserved space.

    The overflow case uses real SQLite payload distribution and linked overflow
    pages rather than inventing a B-tree type restriction for every page.
    """
    sql = b"CREATE TABLE t(value TEXT)"
    schema_record = record([(23, b"table"), (15, b"t"), (15, b"t"), (1, b"\x02"), (2 * len(sql) + 13, sql)])
    schema_cell = varint(len(schema_record)) + b"\x01" + schema_record
    first = leaf(schema_cell, first=True)
    value_bytes = value.encode("utf-8")
    payload = record([(2 * len(value_bytes) + 13, value_bytes)])
    local_size = len(payload)
    if local_size > USABLE - 35:
        minimum = (USABLE - 12) * 32 // 255 - 23
        local_size = minimum + (len(payload) - minimum) % (USABLE - 4)
        if local_size > USABLE - 35:
            local_size = minimum
    cell = varint(len(payload)) + b"\x01" + payload[:local_size]
    overflow = []
    if local_size != len(payload):
        cell += struct.pack(">I", 3)
        remaining = payload[local_size:]
        chunks = [remaining[i:i + USABLE - 4] for i in range(0, len(remaining), USABLE - 4)]
        for index, chunk in enumerate(chunks):
            following = index + 4 if index + 1 < len(chunks) else 0
            overflow.append((struct.pack(">I", following) + chunk).ljust(PAGE, b"\x00"))
    pages = [first, leaf(cell), *overflow]
    first[:16] = b"SQLite format 3\x00"
    struct.pack_into(">H", first, 16, PAGE)
    first[18:24] = bytes((1, 1, 80, 64, 32, 32))
    for offset, number in ((24, 1), (28, len(pages)), (40, 1), (44, 4), (56, 1), (92, 1), (96, 3050004)):
        struct.pack_into(">I", first, offset, number)
    return [bytes(page) for page in pages]


def encrypt(page: bytes, pgno: int, *, explicit: bool = False) -> bytes:
    iv = hashlib.sha256(struct.pack("<I", pgno) + page).digest()[:16]
    start = 16 if pgno == 1 else 0
    encoder = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
    encoded = encoder.update(page[start:USABLE]) + encoder.finalize()
    header = page[:16] if explicit else SALT
    prefix = (header if pgno == 1 else b"") + encoded + iv
    mac_key = hashlib.pbkdf2_hmac("sha512", KEY, bytes(n ^ 0x3A for n in SALT), 2, 32)
    tag = hmac.new(mac_key, prefix[start:] + struct.pack("<I", pgno), hashlib.sha512).digest()
    return prefix + tag


def checksum(data: bytes, order: str, state: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    # Fixture implementation follows the public SQLite file format, independent
    # of the module's parser and crypto implementation.
    values = struct.unpack(order + str(len(data) // 4) + "I", data)
    a, b = state
    for position in range(0, len(values), 2):
        a = (a + values[position] + b) % (2 ** 32)
        b = (b + values[position + 1] + a) % (2 ** 32)
    return a, b


def wal_bytes(frames: list[tuple[int, int, bytes, bytes | None]], *, big_endian: bool = False) -> bytes:
    salts = b"wal-salt"
    order = ">" if big_endian else "<"
    prefix = struct.pack(">IIII", 0x377F0683 if big_endian else 0x377F0682, 3007000, PAGE, 7) + salts
    state = checksum(prefix, order)
    result = bytearray(prefix + struct.pack(">II", *state))
    for pgno, commit_size, page, override_salts in frames:
        leading = struct.pack(">II", pgno, commit_size)
        state = checksum(leading + page, order, state)
        result += leading + (override_salts or salts) + struct.pack(">II", *state) + page
    return bytes(result)


def prepare(tmp_path: Path, value: str = "old", *, explicit: bool = False) -> tuple[Path, Path]:
    data = tmp_path / "source-data"
    data.mkdir(exist_ok=True)
    source = data / "message.db"
    source.write_bytes(b"".join(encrypt(page, i, explicit=explicit) for i, page in enumerate(sqlite_pages(value), 1)))
    return source, tmp_path / "cache" / "message.db"


def read_value(path: Path) -> str:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as database:
        assert database.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        return database.execute("SELECT value FROM t").fetchone()[0]


@pytest.mark.parametrize("explicit", [False, True])
def test_key_verification_and_authenticated_queryable_snapshot(tmp_path, explicit):
    source, destination = prepare(tmp_path, "你好，SQLite", explicit=explicit)
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    key = KEY + SALT if explicit else KEY
    assert cipher.verify_key(key, source.read_bytes()[:PAGE])
    assert not cipher.verify_key(bytes([255]) * 32, source.read_bytes()[:PAGE])
    assert not cipher.verify_key(key[:31], source.read_bytes()[:PAGE])
    assert not cipher.verify_key(key, source.read_bytes()[:PAGE - 1])
    result = cipher.snapshot(source, destination, key)
    assert result["validated"] is True
    assert result["refreshed"] is True
    assert result["captured_at"].endswith("Z")
    assert result["coverage"]["global_atomic"] is False
    assert read_value(destination) == "你好，SQLite"
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    again = cipher.snapshot(source, destination, key)
    assert again["refreshed"] is False
    assert again["captured_at"] == result["captured_at"]


def test_tampered_hmac_or_ciphertext_rejected(tmp_path):
    source, destination = prepare(tmp_path)
    original = bytearray(source.read_bytes())
    original[100] ^= 1
    assert not cipher.verify_key(KEY, original[:PAGE])
    with pytest.raises(cipher.SnapshotError, match="page 1 HMAC"):
        source.write_bytes(original)
        cipher.snapshot(source, destination, KEY)
    assert not destination.exists()


def test_failed_refresh_preserves_old_verified_cache(tmp_path):
    source, destination = prepare(tmp_path)
    cipher.snapshot(source, destination, KEY)
    old = destination.read_bytes()
    stamp = Path(str(destination) + ".snapshot.json")
    old_stamp = stamp.read_bytes()
    corrupted = bytearray(source.read_bytes())
    corrupted[2 * PAGE - 10] ^= 1
    source.write_bytes(corrupted)
    with pytest.raises(cipher.SnapshotError, match="authentication failed on page 2"):
        cipher.snapshot(source, destination, KEY)
    assert destination.read_bytes() == old
    assert stamp.read_bytes() == old_stamp
    assert read_value(destination) == "old"


@pytest.mark.parametrize("big_endian", [False, True])
def test_wal_commit_boundary_excludes_uncommitted_tail(tmp_path, big_endian):
    source, destination = prepare(tmp_path)
    committed = encrypt(sqlite_pages("committed")[1], 2)
    pending = encrypt(sqlite_pages("not committed")[1], 2)
    Path(str(source) + "-wal").write_bytes(wal_bytes([(2, 2, committed, None), (2, 0, pending, None)], big_endian=big_endian))
    result = cipher.snapshot(source, destination, KEY)
    assert read_value(destination) == "committed"
    assert result["wal_frames"] == 1
    assert result["coverage"]["wal_uncommitted_frames_ignored"] == 1
    assert result["coverage"]["wal_current_generation_frames"] == 2


def test_wal_with_no_commit_keeps_base_database(tmp_path):
    source, destination = prepare(tmp_path)
    pending = encrypt(sqlite_pages("pending")[1], 2)
    Path(str(source) + "-wal").write_bytes(wal_bytes([(2, 0, pending, None)]))
    result = cipher.snapshot(source, destination, KEY)
    assert read_value(destination) == "old"
    assert result["wal_frames"] == 0
    assert result["coverage"]["wal_uncommitted_frames_ignored"] == 1


def test_salt_mismatch_ends_generation_and_never_resumes(tmp_path):
    source, destination = prepare(tmp_path)
    good = encrypt(sqlite_pages("current generation")[1], 2)
    stale = encrypt(sqlite_pages("stale generation")[1], 2)
    later = encrypt(sqlite_pages("must never be applied")[1], 2)
    Path(str(source) + "-wal").write_bytes(wal_bytes([(2, 2, good, None), (2, 2, stale, b"bad-salt"), (2, 2, later, None)]))
    result = cipher.snapshot(source, destination, KEY)
    assert read_value(destination) == "current generation"
    assert result["wal_frames"] == 1
    assert result["coverage"]["wal_stale_frames_ignored"] == 2


def test_wal_commit_truncates_and_overflow_pages_are_accepted(tmp_path):
    source, destination = prepare(tmp_path, "x" * 9000)
    full = cipher.snapshot(source, destination, KEY)
    assert read_value(destination) == "x" * 9000
    assert full["coverage"]["snapshot_database_pages"] > 2
    small = sqlite_pages("shrunk")
    Path(str(source) + "-wal").write_bytes(wal_bytes([(2, 0, encrypt(small[1], 2), None), (1, 2, encrypt(small[0], 1), None)]))
    result = cipher.snapshot(source, destination, KEY)
    assert result["wal_frames"] == 2
    assert destination.stat().st_size == 2 * PAGE
    assert read_value(destination) == "shrunk"


@pytest.mark.parametrize("kind", ["header", "frame_checksum", "page_hmac"])
def test_wal_corruption_is_an_error_and_never_serves_old_as_fresh(tmp_path, kind):
    source, destination = prepare(tmp_path)
    cipher.snapshot(source, destination, KEY)
    old = destination.read_bytes()
    page = bytearray(encrypt(sqlite_pages("new")[1], 2))
    if kind == "page_hmac":
        page[-1] ^= 1
    data = bytearray(wal_bytes([(2, 2, bytes(page), None)]))
    if kind == "header":
        data[31] ^= 1
    elif kind == "frame_checksum":
        data[32 + 23] ^= 1
    Path(str(source) + "-wal").write_bytes(data)
    pattern = "HMAC-SHA512" if kind == "page_hmac" else "checksum mismatch"
    with pytest.raises(cipher.SnapshotError, match=pattern):
        cipher.snapshot(source, destination, KEY)
    assert destination.read_bytes() == old


def test_partial_tail_is_explicitly_reported_and_not_applied(tmp_path):
    source, destination = prepare(tmp_path)
    page = encrypt(sqlite_pages("committed")[1], 2)
    tail = b"incomplete frame"
    Path(str(source) + "-wal").write_bytes(wal_bytes([(2, 2, page, None)]) + tail)
    result = cipher.snapshot(source, destination, KEY)
    assert read_value(destination) == "committed"
    assert result["coverage"]["wal_incomplete_tail_bytes_ignored"] == len(tail)


def test_cache_requires_authenticated_metadata_and_matching_content(tmp_path):
    source, destination = prepare(tmp_path)
    cipher.snapshot(source, destination, KEY)
    stamp = Path(str(destination) + ".snapshot.json")
    metadata = json.loads(stamp.read_text())
    metadata["captured_at"] = "forged"
    stamp.write_text(json.dumps(metadata))
    assert cipher.snapshot(source, destination, KEY)["refreshed"] is True
    damaged = bytearray(destination.read_bytes())
    damaged[150] ^= 1
    destination.write_bytes(damaged)
    assert cipher.snapshot(source, destination, KEY)["refreshed"] is True
    assert read_value(destination) == "old"


def test_source_mutation_has_three_bounded_attempts_and_preserves_cache(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path)
    cipher.snapshot(source, destination, KEY)
    old = destination.read_bytes()
    observe = cipher._source_signature
    counter = 0

    def changing(path):
        nonlocal counter
        counter += 1
        return observe(path) | {"observed_sequence": counter}

    monkeypatch.setattr(cipher, "_source_signature", changing)
    with pytest.raises(cipher.SnapshotError, match="after 3 bounded attempts"):
        cipher.snapshot(source, destination, KEY)
    assert counter == 6
    assert destination.read_bytes() == old


def test_authenticated_but_malformed_sqlite_is_not_published(tmp_path):
    source, destination = prepare(tmp_path)
    pages = sqlite_pages()
    broken = bytearray(pages[1])
    broken[0] = 255
    source.write_bytes(encrypt(pages[0], 1) + encrypt(bytes(broken), 2))
    with pytest.raises(cipher.SnapshotError, match="quick_check failed"):
        cipher.snapshot(source, destination, KEY)
    assert not destination.exists()


def test_rollback_journal_and_destination_source_aliases_rejected(tmp_path):
    source, destination = prepare(tmp_path)
    with pytest.raises(cipher.SnapshotError, match="outside the source data directory"):
        cipher.snapshot(source, source.parent / "decrypted.db", KEY)
    with pytest.raises(cipher.SnapshotError, match="outside the source data directory"):
        cipher.snapshot(source, source, KEY)
    Path(str(source) + "-journal").write_bytes(b"hot")
    with pytest.raises(cipher.SnapshotError, match="rollback journal"):
        cipher.snapshot(source, destination, KEY)
    assert not destination.exists()


def test_publish_metadata_failure_rolls_back_destination(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path)
    cipher.snapshot(source, destination, KEY)
    old = destination.read_bytes()
    old_stamp = Path(str(destination) + ".snapshot.json").read_bytes()
    source.write_bytes(b"".join(encrypt(page, number) for number, page in enumerate(sqlite_pages("new"), 1)))
    replace = cipher.os.replace

    def fail_stamp(src, dst):
        if str(dst).endswith(".snapshot.json"):
            raise OSError("simulated metadata publish failure")
        return replace(src, dst)

    monkeypatch.setattr(cipher.os, "replace", fail_stamp)
    with pytest.raises(cipher.SnapshotError, match="simulated metadata publish failure"):
        cipher.snapshot(source, destination, KEY)
    assert destination.read_bytes() == old
    assert Path(str(destination) + ".snapshot.json").read_bytes() == old_stamp


def test_checksum_matches_a_real_sqlite_wal(tmp_path):
    database_path = tmp_path / "real-sqlite.db"
    with sqlite3.connect(database_path) as database:
        database.execute("PRAGMA page_size=4096")
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE real(value TEXT)")
        database.commit()
        raw = Path(str(database_path) + "-wal").read_bytes()
        header = raw[:32]
        order, _, state = cipher._parse_wal_header(header)
        frame_header = raw[32:56]
        state = cipher._wal_checksum(frame_header[:8], order, state)
        state = cipher._wal_checksum(raw[56:56 + PAGE], order, state)
        assert state == struct.unpack_from(">II", frame_header, 16)


@pytest.mark.skipif(cipher.os.name != "nt", reason="Windows-specific stat/fstat API regression")
def test_windows_birthtime_identity_does_not_compare_legacy_path_ctime():
    common = dict(st_size=8192, st_mtime_ns=200, st_dev=1, st_ino=2, st_birthtime_ns=100)
    path_stat = SimpleNamespace(**common, st_ctime_ns=100)
    handle_stat = SimpleNamespace(**common, st_ctime_ns=300)
    identity = cipher._stat_value(handle_stat, from_handle=True)
    assert identity["handle_change_time_ns"] == 300
    assert cipher._stat_value(path_stat) == {name: value for name, value in identity.items() if name != "handle_change_time_ns"}
    assert cipher._stat_value(handle_stat, from_handle=True) != cipher._stat_value(SimpleNamespace(**common, st_ctime_ns=301), from_handle=True)


def test_source_changes_during_decryption_do_not_discard_acquired_view(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path, "acquired")
    original_capture = cipher._capture
    calls = 0

    def capture_then_source_write(captured, temporary, key, signature):
        nonlocal calls
        calls += 1
        assert captured != source
        source.write_bytes(b"".join(encrypt(page, number)
                                   for number, page in enumerate(sqlite_pages("later source"), 1)))
        return original_capture(captured, temporary, key, signature)

    monkeypatch.setattr(cipher, "_capture", capture_then_source_write)
    result = cipher.snapshot(source, destination, KEY)
    assert calls == 1
    assert read_value(destination) == "acquired"
    assert result["coverage"]["source_changes_after_acquisition_allowed"] is True
    assert result["coverage"]["freshness"] == "latest_authenticated_commit_in_acquired_files_not_at_return_time"
    assert result["source_signature"]["path"] == str(source)
    assert not list(destination.parent.glob("*.capture"))
    monkeypatch.setattr(cipher, "_capture", original_capture)
    assert cipher.snapshot(source, destination, KEY)["refreshed"] is True
    assert read_value(destination) == "later source"


def test_write_during_acquisition_retries_before_decryption(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path, "before write")
    original_copy = cipher._copy_observed_file
    copies = 0

    def copy_then_source_write(incoming, outgoing, expected, head_size):
        nonlocal copies
        original_copy(incoming, outgoing, expected, head_size)
        copies += 1
        if copies == 1:
            source.write_bytes(b"".join(encrypt(page, number)
                                       for number, page in enumerate(sqlite_pages("after write"), 1)))

    monkeypatch.setattr(cipher, "_copy_observed_file", copy_then_source_write)
    cipher.snapshot(source, destination, KEY)
    assert copies == 2
    assert read_value(destination) == "after write"
    assert not list(destination.parent.glob("*.capture"))


def test_wal_appearing_during_acquisition_is_retried(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path)
    original_copy = cipher._copy_observed_file
    written = False

    def copy_then_wal_appears(incoming, outgoing, expected, head_size):
        nonlocal written
        original_copy(incoming, outgoing, expected, head_size)
        if not written:
            written = True
            page = encrypt(sqlite_pages("wal arrived")[1], 2)
            Path(str(source) + "-wal").write_bytes(wal_bytes([(2, 2, page, None)]))

    monkeypatch.setattr(cipher, "_copy_observed_file", copy_then_wal_appears)
    result = cipher.snapshot(source, destination, KEY)
    assert read_value(destination) == "wal arrived"
    assert result["wal_frames"] == 1


def test_detached_corruption_is_not_retried_even_if_live_source_changes(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path)
    cipher.snapshot(source, destination, KEY)
    old = destination.read_bytes()
    old_stamp = Path(str(destination) + ".snapshot.json").read_bytes()
    source.write_bytes(b"".join(encrypt(page, number)
                               for number, page in enumerate(sqlite_pages("refresh"), 1)))
    original_acquire = cipher._acquire
    calls = 0

    def acquire_then_corrupt(*arguments):
        nonlocal calls
        captured, signature, captured_at = original_acquire(*arguments)
        calls += 1
        damaged = bytearray(captured.read_bytes())
        damaged[2 * PAGE - 10] ^= 1
        captured.write_bytes(damaged)
        source.write_bytes(b"".join(encrypt(page, number)
                                   for number, page in enumerate(sqlite_pages("live write"), 1)))
        return captured, cipher._source_signature(captured), captured_at

    monkeypatch.setattr(cipher, "_acquire", acquire_then_corrupt)
    with pytest.raises(cipher.SnapshotError) as raised:
        cipher.snapshot(source, destination, KEY)
    assert calls == 1
    assert raised.value.code == "snapshot_validation_failed"
    assert raised.value.stage == "validation"
    assert raised.value.retryable is False
    assert destination.read_bytes() == old
    assert Path(str(destination) + ".snapshot.json").read_bytes() == old_stamp
    assert not list(destination.parent.glob("*.capture"))


def test_continuous_acquisition_mutation_has_typed_bounded_failure(tmp_path, monkeypatch):
    source, destination = prepare(tmp_path)
    cipher.snapshot(source, destination, KEY)
    old = destination.read_bytes()
    old_stamp = Path(str(destination) + ".snapshot.json").read_bytes()
    source.write_bytes(b"".join(encrypt(page, number)
                               for number, page in enumerate(sqlite_pages("refresh"), 1)))
    original_copy = cipher._copy_observed_file
    copies = 0

    def always_change(incoming, outgoing, expected, head_size):
        nonlocal copies
        original_copy(incoming, outgoing, expected, head_size)
        copies += 1
        source.write_bytes(b"".join(encrypt(page, number)
                                   for number, page in enumerate(sqlite_pages(f"write {copies}"), 1)))

    monkeypatch.setattr(cipher, "_copy_observed_file", always_change)
    with pytest.raises(cipher.SnapshotError) as raised:
        cipher.snapshot(source, destination, KEY)
    assert copies == 3
    assert raised.value.code == "snapshot_unstable"
    assert raised.value.stage == "acquisition"
    assert raised.value.retryable is True
    assert destination.read_bytes() == old
    assert Path(str(destination) + ".snapshot.json").read_bytes() == old_stamp
    assert not list(destination.parent.glob("*.capture"))
