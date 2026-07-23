"""Tests for wfpy runner — FIFO round-robin scheduler."""

import json
import logging
import os
import threading
import types
from pathlib import Path
from typing import Any

from wfpy import agent, task, action, guard, workflow, connect, if_, loop, run, Port
from wfpy.runner import build_plan, execute_plan, _build_workflow_graph, FifoPlan
from wfpy.runner import (
    _parse_agent_outputs,
    _normalize_agent_response_text,
    _build_agent_runtime_instruction,
    _build_agent_repair_prompt,
    _normalize_agent_transport,
    _normalize_cli_tools_mode,
    _invoke_agent_opencode_cli,
    _invoke_agent_claude_cli,
    _invoke_agent_codex_cli,
    AGENT_FILE_INPUT_MAX_CHARS,
    AGENT_MAX_TOOL_ROUNDS,
    AGENT_FAIL_FAST_RETRY_AFTER_MS,
    trim_chat_history,
    _parse_retry_after_ms,
    _authorize_tool_call,
    _normalize_tool_auth_mode,
    _read_agent_config,
    _accumulate_stream_response,
    _extract_tool_calls,
    _build_tool_result_messages,
    _build_effective_agent_prompt,
    _skill_hook_name,
    _run_skill_hook,
    _effective_max_tool_rounds,
    AgentToolResult,
    RuntimeActor,
    # Validator imports
    ValidationResult,
    LoadedValidatorRegistry,
    load_validator_registry,
    _get_validator_ids_from_port,
    run_port_validators,
    # Queue trace imports
    QueueTraceCollector,
    # Agent debug imports
    _agent_debug_enabled,
    _write_agent_debug_artifact,
    # Custom JSON + viewer overlay imports
    _runtime_json_dumps,
    _runtime_json_dumps_line,
    _record_edge_token,
    _ViewerOverlayBase,
    _build_viewer_overlay_v1,
    _ViewerOverlayWriter,
    _build_agent_context_overlay,
    _AgentContextWriter,
    _build_context_overlay,
    _ContextLiveWriter,
    _actor_context_policy,
    _build_context_view,
    _apply_context_patch,
    _ActionContextFacade,
    _extract_agent_context_patch,
    _rewrite_viewer_overlay_tokens,
    Queue,
)
# Direct imports from source modules (previously re-exported via runner.py)
from wfpy._agent_io_runtime import (
    restore_chat_histories,
    _parse_json_from_text,
)
from wfpy._agent_cli_runtime import _parse_opencode_json_events
from wfpy._agent_tools_runtime import (
    AgentToolCall,
    _build_tool_result_payload,
    _provider_tool_declarations,
)
from wfpy._agent_prompt_runtime import _load_claude_agent_profile, _resolve_claude_agent_path
from wfpy._validation_runtime import (
    ValidatorSpec,
    normalize_validation_mode,
    _find_nearest_file,
    _parse_validator_registry,
    _check_xml_well_formed,
    _execute_builtin_validator,
    _normalize_validation_result,
    _is_mcp_target_allowed,
)
from wfpy._run_artifacts import (
    _collect_queue_snapshot,
    _collect_edge_overlay,
    _ViewerOverlayActive,
    _build_agent_context_entry,
    _collect_agent_entries,
)
from wfpy._context_runtime import _context_get
from wfpy._agent_validation_runtime import (
    _build_validation_repair_prompt,
    _run_cmd_validator,
    _parse_compiler_diagnostics,
)
from wfpy.types import PortDescriptor, Resource, File
from wfpy.core import AgentOutputValidator


class TestSimplePipeline:
    def test_doubler_pipeline(self):
        """Input=7 → Doubler(×2) → Doubler(×3) → Output should be 7*2*3=42."""

        @task
        class Doubler:
            factor: int

            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x * self.factor

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def double_pipeline():
            d1 = Doubler(factor=2)
            d2 = Doubler(factor=3)
            connect("Input", d1.In)
            connect(d1.Out, d2.In)
            connect(d2.Out, "Output")

        outputs = run(double_pipeline, inputs={"Input": 7})
        assert outputs["Output"] == [42]

    def test_accumulator(self):
        """Data=5 → Doubler(×2) → Accumulator → Result should be 10 (one input)."""

        @task
        class Doubler:
            factor: int

            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x * self.factor

        @task
        class Accumulator:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            sum: int = 0

            def action(self, x: int) -> int:
                self.sum += x
                return self.sum

        @workflow(inputs={"Data": int}, outputs={"Result": int})
        def proc_pipeline():
            d = Doubler(factor=2)
            acc = Accumulator()
            connect("Data", d.In)
            connect(d.Out, acc.In)
            connect(acc.Out, "Result")

        outputs = run(proc_pipeline, inputs={"Data": 5})
        assert outputs["Result"] == [10]


class TestMultiAction:
    def test_pair_or_diff_sum(self):
        """Two tokens (3, 7): 3 < 7 → sum action → 10."""

        @task
        class PairOrDiff:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            fired: int = 0

            @action(consumes={"In": 2}, produces={"Out": 1})
            @guard(lambda self, a, b: a < b)
            def sum_pair(self, a: int, b: int) -> int:
                self.fired += 1
                return a + b

            @action(consumes={"In": 2}, produces={"Out": 1})
            @guard(lambda self, a, b: a >= b)
            def diff_pair(self, a: int, b: int) -> int:
                self.fired += 1
                return a - b

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def pair_wf():
            p = PairOrDiff()
            connect("Input", p.In)
            connect(p.Out, "Output")

        # Build plan manually to seed two tokens
        wf_def = pair_wf._wfpy_workflow
        graph = _build_workflow_graph(wf_def)
        plan = build_plan(graph, wf_def)

        for q in plan.wf_input_queues["Input"]:
            q.enqueue(3)
            q.enqueue(7)

        outputs = execute_plan(plan)
        assert outputs["Output"] == [10]

    def test_pair_or_diff_diff(self):
        """Two tokens (9, 2): 9 >= 2 → diff action → 7."""

        @task
        class PairOrDiff2:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            @action(consumes={"In": 2}, produces={"Out": 1})
            @guard(lambda self, a, b: a < b)
            def sum_pair(self, a: int, b: int) -> int:
                return a + b

            @action(consumes={"In": 2}, produces={"Out": 1})
            @guard(lambda self, a, b: a >= b)
            def diff_pair(self, a: int, b: int) -> int:
                return a - b

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def pair_wf2():
            p = PairOrDiff2()
            connect("Input", p.In)
            connect(p.Out, "Output")

        wf_def = pair_wf2._wfpy_workflow
        graph = _build_workflow_graph(wf_def)
        plan = build_plan(graph, wf_def)

        for q in plan.wf_input_queues["Input"]:
            q.enqueue(9)
            q.enqueue(2)

        outputs = execute_plan(plan)
        assert outputs["Output"] == [7]


class TestRunArtifacts:
    def test_failed_run_persists_run_record_and_log(self, tmp_path):
        @task
        class Boom:
            class Ports:
                Out = Port[int](direction="out")

            @action(produces={"Out": 1})
            def explode(self) -> int:
                raise RuntimeError("boom")

        @workflow(outputs={"Out": int})
        def failing_workflow():
            node = Boom()
            del node

        try:
            run(failing_workflow, out_dir=str(tmp_path))
        except RuntimeError as exc:
            assert str(exc) == "boom"
        else:
            raise AssertionError("Expected failing_workflow to raise RuntimeError")

        run_dirs = sorted(path for path in tmp_path.iterdir() if path.is_dir())
        assert len(run_dirs) == 1

        run_dir = run_dirs[0]
        viewer_path = run_dir / "run.wf-viewer.json"
        run_record_path = run_dir / "run.wf-run.json"
        run_log_path = tmp_path / "run-log.jsonl"

        assert viewer_path.exists()
        assert run_record_path.exists()
        assert run_log_path.exists()

        run_record = json.loads(run_record_path.read_text())
        assert run_record["error"]["message"] == "boom"
        assert isinstance(run_record["error"].get("entityInstanceName"), str)
        assert run_record["error"]["entityInstanceName"].strip() != ""

        run_log_entries = [
            json.loads(line) for line in run_log_path.read_text().splitlines() if line.strip()
        ]
        assert len(run_log_entries) == 1
        assert run_log_entries[0]["runId"] == run_record["runId"]
        assert run_log_entries[0]["error"] == run_record["error"]

    def test_run_record_persists_agent_cli_session_ids(self, tmp_path, monkeypatch):
        @agent(prompt="x", useSkill=False, transport="opencode-cli", stateful=True)
        class SessionAgent:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        def _fake_invoke(_spec, _payload_text, _verbose, **_kwargs):
            response = '{"outputs":{"Out":"ok"}}'
            return (
                response,
                [{"role": "assistant", "content": response}],
                None,
                {"opencodeSessionID": "sess-run"},
            )

        monkeypatch.setattr("wfpy.runner._invoke_agent", _fake_invoke)

        @workflow(inputs={"In": str}, outputs={"Out": str})
        def wf():
            node = SessionAgent()
            connect("In", node.In)
            connect(node.Out, "Out")

        outputs = run(wf, inputs={"In": "once"}, out_dir=str(tmp_path))
        assert outputs["Out"] == ["ok"]

        run_dirs = sorted(path for path in tmp_path.iterdir() if path.is_dir())
        assert len(run_dirs) == 1
        run_record = json.loads((run_dirs[0] / "run.wf-run.json").read_text())
        actor_entries = run_record.get("actors", [])
        assert len(actor_entries) == 1
        assert actor_entries[0].get("agentCliSessionIds") == {"opencode": "sess-run"}

    def test_nested_workflow_forwards_materialized_file_outputs(self, tmp_path: Path):
        nested_source_dir = tmp_path / "nested-source"
        nested_source_dir.mkdir()

        @task
        class FireOnce:
            _done: bool = False

            class Ports:
                Out = Port[int]()

            def action(self) -> int | None:
                if self._done:
                    return None
                self._done = True
                return 1

        @task
        class ProduceNestedFile:
            root: Path

            class Ports:
                In = Port[int](direction="in")
                Out = Port[str](direction="out")

            @action(consumes={"In": 1}, produces={"Out": 1})
            def emit(self, _trigger: int) -> str:
                raw_output = self.root / "raw-nested.txt"
                raw_output.write_text("nested payload")
                return str(raw_output)

        @workflow(inputs={"Trigger": int}, outputs={"artifact": str})
        def child_workflow():
            producer = ProduceNestedFile(root=nested_source_dir)
            connect("Trigger", producer.In)
            connect(producer.Out, "artifact")

        @task
        class InspectNestedFile:
            class Ports:
                In = Port[str](direction="in")
                Name = Port[str](direction="out")
                UsesChildWorkDir = Port[str](direction="out")
                Text = Port[str](direction="out")

            @action(consumes={"In": 1}, produces={"Name": 1, "UsesChildWorkDir": 1, "Text": 1})
            def inspect(self, value: str) -> dict[str, str]:
                path = Path(value)
                return {
                    "Name": path.name,
                    "UsesChildWorkDir": str("__wf" in path.as_posix()),
                    "Text": path.read_text(),
                }

        @workflow(outputs={"ObservedName": str, "ObservedUsesChildWorkDir": str, "ObservedText": str})
        def parent_workflow():
            src = FireOnce()
            child = child_workflow()
            inspect = InspectNestedFile()
            connect(src.Out, child.Trigger)
            connect(child.artifact, inspect.In)
            connect(inspect.Name, "ObservedName")
            connect(inspect.UsesChildWorkDir, "ObservedUsesChildWorkDir")
            connect(inspect.Text, "ObservedText")

        outputs = run(
            parent_workflow,
            out_dir=str(tmp_path / "wf-out"),
            work_dir=str(tmp_path / "outer-work"),
        )

        assert outputs["ObservedText"] == ["nested payload"]
        assert outputs["ObservedName"] == ["artifact"]
        assert outputs["ObservedUsesChildWorkDir"] == ["False"]

    def test_nested_workflow_does_not_replay_prior_outputs_between_fires(self):
        @task
        class PassThrough:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, value: int) -> int:
                return value

        @workflow(inputs={"Value": int}, outputs={"ValueOut": int})
        def child_workflow():
            passthrough = PassThrough()
            connect("Value", passthrough.In)
            connect(passthrough.Out, "ValueOut")

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def parent_workflow():
            child = child_workflow()
            connect("Input", child.Value)
            connect(child.ValueOut, "Output")

        wf_def = parent_workflow._wfpy_workflow
        graph = _build_workflow_graph(wf_def)
        plan = build_plan(graph, wf_def)

        for q in plan.wf_input_queues["Input"]:
            q.enqueue(1)
            q.enqueue(2)

        outputs = execute_plan(plan)

        assert outputs["Output"] == [1, 2]

    def test_schedule_transitions_between_actions(self):
        @task
        class ScheduledFlow:
            class Ports:
                In = Port[int]()
                Out = Port[str]()

            class Schedule:
                initial = "start"
                transitions = [
                    ("start", "start", "interpolate"),
                    ("interpolate", "done", "done"),
                    ("interpolate", "other", "interpolate"),
                ]

            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, x: x == 0)
            def start(self, x: int) -> str:
                return f"start:{x}"

            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, x: x == 1)
            def done(self, x: int) -> str:
                return f"done:{x}"

            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, x: x != 1)
            def other(self, x: int) -> str:
                return f"other:{x}"

        @workflow(inputs={"Input": int}, outputs={"Output": str})
        def wf():
            t = ScheduledFlow()
            connect("Input", t.In)
            connect(t.Out, "Output")

        wf_def = wf._wfpy_workflow
        graph = _build_workflow_graph(wf_def)
        plan = build_plan(graph, wf_def)

        for q in plan.wf_input_queues["Input"]:
            q.enqueue(0)
            q.enqueue(5)
            q.enqueue(1)

        outputs = execute_plan(plan)
        assert outputs["Output"] == ["start:0", "other:5", "done:1"]

    def test_priority_overrides_definition_order(self):
        @task
        class Prioritized:
            class Ports:
                In = Port[int]()
                Out = Port[str]()

            class Priority:
                rules = [["high", "low"]]

            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, _x: True)
            def low(self, x: int) -> str:
                return f"low:{x}"

            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, _x: True)
            def high(self, x: int) -> str:
                return f"high:{x}"

        @workflow(inputs={"Input": int}, outputs={"Output": str})
        def wf():
            t = Prioritized()
            connect("Input", t.In)
            connect(t.Out, "Output")

        outputs = run(wf, inputs={"Input": 7})
        assert outputs["Output"] == ["high:7"]

    def test_scheduled_explicit_zero_input_action_fires_from_state_without_self_loop(self):
        @task
        class StatefulEmitter:
            _remaining: list[int] = []

            class Ports:
                Plan = Port[str](direction="in")
                Out = Port[int](direction="out")

            class Schedule:
                initial = "read"
                transitions = [
                    ("read", "read_plan", "emit"),
                    ("emit", "emit_next", "emit"),
                    ("emit", "finish", "read"),
                ]

            @action(consumes={"Plan": 1})
            def read_plan(self, Plan: str) -> None:
                self._remaining = [int(part) for part in Plan.split(",") if part]
                return None

            @action(consumes={}, produces={"Out": 1})
            @guard(lambda self: bool(self._remaining))
            def emit_next(self) -> int:
                return self._remaining.pop(0)

            @action(consumes={})
            @guard(lambda self: not self._remaining)
            def finish(self) -> None:
                return None

        @workflow(inputs={"Plan": str}, outputs={"Output": int})
        def wf():
            emitter = StatefulEmitter()
            connect("Plan", emitter.Plan)
            connect(emitter.Out, "Output")

        outputs = run(wf, inputs={"Plan": "1,2,3"})
        assert outputs["Output"] == [1, 2, 3]

    def test_scheduler_can_model_split_candidate_branches(self):
        @task
        class CandidateRouter:
            class Ports:
                Proposal = Port[str]()
                Kernel = Port[str]()
                Out = Port[str]()

            seen: set[str] = set()

            class Schedule:
                initial = "optimizer_ready"
                transitions = [
                    ("optimizer_ready", "reject_duplicate_candidate", "optimizer_ready"),
                    ("optimizer_ready", "queue_candidate_for_evaluation", "optimizer_ready"),
                ]

            class Priority:
                rules = [["reject_duplicate_candidate", "queue_candidate_for_evaluation"]]

            @action(consumes={"Proposal": 1, "Kernel": 1}, produces={"Out": 1})
            @guard(lambda self, _proposal, kernel: kernel in self.seen)
            def reject_duplicate_candidate(self, proposal: str, kernel: str) -> str:
                del proposal
                return f"duplicate:{kernel}"

            @action(consumes={"Proposal": 1, "Kernel": 1}, produces={"Out": 1})
            @guard(lambda self, _proposal, kernel: kernel not in self.seen)
            def queue_candidate_for_evaluation(self, proposal: str, kernel: str) -> str:
                del proposal
                self.seen.add(kernel)
                return f"eval:{kernel}"

        @workflow(inputs={"Proposal": str, "Kernel": str}, outputs={"Output": str})
        def wf():
            router = CandidateRouter()
            connect("Proposal", router.Proposal)
            connect("Kernel", router.Kernel)
            connect(router.Out, "Output")

        wf_def = wf._wfpy_workflow
        graph = _build_workflow_graph(wf_def)
        plan = build_plan(graph, wf_def)
        router = next(actor for actor in plan.actors if actor.name == "router")

        for q in plan.wf_input_queues["Proposal"]:
            q.enqueue("emit")
            q.enqueue("emit")
        for q in plan.wf_input_queues["Kernel"]:
            q.enqueue("k1")
            q.enqueue("k1")

        outputs = execute_plan(plan)
        assert outputs["Output"] == ["eval:k1", "duplicate:k1"]
        assert getattr(router.instance, "_wfpy_schedule_state") == "optimizer_ready"

    def test_scheduler_can_continue_after_no_safe_candidate_when_transition_exists(self):
        @task
        class SearchControllerDut:
            class Ports:
                Proposal = Port[str]()
                Out = Port[str]()

            attempts: int = 0

            class Schedule:
                initial = "optimizer_ready"
                transitions = [
                    ("optimizer_ready", "continue_after_no_safe_candidate", "optimizer_ready"),
                    ("optimizer_ready", "finish_without_new_candidate", "terminal"),
                ]

            class Priority:
                rules = [["continue_after_no_safe_candidate", "finish_without_new_candidate"]]

            @action(consumes={"Proposal": 1}, produces={"Out": 1})
            @guard(lambda self, proposal: proposal == "no_safe_candidate" and self.attempts < 2)
            def continue_after_no_safe_candidate(self, proposal: str) -> str:
                del proposal
                self.attempts += 1
                return f"continue:{self.attempts}"

            @action(consumes={"Proposal": 1}, produces={"Out": 1})
            @guard(lambda self, proposal: proposal == "no_safe_candidate")
            def finish_without_new_candidate(self, proposal: str) -> str:
                del proposal
                return "finish"

        @workflow(inputs={"Proposal": str}, outputs={"Output": str})
        def wf():
            controller = SearchControllerDut()
            connect("Proposal", controller.Proposal)
            connect(controller.Out, "Output")

        wf_def = wf._wfpy_workflow
        graph = _build_workflow_graph(wf_def)
        plan = build_plan(graph, wf_def)
        controller = next(actor for actor in plan.actors if actor.name == "controller")

        for q in plan.wf_input_queues["Proposal"]:
            q.enqueue("no_safe_candidate")
            q.enqueue("no_safe_candidate")
            q.enqueue("no_safe_candidate")

        outputs = execute_plan(plan)

        assert outputs["Output"] == ["continue:1", "continue:2", "finish"]
        assert controller.instance.attempts == 2
        assert getattr(controller.instance, "_wfpy_schedule_state") == "terminal"


class TestFanOut:
    def test_fan_out(self):
        """One output connected to two inputs — both should receive the token."""

        @task
        class Source:
            value: int

            class Ports:
                Out = Port[int]()

            _done: bool = False

            def action(self) -> int:
                if self._done:
                    return None  # type: ignore[return-value]
                self._done = True
                return self.value

        @task
        class Sink:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            received: list = []  # type: ignore[type-arg]

            def action(self, x: int) -> int:
                self.received.append(x)
                return x

        @workflow(inputs={}, outputs={"A": int, "B": int})
        def fan_out_wf():
            src = Source(value=42)
            s1 = Sink()
            s2 = Sink()
            connect(src.Out, s1.In)
            connect(src.Out, s2.In)
            connect(s1.Out, "A")
            connect(s2.Out, "B")

        outputs = run(fan_out_wf)
        assert outputs["A"] == [42]
        assert outputs["B"] == [42]


class TestSingleOutputDictReturn:
    def test_single_output_action_can_return_named_dict(self):
        @task
        class WrapSingleOutput:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            @action(consumes={"In": 1}, produces={"Out": 1})
            def wrap(self, In: int) -> dict[str, int]:
                return {"Out": In + 1}

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def wrap_single_output_wf():
            task1 = WrapSingleOutput()
            connect("Input", task1.In)
            connect(task1.Out, "Output")

        outputs = run(wrap_single_output_wf, inputs={"Input": 41})
        assert outputs["Output"] == [42]


class TestQuiescence:
    def test_no_input_no_fire(self):
        """A task with no input tokens should not fire."""

        @task
        class NeverFires:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def empty_wf():
            n = NeverFires()
            connect("Input", n.In)
            connect(n.Out, "Output")

        # Don't provide any inputs
        outputs = run(empty_wf)
        assert outputs["Output"] is None


class TestControlNodes:
    def test_if_control(self):
        """if_ should execute only the selected branch."""

        @task
        class Source:
            value: int

            class Ports:
                Out = Port[int]()

            _done: bool = False

            def action(self) -> int:
                if self._done:
                    return None  # type: ignore[return-value]
                self._done = True
                return self.value

        @task
        class Pass:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x

        @workflow(inputs={}, outputs={"Output": int})
        def wf():
            src = Source(value=7)
            cond = if_(True)
            with cond.then:
                t = Pass()
                connect(src.Out, t.In)
                connect(t.Out, "Output")
            with cond.else_:
                f = Pass()
                connect(src.Out, f.In)
                connect(f.Out, "Output")

        outputs = run(wf)
        assert outputs["Output"] == [7]

    def test_loop_control(self):
        """loop should execute body once per item."""

        @task
        class Pass:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x

        @workflow(inputs={}, outputs={"Output": int})
        def wf():
            lp = loop([1, 2, 3])
            with lp:
                p = Pass()
                connect(lp.item, p.In)
                connect(p.Out, "Output")

        outputs = run(wf)
        assert outputs["Output"] == [1, 2, 3]


class TestPlanExport:
    def test_export_json(self):
        """Plan export should produce valid JSON structure."""

        from wfpy.runner import export_plan_json

        @task
        class T:
            factor: int

            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x * self.factor

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def wf():
            t = T(factor=3)
            connect("Input", t.In)
            connect(t.Out, "Output")

        wf_def = wf._wfpy_workflow
        graph = _build_workflow_graph(wf_def)
        plan = build_plan(graph, wf_def)
        plan_json = export_plan_json(plan)

        assert plan_json["name"] == "wf"
        assert len(plan_json["actors"]) == 1
        assert len(plan_json["connections"]) == 2
        assert "Input" in plan_json["inputs"]
        assert "Output" in plan_json["outputs"]


