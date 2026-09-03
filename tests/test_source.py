"""The source node: one resource, emitted once, openable in the IDE.

The mirror of ``@viewer``. A viewer is a sink you can open; a source is a
producer you can open, and what it emits is the resource it holds.

Written against the two things that make it different from every other task.
It has no inputs, so nothing triggers it and nothing stops it — the emit-once
latch has to come from somewhere other than the wiring. And its open target is
a path it declares rather than a token it produced, so it is openable before
anything has run.
"""

import tempfile
from pathlib import Path

import pytest

from wfpy import File, Port, Resource, source, task
from wfpy._graph_export import export_graph_json
from wfpy.graph import WorkflowGraph


def make_source(path_type=File, port_type=None, **kwargs):
    """A minimal well-formed source."""

    @source(**kwargs)
    class S:
        path: path_type  # type: ignore[valid-type]

        class Ports:
            Out = Port[port_type or path_type](direction="out")

    return S


class TestDecorator:
    def test_it_is_its_own_kind(self):
        assert make_source()._wfpy_meta.kind == "source"

    def test_the_path_is_a_parameter_not_state(self):
        """One class, many nodes — each carrying its own resource."""
        S = make_source()

        assert "path" in S._wfpy_meta.parameters
        assert S(path=File("a.yuv")).path.path == "a.yuv"
        assert S(path=File("b.yuv")).path.path == "b.yuv"

    def test_a_defaulted_path_is_refused(self):
        """A class-level default makes it state, which every node would share —
        and that reads as a working declaration until two nodes disagree."""
        with pytest.raises(TypeError, match="class-level default"):

            @source
            class S:
                path: File = File("shared.yuv")

                class Ports:
                    Out = Port[File](direction="out")

    def test_a_source_without_a_path_is_refused(self):
        with pytest.raises(TypeError, match="requires a `path` parameter"):

            @source
            class S:
                other: int

                class Ports:
                    Out = Port[File](direction="out")

    def test_a_source_with_an_input_is_refused(self):
        """Something that consumes is a task. The name would be a lie, and the
        emit-once guard would silently ignore whatever arrived."""
        with pytest.raises(TypeError, match="no inputs"):

            @source
            class S:
                path: File

                class Ports:
                    In = Port[File](direction="in")
                    Out = Port[File](direction="out")

    @pytest.mark.parametrize("ports,expected", [
        ("", "exactly one output"),
        ("Out = Port[File](direction='out')\n        Also = Port[File](direction='out')", "exactly one output"),
    ])
    def test_a_source_must_emit_on_exactly_one_port(self, ports, expected):
        namespace: dict = {}
        with pytest.raises(TypeError, match=expected):
            exec(
                "from wfpy import source, File, Port\n"
                "@source\n"
                "class S:\n"
                "    path: File\n"
                "    class Ports:\n"
                f"        {ports or 'pass'}\n",
                namespace,
            )


class TestEmitOnce:
    def test_the_action_is_written_for_you(self):
        """No inputs means nothing triggers it and nothing stops it, so the
        latch cannot come from the wiring the way it does for every other task."""
        S = make_source()
        actions = S._wfpy_meta.actions

        assert [a.name for a in actions] == ["emit"]
        # Explicitly zero-input, not inferred: a source fires with nothing to
        # consume, which is a different statement from "work it out".
        assert actions[0].consumes == {}
        assert actions[0].produces == {"Out": 1}

    def test_the_guard_closes_after_one_firing(self):
        S = make_source()
        emit = S._wfpy_meta.actions[0]
        node = S(path=File("a.yuv"))

        assert emit.guard_fn(node) is True
        emit.fn(node)
        assert emit.guard_fn(node) is False

    def test_the_latch_is_per_node(self):
        """Shared state here would silence every other source of the class."""
        S = make_source()
        emit = S._wfpy_meta.actions[0]
        fired, untouched = S(path=File("a.yuv")), S(path=File("b.yuv"))

        emit.fn(fired)

        assert emit.guard_fn(fired) is False
        assert emit.guard_fn(untouched) is True

    def test_a_class_with_its_own_action_keeps_it(self):
        """A source that does more than hand over its path is an ordinary task,
        and the generated action must not quietly outvote it."""
        from wfpy import action

        @source
        class S:
            path: File

            class Ports:
                Out = Port[File](direction="out")

            @action(consumes={}, produces={"Out": 1})
            def mine(self):
                return self.path

        assert [a.name for a in S._wfpy_meta.actions] == ["mine"]


