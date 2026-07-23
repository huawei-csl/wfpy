"""Tests for the ``ask_user`` human-in-the-loop elicitation feature."""

from __future__ import annotations

import io

import pytest

from wfpy._agent_request_runtime import _build_agent_request
from wfpy._agent_tools_runtime import (
    ASK_USER_TOOL_NAME,
    AgentToolCall,
    _authorize_tool_call,
    _provider_tool_declarations,
)
from wfpy._elicitation_runtime import (
    ElicitationRequest,
    ElicitationResponse,
    ElicitationUnavailableError,
    build_elicit_closure,
    console_elicitation_handler,
)
from wfpy._invoke_runtime import _run_ask_user_tool
from wfpy.core import AgentSpec


class TestAskUserDeclaration:
    def test_declared_for_anthropic(self):
        decls = _provider_tool_declarations("anthropic", include_ask_user=True)
        ask = next((d for d in decls if d.get("name") == ASK_USER_TOOL_NAME), None)
        assert ask is not None
        assert "input_schema" in ask
        props = ask["input_schema"]["properties"]
        assert "question" in props
        assert "choices" in props  # multiple-choice support is in v1

    def test_declared_for_openai(self):
        decls = _provider_tool_declarations("openai", include_ask_user=True)
        names = [d.get("function", {}).get("name") for d in decls]
        assert ASK_USER_TOOL_NAME in names

    def test_not_declared_when_disabled(self):
        decls = _provider_tool_declarations("openai", include_ask_user=False)
        names = [d.get("function", {}).get("name") for d in decls]
        assert ASK_USER_TOOL_NAME not in names

    def test_deny_all_exposes_only_ask_user(self):
        # enable_tools=False (deny-all) + include_ask_user=True → ONLY ask_user
        _headers, body = _build_agent_request(
            "openai",
            "gpt-4o",
            "",
            "prompt",
            "payload",
            enable_tools=False,
            include_ask_user=True,
        )
        tools = body.get("tools")
        assert tools is not None
        names = [t.get("function", {}).get("name") for t in tools]
        assert names == [ASK_USER_TOOL_NAME]  # python is NOT exposed under deny-all

    def test_enable_tools_exposes_python_and_ask_user(self):
        _headers, body = _build_agent_request(
            "openai",
            "gpt-4o",
            "",
            "prompt",
            "payload",
            enable_tools=True,
            include_ask_user=True,
        )
        names = [t.get("function", {}).get("name") for t in body["tools"]]
        assert "python" in names
        assert ASK_USER_TOOL_NAME in names

    def test_no_tools_when_both_disabled(self):
        _headers, body = _build_agent_request(
            "openai",
            "gpt-4o",
            "",
            "prompt",
            "payload",
            enable_tools=False,
            include_ask_user=False,
        )
        assert "tools" not in body


class TestAskUserAuthorization:
    def test_ask_user_allowed_under_deny_all(self):
        call = AgentToolCall(id="c1", name=ASK_USER_TOOL_NAME, arguments={"question": "?"})
        allowed, _reason = _authorize_tool_call({"agent_tool_auth": "deny-all"}, call)
        assert allowed is True

    def test_python_still_denied_under_deny_all(self):
        call = AgentToolCall(id="c2", name="python", arguments={"code": "print(1)"})
        allowed, _reason = _authorize_tool_call({"agent_tool_auth": "deny-all"}, call)
        assert allowed is False


