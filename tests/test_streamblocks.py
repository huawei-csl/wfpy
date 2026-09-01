"""The StreamBlocks node: two facades over one CalPy design.

`design` is an ordinary task and is tested as such — the point is that wfpy
imposes no shape on it. `instance` is a typed facade over `calpy run`, so what
is worth pinning there is the MAPPING: which port becomes which flag, and that a
design rewritten inside one run keeps every version instead of overwriting
itself.
"""

import os
from pathlib import Path

import pytest

from wfpy import Port, Resource, streamblocks
from wfpy.core import TaskMeta
from wfpy._step_streamblocks_runtime import (
    CALPY_COMMAND_ENV,
    _step_streamblocks_instance,
)


class TestDecorator:
    def test_instance_carries_facade_and_network(self):
        @streamblocks(facade="instance", network="designs/adder.py")
        class Adder:
            class Ports:
                stimulus = Port[str](direction="in")
                trace = Port[str](direction="out")

        meta: TaskMeta = Adder._wfpy_meta
        assert meta.kind == "streamblocks"
        assert meta.annotations["streamblocks"] == {
            "facade": "instance",
            "network": "designs/adder.py",
        }

    def test_instance_opens_the_declared_path_not_a_token(self):
        """An instance is openable before anything has run, so it cannot
        resolve through a token the way a design does."""

        @streamblocks(facade="instance", network="designs/adder.py")
        class Adder:
            class Ports:
                stimulus = Port[str](direction="in")

        viewer = Adder._wfpy_meta.annotations["viewer"]
        assert viewer["source"] == "declared"
        assert viewer["path"] == "designs/adder.py"
        assert viewer["viewType"] == "calpy.networkDiagram"

    def test_design_opens_what_it_produced(self):
        @streamblocks(facade="design")
        class Design:
            class Ports:
                project = Port[str](direction="in")
                out = Port[str](direction="out")

        viewer = Design._wfpy_meta.annotations["viewer"]
        assert viewer["source"] == "token"
        # Its target is what it made, so the outputs are the inputs to resolve.
        assert viewer["inputs"] == ["out"]

    def test_instance_without_a_network_is_refused(self):
        """Nothing to compile, run or open — caught at declaration rather than
        left to fail when the workflow is run."""
        with pytest.raises(TypeError, match="requires network="):

            @streamblocks(facade="instance")
            class NoNetwork:
                class Ports:
                    In = Port[str](direction="in")

    def test_an_unknown_facade_is_refused(self):
        with pytest.raises(TypeError, match="expected 'design' or 'instance'"):

            @streamblocks(facade="compile", network="x.py")
            class Bad:
                class Ports:
                    In = Port[str](direction="in")

    def test_a_design_needs_no_network(self):
        @streamblocks(facade="design")
        class Design:
            class Ports:
                out = Port[str](direction="out")

        assert Design._wfpy_meta.annotations["streamblocks"] == {"facade": "design"}


# ── instance firing ────────────────────────────────────────────────────────


class FakeQueue:
    def __init__(self, items=()):
        self.items = list(items)
        self.enqueued = []

    def size(self):
        return len(self.items)

    def dequeue(self):
        return self.items.pop(0)

    def enqueue(self, value):
        self.enqueued.append(value)


class FakeActor:
    def __init__(self, cls, inputs):
        self.meta = cls._wfpy_meta
        self.name = cls.__name__
        self.fire_count = 0
        self.in_queues = {name: [FakeQueue([value])] for name, value in inputs.items()}
        self.out_queues = {name: [FakeQueue()] for name in self.meta.output_ports}


class FakePlan:
    def __init__(self, source_dir):
        self.source_dir = source_dir


@pytest.fixture
def recording_calpy(tmp_path, monkeypatch):
    """A stand-in `calpy` that records the argv it was handed."""
    log = tmp_path / "argv.txt"
    script = tmp_path / "fake-calpy"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"pathlib.Path({str(log)!r}).write_text('\\n'.join(sys.argv[1:]))\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv(CALPY_COMMAND_ENV, str(script))
    return log


