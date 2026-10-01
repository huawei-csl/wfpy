"""Resuming a run at any step of its queue trace (docs/proposals/resume.md, phase 3).

The run starts again from its inputs, and the firings the original run had
completed by the step are replayed from its journal rather than run. What
these check is that: replayed firings do not run, the ones after the step do,
and the outputs are those a clean run gives -- with parallel workers too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wfpy import Port, action, connect, guard, loop, run, task, workflow


class Calls:
    def __init__(self) -> None:
        self.broken = False
        self.by_actor: dict[str, int] = {}
        self.salt = 0

    def fired(self, name: str) -> None:
        self.by_actor[name] = self.by_actor.get(name, 0) + 1


def _pipeline(calls: Calls) -> Any:
    @task
    class Count:
        _n: int = 0

        class Ports:
            Out = Port[int](direction="out")

        @action(consumes={}, produces={"Out": 1})
        @guard(lambda self: self._n < 4)
        def emit(self) -> int:
            calls.fired("c")
            self._n += 1
            return self._n

    @task
    class Pass:
        class Ports:
            In = Port[int](direction="in")
            Out = Port[int](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def go(self, x: int) -> int:
            calls.fired("p")
            return x + calls.salt

    @task
    class Double:
        _seen: int = 0

        class Ports:
            In = Port[int](direction="in")
            Out = Port[int](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def go(self, x: int) -> int:
            if calls.broken and x == 3:
                raise ValueError("broken on 3")
            calls.fired("d")
            self._seen += 1
            return x * 2

    @workflow(outputs={"Out": int})
    def flow():
        c = Count()
        p = Pass()
        d = Double()
        connect(c.Out, p.In)
        connect(p.Out, d.In)
        connect(d.Out, "Out")

    return flow


def _trace(run_dir: Path) -> list[dict[str, Any]]:
    return json.loads((run_dir / "run.wf-queues.json").read_text())["steps"]


def _fired_by(steps: list[dict[str, Any]], step: int) -> dict[str, int]:
    fired: dict[str, int] = {}
    for entry in steps[:step]:
        name = entry["actorInstanceName"]
        fired[name] = fired.get(name, 0) + 1
    return fired


@pytest.mark.parametrize("workers", [1, None])
def test_resuming_at_each_step_replays_what_came_before(
    tmp_path: Path, workers: int | None
) -> None:
    calls = Calls()
    flow = _pipeline(calls)
    out = tmp_path / "wf-out"
    assert run(flow, out_dir=str(out), run_id="first", max_workers=workers)["Out"] == [2, 4, 6, 8]
    steps = _trace(out / "first")
    totals = dict(calls.by_actor)

    for step in range(len(steps) + 1):
        calls.by_actor.clear()
        outputs = run(
            flow,
            out_dir=str(out),
            run_id=f"at{step}",
            resume_from=str(out / "first"),
            at_step=step,
            max_workers=workers,
        )
        assert outputs["Out"] == [2, 4, 6, 8], step
        before = _fired_by(steps, step)
        # What fired by the step is replayed; only what came after runs.
        assert calls.by_actor == {
            name: totals[name] - before.get(name, 0)
            for name in totals
            if totals[name] - before.get(name, 0)
        }, step
        record = json.loads((out / f"at{step}" / "run.wf-run.json").read_text())
        assert record["resumedFrom"] == "first"
        assert record["resumedAtStep"] == step
        assert "replayStopped" not in record


def test_a_failed_run_is_resumed_at_its_last_step(tmp_path: Path) -> None:
    calls = Calls()
    calls.broken = True
    flow = _pipeline(calls)
    out = tmp_path / "wf-out"
    with pytest.raises(ValueError, match="broken on 3"):
        run(flow, out_dir=str(out), run_id="first", max_workers=1)
    steps = _trace(out / "first")  # written although the run failed
    first_calls = dict(calls.by_actor)

    calls.broken = False  # the fix
    calls.by_actor.clear()
    outputs = run(
        flow, out_dir=str(out), run_id="second", resume_from=str(out / "first"), at_step=len(steps)
    )

    assert outputs["Out"] == [2, 4, 6, 8]
    total = {
        name: first_calls.get(name, 0) + calls.by_actor.get(name, 0) for name in ("c", "p", "d")
    }
    assert total == {"c": 4, "p": 4, "d": 4}  # none twice


def test_replayed_steps_are_marked_in_the_new_trace(tmp_path: Path) -> None:
    calls = Calls()
    flow = _pipeline(calls)
    out = tmp_path / "wf-out"
    run(flow, out_dir=str(out), run_id="first", max_workers=1)

    run(
        flow,
        out_dir=str(out),
        run_id="second",
        resume_from=str(out / "first"),
        at_step=3,
        max_workers=1,
    )

    marks = [bool(step.get("replayed")) for step in _trace(out / "second")]
    assert marks[:3] == [True, True, True]
    assert not any(marks[3:])


def test_a_resumed_run_can_itself_be_resumed_at_a_step(tmp_path: Path) -> None:
    calls = Calls()
    flow = _pipeline(calls)
    out = tmp_path / "wf-out"
    run(flow, out_dir=str(out), run_id="first", max_workers=1)
    run(
        flow,
        out_dir=str(out),
        run_id="second",
        resume_from=str(out / "first"),
        at_step=3,
        max_workers=1,
    )

    calls.by_actor.clear()
    outputs = run(
        flow,
        out_dir=str(out),
        run_id="third",
        resume_from=str(out / "second"),
        at_step=5,
        max_workers=1,
    )

    assert outputs["Out"] == [2, 4, 6, 8]
    assert sum(calls.by_actor.values()) == len(_trace(out / "first")) - 5


def test_replayed_firings_keep_their_recorded_answers(tmp_path: Path) -> None:
    calls = Calls()
    flow = _pipeline(calls)
    out = tmp_path / "wf-out"
    run(flow, out_dir=str(out), run_id="first", max_workers=1)
    last = len(_trace(out / "first"))

    calls.salt = 100  # Pass now answers differently, as an agent might
    calls.by_actor.clear()
    outputs = run(
        flow, out_dir=str(out), run_id="second", resume_from=str(out / "first"), at_step=last
    )

    # Every firing is before the step, so every one is replayed with the
    # answer it gave then: resuming at a step keeps what happened before it.
    assert outputs["Out"] == [2, 4, 6, 8]
    assert calls.by_actor == {}
    record = json.loads((out / "second" / "run.wf-run.json").read_text())
    assert "replayStopped" not in record

    # At step 0 nothing is replayed: everything runs, and takes the new answers.
    outputs = run(flow, out_dir=str(out), run_id="third", resume_from=str(out / "first"), at_step=0)
    assert outputs["Out"] == [202, 204, 206, 208]


def test_replay_stops_when_inputs_differ(tmp_path: Path) -> None:
    calls = Calls()
    flow = _pipeline(calls)
    out = tmp_path / "wf-out"
    run(flow, out_dir=str(out), run_id="first", max_workers=1)
    steps = _trace(out / "first")

    # Pass's firings after its first are not in the journal, so they run live,
    # and now answer differently (as an agent might). Double's second recorded
    # firing took Pass's old answer: its inputs no longer match, and replay
    # stops there rather than replaying a firing on inputs it never had.
    journal = out / "first" / "run.wf-journal.jsonl"
    lines = journal.read_text().splitlines()
    seen_pass = 0
    kept = []
    for line in lines:
        entry = json.loads(line)
        if entry.get("actor") == "p":
            seen_pass += 1
            if seen_pass > 1:
                continue
        kept.append(line)
    journal.write_text("\n".join(kept) + "\n")

    calls.salt = 100
    outputs = run(
        flow,
        out_dir=str(out),
        run_id="second",
        resume_from=str(out / "first"),
        at_step=len(steps),
        max_workers=1,
    )

    record = json.loads((out / "second" / "run.wf-run.json").read_text())
    assert "inputs differ" in record["replayStopped"]
    assert record["replayStopped"].startswith("d ")
    assert outputs["Out"] == [2, 204, 206, 208]


def test_a_loop_and_a_child_workflow_are_resumed_at_a_step(tmp_path: Path) -> None:
    calls = Calls()

    @task
    class Pass:
        class Ports:
            In = Port[int](direction="in")
            Out = Port[int](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def go(self, x: int) -> int:
            calls.fired("p")
            return x * 10

    @workflow(inputs={"In": int}, outputs={"Out": int})
    def child():
        p = Pass()
        connect("In", p.In)
        connect(p.Out, "Out")

    @workflow(outputs={"Out": int})
    def flow():
        lp = loop([1, 2, 3])
        with lp:
            ch = child()
            connect(lp.item, ch.In)
            connect(ch.Out, "Out")

    out = tmp_path / "wf-out"
    assert run(flow, out_dir=str(out), run_id="first")["Out"] == [10, 20, 30]
    steps = _trace(out / "first")

    for step in range(len(steps) + 1):
        calls.by_actor.clear()
        outputs = run(
            flow, out_dir=str(out), run_id=f"at{step}", resume_from=str(out / "first"), at_step=step
        )
        assert outputs["Out"] == [10, 20, 30], step
    calls.by_actor.clear()
    run(flow, out_dir=str(out), run_id="last", resume_from=str(out / "first"), at_step=len(steps))
    assert calls.by_actor == {}  # every firing replayed


def test_a_run_resumed_from_its_checkpoint_is_not_resumed_at_a_step(tmp_path: Path) -> None:
    calls = Calls()
    calls.broken = True
    flow = _pipeline(calls)
    out = tmp_path / "wf-out"
    with pytest.raises(ValueError):
        run(flow, out_dir=str(out), run_id="first")
    calls.broken = False
    run(flow, out_dir=str(out), run_id="second", resume_from=str(out / "first"))

    with pytest.raises(ValueError, match="resume at a step of that run instead"):
        run(flow, out_dir=str(out), resume_from=str(out / "second"), at_step=1)


def test_refusals(tmp_path: Path) -> None:
    calls = Calls()
    flow = _pipeline(calls)
    out = tmp_path / "wf-out"
    run(flow, out_dir=str(out), run_id="first")

    with pytest.raises(ValueError, match="needs resume_from"):
        run(flow, out_dir=str(out), at_step=1)
    with pytest.raises(ValueError, match="has no step 99"):
        run(flow, out_dir=str(out), resume_from=str(out / "first"), at_step=99)
    with pytest.raises(ValueError, match="takes no inputs"):
        run(flow, {"In": 1}, out_dir=str(out), resume_from=str(out / "first"), at_step=1)

    @workflow(outputs={"Out": int})
    def rewired():
        connect("Out", "Out")

    rewired._wfpy_workflow.name = "flow"  # the same workflow, wired differently
    with pytest.raises(ValueError, match="graph changed"):
        run(rewired, out_dir=str(out), resume_from=str(out / "first"), at_step=1)


def test_the_cli_resumes_at_a_step(tmp_path: Path) -> None:
    import subprocess
    import sys

    flow = tmp_path / "flow.py"
    flow.write_text(
        """
