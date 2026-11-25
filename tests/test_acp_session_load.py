"""A stateful agent's ACP session outlives the firing that created it only
through session/load: the client asks for it when the agent offers it, and
falls back to a fresh session when it does not or when the load fails."""

from __future__ import annotations

import asyncio
import types

import pytest

pytest.importorskip("acp")

import wfpy.acp_client as acp_client
from wfpy.acp_client import ACPClient, invoke_opencode_acp


class FakeClient:
    """An ACPClient whose protocol calls are recorded, not spoken."""

    def __init__(self, can_load: bool, load_fails: bool = False, **kwargs):
        self.calls: list[tuple] = []
        self.can_load_session = can_load
        self.load_fails = load_fails
        self.client = types.SimpleNamespace(response_text="", events=asyncio.Queue())
        self.last_session_response = None

    async def configure_session(self, session_id, response, model=None, mode=None, effort=None):
        self.calls.append(("configure", session_id, model, mode))
        if effort is not None:
            self.calls.append(("effort", session_id, effort))
        return {}

    async def start(self):
        self.calls.append(("start",))

    async def stop(self):
        self.calls.append(("stop",))

    async def create_session(self, cwd=".", mcp_servers=None):
        self.calls.append(("new_session", cwd) + ((mcp_servers,) if mcp_servers else ()))
        return "fresh"

    async def load_session(self, session_id, cwd=".", mcp_servers=None):
        self.calls.append(("load_session", session_id, cwd) + ((mcp_servers,) if mcp_servers else ()))
        if self.load_fails:
            raise RuntimeError("no such session")


def run(monkeypatch, *, can_load, load_fails=False, session_id="old", **invoke_kw):
    made: list[FakeClient] = []

    def make(**kwargs):
        c = FakeClient(can_load, load_fails)
        made.append(c)
        return c

    async def fake_run(client, session_id, prompt, stuck_timeout, max_retries):
        client.calls.append(("prompt", session_id))
        return {"stop_reason": "end_turn", "text": "ok"}

    monkeypatch.setattr(acp_client, "ACPClient", make)
    monkeypatch.setattr(acp_client, "run_with_stuck_detection", fake_run)
    response = asyncio.run(invoke_opencode_acp(prompt="hi", cwd="/w", session_id=session_id, **invoke_kw))
    return made[0].calls, response


class TestResumingASession:
    def test_the_reasoning_level_reaches_a_loaded_and_a_new_session(self, monkeypatch):
        calls, _response = run(monkeypatch, can_load=True, effort="high")
        assert ("effort", "old", "high") in calls
        calls, _response = run(monkeypatch, can_load=False, session_id=None, effort="max")
        assert ("effort", "fresh", "max") in calls

    def test_a_known_session_is_loaded_before_the_prompt(self, monkeypatch):
        calls, response = run(monkeypatch, can_load=True)
        assert calls == [("start",), ("load_session", "old", "/w"), ("configure", "old", None, None),
                         ("prompt", "old"), ("stop",)]
        assert response["session_id"] == "old"

    def test_an_agent_without_load_gets_a_fresh_session(self, monkeypatch):
        calls, response = run(monkeypatch, can_load=False)
        assert calls == [("start",), ("new_session", "/w"), ("configure", "fresh", None, None),
                         ("prompt", "fresh"), ("stop",)]
        assert response["session_id"] == "fresh"

    def test_a_failed_load_is_a_fresh_session_not_a_failed_firing(self, monkeypatch):
        calls, response = run(monkeypatch, can_load=True, load_fails=True)
        assert calls == [("start",), ("load_session", "old", "/w"), ("new_session", "/w"),
                         ("configure", "fresh", None, None), ("prompt", "fresh"), ("stop",)]
        assert response["session_id"] == "fresh"

    def test_no_session_id_means_a_new_session(self, monkeypatch):
        calls, response = run(monkeypatch, can_load=True, session_id=None)
        assert calls == [("start",), ("new_session", "/w"), ("configure", "fresh", None, None),
                         ("prompt", "fresh"), ("stop",)]

    def test_the_client_reads_the_capability_at_initialize(self):
        client = ACPClient()
        assert client.can_load_session is False