class TestElicitClosure:
    def test_explicit_handler_returns_answer(self):
        seen: dict[str, str] = {}

        def handler(req: ElicitationRequest) -> ElicitationResponse:
            seen["q"] = req.question
            return ElicitationResponse(answer="blue")

        ask = build_elicit_closure({"_elicitation_handler": handler}, agent_name="a")
        resp = ask("Which color?")
        assert resp.answer == "blue"
        assert seen["q"] == "Which color?"

    def test_non_interactive_declines_gracefully(self):
        ask = build_elicit_closure({"elicit_interactive": False}, agent_name="a")
        resp = ask("Which color?")
        assert resp.declined is True
        assert resp.answer is None

    def test_elicit_default_answer(self):
        ask = build_elicit_closure(
            {"elicit_interactive": False, "elicit_default": "green"}, agent_name="a"
        )
        resp = ask("Which color?")
        assert resp.answer == "green"
        assert resp.declined is False

    def test_elicit_require_raises(self):
        ask = build_elicit_closure(
            {"elicit_interactive": False, "elicit_require": True}, agent_name="a"
        )
        with pytest.raises(ElicitationUnavailableError):
            ask("Which color?")

    def test_events_published_without_leaking_answer(self):
        events: list[dict] = []
        ask = build_elicit_closure(
            {"_elicitation_handler": lambda req: ElicitationResponse(answer="secret-answer")},
            agent_name="a",
            event_publish=events.append,
        )
        ask("Q?")
        types = [e["type"] for e in events]
        assert "agent.question.requested" in types
        assert "agent.question.answered" in types
        answered = next(e for e in events if e["type"] == "agent.question.answered")
        assert "secret-answer" not in str(answered)  # replies must not hit the event bus

    def test_handler_error_declines(self):
        def bad(_req: ElicitationRequest) -> ElicitationResponse:
            raise ValueError("boom")

        ask = build_elicit_closure({"_elicitation_handler": bad}, agent_name="a")
        resp = ask("Q?")
        assert resp.declined is True


