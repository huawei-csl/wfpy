"""Shared pytest fixtures for wfpy tests.

Factory fixtures that reduce boilerplate when constructing runtime objects
like RuntimeActor, FifoPlan, TaskMeta, AgentSpec, and PortDescriptor.
"""

from __future__ import annotations

from typing import Any

import pytest

from wfpy.core import AgentSpec, TaskMeta
from wfpy.runner import FifoPlan, RuntimeActor
from wfpy.types import PortDescriptor


# ── PortDescriptor factory ──────────────────────────────────────────────


@pytest.fixture
def make_port() -> ...:
    """Factory for PortDescriptor with sensible defaults.

    Usage::

        pd = make_port("Out", str, direction="out")
    """

    def _factory(
        name: str,
        port_type: Any = str,
        direction: str = "out",
        ext: str = "",
        validate: list[str] | None = None,
    ) -> PortDescriptor:
        return PortDescriptor(
            name=name,
            port_type=port_type,
            direction=direction,
            ext=ext,
            validate=validate or [],
        )

    return _factory


# ── AgentSpec factory ───────────────────────────────────────────────────


@pytest.fixture
def make_agent_spec() -> ...:
    """Factory for AgentSpec with test-friendly defaults.

    Defaults ``use_skill=False`` so tests don't need a skill directory.

    Usage::

        spec = make_agent_spec(prompt="do stuff", model="gpt-4")
        spec = make_agent_spec()  # prompt="test", model="test-model"
    """

    def _factory(
        prompt: str = "test",
        model: str = "test-model",
        use_skill: bool = False,
        **kwargs: Any,
    ) -> AgentSpec:
        return AgentSpec(prompt=prompt, model=model, use_skill=use_skill, **kwargs)

    return _factory


# ── TaskMeta factory ───────────────────────────────────────────────────


@pytest.fixture
def make_task_meta() -> ...:
    """Factory for TaskMeta with empty defaults.

    Usage::

        meta = make_task_meta(name="MyTask", kind="internal")
        meta = make_task_meta(
            output_ports={"out": make_port("out", str, "out")},
            agent_spec=spec,
        )
    """

    def _factory(
        name: str = "Task",
        kind: str = "internal",
        cls: type = object,
        ports: dict[str, PortDescriptor] | None = None,
        input_ports: dict[str, PortDescriptor] | None = None,
        output_ports: dict[str, PortDescriptor] | None = None,
        actions: list[Any] | None = None,
        parameters: dict[str, Any] | None = None,
        state_fields: dict[str, Any] | None = None,
        agent_spec: AgentSpec | None = None,
        tool_spec: Any | None = None,
        **kwargs: Any,
    ) -> TaskMeta:
        meta = TaskMeta(
            cls=cls,
            name=name,
            kind=kind,
            ports=ports or {},
            input_ports=input_ports or {},
            output_ports=output_ports or {},
            actions=actions or [],
            parameters=parameters or {},
            state_fields=state_fields or {},
        )
        if agent_spec is not None:
            meta.agent_spec = agent_spec
        if tool_spec is not None:
            meta.tool_spec = tool_spec
        return meta

    return _factory


# ── RuntimeActor factory ───────────────────────────────────────────────


@pytest.fixture
def make_actor(make_task_meta: Any) -> ...:
    """Factory for RuntimeActor with minimal TaskMeta.

    Usage::

        actor = make_actor("myactor", kind="internal")
        actor = make_actor("agent1", kind="agent", meta=custom_meta)
    """

    def _factory(
        name: str = "actor",
        kind: str = "internal",
        instance: Any | None = None,
        meta: TaskMeta | None = None,
        **meta_kwargs: Any,
    ) -> RuntimeActor:
        if meta is None:
            meta = make_task_meta(name=name, kind=kind, **meta_kwargs)
        if instance is None:
            instance = object()
        return RuntimeActor(name=name, kind=kind, instance=instance, meta=meta)

    return _factory


# ── Agent actor factory (convenience) ──────────────────────────────────


