"""Workflow instances: a nested workflow answered by a run that already happened."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wfpy import File, Port, action, connect, guard, run, task, workflow


def _make_child(raw: Path, builds: list[str]):
    @task
    class Produce:
        _done: bool = False

        class Ports:
            Num = Port[int](direction="out")
            Doc = Port[File](direction="out")

        @action(consumes={}, produces={"Num": 1, "Doc": 1})
        @guard(lambda self: not self._done)
        def emit(self) -> dict[str, object]:
            self._done = True
            path = raw / "doc.txt"
            path.write_text("recorded")
            return {"Num": 7, "Doc": str(path)}

    @workflow(outputs={"Num": int, "Doc": File(ext=".txt")})
    def child():
        builds.append("child")
        p = Produce()
        connect(p.Num, "Num")
        connect(p.Doc, "Doc")

    return child


@task
class Collect:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def go(self, x: int) -> int:
        return x * 10


def test_instance_emits_recorded_outputs_without_running_child(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    builds: list[str] = []
    child = _make_child(raw, builds)

    run(child, out_dir=str(tmp_path / "wf-out"), run_id="first")
    run_dir = tmp_path / "wf-out" / "first"
    assert builds == ["child"]

    @workflow(outputs={"Out": int, "Doc": File(ext=".txt")})
    def parent():
        c = child(instance=str(run_dir))
        col = Collect()
        connect(c.Num, col.In)
        connect(col.Out, "Out")
        connect(c.Doc, "Doc")

    (raw / "doc.txt").unlink()  # the instance must hand on the recorded copy
    outputs = run(parent, out_dir=str(tmp_path / "wf-out"), run_id="second")

    assert builds == ["child"]  # never rebuilt, never run
    assert outputs["Out"] == [70]  # each recorded token sent once
    assert Path(outputs["Doc"][0]).read_text() == "recorded"


def test_instance_accepts_the_run_record_itself(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    child = _make_child(raw, [])
    run(child, out_dir=str(tmp_path / "wf-out"), run_id="first")
    record = tmp_path / "wf-out" / "first" / "run.wf-run.json"

    @workflow(outputs={"Out": int})
    def parent():
        c = child(instance=str(record))
        col = Collect()
        connect(c.Num, col.In)
        connect(col.Out, "Out")

    assert run(parent, out_dir=str(tmp_path / "wf-out"))["Out"] == [70]


def test_instance_refuses_a_child_with_inputs(tmp_path: Path) -> None:
    @workflow(inputs={"In": int}, outputs={"Out": int})
    def fed():
        c = Collect()
        connect("In", c.In)
        connect(c.Out, "Out")

    @workflow(outputs={"Out": int})
    def parent():
        f = fed(instance=str(tmp_path))
        connect(f.Out, "Out")

    with pytest.raises(TypeError, match="an instance has no inputs"):
        run(parent, out_dir=str(tmp_path / "wf-out"))


def test_instance_refuses_a_run_of_another_workflow(tmp_path: Path) -> None:
    run_dir = tmp_path / "other"
    run_dir.mkdir()
    (run_dir / "run.wf-run.json").write_text(
        json.dumps({"workflowName": "someone_else", "outputs": {"Num": [1]}})
    )
    child = _make_child(tmp_path, [])

    @workflow(outputs={"Out": int})
    def parent():
        c = child(instance=str(run_dir))
        connect(c.Num, "Out")

    with pytest.raises(ValueError, match="is of 'someone_else'"):
        run(parent, out_dir=str(tmp_path / "wf-out"))


def test_instance_without_a_run_record(tmp_path: Path) -> None:
    child = _make_child(tmp_path, [])

    @workflow(outputs={"Out": int})
    def parent():
        c = child(instance=str(tmp_path / "missing"))
        connect(c.Num, "Out")

    with pytest.raises(FileNotFoundError, match="run.wf-run.json"):
        run(parent, out_dir=str(tmp_path / "wf-out"))


def test_instance_only_nested(tmp_path: Path) -> None:
    child = _make_child(tmp_path, [])
    with pytest.raises(TypeError, match="only meaningful nested"):
        child(instance=str(tmp_path))


# ── The IDE's side: discovering runs, writing the node, drawing it ──────────

CHILD_SOURCE = """
from __future__ import annotations

