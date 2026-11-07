from __future__ import annotations

import json
from pathlib import Path

import pytest

from qq_bridge import source
from qq_bridge.cipher import SnapshotError
from qq_bridge.keys import DiscoveryError
from test_cipher import KEY, SALT, PAGE, QQ_HEADER, encrypt, prepare, sqlite_pages


def environment(tmp_path, monkeypatch, *, optional=()):
    database, _ = prepare(tmp_path)
    account = database.parent.parent
    executable = tmp_path / "client" / "QQ.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic; never executed")
    state = tmp_path / "bridge-private"
    config = state / "config.json"
    permissions = []

    def private(path):
        permissions.append(path.resolve())
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(source, "restrict_directory", private)
    for name in optional:
        database.with_name(name).write_bytes(database.read_bytes())
    return account, database, executable, state, config, permissions


def initialized(tmp_path, monkeypatch, *, optional=()):
    account, database, executable, state, config, permissions = environment(
        tmp_path, monkeypatch, optional=optional)
    source.initialize(account, executable, state, config)
    return source.LocalSource(config), database, permissions


def capture_mocks(monkeypatch, *, available_keys=None, failing_snapshot=(),
                  discovery=None, discovery_error=None):
    if available_keys is None:
        available_keys = {name: KEY + SALT for name in source.NAMES}
    snapshots, saved, discoveries = [], [], []
    monkeypatch.setattr(source, "load_keys", lambda *args: dict(available_keys))
    monkeypatch.setattr(source, "verify_key", lambda key, page: key == KEY + SALT)

    def discover(account, paths, executable):
        discoveries.append(paths)
        if discovery_error:
            raise discovery_error
        return (discovery or {}), {"verified_databases": len(discovery or {})}

    def snapshot(path, destination, key):
        snapshots.append(path.name)
        if path.name in failing_snapshot:
            raise SnapshotError("synthetic failed current capture")
        return {"validated": True, "path": str(destination), "refreshed": True,
                "captured_at": "2026-10-20T00:00:00Z", "wal_frames": 0,
                "coverage": {"scope": "synthetic unit fixture"}}

    monkeypatch.setattr(source, "discover_keys", discover)
    monkeypatch.setattr(source, "save_keys", lambda *args: saved.append(args))
    monkeypatch.setattr(source, "snapshot", snapshot)
    return snapshots, saved, discoveries


def test_initialize_and_reload_are_explicit_account_bound_and_idempotent(tmp_path, monkeypatch):
    account, database, executable, state, config, permissions = environment(tmp_path, monkeypatch)
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    first = source.initialize(account, executable, state, config)
    old_config = config.read_bytes()
    second = source.initialize(account, executable, state, config)
    assert first == second
    assert len(first["account_tag"]) == 16
    assert config.read_bytes() == old_config
    local = source.LocalSource(config)
    assert local.account == account
    assert local.root == database.parent
    assert local.key_path.parent == state
    assert local.status()["platform"] == "qq"
    assert local.status()["coverage"]["complete_history"] is False
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before
    assert all(not item.is_relative_to(account) for item in permissions)


@pytest.mark.parametrize("kind", ["same", "inside", "ancestor", "config-in-source", "config-outside-state"])
def test_initialization_rejects_source_cache_overlap_before_any_write(tmp_path, monkeypatch, kind):
    account, database, executable, state, config, permissions = environment(tmp_path, monkeypatch)
    if kind == "same":
        state, config = account, account / "config.json"
    elif kind == "inside":
        state, config = account / "bridge", account / "bridge" / "config.json"
    elif kind == "ancestor":
        state, config = account.parent, account.parent / "config.json"
    elif kind == "config-in-source":
        config = account / "config.json"
    else:
        config = tmp_path / "outside" / "config.json"
    before = database.read_bytes()
    with pytest.raises(ValueError, match="重叠|缓存目录内|包含"):
        source.initialize(account, executable, state, config)
    assert permissions == []
    assert database.read_bytes() == before
    assert not config.exists()


@pytest.mark.parametrize("relation", ["inside", "ancestor"])
def test_reload_rejects_tampered_configuration_with_overlapping_cache(tmp_path, monkeypatch, relation):
    account, database, executable, state, config, permissions = environment(tmp_path, monkeypatch)
    source.initialize(account, executable, state, config)
    obj = json.loads(config.read_text(encoding="utf-8"))
    obj["state_dir"] = str(account if relation == "inside" else account.parent)
    config.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="包含|重叠"):
        source.LocalSource(config)


def test_existing_binding_cannot_be_replaced_by_another_account(tmp_path, monkeypatch):
    account, database, executable, state, config, permissions = environment(tmp_path, monkeypatch)
    source.initialize(account, executable, state, config)
    original = config.read_bytes()
    other = tmp_path / "other-account"
    (other / "nt_db").mkdir(parents=True)
    (other / "nt_db" / "nt_msg.db").write_bytes(database.read_bytes())
    with pytest.raises(ValueError, match="另一账号"):
        source.initialize(other, executable, state, config)
    assert config.read_bytes() == original


@pytest.mark.parametrize("change", [{"platform": "wechat"}, {"version": 999}])
def test_reload_refuses_wrong_platform_or_configuration_version(tmp_path, monkeypatch, change):
    local, _, _ = initialized(tmp_path, monkeypatch)
    obj = json.loads(local.config_path.read_text(encoding="utf-8")) | change
    local.config_path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="支持的 QQ"):
        source.LocalSource(local.config_path)


def test_status_reports_missing_main_and_optional_client_version(tmp_path, monkeypatch):
    local, database, _ = initialized(tmp_path, monkeypatch)
    versions = local.executable.parent / "versions"
    versions.mkdir()
    (versions / "config.json").write_text('{"curVersion":"fixture-version"}')
    assert local.status()["client_version"] == "fixture-version"
    database.unlink()
    status = local.status()
    assert not status["source_available"]
    assert status["coverage"]["cross_database_atomic"] is False


