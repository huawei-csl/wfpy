"""The acp transport spawns the connector the agent or the run names, and
sets the session's model and mode where the agent offers them."""

from __future__ import annotations

import json
import asyncio
import types

import pytest

pytest.importorskip("acp")

from wfpy import _agent_cli_runtime as rt
from wfpy import connectors as C
from wfpy.acp_client import ACPClient


def spec(**kw):
    base = dict(prompt="p", transport="acp", model=None, stateful=False, ask_user=False,
                cli_tools_mode="wfpy-none", connector="", mode="", provider=None, endpoint=None, timeout_ms=1000)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def connectors(monkeypatch, tmp_path):
    monkeypatch.setattr(C.shutil, "which", lambda cmd: f"/bin/{cmd}")
    user = tmp_path / "u.toml"
    user.write_text('[connectors.claude]\nmodel = "sonnet"\nmode = "plan"\nenv = { A = "1" }\n')
    monkeypatch.setattr(C, "user_file", lambda: user)
    monkeypatch.setattr(C, "workspace_file", lambda root: tmp_path / "none.toml")


class TestWhichAgent:
    def test_acp_is_the_transport_and_opencode_acp_its_old_name(self):
        assert rt._normalize_agent_transport(spec(transport="acp")) == "opencode-acp"
        assert rt._normalize_agent_transport(spec(transport="opencode-acp")) == "opencode-acp"

    def test_the_agent_names_the_connector(self, connectors):
        assert rt._acp_agent_argv({}, spec(connector="claude")) == ["claude-agent-acp"]

    def test_else_the_run_does(self, connectors):
        assert rt._acp_agent_argv({"acp_connector": "claude"}, spec()) == ["claude-agent-acp"]
        assert rt._acp_agent_argv({"acp_connector": "opencode"}, spec()) == ["opencode", "acp"]

    def test_else_opencode_as_before(self, connectors):
        assert rt._acp_agent_argv({"agent_cli_opencode_command": ""}, spec()) == ["opencode", "acp"]

    def test_an_explicit_command_wins(self, connectors):
        assert rt._acp_agent_argv({"agent_cli_acp_command": "x --y"}, spec(connector="claude")) == ["x", "--y"]

    def test_an_unknown_connector_is_refused_with_the_list(self, connectors):
        with pytest.raises(ValueError, match="Unknown ACP connector 'nope'"):
            rt._acp_agent_argv({}, spec(connector="nope"))


class FakeConnection:
    def __init__(self):
        self.calls = []

    async def set_config_option(self, config_id, session_id, value):
        self.calls.append(("config", config_id, value))

    async def set_session_mode(self, session_id, mode_id):
        self.calls.append(("mode", mode_id))


def offers(models=("default", "sonnet", "haiku", "opus[1m]"), modes=("default", "acceptEdits", "plan")):
    option = types.SimpleNamespace(id="model", options=[types.SimpleNamespace(value=v, name=v.title()) for v in models])
    return types.SimpleNamespace(config_options=[option],
                                 modes=types.SimpleNamespace(available_modes=[types.SimpleNamespace(id=m) for m in modes]))


class TestModelAndMode:
    def _client(self):
        client = ACPClient()
        client.connection = FakeConnection()
        return client

    def test_the_model_by_value_name_or_prefix(self):
        client = self._client()
        applied = asyncio.run(client.configure_session("s", offers(), model="Sonnet"))
        assert applied == {"model": "sonnet"} and client.connection.calls == [("config", "model", "sonnet")]
        client = self._client()
        assert asyncio.run(client.configure_session("s", offers(), model="opus")) == {"model": "opus[1m]"}

    def test_what_the_agent_does_not_offer_is_left(self, caplog):
        client = self._client()
        assert asyncio.run(client.configure_session("s", offers(), model="gpt-9", mode="yolo")) == {}
        assert client.connection.calls == []
        assert "no 'gpt-9'" in caplog.text and "no mode 'yolo'" in caplog.text
        assert asyncio.run(client.configure_session("s", types.SimpleNamespace(), model="sonnet")) == {}

    def test_the_mode_by_id(self):
        client = self._client()
        assert asyncio.run(client.configure_session("s", offers(), mode="plan")) == {"mode": "plan"}
        assert client.connection.calls == [("mode", "plan")]