class TestGraphExport:
    def _export(self, build):
        with WorkflowGraph("wf") as graph:
            build()
        return export_graph_json(graph)["graph"]["nodes"]

    def _viewer_args(self, node):
        annotations = node["meta"]["definitionAnnotations"]
        viewer = next((a for a in annotations if a["name"] == "viewer"), None)
        assert viewer is not None, (
            "the viewer annotation was dropped — without it the node cannot be "
            "opened at all, and nothing else reports the loss"
        )
        return {a["name"]: a["value"].strip('"') for a in viewer["arguments"]}

    def _only_source(self, build):
        nodes = self._export(build)
        # By kind, not label: the instance-name heuristic reads caller locals.
        return next(n for n in nodes if n["kind"] == "source")

    def test_it_declares_itself_external(self):
        """The double-click is only read on an external node, and a source
        stands for something outside the graph by definition."""
        S = make_source()
        node = self._only_source(lambda: S(path=File("notes.md")))

        assert node["meta"]["external"] is True

    def test_the_target_comes_from_the_declaration_not_a_run(self):
        S = make_source()
        args = self._viewer_args(self._only_source(lambda: S(path=File("notes.md"))))

        assert args["source"] == "declared"
        assert args["path"] == "notes.md"

    def test_each_node_exports_its_own_path(self):
        """The annotation dict belongs to the class and is shared by every
        instance of it, so a per-node value written back would leak sideways."""
        S = make_source()
        nodes = [n for n in self._export(lambda: (S(path=File("a.yuv")), S(path=File("b.yuv"))))
                 if n["kind"] == "source"]

        assert sorted(self._viewer_args(n)["path"] for n in nodes) == ["a.yuv", "b.yuv"]
        # And the class itself is unchanged, so a later export says the same.
        assert "path" not in S._wfpy_meta.annotations["viewer"]

    def test_a_named_editor_is_carried(self):
        S = make_source(viewType="product.networkDiagram")
        args = self._viewer_args(self._only_source(lambda: S(path=File("adder.py"))))

        assert args["viewType"] == "product.networkDiagram"
        assert args["action"] == "openWith"

    def test_no_named_editor_means_the_default_one(self):
        S = make_source()
        args = self._viewer_args(self._only_source(lambda: S(path=File("notes.md"))))

        assert "viewType" not in args
        assert args["action"] == "open"


class TestResourceKind:
    """What the target IS gets settled here, because this is the only place the
    filesystem is visible — the diagram cannot tell a folder from a file by
    looking at the string."""

    def _kind_of(self, value, path_type=Resource, port_type=None):
        S = make_source(path_type=path_type, port_type=port_type)
        with WorkflowGraph("wf") as graph:
            S(path=value)
        node = next(n for n in export_graph_json(graph)["graph"]["nodes"]
                    if n["kind"] == "source")
        viewer = next(a for a in node["meta"]["definitionAnnotations"] if a["name"] == "viewer")
        args = {a["name"]: a["value"].strip('"') for a in viewer["arguments"]}
        return args.get("kind")

    def test_a_real_file_is_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "notes.md"
            target.write_text("hi")

            assert self._kind_of(File(str(target)), path_type=File) == "file"

    def test_a_real_folder_is_a_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert self._kind_of(Resource(path=tmp, kind="folder")) == "folder"

    def test_a_url_is_a_url(self):
        assert self._kind_of(Resource(path="https://example.com/x", kind="http")) == "http"

    def test_a_scheme_beats_the_port_type(self):
        """`File` pins kind="file" as a class default, so it describes the port
        rather than this value — and a locator with a scheme is not a local file
        whatever the port says."""
        assert self._kind_of(File("https://example.com/x"), path_type=File) == "http"


class TestFiring:
    def test_it_emits_once_and_the_run_ends(self):
        """A source is ready on every scheduler round — nothing consumes from it
        to make it wait — so an ungated one would emit forever."""
        from wfpy import action, connect, run, workflow

        S = make_source()

        @task
        class Collect:
            received: list = None

            class Ports:
                In = Port[File](direction="in")

            @action(consumes={"In": 1}, produces={})
            def take(self, item):
                Collect.seen.append(item)
                return None

        Collect.seen = []

        @workflow
        def Main():
            s = S(path=File("frames.yuv"))
            c = Collect()
            connect(s.Out, c.In)

        run(Main, verbose=False)

        assert len(Collect.seen) == 1
