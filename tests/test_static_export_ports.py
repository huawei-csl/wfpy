"""The static graph of a file is one a reader can draw: nodes, ports, edges.

`wfpy.exportWorkflowGraph` reads the file and runs nothing, so it is what an
IDE falls back on when the file does not import. It gave the nodes of a
workflow and the edges between them, but no port for an edge to end on, no
node for the workflow's own inputs and outputs, and no node written in a
tuple assignment. These tests hold what it gives now, and hold it against the
graph the runtime builds for every example.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import libcst as cst
import pytest

from wfpy._graph_export import export_graph_json
from wfpy._project_index import ProjectIndex, declared_ports
from wfpy.cli import _find_workflows, _load_module
from wfpy.rewrite import RewriteEngine
from wfpy.runner import _build_workflow_graph
from wfpy.sidecar import _handle_request

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("[0-9][0-9]_*.py")) + sorted(
    (EXAMPLES_DIR / "advanced").glob("*.py")
)

PIPELINE = '''
from wfpy import Port, action, connect, task, workflow


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


@workflow(inputs={"A": int, "B": int, "Spare": str}, outputs={"Out": int})
def scaled_sum() -> None:
    scale_a = Scale(scale=2)
    scale_b = Scale(scale=3)
    add = Add()
    connect("A", scale_a.In)
    connect("B", scale_b.In)
    connect(scale_a.Out, add.A)
    connect(scale_b.Out, add.B)
    connect(add.Out, "Out")
'''


def _engine(tmp_path: Path, source: str, name: str = "wf.py") -> RewriteEngine:
    file_path = tmp_path / name
    file_path.write_text(source, encoding="utf-8")
    return RewriteEngine(str(file_path), cst.parse_module(source), source)


def _node(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    found = [node for node in graph["nodes"] if node["id"] == node_id]
    assert len(found) == 1, f"{node_id}: {[node['id'] for node in graph['nodes']]}"
    return found[0]


def _ports(node: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(port["name"], port["direction"], port["type"]) for port in node["ports"]]


def _loose_ends(graph: dict[str, Any]) -> list[str]:
    """The ends of edges that no port of a node carries."""
    known = {port["id"] for node in graph["nodes"] for port in node["ports"]}
    return [
        end["portId"]
        for edge in graph["edges"]
        for end in (edge["from"], edge["to"])
        if end["portId"] not in known
    ]


# ── Ports of a node ──────────────────────────────────────────────────────


def test_a_node_has_the_ports_its_class_declares(tmp_path: Path) -> None:
    graph = _engine(tmp_path, PIPELINE).export_workflow_graph(workflow="scaled_sum")

    assert _ports(_node(graph, "scale_a")) == [("In", "in", "int"), ("Out", "out", "int")]
    assert _ports(_node(graph, "add")) == [
        ("A", "in", "int"),
        ("B", "in", "int"),
        ("Out", "out", "int"),
    ]
    # Two instances of one class have each their own ports.
    assert [port["id"] for port in _node(graph, "scale_b")["ports"]] == [
        "n:scale_b:p:In",
        "n:scale_b:p:Out",
    ]


def test_a_port_is_the_one_its_edges_end_on_and_says_where_it_is_declared(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, PIPELINE)
    graph = engine.export_workflow_graph(workflow="scaled_sum")

    assert _loose_ends(graph) == []
    port = _node(graph, "add")["ports"][0]
    assert port == {
        "id": "n:add:p:A",
        "name": "A",
        "direction": "in",
        "type": "int",
        "role": "data",
        "location": {"file": engine.file_path, "line": 21},
    }


def test_a_direction_the_source_leaves_open_is_settled_by_name_as_the_runtime_does(
    tmp_path: Path,
) -> None:
    source = '''
from wfpy import Port, connect, task, workflow


@task
class Mixed:
    class Ports:
        In = Port[int]()
        Result = Port[str]()
        summary = Port[str]
        Named = Port[int]("Output")
        Renamed = Port[int](name="Feed", direction="out")
        _hidden = Port[int]()
        limit = 3


@workflow
def flow() -> None:
    mixed = Mixed()
'''
    graph = _engine(tmp_path, source).export_workflow_graph(workflow="flow")

    assert _ports(_node(graph, "mixed")) == [
        ("In", "in", "int"),
        ("Result", "out", "str"),
        ("summary", "out", "str"),
        ("Output", "out", "int"),
        ("Feed", "out", "int"),
    ]
    # The id is by the attribute a `connect` writes, not by the port's name.
    assert _node(graph, "mixed")["ports"][3]["id"] == "n:mixed:p:Named"


def test_a_class_with_no_ports_of_its_own_has_its_base_s(tmp_path: Path) -> None:
    source = '''
from wfpy import Port, task, workflow


@task
class Base:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")


@task
class Same(Base):
    pass


@task
class More(Base):
    class Ports(Base.Ports):
        Out = Port[str](direction="out")
        Log = Port[str](direction="out")


@task
class Own(Base):
    class Ports:
        Only = Port[int](direction="in")


@workflow
def flow() -> None:
    same = Same()
    more = More()
    own = Own()
'''
    graph = _engine(tmp_path, source).export_workflow_graph(workflow="flow")

    assert _ports(_node(graph, "same")) == [("In", "in", "int"), ("Out", "out", "int")]
    assert _ports(_node(graph, "more")) == [
        ("In", "in", "int"),
        ("Out", "out", "str"),
        ("Log", "out", "str"),
    ]
    assert _ports(_node(graph, "own")) == [("Only", "in", "int")]


def test_the_ports_of_a_class_imported_from_another_file_are_read_there(
    tmp_path: Path,
) -> None:
    (tmp_path / "parts.py").write_text(
        '''
from wfpy import Port, task


@task
class Scale:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[float](direction="out")
''',
        encoding="utf-8",
    )
    source = '''
import parts
from parts import Scale as Scaler
from wfpy import connect, workflow


@workflow(inputs={"In": int}, outputs={"Out": float})
def flow() -> None:
    one = Scaler()
    two = parts.Scale()
    connect("In", one.In)
    connect(one.Out, two.In)
    connect(two.Out, "Out")
'''
    graph = _engine(tmp_path, source).export_workflow_graph(workflow="flow")

    for name in ("one", "two"):
        node = _node(graph, name)
        assert _ports(node) == [("In", "in", "int"), ("Out", "out", "float")]
        assert node["ports"][0]["location"]["file"] == str((tmp_path / "parts.py").resolve())
    assert _loose_ends(graph) == []


def test_a_nested_workflow_and_a_control_construct_have_their_own_ports(
    tmp_path: Path,
) -> None:
    source = '''
from wfpy import Port, connect, loop, task, workflow


@task
class Double:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")


@workflow(inputs={"X": int}, outputs={"Y": int})
def inner() -> None:
    double = Double()
    connect("X", double.In)
    connect(double.Out, "Y")


def make(scale: int = 2):
    @workflow(inputs={"In": int}, outputs={"Out": list[int]})
    def built() -> None:
        pass

    return built


@workflow(outputs={"Out": int})
def outer() -> None:
    stream = loop([1, 2, 3])
    child = inner()
    made = make(scale=3)()
    not_made = make(scale=3)
    with stream:
        connect(stream.item, child.X)
    connect(child.Y, "Out")
'''
    graph = _engine(tmp_path, source).export_workflow_graph(workflow="outer")

    assert _ports(_node(graph, "child")) == [("X", "in", "int"), ("Y", "out", "int")]
    # A factory's call is the workflow it builds; the factory alone is a
    # helper of the file, read through, and no node.
    assert _ports(_node(graph, "made")) == [("In", "in", "int"), ("Out", "out", "list[int]")]
    assert "not_made" not in [node["id"] for node in graph["nodes"]]
    stream = _node(graph, "stream")
    assert _ports(stream) == [
        ("iter", "in", "iterable"),
        ("item", "out", "any"),
        ("out", "out", "any"),
    ]
    assert {port["role"] for port in stream["ports"]} == {"control"}
    # It is shown by the name the runtime gives it; an op still goes by `id`.
    assert stream["label"] == "loop"
    assert "label" not in _node(graph, "child")
    assert _loose_ends(graph) == []
    # The child's own graph is one a reader can draw too.
    child_graph = graph["children"][0]["graph"]
    assert _loose_ends(child_graph) == []
    assert [node["id"] for node in child_graph["nodes"]] == ["wf:input:X", "wf:output:Y", "double"]


def test_a_port_only_a_connection_names_is_there_for_its_edge(tmp_path: Path) -> None:
    source = '''
from somewhere_else import Remote
from wfpy import Port, connect, task, workflow


@task
class Add:
    class Ports:
        A = Port[int](direction="in")
        Out = Port[int](direction="out")


@workflow(inputs={"In": int})
def flow() -> None:
    remote = Remote()
    add = Add()
    connect("In", remote.Feed)
    connect(remote.Value, add.A)
    connect(remote.Value, add.Missing)
'''
    graph = _engine(tmp_path, source).export_workflow_graph(workflow="flow")

    # A class the project does not hold: what the connections name, each once.
    assert _ports(_node(graph, "remote")) == [("Feed", "in", "any"), ("Value", "out", "any")]
    # A declared class keeps its ports, and gets the one a connection names
    # that it does not declare: the edge is drawn, and it is not judged here.
    assert _ports(_node(graph, "add")) == [
        ("A", "in", "int"),
        ("Out", "out", "int"),
        ("Missing", "in", "any"),
    ]
    assert _loose_ends(graph) == []


# ── The workflow's own inputs and outputs ────────────────────────────────


def test_the_workflow_s_inputs_and_outputs_are_nodes_ahead_of_its_own(tmp_path: Path) -> None:
    engine = _engine(tmp_path, PIPELINE)
    graph = engine.export_workflow_graph(workflow="scaled_sum")

    # In the order of the runtime graph: inputs by name, outputs by name,
    # then the instances as written.
    assert [node["id"] for node in graph["nodes"]] == [
        "wf:input:A",
        "wf:input:B",
        "wf:input:Spare",  # declared and not connected: still a port of the workflow
        "wf:output:Out",
        "scale_a",
        "scale_b",
        "add",
    ]
    assert _node(graph, "wf:input:A") == {
        "id": "wf:input:A",
        "kind": "wf-input",
        "label": "A",
        "scope": "scope:root",
        "nodeId": "wf:input:A",
        "location": {"file": engine.file_path, "line": 30, "column": 19},
        "ports": [
            {
                "id": "wf:A",
                "name": "A",
                # an input of the workflow is where tokens come out, inside it
                "direction": "out",
                "type": "int",
                "role": "data",
                "location": {"file": engine.file_path, "line": 30, "column": 19},
            }
        ],
    }
    out = _node(graph, "wf:output:Out")
    assert (out["kind"], out["label"], _ports(out)) == ("wf-output", "Out", [("Out", "in", "int")])
    assert _ports(_node(graph, "wf:input:Spare")) == [("Spare", "out", "str")]


def test_a_workflow_port_only_a_connection_names_is_a_node_too(tmp_path: Path) -> None:
    source = '''
from wfpy import Port, connect, task, workflow


@task
class Echo:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")


@workflow
def flow() -> None:
    echo = Echo()
    with_side = Echo()
    connect("Feed", echo.In)
    connect("Feed", with_side.In)
    connect(echo.Out, "Result")
'''
    graph = _engine(tmp_path, source).export_workflow_graph(workflow="flow")

    assert [node["id"] for node in graph["nodes"]] == [
        "wf:input:Feed",
        "wf:output:Result",
        "echo",
        "with_side",
    ]
    assert _ports(_node(graph, "wf:input:Feed")) == [("Feed", "out", "any")]
    assert "location" not in _node(graph, "wf:input:Feed")
    assert _loose_ends(graph) == []


def test_what_the_export_gave_before_is_as_it_was(tmp_path: Path) -> None:
    graph = _engine(tmp_path, PIPELINE).export_workflow_graph(workflow="scaled_sum")

    node = _node(graph, "scale_a")
    kept = ("id", "ref", "kind", "args", "kwargs", "scope", "nodeId")
    assert {key: node[key] for key in kept} == {
        "id": "scale_a",
        "ref": "Scale",
        "kind": "task",
        "args": [],
        "kwargs": {"scale": "2"},
        "scope": "scope:root",
        "nodeId": "n:scale_a",
    }
    edge = graph["edges"][2]
    assert edge["from"] == {
        "kind": "node",
        "nodeId": "scale_a",
        "port": "Out",
        "ref": "Scale",
        "portId": "n:scale_a:p:Out",
    }
    assert edge["edgeId"] == "e:n:scale_a:p:Out->n:add:p:A"
    # The names an op goes by are the instances, not the workflow's ports.
    engine = _engine(tmp_path, PIPELINE)
    assert engine.list_instance_names(workflow="scaled_sum") == ["add", "scale_a", "scale_b"]


# ── Nodes written in a tuple assignment ──────────────────────────────────

TUPLES = '''
from wfpy import Port, connect, task, workflow


@task
class Author:
    class Ports:
        Text = Port[str](direction="out")


@task
class Reader:
    limit: int

    class Ports:
        Text = Port[str](direction="in")
        Answer = Port[int](direction="out")


@workflow(outputs={"Answer": int})
def flow() -> None:
    author, reader = Author(), Reader(limit=3)
    [first, second] = [Author(), Reader(limit=1)]
    connect(author.Text, reader.Text)
    connect(reader.Answer, "Answer")
'''


def test_a_node_written_in_a_tuple_assignment_is_a_node(tmp_path: Path) -> None:
    engine = _engine(tmp_path, TUPLES)
    graph = engine.export_workflow_graph(workflow="flow")

    assert [node["id"] for node in graph["nodes"]][1:] == ["author", "reader", "first", "second"]
    reader = _node(graph, "reader")
    assert (reader["ref"], reader["kwargs"]) == ("Reader", {"limit": "3"})
    # Each is placed at its own call, not at the start of the line they share.
    assert _node(graph, "author")["location"]["column"] == 22
    assert reader["location"]["column"] == 32
    assert _ports(reader) == [("Text", "in", "str"), ("Answer", "out", "int")]
    assert _loose_ends(graph) == []
    assert engine.instance_types(workflow="flow") == {
        "author": "Author",
        "reader": "Reader",
        "first": "Author",
        "second": "Reader",
    }


@pytest.mark.parametrize(
    "line",
    [
        "author, *rest = Author(), Reader(limit=3), Reader(limit=4)",
        "author, reader = make_pair()",
        "author, reader = [Author(), Reader(limit=3)][::-1]",
        "author = reader = Author()",
        "pair = Author(), Reader(limit=3)",
    ],
)
def test_an_assignment_that_does_not_say_which_call_a_name_takes_is_no_node(
    tmp_path: Path, line: str
) -> None:
    source = TUPLES.replace("author, reader = Author(), Reader(limit=3)", line).replace(
        "    connect(author.Text, reader.Text)\n    connect(reader.Answer, \"Answer\")\n", ""
    )
    graph = _engine(tmp_path, source).export_workflow_graph(workflow="flow")

    assert "author" not in [node["id"] for node in graph["nodes"]]
    assert "reader" not in [node["id"] for node in graph["nodes"]]


def test_a_connection_to_a_node_of_a_tuple_assignment_is_checked_and_written(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "wf.py"
    file_path.write_text(TUPLES, encoding="utf-8")

    def ask(op: str, **args: Any) -> Any:
        return _handle_request(
            {"file": str(file_path), "op": f"wfpy.{op}", "args": {"workflow": "flow", **args}}
        )

    # They were "unknown node": the workflow did not know the names it binds.
    assert ask("checkConnection", source="first.Text", target="second.Text").status == "ok"
    refused = ask("checkConnection", source="first.Text", target="second.Answer")
    assert refused.status == "error" and "not an input" in refused.message
    assert ask("connect", from_expr="first.Text", to_expr="second.Text").status == "ok"
    assert "    connect(first.Text, second.Text)\n" in file_path.read_text(encoding="utf-8")


# ── The file as the caller holds it ──────────────────────────────────────


def test_a_place_in_this_file_is_given_by_the_path_the_engine_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "wf.py").write_text(PIPELINE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    engine = RewriteEngine("wf.py", cst.parse_module(PIPELINE), PIPELINE)

    graph = engine.export_workflow_graph(workflow="scaled_sum")

    add = _node(graph, "add")
    assert add["location"]["file"] == "wf.py"
    assert {port["location"]["file"] for port in add["ports"]} == {"wf.py"}


def test_the_ports_are_read_from_the_text_the_engine_holds_not_from_disk(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "wf.py"
    file_path.write_text("this is not python (\n", encoding="utf-8")
    engine = RewriteEngine(str(file_path), cst.parse_module(PIPELINE), PIPELINE)

    graph = engine.export_workflow_graph(workflow="scaled_sum")

    assert _ports(_node(graph, "add")) == [
        ("A", "in", "int"),
        ("B", "in", "int"),
        ("Out", "out", "int"),
    ]
    assert _loose_ends(graph) == []


def test_declared_ports_says_none_for_what_the_project_does_not_define(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "wf.py"
    file_path.write_text(PIPELINE + "\nimport os\nfrom json import loads\n", encoding="utf-8")
    index = ProjectIndex.for_file(file_path)

    assert declared_ports(index, file_path, "loads") is None
    assert declared_ports(index, file_path, "os.getcwd") is None
    assert declared_ports(index, file_path, "nowhere") is None
    assert declared_ports(index, file_path, "connect") is None
    assert declared_ports(index, file_path, "Scale(1).x") is None
    assert declared_ports(index, file_path, "Scale(1)") is None  # an instance called
    assert declared_ports(index, file_path, "scaled_sum(") is None
    assert declared_ports(index, file_path, "") is None
    # A class with no `Ports` is known, and has none.
    file_path.write_text("class Plain:\n    pass\n", encoding="utf-8")
    assert declared_ports(ProjectIndex.for_file(file_path), file_path, "Plain") == []


# ── A file that does not import ──────────────────────────────────────────


def test_a_file_that_does_not_import_is_still_drawn_with_its_ports_and_edges(
    tmp_path: Path,
) -> None:
    """What an IDE falls back on when `wfpy plan` cannot import the file: a
    misspelt name in one task, and the rest of the workflow is all there."""
    broken = PIPELINE.replace('B = Port[int](direction="in")', 'B = Prot[int](direction="in")')
    file_path = tmp_path / "wf.py"
    file_path.write_text(broken, encoding="utf-8")
    with pytest.raises(NameError):
        _load_module(str(file_path))

    response = _handle_request(
        {"file": str(file_path), "op": "wfpy.exportWorkflowGraph", "args": {}}
    )

    assert response.status == "ok"
    assert response.diagnostic is not None
    graph = response.diagnostic["graph"]
    assert [node["id"] for node in graph["nodes"]] == [
        "wf:input:A",
        "wf:input:B",
        "wf:input:Spare",
        "wf:output:Out",
        "scale_a",
        "scale_b",
        "add",
    ]
    assert _loose_ends(graph) == []
    # The port of the misspelt line is not read as a port; the connection to
    # it still has one to end on.
    assert _ports(_node(graph, "add")) == [
        ("A", "in", "int"),
        ("Out", "out", "int"),
        ("B", "in", "any"),
    ]


# ── Against the runtime, on every example ────────────────────────────────


def _drawing(nodes: list[dict[str, Any]], edges: list[tuple[str, str]]) -> tuple[Any, Any]:
    """A graph as a reader draws it: each node by what it is, the name it is
    shown by and its ports with their roles; each edge by the two ports it
    joins."""
    port_at: dict[str, tuple[str, str]] = {}
    drawn = set()
    for node in nodes:
        ports = tuple(
            sorted((p["name"], p["direction"], p["type"], p["role"]) for p in node["ports"])
        )
        drawn.add((node["what"], node["name"], ports))
        for port in node["ports"]:
            port_at[port["id"]] = (node["name"], port["name"])
    return drawn, {(port_at.get(a, a), port_at.get(b, b)) for a, b in edges}


def _what(kind: str) -> str:
    return kind if kind in ("wf-input", "wf-output") else "node"


def _shown_first(nodes: list[dict[str, Any]], name_of: Any) -> list[str]:
    """The workflow's own ports as listed: a reader that gives a node its
    label as an id gives it to the first of that label, so the two graphs
    must list these alike."""
    return [name_of(n) for n in nodes if n["kind"] in ("wf-input", "wf-output")]


def _example_workflows() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for path in EXAMPLE_FILES:
        module = _load_module(str(path))
        for name, workflow in _find_workflows(module).items():
            builder = workflow._wfpy_workflow.builder_fn
            # One an example imports is the other file's, and held there.
            if builder is not None and Path(builder.__code__.co_filename) == path:
                found.append((path, name))
    return found


EXAMPLE_WORKFLOWS = _example_workflows()


def test_the_examples_have_workflows_to_hold_the_static_graph_against() -> None:
    assert len(EXAMPLE_FILES) >= 18
    assert len(EXAMPLE_WORKFLOWS) >= len(EXAMPLE_FILES) - 1  # one example is a library


@pytest.mark.parametrize(
    ("path", "workflow"), EXAMPLE_WORKFLOWS, ids=[f"{p.stem}:{w}" for p, w in EXAMPLE_WORKFLOWS]
)
def test_the_static_graph_of_an_example_is_the_graph_the_runtime_builds(
    path: Path, workflow: str
) -> None:
    """Nodes, the ports of each with direction and type, the workflow's own
    inputs and outputs, and the edges: read from the source, they are what
    building the workflow gives."""
    built = _find_workflows(_load_module(str(path)))[workflow]._wfpy_workflow
    runtime = export_graph_json(_build_workflow_graph(built))["graph"]
    from_runtime = _drawing(
        [
            {"what": _what(n["kind"]), "name": n["label"], "ports": n["ports"]}
            for n in runtime["nodes"]
        ],
        [(edge["from"], edge["to"]) for edge in runtime["edges"]],
    )

    source = path.read_text(encoding="utf-8")
    engine = RewriteEngine(str(path), cst.parse_module(source), source)
    static = engine.export_workflow_graph(workflow=workflow)
    from_source = _drawing(
        [
            {"what": _what(n["kind"]), "name": n.get("label", n["id"]), "ports": n["ports"]}
            for n in static["nodes"]
        ],
        [(edge["from"]["portId"], edge["to"]["portId"]) for edge in static["edges"]],
    )

    assert _loose_ends(static) == []
    assert from_source[0] == from_runtime[0]
    assert from_source[1] == from_runtime[1]
    boundary = _shown_first(runtime["nodes"], lambda n: n["id"])
    assert _shown_first(static["nodes"], lambda n: n["id"]) == boundary
    assert [n["id"] for n in static["nodes"]][: len(boundary)] == boundary
    assert [n["id"] for n in runtime["nodes"]][: len(boundary)] == boundary
