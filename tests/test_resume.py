"""Resuming a run from where it failed (docs/proposals/resume.md, phase 2).

Each test breaks one actor, runs, fixes it -- the way a person fixes the code
-- and resumes. What it checks is the point of resuming: the firings before
the failure do not run again, and the outputs are those a clean run gives.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import decimal
import enum
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from wfpy import File, Port, Resource, action, connect, guard, if_, loop, run, task, workflow
from wfpy._checkpoint_runtime import NotResumable, decode, encode


@dataclasses.dataclass
class Point:
    x: int
    y: int


@dataclasses.dataclass(frozen=True)
class Frozen:
    name: str
    tags: tuple[str, ...] = ()


@dataclasses.dataclass
class Derived:
    base: int
    double: int = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.double = self.base * 2


class Plain:
    """An ordinary class: attributes, no dataclass, no saving hooks."""

    def __init__(self, label: str, children: list[Plain] | None = None) -> None:
        self.label = label
        self.children = children or []
        self.when = datetime.date(2026, 10, 1)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Plain) and vars(self) == vars(other)


class Slotted:
    __slots__ = ("a", "b")

    def __init__(self, a: int, b: str) -> None:
        self.a = a
        self.b = b

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Slotted) and (self.a, self.b) == (other.a, other.b)


class Hooked:
    """Says how it is saved itself: pickled, its own way."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.cache = object()  # not meant to survive

    def __getstate__(self) -> dict[str, int]:
        return {"n": self.n}

    def __setstate__(self, state: dict[str, int]) -> None:
        self.n = state["n"]
        self.cache = None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Hooked) and self.n == other.n


class Color(enum.Enum):
    RED = "red"


class Level(enum.IntEnum):
    HIGH = 3


Pair = collections.namedtuple("Pair", "left right")


