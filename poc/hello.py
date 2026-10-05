"""Non-secret connectivity proof. No mailbox or database access."""

from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from starlette.responses import JSONResponse

mcp = MCPServer("Zoho DMARC connectivity proof")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False))
def dmarc_hello() -> dict[str, str]:
    """Confirm this private MCP server is reachable. Contains no report data."""
    return {"status": "ok", "service": "zoho-dmarc-mcp", "stage": "connectivity-proof"}


@mcp.custom_route("/healthz", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http", host="127.0.0.1", port=8000,
        stateless_http=True, json_response=True, max_request_body_size=16384,
    )
