"""The queue trace says which edge each queue is.

The IDE's stepper draws, at each step, every queue's size and last token on
its edge. It finds the edge the way it finds the run overlay's: by the queue's
source path or its endpoints. A trace entry without them has a size and a
token and no edge to show them on.
"""

from __future__ import annotations

import json
from pathlib import Path

from wfpy import Port, action, connect, guard, run, task, workflow


@task
class Count:
    _n: int = 0

    class Ports:
        Out = Port[int](direction="out")

    @action(consumes={}, produces={"Out": 1})
    @guard(lambda self: self._n < 2)
    def emit(self) -> int:
        self._n += 1
        return self._n


@task
class Double:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def go(self, x: int) -> int:
        return x * 2


@workflow(outputs={"Out": int})
def flow():
    c = Count()
    d = Double()
    connect(c.Out, d.In)
    connect(d.Out, "Out")


def test_each_queue_in_the_trace_names_the_edge_the_overlay_does(tmp_path: Path) -> None:
    run(flow, out_dir=str(tmp_path), run_id="r", max_workers=1)
    run_dir = tmp_path / "r"
    steps = json.loads((run_dir / "run.wf-queues.json").read_text())["steps"]
    overlay = json.loads((run_dir / "run.wf-viewer.json").read_text())["edges"]
    by_queue = {edge["queueId"]: edge for edge in overlay.values()}

    identity = ("fromEntity", "outPort", "toEntity", "inPort")
    for step in steps:
        for queue in step["queueSizes"]:
            edge = by_queue[queue["queueId"]]
            assert {k: queue.get(k) for k in identity} == {k: edge.get(k) for k in identity}

    first = {q["queueId"]: q for q in steps[0]["queueSizes"]}
    assert first["c.Out-->d.In"]["fromEntity"] == "c"
    assert first["c.Out-->d.In"]["toEntity"] == "d"
    # A queue into the workflow's own output has no entity at that end.
    assert "toEntity" not in first["d.Out-->WF.Out"]


def test_a_queue_no_token_has_reached_carries_no_last_token(tmp_path: Path) -> None:
    run(flow, out_dir=str(tmp_path), run_id="r", max_workers=1)
    steps = json.loads((tmp_path / "r" / "run.wf-queues.json").read_text())["steps"]

    first = {q["queueId"]: q for q in steps[0]["queueSizes"]}
    # After the first firing (Count), nothing has reached the output yet: the
    # stepper shows that edge as not carried, and must be able to tell.
    assert steps[0]["actorInstanceName"] == "c"
    assert "lastToken" not in first["d.Out-->WF.Out"]
    assert "lastToken" in first["c.Out-->d.In"]


@task
class Pairs:
    """Emits a different list three times: tokens that go to edge-tokens/ files.

    Not a dict: a dict returned for one output is read as ``{port: value}``.
    """

    _n: int = 0

    class Ports:
        Out = Port[list](direction="out")

    @action(consumes={}, produces={"Out": 1})
    @guard(lambda self: self._n < 3)
    def emit(self) -> list[int]:
        self._n += 1
        return [self._n]


@task
class Keep:
    class Ports:
        In = Port[list](direction="in")
        Out = Port[list](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def go(self, x: list[int]) -> list[int]:
        return x


@workflow(outputs={"Out": list})
def pairs():
    p = Pairs()
    k = Keep()
    connect(p.Out, k.In)
    connect(k.Out, "Out")


def test_the_last_token_at_each_step_is_the_token_then(tmp_path: Path) -> None:
    run(pairs, out_dir=str(tmp_path), run_id="r", max_workers=1)
    steps = json.loads((tmp_path / "r" / "run.wf-queues.json").read_text())["steps"]

    # What the stepper opens for the edge into Keep, step by step: each token
    # as it was when it was the last one, not the run's final token.
    seen = []
    for step in steps:
        queue = next(q for q in step["queueSizes"] if q["queueId"] == "p.Out-->k.In")
        content = json.loads(Path(queue["lastToken"]).read_text())
        if not seen or seen[-1] != content:
            seen.append(content)
    assert seen == [[1], [2], [3]]
