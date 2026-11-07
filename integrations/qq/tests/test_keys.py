from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from qq_bridge import cipher, keys
from test_cipher import KEY, SALT, PAGE, QQ_HEADER, encrypt, prepare, sqlite_pages


def bound_paths(tmp_path):
    database, _ = prepare(tmp_path)
    executable = tmp_path / "client" / "QQ.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable marker; never run")
    return database.parent.parent, database, executable


def proc(pid, executable, files, *, name="QQ.exe", rss=100):
    return SimpleNamespace(
        pid=pid,
        info={"pid": pid, "name": name, "exe": str(executable),
              "memory_info": SimpleNamespace(rss=rss)},
        open_files=lambda: [SimpleNamespace(path=str(path)) for path in files],
    )


def memory_reader(monkeypatch, data_by_pid):
    closed, reads = [], []

    class Reader:
        def __init__(self, pid):
            self.pid = pid
            self.bytes_read = 0

        def blocks(self):
            for data in data_by_pid[self.pid]:
                self.bytes_read += len(data)
                reads.append((self.pid, data))
                yield 0x10000, data

        def close(self):
            closed.append(self.pid)

    monkeypatch.setattr(keys, "_ReadProcess", Reader)
    return closed, reads


def spec(value: bytes = KEY + SALT) -> bytes:
    return b"x'" + value.hex().encode("ascii") + b"'"


