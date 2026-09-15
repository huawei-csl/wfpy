"""An ACP tool call reaches the run's observers with its title and kind.

The protocol's `tool_call` update names the call by a title ("Write
choice-a.txt") and a kind ("edit"); it has no `name`. The event the run
stream carries used to read the missing name and say "unknown", so an IDE
showed every call of a Claude session as "unknown".
"""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("acp")

from wfpy.acp_client import SimpleClient  # noqa: E402


def _events_of(update):
    seen = []
    client = SimpleClient(on_event=seen.append)
    asyncio.run(client.session_update("s1", update))
    return seen


def test_tool_call_is_named_by_its_title_and_carries_kind_and_status():
    update = SimpleNamespace(sessionUpdate="tool_call", title="Write choice-a.txt",
                             kind="edit", status="pending", tool_call_id="c1")
    assert _events_of(update) == [{
        "type": "agent.tool_call", "name": "Write choice-a.txt", "title": "Write choice-a.txt",
        "kind": "edit", "status": "pending", "id": "c1",
    }]


def test_tool_call_update_and_fallbacks():
    update = SimpleNamespace(sessionUpdate="tool_call_update", title="Write choice-a.txt",
                             status="completed", toolCallId="c1")
    (event,) = _events_of(update)
    assert event["type"] == "agent.tool_call_update"
    assert event["name"] == "Write choice-a.txt" and event["status"] == "completed" and event["id"] == "c1"

    bare = SimpleNamespace(sessionUpdate="tool_call", kind="execute")
    assert _events_of(bare)[0]["name"] == "execute"
    named = SimpleNamespace(sessionUpdate="tool_call", name="read_file", title="Read x")
    assert _events_of(named)[0]["name"] == "read_file"
    nothing = SimpleNamespace(sessionUpdate="tool_call")
    assert _events_of(nothing)[0]["name"] == "tool"