def test_optional_snapshot_failure_does_not_discard_authenticated_main(tmp_path, monkeypatch):
    local, _, _ = initialized(tmp_path, monkeypatch, optional=source.NAMES[1:])
    snapshots, saved, discoveries = capture_mocks(monkeypatch, failing_snapshot=("group_info.db",))
    result = local.capture()
    assert set(result["snapshots"]) == {"nt_msg.db", "profile_info.db"}
    assert {row["database"] for row in result["records"]} == set(result["snapshots"])
    assert result["coverage"]["optional_unavailable"] == [
        {"database": "group_info.db", "status": "unavailable"}]
    assert saved == [] and discoveries == []


def test_optional_key_discovery_failure_does_not_block_valid_main(tmp_path, monkeypatch):
    local, _, _ = initialized(tmp_path, monkeypatch, optional=source.NAMES[1:])
    snapshots, saved, discoveries = capture_mocks(
        monkeypatch, available_keys={"nt_msg.db": KEY + SALT},
        discovery_error=DiscoveryError("synthetic QQ unavailable for optional names"))
    result = local.capture()
    assert list(result["snapshots"]) == ["nt_msg.db"]
    assert snapshots == ["nt_msg.db"]
    assert saved == []
    assert len(discoveries) == 1
    assert result["key_discovery"]["optional_key_discovery"] == "unavailable"
    assert {row["database"] for row in result["coverage"]["optional_unavailable"]} == set(source.NAMES[1:])


def test_optional_page_read_failure_does_not_block_valid_main(tmp_path, monkeypatch):
    local, database, _ = initialized(tmp_path, monkeypatch, optional=("group_info.db",))
    capture_mocks(monkeypatch)
    original = Path.open

    def denied(path, *args, **kwargs):
        if path == database.with_name("group_info.db"):
            raise PermissionError("synthetic optional name database unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    result = local.capture()
    assert list(result["snapshots"]) == ["nt_msg.db"]
    assert any(row["database"] == "group_info.db" for row in result["coverage"]["optional_unavailable"])


def test_missing_or_header_only_optional_databases_are_explicit(tmp_path, monkeypatch):
    local, database, _ = initialized(tmp_path, monkeypatch)
    database.with_name("profile_info.db").write_bytes(QQ_HEADER)
    capture_mocks(monkeypatch)
    result = local.capture()
    assert set(result["coverage"]["databases"]) == {"nt_msg.db"}
    assert result["coverage"]["optional_unavailable"] == [
        {"database": name, "status": "unavailable"} for name in source.NAMES[1:]]


def test_main_snapshot_failure_raises_instead_of_returning_last_capture(tmp_path, monkeypatch):
    local, _, _ = initialized(tmp_path, monkeypatch)
    capture_mocks(monkeypatch)
    old = local.capture()
    capture_mocks(monkeypatch, failing_snapshot=("nt_msg.db",))
    with pytest.raises(SnapshotError, match="failed current capture"):
        local.capture()
    assert local.last_capture is old


def test_missing_main_never_attempts_key_discovery_or_snapshot(tmp_path, monkeypatch):
    local, database, _ = initialized(tmp_path, monkeypatch)
    database.unlink()
    snapshots, saved, discoveries = capture_mocks(monkeypatch)
    with pytest.raises(FileNotFoundError):
        local.capture()
    assert snapshots == [] and saved == [] and discoveries == []


def test_main_key_discovery_failure_is_fatal_and_keeps_previous_keys(tmp_path, monkeypatch):
    local, _, _ = initialized(tmp_path, monkeypatch)
    snapshots, saved, discoveries = capture_mocks(
        monkeypatch, available_keys={}, discovery_error=DiscoveryError("no authenticated main key"))
    with pytest.raises(DiscoveryError, match="authenticated main key"):
        local.capture()
    assert snapshots == [] and saved == []
    assert len(discoveries) == 1


def test_discovered_keys_are_saved_only_after_account_scoped_discovery(tmp_path, monkeypatch):
    local, _, _ = initialized(tmp_path, monkeypatch)
    snapshots, saved, discoveries = capture_mocks(
        monkeypatch, available_keys={}, discovery={"nt_msg.db": KEY + SALT})
    result = local.capture()
    assert list(result["snapshots"]) == ["nt_msg.db"]
    assert len(saved) == 1
    assert saved[0] == (local.key_path, local.account, {"nt_msg.db": KEY + SALT})
    assert [path.name for path in discoveries[0]] == ["nt_msg.db"]


@pytest.mark.parametrize("kind", ["nt_db", "main-file"])
def test_database_redirect_outside_bound_account_is_rejected(tmp_path, monkeypatch, kind):
    local, database, _ = initialized(tmp_path, monkeypatch)
    foreign = tmp_path / "foreign-data"
    foreign.mkdir()
    target = foreign / "nt_msg.db"
    target.write_bytes(database.read_bytes())
    database.unlink()
    if kind == "nt_db":
        local.root.rmdir()
        link, link_target = local.root, foreign
    else:
        link, link_target = database, target
    try:
        link.symlink_to(link_target, target_is_directory=(kind == "nt_db"))
    except OSError:
        pytest.skip("Windows symlink permission unavailable for synthetic isolation check")
    snapshots, saved, discoveries = capture_mocks(monkeypatch)
    with pytest.raises(ValueError, match="重定向"):
        if kind == "nt_db":
            source.LocalSource(local.config_path)
        else:
            local.capture()
    assert snapshots == [] and saved == [] and discoveries == []