@pytest.fixture
def make_agent_actor(make_task_meta: Any, make_agent_spec: Any) -> ...:
    """Factory for agent RuntimeActors with AgentSpec.

    Usage::

        actor = make_agent_actor("agent1")
        actor = make_agent_actor("agent1", stateful=True, chat_history=[...])
    """

    def _factory(
        name: str = "agent1",
        prompt: str = "test",
        model: str = "gpt-4",
        stateful: bool = False,
        context_budget: int = 50,
        chat_history: list[dict[str, str]] | None = None,
        agent_spec: AgentSpec | None = None,
        output_ports: dict[str, PortDescriptor] | None = None,
        input_ports: dict[str, PortDescriptor] | None = None,
        parameters: dict[str, Any] | None = None,
        **spec_kwargs: Any,
    ) -> RuntimeActor:
        if agent_spec is None:
            agent_spec = make_agent_spec(
                prompt=prompt,
                model=model,
                stateful=stateful,
                context_budget=context_budget,
                **spec_kwargs,
            )
        meta = make_task_meta(
            name=name,
            kind="agent",
            agent_spec=agent_spec,
            output_ports=output_ports or {},
            input_ports=input_ports or {},
            parameters=parameters or {},
        )
        actor = RuntimeActor(
            name=name,
            kind="agent",
            instance=object(),
            meta=meta,
        )
        if chat_history is not None:
            actor.chat_history = chat_history
        return actor

    return _factory


# ── FifoPlan factory ───────────────────────────────────────────────────


@pytest.fixture
def make_plan() -> ...:
    """Factory for FifoPlan with optional field overrides.

    Usage::

        plan = make_plan(name="wf")
        plan = make_plan(source_path="/tmp/wf.py", options={"debug": True})
    """

    def _factory(
        name: str = "test",
        source_path: str = "",
        work_dir: str = "",
        options: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        search_paths: list[str] | None = None,
        **kwargs: Any,
    ) -> FifoPlan:
        plan = FifoPlan(name=name)
        if source_path:
            plan.source_path = source_path
        if work_dir:
            plan.work_dir = work_dir
        if options is not None:
            plan.options = options
        if env is not None:
            plan.env = env
        if search_paths is not None:
            plan.search_paths = search_paths
        for key, value in kwargs.items():
            setattr(plan, key, value)
        return plan

    return _factory


# ── Environment cleanup fixtures ────────────────────────────────────────


_AGENT_ENV_VARS = [
    "WF_AGENT_DEBUG",
    "WF_AGENT_PROVIDER",
    "WF_AGENT_TOKEN",
    "WF_AGENT_ENDPOINT",
    "WF_AGENT_MODEL",
    "OPENAI_API_KEY",
    "AGENT_API_KEY",
    "ANTHROPIC_API_KEY",
]

_VALIDATOR_ENV_VARS = [
    "WF_PORT_VALIDATION_MODE",
    "WF_VALIDATOR_REGISTRY_PATH",
    "WF_VALIDATOR_MCP_BRIDGE_CMD",
    "WF_VALIDATOR_MCP_BRIDGE_ARGS",
    "WF_VALIDATOR_MCP_ALLOWLIST",
]


@pytest.fixture(autouse=True)
def clean_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear all agent-related environment variables."""
    for var in _AGENT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def clean_validator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear all validator-related environment variables."""
    for var in _VALIDATOR_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ── Mock agent invocation ──────────────────────────────────────────────


@pytest.fixture
def mock_invoke_agent(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``_invoke_agent`` with a fake that captures the payload.

    Returns a dict with a ``"payload"`` key that is populated when the
    mock is called.  The mock returns a successful 4-tuple by default.

    Usage::

        captured = mock_invoke_agent
        _step_agent(actor, tmp_path, plan, verbose=False)
        assert captured["payload"]["instance"] == "agent1"
    """
    captured: dict[str, Any] = {"payload": None, "calls": 0, "request": None}

    def fake_invoke(
        agent_spec: Any, payload_text: str, verbose: bool, **kwargs: Any
    ) -> tuple[str, list[dict[str, str]], None, dict[str, Any]]:
        import json

        captured["payload"] = json.loads(payload_text)
        captured["request"] = kwargs
        captured["calls"] += 1
        return "ok", [{"role": "assistant", "content": "ok"}], None, {}

    monkeypatch.setattr("wfpy.runner._invoke_agent", fake_invoke)
    return captured
