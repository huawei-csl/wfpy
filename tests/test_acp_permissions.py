"""An ACP agent's permission requests: answered in the protocol's shape, by
the user through the elicitation seam or by a policy, on any ACP agent's
command line."""

from __future__ import annotations

import asyncio
import types

import pytest

pytest.importorskip("acp")

from acp.schema import AllowedOutcome, DeniedOutcome, PermissionOption

from wfpy._agent_cli_runtime import _acp_agent_argv, _acp_permission_handler
from wfpy._elicitation_runtime import ElicitationResponse
from wfpy.acp_client import ACPClient, SimpleClient, allow_once, option_of_kind

OPTIONS = [
    PermissionOption(option_id="allow_always", name="Always Allow", kind="allow_always"),
    PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
    PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
]
OFFERED = [{"id": "allow_always", "kind": "allow_always", "name": "Always Allow"},
           {"id": "allow", "kind": "allow_once", "name": "Allow"},
           {"id": "reject", "kind": "reject_once", "name": "Reject"}]
CALL = types.SimpleNamespace(title="Write hello.txt")


def ask(client: SimpleClient):
    return asyncio.run(client.request_permission(OPTIONS, "s1", CALL))


class TestTheReply:
    def test_without_a_handler_the_request_is_allowed_once(self):
        """The reply the protocol takes: a selected option id. The old client
        answered `outcome="approved"`, which the schema refuses, so every
        permissioned tool call failed with 'Invalid params'."""
        events: list[dict] = []
        client = SimpleClient(on_event=events.append)
        reply = ask(client)
        assert isinstance(reply.outcome, AllowedOutcome)
        assert reply.outcome.option_id == "allow"
        assert [e["type"] for e in events] == ["agent.permission.requested", "agent.permission.answered"]
        assert events[0]["title"] == "Write hello.txt"
        assert events[0]["options"] == OFFERED

    def test_the_handler_chooses(self):
        seen: list[tuple] = []

        def handler(title, offered):
            seen.append((title, offered))
            return "reject"

        client = SimpleClient(on_permission=handler)
        reply = ask(client)
        assert seen == [("Write hello.txt", OFFERED)]
        assert reply.outcome == AllowedOutcome(outcome="selected", option_id="reject")
        assert client.permission_pending == 0

    def test_a_handler_returning_none_cancels_the_turn(self):
        client = SimpleClient(on_permission=lambda title, offered: None)
        reply = ask(client)
        assert isinstance(reply.outcome, DeniedOutcome)

    def test_the_default_prefers_allow_once(self):
        assert allow_once("x", OFFERED) == "allow"
        assert allow_once("x", [{"id": "a", "kind": "allow_always", "name": ""}]) == "a"
        assert allow_once("x", [{"id": "r", "kind": "reject_once", "name": ""}]) == "r"
        assert allow_once("x", []) is None
        assert option_of_kind(OFFERED, "reject") == "reject"


class TestWhoAnswers:
    def _options(self, **extra):
        return {"agent_cli_opencode_command": "", **extra}

    def test_the_user_answers_by_name_id_or_number(self):
        answers = iter(["Reject", "allow", "1", "nonsense"])
        asked: list[dict] = []

        def elicit(question, *, context=None, choices=None):
            asked.append({"question": question, "choices": choices})
            return ElicitationResponse(answer=next(answers))

        spec = types.SimpleNamespace(ask_permissions=True)
        handler = _acp_permission_handler(spec, self._options(_wf_elicit=elicit))
        assert handler("Write hello.txt", OFFERED) == "reject"
        assert handler("Write hello.txt", OFFERED) == "allow"
        assert handler("Write hello.txt", OFFERED) == "allow_always"
        assert handler("Write hello.txt", OFFERED) == "reject"   # an unmatched answer is a refusal
        assert asked[0] == {"question": "Write hello.txt", "choices": ["Always Allow", "Allow", "Reject"]}

    def test_a_declined_question_is_a_refusal(self):
        def elicit(question, *, context=None, choices=None):
            return ElicitationResponse(answer=None, declined=True, reason="no user")

        handler = _acp_permission_handler(types.SimpleNamespace(ask_permissions=True), self._options(_wf_elicit=elicit))
        assert handler("Write hello.txt", OFFERED) == "reject"
        assert handler("Write hello.txt", OFFERED[:2]) is None   # nothing to refuse with: cancel

    def test_without_a_user_the_policy_answers(self):
        spec = types.SimpleNamespace(ask_user=False)
        assert _acp_permission_handler(spec, self._options()) is None
        assert _acp_permission_handler(spec, self._options(agent_cli_acp_permissions="allow")) is None
        reject = _acp_permission_handler(spec, self._options(agent_cli_acp_permissions="reject"))
        assert reject("Write hello.txt", OFFERED) == "reject"
        with pytest.raises(ValueError, match="agent_cli_acp_permissions"):
            _acp_permission_handler(spec, self._options(agent_cli_acp_permissions="maybe"))

    def test_ask_user_without_a_seam_falls_back_to_the_policy(self):
        spec = types.SimpleNamespace(ask_permissions=True)
        assert _acp_permission_handler(spec, self._options()) is None


class TestTheAgentCommand:
    def test_opencode_by_default(self):
        assert _acp_agent_argv({"agent_cli_opencode_command": ""}) == ["opencode", "acp"]
        assert _acp_agent_argv({"agent_cli_opencode_command": "/x/opencode"}) == ["/x/opencode", "acp"]

    def test_any_acp_agent_verbatim(self):
        assert _acp_agent_argv({"agent_cli_acp_command": "claude-agent-acp"}) == ["claude-agent-acp"]
        assert _acp_agent_argv({"agent_cli_acp_command": "npx -y @zed-industries/claude-agent-acp"}) == [
            "npx", "-y", "@zed-industries/claude-agent-acp"]

    def test_the_client_spawns_what_it_is_given(self):
        assert ACPClient().agent_argv == ["opencode", "acp"]
        assert ACPClient(opencode_command="/x/opencode").agent_argv == ["/x/opencode", "acp"]
        assert ACPClient(agent_argv=["claude-agent-acp"]).agent_argv == ["claude-agent-acp"]
