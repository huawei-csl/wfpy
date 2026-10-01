"""A firing that fails keeps its inputs.

Every actor kind takes its tokens before it runs, so a firing that raised had
lost them, and a run could not be resumed from where it stopped. Now each one
is given back to the head of its queue, in order, before the error propagates.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from wfpy import Port, action, agent, connect, guard, loop, task, tool, workflow
from wfpy.runner import Queue, _atomic_firing, _build_workflow_graph, build_plan, execute_plan


@task
class Count:
    """Emits 1, 2, 3, one per firing."""

    _n: int = 0

    class Ports:
        Out = Port[int](direction="out")

    @action(consumes={}, produces={"Out": 1})
    @guard(lambda self: self._n < 3)
    def emit(self) -> int:
        self._n += 1
        return self._n


def _plan(wf: Any) -> Any:
    wf_def = wf._wfpy_workflow
    return build_plan(_build_workflow_graph(wf_def), wf_def)


def _queued(plan: Any, actor_name: str, port: str) -> list[Any]:
    actor = next(a for a in plan.actors if a.name == actor_name)
    return list(actor.in_queues[port][0].items)


def _fail(plan: Any, tmp_path: Path, **kwargs: Any) -> None:
    with pytest.raises(Exception):
        execute_plan(plan, out_dir=tmp_path, max_workers=1, **kwargs)


def test_a_failed_action_gives_its_token_back_and_keeps_the_ones_before(tmp_path: Path) -> None:
    @task
    class FailsOnTwo:
        class Ports:
            In = Port[int](direction="in")
            Out = Port[int](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def go(self, x: int) -> int:
            if x == 2:
                raise ValueError("two")
            return x

    @workflow(outputs={"Out": int})
    def wf():
        c = Count()
        f = FailsOnTwo()
        connect(c.Out, f.In)
        connect(f.Out, "Out")

    plan = _plan(wf)
    _fail(plan, tmp_path)

    # 1 went through; 2 failed and is back at the head, ahead of 3.
    assert _queued(plan, "f", "In")[:2] == [2, 3]
    assert list(plan.wf_output_queues["Out"][0].items) == [1]


def test_a_multi_token_firing_gives_them_back_in_order(tmp_path: Path) -> None:
    @task
    class Pairs:
        class Ports:
            In = Port[int](direction="in")
            Out = Port[int](direction="out")

        @action(consumes={"In": 2}, produces={"Out": 1})
        def go(self, a: int, b: int) -> int:
            raise ValueError("no pairs")

    @workflow(outputs={"Out": int})
    def wf():
        c = Count()
        p = Pairs()
        connect(c.Out, p.In)
        connect(p.Out, "Out")

    plan = _plan(wf)
    _fail(plan, tmp_path)

    assert _queued(plan, "p", "In")[:2] == [1, 2]


def test_a_failed_tool_gives_its_token_back(tmp_path: Path) -> None:
    @tool(cmd=sys.executable, args=["-c", "import sys; sys.exit(3)", "{in.In}"])
    class Fails:
        class Ports:
            In = Port[str](direction="in")
            Out = Port[str](direction="out")

    @workflow(inputs={"In": str}, outputs={"Out": str})
    def wf():
        f = Fails()
        connect("In", f.In)
        connect(f.Out, "Out")

    plan = _plan(wf)
    _fail(plan, tmp_path, inputs={"In": "payload"})

    assert _queued(plan, "f", "In") == ["payload"]


def test_a_failed_agent_gives_its_token_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the model is down")

    monkeypatch.setattr("wfpy.runner._invoke_agent", boom)

    @agent(prompt="x", useSkill=False)
    class Writer:
        class Ports:
            In = Port[str](direction="in")
            Out = Port[str](direction="out")

    @workflow(inputs={"In": str}, outputs={"Out": str})
    def wf():
        w = Writer()
        connect("In", w.In)
        connect(w.Out, "Out")

    plan = _plan(wf)
    _fail(plan, tmp_path, inputs={"In": "brief"})

    assert _queued(plan, "w", "In") == ["brief"]


def test_a_failure_inside_a_child_keeps_the_token_in_the_child_only(tmp_path: Path) -> None:
    @task
    class Fails:
        class Ports:
            In = Port[int](direction="in")
            Out = Port[int](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def go(self, x: int) -> int:
            raise ValueError("inside")

    @workflow(inputs={"In": int}, outputs={"Out": int})
    def child():
        f = Fails()
        connect("In", f.In)
        connect(f.Out, "Out")

    @workflow(inputs={"In": int}, outputs={"Out": int})
    def parent():
        c = child()
        connect("In", c.In)
        connect(c.Out, "Out")

    plan = _plan(parent)
    _fail(plan, tmp_path, inputs={"In": 5})

    nested = next(a for a in plan.actors if a.name == "c")
    # Held once: by the child actor that failed, not also by the parent.
    assert _queued(nested.sub_plan, "f", "In") == [5]
    assert _queued(plan, "c", "In") == []


def test_a_successful_firing_keeps_what_it_took() -> None:
    q = Queue("q")
    for value in (1, 2, 3):
        q.enqueue(value)

    with _atomic_firing():
        assert q.dequeue() == 1
        assert q.try_dequeue(1) == [2]

    assert list(q.items) == [3]


def test_a_firing_that_fails_gives_back_across_queues() -> None:
    a, b = Queue("a"), Queue("b")
    for value in (1, 2):
        a.enqueue(value)
    b.enqueue("x")

    with pytest.raises(RuntimeError):
        with _atomic_firing():
            a.dequeue()
            b.dequeue()
            a.dequeue()
            a.enqueue(9)  # produced meanwhile, at the tail
            raise RuntimeError("fail")

    assert list(a.items) == [1, 2, 9]
    assert list(b.items) == ["x"]


def test_inside_a_loop_only_the_failed_firing_gives_back(tmp_path: Path) -> None:
    # A loop's firing runs its body: `a` succeeds on item 2, then `b` fails on
    # it. Only `b`'s token goes back; `a` consumed its own and produced from
    # it, so handing it back as well would run `a` on item 2 twice.
    @task
    class Pass:
        class Ports:
            In = Port[int](direction="in")
            Out = Port[int](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def go(self, x: int) -> int:
            return x

    @task
    class FailsOnTwo:
        class Ports:
            In = Port[int](direction="in")
            Out = Port[int](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def go(self, x: int) -> int:
            if x == 2:
                raise ValueError("two")
            return x

    @workflow(outputs={"Out": int})
    def wf():
        lp = loop([1, 2, 3])
        with lp:
            a = Pass()
            b = FailsOnTwo()
            connect(lp.item, a.In)
            connect(a.Out, b.In)
            connect(b.Out, "Out")

    plan = _plan(wf)
    _fail(plan, tmp_path)

    assert _queued(plan, "b", "In") == [2]
    assert _queued(plan, "a", "In") == []
    assert list(plan.wf_output_queues["Out"][0].items) == [1]
