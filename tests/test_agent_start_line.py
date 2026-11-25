"""The start line of a firing says what the agent runs through.

Every firing of an agent prints one line before its turn. It named a
provider and a model for every transport, read as the HTTP transport reads
them: an agent that runs over ACP or through a CLI and names no model was
announced as

    [wfpy][agent] oracle (Oracle) start: provider=openai, model=gpt-4o, ...

while Codex, Claude Code or OpenCode answered with the model of its own
configuration. The line now names the transport, for ACP the connector, and
a model only where the firing tells the agent one. The HTTP transport's line
is as it was: there the default provider and model are what is called.
"""

from __future__ import annotations

import pytest

from wfpy import connectors as C
from wfpy._agent_cli_runtime import _agent_start_summary
from wfpy.core import AgentSpec
from wfpy.runner import FifoPlan, Queue, RuntimeActor


@pytest.fixture(autouse=True)
def only_the_known_connectors(monkeypatch, tmp_path):
    """No connectors file of the person who runs the tests."""
    monkeypatch.setattr(C, "user_file", lambda: tmp_path / "none.toml")
    monkeypatch.setattr(C, "workspace_file", lambda root: tmp_path / "none.toml")


def summary(options=None, **agent):
    return _agent_start_summary(AgentSpec(prompt="p", **agent), options or {})


class TestHttpIsAsItWas:
    def test_the_defaults_are_what_is_called(self):
        assert summary() == "provider=openai, model=gpt-4o"

    def test_a_model_with_its_provider(self):
        assert summary(model="anthropic/claude-sonnet-4-5") == (
            "provider=anthropic, model=claude-sonnet-4-5")

    def test_a_provider_named_beside_the_model(self):
        assert summary(model="qwen3", provider="ollama") == "provider=ollama, model=qwen3"


class TestAcp:
    def test_no_model_named_is_no_model_said(self):
        assert summary(transport="acp", connector="codex") == "transport=acp, connector=codex"

    def test_the_connector_of_the_run(self):
        assert summary({"acp_connector": "claude"}, transport="acp") == (
            "transport=acp, connector=claude")

    def test_the_agent_s_connector_before_the_run_s(self):
        assert summary({"acp_connector": "claude"}, transport="acp", connector="codex") == (
            "transport=acp, connector=codex")

    def test_no_connector_named_is_opencode(self):
        assert summary(transport="acp") == "transport=acp, connector=opencode"
        assert summary(transport="opencode-acp") == "transport=acp, connector=opencode"

    def test_the_agent_s_model(self):
        assert summary(transport="acp", connector="codex", model="gpt-5.1-codex") == (
            "transport=acp, connector=codex, model=gpt-5.1-codex")

    def test_the_connector_s_model_where_the_agent_names_none(self, monkeypatch, tmp_path):
        user = tmp_path / "u.toml"
        user.write_text('[connectors.claude]\nmodel = "sonnet"\n')
        monkeypatch.setattr(C, "user_file", lambda: user)
        assert summary(transport="acp", connector="claude") == (
            "transport=acp, connector=claude, model=sonnet")
        assert summary(transport="acp", connector="claude", model="opus") == (
            "transport=acp, connector=claude, model=opus")

    def test_a_command_line_in_place_of_a_connector(self):
        options = {"agent_cli_acp_command": "/opt/agents/my-agent --acp --verbose"}
        assert summary(options, transport="acp", connector="codex") == (
            "transport=acp, command=/opt/agents/my-agent")

    def test_a_connector_nobody_declared_is_named_as_written(self):
        """The firing fails on it, with the connectors there are. The line
        before it does not."""
        assert summary(transport="acp", connector="no-such") == (
            "transport=acp, connector=no-such")


class TestTheCliTransports:
    @pytest.mark.parametrize("transport", ["opencode-cli", "claude-cli", "codex-cli"])
    def test_no_model_named_is_no_model_said(self, transport):
        assert summary(transport=transport) == f"transport={transport}"

    @pytest.mark.parametrize("alias, transport", [
        ("claude", "claude-cli"), ("claude-code", "claude-cli"), ("codex", "codex-cli")])
    def test_an_alias_is_said_by_the_transport_s_name(self, alias, transport):
        assert summary(transport=alias) == f"transport={transport}"

    @pytest.mark.parametrize("transport", ["opencode-cli", "claude-cli"])
    def test_a_model_that_is_passed_on_is_said(self, transport):
        assert summary(transport=transport, model="sonnet") == (
            f"transport={transport}, model=sonnet")

    def test_codex_cli_is_not_told_the_model_and_the_line_does_not_say_it(self):
        """`codex-cli` builds its command without the agent's model: Codex
        runs with its own. A line that named the agent's would be wrong."""
        assert summary(transport="codex-cli", model="gpt-5.1-codex") == "transport=codex-cli"


def test_the_offline_transport_names_no_model():
    assert summary(transport="mock") == "transport=mock"
    assert summary(transport="offline", model="anything") == "transport=mock"


@pytest.mark.parametrize("agent, said", [
    (dict(transport="acp", connector="codex"), "transport=acp, connector=codex, "),
    (dict(transport="claude-cli"), "transport=claude-cli, "),
    (dict(), "provider=openai, model=gpt-4o, "),
])
def test_the_line_a_firing_prints(agent, said, tmp_path, monkeypatch, capsys,
                                  make_task_meta, make_port):
    from wfpy.runner import _step_agent

    def stand_in(_spec, _payload_text, _verbose, **_kwargs):
        response = '{"outputs": {"out": "ok"}}'
        return response, [{"role": "assistant", "content": response}], None, {}

    monkeypatch.setattr("wfpy.runner._invoke_agent", stand_in)
    spec = AgentSpec(prompt="p", use_skill=False, timeout_ms=120_000, **agent)
    meta = make_task_meta(name="Oracle", kind="agent", agent_spec=spec,
                          output_ports={"out": make_port("out", str, "out")})
    actor = RuntimeActor(name="oracle", kind="agent", instance=object(), meta=meta)
    actor.out_queues = {"out": [Queue(queue_id="q")]}
    plan = FifoPlan(name="wf")
    plan.options = {}

    assert _step_agent(actor, tmp_path, plan, verbose=False) is True

    (line,) = [text for text in capsys.readouterr().out.splitlines() if " start: " in text]
    assert line == f"[wfpy][agent] oracle (Oracle) start: {said}timeout=120000ms, outputs=[out]"
