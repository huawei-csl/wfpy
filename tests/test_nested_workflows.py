"""Nested workflows: config inheritance, output files, source navigation."""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from wfpy import File, Port, Resource, action, agent, config, connect, run, task, viewer, workflow
from wfpy.cli import cmd_plan
from wfpy.graph import export_graph_json
from wfpy.runner import _build_workflow_graph, build_plan


@task
class Leaf:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def go(self, x: int) -> int:
        return x


def test_config_reaches_every_nesting_level(tmp_path: Path) -> None:
    @workflow(inputs={"In": int}, outputs={"Out": int})
    def grandchild():
        leaf = Leaf()
        connect("In", leaf.In)
        connect(leaf.Out, "Out")

    @workflow(inputs={"In": int}, outputs={"Out": int})
    def child():
        g = grandchild()
        connect("In", g.In)
        connect(g.Out, "Out")

    @config(path=[str(tmp_path / "bin")], env={"WFPY_DEPTH": "3"})
    @workflow(inputs={"In": int}, outputs={"Out": int})
    def top():
        c = child()
        connect("In", c.In)
        connect(c.Out, "Out")

    wf_def = top._wfpy_workflow
    plan = build_plan(_build_workflow_graph(wf_def), wf_def)
    child_plan = next(a.sub_plan for a in plan.actors if a.kind == "workflow")
    grand_plan = next(a.sub_plan for a in child_plan.actors if a.kind == "workflow")

    assert grand_plan.search_paths == [str(tmp_path / "bin")]
    assert grand_plan.env["WFPY_DEPTH"] == "3"


def _answering_agents(
    monkeypatch: pytest.MonkeyPatch, answer: Any = None
) -> dict[str, dict[str, Any]]:
    """Stand in for the model. Returns the options each agent instance was
    invoked with, by instance name; `answer` turns an agent's inputs into its
    reply (default "ok")."""
    seen: dict[str, dict[str, Any]] = {}

    def fake_invoke(
        agent_spec: Any, payload_text: str, verbose: bool, **kwargs: Any
    ) -> tuple[str, list[dict[str, str]], None, dict[str, Any]]:
        payload = json.loads(payload_text)
        seen[payload["instance"]] = dict(kwargs.get("plan_options") or {})
        return (answer(payload["inputs"]) if answer else "ok"), [], None, {}

    monkeypatch.setattr("wfpy.runner._invoke_agent", fake_invoke)
    return seen


