"""Each nested workflow's edges are its own in the run overlay and the queue trace.

Two instances of one workflow connect the same way, so their edges have the
same names (`p.Out-->WF.Out`), and so do two different workflows that happen
to wire the same actor names. The overlay keyed an edge by that name alone:
the instance merged last replaced the others, and the IDE showed, on every
edge of the first instance, the token of the second. Each entry now says
which workflow instance it is in (`instancePath`), and a nested edge's key
carries that path; the top level's keys are as before.
"""

from __future__ import annotations

import json
from pathlib import Path

from wfpy import Port, action, connect, guard, run, task, workflow


@task
class Left:
    _done: bool = False

    class Ports:
        Out = Port[str](direction="out")

    @action(consumes={}, produces={"Out": 1})
    @guard(lambda self: not self._done)
    def emit(self) -> str:
        self._done = True
        return "left"


@task
class Right:
    _done: bool = False

    class Ports:
        Out = Port[str](direction="out")

    @action(consumes={}, produces={"Out": 1})
    @guard(lambda self: not self._done)
    def emit(self) -> str:
        self._done = True
        return "right"


@task
class Pass:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def go(self, x: str) -> str:
        return x


@workflow(inputs={"In": str}, outputs={"Out": str})
def inner():
    p = Pass()
    connect("In", p.In)
    connect(p.Out, "Out")


@workflow(inputs={"In": str}, outputs={"Out": str})
def middle():
    i = inner()
    connect("In", i.In)
    connect(i.Out, "Out")


@workflow(outputs={"A": str, "B": str})
def top():
    left = Left()
    right = Right()
    a = middle()
    b = middle()
    connect(left.Out, a.In)
    connect(right.Out, b.In)
    connect(a.Out, "A")
    connect(b.Out, "B")


def test_each_instance_keeps_its_own_edges_in_the_overlay(tmp_path: Path) -> None:
    run(top, out_dir=str(tmp_path), run_id="r")
    edges = json.loads((tmp_path / "r" / "run.wf-viewer.json").read_text())["edges"]

    for name, word in (("a", "left"), ("b", "right")):
        inside = edges[f"queue:{name}/i/p.Out-->WF.Out"]
        assert inside["instancePath"] == [name, "i"]
        assert inside["queueId"] == "p.Out-->WF.Out"
        assert (inside["fromEntity"], inside["outPort"]) == ("p", "Out")
        assert inside["lastToken"] == word
        assert edges[f"queue:{name}/i.Out-->WF.Out"]["lastToken"] == word

    # The top level's edges keep their keys, and say no instance path.
    top_edge = edges["queue:left.Out-->a.In"]
    assert "instancePath" not in top_edge
    assert top_edge["lastToken"] == "left"
    # One entry per edge: nothing replaced another.
    assert sum(1 for e in edges.values() if e["queueId"] == "p.Out-->WF.Out") == 2


def test_each_instance_keeps_its_own_queues_in_the_trace(tmp_path: Path) -> None:
    run(top, out_dir=str(tmp_path), run_id="r", max_workers=1)
    steps = json.loads((tmp_path / "r" / "run.wf-queues.json").read_text())["steps"]

    last = [q for q in steps[-1]["queueSizes"] if q["queueId"] == "p.Out-->WF.Out"]
    assert sorted((q["instancePath"], q["lastToken"]) for q in last) == [
        (["a", "i"], "left"),
        (["b", "i"], "right"),
    ]
    assert all("instancePath" not in q for q in steps[-1]["queueSizes"] if q["scope"] == "top")
