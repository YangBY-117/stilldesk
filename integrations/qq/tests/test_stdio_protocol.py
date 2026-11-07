import os
from pathlib import Path
import sys

import pytest

@pytest.mark.anyio
async def test_real_stdio_discovers_background_read_tools_without_an_account():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    root = Path(__file__).parents[1]
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONUTF8": "1",
           "STILLDESK_QQ_CONFIG": "", "QQ_BRIDGE_CONFIG": ""}
    parameters = StdioServerParameters(command=sys.executable,
        args=["-X", "utf8", "-m", "qq_bridge.server"], cwd=str(root), env=env)
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert len(tools.tools) == 9
            status = await client.call_tool("qq_status", {})
            assert status.structuredContent["configured"] is False
            caps = await client.call_tool("qq_capabilities", {})
            assert caps.structuredContent["foregroundRequired"] is False
            assert caps.structuredContent["sendMessages"]["supported"] is False
