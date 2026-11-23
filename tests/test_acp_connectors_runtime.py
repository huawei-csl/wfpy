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


def select(id, values, category=None):
    return types.SimpleNamespace(id=id, category=category, current_value=values[0],
                                 options=[types.SimpleNamespace(value=v, name=v.title()) for v in values])


def opencode2_offers():
    """OpenCode 2's new_session: no `modes`; model, effort and mode as config options."""
    return types.SimpleNamespace(modes=None, config_options=[
        select("model", ["opencode/a-free", "opencode/b-free"], "model"),
        select("effort", ["minimal", "low", "medium", "high", "xhigh", "max", "default"], "thought_level"),
        select("mode", ["build", "plan"], "mode"),
    ])


class TestOpenCode2SessionOptions:
    """OpenCode 2 offers the mode and the reasoning level only as config options."""

    def _client(self):
        client = ACPClient()
        client.connection = FakeConnection()
        return client

    def test_the_mode_by_its_config_option(self):
        client = self._client()
        assert asyncio.run(client.configure_session("s", opencode2_offers(), mode="Plan")) == {"mode": "plan"}
        assert client.connection.calls == [("config", "mode", "plan")]

    def test_the_config_option_wins_where_both_offer_the_mode(self):
        client = self._client()
        both = offers()
        both.config_options.append(select("mode", ["default", "plan"], "mode"))
        assert asyncio.run(client.configure_session("s", both, mode="plan")) == {"mode": "plan"}
        assert client.connection.calls == [("config", "mode", "plan")]

    def test_a_mode_neither_offers_names_what_is_offered(self, caplog):
        client = self._client()
        assert asyncio.run(client.configure_session("s", opencode2_offers(), mode="yolo")) == {}
        assert client.connection.calls == []
        assert "no mode 'yolo'; it offers ['build', 'plan']" in caplog.text

    def test_the_reasoning_level_by_its_category(self):
        client = self._client()
        assert asyncio.run(client.configure_session("s", opencode2_offers(), effort="HIGH")) == {"effort": "high"}
        assert client.connection.calls == [("config", "effort", "high")]

    def test_a_level_the_agent_does_not_offer_is_left(self, caplog):
        client = self._client()
        assert asyncio.run(client.configure_session("s", opencode2_offers(), effort="ultra")) == {}
        assert asyncio.run(client.configure_session("s", offers(), effort="high")) == {}
        assert client.connection.calls == []
        assert "no 'ultra'" in caplog.text and "no reasoning level option" in caplog.text

    def test_model_mode_and_level_together(self):
        client = self._client()
        applied = asyncio.run(client.configure_session("s", opencode2_offers(), model="opencode/b",
                                                       mode="plan", effort="max"))
        assert applied == {"model": "opencode/b-free", "mode": "plan", "effort": "max"}
        assert client.connection.calls == [("config", "model", "opencode/b-free"), ("config", "mode", "plan"),
                                           ("config", "effort", "max")]

    def test_grouped_choices_are_choices(self):
        client = self._client()
        grouped = types.SimpleNamespace(id="model", category="model", options=[
            types.SimpleNamespace(group="a", name="A", options=[types.SimpleNamespace(value="a/one", name="One")]),
            types.SimpleNamespace(group="b", name="B", options=[types.SimpleNamespace(value="b/two", name="Two")]),
        ])
        response = types.SimpleNamespace(modes=None, config_options=[grouped])
        assert asyncio.run(client.configure_session("s", response, model="two")) == {"model": "b/two"}


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

    def test_the_reasoning_level_reaches_the_session(self, monkeypatch, connectors):
        import wfpy.acp_client as acp_client

        seen = {}

        async def fake_invoke(**kw):
            seen.update(kw)
            return {"stop_reason": "end_turn", "text": '{"outputs": {"Out": "ok"}}', "session_id": "s", "applied": {}}

        monkeypatch.setattr(acp_client, "invoke_opencode_acp", fake_invoke)
        ports = {"Out": types.SimpleNamespace(port_type=str, ext="")}
        rt._invoke_agent_opencode_acp(spec(variant="high"), "payload", False, effective_prompt="p",
                                      plan_options={}, output_ports=ports)
        assert seen["effort"] == "high"
        rt._invoke_agent_opencode_acp(spec(variant=""), "payload", False, effective_prompt="p",
                                      plan_options={}, output_ports=ports)
        assert seen["effort"] is None


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