class Broken:
    """Whether the failing actor is still broken, and who fired how often."""

    def __init__(self) -> None:
        self.on = True
        self.calls: dict[str, int] = {}

    def fired(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1


def _make_actors(broken: Broken) -> tuple[Any, Any, Any]:
    @task
    class Count:
        _n: int = 0

        class Ports:
            Out = Port[int](direction="out")

        @action(consumes={}, produces={"Out": 1})
        @guard(lambda self: self._n < 3)
        def emit(self) -> int:
            broken.fired("count")
            self._n += 1
            return self._n

    @task
    class Pass:
        class Ports:
            In = Port[int](direction="in")
            Out = Port[int](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def go(self, x: int) -> int:
            broken.fired("pass")
            return x

    @task
    class Double:
        class Ports:
            In = Port[int](direction="in")
            Out = Port[int](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def go(self, x: int) -> int:
            if broken.on and x == 2:
                raise ValueError("broken on 2")
            broken.fired("double")
            return x * 2

    return Count, Pass, Double


def _fail_then_resume(wf: Any, tmp_path: Path, broken: Broken) -> tuple[dict[str, Any], Path]:
    out = tmp_path / "wf-out"
    with pytest.raises(ValueError, match="broken on 2"):
        run(wf, out_dir=str(out), run_id="first")
    first = out / "first"
    checkpoint = json.loads((first / "run.wf-checkpoint.json").read_text())
    assert checkpoint["resumable"] is True
    assert "broken on 2" in checkpoint["failed"]["message"]

    broken.on = False  # the fix
    outputs = run(wf, out_dir=str(out), run_id="second", resume_from=str(first))
    return outputs, out / "second"


def test_a_failed_run_resumes_without_repeating_what_it_did(tmp_path: Path) -> None:
    broken = Broken()
    Count, Pass, Double = _make_actors(broken)

    @workflow(outputs={"Out": int})
    def wf():
        c = Count()
        p = Pass()
        d = Double()
        connect(c.Out, p.In)
        connect(p.Out, d.In)
        connect(d.Out, "Out")

    outputs, second = _fail_then_resume(wf, tmp_path, broken)

    assert outputs["Out"] == [2, 4, 6]
    # Each fired once per token over both runs: nothing before the failure ran again.
    assert broken.calls == {"count": 3, "pass": 3, "double": 3}
    record = json.loads((second / "run.wf-run.json").read_text())
    assert record["resumedFrom"] == "first"


def test_a_failure_in_a_loop_body_resumes_in_the_body(tmp_path: Path) -> None:
    broken = Broken()
    _Count, Pass, Double = _make_actors(broken)

    @workflow(outputs={"Out": int})
    def wf():
        lp = loop([1, 2, 3])
        with lp:
            p = Pass()
            d = Double()
            connect(lp.item, p.In)
            connect(p.Out, d.In)
            connect(d.Out, "Out")

    outputs, _ = _fail_then_resume(wf, tmp_path, broken)

    assert outputs["Out"] == [2, 4, 6]
    assert broken.calls == {"pass": 3, "double": 3}


def test_a_failure_in_an_if_branch_resumes_in_the_branch(tmp_path: Path) -> None:
    broken = Broken()
    _Count, Pass, Double = _make_actors(broken)

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

    @workflow(outputs={"Out": int})
    def wf():
        src = Two()
        cond = if_(True)
        with cond.then:
            p = Pass()
            d = Double()
            connect(src.Out, p.In)
            connect(p.Out, d.In)
            connect(d.Out, "Out")

    outputs, _ = _fail_then_resume(wf, tmp_path, broken)

    assert outputs["Out"] == [4]
    assert broken.calls == {"pass": 1, "double": 1}


def test_a_failure_inside_a_child_workflow_resumes_inside_it(tmp_path: Path) -> None:
    broken = Broken()
    Count, Pass, Double = _make_actors(broken)

    @workflow(inputs={"In": int}, outputs={"Out": int})
    def child():
        p = Pass()
        d = Double()
        connect("In", p.In)
        connect(p.Out, d.In)
        connect(d.Out, "Out")

    @workflow(outputs={"Out": int})
    def parent():
        c = Count()
        ch = child()
        connect(c.Out, ch.In)
        connect(ch.Out, "Out")

    outputs, _ = _fail_then_resume(parent, tmp_path, broken)

    assert outputs["Out"] == [2, 4, 6]
    assert broken.calls == {"count": 3, "pass": 3, "double": 3}


def test_a_run_whose_graph_changed_is_not_resumed(tmp_path: Path) -> None:
    broken = Broken()
    Count, Pass, Double = _make_actors(broken)

    @workflow(outputs={"Out": int})
    def wf():
        c = Count()
        d = Double()
        connect(c.Out, d.In)
        connect(d.Out, "Out")

    out = tmp_path / "wf-out"
    with pytest.raises(ValueError):
        run(wf, out_dir=str(out), run_id="first")

    @workflow(outputs={"Out": int})
    def wf():  # noqa: F811 - the same workflow, rewired
        c = Count()
        p = Pass()
        d = Double()
        connect(c.Out, p.In)
        connect(p.Out, d.In)
        connect(d.Out, "Out")

    with pytest.raises(ValueError, match="graph changed"):
        run(wf, out_dir=str(out), resume_from=str(out / "first"))


def test_a_resume_takes_no_inputs(tmp_path: Path) -> None:
    @workflow(inputs={"In": int}, outputs={"Out": int})
    def wf():
        connect("In", "Out")

    with pytest.raises(ValueError, match="takes no inputs"):
        run(wf, {"In": 1}, out_dir=str(tmp_path), resume_from=str(tmp_path))


def test_a_run_that_did_not_fail_has_nothing_to_resume(tmp_path: Path) -> None:
    @workflow(inputs={"In": int}, outputs={"Out": int})
    def wf():
        connect("In", "Out")

    run(wf, {"In": 1}, out_dir=str(tmp_path), run_id="ok")
    with pytest.raises(FileNotFoundError, match="only a run that failed"):
        run(wf, out_dir=str(tmp_path), resume_from=str(tmp_path / "ok"))


def test_state_that_cannot_be_saved_makes_the_run_not_resumable(tmp_path: Path) -> None:
    class Opaque:
        pass

    @task
    class Emit:
        # A handle the task keeps between firings: state, saved with the actor.
        _handle: object = None

        class Ports:
            Out = Port[int](direction="out")

        @action(consumes={}, produces={"Out": 1})
        @guard(lambda self: self._handle is None)
        def emit(self) -> int:
            self._handle = Opaque()
            return 1

    @task
    class Fail:
        class Ports:
            In = Port[int](direction="in")
            Out = Port[int](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def go(self, x: int) -> int:
            raise ValueError("no")

    @workflow(outputs={"Out": int})
    def wf():
        e = Emit()
        f = Fail()
        connect(e.Out, f.In)
        connect(f.Out, "Out")

    out = tmp_path / "wf-out"
    with pytest.raises(ValueError, match="no"):
        run(wf, out_dir=str(out), run_id="first")

    checkpoint = json.loads((out / "first" / "run.wf-checkpoint.json").read_text())
    assert checkpoint["resumable"] is False
    (reason,) = checkpoint["notResumable"]
    # A class defined inside a function can be found by neither its name nor pickle.
    assert reason.startswith("e._handle: a ") and "Opaque cannot be saved" in reason
    with pytest.raises(ValueError, match="cannot be resumed: e._handle"):
        run(wf, out_dir=str(out), resume_from=str(out / "first"))


@pytest.mark.parametrize(
    "value",
    [
        None,
        3,
        "s",
        [1, "a", None],
        {"k": [1, 2]},
        (1, (2, 3)),
        {1, 2},
        frozenset({"a"}),
        b"\x00\xff",
        {1: "int key", (2, 3): "tuple key"},
        {"$wfType": "a user's own key"},
        File("/tmp/a.txt", ext=".txt"),
        Resource("https://x/y", kind="url"),
        Path("/tmp/p"),
        Point(1, 2),
        Frozen("f", ("a", "b")),
        Derived(4),
        Plain("root", [Plain("leaf")]),
        Slotted(1, "x"),
        Hooked(5),
        Color.RED,
        Level.HIGH,
        Pair(1, [2]),
        collections.Counter("abca"),
        collections.OrderedDict([("b", 1), ("a", 2)]),
        datetime.datetime(2026, 10, 1, 12, 30, tzinfo=datetime.timezone.utc),
        datetime.date(2026, 10, 1),
        datetime.time(8, 15),
        datetime.timedelta(days=1, seconds=3),
        decimal.Decimal("1.10"),
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        1 + 2j,
    ],
)
def test_values_round_trip(value: Any) -> None:
    back = decode(json.loads(json.dumps(encode(value, "v"))))
    assert back == value
    assert type(back) is type(value)


def test_an_object_is_written_by_name_not_pickled() -> None:
    written = encode(Plain("p"), "v")

    assert written["$wfType"] == "object"
    assert written["type"] == f"{__name__}:Plain"
    assert written["state"]["label"] == "p"


def test_an_object_survives_its_class_changing(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fix between a failure and its resume changes the class's code; the
    # object it saved is still read back, as an object of the new class.
    written = json.loads(json.dumps(encode(Plain("p"), "v")))

    class Plain2(Plain):
        def shout(self) -> str:
            return self.label.upper()

    Plain2.__qualname__ = "Plain"
    monkeypatch.setattr(sys.modules[__name__], "Plain", Plain2)

    back = decode(written)
    assert type(back) is Plain2
    assert back.shout() == "P"


def test_a_run_passing_objects_fails_and_resumes(tmp_path: Path) -> None:
    broken = Broken()

    @task
    class Make:
        _n: int = 0

        class Ports:
            Out = Port[object](direction="out")

        @action(consumes={}, produces={"Out": 1})
        @guard(lambda self: self._n < 3)
        def emit(self) -> Plain:
            broken.fired("make")
            self._n += 1
            return Plain(f"p{self._n}")

    @task
    class Label:
        class Ports:
            In = Port[object](direction="in")
            Out = Port[str](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def go(self, p: Plain) -> str:
            if broken.on and p.label == "p2":
                raise ValueError("broken on 2")
            return p.label

    @workflow(outputs={"Out": str})
    def wf():
        m = Make()
        lb = Label()
        connect(m.Out, lb.In)
        connect(lb.Out, "Out")

    outputs, _ = _fail_then_resume(wf, tmp_path, broken)

    assert outputs["Out"] == ["p1", "p2", "p3"]
    assert broken.calls == {"make": 3}


def test_a_value_that_cannot_be_saved_is_named() -> None:
    import threading

    with pytest.raises(NotResumable, match=r"v\[1\]: a lock cannot be saved"):
        encode([1, threading.Lock()], "v")


CLI_WORKFLOW = """
from pathlib import Path

from wfpy import Port, action, connect, guard, task, workflow

HERE = Path(__file__).parent


@task
class Count:
    _n: int = 0

    class Ports:
        Out = Port[int](direction="out")

    @action(consumes={}, produces={"Out": 1})
    @guard(lambda self: self._n < 3)
    def emit(self) -> int:
        with open(HERE / "fired.log", "a") as log:
            log.write("count\\n")
        self._n += 1
        return self._n


@task
class Double:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def go(self, x: int) -> int:
        if x == 2 and (HERE / "broken").exists():
            raise ValueError("broken on 2")
        return x * 2


@workflow(outputs={"Out": int})
def flow():
    c = Count()
    d = Double()
    connect(c.Out, d.In)
    connect(d.Out, "Out")
"""


def test_the_cli_resumes_a_failed_run(tmp_path: Path) -> None:
    import subprocess
    import sys

    flow = tmp_path / "flow.py"
    flow.write_text(CLI_WORKFLOW)
    (tmp_path / "broken").touch()
    out = tmp_path / "wf-out"

    def wfpy(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", "from wfpy.cli import main; main()", "run", str(flow), *args],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

    failed = wfpy("--out-dir", str(out), "--run-id", "first")
    assert failed.returncode != 0
    assert (out / "first" / "run.wf-checkpoint.json").is_file()

    (tmp_path / "broken").unlink()  # the fix
    resumed = wfpy("--out-dir", str(out), "--run-id", "second", "--resume-from", str(out / "first"))
    assert resumed.returncode == 0, resumed.stderr

    record = json.loads((out / "second" / "run.wf-run.json").read_text())
    assert record["resumedFrom"] == "first"
    assert record["outputs"]["Out"] == [2, 4, 6]
    assert (tmp_path / "fired.log").read_text().count("count") == 3