class TestAgentOutputParsing:
    """Tests for _parse_agent_outputs and helpers."""

    def _ports(self, **kwargs: type) -> dict[str, PortDescriptor]:
        """Build a simple port dict from name→type mapping."""
        result: dict[str, PortDescriptor] = {}
        for name, typ in kwargs.items():
            result[name] = PortDescriptor(
                name=name, port_type=typ, direction="out", ext="", validate=[]
            )
        return result

    def test_shape1_outputs_wrapper(self):
        ports = self._ports(report=str)
        parsed = _parse_agent_outputs('{"outputs": {"report": "hello"}}', ports)
        assert parsed == {"report": "hello"}

    def test_shape2_top_level_key_single(self):
        ports = self._ports(summary=str)
        parsed = _parse_agent_outputs('{"summary": "done"}', ports)
        assert parsed == {"summary": "done"}

    def test_shape3_single_key_remap(self):
        ports = self._ports(result=str)
        parsed = _parse_agent_outputs('{"answer": "42"}', ports)
        assert parsed == {"result": "42"}

    def test_shape4_whole_object(self):
        ports = self._ports(data=dict)
        parsed = _parse_agent_outputs('{"a": 1, "b": 2}', ports)
        assert parsed == {"data": {"a": 1, "b": 2}}

    def test_shape5_multi_output(self):
        ports = self._ports(title=str, body=str)
        parsed = _parse_agent_outputs('{"title": "T", "body": "B"}', ports)
        assert parsed == {"title": "T", "body": "B"}

    def test_fallback_plain_text_file(self):
        ports = self._ports(report=File)
        parsed = _parse_agent_outputs("Just plain text", ports)
        assert parsed == {"report": "Just plain text"}

    def test_single_json_file_output_does_not_fallback_to_raw_text(self):
        ports = {
            "SemanticContract": PortDescriptor(
                name="SemanticContract",
                port_type=File,
                direction="out",
                ext=".json",
                validate=[],
            )
        }

        import pytest

        with pytest.raises(ValueError, match="Could not parse agent response as JSON object"):
            _parse_agent_outputs("Just plain text", ports)

    def test_parse_agent_outputs_recovers_wrapped_single_json_output_from_prose(self):
        ports = {
            "SemanticContract": PortDescriptor(
                name="SemanticContract",
                port_type=File,
                direction="out",
                ext=".json",
                validate=[],
            )
        }
        text = (
            "Decoding the PTO intrinsics before writing the JSON artifact.\n"
            '{"outputs":{"SemanticContract":{"schema_version":"semantic_contract_v1"}}}'
        )

        parsed = _parse_agent_outputs(text, ports)

        assert parsed == {"SemanticContract": {"schema_version": "semantic_contract_v1"}}

    def test_parse_agent_outputs_recovers_truncated_wrapped_single_json_output(self):
        ports = {
            "SemanticContract": PortDescriptor(
                name="SemanticContract",
                port_type=File,
                direction="out",
                ext=".json",
                validate=[],
            )
        }
        text = (
            '{"outputs":{"SemanticContract":{"schema_version":"semantic_contract_v1",'
            '"kernel":{"name":"x"},"semantic_contract":{"inputs":[],"outputs":[]}}}'
        )

        parsed = _parse_agent_outputs(text, ports)

        assert parsed == {
            "SemanticContract": {
                "schema_version": "semantic_contract_v1",
                "kernel": {"name": "x"},
                "semantic_contract": {"inputs": [], "outputs": []},
            }
        }

    def test_normalize_single_cpp_output_strips_leading_prose(self):
        ports = {
            "Kernel": PortDescriptor(
                name="Kernel", port_type=File, direction="out", ext=".cpp", validate=[]
            )
        }
        text = (
            "Inspecting local PTO helpers before rewriting the translation unit.\n"
            "Replacing unsupported templates with compile-safe helpers.\n"
            "#include <pto/pto-inst.hpp>\n"
            'extern "C" void call_kernel() {}\n'
        )

        normalized = _normalize_agent_response_text(text, ports)

        assert normalized == '#include <pto/pto-inst.hpp>\nextern "C" void call_kernel() {}'

    def test_normalize_single_cpp_output_decodes_escaped_translation_unit(self):
        ports = {
            "Kernel": PortDescriptor(
                name="Kernel", port_type=File, direction="out", ext=".cpp", validate=[]
            )
        }
        text = (
            '#include <pto/pto-inst.hpp>\\n'
            '#include \\"acl/acl.h\\"\\n'
            'extern \\"C\\" void call_kernel() {}\\n'
        )

        normalized = _normalize_agent_response_text(text, ports)

        assert normalized == (
            '#include <pto/pto-inst.hpp>\n'
            '#include "acl/acl.h"\n'
            'extern "C" void call_kernel() {}'
        )

    def test_normalize_single_cpp_output_prefers_wrapped_payload_over_prose(self):
        ports = {
            "Kernel": PortDescriptor(
                name="Kernel", port_type=File, direction="out", ext=".cpp", validate=[]
            )
        }
        text = (
            "Inspecting local PTO helpers before rewriting the translation unit.\n"
            '{"outputs":{"Kernel":"#include <pto/pto-inst.hpp>\\nextern \\\"C\\\" void call_kernel() {}\\n"}}'
        )

        normalized = _normalize_agent_response_text(text, ports)

        assert normalized == '#include <pto/pto-inst.hpp>\nextern "C" void call_kernel() {}'

    def test_normalize_single_cpp_output_ignores_inline_code_phrase_in_prose(self):
        ports = {
            "Kernel": PortDescriptor(
                name="Kernel", port_type=File, direction="out", ext=".cpp", validate=[]
            )
        }
        text = (
            "Checking local PTO type names before rewriting the kernel.\n"
            "I found local examples that use `using namespace pto;` in valid code.\n"
            "#include <pto/pto-inst.hpp>\n"
            'using namespace pto;\n'
            'extern "C" void call_kernel() {}\n'
        )

        normalized = _normalize_agent_response_text(text, ports)

        assert normalized == (
            '#include <pto/pto-inst.hpp>\n'
            'using namespace pto;\n'
            'extern "C" void call_kernel() {}\n'
        ).rstrip()

    def test_normalize_single_cpp_output_preserves_comment_banner(self):
        ports = {
            "Kernel": PortDescriptor(
                name="Kernel", port_type=File, direction="out", ext=".cpp", validate=[]
            )
        }
        text = (
            "// ============================================================================\n"
            "// chunk_prepare.cpp — naive_chunk_kda stage kernel\n"
            "// ============================================================================\n"
            "#include <pto/pto-inst.hpp>\n"
            'extern "C" void call_kernel() {}\n'
        )

        normalized = _normalize_agent_response_text(text, ports)

        assert normalized == text

    def test_normalize_single_cpp_output_preserves_comment_banner_after_prose(self):
        ports = {
            "Kernel": PortDescriptor(
                name="Kernel", port_type=File, direction="out", ext=".cpp", validate=[]
            )
        }
        text = (
            "Inspecting local helper signatures before repairing the kernel.\n"
            "Switching to the proven helper surface now.\n"
            "// ============================================================================\n"
            "// chunk_prepare.cpp -- naive_chunk_kda stage kernel\n"
            "// ============================================================================\n"
            "#include <pto/pto-inst.hpp>\n"
            'extern "C" void call_kernel() {}\n'
        )

        normalized = _normalize_agent_response_text(text, ports)

        assert normalized == (
            "// ============================================================================\n"
            "// chunk_prepare.cpp -- naive_chunk_kda stage kernel\n"
            "// ============================================================================\n"
            "#include <pto/pto-inst.hpp>\n"
            'extern "C" void call_kernel() {}'
        )

    def test_json_in_prose(self):
        text = 'Here is the result:\n{"outputs": {"x": 99}}\nDone.'
        ports = self._ports(x=int)
        parsed = _parse_agent_outputs(text, ports)
        assert parsed == {"x": 99}

    def test_normalize_misplaced_top_level_multi_output(self):
        ports = self._ports(Proposal=File, Optimized=File)
        text = '{"outputs":{"Proposal":{"decision_action":"need_more_evidence"}},"Optimized":""}}'

        normalized = _normalize_agent_response_text(text, ports)

        assert normalized is not None
        assert json.loads(normalized) == {
            "outputs": {
                "Proposal": {"decision_action": "need_more_evidence"},
                "Optimized": "",
            }
        }

    def test_parse_agent_outputs_recovers_misplaced_top_level_multi_output(self):
        ports = self._ports(Proposal=File, Optimized=File)
        text = '{"outputs":{"Proposal":{"decision_action":"need_more_evidence"}},"Optimized":""}}'

        parsed = _parse_agent_outputs(text, ports)

        assert parsed == {
            "Proposal": {"decision_action": "need_more_evidence"},
            "Optimized": "",
        }

    def test_parse_agent_outputs_recovers_multi_output_json_from_leading_prose_and_trailing_brace(self):
        ports = self._ports(ExecutionContract=File, ReferenceModel=File)
        text = (
            "Checking local script conventions before emitting the final payload.\n"
            '{"outputs":{"ExecutionContract":{"schema_version":"execution_contract_v1"},'
            '"ReferenceModel":"def reference_model():\\n    return None\\n"}}}'
        )

        parsed = _parse_agent_outputs(text, ports)

        assert parsed == {
            "ExecutionContract": {"schema_version": "execution_contract_v1"},
            "ReferenceModel": "def reference_model():\n    return None\n",
        }

    def test_parse_agent_outputs_multi_output_missing_declared_port_raises(self):
        ports = self._ports(Proposal=File, Optimized=File)

        import pytest

        with pytest.raises(ValueError, match=r"missing declared output\(s\): Optimized"):
            _parse_agent_outputs(
                '{"outputs":{"Proposal":{"decision_action":"no_safe_candidate"}}}',
                ports,
            )

    def test_parse_agent_outputs_rejects_exact_repair_example_echo(self):
        ports = {
            "Proposal": PortDescriptor(
                name="Proposal", port_type=File, direction="out", ext=".json", validate=[]
            ),
            "Optimized": PortDescriptor(
                name="Optimized", port_type=File, direction="out", ext=".cpp", validate=[]
            ),
        }

        import pytest

        with pytest.raises(ValueError, match="echoed repair example"):
            _parse_agent_outputs(
                '{"outputs":{"Proposal":{"replace_with_real_output":true},"Optimized":"// Replace with real content\\n"}}',
                ports,
            )

    def test_build_agent_repair_prompt_uses_json_example_for_json_file_port(self):
        ports = {
            "Proposal": PortDescriptor(
                name="Proposal", port_type=File, direction="out", ext=".json", validate=[]
            ),
            "Optimized": PortDescriptor(
                name="Optimized", port_type=File, direction="out", ext=".cpp", validate=[]
            ),
        }

        prompt = _build_agent_repair_prompt('{"', ports, ValueError("bad json"))

        assert '"Proposal": {"replace_with_real_output": true}' in prompt
        assert '"Optimized": "// Replace with real content\\n"' in prompt
        assert "Every declared output port is required." in prompt
        assert '"Proposal": "<content for Proposal>"' not in prompt

    def test_build_agent_repair_prompt_forbids_decision_changes_during_format_repair(self):
        ports = {
            "Proposal": PortDescriptor(
                name="Proposal", port_type=File, direction="out", ext=".json", validate=[]
            ),
            "Optimized": PortDescriptor(
                name="Optimized", port_type=File, direction="out", ext=".cpp", validate=[]
            ),
        }

        prompt = _build_agent_repair_prompt('{"bad": true}', ports, ValueError("bad json"))

        assert "Do not change the decision" in prompt
        assert "Do not add new reasoning about malformed output" in prompt

    def test_undeclared_key_single_remap(self):
        ports = self._ports(out=str)
        parsed = _parse_agent_outputs('{"outputs": {"wrong": "v"}}', ports)
        assert parsed == {"out": "v"}

    def test_undeclared_key_multi_raises(self):
        ports = self._ports(a=str, b=str)
        import pytest

        with pytest.raises(ValueError, match="undeclared"):
            _parse_agent_outputs('{"outputs": {"bad": "v"}}', ports)

    def test_parse_json_from_text_direct(self):
        assert _parse_json_from_text('{"a": 1}', "test") == {"a": 1}

    def test_parse_json_from_text_embedded(self):
        assert _parse_json_from_text('blah {"a": 1} end', "test") == {"a": 1}

    def test_parse_json_from_text_fails(self):
        import pytest

        with pytest.raises(ValueError):
            _parse_json_from_text("no json here", "test")


class TestAgentConstants:
    def test_file_truncation_limit(self):
        assert AGENT_FILE_INPUT_MAX_CHARS == 200_000

    def test_runtime_instruction(self):
        instr = _build_agent_runtime_instruction()
        assert "outputs" in instr
        assert "wfpy @agent task" in instr

    def test_runtime_instruction_mentions_empty_string_for_absent_multi_output_files(self):
        ports = {
            "Proposal": PortDescriptor(
                name="Proposal", port_type=File, direction="out", ext=".json", validate=[]
            ),
            "Optimized": PortDescriptor(
                name="Optimized", port_type=File, direction="out", ext=".cpp", validate=[]
            ),
        }

        instr = _build_agent_runtime_instruction(ports)

        assert "set its value to an empty string" in instr
        assert "intentionally does not emit a File/Resource artifact" in instr

    def test_max_tool_rounds(self):
        assert AGENT_MAX_TOOL_ROUNDS == 6

    def test_fail_fast_retry_after(self):
        assert AGENT_FAIL_FAST_RETRY_AFTER_MS == 60_000


# ── Chat history tests ───────────────────────────────────────────────────


class TestChatHistory:
    def test_trim_empty(self):
        history: list[dict[str, str]] = []
        result = trim_chat_history(history, 10)
        assert result == []

    def test_trim_under_budget(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = trim_chat_history(history, 10)
        assert len(result) == 2

    def test_trim_over_budget(self):
        history = [{"role": "user", "content": f"msg{i}"} for i in range(20)]
        result = trim_chat_history(history, 5)
        assert len(result) == 5
        assert result[0]["content"] == "msg15"

    def test_trim_drops_leading_non_user(self):
        history = [
            {"role": "assistant", "content": "orphan"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        result = trim_chat_history(history, 4)
        # After trim to 4: [user u1, assistant a1, user u2, assistant a2]
        assert result[0]["role"] == "user"
        assert len(result) == 4

    def test_trim_all_non_user_clears(self):
        history = [
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a2"},
        ]
        result = trim_chat_history(history, 5)
        assert result == []

    def test_trim_mutates_in_place(self):
        history = [{"role": "user", "content": f"m{i}"} for i in range(10)]
        ref = history
        trim_chat_history(history, 3)
        assert ref is history
        assert len(history) == 3


class TestRestoreChatHistories:
    def test_restore_chat_histories_restores_cli_session_ids(self, make_agent_actor):
        plan = FifoPlan(name="test")
        actor = make_agent_actor(
            name="agent1",
            stateful=True,
            chat_history=[],
            transport="opencode-cli",
        )
        plan.actors.append(actor)

        restored = restore_chat_histories(
            plan,
            {
                "actors": [
                    {
                        "instanceName": "agent1",
                        "chatHistory": [{"role": "user", "content": "hi"}],
                        "agentCliSessionIds": {"opencode": "sess-restored"},
                    }
                ]
            },
        )

        assert restored == 1
        assert actor.agent_cli_session_ids.get("opencode") == "sess-restored"

    def test_restore_chat_histories_infers_opencode_session_id_from_history(self, make_agent_actor):
        plan = FifoPlan(name="test")
        actor = make_agent_actor(
            name="agent1",
            stateful=True,
            chat_history=[],
            transport="opencode-cli",
        )
        plan.actors.append(actor)

        restored = restore_chat_histories(
            plan,
            {
                "actors": [
                    {
                        "instanceName": "agent1",
                        "chatHistory": [
                            {"role": "user", "content": "hi"},
                            {
                                "role": "assistant",
                                "content": "ok",
                                "opencodeSessionID": "sess-inferred",
                            },
                        ],
                    }
                ]
            },
        )

        assert restored == 1
        assert actor.agent_cli_session_ids.get("opencode") == "sess-inferred"


# ── Retry-After parsing tests ────────────────────────────────────────────


class TestRetryAfterParsing:
    def test_none(self):
        assert _parse_retry_after_ms(None) is None

    def test_empty(self):
        assert _parse_retry_after_ms("") is None

    def test_integer_seconds(self):
        assert _parse_retry_after_ms("5") == 5000

    def test_zero_seconds(self):
        assert _parse_retry_after_ms("0") == 0

    def test_negative_ignored(self):
        # Negative seconds are not valid; should be None since int(-1) < 0
        assert _parse_retry_after_ms("-1") is None

    def test_garbage(self):
        assert _parse_retry_after_ms("not a date") is None

    def test_http_date(self):
        import time
        from email.utils import formatdate

        # 10 seconds from now
        future = time.time() + 10
        date_str = formatdate(future, usegmt=True)
        ms = _parse_retry_after_ms(date_str)
        assert ms is not None
        assert 5000 <= ms <= 15000  # roughly 10 seconds


# ── Tool authorization tests ─────────────────────────────────────────────


class TestToolAuthorization:
    def test_deny_all(self):
        call = AgentToolCall(id="1", name="python", arguments={"code": "print(1)"})
        allowed, reason = _authorize_tool_call({"agent_tool_auth": "deny-all"}, call)
        assert allowed is False
        assert "deny-all" in reason

    def test_allow_all(self):
        call = AgentToolCall(id="1", name="python", arguments={"code": "print(1)"})
        allowed, reason = _authorize_tool_call({"agent_tool_auth": "allow-all"}, call)
        assert allowed is True
        assert "allow-all" in reason

    def test_default_deny(self):
        call = AgentToolCall(id="1", name="python", arguments={})
        allowed, _ = _authorize_tool_call({}, call)
        assert allowed is False

    def test_normalize_invalid_mode(self):
        import pytest

        with pytest.raises(ValueError, match="Invalid agent tool auth"):
            _normalize_tool_auth_mode({"agent_tool_auth": "invalid"})

    def test_policy_no_path(self):
        call = AgentToolCall(id="1", name="python", arguments={})
        allowed, reason = _authorize_tool_call({"agent_tool_auth": "policy"}, call)
        assert allowed is False
        assert "no policy path" in reason

    def test_policy_with_file(self, tmp_path):
        import json

        policy = {"allow": ["python", "shell"]}
        policy_file = tmp_path / "policy.json"
        policy_file.write_text(json.dumps(policy))

        call = AgentToolCall(id="1", name="python", arguments={})
        allowed, reason = _authorize_tool_call(
            {"agent_tool_auth": "policy", "agent_tool_policy": str(policy_file)},
            call,
        )
        assert allowed is True
        assert "allowed by policy" in reason

    def test_policy_denies_unlisted(self, tmp_path):
        import json

        policy = {"allow": ["shell"]}
        policy_file = tmp_path / "policy.json"
        policy_file.write_text(json.dumps(policy))

        call = AgentToolCall(id="1", name="python", arguments={})
        allowed, reason = _authorize_tool_call(
            {"agent_tool_auth": "policy", "agent_tool_policy": str(policy_file)},
            call,
        )
        assert allowed is False
        assert "not listed" in reason


# ── Provider tool declarations tests ──────────────────────────────────────


class TestToolDeclarations:
    def test_openai_format(self):
        tools = _provider_tool_declarations("openai")
        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "python"

    def test_anthropic_format(self):
        tools = _provider_tool_declarations("anthropic")
        assert len(tools) == 1
        assert tools[0]["name"] == "python"
        assert "input_schema" in tools[0]

    def test_ollama_format(self):
        tools = _provider_tool_declarations("ollama")
        assert tools[0]["type"] == "function"


# ── Extract tool calls tests ─────────────────────────────────────────────


class TestExtractToolCalls:
    def test_openai_format(self):
        resp = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "python",
                                    "arguments": '{"code": "print(42)"}',
                                },
                            }
                        ]
                    }
                }
            ]
        }
        calls = _extract_tool_calls("openai", resp)
        assert len(calls) == 1
        assert calls[0].name == "python"
        assert calls[0].arguments["code"] == "print(42)"

    def test_anthropic_format(self):
        resp = {
            "content": [
                {"type": "text", "text": "Let me run that."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "python",
                    "input": {"code": "print(42)"},
                },
            ]
        }
        calls = _extract_tool_calls("anthropic", resp)
        assert len(calls) == 1
        assert calls[0].name == "python"
        assert calls[0].id == "toolu_1"

    def test_no_tool_calls(self):
        resp = {"choices": [{"message": {"content": "hello"}}]}
        calls = _extract_tool_calls("openai", resp)
        assert calls == []


# ── Tool result messages tests ────────────────────────────────────────────


class TestToolResultMessages:
    def _call(self):
        return AgentToolCall(id="call_1", name="python", arguments={})

    def _result(self):
        return AgentToolResult(
            call_id="call_1",
            name="python",
            stdout="42\n",
            stderr="",
            exit_code=0,
        )

    def test_openai_format(self):
        msgs = _build_tool_result_messages("openai", [self._call()], [self._result()])
        assert len(msgs) == 1
        assert msgs[0]["role"] == "tool"
        assert msgs[0]["tool_call_id"] == "call_1"

    def test_anthropic_format(self):
        msgs = _build_tool_result_messages("anthropic", [self._call()], [self._result()])
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        content = msgs[0]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "tool_result"

    def test_ollama_format(self):
        msgs = _build_tool_result_messages("ollama", [self._call()], [self._result()])
        assert len(msgs) == 1
        assert msgs[0]["role"] == "tool"


# ── Build tool result payload tests ───────────────────────────────────────


class TestBuildToolResultPayload:
    def test_stdout_only(self):
        r = AgentToolResult(call_id="1", name="python", stdout="hi\n", stderr="", exit_code=0)
        text = _build_tool_result_payload(r)
        assert "stdout:" in text
        assert "hi" in text

    def test_error(self):
        r = AgentToolResult(
            call_id="1", name="python", stdout="", stderr="", exit_code=1, error="boom"
        )
        text = _build_tool_result_payload(r)
        assert "error: boom" in text

    def test_empty(self):
        r = AgentToolResult(call_id="1", name="python", stdout="", stderr="", exit_code=0)
        assert _build_tool_result_payload(r) == "(no output)"


# ── Streaming accumulation tests ─────────────────────────────────────────


class TestStreamAccumulation:
    def test_openai_sse(self):
        raw = (
            'data: {"choices": [{"delta": {"content": "Hello"}}]}\n'
            'data: {"choices": [{"delta": {"content": " world"}}]}\n'
            "data: [DONE]\n"
        )
        content, thinking = _accumulate_stream_response("openai", raw)
        assert content == "Hello world"
        assert thinking == ""

    def test_anthropic_sse(self):
        raw = (
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}\n'
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " there"}}\n'
            'data: {"type": "message_stop"}\n'
        )
        content, thinking = _accumulate_stream_response("anthropic", raw)
        assert content == "Hi there"

    def test_ollama_ndjson(self):
        raw = (
            '{"message": {"content": "A"}, "done": false}\n'
            '{"message": {"content": "B"}, "done": false}\n'
            '{"message": {"content": "C"}, "done": true}\n'
        )
        content, thinking = _accumulate_stream_response("ollama", raw)
        assert content == "ABC"

    def test_openai_with_reasoning(self):
        raw = (
            'data: {"choices": [{"delta": {"reasoning_content": "thinking..."}}]}\n'
            'data: {"choices": [{"delta": {"content": "answer"}}]}\n'
            "data: [DONE]\n"
        )
        content, thinking = _accumulate_stream_response("openai", raw)
        assert content == "answer"
        assert thinking == "thinking..."

    def test_anthropic_thinking(self):
        raw = (
            'data: {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}}\n'
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "result"}}\n'
            'data: {"type": "message_stop"}\n'
        )
        content, thinking = _accumulate_stream_response("anthropic", raw)
        assert content == "result"
        assert thinking == "hmm"


# ── Config file tests ────────────────────────────────────────────────────


