from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechat_bridge import runtime
from wechat_bridge.cipher import SnapshotError
from wechat_bridge.errors import CursorError
from wechat_bridge.server import create_server


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_same_version_source_edit_requires_reconnect(tmp_path, monkeypatch):
    (tmp_path / "__init__.py").write_text(f'__version__ = "{runtime.__version__}"\n')
    module = tmp_path / "reader.py"
    module.write_text("implementation = 1\n")
    monkeypatch.setattr(runtime, "_PACKAGE", tmp_path)
    monkeypatch.setattr(runtime, "_STARTUP_SOURCE", runtime.source_identity(tmp_path))
    assert runtime.status()["restart_required"] is False
    module.write_text("implementation = 2\n")
    report = runtime.status()
    assert report["restart_required"] is True
    assert report["loaded_version"] == report["installed_version"]
    assert report["startup_source_sha256"] != report["installed_source_sha256"]


def test_unavailable_code_identity_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_PACKAGE", tmp_path / "missing")
    report = runtime.status()
    assert report["source_identity_available"] is False
    assert report["restart_required"] is None


def test_diagnostic_never_records_exception_content(tmp_path):
    try:
        raise RuntimeError("PRIVATE_SQL_PATH_KEY_SENTINEL")
    except RuntimeError as error:
        runtime.record_error(tmp_path, "sync", error, code="reader_error")
    raw = (tmp_path / "last_mcp_error.json").read_text()
    report = json.loads(raw)
    assert "PRIVATE_SQL_PATH_KEY_SENTINEL" not in raw
    assert report["observed_at"] and report["runtime"]["pid"]
    assert report["frames"][-1]["function"] == "test_diagnostic_never_records_exception_content"
    assert not list(tmp_path.glob(".mcp-error-*.tmp"))


@pytest.mark.anyio
@pytest.mark.parametrize("code,stage,retryable", [
    ("snapshot_unstable", "acquisition", True),
    ("snapshot_validation_failed", "validation", False),
    ("snapshot_io_error", "publish", False),
])
async def test_snapshot_failure_is_typed_and_content_free(tmp_path, code, stage, retryable):
    def fail():
        raise SnapshotError("PRIVATE_SOURCE_SENTINEL", code=code, stage=stage, retryable=retryable)
    app = create_server(reader=SimpleNamespace(state=tmp_path, sync=fail))
    result = await app.call_tool("wechat_sync", {})
    # FastMCP returns (content, structured output) for dictionary tools.
    payload = result[1]
    assert payload["error"]["code"] == code
    assert payload["error"]["stage"] == stage
    assert payload["error"]["retryable"] is retryable
    assert "PRIVATE_SOURCE_SENTINEL" not in json.dumps(result, default=str)
    diagnostic = json.loads((tmp_path / "last_mcp_error.json").read_text())
    assert diagnostic["code"] == code
    assert "PRIVATE_SOURCE_SENTINEL" not in json.dumps(diagnostic)


@pytest.mark.anyio
async def test_status_exposes_running_and_installed_identity(tmp_path):
    app = create_server(reader=SimpleNamespace(state=tmp_path, status=lambda: {"version": "test"}))
    result = await app.call_tool("wechat_status", {})
    assert result[1]["runtime"]["restart_required"] is False
    assert result[1]["runtime"]["loaded_version"] == runtime.__version__


@pytest.mark.anyio
@pytest.mark.parametrize("kind,code,hint", [
    ("history", "invalid_history_cursor", "after"),
    ("changes", "invalid_changes_cursor", "不会补读旧消息"),
])
async def test_cursor_error_explains_recovery_without_echoing_input(tmp_path, kind, code, hint):
    def fail(**kwargs):
        raise CursorError(kind)
    reader = SimpleNamespace(state=tmp_path, get_messages=fail, get_changes=fail)
    app = create_server(reader=reader)
    tool = "wechat_get_messages" if kind == "history" else "wechat_get_changes"
    args = {"chat": "chat-1"} if kind == "history" else {}
    args["cursor"] = "PRIVATE_CURSOR_SENTINEL"
    result = await app.call_tool(tool, args)
    assert result[1]["error"]["code"] == code
    assert hint in result[1]["error"]["message"]
    assert "PRIVATE_CURSOR_SENTINEL" not in json.dumps(result, default=str)