class TestTheTransportHandsThemOn:
    def test_connector_model_mode_and_env_reach_the_invocation(self, monkeypatch, connectors):
        import wfpy.acp_client as acp_client

        seen = {}

        async def fake_invoke(**kw):
            seen.update(kw)
            return {"stop_reason": "end_turn", "text": '{"outputs": {"Out": "ok"}}', "session_id": "s", "applied": {}}

        monkeypatch.setattr(acp_client, "invoke_opencode_acp", fake_invoke)
        rt._invoke_agent_opencode_acp(spec(connector="claude"), "payload", False, effective_prompt="p",
                                      plan_options={}, output_ports={"Out": types.SimpleNamespace(port_type=str, ext="")})
        assert seen["agent_argv"] == ["claude-agent-acp"]
        assert seen["model"] == "sonnet" and seen["mode"] == "plan"      # the connector's: the agent names no model
        assert seen["env"] == {"A": "1"}                                   # no OPENCODE_CONFIG_CONTENT for a non-opencode agent

    def test_the_agents_own_model_and_mode_win(self, monkeypatch, connectors):
        import wfpy.acp_client as acp_client

        seen = {}

        async def fake_invoke(**kw):
            seen.update(kw)
            return {"stop_reason": "end_turn", "text": '{"outputs": {"Out": "ok"}}', "session_id": "s", "applied": {}}

        monkeypatch.setattr(acp_client, "invoke_opencode_acp", fake_invoke)
        rt._invoke_agent_opencode_acp(spec(connector="claude", model="haiku", mode="acceptEdits"), "payload", False,
                                      effective_prompt="p", plan_options={},
                                      output_ports={"Out": types.SimpleNamespace(port_type=str, ext="")})
        assert seen["model"] == "haiku" and seen["mode"] == "acceptEdits"


class TestOpenCodeModel:
    """OpenCode is told its model through OPENCODE_CONFIG_CONTENT, and only a chosen one."""

    def _run(self, monkeypatch, options=None, fail=False, **kw):
        import wfpy.acp_client as acp_client

        seen = {}

        async def fake_invoke(**invoke):
            seen.update(invoke)
            if fail:
                raise RuntimeError("session/load broke")
            return {"stop_reason": "end_turn", "text": '{"outputs": {"Out": "ok"}}', "session_id": "s", "applied": {}}

        def fake_cli(spec, *args, **kwargs):
            seen["cli_model"] = spec.model
            return "", [], None, {}

        monkeypatch.setattr(acp_client, "invoke_opencode_acp", fake_invoke)
        monkeypatch.setattr(rt, "_invoke_agent_opencode_cli", fake_cli)
        rt._invoke_agent_opencode_acp(spec(**kw), "payload", False, effective_prompt="p",
                                      plan_options={"acp_connector": "opencode", **(options or {})},
                                      output_ports={"Out": types.SimpleNamespace(port_type=str, ext="")})
        return seen

    def test_an_agent_naming_no_model_leaves_opencode_its_own(self, monkeypatch, connectors):
        # It was told openai/gpt-4o, the decorator's default, which fails
        # wherever no OpenAI provider is configured.
        assert "OPENCODE_CONFIG_CONTENT" not in (self._run(monkeypatch)["env"] or {})

    def test_a_named_model_is_handed_on_even_the_old_default(self, monkeypatch, connectors):
        for model in ("opencode/big-pickle", "openai/gpt-4o"):
            env = self._run(monkeypatch, model=model)["env"]
            assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {"model": model}

    def test_the_connectors_model_joins_its_own_configuration(self, monkeypatch, connectors, tmp_path):
        (tmp_path / "u.toml").write_text(
            '[connectors.opencode]\nmodel = "zen/x"\n'
            'env = { OPENCODE_CONFIG_CONTENT = \'{"provider": {"zen": {}}}\' }\n'
        )
        env = self._run(monkeypatch)["env"]
        assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {"provider": {"zen": {}}, "model": "zen/x"}

    def test_the_cli_fallback_runs_on_the_same_model(self, monkeypatch, connectors):
        assert self._run(monkeypatch, fail=True)["cli_model"] is None
        assert self._run(monkeypatch, fail=True, model="opencode/big-pickle")["cli_model"] == "opencode/big-pickle"

    def test_an_explicit_command_line_is_told_both_ways(self, monkeypatch, connectors):
        seen = self._run(monkeypatch, options={"acp_connector": "", "agent_cli_acp_command": "claude-agent-acp"}, model="haiku")
        assert seen["model"] == "haiku"
        assert json.loads(seen["env"]["OPENCODE_CONFIG_CONTENT"]) == {"model": "haiku"}


