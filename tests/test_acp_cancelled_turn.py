"""A turn an ACP agent ends as `cancelled` has no answer.

Codex ends its turn so when a permission request is refused: its one refusal
is "No, and tell Codex what to do differently", and with nobody to tell it
the turn is over. What it wrote until then ("I'll request approval to write
`made.txt`.") was taken for its reply and sent on as the output token, with
the run ending in success. The firing fails instead, and says which request
the turn ended on.
"""

from __future__ import annotations

import asyncio
import types

import pytest

pytest.importorskip("acp")

from acp.schema import PermissionOption

import wfpy.acp_client as acp_client
from wfpy import _agent_cli_runtime as rt
from wfpy.acp_client import ACPClient, SimpleClient

# What codex-acp 2.12 offers for a command that needs approval.
CODEX_OPTIONS = [
    PermissionOption(option_id="allow_once", name="Yes, proceed", kind="allow_once"),
    PermissionOption(option_id="accept_execpolicy_amendment",
                     name="Yes, and don't ask again", kind="allow_always"),
    PermissionOption(option_id="cancel",
                     name="No, and tell Codex what to do differently", kind="reject_once"),
]
CALL = types.SimpleNamespace(title="printf '%s' heron > made.txt")
PREAMBLE = "I’ll request approval to write `heron` to `made.txt` in the current directory."


def ask(client: SimpleClient):
    return asyncio.run(client.request_permission(CODEX_OPTIONS, "s1", CALL))


class TestWhatTheClientRemembers:
    def test_a_refused_request_is_remembered_by_its_title(self):
        client = SimpleClient(on_permission=lambda title, offered: "cancel")
        ask(client)
        assert client.refused == ["printf '%s' heron > made.txt"]

    def test_so_is_one_answered_with_no_option(self):
        client = SimpleClient(on_permission=lambda title, offered: None)
        ask(client)
        assert client.refused == ["printf '%s' heron > made.txt"]

    def test_an_allowed_request_is_not(self):
        client = SimpleClient()
        ask(client)
        client = SimpleClient(on_permission=lambda title, offered: "accept_execpolicy_amendment")
        ask(client)
        assert client.refused == []


class FakeConnection:
    """A connection whose turn streams one chunk and refuses one request."""

    def __init__(self, inner: SimpleClient, stop_reason: str = "end_turn"):
        self.inner = inner
        self.stop_reason = stop_reason

    async def prompt(self, session_id, prompt):
        self.inner.response_text += "chunk."
        self.inner.refused.append("a request")
        return types.SimpleNamespace(stopReason=self.stop_reason)


def started_client(stop_reason: str = "end_turn") -> ACPClient:
    client = ACPClient(agent_argv=["codex-acp"])
    client.client = SimpleClient()
    client.connection = FakeConnection(client.client, stop_reason)
    return client


class TestATurnsTextAndRefusals:
    def test_a_turn_starts_from_nothing(self):
        client = started_client()
        client.client.response_text = "an earlier turn."
        client.client.refused = ["an earlier request"]
        reply = asyncio.run(client.send_prompt("s1", "go"))
        assert reply == {"stop_reason": "end_turn", "text": "chunk.", "refused": ["a request"]}


