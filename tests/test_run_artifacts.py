import json
from types import SimpleNamespace

from wfpy.runner import _AgentContextWriter, _plan_has_agents, _record_agent_chat_history
from wfpy._agent_request_runtime import _sanitize_prior_history_for_request


def test_plan_has_agents_detects_nested_agents():
    nested_agent = SimpleNamespace(kind="agent")
    nested_plan = SimpleNamespace(actors=[nested_agent])
    nested_workflow = SimpleNamespace(
        kind="workflow",
        sub_plan=nested_plan,
    )
    root_plan = SimpleNamespace(actors=[nested_workflow])

    assert _plan_has_agents(root_plan) is True


def test_non_stateful_agents_still_record_visible_chat_history():
    actor = SimpleNamespace(name="optimizer", chat_history=[])
    agent_spec = SimpleNamespace(
        stateful=False,
        truncation_strategy="sliding",
        context_budget=1,
    )

    _record_agent_chat_history(
        actor,
        agent_spec,
        [
            {"role": "user", "content": "optimize this kernel"},
            {"role": "assistant", "content": "candidate ready", "thinking": "checked constraints"},
        ],
        verbose=False,
    )

    assert [msg["role"] for msg in actor.chat_history] == ["user", "assistant"]
    assert actor.chat_history[1]["thinking"] == "checked constraints"


def test_prior_history_sanitization_drops_non_request_metadata():
    cleaned = _sanitize_prior_history_for_request(
        "openai",
        [
            {
                "role": "assistant",
                "content": "candidate ready",
                "thinking": "checked constraints",
                "replyTimeMs": 1234,
            }
        ],
    )

    assert cleaned == [{"role": "assistant", "content": "candidate ready"}]


def test_agent_context_writer_updates_for_nested_agent_history(tmp_path):
    agent_spec = SimpleNamespace(
        model="gpt-test",
        stateful=True,
        context_budget=8,
        truncation_strategy="sliding",
    )
    nested_agent = SimpleNamespace(
        kind="agent",
        name="optimizer",
        fire_count=0,
        chat_history=[],
        instance=SimpleNamespace(),
        meta=SimpleNamespace(agent_spec=agent_spec),
    )
    nested_plan = SimpleNamespace(actors=[nested_agent])
    nested_workflow = SimpleNamespace(
        kind="workflow",
        name="optimize",
        sub_plan=nested_plan,
    )
    root_plan = SimpleNamespace(actors=[nested_workflow])
    live_path = tmp_path / "run.wf-agent-context.live.json"

    writer = _AgentContextWriter(
        root_plan,
        "run-1",
        "chunk_o_optimize_composed_workflow",
        "/tmp/root.py",
        live_path,
    )

    writer.write()
    assert not live_path.exists()

    nested_agent.fire_count = 1
    nested_agent.chat_history = [{"role": "user", "content": "optimize this kernel"}]
    writer.write()

    payload = json.loads(live_path.read_text())
    assert payload["runId"] == "run-1"
    assert payload["agents"][0]["instanceName"] == "optimizer"
    assert payload["agents"][0]["chatHistory"][0]["content"] == "optimize this kernel"
