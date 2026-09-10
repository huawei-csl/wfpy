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
    """A stand-in `calpy` that records the argv it was handed.

    It also prints a `binary:` line into a build directory it creates, because
    the real one does: that line is the only way to find where
    `--keep-artifacts` left the build, the directory being a
    `calpy_native_<random>` temp dir no flag selects.
    """
    log = tmp_path / "argv.txt"
    build = tmp_path / "calpy_native_fake"
    script = tmp_path / "fake-calpy"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"pathlib.Path({str(log)!r}).write_text('\\n'.join(sys.argv[1:]))\n"
        f"build = pathlib.Path({str(build)!r})\n"
        "build.mkdir(parents=True, exist_ok=True)\n"
        "(build / 'llvm.ll').write_text('; ir')\n"
        "print(f'  binary: {build / \"decoder_native\"}')\n"
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

    def test_it_collects_the_build_from_where_calpy_says_it_is(self, tmp_path, monkeypatch):
        """Where `--keep-artifacts` leaves the build cannot be guessed: it is a
        `calpy_native_<random>` temp dir chosen by mkdtemp, and no flag selects
        it. What can be relied on is the `binary:` line, whose parent IS that
        directory."""
        build_dir = tmp_path / "calpy_native_abc123"
        build_dir.mkdir()
        (build_dir / "llvm.ll").write_text("; ir")
        (build_dir / "decoder_native").write_text("elf")

        script = tmp_path / "calpy-with-artifacts"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"print('  binary: {build_dir / 'decoder_native'}')\n"
        )
        script.chmod(0o755)
        monkeypatch.setenv(CALPY_COMMAND_ENV, str(script))

        @streamblocks(facade="instance", network="n.py")
        class Node:
            class Ports:
                stimulus = Port[str](direction="in")
                build = Port[Resource(kind="folder")](direction="out")

        actor = FakeActor(Node, {"stimulus": "a.bin"})
        _step_streamblocks_instance(actor, tmp_path, FakePlan(tmp_path), False)

        collected = Path(actor.out_queues["build"][0].enqueued[-1])
        # The earlier version looked beside the network for a `calpy-out` folder
        # that is never created, so it copied nothing and handed on an empty
        # directory — which looks exactly like success.
        assert (collected / "llvm.ll").read_text() == "; ir"
        assert (collected / "decoder_native").exists()

    def test_it_refuses_to_hand_on_an_empty_build_folder(self, tmp_path, monkeypatch):
        """A silent no-op producing an empty folder is the bug this replaced."""
        silent = tmp_path / "silent-calpy"
        silent.write_text("#!/usr/bin/env python3\n")
        silent.chmod(0o755)
        monkeypatch.setenv(CALPY_COMMAND_ENV, str(silent))

        @streamblocks(facade="instance", network="n.py")
        class Node:
            class Ports:
                stimulus = Port[str](direction="in")
                build = Port[Resource(kind="folder")](direction="out")

        actor = FakeActor(Node, {"stimulus": "a.bin"})
        with pytest.raises(RuntimeError, match="no `binary:` line"):
            _step_streamblocks_instance(actor, tmp_path, FakePlan(tmp_path), False)

    def test_the_workflow_environment_reaches_calpy(self, tmp_path, monkeypatch):
        """`@config(env=)` is how a workflow points at a different toolchain —
        CALPY_CLANG when the default one is too old, for instance. This step
        passed no environment at all, so it inherited the OS one and the
        workflow's was silently ignored."""
        seen = tmp_path / "env.txt"
        script = tmp_path / "env-calpy"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib\n"
            f"pathlib.Path({str(seen)!r}).write_text(os.environ.get('CALPY_CLANG', '<unset>'))\n"
        )
        script.chmod(0o755)
        monkeypatch.setenv(CALPY_COMMAND_ENV, str(script))

        @streamblocks(facade="instance", network="n.py")
        class Node:
            class Ports:
                stimulus = Port[str](direction="in")

        plan = FakePlan(tmp_path)
        plan.env = {"CALPY_CLANG": "/opt/llvm22/bin/clang"}
        actor = FakeActor(Node, {"stimulus": "a.bin"})
        _step_streamblocks_instance(actor, tmp_path, plan, False)

        assert seen.read_text() == "/opt/llvm22/bin/clang"

    def test_a_node_can_override_the_workflow_environment(self, tmp_path, monkeypatch):
        seen = tmp_path / "env.txt"
        script = tmp_path / "env-calpy"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib\n"
            f"pathlib.Path({str(seen)!r}).write_text(os.environ.get('CALPY_CLANG', '<unset>'))\n"
        )
        script.chmod(0o755)
        monkeypatch.setenv(CALPY_COMMAND_ENV, str(script))

        @streamblocks(facade="instance", network="n.py", env={"CALPY_CLANG": "/node/clang"})
        class Node:
            class Ports:
                stimulus = Port[str](direction="in")

        plan = FakePlan(tmp_path)
        plan.env = {"CALPY_CLANG": "/workflow/clang"}
        actor = FakeActor(Node, {"stimulus": "a.bin"})
        _step_streamblocks_instance(actor, tmp_path, plan, False)

        # Node over workflow, as a tool's env is over @config's.
        assert seen.read_text() == "/node/clang"

    def test_search_paths_lead_the_path(self, tmp_path, monkeypatch):
        seen = tmp_path / "path.txt"
        script = tmp_path / "path-calpy"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib\n"
            f"pathlib.Path({str(seen)!r}).write_text(os.environ.get('PATH', ''))\n"
        )
        script.chmod(0o755)
        monkeypatch.setenv(CALPY_COMMAND_ENV, str(script))

        @streamblocks(facade="instance", network="n.py")
        class Node:
            class Ports:
                stimulus = Port[str](direction="in")

        plan = FakePlan(tmp_path)
        plan.search_paths = ["/opt/toolchain/bin"]
        actor = FakeActor(Node, {"stimulus": "a.bin"})
        _step_streamblocks_instance(actor, tmp_path, plan, False)

        assert seen.read_text().startswith("/opt/toolchain/bin")

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

    def test_it_declares_itself_external(self):
        """The platform does not know the word `streamblocks`, and should not
        have to: it renders and treats a node as external because the exporter
        said so. Without this the node draws as an ordinary actor and cannot be
        opened by double-click, since annotations are only read on an
        external-actor node."""

        @streamblocks(facade="instance", network="designs/adder.py")
        class Adder:
            class Ports:
                stimulus = Port[str](direction="in")

        def build(connect):
            a = Adder()
            connect("In", a.stimulus)

        nodes = self._export(build)["graph"]["nodes"]
        node = next(n for n in nodes if n["kind"] == "streamblocks")

        assert node["meta"]["external"] is True

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


