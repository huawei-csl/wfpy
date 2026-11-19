"""What the shipped skills say wfpy does, run.

Each test is a sentence of one of the skills in `src/wfpy/skills`, as a small
workflow. The skills are about what fails without raising, so a wfpy that
stops behaving as written fails nothing else. A test that fails here means a
skill is wrong now: change the skill in the commit that changes the behaviour.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from wfpy import File, Port, action, agent, connect, guard, run, source, task, tool, workflow
from wfpy.cli import _load_module
from wfpy.graph import export_graph_json
from wfpy.runner import _build_workflow_graph


def leftovers(out_dir: Path, run_id: str) -> list[dict[str, Any]]:
    trace = json.loads((out_dir / run_id / "run.wf-queues.json").read_text(encoding="utf-8"))
    return list(trace["leftovers"])


@task
class Upper:
    """One token in, one out: the node the wiring tests put around a case."""

    tag: str

    class Ports:
        In = Port[object](direction="in")
        Out = Port[str](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def go(self, value: object) -> str:
        return f"{self.tag}:{value!r}"


# ── wfpy-actor-authoring ───────────────────────────────────────────────────


class TestFields:
    def test_a_field_with_a_default_is_state_not_a_parameter(self):
        @task
        class Prepare:
            run_mode: str
            timeout_s: int = 900

            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        with pytest.raises(
            TypeError, match=r"Prepare\(\) got unexpected parameter\(s\): 'timeout_s'"
        ):
            Prepare(run_mode="sim", timeout_s=5)
        with pytest.raises(TypeError, match=r"Prepare\(\) missing required parameter 'run_mode'"):
            Prepare()

    def test_a_mutable_default_is_copied_for_each_node(self, tmp_path):
        @task
        class History:
            _history: list = []

            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

            @action(consumes={"In": 1}, produces={"Out": 1})
            def go(self, value: str) -> str:
                self._history.append(value)
                return ",".join(self._history)

        @workflow(inputs={"A": str, "B": str}, outputs={"OutA": str, "OutB": str})
        def wf() -> None:
            a, b = History(), History()
            connect("A", a.In)
            connect("B", b.In)
            connect(a.Out, "OutA")
            connect(b.Out, "OutB")

        assert run(wf, {"A": "a", "B": "b"}, out_dir=tmp_path) == {"OutA": ["a"], "OutB": ["b"]}


@dataclass(frozen=True)
class JobSpec:
    kernel: str


class TestWhatAnActionReturns:
    def one_output(self, tmp_path: Path, returned: Any) -> Any:
        @task
        class Prepare:
            class Ports:
                Kernel = Port[str](direction="in")
                Job = Port[object](direction="out")

            @action(consumes={"Kernel": 1}, produces={"Job": 1})
            def prepare(self, kernel: str) -> Any:
                return returned(kernel)

        @workflow(inputs={"Kernel": str}, outputs={"Out": str})
        def wf() -> None:
            prepare, sink = Prepare(), Upper(tag="got")
            connect("Kernel", prepare.Kernel)
            connect(prepare.Job, sink.In)
            connect(sink.Out, "Out")

        return run(wf, {"Kernel": "k"}, out_dir=tmp_path)["Out"]

    def test_a_dict_without_the_port_emits_nothing_and_raises_nothing(self, tmp_path):
        assert self.one_output(tmp_path, lambda k: {"kernel": k, "name": "x"}) is None

    def test_the_three_shapes_that_emit(self, tmp_path):
        assert self.one_output(tmp_path / "a", JobSpec) == ["got:JobSpec(kernel='k')"]
        assert self.one_output(tmp_path / "b", lambda k: {"Job": JobSpec(k)}) == [
            "got:JobSpec(kernel='k')"
        ]
        assert self.one_output(tmp_path / "c", lambda k: f"/x/{k}") == ["got:'/x/k'"]

    def two_outputs(self, tmp_path: Path, returned: Any) -> Any:
        @task
        class Split:
            class Ports:
                In = Port[str](direction="in")
                A = Port[str](direction="out")
                B = Port[str](direction="out")

            @action(consumes={"In": 1}, produces={"A": 1, "B": 1})
            def go(self, value: str) -> Any:
                return returned(value)

        @task
        class Join:
            class Ports:
                A = Port[str](direction="in")
                B = Port[str](direction="in")
                Out = Port[str](direction="out")

            @action(consumes={"A": 1, "B": 1}, produces={"Out": 1})
            def go(self, a: str, b: str) -> str:
                return f"{a}+{b}"

        @workflow(inputs={"In": str}, outputs={"Out": str})
        def wf() -> None:
            split, join = Split(), Join()
            connect("In", split.In)
            connect(split.A, join.A)
            connect(split.B, join.B)
            connect(join.Out, "Out")

        return run(wf, {"In": "k"}, out_dir=tmp_path)["Out"]

    def test_a_port_left_out_of_the_dict_leaves_its_consumer_waiting(self, tmp_path):
        assert self.two_outputs(tmp_path / "both", lambda v: {"A": v, "B": v.upper()}) == ["k+K"]
        assert self.two_outputs(tmp_path / "one", lambda v: {"A": v}) is None

    def test_a_tuple_goes_by_position_and_another_value_to_every_port(self, tmp_path):
        assert self.two_outputs(tmp_path / "tuple", lambda v: (v, v.upper())) == ["k+K"]
        assert self.two_outputs(tmp_path / "plain", lambda v: v) == ["k+k"]

    def test_one_firing_is_one_token_whatever_produces_counts(self, tmp_path):
        @task
        class Emit:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[object](direction="out")

            @action(consumes={"In": 1}, produces={"Out": 2})
            def go(self, value: str) -> list[str]:
                return [value, value + "2"]

        @workflow(inputs={"In": str}, outputs={"O1": str, "O2": str})
        def wf() -> None:
            emit, one, two = Emit(), Upper(tag="a"), Upper(tag="b")
            connect("In", emit.In)
            connect(emit.Out, one.In)  # an output port broadcasts
            connect(emit.Out, two.In)
            connect(one.Out, "O1")
            connect(two.Out, "O2")

        assert run(wf, {"In": "k"}, out_dir=tmp_path) == {
            "O1": ["a:['k', 'k2']"],
            "O2": ["b:['k', 'k2']"],
        }


class TestPorts:
    def test_a_port_with_no_direction_is_classified_by_its_name(self):
        @task
        class Guess:
            class Ports:
                Kernel = Port[str]()
                Report = Port[str]()
                Verdict = Port[str]()

        meta = Guess._wfpy_meta
        assert set(meta.input_ports) == {"Kernel", "Verdict"}
        assert set(meta.output_ports) == {"Report"}

    def test_a_returned_path_keeps_its_suffix_and_ext_names_one_that_has_none(self, tmp_path):
        made = tmp_path / "made"
        made.mkdir()
        seen: dict[str, list[str]] = {}

        @task
        class Writer:
            class Ports:
                In = Port[str](direction="in")
                Bare = Port[File](direction="out", ext=".md")
                Suffixed = Port[File](direction="out", ext=".md")
                NoExt = Port[File](direction="out")

            @action(consumes={"In": 1}, produces={"Bare": 1, "Suffixed": 1, "NoExt": 1})
            def go(self, text: str) -> dict[str, str]:
                for name in ("a", "b.txt", "c"):
                    (made / name).write_text(text, encoding="utf-8")
                return {
                    "Bare": str(made / "a"),
                    "Suffixed": str(made / "b.txt"),
                    "NoExt": str(made / "c"),
                }

        @task
        class Names:
            class Ports:
                A = Port[File](direction="in")
                B = Port[File](direction="in")
                C = Port[File](direction="in")
                Out = Port[str](direction="out")

            @action(consumes={"A": 1, "B": 1, "C": 1}, produces={"Out": 1})
            def go(self, a: object, b: object, c: object) -> str:
                seen["names"] = [Path(str(token)).name for token in (a, b, c)]
                return "seen"

        @workflow(inputs={"In": str}, outputs={"Out": str})
        def wf() -> None:
            writer, names = Writer(), Names()
            connect("In", writer.In)
            connect(writer.Bare, names.A)
            connect(writer.Suffixed, names.B)
            connect(writer.NoExt, names.C)
            connect(names.Out, "Out")

        run(wf, {"In": "text"}, out_dir=tmp_path / "out")
        assert seen["names"] == [
            "writer__Bare__0.md",
            "writer__Suffixed__0.txt",
            "writer__NoExt__0",
        ]

    @pytest.mark.parametrize(
        ("boundary", "name"), [(File, "flow__Report__0"), (File(ext=".md"), "flow__Report__0.md")]
    )
    def test_a_boundary_output_takes_its_suffix_from_its_own_ext_alone(
        self, tmp_path, boundary, name
    ):
        made = tmp_path / "made"
        made.mkdir()
        seen: dict[str, str] = {}

        @task
        class Writer:
            class Ports:
                In = Port[str](direction="in")
                Report = Port[File](direction="out", ext=".md")

            @action(consumes={"In": 1}, produces={"Report": 1})
            def go(self, text: str) -> str:
                (made / "report.md").write_text(text, encoding="utf-8")
                return str(made / "report.md")

        @task
        class See:
            class Ports:
                Report = Port[File](direction="in")
                Out = Port[str](direction="out")

            @action(consumes={"Report": 1}, produces={"Out": 1})
            def go(self, report: object) -> str:
                seen["name"] = Path(str(report)).name
                return "seen"

        @workflow(inputs={"Kernel": str}, outputs={"Report": boundary})
        def inner() -> None:
            writer = Writer()
            connect("Kernel", writer.In)
            connect(writer.Report, "Report")

        @workflow(inputs={"Kernel": str}, outputs={"Out": str})
        def top() -> None:
            flow, see = inner(), See()
            connect("Kernel", flow.Kernel)
            connect(flow.Report, see.Report)
            connect(see.Out, "Out")

        run(top, {"Kernel": "k"}, out_dir=tmp_path / "out")
        assert seen["name"] == name

    def test_a_file_is_made_from_text_not_from_a_path(self):
        with pytest.raises(AttributeError, match="startswith"):
            File(Path("/x/kernel.txt"))  # type: ignore[arg-type]
        assert str(File(str(Path("/x/kernel.txt")))) == "/x/kernel.txt"


class TestGuards:
    def route(self, tmp_path: Path, cls: type) -> Any:
        @workflow(inputs={"In": int}, outputs={"Out": str})
        def wf() -> None:
            node = cls()
            connect("In", node.In)
            connect(node.Out, "Out")

        return run(wf, {"In": 7}, out_dir=tmp_path)["Out"]

    def test_the_first_action_declared_that_can_fire_does(self, tmp_path):
        @task
        class Route:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[str](direction="out")

            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, value: True)
            def first(self, value: int) -> str:
                return "first"

            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, value: True)
            def second(self, value: int) -> str:
                return "second"

        assert self.route(tmp_path, Route) == ["first"]

    def test_an_unguarded_action_shadows_the_guarded_one_below_it(self, tmp_path):
        @task
        class Route:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[str](direction="out")

            @action(consumes={"In": 1}, produces={"Out": 1})
            def anything(self, value: int) -> str:
                return "anything"

            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, value: value == 7)
            def seven(self, value: int) -> str:
                return "seven"

        assert self.route(tmp_path, Route) == ["anything"]

    def test_a_guard_that_raises_counts_as_false_and_the_run_ends_without_an_error(self, tmp_path):
        @task
        class Typo:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[str](direction="out")

            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, value: value.okay)
            def go(self, value: int) -> str:
                return "went"

        assert self.route(tmp_path, Typo) is None

    def test_a_guard_is_called_again_while_its_token_waits(self, tmp_path):
        calls: list[str] = []

        @task
        class Gate:
            _open: bool = False

            class Ports:
                Data = Port[str](direction="in")
                Key = Port[str](direction="in")
                Out = Port[str](direction="out")

            @action(consumes={"Data": 1}, produces={"Out": 1})
            @guard(lambda self, data: bool(calls.append("test")) or self._open)
            def passes(self, data: str) -> str:
                return data

            @action(consumes={"Key": 1})
            def unlock(self, key: str) -> None:
                self._open = True

        @workflow(inputs={"Data": str, "Key": str}, outputs={"Out": str})
        def wf() -> None:
            gate, late = Gate(), Upper(tag="late")
            connect("Data", gate.Data)
            connect("Key", late.In)
            connect(late.Out, gate.Key)
            connect(gate.Out, "Out")

        assert run(wf, {"Data": "d", "Key": "k"}, out_dir=tmp_path)["Out"] == ["d"]
        assert len(calls) > 1


@dataclass(frozen=True)
class Outcome:
    kernel: str
    ok: bool


class TestSchedulesAndPriorities:
    """The two nodes of the skill's section on which action fires, and what a
    schedule and a priority do not check."""

    def loop(self, tmp_path: Path, budget: int) -> Any:
        @task
        class RunKernel:
            class Ports:
                Initial = Port[str](direction="in")
                Retry = Port[str](direction="in")
                Result = Port[Outcome](direction="out")

            class Schedule:
                initial = "fresh"
                transitions = [
                    ("fresh", "from_initial", "looping"),
                    ("looping", "from_retry", "looping"),
                ]

            def _run(self, kernel: str) -> Outcome:
                return Outcome(kernel, ok=kernel.endswith("v3"))

            # The retry is declared first: the schedule decides, not the order.
            @action(consumes={"Retry": 1}, produces={"Result": 1})
            def from_retry(self, kernel: str) -> Outcome:
                return self._run(kernel)

            @action(consumes={"Initial": 1}, produces={"Result": 1})
            def from_initial(self, kernel: str) -> Outcome:
                return self._run(kernel)

        @task
        class Decide:
            budget: int
            _rounds: int = 0

            class Ports:
                Result = Port[Outcome](direction="in")
                Retry = Port[str](direction="out")
                Done = Port[str](direction="out")

            class Schedule:
                initial = "deciding"
                transitions = [
                    ("deciding", "accept", "done"),
                    ("deciding", "ask_again", "deciding"),
                    ("deciding", "give_up", "done"),
                ]

            class Priority:
                rules = [["accept", "ask_again", "give_up"]]

            # Declared from low to high: the priority decides, not the order.
            @action(consumes={"Result": 1}, produces={"Done": 1})
            def give_up(self, result: Outcome) -> str:
                return f"gave up on {result.kernel}"

            @action(consumes={"Result": 1}, produces={"Retry": 1})
            @guard(lambda self, result: self._rounds < self.budget)
            def ask_again(self, result: Outcome) -> str:
                self._rounds += 1
                return f"k:v{self._rounds + 1}"

            @action(consumes={"Result": 1}, produces={"Done": 1})
            @guard(lambda self, result: result.ok)
            def accept(self, result: Outcome) -> str:
                return f"accepted {result.kernel}"

        @workflow(inputs={"Kernel": str}, outputs={"Out": str})
        def wf() -> None:
            runner, decide = RunKernel(), Decide(budget=budget)
            connect("Kernel", runner.Initial)
            connect(runner.Result, decide.Result)
            connect(decide.Retry, runner.Retry)
            connect(decide.Done, "Out")

        return run(wf, {"Kernel": "k:v1"}, out_dir=tmp_path)["Out"]

    def test_a_loop_whose_order_is_said_by_a_schedule_and_a_priority(self, tmp_path):
        assert self.loop(tmp_path / "accepts", budget=5) == ["accepted k:v3"]
        # The action with no guard is the default: it fires when no other can.
        assert self.loop(tmp_path / "gives-up", budget=1) == ["gave up on k:v2"]

    def scheduled(self, rows: list[tuple[str, str, str]], first: str = "ready") -> type:
        @task
        class Scheduled:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[str](direction="out")

            class Schedule:
                initial = first
                transitions = rows

            @action(consumes={"In": 1}, produces={"Out": 1})
            @guard(lambda self, value: value != 0)
            def go(self, value: int) -> str:
                return f"went:{value}"

            @action(consumes={"In": 1}, produces={"Out": 1})
            def other(self, value: int) -> str:
                return f"other:{value}"

        return Scheduled

    def tokens(self, cls: type, values: list[int]) -> tuple[Any, str]:
        from wfpy.runner import build_plan, execute_plan

        @workflow(inputs={"In": int}, outputs={"Out": str})
        def wf() -> None:
            node = cls()
            connect("In", node.In)
            connect(node.Out, "Out")

        definition = wf._wfpy_workflow
        plan = build_plan(_build_workflow_graph(definition), definition)
        for queue in plan.wf_input_queues["In"]:
            for value in values:
                queue.enqueue(value)
        outputs = execute_plan(plan)
        node = next(actor for actor in plan.actors if actor.name == "node")
        return outputs["Out"], node.instance._wfpy_schedule_state

    def test_an_action_in_no_transition_never_fires(self):
        cls = self.scheduled([("ready", "go", "ready")])
        # 0 is a token only `other` takes, and `other` is in no transition.
        assert self.tokens(cls, [0]) == (None, "ready")
        assert self.tokens(cls, [1, 2]) == (["went:1", "went:2"], "ready")

    def test_a_next_state_spelled_wrong_ends_the_task_without_a_word(self):
        cls = self.scheduled([("ready", "go", "raedy")])
        assert self.tokens(cls, [1, 2, 3]) == (["went:1"], "raedy")

    def test_an_initial_state_with_no_transition_is_accepted_and_never_fires(self):
        cls = self.scheduled([("ready", "go", "ready")], first="nowhere")
        assert self.tokens(cls, [1]) == (None, "nowhere")

    @pytest.mark.parametrize(
        ("transitions", "message"),
        [
            ([("ready", "nope", "ready")], "Schedule references unknown action 'nope'"),
            ([("ready", "go", "a"), ("ready", "go", "b")], "Schedule has duplicate transition"),
        ],
    )
    def test_what_a_schedule_refuses_when_the_class_is_made(self, transitions, message):
        with pytest.raises(TypeError, match=message):
            self.scheduled(transitions)

    def ranked(self, order: Any) -> type:
        @task
        class Ranked:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[str](direction="out")

            class Priority:
                rules = order

            @action(consumes={"In": 1}, produces={"Out": 1})
            def a(self, value: int) -> str:
                return "a"

            @action(consumes={"In": 1}, produces={"Out": 1})
            def b(self, value: int) -> str:
                return "b"

            @action(consumes={"In": 1}, produces={"Out": 1})
            def c(self, value: int) -> str:
                return "c"

        return Ranked

    def test_a_listed_action_outranks_an_unlisted_one_and_an_earlier_group_a_later(self, tmp_path):
        def fired(rules: Any, name: str) -> Any:
            cls = self.ranked(rules)

            @workflow(inputs={"In": int}, outputs={"Out": str})
            def wf() -> None:
                node = cls()
                connect("In", node.In)
                connect(node.Out, "Out")

            return run(wf, {"In": 1}, out_dir=tmp_path / name)["Out"]

        assert fired([["c"], ["b"]], "groups") == ["c"]  # `a` is declared first, and unlisted
        assert fired([["b", "c"]], "one-group") == ["b"]

    @pytest.mark.parametrize(
        ("rules", "message"),
        [
            ([["a", "zzz"]], "Priority references unknown action 'zzz'"),
            (["a", "b"], r"Priority\.rules\[0\] must be a list of action names"),
        ],
    )
    def test_what_a_priority_refuses_when_the_class_is_made(self, rules, message):
        with pytest.raises(TypeError, match=message):
            self.ranked(rules)

    def test_the_graph_carries_the_schedule_and_the_priority_of_a_node(self):
        @task
        class Decide:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[str](direction="out")

            class Schedule:
                initial = "deciding"
                transitions = [("deciding", "a", "done"), ("deciding", "b", "deciding")]

            class Priority:
                rules = [["a", "b"]]

            @action(consumes={"In": 1}, produces={"Out": 1})
            def a(self, value: int) -> str:
                return "a"

            @action(consumes={"In": 1}, produces={"Out": 1})
            def b(self, value: int) -> str:
                return "b"

        @workflow(inputs={"In": int}, outputs={"Out": str})
        def wf() -> None:
            node = Decide()
            connect("In", node.In)
            connect(node.Out, "Out")

        graph = export_graph_json(_build_workflow_graph(wf._wfpy_workflow))["graph"]
        meta = next(node for node in graph["nodes"] if node["label"] == "node")["meta"]
        assert meta["schedule"] == {
            "initial": "deciding",
            "transitions": [
                {"state": "deciding", "action": "a", "nextState": "done"},
                {"state": "deciding", "action": "b", "nextState": "deciding"},
            ],
        }
        assert meta["priority"] == {"rules": [["a", "b"]]}

    def test_a_schedule_is_the_latch_of_a_node_with_no_input(self, tmp_path):
        @task
        class Once:
            class Ports:
                Out = Port[str](direction="out")

            class Schedule:
                initial = "armed"
                transitions = [("armed", "emit", "spent")]

            @action(consumes={}, produces={"Out": 1})
            def emit(self) -> str:
                return "once"

        @workflow(inputs={}, outputs={"Out": str})
        def wf() -> None:
            connect(Once().Out, "Out")

        assert run(wf, {}, out_dir=tmp_path) == {"Out": ["once"]}


class TestTheOtherDecorators:
    def test_a_tool_that_exits_non_zero_ends_the_run(self, tmp_path):
        @tool(cmd="sh", args=["-c", "exit 3"])
        class Failing:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        @workflow(inputs={"In": str}, outputs={"Out": str})
        def wf() -> None:
            failing = Failing()
            connect("In", failing.In)
            connect(failing.Out, "Out")

        with pytest.raises(RuntimeError, match="exited with code 3"):
            run(wf, {"In": "x"}, out_dir=tmp_path)

    @pytest.mark.parametrize("coerce", [False, True])
    def test_a_source_emits_a_file_object(self, tmp_path, coerce):
        kernel = tmp_path / "kernel.txt"
        kernel.write_text("body", encoding="utf-8")

        @source
        class KernelSource:
            path: File

            class Ports:
                Out = Port[File](direction="out")

        @task
        class Reader:
            class Ports:
                Kernel = Port[File](direction="in")
                Text = Port[str](direction="out")

            @action(consumes={"Kernel": 1}, produces={"Text": 1})
            def read(self, token: Any) -> str:
                assert isinstance(token, File)
                return Path(str(token) if coerce else token).read_text(encoding="utf-8")

        @workflow(inputs={}, outputs={"Out": str})
        def wf() -> None:
            head, reader = KernelSource(path=File(str(kernel))), Reader()
            connect(head.Out, reader.Kernel)
            connect(reader.Text, "Out")

        if coerce:
            assert run(wf, {}, out_dir=tmp_path / "out") == {"Out": ["body"]}
        else:
            # "..., not 'File'" from Python 3.12, "..., not File" before it.
            with pytest.raises(TypeError, match=r"os\.PathLike object.*not '?File'?"):
                run(wf, {}, out_dir=tmp_path / "out")


# ── wfpy-graph-composition ─────────────────────────────────────────────────


class TestWiring:
    def test_a_second_producer_into_one_input_port_is_never_read(self, tmp_path):
        @workflow(inputs={"A": str, "B": str}, outputs={"Out": str})
        def wf() -> None:
            a, b, t = Upper(tag="a"), Upper(tag="b"), Upper(tag="t")
            connect("A", a.In)
            connect("B", b.In)
            connect(a.Out, t.In)
            connect(b.Out, t.In)
            connect(t.Out, "Out")

        result = run(wf, {"A": "x", "B": "y"}, out_dir=tmp_path, run_id="fan-in")
        assert result == {"Out": ["t:\"a:'x'\""]}
        assert leftovers(tmp_path, "fan-in") == [{"queueId": "b.Out-->t.In", "count": 1}]

    def test_a_run_in_which_nothing_fired_writes_no_queue_trace(self, tmp_path):
        @workflow(inputs={"In": str}, outputs={"Out": str})
        def wf() -> None:
            node = Upper(tag="a")
            connect("In", node.In)
            connect(node.Out, "Out")

        assert run(wf, {}, out_dir=tmp_path, run_id="nothing") == {"Out": None}
        assert (tmp_path / "nothing" / "run.wf-run.json").is_file()
        assert not (tmp_path / "nothing" / "run.wf-queues.json").exists()

    def test_a_task_that_consumes_nothing_does_not_stop(self, tmp_path):
        fired = 0

        @task
        class Tick:
            class Ports:
                Out = Port[str](direction="out")

            @action(consumes={}, produces={"Out": 1})
            def go(self) -> str:
                nonlocal fired
                fired += 1
                if fired > 20:
                    raise RuntimeError("still firing")
                return "tick"

        @workflow(inputs={}, outputs={"Out": str})
        def wf() -> None:
            connect(Tick().Out, "Out")

        with pytest.raises(RuntimeError, match="still firing"):
            run(wf, {}, out_dir=tmp_path)

    def test_the_graph_lists_the_boundary_ports_beside_the_nodes(self):
        @workflow(inputs={"Kernel": str}, outputs={"Out": str})
        def inner() -> None:
            node = Upper(tag="a")
            connect("Kernel", node.In)
            connect(node.Out, "Out")

        @workflow(inputs={"Kernel": str}, outputs={"Out": str})
        def top() -> None:
            flow = inner()
            connect("Kernel", flow.Kernel)
            connect(flow.Out, "Out")

        def kinds(wf: Any) -> list[tuple[str, str]]:
            graph = export_graph_json(_build_workflow_graph(wf._wfpy_workflow))["graph"]
            return sorted((node["kind"], node["label"]) for node in graph["nodes"])

        assert kinds(top) == [("wf-input", "Kernel"), ("wf-output", "Out"), ("workflow", "flow")]
        assert kinds(inner) == [("internal", "node"), ("wf-input", "Kernel"), ("wf-output", "Out")]


# ── wfpy-agent-nodes, on the mock transport ────────────────────────────────


class TestAgents:
    def test_an_agent_fires_only_with_a_token_on_every_input(self, tmp_path):
        @agent(prompt="Repair.", transport="mock", mock_outputs={"Patched": "NOT A PATCH"})
        class Repair:
            class Ports:
                Initial = Port[str](direction="in")
                Retry = Port[str](direction="in")
                Patched = Port[str](direction="out")

        @workflow(inputs={"A": str, "B": str}, outputs={"Out": str})
        def wf() -> None:
            repair = Repair()
            connect("A", repair.Initial)
            connect("B", repair.Retry)
            connect(repair.Patched, "Out")

        assert run(wf, {"A": "a"}, out_dir=tmp_path / "one") == {"Out": None}
        assert run(wf, {"A": "a", "B": "b"}, out_dir=tmp_path / "both") == {"Out": ["NOT A PATCH"]}

    def test_a_mock_port_left_out_looks_like_an_answer(self, tmp_path):
        @agent(prompt="Summarize.", transport="mock")
        class Summarizer:
            class Ports:
                In = Port[str](direction="in")
                Summary = Port[str](direction="out")
                Score = Port[int](direction="out")

        @workflow(inputs={"In": str}, outputs={"Summary": str, "Score": int})
        def wf() -> None:
            summarizer = Summarizer()
            connect("In", summarizer.In)
            connect(summarizer.Summary, "Summary")
            connect(summarizer.Score, "Score")

        assert run(wf, {"In": "x"}, out_dir=tmp_path) == {"Summary": ["mock:Summary"], "Score": [0]}

    def test_an_empty_reply_is_a_token_like_any_other(self, tmp_path):
        @agent(prompt="Candidate.", transport="mock", mock_outputs={"Candidate": ""})
        class Empty:
            class Ports:
                In = Port[str](direction="in")
                Candidate = Port[str](direction="out")

        @workflow(inputs={"In": str}, outputs={"Out": str})
        def wf() -> None:
            empty = Empty()
            connect("In", empty.In)
            connect(empty.Candidate, "Out")

        assert run(wf, {"In": "x"}, out_dir=tmp_path) == {"Out": [""]}

    def test_fireable_without_input_bounds_an_agent_that_has_an_input_port(self, tmp_path):
        @agent(
            prompt="Tick.", transport="mock", fireable_without_input=2, mock_outputs={"Out": "tick"}
        )
        class Ticker:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        @workflow(inputs={"In": str}, outputs={"Out": str})
        def wf() -> None:
            ticker = Ticker()
            connect("In", ticker.In)
            connect(ticker.Out, "Out")

        assert run(wf, {}, out_dir=tmp_path / "none") == {"Out": ["tick", "tick"]}
        assert run(wf, {"In": "x"}, out_dir=tmp_path / "one") == {"Out": ["tick"] * 3}

    def with_skill(self, tmp_path: Path, name: str) -> Any:
        @agent(prompt="Do it.", transport="mock", skill=name, mock_outputs={"Out": "x"})
        class Skilled:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str](direction="out")

        @workflow(inputs={"In": str}, outputs={"Out": str})
        def wf() -> None:
            skilled = Skilled()
            connect("In", skilled.In)
            connect(skilled.Out, "Out")

        return wf

    def test_a_skill_that_is_not_found_fails_when_the_agent_fires(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WF_SKILLS_ROOT", str(tmp_path / "skills"))
        wf = self.with_skill(tmp_path, "nowhere")  # the graph builds
        with pytest.raises(
            FileNotFoundError, match=r"Skill 'nowhere' was not found\. Searched roots: "
        ):
            run(wf, {"In": "x"}, out_dir=tmp_path / "out")

    def test_a_skill_name_is_one_folder_name(self, tmp_path):
        with pytest.raises(ValueError, match="Skill name must not include path separators"):
            run(self.with_skill(tmp_path, "parent/child"), {"In": "x"}, out_dir=tmp_path / "out")

    def test_a_reference_to_a_file_of_the_skill_is_made_absolute(self, tmp_path):
        from wfpy._agent_prompt_runtime import _rewrite_skill_body_paths

        skill = tmp_path / "demo"
        (skill / "references").mkdir(parents=True)
        (skill / "references" / "notes.md").write_text("notes", encoding="utf-8")
        body = (
            "Read [the notes](references/notes.md), `references/notes.md`, then `scripts/gone.sh`."
        )

        notes = skill.resolve() / "references" / "notes.md"
        assert _rewrite_skill_body_paths(body, skill) == (
            f"Read [the notes]({notes}), `{notes}`, then `scripts/gone.sh`."
        )


# ── wfpy-workflow-builder ──────────────────────────────────────────────────


class TestTheProjectLayout:
    @pytest.fixture()
    def project(self, tmp_path, monkeypatch):
        """`src/workflows/flow.py` importing `src/tasks/step.py`, as the layout
        has it. The folder names are this test's own: what a load puts in
        `sys.modules` stays there for the tests that follow."""
        src = tmp_path / "src"
        (src / "skl_tasks").mkdir(parents=True)
        (src / "skl_workflows").mkdir()
        (src / "skl_tasks" / "step.py").write_text("VALUE = 'step'\n", encoding="utf-8")
        (src / "skl_workflows" / "flow.py").write_text(
            "from skl_tasks.step import VALUE\n", encoding="utf-8"
        )
        (src / "top.py").write_text("from skl_tasks.step import VALUE\n", encoding="utf-8")
        monkeypatch.setattr(sys, "path", list(sys.path))
        yield src
        for name in [n for n in sys.modules if n.split(".")[0] in {"skl_tasks", "skl_workflows"}]:
            sys.modules.pop(name, None)
        sys.modules.pop("__wfpy_user_module__", None)

    def test_a_top_level_file_loads_with_no_package(self, project):
        assert _load_module(str(project / "top.py")).VALUE == "step"

    def test_a_file_below_src_loads_only_from_a_package(self, project):
        with pytest.raises(ModuleNotFoundError, match="No module named 'skl_tasks'"):
            _load_module(str(project / "skl_workflows" / "flow.py"))

        (project / "skl_workflows" / "__init__.py").write_text("", encoding="utf-8")
        assert _load_module(str(project / "skl_workflows" / "flow.py")).VALUE == "step"

    def test_a_file_the_command_loads_is_not_main(self, project):
        """So `main()` and the `__main__` block of a workflow file do not run
        under `wfpy run`, which is how the IDE starts one."""
        (project / "flow.py").write_text(
            "RAN_MAIN = False\nif __name__ == '__main__':\n    RAN_MAIN = True\n", encoding="utf-8"
        )
        module = _load_module(str(project / "flow.py"))
        assert module.__name__ == "__wfpy_user_module__"
        assert module.RAN_MAIN is False