class TestSessionCwd:
    """ACP requires a session's cwd to be an absolute path; the client makes it one."""

    class Connection:
        def __init__(self):
            self.cwds = []

        async def new_session(self, cwd, mcp_servers):
            self.cwds.append(cwd)
            return types.SimpleNamespace(session_id="s")

        async def load_session(self, session_id, cwd, mcp_servers):
            self.cwds.append(cwd)

    def _client(self):
        client = ACPClient()
        client.connection = self.Connection()
        client.client = types.SimpleNamespace(response_text="")
        return client

    def test_a_new_session_gets_an_absolute_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        client = self._client()
        asyncio.run(client.create_session())
        asyncio.run(client.create_session(cwd="sub"))
        assert client.connection.cwds == [str(tmp_path.resolve()), str(tmp_path.resolve() / "sub")]

    def test_a_loaded_session_gets_an_absolute_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        client = self._client()
        asyncio.run(client.load_session("old"))
        assert client.connection.cwds == [str(tmp_path.resolve())]

    def test_an_absolute_cwd_is_kept(self, tmp_path):
        client = self._client()
        asyncio.run(client.create_session(cwd=str(tmp_path)))
        assert client.connection.cwds == [str(tmp_path)]


class TestThePrompt:
    """The prompt goes out as one text content block, built from the SDK's own
    schema model: the `text_block` helper that used to build it was removed in
    SDK 1.0, and with it every prompt failed before it was sent."""

    class Connection:
        def __init__(self):
            self.prompts = []

        async def prompt(self, session_id, prompt):
            self.prompts.append((session_id, prompt))
            return types.SimpleNamespace(stopReason="end_turn")

    def test_the_prompt_is_one_text_block(self):
        client = ACPClient()
        client.connection = self.Connection()
        client.client = types.SimpleNamespace(response_text="")
        result = asyncio.run(client.send_prompt("s", "What is 2+2?"))
        assert result == {"stop_reason": "end_turn", "text": ""}
        [(session_id, blocks)] = client.connection.prompts
        assert session_id == "s"
        # What the protocol puts on the wire for it.
        assert [b.model_dump(by_alias=True, exclude_none=True) for b in blocks] == [
            {"type": "text", "text": "What is 2+2?"}
        ]


class TestWhenAcpFails:
    """Only OpenCode has a CLI to fall back to; another agent's failure is the firing's error."""

    def _fail(self, monkeypatch):
        import wfpy.acp_client as acp_client

        async def failing_invoke(**kw):
            raise RuntimeError("Invalid params: boom")

        fell_back = []

        def fake_cli(*args, **kwargs):
            fell_back.append(True)
            return "cli", [], None, {"transport": "opencode-cli"}

        monkeypatch.setattr(acp_client, "invoke_opencode_acp", failing_invoke)
        monkeypatch.setattr(rt, "_invoke_agent_opencode_cli", fake_cli)
        return fell_back

    def test_another_agents_failure_is_reported_not_rerun(self, monkeypatch, connectors):
        fell_back = self._fail(monkeypatch)
        text, messages, error, meta = rt._invoke_agent_opencode_acp(
            spec(connector="claude"), "payload", False, effective_prompt="p", plan_options={})
        assert not fell_back
        assert text == "" and messages == [{"role": "user", "content": "payload"}]
        assert "claude-agent-acp" in str(error) and "Invalid params: boom" in str(error)
        assert meta["acpError"] == "Invalid params: boom" and meta["acpConnector"] == "claude"

    def test_an_explicit_command_is_not_rerun_on_opencode_either(self, monkeypatch, connectors):
        fell_back = self._fail(monkeypatch)
        _, _, error, _ = rt._invoke_agent_opencode_acp(
            spec(), "payload", False, effective_prompt="p",
            plan_options={"agent_cli_acp_command": "my-agent --acp"})
        assert not fell_back and "my-agent" in str(error)

    def test_opencode_still_falls_back_to_its_cli(self, monkeypatch, connectors):
        fell_back = self._fail(monkeypatch)
        for agent in (spec(), spec(connector="opencode")):
            text, _, error, _ = rt._invoke_agent_opencode_acp(
                agent, "payload", False, effective_prompt="p", plan_options={})
            assert text == "cli" and error is None
        assert fell_back == [True, True]