class TestConsoleHandler:
    def test_reads_stdin_line(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("teal\n"))
        req = ElicitationRequest(question="Color?", timeout_ms=0)
        resp = console_elicitation_handler(req)
        assert resp.answer == "teal"

    def test_empty_reply_declines(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
        req = ElicitationRequest(question="Color?", timeout_ms=0)
        resp = console_elicitation_handler(req)
        assert resp.declined is True

    def test_eof_declines(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        req = ElicitationRequest(question="Color?", timeout_ms=0)
        resp = console_elicitation_handler(req)
        assert resp.declined is True


class TestRunAskUserTool:
    def test_returns_answer(self):
        call = AgentToolCall(id="c1", name=ASK_USER_TOOL_NAME, arguments={"question": "Q?"})
        options = {"_wf_elicit": lambda q, **kw: ElicitationResponse(answer="42")}
        result = _run_ask_user_tool(call, options)
        assert result.stdout == "42"
        assert result.exit_code == 0

    def test_declined_is_graceful(self):
        call = AgentToolCall(id="c1", name=ASK_USER_TOOL_NAME, arguments={"question": "Q?"})
        options = {
            "_wf_elicit": lambda q, **kw: ElicitationResponse(
                answer=None, declined=True, reason="nope"
            )
        }
        result = _run_ask_user_tool(call, options)
        assert result.exit_code == 0
        assert "no answer" in result.stdout

    def test_no_closure_is_graceful(self):
        call = AgentToolCall(id="c1", name=ASK_USER_TOOL_NAME, arguments={"question": "Q?"})
        result = _run_ask_user_tool(call, {})
        assert result.exit_code == 0
        assert "No user is available" in result.stdout


class TestInvokeAgentAskUser:
    """End-to-end: model calls ask_user, we answer, the loop continues."""

    def test_ask_user_round_trip(self, monkeypatch):
        httpx = pytest.importorskip("httpx")
        from wfpy import _invoke_runtime

        responses = [
            {  # round 1: the model calls ask_user
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "ask_user",
                                        "arguments": '{"question": "Which color?"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {  # round 2: the model answers using the reply
                "choices": [{"message": {"role": "assistant", "content": "You chose blue."}}]
            },
        ]

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
                self.status_code = 200
                self.text = ""
                self.headers: dict[str, str] = {}

            def json(self):
                return self._payload

            def raise_for_status(self):
                return None

        state = {"n": 0, "seen_question": None, "bodies": []}

        def fake_post(url, json=None, headers=None, timeout=None):
            state["bodies"].append(json)
            idx = state["n"]
            state["n"] += 1
            return FakeResp(responses[idx])

        monkeypatch.setattr(httpx, "post", fake_post)

        def handler(question, **_kw):
            state["seen_question"] = question
            return ElicitationResponse(answer="blue")

        spec = AgentSpec(prompt="p", model="openai/gpt-4o", ask_user=True)
        # No agent_tool_auth → deny-all default; ask_user must still work.
        options = {"_wf_elicit": handler}
        content, _firing, err, debug = _invoke_runtime._invoke_agent(
            spec, "payload", False, effective_prompt="p", plan_options=options
        )

        assert err is None
        assert content == "You chose blue."
        assert state["seen_question"] == "Which color?"
        assert state["n"] == 2  # exactly two HTTP rounds
        # round 1 exposes only ask_user (deny-all decoupling)...
        round1_tools = [
            t.get("function", {}).get("name") for t in (state["bodies"][0].get("tools") or [])
        ]
        assert round1_tools == [ASK_USER_TOOL_NAME]
        # ...and so does the continuation round (python never leaks in).
        round2_tools = [
            t.get("function", {}).get("name") for t in (state["bodies"][1].get("tools") or [])
        ]
        assert round2_tools == [ASK_USER_TOOL_NAME]
        # the exchange is recorded for the debug artifact
        assert debug.get("elicitations")
        assert debug["elicitations"][0]["question"] == "Which color?"
        assert debug["elicitations"][0]["answer"] == "blue"

    def test_strict_mode_fails_firing(self, monkeypatch):
        httpx = pytest.importorskip("httpx")
        from wfpy import _invoke_runtime

        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "ask_user",
                                    "arguments": '{"question": "Which color?"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }

        class FakeResp:
            status_code = 200
            text = ""
            headers: dict[str, str] = {}

            def json(self):
                return response

            def raise_for_status(self):
                return None

        monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp())

        spec = AgentSpec(prompt="p", model="openai/gpt-4o", ask_user=True)
        ask = build_elicit_closure(
            {"elicit_interactive": False, "elicit_require": True}, agent_name="a"
        )
        _content, _firing, err, _debug = _invoke_runtime._invoke_agent(
            spec, "payload", False, effective_prompt="p", plan_options={"_wf_elicit": ask}
        )
        assert isinstance(err, ElicitationUnavailableError)


class TestRunnerAskUserWiring:
    """Full ``run()`` path: elicitation_handler → plan.options → _step_agent → agent."""

    def test_run_end_to_end_with_handler(self, tmp_path, monkeypatch):
        httpx = pytest.importorskip("httpx")
        from wfpy import Port, agent, connect, run, task, workflow

        @task
        class Src:
            class Ports:
                Out = Port[str](direction="out")

            _fired: bool = False

            def action(self) -> str | None:
                if self._fired:
                    return None
                self._fired = True
                return "go"

        @agent(
            prompt="Ask for the color, then answer.",
            model="openai/gpt-4o",
            provider="openai",
            use_skill=False,
            ask_user=True,
        )
        class AskAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        @workflow(outputs={"Result": str})
        def wf():
            src = Src()
            ask = AskAgent()
            connect(src.Out, ask.In)
            connect(ask.Out, "Result")

        responses = [
            {  # the agent asks the user
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "ask_user",
                                        "arguments": '{"question": "Favorite color?"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {  # ...then emits its output using the reply
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"outputs": {"Out": "blue is the color"}}',
                        }
                    }
                ]
            },
        ]

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
                self.status_code = 200
                self.text = ""
                self.headers: dict[str, str] = {}

            def json(self):
                return self._payload

            def raise_for_status(self):
                return None

        state = {"n": 0, "seen": None}

        def fake_post(url, json=None, headers=None, timeout=None):
            idx = state["n"]
            state["n"] += 1
            return FakeResp(responses[idx])

        monkeypatch.setattr(httpx, "post", fake_post)

        def handler(req: ElicitationRequest) -> ElicitationResponse:
            state["seen"] = req.question
            return ElicitationResponse(answer="blue")

        outputs = run(
            wf,
            out_dir=str(tmp_path),
            elicitation_handler=handler,
            queue_trace=False,
        )

        assert state["seen"] == "Favorite color?"  # the handler was actually invoked
        # reply-informed output flowed through (workflow outputs collect as a list)
        result = outputs["Result"]
        if isinstance(result, list):
            result = result[-1]
        assert result == "blue is the color"
