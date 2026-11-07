import os
from pathlib import Path
import sys

import pytest

@pytest.mark.anyio
async def test_real_stdio_discovers_quiet_tools_without_private_configuration():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    root = Path(__file__).parents[1]
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONUTF8": "1",
           "STILLDESK_WECHAT_CONFIG": "", "WECHAT_BRIDGE_CONFIG": ""}
    parameters = StdioServerParameters(command=sys.executable,
        args=["-X", "utf8", "-m", "wechat_bridge.server"], cwd=str(root), env=env)
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert len(tools.tools) == 16
            status = await client.call_tool("wechat_status", {})
            assert status.structuredContent["configured"] is False
            assert status.structuredContent["capabilities"]["desktopFallback"] is False
            capabilities = await client.call_tool("wechat_capabilities", {})
            assert capabilities.structuredContent["sendText"]["supported"] is False

@pytest.fixture
def anyio_backend():
    return "asyncio"
