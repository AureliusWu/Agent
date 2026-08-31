"""Named MCP Streamable HTTP transport entry point.

The v15 HTTP implementation accepts both JSON and event-stream response media
types.  This alias lets callers select the protocol by its specification name
without duplicating lifecycle code.
"""

from app.mcp.transports.http import HttpMcpTransport


StreamableHttpMcpTransport = HttpMcpTransport

__all__ = ["StreamableHttpMcpTransport"]
