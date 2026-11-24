"""`wfpy plan --format graph --best-effort` draws a file it cannot import.

The command imports the file to ask the runtime for its graph. A file that
does not import (a misspelt name, a failing import, a syntax fault) left it
nothing to ask, and it ended on the traceback with `--best-effort` or without.
It now draws the file from the source, and says where the error is: at its
line, and on the nodes it belongs to. These tests hold that document against
the one the runtime gives for the same file when it does import.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from wfpy._graph_export import export_graph_json
from wfpy._static_plan import static_graph_document
from wfpy.cli import _find_workflows, _load_module
from wfpy.partial import build_partial_graph
from wfpy.runner import _build_workflow_graph
from wfpy.sidecar import _handle_request

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = ROOT / "examples"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("[0-9][0-9]_*.py")) + sorted(
    (EXAMPLES_DIR / "advanced").glob("*.py")
)

PIPELINE = '''from wfpy import Port, action, connect, loop, task, workflow

LIMIT = int("10")


@task
class Scale:
    scale: int

    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def apply(self, x: int) -> int:
        return self.scale * x


@task
class Add:
    class Ports:
        A = Port[int](direction="in")
        B = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"A": 1, "B": 1}, produces={"Out": 1})
    def add(self, a: int, b: int) -> int:
        return a + b


@workflow(inputs={"A": int}, outputs={"Out": int})
def scaled_sum():
    scale_a = Scale(scale=2)
    scale_b, add = Scale(scale=3), Add()
    stream = loop([1, 2, 3])
    with stream:
        connect(stream.item, scale_b.In)
    connect("A", scale_a.In)
    connect(scale_a.Out, add.A)
    connect(scale_b.Out, add.B)
    connect(add.Out, "Out")
'''


def _plan(path: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "wfpy.cli", "plan", str(path), "--format", "graph", *flags],
        capture_output=True,
        text=True,
        cwd=str(path.parent),
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
    )


def _best_effort(path: Path) -> dict[str, Any]:
    done = _plan(path, "--best-effort")
    assert done.returncode == 0, done.stderr
    document: dict[str, Any] = json.loads(done.stdout)
    return document


def _written(tmp_path: Path, good: str = "", bad: str = "", name: str = "wf.py") -> Path:
    """The pipeline in a file, with the line `good` written as `bad`."""
    text = PIPELINE
    if good:
        assert text.count(good) == 1, good
        text = text.replace(good, bad)
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _shape(document: dict[str, Any]) -> tuple[Any, Any, Any]:
    """The nodes with their ports, the edges and the scopes, by their ids."""
    graph = document["graph"]
    nodes = {
        node["id"]: (
            node["kind"],
            node["label"],
            node.get("type"),
            node["scope"],
            sorted(
                (p["id"], p["name"], p["direction"], p["type"], p["role"]) for p in node["ports"]
            ),
        )
        for node in graph["nodes"]
    }
    edges = {
        edge["id"]: (edge["from"], edge["to"], edge["scope"], edge["fromNode"], edge["toNode"])
        for edge in graph["edges"]
    }
    scopes = {
        scope["id"]: (
            scope["kind"],
            scope["parent"],
            scope["controlNodeId"],
            scope["children"],
            sorted(scope["nodes"]),
            sorted(scope["edges"]),
        )
        for scope in graph["subgraphs"]
    }
    return nodes, edges, scopes


def _flagged(document: dict[str, Any]) -> list[str]:
    graph = document["graph"]
    return sorted(
        element["id"]
        for element in [*graph["nodes"], *graph["edges"]]
        if (element.get("meta") or {}).get("isErrored")
    )


@pytest.fixture
def good(tmp_path: Path) -> dict[str, Any]:
    """The document the runtime gives for the pipeline as it is written."""
    folder = tmp_path / "good"
    folder.mkdir()
    done = _plan(_written(folder))
    assert done.returncode == 0, done.stderr
    document: dict[str, Any] = json.loads(done.stdout)
    return document


# ── A file that does not import ────────────────────────────────────────


def test_a_misspelt_name_in_a_class_is_an_error_on_the_nodes_of_that_class(
    tmp_path: Path, good: dict[str, Any]
) -> None:
    path = _written(tmp_path, 'B = Port[int](direction="in")', 'B = Prot[int](direction="in")')

    document = _best_effort(path)

    # The document of the file as it was before the fault, by every id.
    assert _shape(document)[1:] == _shape(good)[1:]
    nodes, good_nodes = _shape(document)[0], _shape(good)[0]
    assert set(nodes) == set(good_nodes)
    # `Add.B` is not read as a port any more; the connection to it names it.
    assert {port[1] for port in nodes["node:scope:root:add"][4]} == {"A", "B", "Out"}
    assert {name: nodes[name] for name in nodes if not name.endswith(":add")} == {
        name: good_nodes[name] for name in good_nodes if not name.endswith(":add")
    }

    assert document["partial"] is True
    assert document["errors"] == [
        {
            "message": "NameError: name 'Prot' is not defined",
            "file": str(path),
            "line": 23,
            "column": None,
        }
    ]
    # The error is in the body of `Add`: the node of `Add` carries it.
    assert _flagged(document) == ["node:scope:root:add"]
    add = next(n for n in document["graph"]["nodes"] if n["label"] == "add")
    assert add["meta"]["errorMessage"] == "NameError: name 'Prot' is not defined"
    assert "diagnostics" not in add["meta"]  # reported once, in `errors`


def test_without_best_effort_the_error_is_still_the_answer(tmp_path: Path) -> None:
    path = _written(tmp_path, 'B = Port[int](direction="in")', 'B = Prot[int](direction="in")')

    done = _plan(path)

    assert done.returncode != 0
    assert done.stdout == ""
    assert "NameError: name 'Prot' is not defined" in done.stderr


def test_an_error_at_the_top_of_the_module_is_the_file_s_and_no_node_s(
    tmp_path: Path, good: dict[str, Any]
) -> None:
    path = _written(tmp_path, 'LIMIT = int("10")', 'LIMIT = int("ten")')

    document = _best_effort(path)

    assert _shape(document) == _shape(good)
    assert [(e["message"], e["line"]) for e in document["errors"]] == [
        ("ValueError: invalid literal for int() with base 10: 'ten'", 3)
    ]
    assert _flagged(document) == []


def test_a_failing_import_is_at_its_line_and_the_graph_is_drawn(
    tmp_path: Path, good: dict[str, Any]
) -> None:
    path = _written(tmp_path, 'LIMIT = int("10")', "import a_module_nobody_has")

    document = _best_effort(path)

    assert _shape(document) == _shape(good)
    assert [(e["message"], e["line"]) for e in document["errors"]] == [
        ("ModuleNotFoundError: No module named 'a_module_nobody_has'", 3)
    ]


def test_an_error_raised_in_a_module_the_file_imports_is_at_the_import(tmp_path: Path) -> None:
    (tmp_path / "broken_helper.py").write_text("VALUE = undefined_name\n", encoding="utf-8")
    path = _written(tmp_path, 'LIMIT = int("10")', "from broken_helper import VALUE")

    document = _best_effort(path)

    assert [(e["message"], Path(e["file"]).name, e["line"]) for e in document["errors"]] == [
        ("NameError: name 'undefined_name' is not defined", "wf.py", 3)
    ]
    assert len(document["graph"]["edges"]) == 5


def test_a_file_that_is_not_there_is_still_an_error(tmp_path: Path) -> None:
    done = _plan(tmp_path / "nowhere.py", "--best-effort")

    assert done.returncode != 0
    assert "file not found" in done.stderr


# ── A file that does not parse ─────────────────────────────────────────


def test_a_syntax_fault_on_a_node_s_line_is_on_that_node(
    tmp_path: Path, good: dict[str, Any]
) -> None:
    path = _written(tmp_path, "scale_a = Scale(scale=2)", "scale_a = Scale(scale=2")

    document = _best_effort(path)

    assert _shape(document) == _shape(good)
    with pytest.raises(SyntaxError) as python:
        ast.parse(path.read_text(encoding="utf-8"))
    assert document["errors"] == [
        {
            "message": f"SyntaxError: {python.value.msg}",
            "file": str(path),
            "line": 33,
            "column": python.value.offset,
        }
    ]
    assert _flagged(document) == ["node:scope:root:scale_a"]


def test_a_syntax_fault_on_a_connection_s_line_is_on_that_edge(
    tmp_path: Path, good: dict[str, Any]
) -> None:
    path = _written(tmp_path, "connect(scale_a.Out, add.A)", "connect(scale_a.Out add.A)")

    document = _best_effort(path)

    assert _shape(document) == _shape(good)
    assert _flagged(document) == [
        "edge:port:node:scope:root:scale_a:Out->port:node:scope:root:add:A"
    ]
    assert [e["line"] for e in document["errors"]] == [39]


def test_a_fault_on_a_later_line_of_a_node_s_statement_is_on_that_node(
    tmp_path: Path, good: dict[str, Any]
) -> None:
    # Python stops on the second line of the statement, which the repair
    # leaves empty: the node is the one of the statement, not of that line.
    path = _written(
        tmp_path, "    scale_a = Scale(scale=2)\n", "    scale_a = Scale(\n        scale=2 +)\n"
    )

    document = _best_effort(path)

    assert _shape(document) == _shape(good)
    assert [e["line"] for e in document["errors"]] == [34]
    assert _flagged(document) == ["node:scope:root:scale_a"]
    response = _handle_request({"file": str(path), "op": "wfpy.exportWorkflowGraph", "args": {}})
    assert response.diagnostic is not None
    assert [
        node["id"]
        for node in response.diagnostic["graph"]["nodes"]
        if (node.get("metadata") or {}).get("isErrored")
    ] == ["scale_a"]


@pytest.mark.parametrize(
    ("good_line", "bad_line", "flagged"),
    [
        (
            '        In = Port[int](direction="in")',
            '        In = Port[int](direction="in"',
            ["scale_a", "scale_b"],
        ),
        ("class Add:", "class Add", ["add"]),
        (
            '    @action(consumes={"A": 1, "B": 1}, produces={"Out": 1})',
            '    @action(consumes={"A": 1, "B": 1}, produces={"Out": 1)',
            ["add"],
        ),
        ("        return a + b", "        return a +", ["add"]),
        ("def scaled_sum():", "def scaled_sum(:", []),
    ],
    ids=["port", "class header", "decorator of a method", "method body", "workflow header"],
)
def test_a_syntax_fault_in_a_class_is_on_every_node_of_that_class(
    tmp_path: Path, good: dict[str, Any], good_line: str, bad_line: str, flagged: list[str]
) -> None:
    path = _written(tmp_path, good_line, bad_line)

    document = _best_effort(path)

    assert _shape(document)[1:] == _shape(good)[1:]
    assert _flagged(document) == [f"node:scope:root:{name}" for name in sorted(flagged)]
    # The sidecar's static graph of the same file flags the same nodes.
    response = _handle_request({"file": str(path), "op": "wfpy.exportWorkflowGraph", "args": {}})
    assert response.diagnostic is not None
    static = response.diagnostic["graph"]
    assert [
        node["id"] for node in static["nodes"] if (node.get("metadata") or {}).get("isErrored")
    ] == flagged
    assert len(static["errors"]) == len(document["errors"]) == 1
    assert static["errors"][0]["location"]["line"] == document["errors"][0]["line"]


# ── What cannot be drawn ───────────────────────────────────────────────


def test_a_file_with_nothing_to_draw_is_an_empty_document_with_the_error(tmp_path: Path) -> None:
    path = tmp_path / "wf.py"
    path.write_text("VALUE = undefined_name\n", encoding="utf-8")

    assert static_graph_document(str(path), None, NameError("undefined_name")) is None
    document = build_partial_graph(str(path), None, NameError("undefined_name"), imports=False)

    assert document["partial"] is True
    assert document["graph"]["nodes"] == []
    assert document["errors"][0]["message"] == "NameError: undefined_name"


# ── Every example ──────────────────────────────────────────────────────


def _own_workflows(path: Path) -> list[str]:
    module = _load_module(str(path))
    names = []
    for name, wf in _find_workflows(module).items():
        builder = getattr(wf._wfpy_workflow, "builder_fn", None)
        if builder is not None and Path(builder.__code__.co_filename).resolve() == path.resolve():
            names.append(name)
    return sorted(names)


def _defined_twice(path: Path) -> set[str]:
    """The classes a file defines under a condition, once on each side: which
    one a name is bound to is known only when the file runs."""
    seen: dict[str, int] = {}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ClassDef):
            seen[node.name] = seen.get(node.name, 0) + 1
    return {name for name, count in seen.items() if count > 1}


@pytest.mark.parametrize("example", EXAMPLE_FILES, ids=lambda path: path.stem)
def test_the_static_document_of_an_example_is_the_runtime_s(example: Path) -> None:
    conditional = set()
    for imported in EXAMPLE_FILES:
        if imported.parent == example.parent:
            conditional |= _defined_twice(imported)
    module = _load_module(str(example))
    workflows = _find_workflows(module)
    names = _own_workflows(example)
    if not names:
        pytest.skip("no workflow of its own to draw")
    for name in names:
        runtime = export_graph_json(_build_workflow_graph(workflows[name]._wfpy_workflow))

        static = static_graph_document(str(example), name, RuntimeError("not imported"))

        assert static is not None
        assert static["graph"]["id"] == runtime["graph"]["id"]
        nodes, edges, scopes = _shape(static)
        runtime_nodes, runtime_edges, runtime_scopes = _shape(runtime)
        assert edges == runtime_edges
        assert scopes == runtime_scopes
        assert set(nodes) == set(runtime_nodes)
        for node_id, node in nodes.items():
            if node[2] in conditional:
                # The ports are the same on both sides of the condition.
                assert node[4] == runtime_nodes[node_id][4]
                continue
            assert node == runtime_nodes[node_id], node_id
        # Where each node is written and what it is an instance of.
        for drawn in static["graph"]["nodes"]:
            if drawn["kind"] in ("wf-input", "wf-output", "loop", "if"):
                continue
            real = next(n for n in runtime["graph"]["nodes"] if n["id"] == drawn["id"])
            assert drawn["meta"].get("source") == real["meta"].get("source"), drawn["id"]
            if drawn.get("type") not in conditional:
                assert drawn["meta"].get("referencedSource") == real["meta"].get(
                    "referencedSource"
                ), drawn["id"]