def test_missing_key_cache_requires_no_dpapi(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("missing cache must not invoke DPAPI")

    monkeypatch.setattr(keys, "_dpapi", forbidden)
    assert keys.load_keys(tmp_path / "missing.dpapi", tmp_path) == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI current-user integration")
def test_current_user_dpapi_roundtrip_is_encrypted_and_account_bound(tmp_path):
    account, database, executable = bound_paths(tmp_path)
    path = tmp_path / "keys.dpapi"
    mapping = {"nt_msg.db": KEY + SALT}
    keys.save_keys(path, account, mapping)
    raw = path.read_bytes()
    assert (KEY + SALT).hex().encode() not in raw
    assert b'"account"' not in raw and b'"keys"' not in raw
    assert keys.load_keys(path, account / ".") == mapping
    other = tmp_path / "another-account"
    other.mkdir()
    with pytest.raises(keys.DiscoveryError, match="账号不匹配"):
        keys.load_keys(path, other)
    assert path.read_bytes() == raw


def test_empty_save_cannot_overwrite_existing_cache(tmp_path):
    path = tmp_path / "keys.dpapi"
    path.write_bytes(b"previous encrypted cache")
    with pytest.raises(keys.DiscoveryError, match="拒绝覆盖"):
        keys.save_keys(path, tmp_path, {})
    assert path.read_bytes() == b"previous encrypted cache"


def test_dpapi_failure_preserves_previous_cache_and_does_not_expose_key(tmp_path, monkeypatch):
    path = tmp_path / "keys.dpapi"
    path.write_bytes(b"previous")

    def denied(*args, **kwargs):
        raise keys.DiscoveryError("synthetic different Windows user denied")

    monkeypatch.setattr(keys, "_dpapi", denied)
    with pytest.raises(keys.DiscoveryError, match="different Windows user") as error:
        keys.save_keys(path, tmp_path, {"nt_msg.db": KEY + SALT})
    assert KEY.hex() not in str(error.value)
    assert path.read_bytes() == b"previous"
    with pytest.raises(keys.DiscoveryError, match="different Windows user"):
        keys.load_keys(path, tmp_path)


def test_failed_key_publication_does_not_replace_previous_ciphertext(tmp_path, monkeypatch):
    path = tmp_path / "keys.dpapi"
    path.write_bytes(b"previous")
    monkeypatch.setattr(keys, "_dpapi", lambda data, decrypt=False: b"encrypted replacement")

    def failed(*args):
        raise OSError("synthetic atomic publication failure")

    monkeypatch.setattr(keys.os, "replace", failed)
    with pytest.raises(OSError, match="publication failure"):
        keys.save_keys(path, tmp_path, {"nt_msg.db": KEY + SALT})
    assert path.read_bytes() == b"previous"


def test_discovery_selects_executable_account_and_authenticates_pages(tmp_path, monkeypatch):
    account, database, executable = bound_paths(tmp_path)
    processes = [proc(1, executable.with_name("other.exe"), [database]),
                 proc(2, executable, [tmp_path / "another-account" / "nt_db" / "nt_msg.db"]),
                 proc(3, executable, [database], name="unrelated.exe"),
                 proc(4, executable, [database])]
    monkeypatch.setattr(keys.psutil, "process_iter", lambda attrs: iter(processes))
    bad_key = bytes([255]) * 32 + SALT
    bad_salt = KEY + bytes([255]) * 16
    closed, reads = memory_reader(monkeypatch, {4: [spec(KEY), spec(bad_salt) + spec(bad_key),
                                                  spec() + spec()]})
    recovered, metrics = keys.discover_keys(account, [database], executable)
    assert recovered == {"nt_msg.db": KEY + SALT}
    assert closed == [4]
    assert [pid for pid, _ in reads] == [4, 4, 4]
    assert metrics["verified_databases"] == 1
    assert metrics["processes_examined"] == 1
    assert metrics["mode"] == "PROCESS_VM_READ|PROCESS_QUERY_INFORMATION"
    assert metrics["bounded"] is True
    assert (KEY + SALT).hex() not in json.dumps(metrics)
    assert str(account) not in json.dumps(metrics)


@pytest.mark.parametrize("hex_length", [65, 95, 97, 191])
def test_odd_length_unrelated_keyspectext_cannot_abort_discovery(tmp_path, monkeypatch, hex_length):
    account, database, executable = bound_paths(tmp_path)
    monkeypatch.setattr(keys.psutil, "process_iter", lambda attrs: iter([proc(1, executable, [database])]))
    noise = b"x'" + b"a" * hex_length + b"'"
    closed, _ = memory_reader(monkeypatch, {1: [noise + b" unrelated " + spec()]})
    recovered, metrics = keys.discover_keys(account, [database], executable)
    assert recovered == {"nt_msg.db": KEY + SALT}
    assert closed == [1]


def test_foreign_database_path_is_rejected_before_inspecting_processes(tmp_path, monkeypatch):
    account, database, executable = bound_paths(tmp_path)
    foreign = tmp_path / "foreign.db"
    foreign.write_bytes(database.read_bytes())

    def forbidden(*args):
        pytest.fail("foreign source must be rejected before process inventory")

    monkeypatch.setattr(keys.psutil, "process_iter", forbidden)
    with pytest.raises(keys.DiscoveryError, match="已绑定"):
        keys.discover_keys(account, [foreign], executable)


def test_other_account_process_is_not_read(tmp_path, monkeypatch):
    account, database, executable = bound_paths(tmp_path)
    foreign = tmp_path / "foreign-account" / "nt_db" / "nt_msg.db"
    monkeypatch.setattr(keys.psutil, "process_iter", lambda attrs: iter([proc(1, executable, [foreign])]))

    def forbidden(pid):
        pytest.fail("another account process must not be opened")

    monkeypatch.setattr(keys, "_ReadProcess", forbidden)
    with pytest.raises(keys.DiscoveryError, match="所选 QQ 账号"):
        keys.discover_keys(account, [database], executable)


def test_memory_scan_stops_at_byte_budget_and_closes_handle(tmp_path, monkeypatch):
    account, database, executable = bound_paths(tmp_path)
    monkeypatch.setattr(keys.psutil, "process_iter", lambda attrs: iter([proc(1, executable, [database])]))
    noise = b"not a key" * 30
    closed, reads = memory_reader(monkeypatch, {1: [noise, spec()]})
    with pytest.raises(keys.DiscoveryError, match="保留已有缓存"):
        keys.discover_keys(account, [database], executable, max_read_bytes=len(noise))
    assert len(reads) == 1
    assert closed == [1]


def test_memory_scan_stops_at_timeout_and_closes_handle(tmp_path, monkeypatch):
    account, database, executable = bound_paths(tmp_path)
    monkeypatch.setattr(keys.psutil, "process_iter", lambda attrs: iter([proc(1, executable, [database])]))
    ticks = iter([0.0, 1.0, 1.0, 1.0])
    monkeypatch.setattr(keys.time, "monotonic", lambda: next(ticks))
    closed, reads = memory_reader(monkeypatch, {1: [b"noise", spec()]})
    with pytest.raises(keys.DiscoveryError, match="保留已有缓存"):
        keys.discover_keys(account, [database], executable, timeout=0.5)
    assert len(reads) == 1
    assert closed == [1]


def test_partial_recovery_is_reported_without_claiming_all_databases(tmp_path, monkeypatch):
    account, database, executable = bound_paths(tmp_path)
    optional = database.with_name("group_info.db")
    other_salt = bytes(range(128, 144))
    optional.write_bytes(QQ_HEADER + encrypt(sqlite_pages()[0], 1,
                                            key=bytes([100]) * 32, salt=other_salt))
    monkeypatch.setattr(keys.psutil, "process_iter", lambda attrs: iter([proc(1, executable, [database])]))
    closed, _ = memory_reader(monkeypatch, {1: [spec()]})
    recovered, metrics = keys.discover_keys(account, [database, optional], executable)
    assert recovered == {"nt_msg.db": KEY + SALT}
    assert metrics["verified_databases"] == 1
    assert closed == [1]


@pytest.mark.skipif(os.name != "nt", reason="Windows process permission API")
def test_process_open_requests_only_read_and_query_permissions(monkeypatch):
    calls = []

    class Function:
        def __init__(self, name, result=True):
            self.name, self.result = name, result

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.result

    native = SimpleNamespace(OpenProcess=Function("OpenProcess", 123),
                             CloseHandle=Function("CloseHandle"),
                             VirtualQueryEx=Function("VirtualQueryEx"),
                             ReadProcessMemory=Function("ReadProcessMemory"))
    monkeypatch.setattr(keys.ctypes, "WinDLL", lambda *args, **kwargs: native)
    reader = keys._ReadProcess(321)
    reader.close()
    assert calls == [("OpenProcess", (0x0410, False, 321)), ("CloseHandle", (123,))]
