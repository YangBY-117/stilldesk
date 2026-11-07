"""The absence of a native provider must never turn into a desktop action."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

from wechat_bridge.actions import BridgeActions
from wechat_bridge import session
from wechat_bridge.capabilities import capabilities

class PoisonReader:
    def __getattr__(self, name):
        raise AssertionError("Real-send rejection must precede account, file or UI access: " + name)

@pytest.mark.parametrize("operation", ["text", "file", "image", "video"])
def test_real_send_never_touches_account_files_or_ui(operation, monkeypatch):
    monkeypatch.setitem(sys.modules, "uiautomation", None)
    actions = BridgeActions(PoisonReader())
    result = (actions.send_text("selected-id", "hello", dry_run=False) if operation == "text"
              else actions.send_media("selected-id", "/file-does-not-exist", operation, dry_run=False))
    assert result["error"]["code"] == "background_backend_unavailable"
    assert result["attempted"] is False and result["foregroundRequired"] is False
    assert result["delivered"] is None

def test_legacy_desktop_flag_is_rejected_before_source_access():
    result = BridgeActions(PoisonReader()).download_attachment("selected-id", allow_desktop=True)
    assert result["error"]["code"] == "foreground_operation_forbidden"
    assert result["attempted"] is False

def test_no_foreground_adapter_is_shipped():
    package = Path(__file__).parents[1] / "wechat_bridge"
    assert not any((package / name).exists() for name in ["desktop.py", "accessibility.py", "editor_watch.py"])
    assert capabilities()["desktopFallback"] is False

def test_existing_tray_client_is_reused_without_uia(tmp_path, monkeypatch):
    exe, account = tmp_path / "Weixin.exe", tmp_path / "selected"
    exe.write_bytes(b"synthetic never executed")
    account.mkdir()
    process = SimpleNamespace(pid=4242, info={"exe": str(exe), "name": "Weixin.exe"},
                              open_files=lambda: [SimpleNamespace(path=str(account / "db_storage/message/message_0.db"))])
    monkeypatch.setattr(session.psutil, "process_iter", lambda _: iter([process]))
    monkeypatch.setattr(session, "file_version", lambda _: "4.2.0.11")
    monkeypatch.setitem(sys.modules, "uiautomation", None)
    result = session.inspect_session(exe, account)
    assert result["account_bound"] is True and result["reuse_existing_client"] is True
    assert result["relogin_recommended"] is False and result["login_state"] == "unknown"
    assert "selected" not in json.dumps(result)

@pytest.mark.anyio
async def test_mcp_starts_and_reports_capabilities_without_an_account():
    from wechat_bridge.server import create_server
    app = create_server()
    names = {tool.name for tool in await app.list_tools()}
    assert {"wechat_capabilities", "wechat_get_message", "wechat_search_messages"} <= names
    content, structured = await app.call_tool("wechat_capabilities", {})
    assert structured["foregroundRequired"] is False
    content, structured = await app.call_tool("wechat_status", {})
    assert structured["configured"] is False

@pytest.fixture
def anyio_backend():
    return "asyncio"
