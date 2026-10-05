"""Run inside the MCP container to verify the deployed hello transport."""

import asyncio
import json

from mcp import Client


async def main():
    async with Client("http://127.0.0.1:8000/mcp") as client:
        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == ["dmarc_hello"]
        assert tools.tools[0].annotations.read_only_hint is True
        result = await client.call_tool("dmarc_hello", {})
        assert not result.is_error
        assert result.structured_content["stage"] == "connectivity-proof"
        print(json.dumps(result.structured_content))


asyncio.run(main())