# ── network as a node parameter, run=False ─────────────────────────────────


class TestNetworkParameter:
    def test_an_instance_can_take_its_network_as_a_parameter(self):
        @streamblocks(facade="instance", run=False)
        class Net:
            network: str

            class Ports:
                In = Port[str]()
                Out = Port[str](direction="out")

        meta: TaskMeta = Net._wfpy_meta
        assert meta.annotations["streamblocks"] == {"facade": "instance", "run": False}
        assert "network" in meta.parameters
        assert meta.annotations["viewer"]["source"] == "declared"

    def test_run_false_is_only_for_an_instance(self):
        with pytest.raises(TypeError, match="run=False"):

            @streamblocks(facade="design", run=False)
            class Design:
                class Ports:
                    Out = Port[str](direction="out")

    def test_run_false_hands_its_network_on_and_runs_nothing(self, tmp_path, monkeypatch):
        # a `calpy` that would fail loudly if it were called
        monkeypatch.setenv(CALPY_COMMAND_ENV, str(tmp_path / "no-such-calpy"))

        @streamblocks(facade="instance", run=False)
        class Net:
            network: str

            class Ports:
                In = Port[str]()
                Out = Port[str](direction="out")

        actor = FakeActor(Net, {})  # `In` not connected
        actor.instance = Net(network="designs/core.py")
        plan = FakePlan(tmp_path)

        assert _step_streamblocks_instance(actor, tmp_path, plan, verbose=False)
        assert actor.out_queues["Out"][0].enqueued == ["designs/core.py"]
        # with nothing connected, once
        assert not _step_streamblocks_instance(actor, tmp_path, plan, verbose=False)

    def test_a_connected_input_fires_it_per_token(self, tmp_path):
        @streamblocks(facade="instance", run=False)
        class Net:
            network: str

            class Ports:
                In = Port[str]()
                Out = Port[str](direction="out")

        actor = FakeActor(Net, {"In": "go"})
        actor.instance = Net(network="designs/core.py")
        plan = FakePlan(tmp_path)

        assert _step_streamblocks_instance(actor, tmp_path, plan, verbose=False)
        assert actor.in_queues["In"][0].items == []
        assert not _step_streamblocks_instance(actor, tmp_path, plan, verbose=False)

    def test_each_node_opens_its_own_network(self):
        from wfpy import connect, workflow
        from wfpy.graph import export_graph_json
        from wfpy.runner import _build_workflow_graph

        @streamblocks(facade="instance", run=False)
        class Net:
            network: str

            class Ports:
                In = Port[str]()
                Out = Port[str](direction="out")

        @workflow(outputs={"A": str, "B": str})
        def two():
            a = Net(network="designs/a.py")
            b = Net(network="designs/b.py")
            connect(a.Out, "A")
            connect(b.Out, "B")

        nodes = export_graph_json(_build_workflow_graph(two._wfpy_workflow))["graph"]["nodes"]
        paths = {}
        for node in nodes:
            for annotation in node.get("meta", {}).get("definitionAnnotations") or []:
                if annotation["name"] == "viewer":
                    args = {arg["name"]: arg["value"] for arg in annotation["arguments"]}
                    paths[node["label"]] = args.get("path")
        assert paths == {"a": '"designs/a.py"', "b": '"designs/b.py"'}

    def test_without_an_input_it_hands_its_network_on_once(self, tmp_path):
        from wfpy import connect, run, workflow

        # run=False needs no input: nothing it would read one for
        @streamblocks(facade="instance", run=False)
        class Net:
            network: str

            class Ports:
                Out = Port[str](direction="out")

        @workflow(outputs={"Network": str})
        def only_the_network():
            net = Net(network="designs/core.py")
            connect(net.Out, "Network")

        outputs = run(only_the_network, out_dir=str(tmp_path / "wf-out"))

        assert outputs == {"Network": ["designs/core.py"]}

    def test_a_network_parameter_is_not_reported_missing(self):
        """The diagram paints a node with an error diagnostic red, so a node
        that names its network as a parameter must not get one."""
        from wfpy import connect, workflow
        from wfpy.graph import export_graph_json
        from wfpy.runner import _build_workflow_graph

        @streamblocks(facade="instance", run=False)
        class Net:
            network: str

            class Ports:
                Out = Port[str](direction="out")

        @workflow(outputs={"Network": str})
        def one():
            net = Net(network="designs/core.py")
            connect(net.Out, "Network")

        nodes = export_graph_json(_build_workflow_graph(one._wfpy_workflow))["graph"]["nodes"]
        net = next(node for node in nodes if node.get("label") == "net")

        assert not net["meta"].get("diagnostics")
