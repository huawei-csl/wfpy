"""Tests for the offline ``transport="mock"`` agent backend."""

from __future__ import annotations

import json

import pytest

from wfpy import File, Port, agent, connect, run, task, workflow
from wfpy._agent_cli_runtime import _normalize_agent_transport
from wfpy._mock_agent_runtime import build_mock_outputs, invoke_mock_agent
from wfpy.core import AgentSpec


class TestTransportNormalization:
    def test_mock_and_offline_alias_to_mock(self):
        assert _normalize_agent_transport(AgentSpec(prompt="p", transport="mock")) == "mock"
        assert _normalize_agent_transport(AgentSpec(prompt="p", transport="offline")) == "mock"
        assert _normalize_agent_transport(AgentSpec(prompt="p", transport="MOCK")) == "mock"

    def test_unknown_transport_still_rejected_and_lists_mock(self):
        with pytest.raises(ValueError, match="Unsupported agent transport") as exc:
            _normalize_agent_transport(AgentSpec(prompt="p", transport="telepathy"))
        assert "mock" in str(exc.value)

    def test_default_transport_unchanged(self):
        assert _normalize_agent_transport(AgentSpec(prompt="p")) == "http"


class TestSynthesizedValues:
    def _ports(self, **kinds):
        @agent(prompt="p", transport="mock")
        class A:
            Ports = type("Ports", (), {n: Port[t](direction="out") for n, t in kinds.items()})

        return A._wfpy_meta

    def test_values_match_declared_port_types(self):
        meta = self._ports(S=str, I=int, B=bool, F=float)
        out = build_mock_outputs(meta.agent_spec, meta.output_ports)

        assert out["S"] == "mock:S"
        assert out["I"] == 0 and isinstance(out["I"], int)
        assert out["B"] is False
        assert out["F"] == 0.0 and isinstance(out["F"], float)

    def test_file_ports_get_text_content_to_materialize(self):
        meta = self._ports(Doc=File)
        out = build_mock_outputs(meta.agent_spec, meta.output_ports)
        assert isinstance(out["Doc"], str) and out["Doc"].strip()

    def test_bool_is_not_treated_as_int(self):
        meta = self._ports(B=bool)
        assert build_mock_outputs(meta.agent_spec, meta.output_ports)["B"] is False

    def test_no_declared_ports_yields_empty_outputs(self):
        assert build_mock_outputs(AgentSpec(prompt="p", transport="mock"), {}) == {}


class TestMockOutputsOverride:
    def test_explicit_values_win_and_gaps_are_synthesized(self):
        @agent(prompt="p", transport="mock", mock_outputs={"Summary": "fixed"})
        class A:
            class Ports:
                Summary = Port[str](direction="out")
                Score = Port[int](direction="out")

        out = build_mock_outputs(A._wfpy_meta.agent_spec, A._wfpy_meta.output_ports)
        assert out["Summary"] == "fixed"
        assert out["Score"] == 0

    def test_camelcase_alias_accepted(self):
        @agent(prompt="p", transport="mock", mockOutputs={"Summary": "camel"})
        class A:
            class Ports:
                Summary = Port[str](direction="out")

        assert A._wfpy_meta.agent_spec.mock_outputs == {"Summary": "camel"}

    def test_undeclared_port_is_kept_not_dropped(self):
        # Surfaces as a normal output-validation error rather than vanishing.
        spec = AgentSpec(prompt="p", transport="mock", mock_outputs={"Typo": 1})
        assert build_mock_outputs(spec, {}) == {"Typo": 1}


class TestInvokeContract:
    def test_returns_the_standard_four_tuple(self):
        @agent(prompt="p", transport="mock")
        class A:
            class Ports:
                Out = Port[str](direction="out")

        meta = A._wfpy_meta
        text, messages, error, debug = invoke_mock_agent(
            meta.agent_spec, "payload", output_ports=meta.output_ports
        )

        assert error is None
        assert json.loads(text) == {"outputs": {"Out": "mock:Out"}}
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "payload"
        assert debug["transport"] == "mock" and debug["mock"] is True


class TestEndToEnd:
    def test_agent_graph_runs_without_credentials(self, monkeypatch, tmp_path):
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
            monkeypatch.delenv(var, raising=False)

        @agent(prompt="Summarize.", transport="mock", mock_outputs={"Summary": "a summary"})
        class Summarizer:
            class Ports:
                In = Port[str](direction="in")
                Summary = Port[str](direction="out")

        @task
        class Shout:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

            def action(self, text: str) -> str:
                return f"{text.upper()}!"

        @workflow(inputs={"In": str}, outputs={"Out": str})
        def demo():
            s = Summarizer()
            u = Shout()
            connect("In", s.In)
            connect(s.Summary, u.In)
            connect(u.Out, "Out")

        result = run(demo, inputs={"In": "long text"}, out_dir=str(tmp_path), verbose=False)
        assert result["Out"] == ["A SUMMARY!"]

    def test_bare_agent_needs_no_skill(self, monkeypatch, tmp_path):
        """Regression: `use_skill` defaults True, and used to raise without a skill."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        @agent(prompt="Do a thing.", transport="mock")
        class Bare:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        @workflow(inputs={"In": str}, outputs={"Out": str})
        def demo():
            b = Bare()
            connect("In", b.In)
            connect(b.Out, "Out")

        result = run(demo, inputs={"In": "x"}, out_dir=str(tmp_path), verbose=False)
        assert result["Out"] == ["mock:Out"]