class TestInstanceFiring:
    def test_ports_become_the_commands_flags(self, tmp_path, recording_calpy):
        @streamblocks(facade="instance", network="designs/adder.py")
        class Adder:
            class Ports:
                stimulus = Port[str](direction="in")

        actor = FakeActor(Adder, {"stimulus": "bits.bin"})
        fired = _step_streamblocks_instance(actor, tmp_path, FakePlan(tmp_path), False)

        assert fired is True
        argv = recording_calpy.read_text().splitlines()
        assert argv[:2] == ["run", "designs/adder.py"]
        # A lone input is the stimulus, which `calpy run` requires.
        assert argv[2:] == ["--input", "bits.bin"]

    def test_a_named_flag_wins_over_the_default(self, tmp_path, recording_calpy):
        @streamblocks(facade="instance", network="n.py")
        class Node:
            class Ports:
                stimulus = Port[str](direction="in")
                trace = Port[str](direction="out", ext=".jsonl")

        Node._wfpy_meta.annotations["streamblocks"]["flags"] = {
            "trace": "--turnus-trace-file"
        }
        actor = FakeActor(Node, {"stimulus": "bits.bin"})
        _step_streamblocks_instance(actor, tmp_path, FakePlan(tmp_path), False)

        argv = recording_calpy.read_text().splitlines()
        assert "--turnus-trace-file" in argv
        trace_path = argv[argv.index("--turnus-trace-file") + 1]
        assert trace_path.endswith("Node__trace__0.jsonl")

    def test_it_does_not_fire_without_a_token(self, tmp_path, recording_calpy):
        @streamblocks(facade="instance", network="n.py")
        class Node:
            class Ports:
                stimulus = Port[str](direction="in")

        actor = FakeActor(Node, {})
        actor.in_queues = {"stimulus": [FakeQueue()]}

        assert _step_streamblocks_instance(actor, tmp_path, FakePlan(tmp_path), False) is False
        assert not recording_calpy.exists()

    def test_each_firing_gets_its_own_folder(self, tmp_path, recording_calpy):
        """The point of the whole versioning scheme: a design rewritten twice in
        one run leaves two folders, not one overwritten twice."""

        @streamblocks(facade="instance", network="n.py")
        class Node:
            class Ports:
                stimulus = Port[str](direction="in")
                build = Port[Resource(kind="folder")](direction="out")

        actor = FakeActor(Node, {"stimulus": "a.bin"})
        _step_streamblocks_instance(actor, tmp_path, FakePlan(tmp_path), False)
        first = actor.out_queues["build"][0].enqueued[-1]

        actor.in_queues["stimulus"][0].items.append("b.bin")
        _step_streamblocks_instance(actor, tmp_path, FakePlan(tmp_path), False)
        second = actor.out_queues["build"][0].enqueued[-1]

        assert first != second
        assert first.endswith("Node__build__0")
        assert second.endswith("Node__build__1")

    def test_a_failing_run_is_not_silent(self, tmp_path, monkeypatch):
        failing = tmp_path / "failing-calpy"
        failing.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n")
        failing.chmod(0o755)
        monkeypatch.setenv(CALPY_COMMAND_ENV, str(failing))

        @streamblocks(facade="instance", network="n.py")
        class Node:
            class Ports:
                stimulus = Port[str](direction="in")

        actor = FakeActor(Node, {"stimulus": "a.bin"})
        with pytest.raises(RuntimeError, match="exited with code 3"):
            _step_streamblocks_instance(actor, tmp_path, FakePlan(tmp_path), False)


# ── graph export ───────────────────────────────────────────────────────────


class TestExport:
    """The double-click rides the `viewer` annotation, so the export must carry
    it for a node that is not a viewer — which is the whole point of decoupling
    "openable in an editor" from "is a runtime sink"."""

    def _export(self, build):
        from wfpy import connect, workflow
        from wfpy.graph import WorkflowGraph, export_graph_json

        graph = WorkflowGraph("wf")
        with graph:
            build(connect)
        return export_graph_json(graph)

    def test_a_streamblocks_node_exports_its_viewer_annotation(self):
        @streamblocks(facade="instance", network="designs/adder.py")
        class Adder:
            class Ports:
                stimulus = Port[str](direction="in")

        def build(connect):
            a = Adder()
            connect("In", a.stimulus)

        nodes = self._export(build)["graph"]["nodes"]
        # By kind, not label: the instance-name heuristic reads caller locals
        # and names it from whatever it finds there.
        node = next(n for n in nodes if n["kind"] == "streamblocks")

        annotations = node["meta"]["definitionAnnotations"]
        viewer = next((a for a in annotations if a["name"] == "viewer"), None)
        assert viewer is not None, (
            "the viewer annotation was dropped — the export used to gate it on "
            "kind == 'viewer', which is exactly what this node cannot be"
        )

        # Assert the ARGUMENTS, not merely that something named `viewer` is
        # present. A generic exporter already emits every annotation key, so
        # presence proves nothing; what matters is that the specialised path —
        # which replaces that entry wholesale — carries the two fields the
        # declared-target branch needs. It dropped both until this caught it.
        args = {a["name"]: a["value"] for a in viewer["arguments"]}
        assert args["source"] == '"declared"'
        assert args["path"] == '"designs/adder.py"'
        assert args["viewType"] == '"calpy.networkDiagram"'

    def test_it_carries_the_facade_through(self):
        @streamblocks(facade="instance", network="designs/adder.py")
        class Adder:
            class Ports:
                stimulus = Port[str](direction="in")

        def build(connect):
            a = Adder()
            connect("In", a.stimulus)

        nodes = self._export(build)["graph"]["nodes"]
        node = next(n for n in nodes if n["kind"] == "streamblocks")

        assert node["meta"]["streamblocks"]["facade"] == "instance"
        assert node["meta"]["streamblocks"]["network"] == "designs/adder.py"


class TestDesignIsAnOrdinaryTask:
    def test_it_fires_its_own_actions(self):
        """No special step: a design declares ports and actions and runs like
        any other task, which is why the loop shape is the user's to choose."""
        from wfpy import action, connect, run, workflow

        @streamblocks(facade="design")
        class Refine:
            class Ports:
                In = Port[int](direction="in")
                Out = Port[int](direction="out")

            @action(consumes={"In": 1}, produces={"Out": 1})
            def refine(self, value: int) -> int:
                return value + 1

        @workflow(inputs={"In": int}, outputs={"Out": int})
        def wf() -> None:
            r = Refine()
            connect("In", r.In)
            connect(r.Out, "Out")

        assert run(wf, inputs={"In": 41}) == {"Out": [42]}