from pathlib import Path

from wfpy import Port, action, connect, guard, task, workflow

LOG = Path(__file__).parent / "fired.log"


@task
class Count:
    _n: int = 0

    class Ports:
        Out = Port[int](direction="out")

    @action(consumes={}, produces={"Out": 1})
    @guard(lambda self: self._n < 3)
    def emit(self) -> int:
        with open(LOG, "a") as log:
            log.write("count\\n")
        self._n += 1
        return self._n


@workflow(outputs={"Out": int})
def flow():
    c = Count()
    connect(c.Out, "Out")
"""
    )
    out = tmp_path / "wf-out"

    def wfpy(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", "from wfpy.cli import main; main()", "run", str(flow), *args],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

    assert wfpy("--out-dir", str(out), "--run-id", "first").returncode == 0
    resumed = wfpy(
        "--out-dir",
        str(out),
        "--run-id",
        "second",
        "--resume-from",
        str(out / "first"),
        "--at-step",
        "2",
    )
    assert resumed.returncode == 0, resumed.stderr

    record = json.loads((out / "second" / "run.wf-run.json").read_text())
    assert record["outputs"]["Out"] == [1, 2, 3]
    assert record["resumedAtStep"] == 2
    # Three in the first run; in the second only the third firing ran.
    assert (tmp_path / "fired.log").read_text().count("count") == 4
