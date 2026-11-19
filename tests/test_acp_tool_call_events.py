"""An ACP tool call reaches the run's observers with its title and kind.

The protocol's `tool_call` update names the call by a title ("Write
choice-a.txt") and a kind ("edit"); it had no `name`. The event the run
stream carries used to read the missing name and say "unknown", so an IDE
showed every call of a Claude session as "unknown".

Since schema 1.23 (SDK 1.0) a tool call may carry the tool's own `name`
("Write") beside its title. It is then the event's name, for the call and for
the updates of that call, which need not repeat it.
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


def test_an_update_keeps_the_tools_own_name_of_its_call():
    """The call says "Write"; its update says only that it completed. Named by
    its title, the update would change the call's name half-way."""
    seen = []
    client = SimpleClient(on_event=seen.append)
    updates = [
        SimpleNamespace(sessionUpdate="tool_call", name="Write", title="Write choice-a.txt",
                        kind="edit", status="pending", tool_call_id="c1"),
        SimpleNamespace(sessionUpdate="tool_call_update", title="Write choice-a.txt",
                        status="completed", tool_call_id="c1"),
        # Another call, which never had a name of its own: by its title, as before.
        SimpleNamespace(sessionUpdate="tool_call_update", title="Read notes.md",
                        status="completed", tool_call_id="c2"),
    ]

    async def drive():
        for update in updates:
            await client.session_update("s1", update)

    asyncio.run(drive())
    assert [e["name"] for e in seen] == ["Write", "Write", "Read notes.md"]
    assert [e["title"] for e in seen] == ["Write choice-a.txt", "Write choice-a.txt", "Read notes.md"]


def test_the_sdks_own_tool_call_carries_its_name_through():
    """The SDK's model, not a stand-in: before 1.0 it dropped the field."""
    from acp.schema import ToolCallStart

    if "name" not in ToolCallStart.model_fields:
        pytest.skip("a tool call has a name of its own from SDK 1.0 on")
    update = ToolCallStart.model_validate({
        "sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Write choice-a.txt",
        "kind": "edit", "name": "Write",
    })
    (event,) = _events_of(update)
    assert (event["name"], event["title"], event["id"]) == ("Write", "Write choice-a.txt", "c1")
