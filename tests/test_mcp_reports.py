import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
from mcp import Client
from zoho_dmarc.parser import parse
from zoho_dmarc.storage import Writer
from test_reports import XML, enqueue


def test_real_transport_reporting_schema_and_bounds(tmp_path):
    path = tmp_path/'history.sqlite'
    writer = Writer(path)
    writer.ingest(enqueue(writer), parse(XML, ('example.test',)))
    root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen([sys.executable, '-m', 'zoho_dmarc', 'server'], cwd=root, env={**os.environ, 'DMARC_DATABASE': str(path)})
    try:
        deadline = time.monotonic()+15
        while True:
            try:
                with urlopen('http://127.0.0.1:8000/readyz', timeout=1) as response:
                    assert response.status == 200
                break
            except URLError:
                if process.poll() is not None or time.monotonic()>deadline:
                    raise AssertionError('Reporting MCP did not become ready')
                time.sleep(.1)
        async def check():
            async with Client('http://127.0.0.1:8000/mcp') as client:
                tools = (await client.list_tools()).tools
                assert {tool.name for tool in tools} == {'dmarc_summary','dmarc_failures','dmarc_report_details','dmarc_health'}
                assert all(tool.annotations.read_only_hint and tool.output_schema for tool in tools)
                args = {'start':'2024-10-04T00:00:00Z', 'end':'2024-10-05T00:00:00Z'}
                summary = await client.call_tool('dmarc_summary', args)
                assert not summary.is_error
                assert summary.structured_content['totals']['messages'] == 12
                failure = await client.call_tool('dmarc_failures', args)
                assert failure.structured_content['failures'][0]['count'] == 12
                details = await client.call_tool('dmarc_report_details', {'report_id':1})
                assert len(details.structured_content['rows']) == 1
                health = await client.call_tool('dmarc_health', {})
                assert health.structured_content['status']=='never_initialized'
                invalid = await client.call_tool('dmarc_summary', {**args,'start':'2024-10-04'})
                assert invalid.is_error
        asyncio.run(check())
    finally:
        process.terminate()
        process.wait(timeout=10)
        writer.close()
