"""A firing records the agent session it talked to -- the agent's own id,
the connector that ran it, the directory it ran in -- on the run's event
stream and on the agent's reply, so the IDE can open it again in the agent's
own CLI (`claude --resume`, `opencode -s`)."""

from __future__ import annotations

import os
import sys
import types

import pytest

from wfpy import _agent_cli_runtime as rt
from wfpy import connectors as C
from wfpy.runner import _attach_agent_response_meta


def spec(**kw):
    base = dict(prompt="p", transport="acp", model="openai/gpt-4o", stateful=False,
                ask_user=False, ask_permissions=False, cli_tools_mode="wfpy-none", connector="",
                mode="", provider=None, endpoint=None, timeout_ms=1000)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def fake_acp(monkeypatch, tmp_path):
    """`wfpy.acp_client` stood in for, so no ACP package is needed; every
    connector resolves on the PATH."""
    calls: list[dict] = []

    async def invoke_opencode_acp(**kw):
        calls.append(kw)
        return {"stop_reason": "end_turn", "text": '{"outputs": {"Out": "ok"}}',
                "session_id": "sess-1", "applied": {}}

    module = types.ModuleType("wfpy.acp_client")
    module.invoke_opencode_acp = invoke_opencode_acp
    module.option_of_kind = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "wfpy.acp_client", module)
    monkeypatch.setattr(C.shutil, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(C, "user_file", lambda: tmp_path / "none.toml")
    monkeypatch.setattr(C, "workspace_file", lambda root: tmp_path / "none.toml")
    return calls


def invoke(agent, options):
    events: list[dict] = []
    text, messages, err, meta = rt._invoke_agent_opencode_acp(
        agent, "payload", False, effective_prompt="p",
        plan_options={**options, "_wf_event_publish": events.append},
        output_ports={"Out": types.SimpleNamespace(port_type=str, ext="")},
    )
    assert err is None
    return events, messages, meta


class TestTheFiringRecordsItsSession:
    def test_the_connector_the_agent_names(self, fake_acp):
        events, _, meta = invoke(spec(connector="claude"), {})
        session = {"id": "sess-1", "connector": "claude", "cwd": os.path.abspath(".")}
        assert meta["agentSession"] == session
        assert events[-1] == {"type": "agent.message.end", "stopReason": "end_turn",
                              "session": session}

    def test_the_default_connector_when_none_is_named(self, fake_acp):
        _, _, meta = invoke(spec(), {})
        assert meta["agentSession"]["connector"] == C.DEFAULT_CONNECTOR

    def test_no_connector_for_an_explicit_command_line(self, fake_acp):
        _, _, meta = invoke(spec(), {"agent_cli_acp_command": "my-acp --flag"})
        assert meta["agentSession"] == {"id": "sess-1", "cwd": os.path.abspath(".")}

    def test_its_directory_is_absolute(self, fake_acp, tmp_path):
        _, _, meta = invoke(spec(connector="claude"), {"work_dir": str(tmp_path)})
        assert meta["agentSession"]["cwd"] == str(tmp_path)


class TestTheReplyCarriesIt:
    def test_on_the_last_assistant_message(self):
        messages = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        session = {"id": "sess-1", "connector": "claude", "cwd": "/w"}
        meta = {"agentSession": session, "opencodeSessionID": "sess-1"}
        _attach_agent_response_meta(messages, meta)
        assert messages[1]["agentSession"] == session
        assert messages[1]["opencodeSessionID"] == "sess-1"  # the older field, kept
        assert "agentSession" not in messages[0]

    def test_no_record_without_an_id(self):
        assert rt._agent_session_record("", "claude", ".") is None
        assert rt._agent_session_record(None, None, ".") is None