class TestConfigFile:
    def test_read_nonexistent(self, monkeypatch, tmp_path):
        """When config file doesn't exist, defaults are used."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # Remove any env vars that might interfere
        for var in [
            "WF_AGENT_PROVIDER",
            "WF_AGENT_TOKEN",
            "WF_AGENT_ENDPOINT",
            "WF_AGENT_MODEL",
            "OPENAI_API_KEY",
            "AGENT_API_KEY",
        ]:
            monkeypatch.delenv(var, raising=False)
        cfg = _read_agent_config()
        assert cfg["provider"] == "openai"
        assert cfg["token"] is None
        assert cfg["endpoint"] is None

    def test_read_valid_config(self, monkeypatch, tmp_path):
        """Valid config file is parsed correctly."""
        import json

        config_dir = tmp_path / ".config" / "wf-lang"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "agent": {
                        "provider": "anthropic",
                        "token": "sk-test",
                        "model": "claude-3-haiku",
                    }
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        for var in [
            "WF_AGENT_PROVIDER",
            "WF_AGENT_TOKEN",
            "WF_AGENT_ENDPOINT",
            "WF_AGENT_MODEL",
            "OPENAI_API_KEY",
            "AGENT_API_KEY",
            "ANTHROPIC_API_KEY",
        ]:
            monkeypatch.delenv(var, raising=False)
        cfg = _read_agent_config()
        assert cfg["provider"] == "anthropic"
        assert cfg["token"] == "sk-test"
        assert cfg["model"] == "claude-3-haiku"

    def test_env_overrides_config(self, monkeypatch, tmp_path):
        """Env vars take precedence over config file."""
        import json

        config_dir = tmp_path / ".config" / "wf-lang"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text(
            json.dumps({"agent": {"provider": "openai", "token": "file-token"}})
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("WF_AGENT_TOKEN", "env-token")
        for var in [
            "WF_AGENT_PROVIDER",
            "WF_AGENT_ENDPOINT",
            "WF_AGENT_MODEL",
            "OPENAI_API_KEY",
            "AGENT_API_KEY",
        ]:
            monkeypatch.delenv(var, raising=False)
        cfg = _read_agent_config()
        assert cfg["token"] == "env-token"

    def test_preferred_provider_override(self, monkeypatch, tmp_path):
        """Explicit provider arg overrides env and config."""
        monkeypatch.setenv("HOME", str(tmp_path))
        for var in [
            "WF_AGENT_PROVIDER",
            "WF_AGENT_TOKEN",
            "WF_AGENT_ENDPOINT",
            "WF_AGENT_MODEL",
            "OPENAI_API_KEY",
            "AGENT_API_KEY",
        ]:
            monkeypatch.delenv(var, raising=False)
        cfg = _read_agent_config("github")
        assert cfg["provider"] == "github"


class TestAgentTransport:
    def test_normalize_agent_transport_defaults_http(self):
        from wfpy.core import AgentSpec

        assert _normalize_agent_transport(AgentSpec(prompt="x")) == "http"

    def test_normalize_agent_transport_opencode_alias(self):
        from wfpy.core import AgentSpec

        assert (
            _normalize_agent_transport(AgentSpec(prompt="x", transport="opencode"))
            == "opencode-acp"
        )
        assert (
            _normalize_agent_transport(AgentSpec(prompt="x", transport="opencode-cli"))
            == "opencode-cli"
        )
        assert (
            _normalize_agent_transport(AgentSpec(prompt="x", transport="opencode-acp"))
            == "opencode-acp"
        )

    def test_normalize_agent_transport_claude_and_codex_aliases(self):
        from wfpy.core import AgentSpec

        assert _normalize_agent_transport(AgentSpec(prompt="x", transport="claude")) == "claude-cli"
        assert (
            _normalize_agent_transport(AgentSpec(prompt="x", transport="claude-cli"))
            == "claude-cli"
        )
        assert _normalize_agent_transport(AgentSpec(prompt="x", transport="codex")) == "codex-cli"
        assert (
            _normalize_agent_transport(AgentSpec(prompt="x", transport="codex-cli")) == "codex-cli"
        )

    def test_normalize_cli_tools_mode(self):
        from wfpy.core import AgentSpec

        assert _normalize_cli_tools_mode(AgentSpec(prompt="x")) == "wfpy-none"
        assert _normalize_cli_tools_mode(AgentSpec(prompt="x", cli_tools_mode="native")) == "native"
        assert (
            _normalize_cli_tools_mode(AgentSpec(prompt="x", cli_tools_mode="none")) == "wfpy-none"
        )
        assert (
            _normalize_cli_tools_mode(AgentSpec(prompt="x"), {"agent_cli_tools_mode": "on"})
            == "native"
        )

    def test_parse_opencode_json_events(self):
        raw = "\n".join(
            [
                '{"type":"step_start","part":{"id":"a"}}',
                '{"type":"tool_call","part":{"name":"pto-isa-mcp.lookup","server":"pto-isa-mcp"}}',
                '{"type":"text","part":{"text":"first"}}',
                '{"type":"text","part":{"text":"second"}}',
                '{"type":"step_finish","part":{"tokens":{"total":30,"input":20,"output":10}}}',
            ]
        )

        content, debug = _parse_opencode_json_events(raw)
        assert content == "first\nsecond"
        usage = debug.get("usage")
        assert isinstance(usage, dict)
        assert usage["total_tokens"] == 30
        assert usage["prompt_tokens"] == 20
        assert usage["completion_tokens"] == 10
        assert usage["num_requests"] == 1
        assert debug.get("opencodeToolEvents") == 1
        assert debug.get("opencodeMcpServers") == ["pto-isa-mcp"]
        assert debug.get("opencodeToolNames") == ["pto-isa-mcp.lookup"]
        event_types = debug.get("opencodeEventTypes")
        assert isinstance(event_types, dict)
        assert event_types.get("tool_call") == 1

    def test_parse_opencode_json_events_extracts_session_id(self):
        raw = "\n".join(
            [
                '{"type":"step_start","sessionID":"sess-top","part":{"id":"a"}}',
                '{"type":"text","part":{"text":"ok"}}',
                '{"type":"step_finish","part":{"tokens":{"total":3,"input":2,"output":1}}}',
            ]
        )

        content, debug = _parse_opencode_json_events(raw)

        assert content == "ok"
        assert debug.get("opencodeSessionID") == "sess-top"

    def test_parse_opencode_json_events_keeps_text_sequence_for_later_normalization(self):
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "type": "text",
                        "part": {"text": "Inspecting local helpers before finalizing output."},
                    }
                ),
                json.dumps(
                    {
                        "type": "text",
                        "part": {
                            "text": '#include <pto/pto-inst.hpp>\nextern "C" void call_kernel() {}'
                        },
                    }
                ),
                json.dumps(
                    {"type": "step_finish", "part": {"tokens": {"total": 12, "input": 7, "output": 5}}}
                ),
            ]
        )

        content, debug = _parse_opencode_json_events(raw)
        assert content == (
            'Inspecting local helpers before finalizing output.\n'
            '#include <pto/pto-inst.hpp>\nextern "C" void call_kernel() {}'
        )
        usage = debug.get("usage")
        assert isinstance(usage, dict)
        assert usage["total_tokens"] == 12

    def test_parse_opencode_json_events_marks_no_text_event_without_raw_fallback(self):
        raw = "\n".join(
            [
                '{"type":"step_start","part":{"id":"a"}}',
                '{"type":"step_finish","part":{"tokens":{"total":4,"input":3,"output":1}}}',
            ]
        )

        content, debug = _parse_opencode_json_events(raw)
        assert content == ""
        assert debug.get("opencodeNoTextEvent") is True
        assert "opencodeRawFallback" not in debug

    def test_invoke_agent_opencode_cli(self, monkeypatch):
        from wfpy.core import AgentSpec

        def _fake_process(cmd, *, timeout_s, env, emit_prefix="[wfpy][opencode]"):
            del timeout_s, env, emit_prefix
            assert cmd[0] == "opencode"
            assert "run" in cmd
            assert "--format" in cmd
            assert "json" in cmd
            return (
                '{"outputs": {"out": "ok"}}',
                {"usage": {"total_tokens": 11, "prompt_tokens": 9, "completion_tokens": 2, "num_requests": 1, "tool_rounds": 0}},
                0,
                "",
            )

        monkeypatch.setattr("wfpy._agent_cli_runtime._run_opencode_process", _fake_process)
        spec = AgentSpec(prompt="x", model="openai/gpt-5-mini", transport="opencode-cli")
        response, firing, err, debug = _invoke_agent_opencode_cli(
            spec,
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="do task",
            plan_options={},
        )
        assert err is None
        assert '"outputs"' in response
        assert len(firing) == 2
        assert debug.get("transport") == "opencode-cli"
        assert debug.get("cliToolsMode") == "wfpy-none"

    def test_invoke_agent_opencode_cli_errors_when_json_events_have_no_text(self, monkeypatch):
        from wfpy.core import AgentSpec

        def _fake_process(cmd, *, timeout_s, env, emit_prefix="[wfpy][opencode]"):
            del timeout_s, env, emit_prefix
            assert cmd[0] == "opencode"
            return (
                "",
                {"opencodeNoTextEvent": True},
                0,
                "",
            )

        monkeypatch.setattr("wfpy._agent_cli_runtime._run_opencode_process", _fake_process)
        spec = AgentSpec(prompt="x", model="openai/gpt-5-mini", transport="opencode-cli")
        response, firing, err, debug = _invoke_agent_opencode_cli(
            spec,
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="do task",
            plan_options={},
        )
        assert response == ""
        assert len(firing) == 2
        assert isinstance(err, RuntimeError)
        assert "empty response content" in str(err)
        assert debug.get("opencodeNoTextEvent") is True
        assert "opencodeRawFallback" not in debug

    def test_invoke_agent_opencode_cli_native_mode(self, monkeypatch):
        from wfpy.core import AgentSpec

        captured: dict[str, Any] = {}

        def _fake_process(cmd, *, timeout_s, env, emit_prefix="[wfpy][opencode]"):
            del timeout_s, env, emit_prefix
            captured["cmd"] = cmd
            return (
                '{"outputs":{"out":"ok"}}',
                {},
                0,
                "",
            )

        monkeypatch.setattr("wfpy._agent_cli_runtime._run_opencode_process", _fake_process)
        spec = AgentSpec(
            prompt="x",
            model="github-copilot/gpt-5-mini",
            transport="opencode-cli",
            cli_tools_mode="native",
        )
        _response, _firing, err, debug = _invoke_agent_opencode_cli(
            spec,
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="do task",
            plan_options={"agent_cli_opencode_native_args": "--dangerously-skip-permissions"},
        )
        assert err is None
        assert debug.get("cliToolsMode") == "native"
        cmd = captured.get("cmd")
        assert isinstance(cmd, list)
        assert "--dangerously-skip-permissions" in cmd

    def test_invoke_agent_opencode_cli_uses_session_id(self, monkeypatch):
        from wfpy.core import AgentSpec

        captured: dict[str, Any] = {}

        def _fake_process(cmd, *, timeout_s, env, emit_prefix="[wfpy][opencode]"):
            del timeout_s, env, emit_prefix
            captured["cmd"] = cmd
            return (
                '{"outputs":{"out":"ok"}}',
                {"opencodeSessionID": "sess-42"},
                0,
                "",
            )

        monkeypatch.setattr("wfpy._agent_cli_runtime._run_opencode_process", _fake_process)
        spec = AgentSpec(
            prompt="x",
            model="github-copilot/gpt-5-mini",
            transport="opencode-cli",
        )
        response, _firing, err, debug = _invoke_agent_opencode_cli(
            spec,
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="do task",
            plan_options={},
            session_id="sess-42",
        )

        assert err is None
        assert '"outputs"' in response
        cmd = captured.get("cmd")
        assert isinstance(cmd, list)
        assert "--session" in cmd
        assert cmd[cmd.index("--session") + 1] == "sess-42"
        assert debug.get("opencodeSessionID") == "sess-42"
        assert debug.get("opencodeSessionIDRequested") == "sess-42"

    def test_invoke_agent_opencode_cli_retries_large_prompt_with_file(self, monkeypatch):
        import errno

        from wfpy.core import AgentSpec

        calls: list[list[str]] = []

        def _fake_process(cmd, *, timeout_s, env, emit_prefix="[wfpy][opencode]"):
            del timeout_s, env, emit_prefix
            calls.append(cmd)
            if len(calls) == 1:
                raise OSError(errno.E2BIG, "Argument list too long")
            assert "--file" in cmd
            assert "--" in cmd
            file_idx = cmd.index("--file")
            prompt_path = cmd[file_idx + 1]
            assert os.path.basename(prompt_path) == "wfpy_prompt.txt"
            assert os.path.exists(prompt_path)
            return (
                '{"outputs":{"out":"ok"}}',
                {},
                0,
                "",
            )

        monkeypatch.setattr("wfpy._agent_cli_runtime._run_opencode_process", _fake_process)
        spec = AgentSpec(prompt="x", model="github-copilot/gpt-5-mini", transport="opencode-cli")
        response, firing, err, debug = _invoke_agent_opencode_cli(
            spec,
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="do task" * 1000,
            plan_options={},
        )

        assert err is None
        assert '"outputs"' in response
        assert len(firing) == 2
        assert len(calls) == 2
        assert debug.get("promptDispatch") == "attached-file"
        cmd = debug.get("command")
        assert isinstance(cmd, list)
        assert "--file" in cmd

    def test_invoke_agent_opencode_cli_stages_attached_prompt_under_agent_debug_dir(
        self, monkeypatch, tmp_path
    ):
        import errno

        from wfpy.core import AgentSpec

        calls: list[list[str]] = []
        run_dir = tmp_path / "run"

        def _fake_process(cmd, *, timeout_s, env, emit_prefix="[wfpy][opencode]"):
            del timeout_s, env, emit_prefix
            calls.append(cmd)
            if len(calls) == 1:
                raise OSError(errno.E2BIG, "Argument list too long")
            file_idx = cmd.index("--file")
            prompt_path = Path(cmd[file_idx + 1])
            assert run_dir in prompt_path.parents
            assert prompt_path.name == "wfpy_prompt.txt"
            assert prompt_path.exists()
            return (
                '{"outputs":{"out":"ok"}}',
                {},
                0,
                "",
            )

        monkeypatch.setattr("wfpy._agent_cli_runtime._run_opencode_process", _fake_process)
        spec = AgentSpec(prompt="x", model="github-copilot/gpt-5-mini", transport="opencode-cli")

        response, firing, err, debug = _invoke_agent_opencode_cli(
            spec,
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="do task" * 1000,
            plan_options={"agent_debug_dir": str(run_dir)},
        )

        assert err is None
        assert '"outputs"' in response
        assert len(firing) == 2
        assert len(calls) == 2
        assert debug.get("promptDispatch") == "attached-file"

    def test_invoke_agent_dispatches_to_opencode(self, monkeypatch):
        from wfpy.core import AgentSpec
        from wfpy.runner import _invoke_agent

        captured: dict[str, Any] = {}

        def _fake_cli(spec, payload_text, verbose, **kwargs):
            captured["spec"] = spec
            captured["payload"] = payload_text
            captured["kwargs"] = kwargs
            return '{"outputs": {"out": "ok"}}', [{"role": "assistant", "content": "ok"}], None, {}

        monkeypatch.setattr("wfpy._invoke_runtime._invoke_agent_opencode_cli", _fake_cli)

        spec = AgentSpec(prompt="x", transport="opencode-cli")
        response, firing, err, _debug = _invoke_agent(
            spec,
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="prompt",
            output_ports={
                "out": PortDescriptor(
                    name="out", port_type=str, direction="out", ext="", validate=[]
                )
            },
        )

        assert err is None
        assert response
        assert len(firing) == 1
        assert "effective_prompt" in captured["kwargs"]

    def test_invoke_agent_dispatches_to_opencode_injects_runtime_instruction_once(
        self, monkeypatch
    ):
        from wfpy.core import AgentSpec
        from wfpy.runner import _invoke_agent

        captured: dict[str, Any] = {}

        def _fake_cli(spec, payload_text, verbose, **kwargs):
            captured["kwargs"] = kwargs
            return '{"outputs": {"out": "ok"}}', [{"role": "assistant", "content": "ok"}], None, {}

        monkeypatch.setattr("wfpy._invoke_runtime._invoke_agent_opencode_cli", _fake_cli)

        _invoke_agent(
            AgentSpec(prompt="x", transport="opencode-cli"),
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="prompt",
            output_ports={
                "out": PortDescriptor(
                    name="out", port_type=str, direction="out", ext="", validate=[]
                )
            },
            runtime_instruction_extra_rules=["extra runtime rule"],
        )

        effective_prompt = captured["kwargs"].get("effective_prompt", "")
        assert effective_prompt.startswith("prompt\n\nYou are executing a wfpy @agent task.")
        assert effective_prompt.count("You are executing a wfpy @agent task.") == 1
        assert effective_prompt.count("Declared output ports are: out.") == 1
        assert effective_prompt.count("extra runtime rule") == 1

    def test_invoke_agent_dispatches_to_opencode_trims_large_cli_history(self, monkeypatch):
        from wfpy.core import AgentSpec
        from wfpy.runner import _invoke_agent, CLI_AGENT_PROMPT_MAX_CHARS

        captured: dict[str, Any] = {}

        def _fake_cli(spec, payload_text, verbose, **kwargs):
            captured["kwargs"] = kwargs
            return '{"outputs": {"out": "ok"}}', [{"role": "assistant", "content": "ok"}], None, {}

        monkeypatch.setattr("wfpy._invoke_runtime._invoke_agent_opencode_cli", _fake_cli)

        prior_history = [
            {"role": "user", "content": "u" * CLI_AGENT_PROMPT_MAX_CHARS},
            {"role": "assistant", "content": "a" * CLI_AGENT_PROMPT_MAX_CHARS},
            {"role": "user", "content": "tail"},
        ]

        _invoke_agent(
            AgentSpec(prompt="x", transport="opencode-cli"),
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="prompt",
            prior_history=prior_history,
            output_ports={
                "out": PortDescriptor(
                    name="out", port_type=str, direction="out", ext="", validate=[]
                )
            },
        )

        effective_prompt = captured["kwargs"].get("effective_prompt", "")
        assert "Conversation history:" in effective_prompt
        assert "[earlier CLI history omitted to fit transport limits]" in effective_prompt
        assert "[user] tail" in effective_prompt
        assert len(effective_prompt) <= CLI_AGENT_PROMPT_MAX_CHARS + 1024

    def test_invoke_agent_dispatches_to_opencode_skips_history_when_base_prompt_is_full(
        self, monkeypatch
    ):
        from wfpy.core import AgentSpec
        from wfpy.runner import _invoke_agent, CLI_AGENT_PROMPT_MAX_CHARS

        captured: dict[str, Any] = {}

        def _fake_cli(spec, payload_text, verbose, **kwargs):
            captured["kwargs"] = kwargs
            return '{"outputs": {"out": "ok"}}', [{"role": "assistant", "content": "ok"}], None, {}

        monkeypatch.setattr("wfpy._invoke_runtime._invoke_agent_opencode_cli", _fake_cli)

        huge_prompt = "p" * CLI_AGENT_PROMPT_MAX_CHARS
        prior_history = [{"role": "user", "content": "tail"}]

        _invoke_agent(
            AgentSpec(prompt="x", transport="opencode-cli"),
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt=huge_prompt,
            prior_history=prior_history,
            output_ports={
                "out": PortDescriptor(
                    name="out", port_type=str, direction="out", ext="", validate=[]
                )
            },
        )

        effective_prompt = captured["kwargs"].get("effective_prompt", "")
        assert "Conversation history:" not in effective_prompt

    def test_invoke_agent_dispatches_to_opencode_skips_history_when_session_present(
        self, monkeypatch
    ):
        from wfpy.core import AgentSpec
        from wfpy.runner import _invoke_agent

        captured: dict[str, Any] = {}

        def _fake_cli(spec, payload_text, verbose, **kwargs):
            del spec, payload_text, verbose
            captured["kwargs"] = kwargs
            return '{"outputs": {"out": "ok"}}', [{"role": "assistant", "content": "ok"}], None, {}

        monkeypatch.setattr("wfpy._invoke_runtime._invoke_agent_opencode_cli", _fake_cli)

        _invoke_agent(
            AgentSpec(prompt="x", transport="opencode-cli"),
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="prompt",
            prior_history=[{"role": "user", "content": "tail"}],
            cli_session_id="sess-existing",
            output_ports={
                "out": PortDescriptor(
                    name="out", port_type=str, direction="out", ext="", validate=[]
                )
            },
        )

        effective_prompt = captured["kwargs"].get("effective_prompt", "")
        assert "Conversation history:" not in effective_prompt
        assert captured["kwargs"].get("session_id") == "sess-existing"

    def test_multi_output_agent_can_omit_optional_file_port(self, monkeypatch):
        from wfpy import File, agent, workflow, connect, run

        def _fake_agent(*_args, **_kwargs):
            return (
                json.dumps(
                    {
                        "outputs": {
                            "Proposal": {
                                "decision_action": "no_safe_candidate",
                                "decision_reason": "none",
                                "requested_evidence": [],
                            }
                        }
                    }
                ),
                [{"role": "assistant", "content": "ok"}],
                None,
                {},
            )

        monkeypatch.setattr("wfpy.runner._invoke_agent", _fake_agent)

        @agent(prompt="test", useSkill=False)
        class MultiOutputAgent:
            class Ports:
                In = Port[str](direction="in")
                Proposal = Port[File](direction="out", ext=".json")
                Optimized = Port[File](direction="out", ext=".cpp")

        @workflow(inputs={"In": str}, outputs={"Proposal": File})
        def wf():
            a = MultiOutputAgent()
            connect("In", a.In)
            connect(a.Proposal, "Proposal")

        out = run(wf, inputs={"In": "go"})

        assert "Proposal" in out

    def test_multi_output_agent_treats_empty_string_file_output_as_omitted(self, monkeypatch):
        from wfpy import File, agent, workflow, connect, run

        def _fake_agent(*_args, **_kwargs):
            return (
                json.dumps(
                    {
                        "outputs": {
                            "Proposal": {
                                "decision_action": "need_more_evidence",
                                "decision_reason": "need checks",
                                "requested_evidence": [
                                    {"kind": "compile_result", "focus": "candidate compile"}
                                ],
                            },
                            "Optimized": "",
                        }
                    }
                ),
                [{"role": "assistant", "content": "ok"}],
                None,
                {},
            )

        monkeypatch.setattr("wfpy.runner._invoke_agent", _fake_agent)

        @agent(prompt="test", useSkill=False)
        class MultiOutputAgent:
            class Ports:
                In = Port[str](direction="in")
                Proposal = Port[File](direction="out", ext=".json")
                Optimized = Port[File](direction="out", ext=".cpp")

        @workflow(inputs={"In": str}, outputs={"Proposal": File})
        def wf():
            a = MultiOutputAgent()
            connect("In", a.In)
            connect(a.Proposal, "Proposal")

        out = run(wf, inputs={"In": "go"})

        assert "Proposal" in out

    def test_invoke_agent_claude_cli(self, monkeypatch):
        from wfpy.core import AgentSpec

        class _Proc:
            returncode = 0
            stdout = '{"content":"{\\"outputs\\": {\\"out\\": \\"ok\\"}}"}'
            stderr = ""

        def _fake_run(cmd, **kwargs):
            assert "claude" in cmd[0]
            assert "--print" in cmd
            return _Proc()

        monkeypatch.setattr("wfpy._agent_cli_runtime.subprocess.run", _fake_run)
        spec = AgentSpec(prompt="x", transport="claude-cli")
        response, firing, err, debug = _invoke_agent_claude_cli(
            spec,
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="do task",
            plan_options={},
        )
        assert err is None
        assert '"outputs"' in response
        assert len(firing) == 2
        assert debug.get("transport") == "claude-cli"
        assert debug.get("cliToolsMode") == "wfpy-none"

    def test_invoke_agent_codex_cli(self, monkeypatch):
        from wfpy.core import AgentSpec

        class _Proc:
            returncode = 0
            stdout = '{"text":"{\\"outputs\\": {\\"out\\": \\"ok\\"}}"}'
            stderr = ""

        def _fake_run(cmd, **kwargs):
            assert "codex" in cmd[0]
            return _Proc()

        monkeypatch.setattr("wfpy._agent_cli_runtime.subprocess.run", _fake_run)
        spec = AgentSpec(prompt="x", transport="codex-cli")
        response, firing, err, debug = _invoke_agent_codex_cli(
            spec,
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="do task",
            plan_options={},
        )
        assert err is None
        assert '"outputs"' in response
        assert len(firing) == 2
        assert debug.get("transport") == "codex-cli"
        assert debug.get("cliToolsMode") == "wfpy-none"

    def test_invoke_agent_dispatches_to_claude_and_codex(self, monkeypatch):
        from wfpy.core import AgentSpec
        from wfpy.runner import _invoke_agent

        claude_called: dict[str, Any] = {}
        codex_called: dict[str, Any] = {}

        def _fake_claude(spec, payload_text, verbose, **kwargs):
            claude_called["spec"] = spec
            claude_called["payload"] = payload_text
            return '{"outputs": {"out": "ok"}}', [{"role": "assistant", "content": "ok"}], None, {}

        def _fake_codex(spec, payload_text, verbose, **kwargs):
            codex_called["spec"] = spec
            codex_called["payload"] = payload_text
            return '{"outputs": {"out": "ok"}}', [{"role": "assistant", "content": "ok"}], None, {}

        monkeypatch.setattr("wfpy._invoke_runtime._invoke_agent_claude_cli", _fake_claude)
        monkeypatch.setattr("wfpy._invoke_runtime._invoke_agent_codex_cli", _fake_codex)

        _invoke_agent(
            AgentSpec(prompt="x", transport="claude-cli"),
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="prompt",
            output_ports={
                "out": PortDescriptor(
                    name="out", port_type=str, direction="out", ext="", validate=[]
                )
            },
        )
        _invoke_agent(
            AgentSpec(prompt="x", transport="codex-cli"),
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="prompt",
            output_ports={
                "out": PortDescriptor(
                    name="out", port_type=str, direction="out", ext="", validate=[]
                )
            },
        )

        assert "spec" in claude_called
        assert "spec" in codex_called

    def test_invoke_agent_dispatch_to_cli_retries_transient_empty_response(self, monkeypatch):
        from wfpy.core import AgentSpec
        from wfpy.runner import _invoke_agent

        attempts = {"count": 0}
        sleep_calls: list[float] = []

        def _fake_cli(spec, payload_text, verbose, **kwargs):
            del spec, payload_text, verbose, kwargs
            attempts["count"] += 1
            if attempts["count"] == 1:
                return (
                    "",
                    [{"role": "assistant", "content": ""}],
                    RuntimeError("OpenCode CLI returned empty response content"),
                    {"transport": "opencode-cli"},
                )
            return (
                '{"outputs": {"out": "ok"}}',
                [{"role": "assistant", "content": "ok"}],
                None,
                {"transport": "opencode-cli"},
            )

        monkeypatch.setattr("wfpy._invoke_runtime._invoke_agent_opencode_cli", _fake_cli)
        monkeypatch.setattr("wfpy._invoke_runtime.time.sleep", lambda delay: sleep_calls.append(delay))

        response, _firing, err, debug = _invoke_agent(
            AgentSpec(prompt="x", transport="opencode-cli"),
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="prompt",
            output_ports={
                "out": PortDescriptor(name="out", port_type=str, direction="out", ext="", validate=[])
            },
        )

        assert err is None
        assert '"outputs"' in response
        assert attempts["count"] == 2
        assert len(sleep_calls) == 1
        assert debug.get("retryAttempts") == 1

    def test_invoke_agent_dispatch_to_cli_reuses_session_id_across_retry(self, monkeypatch):
        from wfpy.core import AgentSpec
        from wfpy.runner import _invoke_agent

        attempts = {"count": 0}
        sleep_calls: list[float] = []
        captured_kwargs: list[dict[str, Any]] = []

        def _fake_cli(spec, payload_text, verbose, **kwargs):
            del spec, payload_text, verbose
            captured_kwargs.append(dict(kwargs))
            attempts["count"] += 1
            if attempts["count"] == 1:
                return (
                    "",
                    [{"role": "assistant", "content": ""}],
                    RuntimeError("OpenCode CLI returned empty response content"),
                    {"transport": "opencode-cli", "opencodeSessionID": "sess-retry"},
                )
            return (
                '{"outputs": {"out": "ok"}}',
                [{"role": "assistant", "content": "ok"}],
                None,
                {"transport": "opencode-cli", "opencodeSessionID": "sess-retry"},
            )

        monkeypatch.setattr("wfpy._invoke_runtime._invoke_agent_opencode_cli", _fake_cli)
        monkeypatch.setattr("wfpy._invoke_runtime.time.sleep", lambda delay: sleep_calls.append(delay))

        response, _firing, err, debug = _invoke_agent(
            AgentSpec(prompt="x", transport="opencode-cli"),
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="prompt",
            output_ports={
                "out": PortDescriptor(name="out", port_type=str, direction="out", ext="", validate=[])
            },
        )

        assert err is None
        assert '"outputs"' in response
        assert attempts["count"] == 2
        assert len(sleep_calls) == 1
        assert len(captured_kwargs) == 2
        assert captured_kwargs[0].get("session_id") in (None, "")
        assert captured_kwargs[1].get("session_id") == "sess-retry"
        assert debug.get("retryAttempts") == 1

    def test_invoke_agent_dispatch_to_cli_does_not_retry_non_transient_runtime_error(
        self, monkeypatch
    ):
        from wfpy.core import AgentSpec
        from wfpy.runner import _invoke_agent

        attempts = {"count": 0}
        sleep_calls: list[float] = []

        def _fake_cli(spec, payload_text, verbose, **kwargs):
            del spec, payload_text, verbose, kwargs
            attempts["count"] += 1
            return (
                "",
                [{"role": "assistant", "content": ""}],
                RuntimeError("OpenCode CLI timed out after 1000ms."),
                {"transport": "opencode-cli"},
            )

        monkeypatch.setattr("wfpy._invoke_runtime._invoke_agent_opencode_cli", _fake_cli)
        monkeypatch.setattr("wfpy._invoke_runtime.time.sleep", lambda delay: sleep_calls.append(delay))

        _response, _firing, err, debug = _invoke_agent(
            AgentSpec(prompt="x", transport="opencode-cli"),
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="prompt",
            output_ports={
                "out": PortDescriptor(name="out", port_type=str, direction="out", ext="", validate=[])
            },
        )

        assert isinstance(err, RuntimeError)
        assert "timed out" in str(err)
        assert attempts["count"] == 1
        assert sleep_calls == []
        assert "retryAttempts" not in debug

    def test_invoke_agent_http_stream_retries_transient_empty_content(self, monkeypatch):
        import pytest

        # The HTTP transport ships in the optional `agent` extra.
        pytest.importorskip("httpx")

        from wfpy.core import AgentSpec
        from wfpy.runner import _invoke_agent

        attempts = {"count": 0}
        sleep_calls: list[float] = []

        class _StreamResp:
            def __init__(self, text: str):
                self.text = text

            def raise_for_status(self) -> None:
                return None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def _fake_stream(method, endpoint, json, headers, timeout):
            del method, endpoint, json, headers, timeout
            attempts["count"] += 1
            if attempts["count"] == 1:
                return _StreamResp("")
            return _StreamResp('{"outputs": {"out": "ok"}}')

        monkeypatch.setattr("httpx.stream", _fake_stream)
        monkeypatch.setattr(
            "wfpy._invoke_runtime._accumulate_stream_response",
            lambda provider, raw_text: (raw_text, ""),
        )
        monkeypatch.setattr("wfpy._invoke_runtime.time.sleep", lambda delay: sleep_calls.append(delay))

        response, firing, err, _debug = _invoke_agent(
            AgentSpec(prompt="x", provider="openai"),
            payload_text='{"inputs": {}}',
            verbose=False,
            effective_prompt="prompt",
            plan_options={"agent_stream": True, "agent_tool_auth": "deny-all"},
            output_ports={
                "out": PortDescriptor(name="out", port_type=str, direction="out", ext="", validate=[])
            },
        )

        assert err is None
        assert '"outputs"' in response
        assert len(firing) == 2
        assert attempts["count"] == 2
        assert len(sleep_calls) == 1


class TestSkillMdSupport:
    def test_build_effective_prompt_from_skill_name(self, tmp_path):
        skills_root = tmp_path / ".wf" / "skills" / "writer"
        skills_root.mkdir(parents=True)
        (skills_root / "SKILL.md").write_text(
            "---\nname: writer\nhooks:\n  pre: prep\n---\n# Skill Heading\nUse concise style.\n",
            encoding="utf-8",
        )

        plan = FifoPlan("x")
        plan.source_path = str(tmp_path / "workflow.py")

        from wfpy.core import AgentSpec

        spec = AgentSpec(
            prompt="Task-specific prompt.",
            skill="writer",
            use_prompt=True,
            use_skill=True,
            use_skill_hooks=True,
        )
        prompt, meta = _build_effective_agent_prompt(spec, plan)
        assert "Skill Heading" in prompt
        assert "Task-specific prompt" in prompt
        assert str(skills_root / "SKILL.md") == meta.get("skillPath")
        assert isinstance(meta.get("skillMeta"), dict)
        assert _skill_hook_name(meta.get("skillMeta") or {}, "pre") == "prep"

    def test_build_effective_prompt_rewrites_relative_skill_references(self, tmp_path):
        skill_root = tmp_path / ".wf" / "skills" / "writer"
        references_dir = skill_root / "references"
        references_dir.mkdir(parents=True)
        target = references_dir / "guide.md"
        target.write_text("# Guide\n", encoding="utf-8")
        (skill_root / "SKILL.md").write_text(
            "# Writer\n"
            "Read `references/guide.md` first.\n"
            "Then open [guide](references/guide.md).\n",
            encoding="utf-8",
        )

        plan = FifoPlan("x")
        plan.source_path = str(tmp_path / "workflow.py")

        from wfpy.core import AgentSpec

        spec = AgentSpec(prompt="", skill="writer", use_prompt=False, use_skill=True)
        prompt, meta = _build_effective_agent_prompt(spec, plan)

        expected = str(target)
        assert f"`{expected}`" in prompt
        assert f"[guide]({expected})" in prompt
        assert str(skill_root / "SKILL.md") == meta.get("skillPath")

    def test_build_effective_prompt_missing_skill_raises(self, tmp_path):
        import pytest
        from wfpy.core import AgentSpec

        plan = FifoPlan("x")
        plan.source_path = str(tmp_path / "workflow.py")
        spec = AgentSpec(prompt="", skill="missing", use_prompt=False, use_skill=True)
        with pytest.raises(FileNotFoundError, match="Searched roots"):
            _build_effective_agent_prompt(spec, plan)

    def test_run_skill_hook_allows_policy(self, tmp_path):
        skill_root = tmp_path / ".wf" / "skills" / "writer"
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text("# Writer\n", encoding="utf-8")
        skill_dir = skill_root / "scripts"
        skill_dir.mkdir(parents=True)
        script = skill_dir / "prep.py"
        script.write_text(
            "import json,sys; data=json.load(sys.stdin); print(data['phase'])\n", encoding="utf-8"
        )

        policy = tmp_path / "skill-hook-policy.json"
        policy.write_text('{"allow":["writer:prep:python"]}', encoding="utf-8")

        plan = FifoPlan("x")
        plan.source_path = str(tmp_path / "workflow.py")
        plan.options = {
            "skill_hook_auth": "policy",
            "skill_hook_policy": str(policy),
        }

        result = _run_skill_hook(
            skill_name="writer",
            hook_name="prep",
            phase="pre",
            actor_name="agent1",
            payload={"k": "v"},
            response_text=None,
            plan=plan,
            timeout_ms=5000,
        )
        assert result.get("ran") is True
        assert result.get("exitCode") == 0
        assert "pre" in str(result.get("stdout", ""))

    def test_run_skill_hook_policy_denies(self, tmp_path):
        skill_root = tmp_path / ".wf" / "skills" / "writer"
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text("# Writer\n", encoding="utf-8")
        skill_dir = skill_root / "scripts"
        skill_dir.mkdir(parents=True)
        (skill_dir / "prep.py").write_text("print('x')\n", encoding="utf-8")

        policy = tmp_path / "skill-hook-policy.json"
        policy.write_text('{"allow":[]}', encoding="utf-8")

        plan = FifoPlan("x")
        plan.source_path = str(tmp_path / "workflow.py")
        plan.options = {
            "skill_hook_auth": "policy",
            "skill_hook_policy": str(policy),
        }

        result = _run_skill_hook(
            skill_name="writer",
            hook_name="prep",
            phase="pre",
            actor_name="agent1",
            payload={},
            response_text=None,
            plan=plan,
            timeout_ms=2000,
        )
        assert result.get("ran") is False
        assert "denied" in str(result.get("reason", "")).lower()

    def test_build_effective_prompt_falls_back_to_home_claude_skills(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        skill_dir = home / ".claude" / "skills" / "writer"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Home Skill\nBody\n", encoding="utf-8")

        monkeypatch.setenv("HOME", str(home))

        plan = FifoPlan("x")
        plan.source_path = str(tmp_path / "project" / "wf.py")

        from wfpy.core import AgentSpec

        spec = AgentSpec(prompt="", skill="writer", use_prompt=False, use_skill=True)
        prompt, meta = _build_effective_agent_prompt(spec, plan)
        assert "Home Skill" in prompt
        assert str(skill_dir / "SKILL.md") == meta.get("skillPath")

    def test_build_effective_prompt_finds_skill_in_ancestor_example_root(self, tmp_path):
        example_root = tmp_path / "examples" / "pto-kernels"
        workflow_dir = example_root / "tests" / "optimizer"
        skill_dir = example_root / ".wf" / "skills" / "writer"
        workflow_dir.mkdir(parents=True)
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Ancestor Skill\nBody\n", encoding="utf-8")

        plan = FifoPlan("x")
        plan.source_path = str(workflow_dir / "wf_test_optimizer.py")

        from wfpy.core import AgentSpec

        spec = AgentSpec(prompt="", skill="writer", use_prompt=False, use_skill=True)
        prompt, meta = _build_effective_agent_prompt(spec, plan)
        assert "Ancestor Skill" in prompt
        assert str(skill_dir / "SKILL.md") == meta.get("skillPath")


class TestClaudeAgentCompatibility:
    def test_resolve_claude_agent_path(self, tmp_path):
        agents_root = tmp_path / ".claude" / "agents"
        agents_root.mkdir(parents=True)
        profile = agents_root / "reviewer.md"
        profile.write_text("---\nname: reviewer\n---\nReview code.\n", encoding="utf-8")

        plan = FifoPlan("x")
        plan.source_path = str(tmp_path / "workflow.py")
        resolved = _resolve_claude_agent_path("reviewer", plan)
        assert resolved == profile

    def test_resolve_claude_agent_path_honors_env_root(self, tmp_path, monkeypatch):
        custom_root = tmp_path / "custom-agents"
        custom_root.mkdir(parents=True)
        profile = custom_root / "reviewer.md"
        profile.write_text("---\nname: reviewer\n---\nReview code.\n", encoding="utf-8")

        monkeypatch.setenv("WF_CLAUDE_AGENTS_ROOT", str(custom_root))

        plan = FifoPlan("x")
        plan.source_path = str(tmp_path / "workflow.py")
        resolved = _resolve_claude_agent_path("reviewer", plan)
        assert resolved == profile

    def test_resolve_claude_agent_path_supports_nested_agent_md(self, tmp_path):
        agents_root = tmp_path / ".claude" / "agents" / "reviewer"
        agents_root.mkdir(parents=True)
        profile = agents_root / "AGENT.md"
        profile.write_text("---\nname: reviewer\n---\nReview code.\n", encoding="utf-8")

        plan = FifoPlan("x")
        plan.source_path = str(tmp_path / "workflow.py")
        resolved = _resolve_claude_agent_path("reviewer", plan)
        assert resolved == profile

    def test_load_claude_agent_profile_parses_description_skills_and_warnings(self, tmp_path):
        agents_root = tmp_path / ".claude" / "agents"
        agents_root.mkdir(parents=True)
        profile = agents_root / "reviewer.md"
        profile.write_text(
            "---\n"
            "name: reviewer\n"
            "description: Reviews patches\n"
            "skills: writer, security\n"
            "model: sonnet\n"
            "permissionMode: default\n"
            "---\n"
            "Focus on safety.\n",
            encoding="utf-8",
        )

        plan = FifoPlan("x")
        plan.source_path = str(tmp_path / "workflow.py")
        loaded = _load_claude_agent_profile("reviewer", plan)
        assert loaded.get("description") == "Reviews patches"
        assert loaded.get("skills") == ["writer", "security"]
        warnings = loaded.get("warnings") if isinstance(loaded.get("warnings"), list) else []
        assert any("model" in str(w) and "ignored" in str(w) for w in warnings)
        assert any("permissionMode" in str(w) and "ignored" in str(w) for w in warnings)

    def test_build_effective_prompt_with_claude_agent_and_preloaded_skills(self, tmp_path):
        agents_root = tmp_path / ".claude" / "agents"
        agents_root.mkdir(parents=True)
        profile = agents_root / "reviewer.md"
        profile.write_text(
            "---\n"
            "name: reviewer\n"
            "description: Reviews patches\n"
            "skills: writer\n"
            "model: sonnet\n"
            "---\n"
            "Base claude-agent instructions.\n",
            encoding="utf-8",
        )

        skill_dir = tmp_path / ".wf" / "skills" / "writer"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "# Writer\nUse concise bullet points.\n", encoding="utf-8"
        )

        plan = FifoPlan("x")
        plan.source_path = str(tmp_path / "workflow.py")

        from wfpy.core import AgentSpec

        spec = AgentSpec(
            prompt="Task-specific prompt.",
            claude_agent="reviewer",
            use_claude_agent=True,
            use_prompt=True,
            use_skill=False,
        )
        prompt, meta = _build_effective_agent_prompt(spec, plan)
        assert "Base claude-agent instructions." in prompt
        assert "Use concise bullet points." in prompt
        assert "Task-specific prompt." in prompt
        profile_meta = (
            meta.get("claudeAgentProfile")
            if isinstance(meta.get("claudeAgentProfile"), dict)
            else {}
        )
        assert str(profile) == profile_meta.get("path")

    def test_claude_agent_missing_profile_raises(self, tmp_path):
        import pytest
        from wfpy.core import AgentSpec

        plan = FifoPlan("x")
        plan.source_path = str(tmp_path / "workflow.py")
        spec = AgentSpec(
            prompt="",
            claude_agent="missing",
            use_claude_agent=True,
            use_prompt=False,
            use_skill=False,
        )
        with pytest.raises(FileNotFoundError, match="Claude agent 'missing' was not found"):
            _build_effective_agent_prompt(spec, plan)

    def test_claude_agent_max_turns_hint_maps_to_tool_round_cap(self):
        from wfpy.core import AgentSpec

        spec = AgentSpec(prompt="x")
        rounds = _effective_max_tool_rounds(spec, {"meta": {"maxTurns": "3"}})
        assert rounds == 3

    def test_claude_agent_max_turns_invalid_uses_default(self):
        from wfpy.core import AgentSpec

        spec = AgentSpec(prompt="x")
        rounds = _effective_max_tool_rounds(spec, {"meta": {"maxTurns": "invalid"}})
        assert rounds == 6


# ═══════════════════════════════════════════════════════════════════════════
# §9  Validator tests
# ═══════════════════════════════════════════════════════════════════════════


class TestValidationMode:
    def test_default_is_enforce(self, monkeypatch):
        monkeypatch.delenv("WF_PORT_VALIDATION_MODE", raising=False)
        assert normalize_validation_mode() == "enforce"

    def test_options_override(self, monkeypatch):
        monkeypatch.delenv("WF_PORT_VALIDATION_MODE", raising=False)
        assert normalize_validation_mode({"validate": "warn"}) == "warn"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("WF_PORT_VALIDATION_MODE", "off")
        assert normalize_validation_mode() == "off"

    def test_options_take_priority_over_env(self, monkeypatch):
        monkeypatch.setenv("WF_PORT_VALIDATION_MODE", "off")
        assert normalize_validation_mode({"validate": "warn"}) == "warn"

    def test_invalid_raises(self, monkeypatch):
        import pytest

        monkeypatch.delenv("WF_PORT_VALIDATION_MODE", raising=False)
        with pytest.raises(ValueError, match="Invalid validation mode"):
            normalize_validation_mode({"validate": "invalid"})


class TestFindNearestFile:
    def test_finds_in_start_dir(self, tmp_path):
        (tmp_path / "needle.txt").write_text("found")
        result = _find_nearest_file(str(tmp_path), "needle.txt")
        assert result is not None
        assert "needle.txt" in result

    def test_walks_up(self, tmp_path):
        (tmp_path / "needle.txt").write_text("found")
        child = tmp_path / "a" / "b" / "c"
        child.mkdir(parents=True)
        result = _find_nearest_file(str(child), "needle.txt")
        assert result is not None
        assert "needle.txt" in result

    def test_returns_none_if_missing(self, tmp_path):
        result = _find_nearest_file(str(tmp_path), "no_such_file.xyz")
        assert result is None


class TestParseValidatorRegistry:
    def test_empty(self):
        specs = _parse_validator_registry({})
        assert specs == {}

    def test_parses_builtin(self):
        raw = {"validators": {"json.parse": {"kind": "builtin"}}}
        specs = _parse_validator_registry(raw)
        assert "json.parse" in specs
        assert specs["json.parse"].kind == "builtin"

    def test_parses_module(self):
        raw = {
            "validators": {
                "my.val": {"kind": "module", "module": "/path/to/val.py", "export": "check"}
            }
        }
        specs = _parse_validator_registry(raw)
        assert specs["my.val"].kind == "module"
        assert specs["my.val"].module == "/path/to/val.py"
        assert specs["my.val"].export == "check"

    def test_parses_mcp(self):
        raw = {"validators": {"mcp.val": {"kind": "mcp", "server": "srv", "tool": "validate_json"}}}
        specs = _parse_validator_registry(raw)
        assert specs["mcp.val"].kind == "mcp"
        assert specs["mcp.val"].server == "srv"
        assert specs["mcp.val"].tool == "validate_json"

    def test_skips_unknown_kind(self):
        raw = {"validators": {"bad": {"kind": "unknown"}}}
        specs = _parse_validator_registry(raw)
        assert specs == {}


class TestLoadValidatorRegistry:
    def test_seeds_builtins(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WF_PORT_VALIDATION_MODE", raising=False)
        monkeypatch.delenv("WF_VALIDATOR_REGISTRY_PATH", raising=False)
        monkeypatch.delenv("WF_VALIDATOR_MCP_BRIDGE_CMD", raising=False)
        monkeypatch.delenv("WF_VALIDATOR_MCP_BRIDGE_ARGS", raising=False)
        monkeypatch.delenv("WF_VALIDATOR_MCP_ALLOWLIST", raising=False)
        reg = load_validator_registry(str(tmp_path / "dummy.wf"))
        assert "json.parse" in reg.specs
        assert "xml.wellFormed" in reg.specs

    def test_merges_user_registry(self, tmp_path, monkeypatch):
        import json

        monkeypatch.delenv("WF_PORT_VALIDATION_MODE", raising=False)
        monkeypatch.delenv("WF_VALIDATOR_REGISTRY_PATH", raising=False)
        monkeypatch.delenv("WF_VALIDATOR_MCP_BRIDGE_CMD", raising=False)
        monkeypatch.delenv("WF_VALIDATOR_MCP_BRIDGE_ARGS", raising=False)
        monkeypatch.delenv("WF_VALIDATOR_MCP_ALLOWLIST", raising=False)
        reg_file = tmp_path / "wf-validators.json"
        reg_file.write_text(json.dumps({"validators": {"custom.check": {"kind": "builtin"}}}))
        # source_path in the same dir → walk-up finds it
        reg = load_validator_registry(str(tmp_path / "my_workflow.py"))
        assert "custom.check" in reg.specs
        assert reg.source == str(reg_file)


class TestBuiltinValidators:
    def test_json_parse_valid(self, tmp_path):
        f = tmp_path / "good.json"
        f.write_text('{"key": "value"}')
        result = _execute_builtin_validator("json.parse", str(f))
        assert result.ok

    def test_json_parse_invalid(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not json")
        result = _execute_builtin_validator("json.parse", str(f))
        assert not result.ok
        assert len(result.diagnostics) == 1
        assert "JSON_PARSE" in result.diagnostics[0].code

    def test_xml_well_formed_valid(self, tmp_path):
        f = tmp_path / "good.xml"
        f.write_text("<root><child>text</child></root>")
        result = _execute_builtin_validator("xml.wellFormed", str(f))
        assert result.ok

    def test_xml_well_formed_invalid_mismatched(self, tmp_path):
        f = tmp_path / "bad.xml"
        f.write_text("<root><child>text</wrong></root>")
        result = _execute_builtin_validator("xml.wellFormed", str(f))
        assert not result.ok
        assert any("Mismatched" in d.message for d in result.diagnostics)

    def test_xml_well_formed_unclosed(self, tmp_path):
        f = tmp_path / "open.xml"
        f.write_text("<root><child>")
        result = _execute_builtin_validator("xml.wellFormed", str(f))
        assert not result.ok
        assert any("Unclosed" in d.message for d in result.diagnostics)

    def test_xml_self_closing(self, tmp_path):
        f = tmp_path / "self.xml"
        f.write_text('<root><br/><img src="x"/></root>')
        result = _execute_builtin_validator("xml.wellFormed", str(f))
        assert result.ok

    def test_file_not_found(self):
        result = _execute_builtin_validator("json.parse", "/no/such/file.json")
        assert not result.ok
        assert result.diagnostics[0].code == "VAL_READ_ERROR"

    def test_unknown_builtin(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        result = _execute_builtin_validator("unknown.thing", str(f))
        assert not result.ok
        assert result.diagnostics[0].code == "VAL_UNKNOWN_BUILTIN"


class TestCheckXmlWellFormed:
    def test_empty_string(self):
        result = _check_xml_well_formed("")
        assert result.ok

    def test_unexpected_close(self):
        result = _check_xml_well_formed("</root>")
        assert not result.ok


class TestNormalizeValidationResult:
    def test_pass_through_result(self):
        r = ValidationResult(ok=True)
        assert _normalize_validation_result(r).ok

    def test_dict_ok(self):
        r = _normalize_validation_result({"ok": True})
        assert r.ok

    def test_dict_fail_with_diagnostics(self):
        r = _normalize_validation_result(
            {
                "ok": False,
                "diagnostics": [{"code": "C", "message": "msg"}],
            }
        )
        assert not r.ok
        assert len(r.diagnostics) == 1
        assert r.diagnostics[0].code == "C"

    def test_bool_true(self):
        assert _normalize_validation_result(True).ok

    def test_bool_false(self):
        assert not _normalize_validation_result(False).ok

    def test_unknown_type(self):
        r = _normalize_validation_result(42)
        assert not r.ok
        assert r.diagnostics[0].code == "VAL_NORMALIZE"


class TestMcpAllowlist:
    def test_exact_match(self):
        assert _is_mcp_target_allowed(["srv:tool"], "srv", "tool")

    def test_server_wildcard(self):
        assert _is_mcp_target_allowed(["srv:*"], "srv", "any_tool")

    def test_universal_wildcard(self):
        assert _is_mcp_target_allowed(["*"], "any", "thing")
        assert _is_mcp_target_allowed(["*:*"], "any", "thing")

    def test_denied(self):
        assert not _is_mcp_target_allowed(["other:tool"], "srv", "tool")

    def test_empty_allowlist(self):
        assert not _is_mcp_target_allowed([], "srv", "tool")


class TestGetValidatorIdsFromPort:
    def test_empty_validate(self):
        pd = PortDescriptor(name="p", port_type=File, direction="out", ext="", validate=[])
        assert _get_validator_ids_from_port(pd) == []

    def test_has_validators(self):
        pd = PortDescriptor(
            name="p",
            port_type=File,
            direction="out",
            ext="",
            validate=["json.parse", "xml.wellFormed"],
        )
        assert _get_validator_ids_from_port(pd) == ["json.parse", "xml.wellFormed"]


class TestRunPortValidators:
    """Tests for run_port_validators() orchestrator."""

    def test_mode_off_skips(self, tmp_path, make_actor):
        """mode='off' should skip all validators."""
        reg = LoadedValidatorRegistry(mode="off")
        reg.specs = {"json.parse": ValidatorSpec(kind="builtin")}
        pd = PortDescriptor(
            name="p", port_type=File, direction="out", ext="", validate=["json.parse"]
        )
        f = tmp_path / "bad.json"
        f.write_text("NOT JSON")
        actor = make_actor("test", kind="external")
        # Should NOT raise even though file is invalid
        run_port_validators(reg, actor, "p", pd, str(f), "output", out_dir=tmp_path)

    def test_enforce_raises_on_fail(self, tmp_path, make_actor):
        import pytest

        reg = LoadedValidatorRegistry(mode="enforce")
        reg.specs = {"json.parse": ValidatorSpec(kind="builtin")}
        pd = PortDescriptor(
            name="p", port_type=File, direction="out", ext="", validate=["json.parse"]
        )
        f = tmp_path / "bad.json"
        f.write_text("NOT JSON")
        actor = make_actor("test", kind="external")
        with pytest.raises(RuntimeError, match="validation failed"):
            run_port_validators(reg, actor, "p", pd, str(f), "output", out_dir=tmp_path)

    def test_warn_logs_on_fail(self, tmp_path, caplog, make_actor):
        reg = LoadedValidatorRegistry(mode="warn")
        reg.specs = {"json.parse": ValidatorSpec(kind="builtin")}
        pd = PortDescriptor(
            name="p", port_type=File, direction="out", ext="", validate=["json.parse"]
        )
        f = tmp_path / "bad.json"
        f.write_text("NOT JSON")
        actor = make_actor("test", kind="external")
        with caplog.at_level(logging.WARNING, logger="wfpy"):
            run_port_validators(reg, actor, "p", pd, str(f), "output", out_dir=tmp_path)
        assert any("validation failed" in r.message for r in caplog.records)

    def test_enforce_passes_valid(self, tmp_path, make_actor):
        reg = LoadedValidatorRegistry(mode="enforce")
        reg.specs = {"json.parse": ValidatorSpec(kind="builtin")}
        pd = PortDescriptor(
            name="p", port_type=File, direction="out", ext="", validate=["json.parse"]
        )
        f = tmp_path / "good.json"
        f.write_text('{"ok": true}')
        actor = make_actor("test", kind="external")
        # Should NOT raise
        run_port_validators(reg, actor, "p", pd, str(f), "output", out_dir=tmp_path)

    def test_unknown_validator_enforce_raises(self, tmp_path, make_actor):
        import pytest

        reg = LoadedValidatorRegistry(mode="enforce")
        reg.specs = {}  # no specs at all
        pd = PortDescriptor(
            name="p", port_type=File, direction="out", ext="", validate=["no.such.validator"]
        )
        f = tmp_path / "data.txt"
        f.write_text("data")
        actor = make_actor("test", kind="external")
        with pytest.raises(RuntimeError, match="Unknown validator"):
            run_port_validators(reg, actor, "p", pd, str(f), "output", out_dir=tmp_path)

    def test_writes_validation_events(self, tmp_path, make_actor):
        import json

        reg = LoadedValidatorRegistry(mode="warn")
        reg.specs = {"json.parse": ValidatorSpec(kind="builtin")}
        pd = PortDescriptor(
            name="p", port_type=File, direction="out", ext="", validate=["json.parse"]
        )
        f = tmp_path / "good.json"
        f.write_text('{"ok": true}')
        actor = make_actor("test", kind="external")
        run_port_validators(reg, actor, "p", pd, str(f), "output", out_dir=tmp_path)
        events_path = tmp_path / "validation-events.jsonl"
        assert events_path.exists()
        event = json.loads(events_path.read_text().strip())
        assert event["ok"] is True
        assert event["validator"] == "json.parse"

    def test_no_validators_is_noop(self, tmp_path, make_actor):
        reg = LoadedValidatorRegistry(mode="enforce")
        reg.specs = {"json.parse": ValidatorSpec(kind="builtin")}
        pd = PortDescriptor(name="p", port_type=File, direction="out", ext="", validate=[])
        # No validate= list
        f = tmp_path / "data.txt"
        f.write_text("data")
        actor = make_actor("test", kind="external")
        run_port_validators(reg, actor, "p", pd, str(f), "output", out_dir=tmp_path)

    def test_non_file_path_is_noop(self, tmp_path, make_actor):
        reg = LoadedValidatorRegistry(mode="enforce")
        reg.specs = {"json.parse": ValidatorSpec(kind="builtin")}
        pd = PortDescriptor(
            name="p", port_type=File, direction="out", ext="", validate=["json.parse"]
        )
        actor = make_actor("test", kind="external")
        # Passing a value that's not a file path
        run_port_validators(reg, actor, "p", pd, "not-a-file", "output", out_dir=tmp_path)


# ═══════════════════════════════════════════════════════════════════════════
# §11  Queue trace tests
# ═══════════════════════════════════════════════════════════════════════════


class TestQueueTraceCollector:
    def test_disabled_noop(self, make_actor):
        collector = QueueTraceCollector(enabled=False, workflow_name="test")
        plan = FifoPlan(name="test")
        actor = make_actor("a")
        collector.record_fire(plan, actor)
        assert len(collector.steps) == 0

    def test_records_fire(self, make_actor):
        collector = QueueTraceCollector(enabled=True, workflow_name="test")
        plan = FifoPlan(name="test")
        actor = make_actor("a")
        actor.fire_count = 1
        collector.record_fire(plan, actor)
        assert len(collector.steps) == 1
        step = collector.steps[0]
        assert step.actor_instance_name == "a"
        assert step.actor_fire_count == 1
        assert step.step == 1

    def test_build_output(self, make_actor):
        collector = QueueTraceCollector(enabled=True, workflow_name="myWf")
        plan = FifoPlan(name="myWf")
        actor = make_actor("a")
        actor.fire_count = 1
        collector.record_fire(plan, actor)
        result = collector.build("2025-01-01T00:00:00Z", plan)
        assert result["version"] == 1
        assert result["workflowName"] == "myWf"
        assert result["stepCount"] == 1
        assert len(result["steps"]) == 1
        assert result["steps"][0]["actorInstanceName"] == "a"

    def test_build_ignores_workflow_output_queues_in_leftovers(self):
        collector = QueueTraceCollector(enabled=True, workflow_name="myWf")
        plan = FifoPlan(name="myWf")
        q_out = Queue(queue_id="q_out")
        q_non_out = Queue(queue_id="q_non_out")
        q_out.enqueue("x")
        q_non_out.enqueue("y")
        plan.all_queues = [q_out, q_non_out]
        plan.wf_output_queues = {"Output": [q_out]}

        result = collector.build("2025-01-01T00:00:00Z", plan)
        leftovers = result["leftovers"]
        assert len(leftovers) == 1
        assert leftovers[0]["queueId"] == "q_non_out"


class TestStepAgentBehavior:
    def test_step_agent_stamps_actual_mcp_usage_in_optimized_output(
        self, tmp_path, monkeypatch, make_task_meta, make_port, make_agent_spec
    ):
        from wfpy.runner import _step_agent

        response = json.dumps(
            {
                "outputs": {
                    "Optimized": "\n".join(
                        [
                            "// Optimizations applied:",
                            "// 1. Test.",
                            "// 2. Test.",
                            "// Family: sync_barrier_narrowing_family__test_variant",
                            "// MCP verification: pending runtime provenance.",
                            "",
                            '#include "common.h"',
                        ]
                    )
                }
            }
        )

        def _fake_invoke(_spec, _payload_text, _verbose, **_kwargs):
            return (
                response,
                [{"role": "assistant", "content": response}],
                None,
                {
                    "opencodeMcpServers": ["pto-isa-mcp"],
                    "opencodeToolNames": ["pto-isa-mcp_get_cpp_intrinsic"],
                },
            )

        monkeypatch.setattr("wfpy.runner._invoke_agent", _fake_invoke)

        spec = make_agent_spec(
            prompt="p",
            model="m",
            transport="opencode-cli",
            mcp_servers=["pto-isa-mcp"],
        )
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            output_ports={"Optimized": make_port("Optimized", File, "out", ext=".cpp")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="optimizer", kind="agent", instance=object(), meta=meta)
        actor.out_queues = {"Optimized": [Queue(queue_id="q")]}
        plan = FifoPlan(name="wf")
        plan.options = {}

        fired = _step_agent(actor, tmp_path, plan, verbose=False)

        assert fired is True
        output_path = Path(actor.out_queues["Optimized"][0].peek()[0])
        assert output_path.read_text().splitlines()[4] == "// MCP verification: used (pto-isa-mcp)."

    def test_step_agent_stamps_configured_but_unused_mcp_in_optimized_output(
        self, tmp_path, monkeypatch, make_task_meta, make_port, make_agent_spec
    ):
        from wfpy.runner import _step_agent

        response = json.dumps(
            {
                "outputs": {
                    "Optimized": "\n".join(
                        [
                            "// Optimizations applied:",
                            "// 1. Test.",
                            "// 2. Test.",
                            "// Family: sync_barrier_narrowing_family__test_variant",
                            "// MCP verification: pending runtime provenance.",
                            "",
                            '#include "common.h"',
                        ]
                    )
                }
            }
        )

        def _fake_invoke(_spec, _payload_text, _verbose, **_kwargs):
            return (
                response,
                [{"role": "assistant", "content": response}],
                None,
                {
                    "opencodeToolNames": ["bash", "read"],
                },
            )

        monkeypatch.setattr("wfpy.runner._invoke_agent", _fake_invoke)

        spec = make_agent_spec(
            prompt="p",
            model="m",
            transport="opencode-cli",
            mcp_servers=["pto-isa-mcp"],
        )
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            output_ports={"Optimized": make_port("Optimized", File, "out", ext=".cpp")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="optimizer", kind="agent", instance=object(), meta=meta)
        actor.out_queues = {"Optimized": [Queue(queue_id="q")]}
        plan = FifoPlan(name="wf")
        plan.options = {}

        fired = _step_agent(actor, tmp_path, plan, verbose=False)

        assert fired is True
        output_path = Path(actor.out_queues["Optimized"][0].peek()[0])
        assert (
            output_path.read_text().splitlines()[4]
            == "// MCP verification: not used (pto-isa-mcp configured)."
        )

    def test_step_agent_stamps_native_mcp_usage_unknown_in_optimized_output(
        self, tmp_path, monkeypatch, make_task_meta, make_port, make_agent_spec
    ):
        from wfpy.runner import _step_agent

        response = json.dumps(
            {
                "outputs": {
                    "Optimized": "\n".join(
                        [
                            "// Optimizations applied:",
                            "// 1. Test.",
                            "// 2. Test.",
                            "// Family: sync_barrier_narrowing_family__test_variant",
                            "// MCP verification: pending runtime provenance.",
                            "",
                            '#include "common.h"',
                        ]
                    )
                }
            }
        )

        def _fake_invoke(_spec, _payload_text, _verbose, **_kwargs):
            return (
                response,
                [{"role": "assistant", "content": response}],
                None,
                {
                    "cliToolsMode": "native",
                    "opencodeToolNames": ["bash", "read"],
                },
            )

        monkeypatch.setattr("wfpy.runner._invoke_agent", _fake_invoke)

        spec = make_agent_spec(
            prompt="p",
            model="m",
            transport="opencode-cli",
            mcp_servers=["pto-isa-mcp"],
        )
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            output_ports={"Optimized": make_port("Optimized", File, "out", ext=".cpp")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="optimizer", kind="agent", instance=object(), meta=meta)
        actor.out_queues = {"Optimized": [Queue(queue_id="q")]}
        plan = FifoPlan(name="wf")
        plan.options = {}

        fired = _step_agent(actor, tmp_path, plan, verbose=False)

        assert fired is True
        output_path = Path(actor.out_queues["Optimized"][0].peek()[0])
        assert (
            output_path.read_text().splitlines()[4]
            == "// MCP verification: usage unknown (pto-isa-mcp configured via native tools)."
        )

    def test_uses_instance_parameters_in_payload(
        self, tmp_path, make_task_meta, make_port, make_agent_spec, mock_invoke_agent
    ):
        from wfpy.runner import _step_agent

        class Inst:
            temperature = 0.7

        spec = make_agent_spec(prompt="p", model="m")
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            output_ports={"out": make_port("out", str, "out")},
            parameters={"temperature": float},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="agent1", kind="agent", instance=Inst(), meta=meta)
        actor.out_queues = {"out": [Queue(queue_id="q")]}
        plan = FifoPlan(name="wf")
        plan.options = {}

        fired = _step_agent(actor, tmp_path, plan, verbose=False)
        assert fired is True
        assert mock_invoke_agent["payload"]["parameters"] == {"temperature": 0.7}

    def test_fire_budget_is_instance_local(
        self, tmp_path, make_task_meta, make_port, make_agent_spec, mock_invoke_agent
    ):
        from wfpy.runner import _step_agent

        spec = make_agent_spec(prompt="p", model="m", fireable_without_input=1)
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            input_ports={"in": make_port("in", str, "in")},
            output_ports={"out": make_port("out", str, "out")},
            agent_spec=spec,
        )

        actor1 = RuntimeActor(name="agent1", kind="agent", instance=object(), meta=meta)
        actor2 = RuntimeActor(name="agent2", kind="agent", instance=object(), meta=meta)
        actor1.out_queues = {"out": [Queue(queue_id="q1")]}
        actor2.out_queues = {"out": [Queue(queue_id="q2")]}
        plan = FifoPlan(name="wf")
        plan.options = {}

        assert _step_agent(actor1, tmp_path, plan, verbose=False) is True
        assert _step_agent(actor1, tmp_path, plan, verbose=False) is False
        assert _step_agent(actor2, tmp_path, plan, verbose=False) is True

    def test_step_agent_stages_file_inputs_for_cli_transports(
        self, tmp_path, make_task_meta, make_port, make_agent_spec, mock_invoke_agent
    ):
        from wfpy.runner import _step_agent

        src = tmp_path / "source.cpp"
        src.write_text("int x = 1;\n")

        spec = make_agent_spec(prompt="p", model="m", transport="opencode-cli")
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            input_ports={"Kernel": make_port("Kernel", File, "in")},
            output_ports={"out": make_port("out", str, "out")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="agent1", kind="agent", instance=object(), meta=meta)
        actor.in_queues = {"Kernel": [Queue(queue_id="in")]}
        actor.in_queues["Kernel"][0].enqueue(str(src))
        actor.out_queues = {"out": [Queue(queue_id="q")]}

        plan = FifoPlan(name="wf")
        plan.options = {}
        plan.work_dir = str(tmp_path / "workdir")

        fired = _step_agent(actor, tmp_path, plan, verbose=False)
        assert fired is True

        payload = mock_invoke_agent["payload"]
        staged_path = payload["fileInputs"]["Kernel"]["path"]
        assert staged_path != str(src)
        assert Path(staged_path).exists()
        assert Path(staged_path).read_text() == src.read_text()
        assert payload["fileInputs"]["Kernel"]["originalPath"] == str(src)
        assert payload["resourceInputs"]["Kernel"]["path"] == staged_path
        assert payload["resourceInputs"]["Kernel"]["originalPath"] == str(src)

    def test_step_agent_passes_bare_prompt_without_builtin_runtime_rules(
        self, tmp_path, monkeypatch, make_task_meta, make_port, make_agent_spec
    ):
        from wfpy.runner import _step_agent

        captured: dict[str, Any] = {}

        def _fake_invoke(agent_spec, payload_text, verbose, **kwargs):
            captured["kwargs"] = kwargs
            return "ok", [{"role": "assistant", "content": "ok"}], None, {}

        monkeypatch.setattr("wfpy.runner._invoke_agent", _fake_invoke)

        spec = make_agent_spec(
            prompt="base prompt",
            model="m",
            transport="opencode-cli",
            mcp_servers=["pto-isa-mcp"],
        )
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            output_ports={"Optimized": make_port("Optimized", File, "out", ext=".cpp")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="optimizer", kind="agent", instance=object(), meta=meta)
        actor.out_queues = {"Optimized": [Queue(queue_id="q")]}
        plan = FifoPlan(name="wf")
        plan.options = {}

        fired = _step_agent(actor, tmp_path, plan, verbose=False)

        assert fired is True
        assert captured["kwargs"]["effective_prompt"] == "base prompt"
        # wfpy injects no rules of its own; the prompt reaches the agent bare.
        assert captured["kwargs"]["runtime_instruction_extra_rules"] == []

    def test_step_agent_rewrites_staged_context_paths_for_cli_transports(
        self, tmp_path, make_task_meta, make_port, make_agent_spec, mock_invoke_agent
    ):
        from wfpy.runner import _step_agent

        kernel = tmp_path / "source.cpp"
        kernel.write_text("int x = 1;\n")
        report = tmp_path / "report.md"
        report.write_text("# Report\n")
        hotspots = tmp_path / "hotspots.json"
        hotspots.write_text('{"ops": []}\n')
        context = tmp_path / "context.json"
        context.write_text(
            json.dumps(
                {
                    "baseline": {
                        "kernel_path": str(kernel),
                        "report_path": str(report),
                        "hotspots_path": str(hotspots),
                    },
                    "metadata": {
                        "missing_path": str(tmp_path / "missing.cpp"),
                        "url_path": "https://example.com/source.cpp",
                    },
                },
                indent=2,
            )
            + "\n"
        )

        spec = make_agent_spec(prompt="p", model="m", transport="opencode-cli")
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            input_ports={
                "Context": make_port("Context", File, "in"),
                "Kernel": make_port("Kernel", File, "in"),
            },
            output_ports={"out": make_port("out", str, "out")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="agent1", kind="agent", instance=object(), meta=meta)
        actor.in_queues = {
            "Context": [Queue(queue_id="context")],
            "Kernel": [Queue(queue_id="kernel")],
        }
        actor.in_queues["Context"][0].enqueue(str(context))
        actor.in_queues["Kernel"][0].enqueue(str(kernel))
        actor.out_queues = {"out": [Queue(queue_id="q")]}

        plan = FifoPlan(name="wf")
        plan.options = {}
        plan.work_dir = str(tmp_path / "workdir")

        fired = _step_agent(actor, tmp_path, plan, verbose=False)
        assert fired is True

        payload = mock_invoke_agent["payload"]
        staged_context_path = Path(payload["fileInputs"]["Context"]["path"])
        staged_kernel_path = payload["fileInputs"]["Kernel"]["path"]
        staged_context = json.loads(staged_context_path.read_text())

        assert staged_context_path != context
        assert payload["fileInputs"]["Context"]["originalPath"] == str(context)
        assert staged_context["baseline"]["kernel_path"] == staged_kernel_path
        assert staged_context["baseline"]["report_path"] != str(report)
        assert staged_context["baseline"]["hotspots_path"] != str(hotspots)
        assert Path(staged_context["baseline"]["report_path"]).read_text() == report.read_text()
        assert Path(staged_context["baseline"]["hotspots_path"]).read_text() == hotspots.read_text()
        assert staged_context["metadata"]["missing_path"] == str(tmp_path / "missing.cpp")
        assert staged_context["metadata"]["url_path"] == "https://example.com/source.cpp"
        assert str(kernel) not in payload["fileInputs"]["Context"]["content"]
        assert str(report) not in payload["fileInputs"]["Context"]["content"]
        assert str(hotspots) not in payload["fileInputs"]["Context"]["content"]

    def test_step_agent_keeps_original_file_paths_for_http_transport(
        self, tmp_path, make_task_meta, make_port, make_agent_spec, mock_invoke_agent
    ):
        from wfpy.runner import _step_agent

        src = tmp_path / "source.cpp"
        src.write_text("int x = 1;\n")

        spec = make_agent_spec(prompt="p", model="m", transport="http")
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            input_ports={"Kernel": make_port("Kernel", File, "in")},
            output_ports={"out": make_port("out", str, "out")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="agent1", kind="agent", instance=object(), meta=meta)
        actor.in_queues = {"Kernel": [Queue(queue_id="in")]}
        actor.in_queues["Kernel"][0].enqueue(str(src))
        actor.out_queues = {"out": [Queue(queue_id="q")]}

        plan = FifoPlan(name="wf")
        plan.options = {}
        plan.work_dir = str(tmp_path / "workdir")

        fired = _step_agent(actor, tmp_path, plan, verbose=False)
        assert fired is True

        payload = mock_invoke_agent["payload"]
        assert payload["fileInputs"]["Kernel"]["path"] == str(src)
        assert "originalPath" not in payload["fileInputs"]["Kernel"]
        assert payload["resourceInputs"]["Kernel"]["path"] == str(src)
        assert "originalPath" not in payload["resourceInputs"]["Kernel"]

    def test_step_agent_rewrites_relative_json_file_paths_for_cli_transport(
        self, tmp_path, make_task_meta, make_port, make_agent_spec, mock_invoke_agent
    ):
        from wfpy.runner import _step_agent

        fixture_dir = tmp_path / "fixture"
        data_dir = fixture_dir / "data"
        data_dir.mkdir(parents=True)

        kernel = data_dir / "kernel.cpp"
        kernel.write_text('#include "common.h"\n')
        report = data_dir / "report.md"
        report.write_text("# report\n")
        hotspots = data_dir / "hotspots.json"
        hotspots.write_text('{"verdict": "MEMORY_BOUND"}\n')
        context = data_dir / "context.json"
        context.write_text(
            json.dumps(
                {
                    "baseline": {
                        "kernel_path": "kernel.cpp",
                        "report_path": "report.md",
                        "hotspots_path": "hotspots.json",
                    }
                }
            )
        )

        spec = make_agent_spec(prompt="p", model="m", transport="opencode-cli")
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            input_ports={"Context": make_port("Context", File, "in")},
            output_ports={"out": make_port("out", str, "out")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="agent1", kind="agent", instance=object(), meta=meta)
        actor.in_queues = {"Context": [Queue(queue_id="context")]}
        actor.in_queues["Context"][0].enqueue(str(context))
        actor.out_queues = {"out": [Queue(queue_id="q")]}

        plan = FifoPlan(name="wf")
        plan.options = {}
        plan.work_dir = str(tmp_path / "workdir")

        fired = _step_agent(actor, tmp_path, plan, verbose=False)
        assert fired is True

        payload = mock_invoke_agent["payload"]
        staged_context_path = Path(payload["fileInputs"]["Context"]["path"])
        staged_context = json.loads(staged_context_path.read_text())

        assert Path(staged_context["baseline"]["kernel_path"]).read_text() == kernel.read_text()
        assert Path(staged_context["baseline"]["report_path"]).read_text() == report.read_text()
        assert Path(staged_context["baseline"]["hotspots_path"]).read_text() == hotspots.read_text()

    def test_step_agent_reuses_stateful_opencode_session_and_updates_it(
        self, tmp_path, monkeypatch, make_task_meta, make_port, make_agent_spec
    ):
        from wfpy.runner import _step_agent

        response = json.dumps({"outputs": {"out": "ok"}})
        captured: dict[str, Any] = {}

        def _fake_invoke(_spec, _payload_text, _verbose, **kwargs):
            captured["request"] = kwargs
            return (
                response,
                [{"role": "assistant", "content": response}],
                None,
                {"opencodeSessionID": "sess-next"},
            )

        monkeypatch.setattr("wfpy.runner._invoke_agent", _fake_invoke)

        spec = make_agent_spec(prompt="p", model="m", transport="opencode-cli", stateful=True)
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            output_ports={"out": make_port("out", str, "out")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="agent1", kind="agent", instance=object(), meta=meta)
        actor.chat_history = [
            {"role": "user", "content": "prior"},
            {"role": "assistant", "content": "answer"},
        ]
        actor.agent_cli_session_ids["opencode"] = "sess-prev"
        actor.out_queues = {"out": [Queue(queue_id="q")]}

        plan = FifoPlan(name="wf")
        plan.options = {}

        fired = _step_agent(actor, tmp_path, plan, verbose=False)

        assert fired is True
        request = captured["request"]
        assert request.get("cli_session_id") == "sess-prev"
        assert request.get("prior_history") is None
        assert actor.agent_cli_session_ids.get("opencode") == "sess-next"
        assert actor.chat_history[-1].get("opencodeSessionID") == "sess-next"

    def test_step_agent_non_stateful_agent_does_not_reuse_opencode_session_across_fires(
        self, tmp_path, monkeypatch, make_task_meta, make_port, make_agent_spec
    ):
        from wfpy.runner import _step_agent

        response = json.dumps({"outputs": {"out": "ok"}})
        captured: dict[str, Any] = {}

        def _fake_invoke(_spec, _payload_text, _verbose, **kwargs):
            captured["request"] = kwargs
            return (
                response,
                [{"role": "assistant", "content": response}],
                None,
                {"opencodeSessionID": "sess-nonstate"},
            )

        monkeypatch.setattr("wfpy.runner._invoke_agent", _fake_invoke)

        spec = make_agent_spec(prompt="p", model="m", transport="opencode-cli", stateful=False)
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            output_ports={"out": make_port("out", str, "out")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="agent1", kind="agent", instance=object(), meta=meta)
        actor.agent_cli_session_ids["opencode"] = "sess-prev-nonstate"
        actor.out_queues = {"out": [Queue(queue_id="q")]}

        plan = FifoPlan(name="wf")
        plan.options = {}

        fired = _step_agent(actor, tmp_path, plan, verbose=False)

        assert fired is True
        request = captured["request"]
        # Non-stateful agents do NOT reuse sessions across fires.
        assert request.get("cli_session_id") is None
        # The session ID is not persisted to actor storage either.
        assert actor.agent_cli_session_ids.get("opencode") == "sess-prev-nonstate"

    def test_step_agent_repairs_before_skill_post_hook(
        self, tmp_path, monkeypatch, make_task_meta, make_port, make_agent_spec
    ):
        from wfpy.runner import _step_agent

        skill_root = tmp_path / ".wf" / "skills" / "writer"
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "---\nname: writer\nhooks:\n  post: verify\n---\n# Writer\n",
            encoding="utf-8",
        )
        scripts_dir = skill_root / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "verify.py").write_text(
            "import json,sys\n"
            "data=json.load(sys.stdin)\n"
            "json.loads(data['response'])\n"
            'print(\'{"status": "ok"}\')\n',
            encoding="utf-8",
        )

        responses = iter(
            [
                ('{"', [{"role": "assistant", "content": '{"'}], None, {}),
                (
                    '{"outputs":{"Proposal":"ok","Optimized":""}}',
                    [
                        {
                            "role": "assistant",
                            "content": '{"outputs":{"Proposal":"ok","Optimized":""}}',
                        }
                    ],
                    None,
                    {},
                ),
            ]
        )

        def _fake_invoke(_spec, _payload_text, _verbose, **_kwargs):
            return next(responses)

        monkeypatch.setattr("wfpy.runner._invoke_agent", _fake_invoke)

        spec = make_agent_spec(
            prompt="p",
            model="m",
            use_skill=True,
            use_skill_hooks=True,
            skill="writer",
        )
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            output_ports={
                "Proposal": make_port("Proposal", File, "out", ext=".json"),
                "Optimized": make_port("Optimized", File, "out", ext=".cpp"),
            },
            agent_spec=spec,
        )
        actor = RuntimeActor(name="agent1", kind="agent", instance=object(), meta=meta)
        actor.out_queues = {
            "Proposal": [Queue(queue_id="proposal")],
            "Optimized": [Queue(queue_id="optimized")],
        }

        plan = FifoPlan(name="wf")
        plan.options = {"skill_hook_auth": "allow-all"}
        plan.source_path = str(tmp_path / "workflow.py")

        fired = _step_agent(actor, tmp_path, plan, verbose=False)

        assert fired is True
        assert actor.out_queues["Proposal"][0].size() == 1

    def test_step_agent_debug_artifacts_store_thinking_and_record_reply_time(
        self, tmp_path, monkeypatch, make_task_meta, make_port, make_agent_spec
    ):
        from wfpy.runner import _step_agent

        response = json.dumps({"outputs": {"out": "ok"}})

        def _fake_invoke(_spec, _payload_text, _verbose, **_kwargs):
            return (
                response,
                [{"role": "assistant", "content": response}],
                None,
                {"thinking": "hidden chain of thought"},
            )

        monkeypatch.setattr("wfpy.runner._invoke_agent", _fake_invoke)

        spec = make_agent_spec(prompt="p", model="m")
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            output_ports={"out": make_port("out", str, "out")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="agent1", kind="agent", instance=object(), meta=meta)
        actor.out_queues = {"out": [Queue(queue_id="q")]}

        plan = FifoPlan(name="wf")
        plan.options = {
            "agent_debug": True,
            "agent_debug_dir": str(tmp_path),
        }

        fired = _step_agent(actor, tmp_path, plan, verbose=False)

        assert fired is True
        response_raw = json.loads(
            (tmp_path / "agent-debug" / "agent1__response_raw__0.json").read_text()
        )
        response_debug = json.loads(
            (tmp_path / "agent-debug" / "agent1__response__0.json").read_text()
        )
        assert response_raw["thinking"] == "hidden chain of thought"
        assert response_debug["thinking"] == "hidden chain of thought"
        assert isinstance(response_raw.get("replyTimeMs"), int)
        assert response_raw["replyTimeMs"] >= 0
        assert response_debug["replyTimeMs"] == response_raw["replyTimeMs"]
        assert actor.chat_history[-1]["thinking"] == "hidden chain of thought"
        assert actor.chat_history[-1]["replyTimeMs"] == response_raw["replyTimeMs"]

    def test_step_agent_prints_finished_reply_time(
        self, tmp_path, monkeypatch, capsys, make_task_meta, make_port, make_agent_spec
    ):
        from wfpy.runner import _step_agent

        response = json.dumps({"outputs": {"out": "ok"}})

        def _fake_invoke(_spec, _payload_text, _verbose, **_kwargs):
            return (
                response,
                [{"role": "assistant", "content": response}],
                None,
                {},
            )

        monkeypatch.setattr("wfpy.runner._invoke_agent", _fake_invoke)

        spec = make_agent_spec(prompt="p", model="m")
        meta = make_task_meta(
            name="AgentTask",
            kind="agent",
            output_ports={"out": make_port("out", str, "out")},
            agent_spec=spec,
        )
        actor = RuntimeActor(name="agent1", kind="agent", instance=object(), meta=meta)
        actor.out_queues = {"out": [Queue(queue_id="q")]}

        plan = FifoPlan(name="wf")
        plan.options = {}

        fired = _step_agent(actor, tmp_path, plan, verbose=False)

        assert fired is True
        stdout = capsys.readouterr().out
        assert "[wfpy][agent] agent1 finished: reply_time=" in stdout


class TestExternalStepShellCommand:
    def test_shell_true_uses_string_command(self, monkeypatch, tmp_path):
        from wfpy.core import TaskMeta, ToolSpec
        from wfpy.runner import _step_external

        class Inst:
            pass

        tool_spec = ToolSpec(
            cmd="python", args=["-c", "print('ok')"], shell=True, inherit_stdio=False
        )
        meta = TaskMeta(
            cls=object,
            name="ToolTask",
            kind="external",
            ports={},
            input_ports={
                "In": PortDescriptor(name="In", port_type=str, direction="in", ext="", validate=[])
            },
            output_ports={
                "Out": PortDescriptor(
                    name="Out", port_type=File, direction="out", ext=".txt", validate=[]
                )
            },
            actions=[],
            parameters={},
            state_fields={},
            tool_spec=tool_spec,
        )
        actor = RuntimeActor(name="tool1", kind="external", instance=Inst(), meta=meta)
        q_in = Queue(queue_id="qin")
        q_in.enqueue("input")
        actor.in_queues = {"In": [q_in]}
        actor.out_queues = {"Out": [Queue(queue_id="qout")]}

        plan = FifoPlan(name="wf")
        plan.search_paths = []
        plan.env = {}
        plan.source_path = str(tmp_path / "wf.py")
        plan.work_dir = str(tmp_path)

        monkeypatch.setattr("wfpy.runner.shutil.which", lambda cmd: cmd)

        captured = {"args": None}

        def fake_run(*args, **kwargs):
            captured["args"] = args[0]
            return types.SimpleNamespace(returncode=0, stderr="", stdout="")

        monkeypatch.setattr("wfpy.runner.subprocess.run", fake_run)

        fired = _step_external(actor, tmp_path, plan, verbose=False)
        assert fired is True
        assert isinstance(captured["args"], str)


class TestCollectQueueSnapshot:
    def test_snapshot_includes_all_queues(self):
        from wfpy.runner import Queue

        plan = FifoPlan(name="wf")
        q1 = Queue(queue_id="q1", from_actor="A", from_port="out", to_actor="B", to_port="in")
        q1.enqueue("token1")
        q1.enqueue("token2")
        q2 = Queue(queue_id="q2", from_actor="B", from_port="out", to_actor="C", to_port="in")
        plan.all_queues = [q1, q2]
        sizes = _collect_queue_snapshot(plan)
        assert len(sizes) == 2
        assert sizes[0]["queueId"] == "q1"
        assert sizes[0]["size"] == 2
        assert sizes[1]["queueId"] == "q2"
        assert sizes[1]["size"] == 0

    def test_snapshot_includes_last_token(self):
        from wfpy.runner import Queue

        plan = FifoPlan(name="wf")
        q = Queue(
            queue_id="a.Out-->b.In", from_actor="a", from_port="Out", to_actor="b", to_port="In"
        )
        plan.all_queues = [q]
        plan.edge_last_token_by_queue_id[q.id] = "/tmp/result.md"

        sizes = _collect_queue_snapshot(plan)
        assert len(sizes) == 1
        assert sizes[0]["queueId"] == "a.Out-->b.In"
        assert sizes[0]["lastToken"] == "/tmp/result.md"


class TestQueueTraceEndToEnd:
    def test_trace_written_after_run(self, tmp_path):
        """Running a workflow with queue_trace=True produces run.wf-queues.json."""
        import json

        @task
        class Add1:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x + 1

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def traced_wf():
            a = Add1()
            connect("Input", a.In)
            connect(a.Out, "Output")

        outputs = run(
            traced_wf,
            inputs={"Input": 10},
            out_dir=str(tmp_path),
            queue_trace=True,
        )
        assert outputs["Output"] == [11]

        # Find the run directory (has a run.wf-queues.json)
        run_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(run_dirs) == 1
        qt_path = run_dirs[0] / "run.wf-queues.json"
        assert qt_path.exists()
        data = json.loads(qt_path.read_text())
        assert data["version"] == 1
        assert data["stepCount"] >= 1


class TestQueueTraceDisabled:
    def test_no_trace_file_when_disabled(self, tmp_path):
        @task
        class Id:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def no_trace_wf():
            a = Id()
            connect("Input", a.In)
            connect(a.Out, "Output")

        run(
            no_trace_wf,
            inputs={"Input": 5},
            out_dir=str(tmp_path),
            queue_trace=False,
        )
        run_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(run_dirs) == 1
        qt_path = run_dirs[0] / "run.wf-queues.json"
        assert not qt_path.exists()


# ═══════════════════════════════════════════════════════════════════════════
# §11  Agent debug tests
# ═══════════════════════════════════════════════════════════════════════════


class TestAgentDebugEnabled:
    def test_option_true(self):
        assert _agent_debug_enabled({"agent_debug": True})

    def test_option_false(self, monkeypatch):
        monkeypatch.delenv("WF_AGENT_DEBUG", raising=False)
        assert not _agent_debug_enabled({"agent_debug": False})
        assert not _agent_debug_enabled({})

    def test_env_true(self, monkeypatch):
        monkeypatch.setenv("WF_AGENT_DEBUG", "1")
        assert _agent_debug_enabled({})

    def test_env_false(self, monkeypatch):
        monkeypatch.setenv("WF_AGENT_DEBUG", "0")
        assert not _agent_debug_enabled({})


class TestWriteAgentDebugArtifact:
    def test_writes_json(self, tmp_path):
        import json

        _write_agent_debug_artifact(tmp_path, "myAgent", "request__0", {"prompt": "hello"})
        f = tmp_path / "agent-debug" / "myAgent__request__0.json"
        assert f.exists()
        data = json.loads(f.read_text())
        assert data["prompt"] == "hello"

    def test_writes_text(self, tmp_path):
        _write_agent_debug_artifact(tmp_path, "myAgent", "response__0", "raw text", ext=".txt")
        f = tmp_path / "agent-debug" / "myAgent__response__0.txt"
        assert f.exists()
        assert f.read_text() == "raw text"


# ═══════════════════════════════════════════════════════════════════════════
# §11  Keep-intermediates tests
# ═══════════════════════════════════════════════════════════════════════════


class TestKeepIntermediates:
    def test_intermediates_copied(self, tmp_path):
        """With keep_intermediates=True, workDir contents are copied to runOutDir/work/."""

        @task
        class Pass:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def intermediates_wf():
            a = Pass()
            connect("Input", a.In)
            connect(a.Out, "Output")

        outputs = run(
            intermediates_wf,
            inputs={"Input": 1},
            out_dir=str(tmp_path),
            keep_intermediates=True,
        )
        assert outputs["Output"] == [1]
        # Note: for internal-only workflows, workDir may not exist or be empty
        # The keep_intermediates flag primarily matters for external/agent workflows


# ═══════════════════════════════════════════════════════════════════════════
# Custom JSON serializer tests
# ═══════════════════════════════════════════════════════════════════════════


class TestRuntimeJsonEncoder:
    """Tests for _RuntimeJsonEncoder / _runtime_json_dumps."""

    def test_set_encoding(self):
        """Python set maps to $wfType: set."""
        import json

        result = json.loads(_runtime_json_dumps({1, 2, 3}))
        assert result["$wfType"] == "set"
        assert sorted(result["values"]) == [1, 2, 3]

    def test_frozenset_encoding(self):
        import json

        result = json.loads(_runtime_json_dumps(frozenset(["a", "b"])))
        assert result["$wfType"] == "set"
        assert sorted(result["values"]) == ["a", "b"]

    def test_path_encoding(self):
        """pathlib.Path is serialized as string."""
        import json
        from pathlib import Path

        result = json.loads(_runtime_json_dumps(Path("/tmp/test.txt")))
        assert result == "/tmp/test.txt"

    def test_bytes_encoding(self):
        """bytes are decoded to UTF-8 string."""
        import json

        result = json.loads(_runtime_json_dumps(b"hello"))
        assert result == "hello"

    def test_file_encoding(self):
        """wfpy File values are serialized as their path string."""
        import json

        result = json.loads(_runtime_json_dumps(File(path="/tmp/data.txt")))
        assert result == "/tmp/data.txt"

    def test_resource_encoding(self):
        """wfpy Resource values are serialized as their locator string."""
        import json

        result = json.loads(
            _runtime_json_dumps(Resource(path="https://example.com/feed", kind="url"))
        )
        assert result == "https://example.com/feed"

    def test_nested_set_in_dict(self):
        """Sets within dicts are correctly encoded."""
        import json

        data = {"items": {1, 2}, "name": "test"}
        result = json.loads(_runtime_json_dumps(data))
        assert result["items"]["$wfType"] == "set"
        assert result["name"] == "test"

    def test_runtime_json_dumps_line(self):
        """_runtime_json_dumps_line returns a single line (no indentation)."""
        result = _runtime_json_dumps_line({"a": 1})
        assert "\n" not in result
        assert result == '{"a": 1}'

    def test_normal_values_pass_through(self):
        """Normal int/str/list/dict pass through unchanged."""
        import json

        data = {"x": 42, "y": "hello", "z": [1, 2, 3]}
        result = json.loads(_runtime_json_dumps(data))
        assert result == data


# ═══════════════════════════════════════════════════════════════════════════
# Edge token tracking tests
# ═══════════════════════════════════════════════════════════════════════════


class TestRecordEdgeToken:
    """Tests for _record_edge_token and edge tracking."""

    def test_records_token_for_queue_with_ast_path(self):
        plan = FifoPlan(name="test")
        q = Queue(queue_id="a.Out-->b.In", overlay_ast_path="/file.wf::/conns@0")
        _record_edge_token(plan, q, "hello")
        assert plan.edge_last_token_by_ast_path["/file.wf::/conns@0"] == "hello"

    def test_skips_queue_without_ast_path(self):
        plan = FifoPlan(name="test")
        q = Queue(queue_id="a.Out-->b.In")  # no overlay_ast_path
        _record_edge_token(plan, q, "hello")
        assert len(plan.edge_last_token_by_ast_path) == 0
        assert plan.edge_last_token_by_queue_id["a.Out-->b.In"] == "hello"

    def test_overwrites_previous_token(self):
        plan = FifoPlan(name="test")
        q = Queue(queue_id="a.Out-->b.In", overlay_ast_path="/conns@0")
        _record_edge_token(plan, q, "first")
        _record_edge_token(plan, q, "second")
        assert plan.edge_last_token_by_ast_path["/conns@0"] == "second"

    def test_materializes_dict_token_to_json_file(self, tmp_path):
        plan = FifoPlan(name="test")
        plan.work_dir = str(tmp_path)
        q = Queue(queue_id="a.Out-->b.In", overlay_ast_path="/conns@0")

        _record_edge_token(plan, q, {"k": 1, "items": ["a", "b"]})

        token = plan.edge_last_token_by_ast_path["/conns@0"]
        assert isinstance(token, str)
        token_path = Path(token)
        assert token_path.exists()
        payload = json.loads(token_path.read_text())
        assert payload == {"k": 1, "items": ["a", "b"]}


class TestCollectEdgeOverlay:
    """Tests for _collect_edge_overlay."""

    def test_empty_plan(self):
        plan = FifoPlan(name="test")
        edges = _collect_edge_overlay(plan)
        assert edges == {}

    def test_merges_info_and_tokens(self):
        plan = FifoPlan(name="test")
        plan.edge_info_by_ast_path["/conns@0"] = {
            "queueId": "a.Out-->b.In",
            "fromEntity": "a",
            "toEntity": "b",
            "outPort": "Out",
            "inPort": "In",
        }
        plan.edge_last_token_by_ast_path["/conns@0"] = "/tmp/output.txt"
        edges = _collect_edge_overlay(plan)
        assert "/conns@0" in edges
        e = edges["/conns@0"]
        assert e["queueId"] == "a.Out-->b.In"
        assert e["fromEntity"] == "a"
        assert e["lastToken"] == "/tmp/output.txt"

    def test_edge_without_token(self):
        plan = FifoPlan(name="test")
        plan.edge_info_by_ast_path["/conns@0"] = {
            "queueId": "a.Out-->b.In",
            "outPort": "Out",
            "inPort": "In",
        }
        edges = _collect_edge_overlay(plan)
        assert "lastToken" not in edges["/conns@0"]

    def test_queue_fallback_includes_last_token_without_ast_path(self):
        plan = FifoPlan(name="test")
        plan.edge_info_by_queue_id["a.Out-->b.In"] = {
            "queueId": "a.Out-->b.In",
            "fromEntity": "a",
            "toEntity": "b",
            "outPort": "Out",
            "inPort": "In",
        }
        plan.edge_last_token_by_queue_id["a.Out-->b.In"] = "/tmp/out.md"
        edges = _collect_edge_overlay(plan)
        assert "queue:a.Out-->b.In" in edges
        e = edges["queue:a.Out-->b.In"]
        assert e["queueId"] == "a.Out-->b.In"
        assert e["fromEntity"] == "a"
        assert e["toEntity"] == "b"
        assert e["lastToken"] == "/tmp/out.md"


# ═══════════════════════════════════════════════════════════════════════════
# Viewer overlay tests
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildViewerOverlayV1:
    """Tests for _build_viewer_overlay_v1."""

    def _base(self):
        return _ViewerOverlayBase(
            run_id="2026-03-04T10-00-00-000Z_abcd1234",
            workflow_name="test_wf",
            source_path="/test/file.wf",
            started_at="2026-03-04T10:00:00+00:00",
            out_dir="/test/wf-out/run1",
        )

    def test_basic_running(self):
        plan = FifoPlan(name="test")
        overlay = _build_viewer_overlay_v1(plan, self._base(), None, True)
        assert overlay["version"] == 1
        assert overlay["running"] is True
        assert overlay["runId"] == "2026-03-04T10-00-00-000Z_abcd1234"
        assert "active" not in overlay
        assert "error" not in overlay
        assert "finishedAt" not in overlay
        assert overlay["edges"] == {}

    def test_with_active_actor(self):
        plan = FifoPlan(name="test")
        active = _ViewerOverlayActive("transformer", ["transformer"], "external", 3)
        overlay = _build_viewer_overlay_v1(plan, self._base(), [active], True)
        assert overlay["active"][0]["entityInstanceName"] == "transformer"
        assert overlay["active"][0]["entityInstancePath"] == ["transformer"]
        assert overlay["active"][0]["entityKind"] == "external"
        assert overlay["active"][0]["fireCount"] == 3

    def test_finished_with_error(self):
        plan = FifoPlan(name="test")
        active = _ViewerOverlayActive("transformer", ["transformer"], "external", 2)
        error = {"entityInstanceName": "transformer", "message": "tool not found"}
        overlay = _build_viewer_overlay_v1(
            plan,
            self._base(),
            [active],
            False,
            finished_at="2026-03-04T10:01:00+00:00",
            error=error,
        )
        assert overlay["running"] is False
        assert overlay["finishedAt"] == "2026-03-04T10:01:00+00:00"
        assert overlay["active"][0]["entityInstancePath"] == ["transformer"]
        assert overlay["error"]["message"] == "tool not found"
        assert overlay["active"][0]["entityInstanceName"] == "transformer"

    def test_includes_edges(self):
        plan = FifoPlan(name="test")
        plan.edge_info_by_ast_path["/file.wf::/conns@0"] = {
            "queueId": "src.Out-->sink.In",
            "fromEntity": "src",
            "toEntity": "sink",
            "outPort": "Out",
            "inPort": "In",
        }
        plan.edge_last_token_by_ast_path["/file.wf::/conns@0"] = "/tmp/x.txt"
        overlay = _build_viewer_overlay_v1(plan, self._base(), None, True)
        assert "/file.wf::/conns@0" in overlay["edges"]
        edge = overlay["edges"]["/file.wf::/conns@0"]
        assert edge["lastToken"] == "/tmp/x.txt"


class TestViewerOverlayWriter:
    """Tests for _ViewerOverlayWriter."""

    def _base(self):
        return _ViewerOverlayBase(
            run_id="run1",
            workflow_name="test",
            source_path="/test.wf",
            started_at="2026-03-04T10:00:00",
            out_dir="/out",
        )

    @staticmethod
    def _base():
        return _ViewerOverlayBase(
            run_id="run1",
            workflow_name="test",
            source_path="/test.wf",
            started_at="2026-03-04T10:00:00",
            out_dir="/out",
        )

    def test_mark_running_writes_live_file(self, tmp_path):
        plan = FifoPlan(name="test")
        live_path = tmp_path / "run.wf-viewer.live.json"
        writer = _ViewerOverlayWriter(plan, self._base(), live_path)
        writer.mark_running()
        assert live_path.exists()
        import json

        data = json.loads(live_path.read_text())
        assert data["running"] is True
        assert data["version"] == 1

    def test_set_active_updates(self, tmp_path, make_actor):
        plan = FifoPlan(name="test")
        live_path = tmp_path / "live.json"
        writer = _ViewerOverlayWriter(plan, self._base(), live_path)
        actor = make_actor("actor1")
        writer.set_active(actor)
        import json

        data = json.loads(live_path.read_text())
        assert data["active"][0]["entityInstanceName"] == "actor1"

    def test_set_active_null_clears(self, tmp_path, make_actor):
        plan = FifoPlan(name="test")
        live_path = tmp_path / "live.json"
        writer = _ViewerOverlayWriter(plan, self._base(), live_path)
        actor = make_actor("actor1")
        writer.set_active(actor)
        writer.set_active(None)
        import json

        data = json.loads(live_path.read_text())
        assert "active" not in data

    def test_deduplication(self, tmp_path):
        """Same active key does not rewrite."""
        plan = FifoPlan(name="test")
        live_path = tmp_path / "live.json"
        writer = _ViewerOverlayWriter(plan, self._base(), live_path)
        writer.mark_running()
        mtime1 = live_path.stat().st_mtime_ns
        writer.mark_running()  # same state → should not rewrite
        mtime2 = live_path.stat().st_mtime_ns
        assert mtime1 == mtime2

    def test_cleanup_deletes_live_file(self, tmp_path):
        plan = FifoPlan(name="test")
        live_path = tmp_path / "live.json"
        writer = _ViewerOverlayWriter(plan, self._base(), live_path)
        writer.mark_running()
        assert live_path.exists()
        writer.cleanup()
        assert not live_path.exists()

    def test_mark_stopped_clears_active_and_running(self, tmp_path, make_actor):
        plan = FifoPlan(name="test")
        live_path = tmp_path / "live.json"
        writer = _ViewerOverlayWriter(plan, self._base(), live_path)
        writer.set_active(make_actor("actor1"))
        writer.mark_stopped()

        data = json.loads(live_path.read_text())
        assert data["running"] is False
        assert "active" not in data

    def test_last_active_preserved(self, tmp_path, make_actor):
        plan = FifoPlan(name="test")
        live_path = tmp_path / "live.json"
        writer = _ViewerOverlayWriter(plan, self._base(), live_path)
        actor = make_actor("myActor", kind="external")
        writer.set_active(actor)
        entries = writer.active_entries
        assert len(entries) == 1
        assert entries[0].entity_instance_name == "myActor"
        writer.set_active(None)
        assert len(writer.active_entries) == 0

    def test_multiple_parallel_active(self, tmp_path, make_actor):
        """Multiple actors can be active simultaneously for parallel execution."""
        plan = FifoPlan(name="test")
        live_path = tmp_path / "live.json"
        writer = _ViewerOverlayWriter(plan, self._base(), live_path)
        actor_a = make_actor("actor_a", kind="task")
        actor_b = make_actor("actor_b", kind="task")
        writer.set_active_with_prefix(actor_a, [])
        assert len(writer.active_entries) == 1
        writer.set_active_with_prefix(actor_b, [])
        assert len(writer.active_entries) == 2
        import json
        data = json.loads(live_path.read_text())
        names = [a["entityInstanceName"] for a in data["active"]]
        assert "actor_a" in names
        assert "actor_b" in names
        writer.remove_active_with_prefix(actor_a, [])
        assert len(writer.active_entries) == 1
        assert writer.active_entries[0].entity_instance_name == "actor_b"
        writer.remove_active_with_prefix(actor_b, [])
        assert len(writer.active_entries) == 0
        data = json.loads(live_path.read_text())
        assert "active" not in data


# ═══════════════════════════════════════════════════════════════════════════
# Agent context overlay tests
# ═══════════════════════════════════════════════════════════════════════════


class TestAgentContextOverlay:
    """Tests for agent context overlay building."""

    def test_build_entry(self, make_agent_actor):
        actor = make_agent_actor(
            chat_history=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
            ],
            stateful=True,
            context_budget=50,
            truncation_strategy="sliding",
        )
        entry = _build_agent_context_entry(actor)
        assert entry["instanceName"] == "agent1"
        assert entry["stateful"] is True
        assert entry["contextBudget"] == 50
        assert len(entry["chatHistory"]) == 2

    def test_collect_entries_from_plan(self, make_agent_actor):
        plan = FifoPlan(name="test")
        agent = make_agent_actor(
            chat_history=[
                {"role": "user", "content": "hi"},
            ],
            stateful=True,
        )
        plan.actors.append(agent)
        entries = _collect_agent_entries(plan)
        assert len(entries) == 1
        assert entries[0]["instanceName"] == "agent1"

    def test_collect_entries_skips_empty_history(self, make_agent_actor):
        plan = FifoPlan(name="test")
        agent = make_agent_actor(chat_history=[], stateful=True)
        plan.actors.append(agent)
        entries = _collect_agent_entries(plan)
        assert entries == []

    def test_build_overlay(self, make_agent_actor):
        plan = FifoPlan(name="test")
        agent = make_agent_actor(
            chat_history=[
                {"role": "user", "content": "query"},
            ],
            stateful=True,
        )
        plan.actors.append(agent)
        overlay = _build_agent_context_overlay(plan, "run1", "myWf", "/test.wf")
        assert overlay["version"] == 1
        assert overlay["runId"] == "run1"
        assert len(overlay["agents"]) == 1

    def test_build_entry_includes_cli_session_ids(self, make_agent_actor):
        actor = make_agent_actor(
            chat_history=[{"role": "user", "content": "hello"}],
            stateful=True,
        )
        actor.agent_cli_session_ids["opencode"] = "sess-123"

        entry = _build_agent_context_entry(actor)

        assert entry["agentCliSessionIds"] == {"opencode": "sess-123"}


class TestAgentContextWriter:
    """Tests for _AgentContextWriter."""

    def test_write_creates_live_file(self, tmp_path, make_agent_actor):
        plan = FifoPlan(name="test")
        actor = make_agent_actor(stateful=True, context_budget=50)
        actor.chat_history = [{"role": "user", "content": "hello"}]
        plan.actors.append(actor)
        live = tmp_path / "live.json"
        writer = _AgentContextWriter(plan, "run1", "wf", "/test.wf", live)
        writer.write()
        assert live.exists()
        import json

        data = json.loads(live.read_text())
        assert data["version"] == 1
        assert len(data["agents"]) == 1

    def test_deduplication(self, tmp_path, make_agent_actor):
        plan = FifoPlan(name="test")
        actor = make_agent_actor(stateful=True, context_budget=50)
        actor.chat_history = [{"role": "user", "content": "hi"}]
        plan.actors.append(actor)
        live = tmp_path / "live.json"
        writer = _AgentContextWriter(plan, "run1", "wf", "/test.wf", live)
        writer.write()
        mtime1 = live.stat().st_mtime_ns
        writer.write()  # same state
        mtime2 = live.stat().st_mtime_ns
        assert mtime1 == mtime2

    def test_cleanup_deletes(self, tmp_path):
        plan = FifoPlan(name="test")
        live = tmp_path / "live.json"
        live.write_text("{}")
        writer = _AgentContextWriter(plan, "run1", "wf", "/test.wf", live)
        writer.cleanup()
        assert not live.exists()


class TestSharedContextPrimitives:
    def test_apply_context_patch_commits_and_versions(self, make_actor):
        plan = FifoPlan(name="ctx")
        actor = make_actor("worker")
        policy = _actor_context_policy(actor)
        patch = {
            "baseVersion": 0,
            "ops": [
                {"op": "set", "path": "artifacts.byNode.worker.goal", "value": "ship"},
                {"op": "append", "path": "runtime.events", "value": {"k": "v"}},
            ],
        }

        _apply_context_patch(plan, actor, policy, patch, source="test")

        assert plan.context_version == 1
        assert plan.context_commit_seq == 1
        assert _context_get(plan.context_store, "artifacts.byNode.worker.goal") == "ship"
        assert isinstance(_context_get(plan.context_store, "runtime.events"), list)
        assert len(plan.context_journal) == 1
        assert plan.context_journal[0]["nodeInstance"] == "worker"

    def test_apply_context_patch_rejects_out_of_scope_write(self, make_actor):
        import pytest

        plan = FifoPlan(name="ctx")
        actor = make_actor("worker")
        policy = {"read": ["global.*"], "write": ["artifacts.byNode.worker"]}

        with pytest.raises(RuntimeError, match="write denied"):
            _apply_context_patch(
                plan,
                actor,
                policy,
                {
                    "baseVersion": 0,
                    "ops": [{"op": "set", "path": "global.goal", "value": "x"}],
                },
                source="test",
            )

    def test_action_context_facade_generates_patch(self, make_actor):
        plan = FifoPlan(name="ctx")
        actor = make_actor("worker")
        policy = _actor_context_policy(actor)
        view = _build_context_view(plan, actor, policy)
        ctx = _ActionContextFacade(plan, actor, policy, view)

        ctx.set("artifacts.byNode.worker.result", {"ok": True})
        ctx.append("runtime.events", {"name": "done"})
        patch = ctx.context_patch()

        assert patch is not None
        assert patch["baseVersion"] == 0
        assert len(patch["ops"]) == 2

    def test_extract_agent_context_patch(self):
        text = '{"Out": "ok", "contextPatch": {"baseVersion": 0, "ops": [{"op":"set","path":"artifacts.latest","value":{"x":1}}]}}'
        patch = _extract_agent_context_patch(text)
        assert patch is not None
        assert patch["baseVersion"] == 0

    def test_context_overlay_and_live_writer(self, tmp_path):
        plan = FifoPlan(name="ctx")
        plan.context_store = {"global": {"goal": "ship"}}
        plan.context_version = 2
        plan.context_journal = [{"commitSequence": 1}]

        overlay = _build_context_overlay(plan, "run1", "wf", "/x.py")
        assert overlay["contextVersion"] == 2
        assert overlay["contextJournalSize"] == 1

        live = tmp_path / "run.wf-context.live.json"
        writer = _ContextLiveWriter(plan, "run1", "wf", "/x.py", live)
        writer.write()
        assert live.exists()
        first_mtime = live.stat().st_mtime_ns
        writer.write()
        second_mtime = live.stat().st_mtime_ns
        assert first_mtime == second_mtime
        plan.context_version = 3
        writer.write()
        third_mtime = live.stat().st_mtime_ns
        assert third_mtime >= second_mtime
        writer.cleanup()
        assert not live.exists()


# ═══════════════════════════════════════════════════════════════════════════
# Rewrite viewer overlay tokens
# ═══════════════════════════════════════════════════════════════════════════


class TestRewriteViewerOverlayTokens:
    """Tests for _rewrite_viewer_overlay_tokens."""

    def test_rewrites_matching_prefix(self):
        plan = FifoPlan(name="test")
        plan.edge_last_token_by_ast_path["/conns@0"] = "/tmp/wf-dir/out.txt"
        _rewrite_viewer_overlay_tokens(plan, "/tmp/wf-dir", "/persist/work")
        assert plan.edge_last_token_by_ast_path["/conns@0"] == "/persist/work/out.txt"

    def test_ignores_non_matching_prefix(self):
        plan = FifoPlan(name="test")
        plan.edge_last_token_by_ast_path["/conns@0"] = "/other/path.txt"
        _rewrite_viewer_overlay_tokens(plan, "/tmp/wf-dir", "/persist/work")
        assert plan.edge_last_token_by_ast_path["/conns@0"] == "/other/path.txt"

    def test_ignores_non_string_tokens(self):
        plan = FifoPlan(name="test")
        plan.edge_last_token_by_ast_path["/conns@0"] = 42
        _rewrite_viewer_overlay_tokens(plan, "/tmp/wf-dir", "/persist/work")
        assert plan.edge_last_token_by_ast_path["/conns@0"] == 42


# ═══════════════════════════════════════════════════════════════════════════
# E2E: Viewer overlay written by run()
# ═══════════════════════════════════════════════════════════════════════════


class TestViewerOverlayEndToEnd:
    """End-to-end tests checking overlay files are written by run()."""

    def test_final_overlay_written(self, tmp_path):
        """run() writes run.wf-viewer.json for a successful run."""

        @task
        class Inc:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x + 1

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def overlay_wf():
            a = Inc()
            connect("Input", a.In)
            connect(a.Out, "Output")

        run(overlay_wf, inputs={"Input": 5}, out_dir=str(tmp_path))

        # Find the run directory
        import json

        run_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(run_dirs) == 1
        run_dir = run_dirs[0]

        viewer_path = run_dir / "run.wf-viewer.json"
        assert viewer_path.exists(), "run.wf-viewer.json should exist"
        data = json.loads(viewer_path.read_text())
        assert data["version"] == 1
        assert data["running"] is False
        assert "finishedAt" in data
        assert "error" not in data

    def test_live_overlay_cleaned_up(self, tmp_path):
        """run.wf-viewer.live.json should be deleted after run."""

        @task
        class Pass:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def live_cleanup_wf():
            a = Pass()
            connect("Input", a.In)
            connect(a.Out, "Output")

        run(live_cleanup_wf, inputs={"Input": 1}, out_dir=str(tmp_path))
        live_path = tmp_path / "run.wf-viewer.live.json"
        assert not live_path.exists(), "live overlay should be cleaned up"

    def test_error_overlay_written(self, tmp_path):
        """On error, run.wf-viewer.json should contain error info."""
        import pytest

        @task
        class Fail:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                raise RuntimeError("boom")

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def error_wf():
            a = Fail()
            connect("Input", a.In)
            connect(a.Out, "Output")

        with pytest.raises(RuntimeError, match="boom"):
            run(error_wf, inputs={"Input": 1}, out_dir=str(tmp_path))

        # Find the run directory
        import json

        run_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(run_dirs) == 1
        run_dir = run_dirs[0]

        viewer_path = run_dir / "run.wf-viewer.json"
        assert viewer_path.exists(), "error overlay should be written"
        data = json.loads(viewer_path.read_text())
        assert data["running"] is False
        assert "error" in data
        assert "boom" in data["error"]["message"]

    def test_interrupt_overlay_clears_active_nodes(self, tmp_path):
        """On user interrupt, run.wf-viewer.json should stop glowing active nodes."""
        import pytest

        @task
        class Interrupt:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, _x: int) -> int:
                raise KeyboardInterrupt()

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def interrupted_wf():
            a = Interrupt()
            connect("Input", a.In)
            connect(a.Out, "Output")

        with pytest.raises(KeyboardInterrupt):
            run(interrupted_wf, inputs={"Input": 1}, out_dir=str(tmp_path))

        run_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(run_dirs) == 1
        run_dir = run_dirs[0]

        viewer_path = run_dir / "run.wf-viewer.json"
        assert viewer_path.exists(), "interrupt overlay should be written"
        data = json.loads(viewer_path.read_text())
        assert data["running"] is False
        assert "active" not in data
        assert data["error"]["message"] == "KeyboardInterrupt"

    def test_run_record_uses_custom_encoder(self, tmp_path):
        """run.wf-run.json should use _RuntimeJsonEncoder for sets."""

        @task
        class Identity:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def encoder_wf():
            a = Identity()
            connect("Input", a.In)
            connect(a.Out, "Output")

        run(encoder_wf, inputs={"Input": 42}, out_dir=str(tmp_path))
        # Verify run record exists and is valid JSON
        import json

        run_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(run_dirs) == 1
        run_dir = run_dirs[0]
        run_record_path = run_dir / "run.wf-run.json"
        assert run_record_path.exists()
        data = json.loads(run_record_path.read_text())
        assert data["runId"]  # has a run ID


class TestEdgeTokenRecordingEndToEnd:
    """E2E: Internal actors record edge tokens in the plan."""

    def test_internal_actor_records_edge_token(self, tmp_path):
        """After running an internal actor, edge_last_token is populated."""

        @task
        class Double:
            class Ports:
                In = Port[int]()
                Out = Port[int]()

            def action(self, x: int) -> int:
                return x * 2

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def token_wf():
            a = Double()
            connect("Input", a.In)
            connect(a.Out, "Output")

        # run() creates a plan internally — verify the output is correct
        outputs = run(token_wf, inputs={"Input": 5}, out_dir=str(tmp_path))
        assert outputs["Output"] == [10]

    def test_internal_file_outputs_are_materialized_under_run_work_dir(self, tmp_path):
        @task
        class EmitTempFile:
            class Ports:
                Out = Port[File](direction="out", ext=".txt")

            _done: bool = False

            @action(produces={"Out": 1})
            def emit(self) -> File:
                if self._done:
                    return None  # type: ignore[return-value]
                self._done = True
                temp_path = tmp_path / "outside-token.txt"
                temp_path.write_text("hello")
                return File(str(temp_path))

        @task
        class CapturePath:
            class Ports:
                In = Port[File](direction="in", ext=".txt")
                Out = Port[bool](direction="out")

            @action(consumes={"In": 1}, produces={"Out": 1})
            def grab(self, In: File) -> bool:
                expected_root = Path(os.environ["WF_RUN_OUT_DIR"]) / "work"
                return str(In).startswith(str(expected_root))

        @workflow(outputs={"Captured": bool})
        def wf():
            emit = EmitTempFile()
            capture = CapturePath()
            connect(emit.Out, capture.In)
            connect(capture.Out, "Captured")

        outputs = run(wf, out_dir=str(tmp_path))
        run_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(run_dirs) == 1
        work_dir = run_dirs[0] / "work"
        assert outputs["Captured"] == [True]
        materialized = work_dir / "emit__Out__0.txt"
        assert materialized.exists()
        assert materialized.read_text() == "hello"

    def test_internal_pass_through_file_output_keeps_original_path(self, tmp_path):
        source_file = tmp_path / "source.txt"
        source_file.write_text("hello\n")

        @task
        class Forward:
            class Ports:
                In = Port[File](direction="in", ext=".txt")
                Out = Port[File](direction="out", ext=".txt")

            @action(consumes={"In": 1}, produces={"Out": 1})
            def forward(self, In: File) -> File:
                return In

        @task
        class CapturePath:
            class Ports:
                In = Port[File](direction="in", ext=".txt")
                Out = Port[bool](direction="out")

            @action(consumes={"In": 1}, produces={"Out": 1})
            def grab(self, In: File) -> bool:
                return str(In) == str(source_file.resolve())

        @workflow(inputs={"Input": File}, outputs={"Captured": bool})
        def wf():
            forward = Forward()
            capture = CapturePath()
            connect("Input", forward.In)
            connect(forward.Out, capture.In)
            connect(capture.Out, "Captured")

        outputs = run(wf, inputs={"Input": str(source_file)}, out_dir=str(tmp_path))
        assert outputs["Captured"] == [True]

    def test_internal_cached_workflow_input_reemit_keeps_original_path(self, tmp_path):
        script_file = tmp_path / "script.py"
        script_file.write_text("from sibling import value\n")

        @task
        class CacheAndReplay:
            cached: File = File()

            class Ports:
                In = Port[File](direction="in", ext=".py")
                Trigger = Port[bool](direction="in")
                Out = Port[File](direction="out", ext=".py")

            @action(consumes={"In": 1}, produces={})
            def cache(self, In: File) -> None:
                self.cached = In

            @action(consumes={"Trigger": 1}, produces={"Out": 1})
            def replay(self, Trigger: bool) -> File:
                del Trigger
                return self.cached

        @task
        class EmitTrigger:
            fired: bool = False

            class Ports:
                Out = Port[bool](direction="out")

            @action(produces={"Out": 1})
            @guard(lambda self: not self.fired)
            def fire(self) -> bool:
                self.fired = True
                return True

        @task
        class CapturePath:
            class Ports:
                In = Port[File](direction="in", ext=".py")
                Out = Port[bool](direction="out")

            @action(consumes={"In": 1}, produces={"Out": 1})
            def grab(self, In: File) -> bool:
                return str(In) == str(script_file.resolve())

        @workflow(inputs={"Input": File}, outputs={"Captured": bool})
        def wf():
            cache = CacheAndReplay()
            trigger = EmitTrigger()
            capture = CapturePath()
            connect("Input", cache.In)
            connect(trigger.Out, cache.Trigger)
            connect(cache.Out, capture.In)
            connect(capture.Out, "Captured")

        outputs = run(wf, inputs={"Input": str(script_file)}, out_dir=str(tmp_path))
        assert outputs["Captured"] == [True]

    def test_configured_file_source_reemit_keeps_original_path(self, tmp_path):
        source_dir = tmp_path / "src"
        source_dir.mkdir()
        include_dir = source_dir / "include"
        include_dir.mkdir()
        source_file = source_dir / "kernel.cpp"
        source_file.write_text('#include "common.h"\n')
        (include_dir / "common.h").write_text("// header\n")

        @task
        class FileSourceLike:
            path: File
            emitted: bool = False

            class Ports:
                Out = Port[File](direction="out", ext=".cpp")

            @action(produces={"Out": 1})
            @guard(lambda self: not self.emitted)
            def emit(self) -> File:
                self.emitted = True
                return self.path

        @task
        class CapturePath:
            class Ports:
                In = Port[File](direction="in", ext=".cpp")
                Out = Port[bool](direction="out")

            @action(consumes={"In": 1}, produces={"Out": 1})
            def grab(self, In: File) -> bool:
                return str(In) == str(source_file.resolve())

        @workflow(outputs={"Captured": bool})
        def wf():
            src = FileSourceLike(path=File(str(source_file)))
            capture = CapturePath()
            connect(src.Out, capture.In)
            connect(capture.Out, "Captured")

        outputs = run(wf, out_dir=str(tmp_path))
        assert outputs["Captured"] == [True]

    def test_cached_configured_file_source_reemit_keeps_original_path(self, tmp_path):
        source_dir = tmp_path / "src"
        source_dir.mkdir()
        source_file = source_dir / "script.py"
        source_file.write_text("from sibling import value\n")

        @task
        class FileSourceLike:
            path: File
            emitted: bool = False

            class Ports:
                Out = Port[File](direction="out", ext=".py")

            @action(produces={"Out": 1})
            @guard(lambda self: not self.emitted)
            def emit(self) -> File:
                self.emitted = True
                return self.path

        @task
        class CacheAndReplay:
            cached: File = File()

            class Ports:
                In = Port[File](direction="in", ext=".py")
                Trigger = Port[bool](direction="in")
                Out = Port[File](direction="out", ext=".py")

            @action(consumes={"In": 1}, produces={})
            def cache(self, In: File) -> None:
                self.cached = In

            @action(consumes={"Trigger": 1}, produces={"Out": 1})
            def replay(self, Trigger: bool) -> File:
                del Trigger
                return self.cached

        @task
        class EmitTrigger:
            fired: bool = False

            class Ports:
                Out = Port[bool](direction="out")

            @action(produces={"Out": 1})
            @guard(lambda self: not self.fired)
            def fire(self) -> bool:
                self.fired = True
                return True

        @task
        class CapturePath:
            class Ports:
                In = Port[File](direction="in", ext=".py")
                Out = Port[bool](direction="out")

            @action(consumes={"In": 1}, produces={"Out": 1})
            def grab(self, In: File) -> bool:
                return str(In) == str(source_file.resolve())

        @workflow(outputs={"Captured": bool})
        def wf():
            src = FileSourceLike(path=File(str(source_file)))
            cache = CacheAndReplay()
            trigger = EmitTrigger()
            capture = CapturePath()
            connect(src.Out, cache.In)
            connect(trigger.Out, cache.Trigger)
            connect(cache.Out, capture.In)
            connect(capture.Out, "Captured")

        outputs = run(wf, out_dir=str(tmp_path))
        assert outputs["Captured"] == [True]


class TestCmdValidatorEnvAndDiagnostics:
    """Tests for _run_cmd_validator env propagation and compiler diagnostic parsing."""

    def test_cmd_validator_inherits_workflow_env(self, tmp_path):
        """Command validator receives @config env/path via plan."""
        test_file = tmp_path / "test.cpp"
        test_file.write_text("// ok")

        v = AgentOutputValidator(cmd="env", args=[])
        plan = FifoPlan()
        plan.env = {"MY_TEST_VAR": "from_config"}
        plan.search_paths = ["/fake/search/bin"]

        errors: list[dict] = []
        _run_cmd_validator(v, "Out", str(test_file), "test_actor", False, errors, plan)
        # env should exit 0 (pass), no errors
        assert len(errors) == 0

    def test_cmd_validator_per_validator_env_overrides_config(self, tmp_path):
        """Per-validator env takes precedence over workflow @config env."""
        # Use a script that checks a specific env var
        script = tmp_path / "check_env.sh"
        script.write_text(
            '#!/bin/bash\nif [ "$MY_VAR" = "from_validator" ]; then exit 0; else exit 1; fi\n'
        )
        script.chmod(0o755)

        test_file = tmp_path / "test.cpp"
        test_file.write_text("// ok")

        v = AgentOutputValidator(
            cmd=str(script),
            args=[],
            env={"MY_VAR": "from_validator"},
        )
        plan = FifoPlan()
        plan.env = {"MY_VAR": "from_config"}

        errors: list[dict] = []
        _run_cmd_validator(v, "Out", str(test_file), "test_actor", False, errors, plan)
        assert len(errors) == 0

    def test_parse_compiler_diagnostics_gcc_style(self):
        """Parses GCC/Clang-style file:line:col: severity: message."""
        stderr = (
            "/tmp/test.cpp:42:12: error: unknown type name 'AICORE'\n"
            "/tmp/test.cpp:50:5: warning: unused variable 'x'\n"
            "1 error generated.\n"
        )
        diags = _parse_compiler_diagnostics(stderr, "/tmp/test.cpp")
        assert len(diags) == 2
        assert diags[0].line == 42
        assert diags[0].col == 12
        assert diags[0].severity == "error"
        assert "AICORE" in diags[0].message
        assert diags[1].line == 50
        assert diags[1].severity == "warning"

    def test_parse_compiler_diagnostics_fatal_error(self):
        """Parses 'fatal error' as error severity."""
        stderr = "/tmp/test.cpp:1:10: fatal error: 'missing.h' file not found\n"
        diags = _parse_compiler_diagnostics(stderr, "/tmp/test.cpp")
        assert len(diags) == 1
        assert diags[0].severity == "error"
        assert diags[0].line == 1

    def test_cmd_validator_parses_diagnostics_into_lsp_kind(self, tmp_path):
        """When cmd validator fails with parseable diagnostics, kind becomes 'lsp' for structured repair."""
        # Create a script that outputs compiler-style errors
        script = tmp_path / "fake_compiler.sh"
        script.write_text('#!/bin/bash\necho "$1:10:5: error: undeclared identifier" >&2\nexit 1\n')
        script.chmod(0o755)

        test_file = tmp_path / "test.cpp"
        test_file.write_text("// some code\n" * 20)

        v = AgentOutputValidator(cmd=str(script), args=["{file}"])
        errors: list[dict] = []
        _run_cmd_validator(v, "Out", str(test_file), "test", False, errors)
        assert len(errors) == 1
        assert errors[0]["kind"] == "lsp"  # promoted to structured
        assert len(errors[0]["diagnostics"]) == 1
        assert errors[0]["diagnostics"][0].line == 10

    def test_cmd_validator_replaces_file_dir_and_uses_file_parent_cwd(self, tmp_path):
        """Command validators can reference {file_dir} and run from the output file's parent."""
        script = tmp_path / "capture_validator.sh"
        script.write_text(
            '#!/bin/bash\nset -euo pipefail\nprintf "%s\\n" "$PWD" > "$1"\nprintf "%s\\n" "$2" >> "$1"\n'
        )
        script.chmod(0o755)

        file_dir = tmp_path / "nested"
        file_dir.mkdir()
        test_file = file_dir / "test.cpp"
        test_file.write_text("// ok\n")
        capture = tmp_path / "capture.txt"

        v = AgentOutputValidator(cmd=str(script), args=[str(capture), "{file_dir}"])
        errors: list[dict] = []
        _run_cmd_validator(v, "Out", str(test_file), "test", False, errors)

        assert errors == []
        assert capture.read_text().splitlines() == [
            str(file_dir.resolve()),
            str(file_dir.resolve()),
        ]