class TestNotificationsTheSdkDoesNotKnow:
    """A notification has no reply, so one the SDK has no route for is dropped.
    `claude-agent-acp` pushes `auth/status_update` on every turn, and the SDK
    logged each as an unhandled error, traceback and all."""

    @staticmethod
    def _wrapped(handler):
        from wfpy.acp_client import _drop_unrouted_notifications

        connection = types.SimpleNamespace(_conn=types.SimpleNamespace(_handler=handler))
        _drop_unrouted_notifications(connection)
        return connection._conn._handler

    @staticmethod
    async def _router(method, params, is_notification):
        import acp

        if method == "session/update":
            return "routed"
        if method == "session/broken":
            raise ValueError("the client's own handler failed")
        if method == "session/nested":
            # A routed notification whose handler made a call that was refused.
            raise acp.RequestError.method_not_found("fs/read_text_file")
        raise acp.RequestError.method_not_found(method)

    def test_a_routine_one_is_dropped_without_a_word(self, caplog):
        handle = self._wrapped(self._router)
        with caplog.at_level("INFO"):
            assert asyncio.run(handle("auth/status_update", {}, True)) is None
        assert caplog.records == []

    def test_one_wfpy_has_not_met_is_dropped_and_named(self, caplog):
        """Without its traceback, but not without a trace: it may matter."""
        handle = self._wrapped(self._router)
        with caplog.at_level("INFO"):
            assert asyncio.run(handle("quota/exceeded", {}, True)) is None
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ("WARNING", "The agent sent a notification wfpy does not know: quota/exceeded (ignored)")
        ]

    def test_a_refusal_of_another_method_is_not_taken_for_it(self):
        import acp

        handle = self._wrapped(self._router)
        with pytest.raises(acp.RequestError, match="Method not found"):
            asyncio.run(handle("session/nested", {}, True))

    def test_an_extension_notification_is_named_unless_it_is_routine(self, caplog):
        from wfpy.acp_client import SimpleClient

        client = SimpleClient()
        with caplog.at_level("INFO"):
            asyncio.run(client.ext_notification("auth/status_update", {}))
            asyncio.run(client.ext_notification("quota/exceeded", {}))
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ("WARNING", "Extension notification not implemented: quota/exceeded")
        ]

    def test_a_routed_one_is_handled_as_before(self):
        handle = self._wrapped(self._router)
        assert asyncio.run(handle("session/update", {}, True)) == "routed"

    def test_an_unrouted_request_still_fails(self):
        """It has a reply, and the error belongs in it."""
        import acp

        handle = self._wrapped(self._router)
        with pytest.raises(acp.RequestError, match="Method not found"):
            asyncio.run(handle("auth/status", {}, False))

    def test_another_failure_in_a_notification_is_not_hidden(self):
        handle = self._wrapped(self._router)
        with pytest.raises(ValueError, match="own handler failed"):
            asyncio.run(handle("session/broken", {}, True))

    def test_a_connection_of_another_shape_is_left_alone(self):
        from wfpy.acp_client import _drop_unrouted_notifications

        _drop_unrouted_notifications(types.SimpleNamespace())

    def test_the_sdks_own_connection_is_the_one_wrapped(self):
        """Against the SDK itself: its router has no route for the method, and
        its connection keeps the handler where the wrapper looks for it. When
        an SDK release moves either, this is the test that says so."""
        import acp
        from acp.client.connection import ClientSideConnection

        from wfpy.acp_client import SimpleClient, _drop_unrouted_notifications

        class Transport:
            async def send(self, message):
                pass

            async def receive(self):
                return None

            async def close(self):
                pass

        async def drive():
            connection = ClientSideConnection(SimpleClient(), Transport(), listening=False)
            with pytest.raises(acp.RequestError, match="Method not found"):
                await connection._conn._handler("auth/status_update", {}, True)
            _drop_unrouted_notifications(connection)
            assert await connection._conn._handler("auth/status_update", {}, True) is None

        asyncio.run(drive())


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
