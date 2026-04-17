"""MCP (Model Context Protocol) layer for Hope."""

from hope.mcp.client import MCPClient
from hope.mcp.protocol import MCPError, MCPNotification, MCPRequest, MCPResponse
from hope.mcp.server import MCPServer
from hope.mcp.transport import (
    InProcessTransport,
    MCPTransport,
    SSETransport,
    StdioTransport,
    StreamableHTTPTransport,
)

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPNotification",
    "MCPRequest",
    "MCPResponse",
    "MCPServer",
    "MCPTransport",
    "InProcessTransport",
    "SSETransport",
    "StdioTransport",
    "StreamableHTTPTransport",
]