class TestTheRecordedIdIsHandedOn:
    def test_the_transport_passes_a_known_session_id(self, monkeypatch):
        from wfpy import _agent_cli_runtime as rt
        seen: dict = {}

        async def fake_invoke(**kwargs):
            seen.update(kwargs)
            return {"stop_reason": "end_turn", "text": '{"outputs": {"Out": "ok"}}', "session_id": kwargs.get("session_id")}

        monkeypatch.setattr(acp_client, "invoke_opencode_acp", fake_invoke)
        spec = types.SimpleNamespace(prompt="be brief", transport="opencode-acp", model="m", stateful=True,
                                     ask_user=False, cli_tools_mode="wfpy-none", provider=None,
                                     endpoint=None, timeout_ms=1000, claude_agent=None, skill=None)
        rt._invoke_agent_opencode_acp(spec, "payload", False, effective_prompt="be brief",
                                      plan_options={}, session_id="s-42", continue_session=False,
                                      output_ports={"Out": types.SimpleNamespace(port_type=str, ext="")})
        assert seen["session_id"] == "s-42"


class TestTheReplayedHistory:
    """`session/load` is answered by the agent replaying the session's
    history as updates. Measured with Codex: the second firing of a stateful
    agent published the first firing's sentence, answer and tool call again,
    ahead of its own, as if it had written them."""

    def _loaded(self, events: list[dict]) -> ACPClient:
        from wfpy.acp_client import SimpleClient

        inner = SimpleClient(on_event=events.append)

        def chunk(text: str):
            return types.SimpleNamespace(sessionUpdate="agent_message_chunk",
                                         content=types.SimpleNamespace(type="text", text=text))

        class Connection:
            async def load_session(self, session_id, cwd, mcp_servers):
                await inner.session_update(session_id, chunk('{"outputs": {"Out": "OK"}}'))
                await inner.session_update(session_id, types.SimpleNamespace(
                    sessionUpdate="tool_call", title="sleep 8; echo token", kind="execute",
                    status="completed", tool_call_id="exec-1"))
                return types.SimpleNamespace(config_options=[], modes=None)

        client = ACPClient(agent_argv=["codex-acp"])
        client.client = inner
        client.connection = Connection()
        asyncio.run(client.load_session("s-1", cwd="/w"))
        self.chunk = chunk
        return client

    def test_it_is_not_published_as_this_firing(self):
        events: list[dict] = []
        client = self._loaded(events)
        assert events == []
        assert client.client.response_text == ""

    def test_it_is_not_left_for_the_turn_s_stuck_detector(self):
        """The detector does not watch a load. What the load replayed would
        only wait in its queue, and be counted as the new turn's first signs
        of life."""
        client = self._loaded([])
        assert client.client.events.qsize() == 0

    def test_what_comes_after_the_load_is_this_firing(self):
        events: list[dict] = []
        client = self._loaded(events)
        asyncio.run(client.client.session_update("s-1", self.chunk("fresh")))
        assert events == [{"type": "agent.message.delta", "field": "text", "delta": "fresh"}]
        assert client.client.response_text == "fresh"

    def test_a_load_that_fails_does_not_leave_the_client_deaf(self):
        from wfpy.acp_client import SimpleClient

        events: list[dict] = []

        class Connection:
            async def load_session(self, session_id, cwd, mcp_servers):
                raise RuntimeError("no such session")

        client = ACPClient(agent_argv=["codex-acp"])
        client.client = SimpleClient(on_event=events.append)
        client.connection = Connection()
        with pytest.raises(RuntimeError):
            asyncio.run(client.load_session("s-1", cwd="/w"))
        asyncio.run(client.client.session_update("s-2", types.SimpleNamespace(
            sessionUpdate="agent_message_chunk",
            content=types.SimpleNamespace(type="text", text="new"))))
        assert [e["delta"] for e in events] == ["new"]
