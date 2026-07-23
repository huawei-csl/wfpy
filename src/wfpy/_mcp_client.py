"""
Native MCP client lifecycle for ``stdio`` and ``http`` transports.

Uses the ``mcp`` Python package (optional — only loaded when needed).
Falls back gracefully when the package is not installed.

Note: Each public coroutine (``list_mcp_tools``, ``call_mcp_tool``) creates
a fresh connection and tears it down before returning.  This avoids stale-pool
bugs when the caller wraps each invocation in a separate ``asyncio.run()``.
"""
from __future__ import annotations

import dataclasses
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

logger = logging.getLogger("wfpy.mcp_client")

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class McpToolCallResult:
    ok: bool
    result: str = ""
    error: str = ""


@dataclasses.dataclass
class McpDiscoveredTool:
    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Ephemeral connection helper
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _connect(
    server_name: str,
    transport: str,
    command: str = "",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    url: str = "",
) -> AsyncIterator[Any]:
    """Open a short-lived ``ClientSession`` to an MCP server.

    Yields the initialised ``ClientSession``.  The transport and session are
    torn down when the context manager exits, so callers never have to worry
    about stale connections or cross-event-loop issues.
    """
    try:
        from mcp import ClientSession
    except ImportError as exc:
        raise RuntimeError(
            "The 'mcp' package is required for native MCP transport. "
            "Install it with: pip install 'wfpy[agent]'"
        ) from exc

    if transport == "stdio":
        from mcp.client.stdio import stdio_client, StdioServerParameters
        if not command:
            raise RuntimeError(
                f"MCP server '{server_name}' (stdio) requires a non-empty 'command'."
            )
        import os as _os
        merged_env = {**_os.environ, **(env or {})} if env else None
        server_params = StdioServerParameters(
            command=command,
            args=args or [],
            env=merged_env,
        )
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
        return

    if transport == "http":
        from mcp.client.sse import sse_client
        if not url:
            raise RuntimeError(
                f"MCP server '{server_name}' (http) requires a non-empty 'url'."
            )
        async with sse_client(url=url) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
        return

    if transport == "streamable-http":
        if not url:
            raise RuntimeError(
                f"MCP server '{server_name}' (streamable-http) requires a non-empty 'url'."
            )
        try:
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError:
            raise RuntimeError(
                f"MCP server '{server_name}' uses 'streamable-http' transport but "
                "the installed mcp package does not support it. "
                "Upgrade with: pip install --upgrade mcp"
            )
        async with streamablehttp_client(url=url) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
        return

    raise RuntimeError(
        f"MCP server '{server_name}' has transport '{transport}' which is not "
        "a native SDK transport (stdio, http, streamable-http). Use bridge dispatch instead."
    )


# ---------------------------------------------------------------------------
# Tool invocation
# ---------------------------------------------------------------------------


async def call_mcp_tool(
    server_name: str,
    transport: str,
    tool_name: str,
    arguments: dict[str, Any],
    command: str = "",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    url: str = "",
    timeout_ms: int = 30_000,
) -> McpToolCallResult:
    """Call a tool via the native MCP SDK client."""
    try:
        async with _connect(server_name, transport, command, args, env, url) as session:
            result = await session.call_tool(tool_name, arguments=arguments)

            # result.content is a list of content blocks
            texts: list[str] = []
            for block in getattr(result, "content", []):
                if getattr(block, "type", None) == "text":
                    texts.append(getattr(block, "text", ""))
            text = "\n".join(texts)

            is_error = getattr(result, "isError", False)
            return McpToolCallResult(
                ok=not is_error,
                result="" if is_error else text,
                error=text if is_error else "",
            )
    except Exception as exc:
        return McpToolCallResult(ok=False, error=str(exc))


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------


async def list_mcp_tools(
    server_name: str,
    transport: str,
    command: str = "",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    url: str = "",
) -> list[McpDiscoveredTool]:
    """List all tools exposed by an MCP server via ``tools/list``."""
    async with _connect(server_name, transport, command, args, env, url) as session:
        result = await session.list_tools()
        tools: list[McpDiscoveredTool] = []
        for t in getattr(result, "tools", []):
            tools.append(McpDiscoveredTool(
                name=getattr(t, "name", ""),
                description=getattr(t, "description", ""),
                input_schema=getattr(t, "inputSchema", None),
            ))
        return tools


# ---------------------------------------------------------------------------
# Cleanup (no-op — connections are ephemeral now)
# ---------------------------------------------------------------------------


async def close_all_mcp_clients() -> None:
    """No-op — kept for backwards compatibility.

    Connections are now ephemeral (opened and closed per call), so there is
    nothing to clean up.
    """
    pass
