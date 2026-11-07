"""Public recovery categories do not expose private exception text or SQL."""
import json
from types import SimpleNamespace

import pytest

from wechat_bridge.errors import ChatResolutionError, CursorError
from wechat_bridge.server import create_server


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("candidates,code", [([], "chat_not_found"),
    ([{"id": "chat-one", "name": "同名"}, {"id": "chat-two", "name": "同名"}], "ambiguous_chat")])
async def test_chat_resolution_has_actionable_candidates(tmp_path, candidates, code):
    def fail(**kwargs):
        raise ChatResolutionError(candidates)
    app = create_server(reader=SimpleNamespace(state=tmp_path, get_messages=fail))
    result = (await app.call_tool("wechat_get_messages", {"chat": "fragment"}))[1]
    assert result["error"]["code"] == code
    assert result["error"]["stage"] == "chat_resolution"
    assert result["error"]["details"]["candidates"] == candidates
    assert result["error"]["details"]["next_tool"] == "wechat_list_chats"
    assert "CLI sync" not in result["error"]["message"]


@pytest.mark.anyio
async def test_search_cursor_recovery_has_own_category(tmp_path):
    def fail(**kwargs):
        raise CursorError("search")
    app = create_server(reader=SimpleNamespace(state=tmp_path, search_messages=fail))
    result = (await app.call_tool("wechat_search_messages", {
        "query": "材料", "cursor": "PRIVATE_CURSOR_SENTINEL", "refresh": False,
    }))[1]
    assert result["error"]["code"] == "invalid_search_cursor"
    assert "query" in result["error"]["message"]
    assert "PRIVATE_CURSOR_SENTINEL" not in json.dumps(result)