from wfpy import Port, action, connect, guard, task, workflow


@task
class Produce:
    _done: bool = False

    class Ports:
        Num = Port[int](direction="out")

    @action(consumes={}, produces={"Num": 1})
    @guard(lambda self: not self._done)
    def emit(self) -> int:
        self._done = True
        return 7


@task
class Pass:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def go(self, x: int) -> int:
        return x


@workflow(outputs={"Num": int})
def child():
    p = Produce()
    connect(p.Num, "Num")


@workflow(inputs={"In": int}, outputs={"Out": int})
def fed():
    p = Pass()
    connect("In", p.In)
    connect(p.Out, "Out")
""".lstrip()

PARENT_SOURCE = '''
"""A parent that nests a past run."""
from __future__ import annotations

from wfpy import workflow


@workflow(outputs={"Out": int})
def parent():
    pass
'''.lstrip()


def _load(path: Path, name: str):
    import importlib.util
    import sys

    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sys

    root = tmp_path / "proj"
    flows = root / "flows"
    flows.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'proj'\n")
    child_file = flows / "child.py"
    child_file.write_text(CHILD_SOURCE)
    parent_file = flows / "parent.py"
    parent_file.write_text(PARENT_SOURCE)

    mod = _load(child_file, "child")
    run(mod.child, out_dir=str(root / "wf-out"), run_id="r1", source_path=str(child_file))
    run(mod.fed, {"In": 1}, out_dir=str(root / "wf-out"), run_id="r2", source_path=str(child_file))
    yield root, parent_file
    sys.modules.pop("child", None)
    sys.modules.pop("parent", None)


def test_discovery_offers_runs_of_workflows_without_inputs(project) -> None:
    from wfpy._workflow_instance import discover_workflow_instances

    _root, parent_file = project
    candidates = discover_workflow_instances(parent_file)

    assert [c["type"] for c in candidates] == ["child"]  # `fed` takes inputs
    (only,) = candidates
    assert only["value"] == "../wf-out/r1"
    assert only["nodeArgs"] == {"importFrom": "child"}
    assert only["detail"] == "outputs: Num"


def test_sidecar_lists_instances_and_writes_the_node(project, tmp_path, monkeypatch) -> None:
    from wfpy.graph import export_graph_json
    from wfpy.runner import _build_workflow_graph
    from wfpy.sidecar import _handle_request

    root, parent_file = project
    listed = _handle_request({"file": str(parent_file), "op": "wfpy.listWorkflowInstances"})
    assert listed.status == "ok"
    (candidate,) = listed.diagnostic["candidates"]

    created = _handle_request(
        {
            "file": str(parent_file),
            "op": "wfpy.createNode",
            "args": {
                "workflow": "parent",
                "type": candidate["type"],
                "name": "c",
                "params": {"instance": candidate["value"]},
                **candidate["nodeArgs"],
            },
        }
    )
    assert created.status == "ok", created.message
    text = parent_file.read_text()
    assert text.index("from __future__") < text.index("from child import child")
    assert "c = child(instance='../wf-out/r1')" in text

    # The relative run resolves against the file, wherever the run starts.
    monkeypatch.chdir(tmp_path)
    parent = _load(parent_file, "parent").parent
    nodes = export_graph_json(_build_workflow_graph(parent._wfpy_workflow))["graph"]["nodes"]
    node = next(n for n in nodes if n["label"] == "c")
    assert node["meta"]["workflowInstance"] == {"run": "../wf-out/r1"}
    assert node["meta"]["definitionAnnotations"][0]["name"] == "instance"
