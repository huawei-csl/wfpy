"""A workflow file with a syntax fault still has a diagram.

Neither `ast` nor libcst gives a tree for a file with one bracket left open,
so `wfpy.exportWorkflowGraph` answered with an empty graph and the diagram of
a file under edit went blank at every keystroke that left it unparsable. The
export now draws from a repaired copy of the text. These tests hold what it
draws, what it says about the faults, and that the copy stays a copy: nothing
is run, nothing is written, and an op that edits the file still refuses it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from wfpy._drawable_source import (
    _kept_header,
    _kept_statement,
    drawable_source,
    mark_faults,
    syntax_fault,
)
from wfpy._source_repair import RepairedSource, repair_source
from wfpy.sidecar import _current_revision, _handle_request

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("[0-9][0-9]_*.py")) + sorted(
    (EXAMPLES_DIR / "advanced").glob("*.py")
)

PIPELINE = '''from wfpy import Port, action, connect, loop, task, workflow


@task
class Scale:
    scale: int

    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")
        Spare = Port[str](direction="in")

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


def _export(path: Path, **args: Any) -> dict[str, Any]:
    response = _handle_request({"file": str(path), "op": "wfpy.exportWorkflowGraph", "args": args})
    assert response.status == "ok", response.message
    assert response.diagnostic is not None
    graph: dict[str, Any] = response.diagnostic["graph"]
    return graph


def _broken(tmp_path: Path, good: str, bad: str, text: str = PIPELINE) -> Path:
    """`text` with the line `good` written as `bad`, in a file."""
    assert text.count(good) == 1, good
    path = tmp_path / "wf.py"
    path.write_text(text.replace(good, bad), encoding="utf-8")
    with pytest.raises(SyntaxError):
        ast.parse(path.read_text(encoding="utf-8"))
    return path


def _good(tmp_path: Path, text: str = PIPELINE) -> dict[str, Any]:
    path = tmp_path / "good" / "wf.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return _export(path)


def _shape(graph: dict[str, Any]) -> tuple[Any, Any]:
    """The nodes with their ports, and the edges with their scope."""
    nodes = {
        node["id"]: sorted((p["name"], p["direction"], p["type"]) for p in node["ports"])
        for node in graph["nodes"]
    }
    edges = sorted(
        (edge["from"]["portId"], edge["to"]["portId"], edge["scope"]) for edge in graph["edges"]
    )
    return nodes, edges


def _flagged(graph: dict[str, Any]) -> list[str]:
    nodes = [n["id"] for n in graph["nodes"] if (n.get("metadata") or {}).get("isErrored")]
    edges = [e["id"] for e in graph["edges"] if (e.get("meta") or {}).get("isErrored")]
    return nodes + edges


def _python_stops_at(path: Path) -> dict[str, Any]:
    """The place and the words of `python wf.py` for this file."""
    with pytest.raises(SyntaxError) as caught:
        ast.parse(path.read_text(encoding="utf-8"))
    error = caught.value
    location: dict[str, Any] = {"file": str(path), "line": error.lineno}
    if error.offset:
        location["column"] = error.offset
    return {
        "severity": "error",
        "code": "syntax_error",
        "message": error.msg,
        "location": location,
    }


# ── What is drawn ──────────────────────────────────────────────────────


def test_a_node_with_a_bracket_left_open_is_drawn_and_flagged(tmp_path: Path) -> None:
    path = _broken(tmp_path, "scale_a = Scale(scale=2)", "scale_a = Scale(scale=2")

    graph = _export(path)

    # Every node, port and edge of the file as it was before the fault.
    assert _shape(graph) == _shape(_good(tmp_path))
    assert graph["partial"] is True
    assert graph["errors"] == [_python_stops_at(path)]
    assert graph["errors"][0]["location"]["line"] == 32

    # The node of the faulty line is drawn from the start of its statement:
    # what it is, and none of the arguments the fault may have touched.
    node = next(n for n in graph["nodes"] if n["id"] == "scale_a")
    assert (node["ref"], node["args"], node["kwargs"]) == ("Scale", [], {})
    assert node["location"]["line"] == 32
    # It is flagged. The fault is reported once, in `errors`, at Python's
    # place, and not a second time at the start of the node's statement.
    assert node["metadata"] == {
        "isErrored": True,
        "errorMessage": graph["errors"][0]["message"],
    }
    assert _flagged(graph) == ["scale_a"]


def test_the_nodes_of_a_broken_tuple_assignment_are_drawn(tmp_path: Path) -> None:
    path = _broken(
        tmp_path, "scale_b, add = Scale(scale=3), Add()", "scale_b, add = Scale(scale=3), Add("
    )

    graph = _export(path)

    assert _shape(graph) == _shape(_good(tmp_path))
    assert _flagged(graph) == ["scale_b", "add"]


def test_a_connection_that_lost_its_comma_is_drawn_and_flagged(tmp_path: Path) -> None:
    path = _broken(tmp_path, "connect(scale_a.Out, add.A)", "connect(scale_a.Out add.A)")

    graph = _export(path)

    assert _shape(graph) == _shape(_good(tmp_path))
    assert graph["errors"] == [_python_stops_at(path)]
    edge = next(e for e in graph["edges"] if e["to"]["portId"] == "n:add:p:A")
    assert edge["location"]["line"] == 38
    assert edge["meta"] == {"isErrored": True, "errorMessage": graph["errors"][0]["message"]}
    assert _flagged(graph) == [edge["id"]]


def test_a_connection_whose_end_cannot_be_read_is_left_out(tmp_path: Path) -> None:
    path = _broken(tmp_path, "connect(scale_a.Out, add.A)", "connect(scale_a.Out, add.")

    graph = _export(path)

    nodes, edges = _shape(graph)
    good_nodes, good_edges = _shape(_good(tmp_path))
    assert set(nodes) == set(good_nodes)
    assert edges == [edge for edge in good_edges if edge[1] != "n:add:p:A"]
    # The port is still there: the class declares it.
    assert nodes["add"] == good_nodes["add"]
    assert _flagged(graph) == []
    assert [error["location"]["line"] for error in graph["errors"]] == [38]


def test_the_scope_of_a_with_header_that_lost_its_colon_is_kept(tmp_path: Path) -> None:
    path = _broken(tmp_path, "    with stream:\n", "    with stream\n")

    graph = _export(path)

    # The connection under it is still drawn inside the loop.
    assert _shape(graph) == _shape(_good(tmp_path))
    inside = next(e for e in graph["edges"] if e["to"]["portId"] == "n:scale_b:p:In")
    assert inside["scope"] != "scope:root"


def test_a_node_whose_line_is_indented_wrong_is_drawn_whole_and_flagged(tmp_path: Path) -> None:
    path = _broken(tmp_path, "    scale_a = Scale(scale=2)", "  scale_a = Scale(scale=2)")

    graph = _export(path)

    assert _shape(graph) == _shape(_good(tmp_path))
    node = next(n for n in graph["nodes"] if n["id"] == "scale_a")
    assert node["kwargs"] == {"scale": "2"}  # only the indentation was repaired
    assert _flagged(graph) == ["scale_a"]


@pytest.mark.parametrize(
    ("good", "bad", "line"),
    [
        ('        In = Port[int](direction="in")', '        In = Port[int](direction="in"', 9),
        ("class Add:", "class Add", 19),
        (
            '    @action(consumes={"A": 1, "B": 1}, produces={"Out": 1})',
            '    @action(consumes={"A": 1, "B": 1}, produces={"Out": 1)',
            25,
        ),
        (
            "    def add(self, a: int, b: int) -> int:",
            "    def add(self, a: int b: int) -> int:",
            26,
        ),
        ("        return a + b", "        return a +", 27),
        (
            '@workflow(inputs={"A": int}, outputs={"Out": int})',
            '@workflow(inputs={"A": int}, outputs={"Out": int}',
            30,
        ),
        ("def scaled_sum():", "def scaled_sum(:", 31),
    ],
    ids=[
        "port",
        "class header",
        "decorator",
        "method header",
        "method body",
        "workflow decorator",
        "workflow header",
    ],
)
def test_a_fault_outside_the_workflow_s_statements_leaves_the_graph_whole(
    tmp_path: Path, good: str, bad: str, line: int
) -> None:
    path = _broken(tmp_path, good, bad)

    graph = _export(path)

    nodes, edges = _shape(graph)
    good_nodes, good_edges = _shape(_good(tmp_path))
    # Every node and every edge. A port is the declared one where its
    # declaration is read, and the one a connection names where it is not.
    assert set(nodes) == set(good_nodes)
    assert edges == good_edges
    for name, ports in nodes.items():
        assert {port[0] for port in ports} >= {
            port[0] for port in good_nodes[name] if port[0] != "Spare"
        }
    assert graph["partial"] is True
    assert [error["location"]["line"] for error in graph["errors"]] == [line]
    assert graph["errors"] == [_python_stops_at(path)]
    assert _flagged(graph) == []


def test_two_faults_are_two_errors_in_line_order(tmp_path: Path) -> None:
    text = PIPELINE.replace("        return a + b", "        return a +")
    path = _broken(tmp_path, "connect(add.Out, \"Out\")", "connect(add.Out, \"Out\"", text)

    graph = _export(path)

    assert _shape(graph) == _shape(_good(tmp_path))
    assert [error["location"]["line"] for error in graph["errors"]] == [27, 40]
    assert {error["code"] for error in graph["errors"]} == {"syntax_error"}
    # Python names the first only.
    assert graph["errors"][0] == _python_stops_at(path)
    assert len(_flagged(graph)) == 1


# ── The copy stays a copy ──────────────────────────────────────────────


def test_the_file_is_not_written_and_the_revision_is_the_file_s(tmp_path: Path) -> None:
    path = _broken(tmp_path, "scale_a = Scale(scale=2)", "scale_a = Scale(scale=2")
    before = path.read_bytes()

    response = _handle_request({"file": str(path), "op": "wfpy.exportWorkflowGraph", "args": {}})

    assert path.read_bytes() == before
    assert response.revision == _current_revision(str(path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["wf.py"]


@pytest.mark.parametrize(
    ("op", "args"),
    [
        (
            "wfpy.connect",
            {"workflow": "scaled_sum", "from_expr": "add.Out", "to_expr": "scale_a.In"},
        ),
        ("wfpy.createNode", {"workflow": "scaled_sum", "type": "Add", "name": "more"}),
        ("wfpy.deleteNode", {"workflow": "scaled_sum", "name": "add"}),
        ("wfpy.renameNode", {"workflow": "scaled_sum", "name": "add", "newName": "plus"}),
        ("wfpy.checkConnection", {"sourceId": "n:add:p:Out", "targetId": "n:scale_a:p:In"}),
    ],
)
def test_an_op_other_than_the_export_still_refuses_the_file(
    tmp_path: Path, op: str, args: dict[str, Any]
) -> None:
    path = _broken(tmp_path, "scale_a = Scale(scale=2)", "scale_a = Scale(scale=2")
    before = path.read_bytes()

    response = _handle_request({"file": str(path), "op": op, "args": args})

    assert response.status == "error"
    assert path.read_bytes() == before


def test_a_file_that_parses_is_exported_as_before(tmp_path: Path) -> None:
    graph = _good(tmp_path)

    assert "partial" not in graph
    assert "errors" not in graph
    assert all("metadata" not in node for node in graph["nodes"])
    assert drawable_source(PIPELINE) is None


# ── What cannot be drawn ───────────────────────────────────────────────


def test_a_text_that_cannot_be_repaired_gives_the_empty_graph_at_python_s_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bracket is opened on line 32 and the statement runs on: Python
    # names the line it was opened on, libcst the end of the file.
    path = _broken(
        tmp_path, "scale_a = Scale(scale=2)", "scale_a = Scale(\n        scale=2,"
    )
    monkeypatch.setattr(
        "wfpy._drawable_source.repair_source",
        lambda text: RepairedSource(text=text, faults=(), parsed=False),
    )

    graph = _export(path)

    assert (graph["nodes"], graph["edges"], graph["partial"]) == ([], [], True)
    assert graph["errors"] == [_python_stops_at(path)]
    assert graph["errors"][0]["location"]["line"] == 32


def test_a_repaired_text_with_no_such_workflow_gives_the_empty_graph_and_the_faults(
    tmp_path: Path,
) -> None:
    path = _broken(tmp_path, "scale_a = Scale(scale=2)", "scale_a = Scale(scale=2")

    graph = _export(path, workflow="not_there")

    assert (graph["nodes"], graph["edges"], graph["partial"]) == ([], [], True)
    assert graph["errors"] == [_python_stops_at(path)]


def test_a_definition_header_is_reported_on_its_own_line(tmp_path: Path) -> None:
    # libcst names the line after this one; the file has no workflow, so the
    # graph is empty, and the error is where Python puts it.
    path = tmp_path / "wf.py"
    path.write_text("def f(:\n    x = 1\n", encoding="utf-8")

    graph = _export(path)

    assert (graph["nodes"], graph["edges"]) == ([], [])
    assert graph["errors"][0]["location"]["line"] == 1
    assert graph["errors"] == [_python_stops_at(path)]


# ── More than one file ─────────────────────────────────────────────────

CHILD = '''from wfpy import Port, action, connect, task, workflow


@task
class Double:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")
        Spare = Port[str](direction="in")

    @action
    def fire(self, In: int) -> int:
        return In * 2


@workflow(inputs={"X": int}, outputs={"Y": int})
def doubled():
    double = Double()
    connect("X", double.In)
    connect(double.Out, "Y")
'''

PARENT = '''from wfpy import connect, workflow

from child import Double, doubled


@workflow(inputs={"In": int}, outputs={"Out": int})
def twice():
    first = doubled()
    last = Double()
    connect("In", first.X)
    connect(first.Y, last.In)
    connect(last.Out, "Out")
'''


def test_a_fault_in_a_nested_file_is_that_file_s_and_the_parent_is_drawn(tmp_path: Path) -> None:
    (tmp_path / "parent.py").write_text(PARENT, encoding="utf-8")
    child = tmp_path / "child.py"
    child.write_text(CHILD.replace('connect(double.Out, "Y")', 'connect(double.Out, "Y"'))

    graph = _export(tmp_path / "parent.py")

    # The parent parses: it is whole, and nothing of it is at fault. It was
    # an empty graph with the child's fault reported at a line of the parent.
    assert [node["id"] for node in graph["nodes"]] == [
        "wf:input:In",
        "wf:output:Out",
        "first",
        "last",
    ]
    assert len(graph["edges"]) == 3
    assert "partial" not in graph
    assert "errors" not in graph

    nested = graph["children"][0]["graph"]
    assert [node["id"] for node in nested["nodes"]] == ["wf:input:X", "wf:output:Y", "double"]
    assert len(nested["edges"]) == 2
    assert nested["partial"] is True
    assert nested["errors"] == [_python_stops_at(child)]
    assert [edge["to"]["portId"] for edge in nested["edges"] if "meta" in edge] == ["wf:Y"]


def test_a_class_of_a_file_with_a_fault_still_has_the_ports_it_declares(tmp_path: Path) -> None:
    (tmp_path / "parent.py").write_text(PARENT, encoding="utf-8")
    (tmp_path / "child.py").write_text(CHILD.replace("return In * 2", "return In *"))

    graph = _export(tmp_path / "parent.py")

    last = next(node for node in graph["nodes"] if node["id"] == "last")
    # `Spare` is on no connection: it is read from the class, in a file that
    # does not parse.
    assert [port["name"] for port in last["ports"]] == ["In", "Out", "Spare"]
    assert last["ports"][2]["location"] == {"file": str(tmp_path / "child.py"), "line": 9}


def test_mark_faults_leaves_the_graph_of_another_file_alone(tmp_path: Path) -> None:
    repaired = repair_source("x = (\n")
    assert repaired.faults
    graph: dict[str, Any] = {
        "file": str(tmp_path / "a.py"),
        "nodes": [{"id": "n", "location": {"file": str(tmp_path / "a.py"), "line": 1}}],
        "edges": [],
        "children": [
            {
                "graph": {
                    "file": str(tmp_path / "b.py"),
                    "nodes": [{"id": "m", "location": {"file": str(tmp_path / "b.py"), "line": 1}}],
                    "edges": [],
                }
            }
        ],
    }

    mark_faults(graph, repaired.faults, str(tmp_path / "a.py"))

    assert graph["nodes"][0]["metadata"]["isErrored"] is True
    assert "metadata" not in graph["children"][0]["graph"]["nodes"][0]
    assert "partial" not in graph["children"][0]["graph"]


# ── What is kept of a replaced statement ───────────────────────────────


@pytest.mark.parametrize(
    ("statement", "kept"),
    [
        ("scale_a = Scale(scale=2", "scale_a = Scale()"),
        ("stream = wfpy.loop([1, 2", "stream = wfpy.loop()"),
        ("a, b = A(), B(", "a, b = A(), B()"),
        ("a b = A(), B()", "a, b = A(), B()"),
        ("a, b = A() B()", "a, b = A(), B()"),
        ("a, b, c = A(f=str(N)), B(), C() +", "a, b, c = A(), B(), C()"),
        ('x = Foo(")"', "x = Foo()"),
        ("connect(a.Out, b.In", "connect(a.Out, b.In)"),
        ("connect(a.Out b.In)", "connect(a.Out, b.In)"),
        ('connect("In", t.In', 'connect("In", t.In)'),
        ("wfpy.connect(a.Out, 'Out'  # the result", "wfpy.connect(a.Out, 'Out')"),
        ("connect(a.Out, b.In, capacity=", "connect(a.Out, b.In)"),
        # Not one call a name: which name is which is not known.
        ("a, b = A(f=str(N), B()", None),
        ("a, b = A()", None),
        # An end that stops short, a name that is none, a statement that is
        # no node and no connection.
        ("connect(a.Out, b.", None),
        ("connect(a.Out,", None),
        ("class = Foo(", None),
        ("x == Foo(", None),
        ("x = not Foo(", None),
        ("x = y", None),
        ("self.x = Foo(", None),
        ("x: int = Foo(", None),
        ('In = Port[int](direction="in"', None),
    ],
)
def test_what_is_kept_of_a_replaced_statement(statement: str, kept: str | None) -> None:
    assert _kept_statement(statement) == kept


@pytest.mark.parametrize(
    ("header", "kept"),
    [
        ("with stream", "with stream:"),
        ("with branch.then  # the taken side", "with branch.then:"),
        ("with open(x) as f", None),
        ("with stream as s", None),
        ("with a, b", None),
    ],
)
def test_what_is_kept_of_a_replaced_with_header(header: str, kept: str | None) -> None:
    assert _kept_header(header) == kept


def test_the_drawable_text_parses_and_has_the_lines_of_the_file() -> None:
    broken = PIPELINE.replace("scale_a = Scale(scale=2)", "scale_a = Scale(scale=2")

    drawable = drawable_source(broken)

    assert drawable is not None
    ast.parse(drawable.text)
    was, now = broken.split("\n"), drawable.text.split("\n")
    assert len(now) == len(was)
    assert [i + 1 for i, (a, b) in enumerate(zip(was, now)) if a != b] == [32]
    assert now[31] == "    scale_a = Scale()"
    assert syntax_fault(broken) is not None
    assert syntax_fault(drawable.text) is None


# ── Every example, broken ──────────────────────────────────────────────

FAULTS = {
    "bracket not closed": lambda s: s[:-1] if s[-1] in ")]}" else None,
    "colon missing": lambda s: s[:-1] if s[-1] == ":" else None,
    "comma missing": lambda s: s.replace(",", "", 1) if "," in s else None,
    "operator left hanging": lambda s: s + " +" if s[-1] not in "([{:,\\" else None,
    "indent off by one": lambda s: s[1:] if s.startswith("    ") else None,
}


def _last_fault(text: str, kind: str) -> str | None:
    """`text` with the last line `kind` can break broken, so that it does
    not parse: the workflow of an example is at the end of its file."""
    lines = text.split("\n")
    for at in range(len(lines) - 1, -1, -1):
        line = lines[at].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        broken = FAULTS[kind](line)
        if broken is None:
            continue
        candidate = "\n".join(lines[:at] + [broken] + lines[at + 1 :])
        try:
            ast.parse(candidate)
        except SyntaxError:
            return candidate
    return None


@pytest.mark.parametrize("kind", sorted(FAULTS))
@pytest.mark.parametrize("example", EXAMPLE_FILES, ids=lambda path: path.stem)
def test_an_example_with_a_fault_keeps_the_nodes_and_edges_it_had(
    example: Path, kind: str, tmp_path: Path
) -> None:
    text = example.read_text(encoding="utf-8")
    good = _export(example)
    if not good["nodes"]:
        pytest.skip("no workflow of its own to draw")
    broken = _last_fault(text, kind)
    if broken is None:
        pytest.skip(f"no line of this file can be broken this way: {kind}")
    path = tmp_path / example.name
    path.write_text(broken, encoding="utf-8")

    graph = _export(path)

    nodes, edges = _shape(graph)
    good_nodes, good_edges = _shape(good)
    assert set(nodes) == set(good_nodes)
    assert [edge[:2] for edge in edges] == [edge[:2] for edge in good_edges]
    assert graph["partial"] is True
    assert graph["errors"] and graph["errors"][0]["code"] == "syntax_error"
    assert path.read_text(encoding="utf-8") == broken
