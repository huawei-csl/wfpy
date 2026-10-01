"""The native MCP client across mcp versions: which streamable-http client it
opens, and how it reads tool listings and call results.

A fake `mcp` package stands in for the real one, which the `[dev]` extra does
not install. mcp 1's models have camelCase attributes (`isError`,
`inputSchema`); mcp 2's snake_case ones (`is_error`, `input_schema`), with
the camelCase names left only as JSON aliases.
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import pytest

from wfpy._mcp_client import _connect, call_mcp_tool, list_mcp_tools

_URL = "http://host/mcp"
_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}}


class _Session:
    """Answers `list_tools` and `call_tool` with the class's `listing` and
    `call_result`, which a test sets."""

    listing: Any = None
    call_result: Any = None

    def __init__(self, read_stream: Any, write_stream: Any) -> None:
        self.streams = (read_stream, write_stream)

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def initialize(self) -> None:
        pass

    async def list_tools(self) -> Any:
        return self.listing

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return self.call_result


@asynccontextmanager
async def _two_streams(url: str, **_: Any) -> AsyncIterator[tuple[Any, Any]]:
    yield ("read", "write")


def _install_fake_mcp(monkeypatch: pytest.MonkeyPatch, clients: dict[str, Any]) -> None:
    mcp = types.ModuleType("mcp")
    mcp.ClientSession = _Session  # type: ignore[attr-defined]
    client = types.ModuleType("mcp.client")
    streamable_http = types.ModuleType("mcp.client.streamable_http")
    for name, fn in clients.items():
        setattr(streamable_http, name, fn)
    client.streamable_http = streamable_http  # type: ignore[attr-defined]
    for module in (mcp, client, streamable_http):
        monkeypatch.setitem(sys.modules, module.__name__, module)


async def _session_streams(url: str) -> tuple[Any, Any]:
    async with _connect("srv", "streamable-http", url=url) as session:
        return session.streams  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("name", "yielded"),
    [
        # mcp 2: the read and write streams only
        ("streamable_http_client", ("read", "write")),
        # mcp 1: a session-id getter after them
        ("streamablehttp_client", ("read", "write", lambda: None)),
    ],
)
def test_streamable_http_uses_the_client_the_installed_mcp_has(
    monkeypatch: pytest.MonkeyPatch, name: str, yielded: tuple[Any, ...]
) -> None:
    opened: list[str] = []

    @asynccontextmanager
    async def client(url: str, **_: Any) -> AsyncIterator[tuple[Any, ...]]:
        opened.append(url)
        yield yielded

    _install_fake_mcp(monkeypatch, {name: client})

    assert asyncio.run(_session_streams(_URL)) == ("read", "write")
    assert opened == [_URL]


def test_streamable_http_without_either_client_says_to_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_mcp(monkeypatch, {})

    with pytest.raises(RuntimeError, match="does not support it"):
        asyncio.run(_session_streams(_URL))


@pytest.mark.parametrize("error_attr", ["is_error", "isError"])  # mcp 2, mcp 1
@pytest.mark.parametrize("failed", [True, False])
def test_a_tool_call_reports_its_error_flag_in_either_mcp(
    monkeypatch: pytest.MonkeyPatch, error_attr: str, failed: bool
) -> None:
    _install_fake_mcp(monkeypatch, {"streamable_http_client": _two_streams})
    content = [types.SimpleNamespace(type="text", text="said")]
    monkeypatch.setattr(
        _Session, "call_result", types.SimpleNamespace(content=content, **{error_attr: failed})
    )

    result = asyncio.run(call_mcp_tool("srv", "streamable-http", "tool", {}, url=_URL))

    if failed:
        assert (result.ok, result.result, result.error) == (False, "", "said")
    else:
        assert (result.ok, result.result, result.error) == (True, "said", "")


@pytest.mark.parametrize("schema_attr", ["input_schema", "inputSchema"])  # mcp 2, mcp 1
def test_a_tool_listing_keeps_each_schema_in_either_mcp(
    monkeypatch: pytest.MonkeyPatch, schema_attr: str
) -> None:
    _install_fake_mcp(monkeypatch, {"streamable_http_client": _two_streams})
    tool = types.SimpleNamespace(name="echo", description="Echo.", **{schema_attr: _SCHEMA})
    monkeypatch.setattr(_Session, "listing", types.SimpleNamespace(tools=[tool]))

    tools = asyncio.run(list_mcp_tools("srv", "streamable-http", url=_URL))

    assert [(t.name, t.description, t.input_schema) for t in tools] == [
        ("echo", "Echo.", _SCHEMA)
    ]
