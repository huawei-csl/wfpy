"""A run's error names the actor that failed, under every nested workflow.

An IDE navigating a hierarchy goes from the root to the failure by this path;
the error used to carry only the last actor the overlay saw become active --
no path, and with parallel workers not necessarily the one that failed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wfpy import Port, action, connect, guard, run, task, workflow


@task
class Two:
    _done: bool = False

    class Ports:
        Out = Port[int](direction="out")

    @action(consumes={}, produces={"Out": 1})
    @guard(lambda self: not self._done)
    def emit(self) -> int:
        self._done = True
        return 2


@task
class Pass:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def go(self, x: int) -> int:
        return x


@task
class Fails:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def go(self, x: int) -> int:
        raise ValueError("deep down")


@workflow(inputs={"In": int}, outputs={"Out": int})
def inner():
    p = Pass()
    x = Fails()
    connect("In", p.In)
    connect(p.Out, x.In)
    connect(x.Out, "Out")


@workflow(inputs={"In": int}, outputs={"Out": int})
def middle():
    i = inner()
    connect("In", i.In)
    connect(i.Out, "Out")


@workflow(outputs={"Out": int})
def top():
    src = Two()
    m = middle()
    connect(src.Out, m.In)
    connect(m.Out, "Out")


def _error(run_dir: Path, artifact: str) -> dict:
    return json.loads((run_dir / artifact).read_text())["error"]


def test_an_error_deep_in_the_hierarchy_names_its_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="deep down"):
        run(top, out_dir=str(tmp_path), run_id="r")

    for artifact in ("run.wf-run.json", "run.wf-viewer.json"):
        error = _error(tmp_path / "r", artifact)
        assert error["entityInstancePath"] == ["m", "i", "x"], artifact
        assert error["entityInstanceName"] == "x"
        assert "deep down" in error["message"]


def test_an_error_at_the_root_names_the_actor(tmp_path: Path) -> None:
    @workflow(outputs={"Out": int})
    def flat():
        src = Two()
        x = Fails()
        connect(src.Out, x.In)
        connect(x.Out, "Out")

    with pytest.raises(ValueError):
        run(flat, out_dir=str(tmp_path), run_id="r", max_workers=4)

    assert _error(tmp_path / "r", "run.wf-run.json")["entityInstancePath"] == ["x"]