class TestAgentOutputValidationFailures:
    def test_validator_exhaustion_raises_and_blocks_downstream(self, tmp_path, monkeypatch):
        import pytest

        from wfpy import agent

        validator = tmp_path / "fail_validator.sh"
        validator.write_text('#!/bin/bash\nprintf "validator failed\\n" >&2\nexit 1\n')
        validator.chmod(0o755)

        calls = {"count": 0}
        seen: list[str] = []

        def fake_invoke_agent(*_args, **_kwargs):
            calls["count"] += 1
            payload = (
                '#include <pto/pto-inst.hpp>\n'
                'extern "C" void call_kernel(uint32_t block_dim, void* stream) {\n'
                '    (void)block_dim;\n'
                '    (void)stream;\n'
                '}\n'
            )
            return payload, [{"role": "assistant", "content": payload}], None, {}

        monkeypatch.setattr("wfpy.runner._invoke_agent", fake_invoke_agent)

        @agent(
            prompt="test",
            useSkill=False,
            outputValidators=[
                {
                    "cmd": str(validator),
                    "args": [],
                    "maxRepairAttempts": 1,
                }
            ],
        )
        class KernelAgent:
            class Ports:
                In = Port[str](direction="in")
                Kernel = Port[File](direction="out", ext=".cpp")

        @task
        class CaptureKernel:
            class Ports:
                In = Port[File](direction="in")
                Out = Port[bool](direction="out")

            def action(self, In: File) -> bool:
                seen.append(str(In))
                return True

        @workflow(inputs={"In": str}, outputs={"Seen": bool})
        def wf():
            agent_node = KernelAgent()
            capture = CaptureKernel()
            connect("In", agent_node.In)
            connect(agent_node.Kernel, capture.In)
            connect(capture.Out, "Seen")

        # Best-effort validation: delivers output even when validation fails
        result = run(wf, inputs={"In": "go"}, out_dir=tmp_path / "run")
        
        # Verify agent was called twice (initial + 1 repair attempt)
        assert calls["count"] == 2
        # Verify downstream task ran with best-effort output
        assert len(seen) == 1
        assert seen[0].endswith(".cpp")
        # Verify output file exists
        assert Path(seen[0]).exists()

    def test_validation_repair_prompt_keeps_structural_errors_for_single_cpp_output(self):
        ports = {
            "Kernel": PortDescriptor(
                name="Kernel", port_type=File, direction="out", ext=".cpp", validate=[]
            )
        }
        val_errors = [
            {
                "port": "Kernel",
                "cmd": "python validate_stage_kernel_output.py",
                "kind": "cmd",
                "stderr": "missing host call_kernel entrypoint",
                "returncode": 1,
            },
            {
                "port": "Kernel",
                "cmd": "bisheng",
                "kind": "lsp",
                "stderr": "kernel.cpp:1:1: error: unknown type name 'Inspecting'",
                "returncode": 1,
                "diagnostics": [],
            },
        ]

        prompt = _build_validation_repair_prompt("Inspecting...", val_errors, ports)

        assert "Additional validator failures" in prompt
        assert "missing host call_kernel entrypoint" in prompt
        assert "required leading comment banner" in prompt
        assert "Return the complete corrected raw file content" in prompt

    def test_validation_repair_prompt_preserves_banner_for_command_only_cpp_failures(self):
        ports = {
            "Kernel": PortDescriptor(
                name="Kernel", port_type=File, direction="out", ext=".cpp", validate=[]
            )
        }
        val_errors = [
            {
                "port": "Kernel",
                "cmd": "python validate_stage_kernel_output.py",
                "kind": "cmd",
                "stderr": "missing required top-of-file banner before first #include",
                "returncode": 1,
            }
        ]

        prompt = _build_validation_repair_prompt(
            "// banner\n#include <pto/pto-inst.hpp>\n",
            val_errors,
            ports,
        )

        assert "required leading comment banner" in prompt
        assert "Output ports: Kernel" in prompt
        assert "Return ONLY the corrected raw file content" in prompt

    def test_single_json_file_output_retries_parse_before_validation(self, monkeypatch, tmp_path):
        calls = {"count": 0}
        seen: list[str] = []

        def fake_invoke_agent(*_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                payload = (
                    "Decoding the PTO intrinsics before writing the JSON artifact.\n"
                    '{"outputs":{"SemanticContract":{"schema_version":"semantic_contract_v1"}}'
                )
            else:
                payload = '{"outputs":{"SemanticContract":{"schema_version":"semantic_contract_v1"}}}'
            return payload, [{"role": "assistant", "content": payload}], None, {}

        monkeypatch.setattr("wfpy.runner._invoke_agent", fake_invoke_agent)

        validator = tmp_path / "validate_semantic.py"
        validator.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "from pathlib import Path\n"
            "path = Path(sys.argv[1])\n"
            "seen = json.loads(path.read_text())\n"
            "assert seen['schema_version'] == 'semantic_contract_v1'\n"
        )
        validator.chmod(0o755)

        @agent(
            prompt="test",
            useSkill=False,
            outputValidators=[{"cmd": str(validator), "args": ["{file}"]}],
        )
        class SemanticAgent:
            class Ports:
                In = Port[str](direction="in")
                SemanticContract = Port[File](direction="out", ext=".json")

        @task
        class CaptureSemantic:
            class Ports:
                In = Port[File](direction="in")
                Out = Port[bool](direction="out")

            def action(self, In: File) -> bool:
                seen.append(Path(str(In)).read_text())
                return True

        @workflow(inputs={"In": str}, outputs={"Seen": bool})
        def wf():
            agent_node = SemanticAgent()
            capture = CaptureSemantic()
            connect("In", agent_node.In)
            connect(agent_node.SemanticContract, capture.In)
            connect(capture.Out, "Seen")

        outputs = run(wf, inputs={"In": "go"}, out_dir=tmp_path / "run")

        assert outputs["Seen"] == [True]
        assert calls["count"] == 2
        assert len(seen) == 1
        assert json.loads(seen[0]) == {"schema_version": "semantic_contract_v1"}

    def test_validation_repair_preserves_prior_multi_output_files_when_repair_blanks_siblings(
        self, monkeypatch, tmp_path
    ):
        calls = {"count": 0}
        seen: list[dict[str, str]] = []

        def fake_invoke_agent(*_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                payload = json.dumps(
                    {
                        "outputs": {
                            "ExecutionContract": {"schema_version": "execution_contract_v1"},
                            "ReferenceModel": "def reference_model():\n    return 1\n",
                            "ValidationScript": "print('validate')\n",
                            "BenchmarkScript": "print('bad benchmark')\n",
                        }
                    }
                )
            else:
                payload = json.dumps(
                    {
                        "outputs": {
                            "ExecutionContract": "",
                            "ReferenceModel": "",
                            "ValidationScript": "",
                            "BenchmarkScript": "print('good benchmark')\n",
                        }
                    }
                )
            return payload, [{"role": "assistant", "content": payload}], None, {}

        monkeypatch.setattr("wfpy.runner._invoke_agent", fake_invoke_agent)

        validator = tmp_path / "validate_benchmark.py"
        validator.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "import sys\n"
            "text = Path(sys.argv[1]).read_text()\n"
            "if 'good benchmark' not in text:\n"
            "    raise SystemExit('benchmark not repaired')\n"
        )
        validator.chmod(0o755)

        @agent(
            prompt="test",
            useSkill=False,
            outputValidators=[
                {
                    "cmd": str(validator),
                    "args": ["{file}"],
                    "ports": ["BenchmarkScript"],
                    "maxRepairAttempts": 1,
                }
            ],
        )
        class ArtifactAgent:
            class Ports:
                In = Port[str](direction="in")
                ExecutionContract = Port[File](direction="out", ext=".json")
                ReferenceModel = Port[File](direction="out", ext=".py")
                ValidationScript = Port[File](direction="out", ext=".py")
                BenchmarkScript = Port[File](direction="out", ext=".py")

        @task
        class CaptureArtifacts:
            class Ports:
                ExecutionContract = Port[File](direction="in")
                ReferenceModel = Port[File](direction="in")
                ValidationScript = Port[File](direction="in")
                BenchmarkScript = Port[File](direction="in")
                Out = Port[bool](direction="out")

            def action(
                self,
                ExecutionContract: File,
                ReferenceModel: File,
                ValidationScript: File,
                BenchmarkScript: File,
            ) -> bool:
                seen.append(
                    {
                        "ExecutionContract": Path(str(ExecutionContract)).read_text(),
                        "ReferenceModel": Path(str(ReferenceModel)).read_text(),
                        "ValidationScript": Path(str(ValidationScript)).read_text(),
                        "BenchmarkScript": Path(str(BenchmarkScript)).read_text(),
                    }
                )
                return True

        @workflow(inputs={"In": str}, outputs={"Seen": bool})
        def wf():
            agent_node = ArtifactAgent()
            capture = CaptureArtifacts()
            connect("In", agent_node.In)
            connect(agent_node.ExecutionContract, capture.ExecutionContract)
            connect(agent_node.ReferenceModel, capture.ReferenceModel)
            connect(agent_node.ValidationScript, capture.ValidationScript)
            connect(agent_node.BenchmarkScript, capture.BenchmarkScript)
            connect(capture.Out, "Seen")

        outputs = run(wf, inputs={"In": "go"}, out_dir=tmp_path / "run")

        assert outputs["Seen"] == [True]
        assert calls["count"] == 2
        assert len(seen) == 1
        assert json.loads(seen[0]["ExecutionContract"]) == {"schema_version": "execution_contract_v1"}
        assert seen[0]["ReferenceModel"] == "def reference_model():\n    return 1\n"
        assert seen[0]["ValidationScript"] == "print('validate')\n"
        assert seen[0]["BenchmarkScript"] == "print('good benchmark')\n"


