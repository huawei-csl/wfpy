"""Nested workflows: config inheritance, output files, source navigation."""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

import pytest

from wfpy import File, Port, Resource, action, config, connect, run, task, workflow
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
