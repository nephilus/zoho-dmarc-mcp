import asyncio
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

from mcp import Client


def test_streamable_http_discovery_and_read_only_hello():
    root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen([sys.executable, str(root / "poc/hello.py")], cwd=root)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen("http://127.0.0.1:8000/healthz", timeout=1) as response:
                    assert response.status == 200
                break
            except URLError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("Hello server did not become healthy")
                time.sleep(0.1)

        async def check():
            async with Client("http://127.0.0.1:8000/mcp") as client:
                tools = await client.list_tools()
                assert [tool.name for tool in tools.tools] == ["dmarc_hello"]
                assert tools.tools[0].annotations.read_only_hint is True
                result = await client.call_tool("dmarc_hello", {})
                assert not result.is_error
                assert result.structured_content["stage"] == "connectivity-proof"

        asyncio.run(check())
    finally:
        process.terminate()
        process.wait(timeout=10)