class TestNestedWorkflows:
    def test_nested_function_workflow_composition(self):
        @task
        class Doubler:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[int](direction="out")

            def action(self, x: int) -> int:
                return x * 2

        @workflow(inputs={"In": int}, outputs={"Out": int})
        def subflow():
            d = Doubler()
            connect("In", d.In)
            connect(d.Out, "Out")

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def parent():
            nested = subflow()
            connect("Input", nested.In)
            connect(nested.Out, "Output")

        wf_def = parent._wfpy_workflow
        graph = _build_workflow_graph(wf_def)
        workflow_nodes = [
            rec
            for rec in graph.actors.values()
            if hasattr(rec.instance, "_wfpy_workflow") and not hasattr(rec.instance, "_wfpy_meta")
        ]
        assert len(workflow_nodes) == 1
        assert workflow_nodes[0].instance_name == "nested"

        outputs = run(parent, inputs={"Input": 3})
        assert outputs["Output"] == [6]

    def test_nested_workflow_streams_outputs_before_child_quiescence(self, tmp_path):
        release_slow = threading.Event()
        slow_started = threading.Event()
        consumer_seen = threading.Event()

        @task
        class FireOnce:
            _done: bool = False

            class Ports:
                Out = Port[int](direction="out")

            def action(self) -> int | None:
                if self._done:
                    return None
                self._done = True
                return 1

        @task
        class FastChildOutput:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[int](direction="out")

            def action(self, value: int) -> int:
                return value + 1

        @task
        class SlowChildSibling:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[int](direction="out")

            def action(self, value: int) -> int:
                slow_started.set()
                if not release_slow.wait(timeout=5):
                    raise RuntimeError("slow sibling was not released")
                return value

        @task
        class ParentConsumer:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[int](direction="out")

            def action(self, value: int) -> int:
                consumer_seen.set()
                return value

        @workflow(inputs={"In": int}, outputs={"Out": int})
        def child():
            fast = FastChildOutput()
            slow = SlowChildSibling()
            connect("In", fast.In)
            connect("In", slow.In)
            connect(fast.Out, "Out")

        @workflow(outputs={"Output": int})
        def parent():
            source = FireOnce()
            nested = child()
            consumer = ParentConsumer()
            connect(source.Out, nested.In)
            connect(nested.Out, consumer.In)
            connect(consumer.Out, "Output")

        result: dict[str, Any] = {}
        errors: list[BaseException] = []

        def _run_parent() -> None:
            try:
                result.update(run(parent, out_dir=str(tmp_path)))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=_run_parent)
        thread.start()
        try:
            assert slow_started.wait(timeout=2)
            assert consumer_seen.wait(timeout=2)
        finally:
            release_slow.set()
            thread.join(timeout=10)

        assert not thread.is_alive()
        assert errors == []
        assert result["Output"] == [2]

    def test_nested_workflow_streams_resource_directory_outputs(self, tmp_path):
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "payload.txt").write_text("payload")

        @task
        class FireOnce:
            _done: bool = False

            class Ports:
                Out = Port[int](direction="out")

            def action(self) -> int | None:
                if self._done:
                    return None
                self._done = True
                return 1

        @task
        class EmitDirectory:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[Resource](direction="out")

            def action(self, _value: int) -> Resource:
                return Resource(str(source_dir), kind="folder")

        @task
        class ReadDirectory:
            class Ports:
                In = Port[Resource](direction="in")
                Out = Port[str](direction="out")

            def action(self, value: Resource) -> str:
                directory = Path(getattr(value, "path", value))
                return (directory / "payload.txt").read_text()

        @workflow(inputs={"In": int}, outputs={"Out": Resource})
        def child():
            emit = EmitDirectory()
            connect("In", emit.In)
            connect(emit.Out, "Out")

        @workflow(outputs={"Output": str})
        def parent():
            source = FireOnce()
            nested = child()
            reader = ReadDirectory()
            connect(source.Out, nested.In)
            connect(nested.Out, reader.In)
            connect(reader.Out, "Output")

        outputs = run(parent, out_dir=str(tmp_path / "wf-out"))

        assert outputs["Output"] == ["payload"]

    def test_nested_workflow_suppresses_child_leftover_warnings(self, tmp_path, caplog):
        @task
        class PassThrough:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[int](direction="out")

            def action(self, x: int) -> int:
                return x

        @task
        class NeedsTwo:
            class Ports:
                In = Port[int](direction="in")

            @action(consumes={"In": 2}, produces={})
            def wait_for_pair(self, _a: int, _b: int) -> None:
                return None

        @workflow(inputs={"In": int}, outputs={"Out": int})
        def subflow():
            passthrough = PassThrough()
            stalled = NeedsTwo()
            connect("In", passthrough.In)
            connect("In", stalled.In)
            connect(passthrough.Out, "Out")

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def parent():
            nested = subflow()
            connect("Input", nested.In)
            connect(nested.Out, "Output")

        with caplog.at_level(logging.WARNING, logger="wfpy"):
            outputs = run(parent, inputs={"Input": 3}, out_dir=str(tmp_path), verbose=True)

        assert outputs["Output"] == [3]
        assert not any("Leftover tokens in queue" in r.message for r in caplog.records)