def test_the_runs_options_reach_agents_at_every_nesting_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run()` sets the options on the top plan only. An agent inside a nested
    workflow used to be invoked with none of them: not the connector, not the
    stuck window, not the path its workspace connectors are found from."""
    seen = _answering_agents(monkeypatch)

    @agent(prompt="Answer.")
    class Ask:
        class Ports:
            In = Port[str](direction="in")
            Out = Port[str](direction="out")

    @workflow(inputs={"In": str}, outputs={"Out": str})
    def grandchild():
        deep = Ask()
        connect("In", deep.In)
        connect(deep.Out, "Out")

    @workflow(inputs={"In": str}, outputs={"Out": str})
    def child():
        mid = Ask()
        g = grandchild()
        connect("In", mid.In)
        connect(mid.Out, g.In)
        connect(g.Out, "Out")

    @workflow(inputs={"In": str}, outputs={"Out": str})
    def top():
        first = Ask()
        c = child()
        connect("In", first.In)
        connect(first.Out, c.In)
        connect(c.Out, "Out")

    run(
        top,
        inputs={"In": "x"},
        out_dir=tmp_path / "out",
        acp_connector="claude",
        acp_stuck_timeout_ms=1_800_000,
        agent_tool_policy="deny",
        verbose=False,
    )

    assert set(seen) == {"first", "mid", "deep"}
    for name in ("first", "mid", "deep"):
        assert seen[name]["acp_connector"] == "claude", name
        assert seen[name]["acp_stuck_timeout_ms"] == 1_800_000, name
        assert seen[name]["agent_tool_policy"] == "deny", name
        assert seen[name]["source_path"] == str(Path(__file__).resolve()), name


def test_agents_of_two_instances_of_a_workflow_keep_their_files_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one option a nested workflow does not receive is the directory a
    top-level agent writes to: both instances hold an agent named `w`, and in
    one directory the second would write over the first one's file."""
    _answering_agents(monkeypatch, answer=lambda inputs: f"note for {inputs['In']}")

    @agent(prompt="Write it down.")
    class Note:
        class Ports:
            In = Port[str](direction="in")
            Out = Port[File](direction="out", ext=".md")

    @workflow(inputs={"In": str}, outputs={"Out": File(ext=".md")})
    def notes():
        w = Note()
        connect("In", w.In)
        connect(w.Out, "Out")

    @workflow(
        inputs={"A": str, "B": str},
        outputs={"OutA": File(ext=".md"), "OutB": File(ext=".md")},
    )
    def top():
        a = notes()
        b = notes()
        connect("A", a.In)
        connect("B", b.In)
        connect(a.Out, "OutA")
        connect(b.Out, "OutB")

    outputs = run(top, inputs={"A": "a", "B": "b"}, out_dir=tmp_path / "out", verbose=False)

    assert [Path(p).read_text() for p in outputs["OutA"]] == ["note for a"]
    assert [Path(p).read_text() for p in outputs["OutB"]] == ["note for b"]


def test_sibling_workflow_outputs_keep_their_own_files(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()

    @task
    class FireOnce:
        _done: bool = False

        class Ports:
            Out = Port[int]()

        def action(self) -> int | None:
            if self._done:
                return None
            self._done = True
            return 1

    @task
    class Start:
        class Ports:
            In = Port[int](direction="in")
            Out = Port[File](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def write(self, _trigger: int) -> str:
            path = raw / "first.raw"
            path.write_text("first")
            return str(path)

    @task
    class Follow:
        class Ports:
            In = Port[File](direction="in")
            Out = Port[File](direction="out")

        @action(consumes={"In": 1}, produces={"Out": 1})
        def write(self, _previous: str) -> str:
            path = raw / "second.raw"
            path.write_text("second")
            return str(path)

    @workflow(inputs={"In": int}, outputs={"Out": File(ext=".txt")})
    def first_child():
        start = Start()
        connect("In", start.In)
        connect(start.Out, "Out")

    @workflow(inputs={"In": File}, outputs={"Out": File(ext=".txt")})
    def second_child():
        follow = Follow()
        connect("In", follow.In)
        connect(follow.Out, "Out")

    # `first.Out` is read by the parent after `second` has finished: when the
    # two siblings' copies shared one file, "A" read "second".
    @workflow(outputs={"A": File(ext=".txt"), "B": File(ext=".txt")})
    def parent():
        src = FireOnce()
        first = first_child()
        second = second_child()
        connect(src.Out, first.In)
        connect(first.Out, second.In)
        connect(first.Out, "A")
        connect(second.Out, "B")

    outputs = run(parent, out_dir=str(tmp_path / "wf-out"), work_dir=str(tmp_path / "work"))

    a, b = Path(outputs["A"][0]), Path(outputs["B"][0])
    assert (a.name, a.read_text()) == ("A.txt", "first")
    assert (b.name, b.read_text()) == ("B.txt", "second")


def test_a_workflow_without_inputs_fires_once(tmp_path: Path) -> None:
    """A sub-workflow that takes no input runs once, and the run around it ends.

    In a child process: a regression spins forever, and only a process can be
    stopped when the time is up.
    """
    import subprocess
    import sys

    (tmp_path / "closed_wf.py").write_text(
        textwrap.dedent(
            """
            from wfpy import Port, action, connect, task, workflow


            @task
            class Emit:
                _done: bool = False

                class Ports:
                    Out = Port[int]()

                def action(self) -> int | None:
                    if self._done:
                        return None
                    self._done = True
                    return 7


            @task
            class Show:
                class Ports:
                    In = Port[int](direction="in")

                @action(consumes={"In": 1})
                def show(self, x: int) -> None:
                    print("GOT", x)


            @workflow(outputs={"Out": int})
            def closed():
                emit = Emit()
                connect(emit.Out, "Out")


            @workflow
            def outer():
                sub = closed()
                show = Show()
                connect(sub.Out, show.In)
            """
        ),
        encoding="utf-8",
    )
    script = (
        "import sys\nfrom wfpy.cli import main\n"
        "sys.argv = ['wfpy', 'run', 'closed_wf.py', '--workflow', 'outer', '--no-interactive', '--out-dir', 'wf-out']\n"
        "main()\n"
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the run never ended")
    assert done.returncode == 0, done.stderr
    assert done.stdout.count("GOT 7") == 1


def test_folder_output_is_handed_on(tmp_path: Path) -> None:
    hdl = tmp_path / "hdl"

    @task
    class MakeFolder:
        _done: bool = False

        class Ports:
            Out = Port[Resource(kind="folder")](direction="out")

        def action(self) -> str | None:
            if self._done:
                return None
            self._done = True
            hdl.mkdir()
            (hdl / "top.vhd").write_text("--")
            return str(hdl)

    # A declared folder output is not a file to copy: it used to come back as
    # a path that was never written.
    @workflow(outputs={"Rtl": Resource(kind="folder")})
    def emit():
        make = MakeFolder()
        connect(make.Out, "Rtl")

    outputs = run(emit, out_dir=str(tmp_path / "wf-out"))

    rtl = Path(outputs["Rtl"][0])
    assert rtl.is_dir()
    assert (rtl / "top.vhd").read_text() == "--"


def test_nested_workflow_node_points_at_its_instance_and_definition() -> None:
    @workflow(inputs={"In": int}, outputs={"Out": int})
    def child():
        leaf = Leaf()
        connect("In", leaf.In)
        connect(leaf.Out, "Out")

    def make_child(label: str):
        @workflow(inputs={"In": int}, outputs={"Out": int})
        def made():
            _ = label
            leaf = Leaf()
            connect("In", leaf.In)
            connect(leaf.Out, "Out")

        return made

    @workflow(inputs={"In": int}, outputs={"Out": int})
    def parent():
        c = child()
        m = make_child(label="x")()
        connect("In", c.In)
        connect(c.Out, m.In)
        connect(m.Out, "Out")

    nodes = export_graph_json(_build_workflow_graph(parent._wfpy_workflow))["graph"]["nodes"]
    meta = {
        node["id"].rsplit(":", 1)[-1]: node["meta"]
        for node in nodes
        if node.get("kind") == "workflow"
    }

    # `source` is the line in parent() that creates the instance -- the IDE
    # resolves a definition from it, in the file the diagram shows -- and
    # `referencedSource` is the workflow's own definition, not wfpy's proxy.
    here = Path(__file__).resolve()
    for name in ("c", "m"):
        assert Path(meta[name]["source"]["file"]).resolve() == here
        assert Path(meta[name]["referencedSource"]["file"]).resolve() == here
    assert meta["m"]["source"]["line"] == meta["c"]["source"]["line"] + 1
    assert (
        meta["c"]["referencedSource"]["line"]
        < meta["m"]["referencedSource"]["line"]
        < meta["c"]["source"]["line"]
    )


def test_plan_graph_draws_a_factory_workflow(tmp_path: Path) -> None:
    module = tmp_path / "factory_wf.py"
    module.write_text(
        textwrap.dedent(
            '''
            from wfpy import Port, action, connect, task, workflow


            @task
            class Scale:
                k: str

                class Ports:
                    In = Port[int](direction="in")
                    Out = Port[int](direction="out")

                @action(consumes={"In": 1}, produces={"Out": 1})
                def go(self, x: int) -> int:
                    return x


            def make_inner(k: str, mode: str = "fast"):
                @workflow(inputs={"In": int}, outputs={"Out": int})
                def inner():
                    s = Scale(k=f"{k}/{mode}")
                    connect("In", s.In)
                    connect(s.Out, "Out")

                return inner
            '''
        ),
        encoding="utf-8",
    )
    out = tmp_path / "graph.json"

    def plan_args(best_effort: bool) -> argparse.Namespace:
        return argparse.Namespace(
            file=str(module), workflow="inner", output=str(out),
            format="graph", best_effort=best_effort,
        )

    cmd_plan(plan_args(best_effort=True))
    graph = json.loads(out.read_text())["graph"]
    assert any(node["id"].endswith(":s") for node in graph["nodes"])

    # Only a drawing: without --best-effort the factory is not a workflow.
    with pytest.raises(SystemExit):
        cmd_plan(plan_args(best_effort=False))


def test_edge_and_task_sources_are_workflow_lines() -> None:
    @workflow(inputs={"In": int}, outputs={"Out": int})
    def wired():
        leaf = Leaf()
        connect("In", leaf.In)
        connect(leaf.Out, "Out")

    graph = export_graph_json(_build_workflow_graph(wired._wfpy_workflow))["graph"]
    edges = graph["edges"]
    leaf = next(node for node in graph["nodes"] if node["id"].endswith(":leaf"))["meta"]

    here = Path(__file__).resolve()
    lines = sorted(edge["source"]["line"] for edge in edges)
    assert len(edges) == 2 and lines[1] == lines[0] + 1
    assert all(Path(edge["source"]["file"]).resolve() == here for edge in edges)
    # the task is created on the line above the first connect, defined at Leaf
    assert Path(leaf["source"]["file"]).resolve() == here
    assert leaf["source"]["line"] == lines[0] - 1
    assert leaf["referencedSource"]["line"] < leaf["source"]["line"]


def test_definitions_are_their_class_or_def_line_not_a_decorator() -> None:
    # The IDE resolves "go to source" from the line it is given; on a
    # decorator line that is wfpy's `viewer` / `workflow`, not the definition.
    @viewer(inputs=["In"])
    class Shown:
        class Ports:
            In = Port[int](direction="in")

    def make_inner():
        @workflow(inputs={"In": int}, outputs={"Out": int})
        def inner():
            leaf = Leaf()
            connect("In", leaf.In)
            connect(leaf.Out, "Out")

        return inner

    @config(env={"WFPY_DEPTH": "1"})
    @workflow(inputs={"In": int}, outputs={"Out": int})
    def stacked():
        leaf = Leaf()
        connect("In", leaf.In)
        connect(leaf.Out, "Out")

    @workflow(inputs={"In": int})
    def outer():
        i = make_inner()()
        s = stacked()
        shown = Shown()
        connect("In", i.In)
        connect(i.Out, s.In)
        connect(s.Out, shown.In)

    nodes = export_graph_json(_build_workflow_graph(outer._wfpy_workflow))["graph"]["nodes"]
    lines = Path(__file__).read_text().splitlines()
    declared = {}
    for node in nodes:
        ref = (node.get("meta") or {}).get("referencedSource")
        if ref is not None:
            declared[node["label"]] = lines[ref["line"] - 1].strip()
    assert declared == {
        "i": "def inner():",
        "s": "def stacked():",
        "shown": "class Shown:",
    }


def test_a_node_given_to_connect_whole_names_its_ports() -> None:
    """``connect(a, b)`` is the usual slip: the error says which ports to name."""

    @workflow(outputs={"Out": int})
    def closed():
        leaf = Leaf()
        connect(leaf.Out, "Out")

    @task
    class Sink:
        class Ports:
            In = Port[int](direction="in")

        @action(consumes={"In": 1})
        def take(self, x: int) -> None:
            return None

    @workflow
    def node_as_target():
        first = Leaf()
        second = Leaf()
        connect(first.Out, second)

    @workflow
    def node_as_target_by_operator():
        first = Leaf()
        second = Leaf()
        first.Out >> second

    @workflow
    def workflow_as_source():
        sub = closed()
        leaf = Leaf()
        connect(sub, leaf.In)

    @workflow
    def sink_as_source():
        sink = Sink()
        leaf = Leaf()
        connect(sink, leaf.In)

    with pytest.raises(TypeError, match=r"target is the node 'second'.*\(second\.In\)"):
        _build_workflow_graph(node_as_target._wfpy_workflow)
    with pytest.raises(TypeError, match=r"target is the node 'second'.*\(second\.In\)"):
        _build_workflow_graph(node_as_target_by_operator._wfpy_workflow)
    with pytest.raises(TypeError, match=r"source is the node 'sub'.*\(sub\.Out\)"):
        _build_workflow_graph(workflow_as_source._wfpy_workflow)
    with pytest.raises(TypeError, match=r"source is the node 'sink', which has no output port"):
        _build_workflow_graph(sink_as_source._wfpy_workflow)
