"""A port declared without ``direction="in"`` or ``direction="out"`` is
deprecated. The runtime warns of it at the line that declares it and runs the
workflow as it did. The sidecar says the same on its graph, in the runtime's
words, and refuses no connection for such a port, since the runtime checks no
direction on it.

When wfpy refuses such a port, this is the file to turn round.
"""

from __future__ import annotations

import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest

from wfpy import Port, run, task
from wfpy.cli import _load_module
from wfpy.sidecar import _handle_request
from wfpy.types import port_without_direction_notice

# `Out` of `Up` is written on line 8, with no direction.
FLOW = '''\
from wfpy import Port, action, connect, task, workflow


@task
class Up:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str]()

    @action(consumes={"In": 1}, produces={"Out": 1})
    def up(self, In: str) -> str:
        return In.upper()


@task
class Bang:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def bang(self, In: str) -> str:
        return In + "!"


@workflow(inputs={"Text": str}, outputs={"Result": str})
def flow():
    a = Up()
    b = Bang()
    connect("Text", a.In)
    connect(a.Out, b.In)
    connect(b.Out, "Result")
'''
EDGE = "    connect(a.Out, b.In)\n"

# A port that is no output by its name sends, one that is an output by its
# name receives: the runtime runs it, an action naming each.
EITHER_END = '''\
from wfpy import Port, action, connect, task, workflow


@task
class Up:
    class Ports:
        In = Port[str](direction="in")
        Data = Port[str]()

    @action(consumes={"In": 1}, produces={"Data": 1})
    def up(self, In: str) -> str:
        return In.upper()


@task
class Bang:
    class Ports:
        Result = Port[str]()
        Done = Port[str](direction="out")

    @action(consumes={"Result": 1}, produces={"Done": 1})
    def bang(self, Result: str) -> str:
        return Result + "!"


@workflow(inputs={"Text": str}, outputs={"Final": str})
def flow():
    a = Up()
    b = Bang()
    connect("Text", a.In)
    connect(a.Data, b.Result)
    connect(b.Done, "Final")
'''