def spec(**kw):
    base = dict(prompt="p", transport="acp", model=None, stateful=False, ask_user=False,
                ask_permissions=False, cli_tools_mode="native", connector="", mode="", variant="",
                provider=None, endpoint=None, timeout_ms=1000, claude_agent=None, skill=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def fire(monkeypatch, response: dict, **options):
    """One firing over a transport that answers `response`."""
    events: list[dict] = []

    async def fake_invoke(**kwargs):
        return {"session_id": "0199-codex", "applied": {}, **response}

    def no_cli(*args, **kwargs):
        raise AssertionError("a cancelled turn must not be run again through the CLI")

    monkeypatch.setattr(acp_client, "invoke_opencode_acp", fake_invoke)
    monkeypatch.setattr(rt, "_invoke_agent_opencode_cli", no_cli)
    out = rt._invoke_agent_opencode_acp(
        spec(), "payload", False, effective_prompt="p",
        plan_options={"_wf_event_publish": events.append, **options},
        output_ports={"Status": types.SimpleNamespace(port_type=str, ext="")})
    return out, events


CODEX = {"agent_cli_acp_command": "codex-acp"}


class TestTheFiring:
    def test_a_turn_cancelled_at_a_refusal_is_an_error_not_a_reply(self, monkeypatch):
        (text, messages, error, meta), _ = fire(
            monkeypatch, {"stop_reason": "cancelled", "text": PREAMBLE,
                          "refused": ["printf '%s' heron > made.txt"]}, **CODEX)
        assert text == ""                       # not the sentence before the request
        assert messages == [{"role": "user", "content": "payload"}]
        assert isinstance(error, RuntimeError)
        said = str(error)
        assert "ACP agent 'codex-acp' ended its turn without an answer" in said
        assert "(stop reason 'cancelled')" in said
        assert "after its permission request was refused: printf '%s' heron > made.txt" in said
        assert meta["acpStopReason"] == "cancelled"
        assert meta["acpRefusedPermissions"] == ["printf '%s' heron > made.txt"]
        assert "acpSuccess" not in meta

    def test_the_last_refused_request_is_the_one_named(self, monkeypatch):
        (_, _, error, _), _ = fire(
            monkeypatch,
            {"stop_reason": "cancelled", "text": "", "refused": ["rm a", "rm b"]}, **CODEX)
        assert "was refused: rm b." in str(error)

    def test_a_cancelled_turn_with_no_refusal_is_an_error_too(self, monkeypatch):
        (text, _, error, meta), _ = fire(
            monkeypatch,
            {"stop_reason": "cancelled", "text": "half an answer", "refused": []}, **CODEX)
        assert text == ""
        assert str(error) == (
            "ACP agent 'codex-acp' ended its turn without an answer (stop reason 'cancelled').")
        assert meta["acpRefusedPermissions"] == []

    def test_the_session_is_still_recorded_and_the_end_is_published(self, monkeypatch):
        """The session is the place to see what the agent asked for: the id
        is kept, and the observers are told how the turn ended."""
        (_, _, _, meta), events = fire(
            monkeypatch, {"stop_reason": "cancelled", "text": PREAMBLE, "refused": ["x"]}, **CODEX)
        assert meta["opencodeSessionID"] == "0199-codex"
        assert meta["agentSession"]["id"] == "0199-codex"
        assert [e["type"] for e in events] == ["agent.message.start", "agent.message.end"]
        assert events[-1]["stopReason"] == "cancelled"
        assert events[-1]["session"]["id"] == "0199-codex"

    def test_opencode_is_not_run_again_through_its_cli(self, monkeypatch):
        """Another ACP failure of OpenCode falls back to `opencode run`. A
        turn that ended at a refusal must not: the CLI would do what was
        refused."""
        (text, _, error, meta), _ = fire(
            monkeypatch, {"stop_reason": "cancelled", "text": "", "refused": ["Write hello.txt"]})
        assert text == "" and isinstance(error, RuntimeError)
        assert "ACP agent 'opencode acp'" in str(error)
        assert "acpFallbackToCli" not in meta

    def test_a_turn_that_ended_is_a_reply_as_before(self, monkeypatch):
        (text, messages, error, meta), _ = fire(
            monkeypatch,
            {"stop_reason": "end_turn", "text": '{"outputs": {"Status": "DONE"}}', "refused": []},
            **CODEX)
        assert error is None
        assert text == '{"outputs": {"Status": "DONE"}}'
        assert messages[-1] == {"role": "assistant", "content": text}
        assert meta["acpSuccess"] is True and meta["acpStopReason"] == "end_turn"

    def test_a_refusal_the_agent_went_on_from_is_a_reply(self, monkeypatch):
        """Claude Code and OpenCode take a refusal as the tool call's result
        and finish the turn: that answer stands."""
        (text, _, error, _), _ = fire(
            monkeypatch, {"stop_reason": "end_turn", "text": '{"outputs": {"Status": "BLOCKED"}}',
                          "refused": ["Write hello.txt"]}, **CODEX)
        assert error is None and "BLOCKED" in text
