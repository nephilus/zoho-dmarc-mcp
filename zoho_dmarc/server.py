from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from starlette.responses import JSONResponse
import os
from typing import Any
from .config import database
from .queries import Queries

mcp = MCPServer("Zoho DMARC reports")
queries = Queries(database(), os.getenv("DMARC_REMEDIATION"))
readonly = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


@mcp.tool(annotations=readonly)
def dmarc_summary(start: str, end: str, page_size: int = 100, cursor: int = 0) -> dict[str, Any]:
    """Whole-report DMARC counts for explicit UTC bounds, at most 365 days. Includes freshness, observed coverage, periods and conflicts. Cursor is the last report ID."""
    return queries.summary(start, end, page_size, cursor)


@mcp.tool(annotations=readonly)
def dmarc_failures(start: str, end: str, page_size: int = 100, cursor: int = 0) -> dict[str, Any]:
    """DMARC aggregate rows failing both aligned SPF and DKIM, with explicit UTC bounds at most 365 days. Cursor is the last row ID."""
    return queries.failures(start, end, page_size, cursor)


@mcp.tool(annotations=readonly)
def dmarc_report_details(report_id: int, page_size: int = 100, cursor: int = 0) -> dict[str, Any]:
    """One parsed report, actual period, bounded aggregate rows and attachment provenance. Cursor is the last row ID."""
    return queries.details(report_id, page_size, cursor)


@mcp.tool(annotations=readonly)
def dmarc_health() -> dict[str, Any]:
    """Collection freshness, pending/rejected attachments, conflicts and backup status. Stale history remains queryable."""
    return queries.health()


@mcp.custom_route("/healthz", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "alive"})


@mcp.custom_route("/readyz", methods=["GET"])
async def ready(request):
    state = queries.health()
    return JSONResponse({"status": state["status"]}, status_code=503 if "reason" in state or state["status"] == "unsupported_schema" else 200)


def run():
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8000, stateless_http=True, json_response=True, max_request_body_size=16384)