def _file(tmp_path: Path, text: str, name: str = "flow.py") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _loaded(path: Path) -> tuple[Any, list[warnings.WarningMessage]]:
    """The module of a workflow file, loaded as `wfpy run` loads it, and what
    it was warned of for its ports."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        module = _load_module(str(path))
    return module, [w for w in caught if "a port declared without direction" in str(w.message)]


def _ask(path: Path, op: str, **args: object) -> Any:
    return _handle_request({"file": str(path), "op": f"wfpy.{op}", "args": args})


def _graph(path: Path) -> dict[str, Any]:
    answer = _ask(path, "exportWorkflowGraph", workflow="flow")
    assert answer.status == "ok", answer
    return (answer.diagnostic or {})["graph"]


def _notices(node: dict[str, Any]) -> list[dict[str, Any]]:
    return list((node.get("metadata") or {}).get("diagnostics") or [])


# --- the runtime ----------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "port_name", "side", "to_write"),
    [
        pytest.param("Out = Port[str]()", "Out", "an output", "out", id="no direction"),
        pytest.param("Out = Port[str]", "Out", "an output", "out", id="not called"),
        pytest.param(
            'Out = Port[str](direction="inout")', "Out", "an output", "out", id="inout"
        ),
        pytest.param("Verdict = Port[str]()", "Verdict", "an input", "in", id="another name"),
        pytest.param(
            'Verdict = Port[str](direction="output")',
            "Verdict",
            "an input",
            "in",
            id="a direction that is neither",
        ),
        pytest.param(
            'Verdict = Port[str]("Report")', "Report", "an output", "out", id="its own name"
        ),
    ],
)
def test_the_runtime_warns_at_the_line_that_declares_it(
    tmp_path: Path, written: str, port_name: str, side: str, to_write: str
) -> None:
    path = _file(
        tmp_path,
        "from wfpy import Port, task\n"
        "\n"
        "\n"
        "@task\n"
        "class Up:\n"
        "    class Ports:\n"
        '        In = Port[str](direction="in")\n'
        f"        {written}\n",
    )
    _module, warned = _loaded(path)
    (one,) = warned
    attr = written.split(" ", 1)[0]
    assert one.category is FutureWarning
    assert (one.filename, one.lineno) == (str(path), 8)
    text = str(one.message)
    assert text == port_without_direction_notice("Up", attr, port_name)
    assert text.startswith(f"Up.Ports.{attr}: ")
    assert f"wfpy takes this one for {side}, by its name" in text
    assert f'write direction="{to_write}" to keep that' in text
    assert "wfpy 2.0 will refuse it" in text


def test_a_port_that_states_its_direction_is_not_warned_of() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")

        @task
        class Stated:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str]("Text", direction="out")

    assert set(Stated._wfpy_meta.output_ports) == {"Out"}  # type: ignore[attr-defined]


def test_the_workflow_runs_as_it_did(tmp_path: Path) -> None:
    module, warned = _loaded(_file(tmp_path, FLOW))
    assert len(warned) == 1
    assert run(module.flow, {"Text": "hello"}, out_dir=tmp_path / "out") == {"Result": ["HELLO!"]}


def test_wfpy_run_shows_the_warning_to_the_author(tmp_path: Path) -> None:
    """Python hides a `DeprecationWarning` of a module that is not
    `__main__`, which a workflow file under `wfpy run` is not."""
    path = _file(tmp_path, FLOW)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONWARNINGS"}
    ran = subprocess.run(
        [sys.executable, "-m", "wfpy.cli", "run", str(path), "--input", "Text=hello",
         "--out-dir", str(tmp_path / "out")],
        capture_output=True, text=True, cwd=tmp_path, env=env, check=False,
    )
    assert ran.returncode == 0, ran.stderr
    assert "HELLO!" in ran.stdout
    assert f"{path}:8: FutureWarning: Up.Ports.Out: a port declared without" in ran.stderr
    assert "Out = Port[str]()" in ran.stderr  # the line itself, under the warning


def test_a_class_whose_source_cannot_be_read_is_warned_of_all_the_same() -> None:
    scope: dict[str, Any] = {}
    with pytest.warns(FutureWarning, match=r"Up\.Ports\.Out: a port declared without"):
        exec(  # noqa: S102 - a class with no file behind it
            "from wfpy import Port, task\n"
            "@task\n"
            "class Up:\n"
            "    class Ports:\n"
            "        Out = Port[str]()\n",
            scope,
        )
    assert set(scope["Up"]._wfpy_meta.output_ports) == {"Out"}


def test_an_inherited_port_is_said_of_the_class_that_writes_it(tmp_path: Path) -> None:
    path = _file(
        tmp_path,
        "from wfpy import Port, connect, task, workflow\n"
        "\n"
        "\n"
        "class Base:\n"
        "    class Ports:\n"
        '        In = Port[str](direction="in")\n'
        "        Out = Port[str]()\n"
        "\n"
        "\n"
        "@task\n"
        "class Plain(Base):\n"
        "    pass\n"
        "\n"
        "\n"
        "@task\n"
        "class Wider(Base):\n"
        "    class Ports(Base.Ports):\n"
        '        More = Port[str](direction="out")\n'
        "\n"
        "\n"
        "@workflow\n"
        "def flow():\n"
        "    p = Plain()\n"
        "    w = Wider()\n"
        "    connect(p.Out, w.In)\n",
    )
    _module, warned = _loaded(path)
    said = port_without_direction_notice("Base", "Out", "Out")
    assert {str(w.message) for w in warned} == {said}
    # The graph has the same words on the nodes of both classes.
    nodes = {n["id"]: n for n in _graph(path)["nodes"]}
    for name in ("p", "w"):
        (notice,) = _notices(nodes[name])
        assert notice["message"] == said
        assert notice["location"] == {"file": str(path), "line": 7}


# --- the sidecar ----------------------------------------------------------


def test_the_graph_says_it_on_the_node_in_the_runtime_s_words(tmp_path: Path) -> None:
    path = _file(tmp_path, FLOW)
    _module, (warned,) = _loaded(path)
    graph = _graph(path)
    nodes = {n["id"]: n for n in graph["nodes"]}

    (notice,) = _notices(nodes["a"])
    assert notice == {
        "severity": "warning",
        "code": "port_without_direction",
        "message": str(warned.message),
        "location": {"file": str(path), "line": warned.lineno},
    }
    # A warning is no fault: nothing is errored and the graph is whole.
    assert nodes["a"]["metadata"] == {"diagnostics": [notice]}
    assert all("metadata" not in n for name, n in nodes.items() if name != "a")
    assert "partial" not in graph
    assert "errors" not in graph
    assert all("meta" not in edge and "metadata" not in edge for edge in graph["edges"])
    # The port is still drawn on the side its name gives it.
    out = next(p for p in nodes["a"]["ports"] if p["name"] == "Out")
    assert out["direction"] == "out"


def test_every_node_of_the_class_has_it(tmp_path: Path) -> None:
    text = FLOW.replace("    b = Bang()\n", "    b = Bang()\n    c = Up()\n")
    nodes = {n["id"]: n for n in _graph(_file(tmp_path, text))["nodes"]}
    assert [len(_notices(nodes[name])) for name in ("a", "b", "c")] == [1, 0, 1]


def test_a_class_of_another_file_is_said_at_its_place_there(tmp_path: Path) -> None:
    head, _, body = FLOW.partition("@workflow")
    tasks = _file(tmp_path, head, "tasks.py")
    top = _file(
        tmp_path,
        "from wfpy import connect, workflow\nfrom tasks import Bang, Up\n\n\n@workflow" + body,
        "top.py",
    )
    nodes = {n["id"]: n for n in _graph(top)["nodes"]}
    (notice,) = _notices(nodes["a"])
    assert notice["message"] == port_without_direction_notice("Up", "Out", "Out")
    assert notice["location"] == {"file": str(tasks.resolve()), "line": 8}


def test_a_connection_out_of_such_a_port_is_accepted(tmp_path: Path) -> None:
    """It was refused as `invalid_source_direction`: the port was taken for
    an input, where the runtime checks no direction on it."""
    assert EDGE in FLOW
    path = _file(tmp_path, FLOW.replace(EDGE, ""))

    checked = _ask(path, "checkConnection", workflow="flow", source="a.Out", target="b.In")
    assert checked.status == "ok", checked
    assert (checked.diagnostic or {}).get("code") == "connection_ok"

    made = _ask(path, "connect", workflow="flow", from_expr="a.Out", to_expr="b.In")
    assert made.status == "ok", made
    # The line is written, last in the workflow, and what was drawn runs.
    assert path.read_text(encoding="utf-8") == FLOW.replace(EDGE, "") + EDGE
    module, _warned = _loaded(path)
    assert run(module.flow, {"Text": "hello"}, out_dir=tmp_path / "out") == {"Result": ["HELLO!"]}


def test_a_connection_that_runs_is_accepted_at_either_end(tmp_path: Path) -> None:
    edge = "    connect(a.Data, b.Result)\n"
    assert edge in EITHER_END
    whole = _file(tmp_path, EITHER_END)
    module, warned = _loaded(whole)
    assert len(warned) == 2
    assert run(module.flow, {"Text": "hello"}, out_dir=tmp_path / "out") == {"Final": ["HELLO!"]}

    graph = _graph(whole)
    assert "partial" not in graph
    assert all("meta" not in e for e in graph["edges"])

    cut = _file(tmp_path, EITHER_END.replace(edge, ""), "cut.py")
    checked = _ask(cut, "checkConnection", workflow="flow", source="a.Data", target="b.Result")
    assert checked.status == "ok", checked


def test_a_stated_direction_is_still_held(tmp_path: Path) -> None:
    path = _file(tmp_path, FLOW.replace(EDGE, ""))

    stated_in = _ask(path, "checkConnection", workflow="flow", source="b.In", target="a.In")
    assert stated_in.status == "error"
    assert (stated_in.diagnostic or {}).get("code") == "invalid_source_direction"

    stated_out = _ask(path, "checkConnection", workflow="flow", source="a.Out", target="b.Out")
    assert stated_out.status == "error"
    assert (stated_out.diagnostic or {}).get("code") == "invalid_target_direction"
