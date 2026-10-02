"""``wfpy plan --format graph --hierarchy``: a workflow and every nested instance.

An IDE navigating a workflow hierarchy in one editor reads every view from one
export. Each nested graph is elaborated from the instance its parent built --
factory parameters and all -- as a run elaborates it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from wfpy import Port, action, connect, task, workflow
from wfpy._hierarchy_export import export_hierarchy


@task
class Scale:
    factor: int

    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def go(self, x: int) -> int:
        return x * self.factor


def scaled(factor: int):
    @workflow(inputs={"In": int}, outputs={"Out": int})
    def stage():
        s = Scale(factor=factor)
        connect("In", s.In)
        connect(s.Out, "Out")

    return stage


@workflow(inputs={"In": int}, outputs={"Out": int})
def block():
    a = scaled(2)()
    b = scaled(3)()
    connect("In", a.In)
    connect(a.Out, b.In)
    connect(b.Out, "Out")


@workflow(inputs={"In": int}, outputs={"Out": int})
def top():
    b1 = block()
    b2 = block()
    connect("In", b1.In)
    connect(b1.Out, b2.In)
    connect(b2.Out, "Out")


def _factor(entry: dict) -> str:
    (node,) = [n for n in entry["graph"]["graph"]["nodes"] if n["label"] == "s"]
    (param,) = [p for p in node["meta"]["parameters"] if p["name"] == "factor"]
    return str(param["value"])


def test_every_instance_is_exported_at_its_path() -> None:
    exported = export_hierarchy(top._wfpy_workflow)
    root = exported["hierarchy"]

    # The root's graph is the export's own, as --format graph gives it.
    assert "graph" in exported and "graph" not in root
    assert root["path"] == [] and root["workflowName"] == "top"
    assert [c["path"] for c in root["children"]] == [["b1"], ["b2"]]
    assert [c["path"] for c in root["children"][0]["children"]] == [["b1", "a"], ["b1", "b"]]
    leaf = root["children"][1]["children"][0]
    assert leaf["workflowName"] == "stage" and leaf["nodeCount"] == 1


def test_each_instance_is_elaborated_as_its_parent_built_it() -> None:
    root = export_hierarchy(top._wfpy_workflow)["hierarchy"]
    a, b = root["children"][0]["children"]

    # Two instances of one factory's workflow, each with its own parameter --
    # which a standalone `plan --workflow stage` could not know.
    assert (_factor(a), _factor(b)) == ("2", "3")


def test_each_instance_says_where_it_is_and_what_defines_it() -> None:
    root = export_hierarchy(top._wfpy_workflow, "/w/top.py")["hierarchy"]
    b1 = root["children"][0]

    assert root["sourcePath"] == "/w/top.py"
    assert b1["sourcePath"] == __file__
    assert b1["nodeId"].endswith(":b1")
    node_ids = {n["id"] for n in export_hierarchy(top._wfpy_workflow)["graph"]["nodes"]}
    assert b1["nodeId"] in node_ids


def test_an_instance_that_does_not_elaborate_is_reported_not_fatal() -> None:
    @workflow(outputs={"Out": int})
    def broken():
        raise ValueError("cannot build")

    @workflow(outputs={"Out": int})
    def holder():
        x = broken()
        connect(x.Out, "Out")

    root = export_hierarchy(holder._wfpy_workflow)["hierarchy"]
    (child,) = root["children"]
    assert "cannot build" in child["error"]
    assert "graph" not in child


def test_the_cli_exports_the_hierarchy(tmp_path: Path) -> None:
    flow = tmp_path / "flow.py"
    flow.write_text(Path(__file__).read_text().split("def _factor")[0])
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from wfpy.cli import main; main()",
            "plan",
            str(flow),
            "--workflow",
            "top",
            "--format",
            "graph",
            "--hierarchy",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert out.returncode == 0, out.stderr
    exported = json.loads(out.stdout)
    assert [c["path"] for c in exported["hierarchy"]["children"]] == [["b1"], ["b2"]]
    assert exported["hierarchy"]["sourcePath"] == str(flow.resolve())