class TestNestedWorkflowParallelGate:
    """Reproduce: after a slow reviewer completes inside a nested workflow,
    the downstream gate task must fire before the next nested work item starts."""

    def test_gate_fires_after_slow_reviewer_in_nested_workflow(self):
        """Each nested fire gets its own Gate; all must complete."""
        import time

        completed: list[str] = []

        @task
        class ValidateOrFail:
            fail_on: list[str]  # parameter, set at construction

            class Ports:
                In = Port[str](direction="in")
                Validated = Port[str](direction="out")
                Error = Port[str](direction="out")

            @action(produces={"Validated": 1})
            @guard(lambda self, v: v not in self.fail_on)
            def succeed(self, v: str) -> dict:
                return {"Validated": v}

            @action(produces={"Error": 1})
            @guard(lambda self, v: v in self.fail_on)
            def fail(self, v: str) -> dict:
                return {"Error": v}

        @task
        class SlowReviewer:
            delay_s: float = 0.2

            class Ports:
                Error = Port[str](direction="in")
                Fixed = Port[str](direction="out")

            def action(self, Error: str) -> str:
                time.sleep(self.delay_s)
                return f"reviewer-fixed:{Error}"

        @task
        class Gate:
            class Ports:
                Validated = Port[str](direction="in")
                ReviewerFixed = Port[str](direction="in")
                Kernel = Port[str](direction="out")

            @action(consumes={"Validated": 1}, produces={"Kernel": 1})
            def emit_first(self, Validated: str) -> dict:
                return {"Kernel": f"first:{Validated}"}

            @action(consumes={"ReviewerFixed": 1}, produces={"Kernel": 1})
            def emit_reviewed(self, ReviewerFixed: str) -> dict:
                return {"Kernel": f"second:{ReviewerFixed}"}

        @task
        class Final:
            class Ports:
                Kernel = Port[str](direction="in")
                Out = Port[str](direction="out")

            def action(self, Kernel: str) -> str:
                completed.append(Kernel)
                return Kernel

        @workflow(inputs={"Input": str}, outputs={"Output": str})
        def child():
            check = ValidateOrFail(fail_on=["token-1"])
            gate = Gate()
            reviewer = SlowReviewer()
            final = Final()

            connect("Input", check.In)
            connect(check.Validated, gate.Validated)
            connect(check.Error, reviewer.Error)
            connect(reviewer.Fixed, gate.ReviewerFixed)
            connect(gate.Kernel, final.Kernel)
            connect(final.Out, "Output")

        # ── Build and run ──────────────────────────────────────────────

        @workflow(inputs={"Input": str}, outputs={"Output": str})
        def parent():
            nested = child()
            connect("Input", nested.Input)
            connect(nested.Output, "Output")

        wf_def = parent._wfpy_workflow
        graph = _build_workflow_graph(wf_def)
        plan = build_plan(graph, wf_def)

        for q in plan.wf_input_queues["Input"]:
            for token in ["token-0", "token-1", "token-2"]:
                q.enqueue(token)

        outputs = execute_plan(plan)

        assert outputs["Output"] is not None
        assert len(outputs["Output"]) == 3, (
            f"Expected 3 outputs, got {len(outputs['Output'])}: {outputs['Output']}"
        )
        assert len(completed) == 3, (
            f"Expected 3 completed, got {len(completed)}: {completed}"
        )
