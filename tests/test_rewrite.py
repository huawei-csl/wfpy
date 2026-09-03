from __future__ import annotations

from pathlib import Path

import libcst as cst
import pytest

from wfpy.rewrite import RewriteEngine, RewriteError, load_module


def _engine_from_source(tmp_path: Path, source: str) -> RewriteEngine:
    file_path = tmp_path / "wf.py"
    file_path.write_text(source, encoding="utf-8")
    module = cst.parse_module(source)
    return RewriteEngine(str(file_path), module, source)


def test_export_workflow_graph_includes_imported_nested_children(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text(
        '''
from wfpy import workflow, connect


@workflow
def grandchild():
    x = X()
    connect("In", x.In)
    connect(x.Out, "Out")


@workflow
def child():
    g = grandchild()
    connect("In", g.In)
    connect(g.Out, "Out")
''',
        encoding="utf-8",
    )

    source = '''
from wfpy import workflow, connect
from helper import child


@workflow
def parent():
    c = child()
    connect("In", c.In)
    connect(c.Out, "Out")
'''
    engine = _engine_from_source(tmp_path, source)
    graph = engine.export_workflow_graph(workflow="parent")

    assert graph["workflow"] == "parent"
    assert graph["file"].endswith("wf.py")
    children = graph.get("children")
    assert isinstance(children, list)
    assert len(children) == 1
    child = children[0]
    assert child["workflow"] == "child"
    assert child["source"] == "import"
    child_graph = child["graph"]
    assert child_graph["workflow"] == "child"
    assert child_graph["file"].endswith("helper.py")
    grand_children = child_graph.get("children")
    assert isinstance(grand_children, list)
    assert len(grand_children) == 1
    assert grand_children[0]["workflow"] == "grandchild"


def test_export_workflow_graph_resolves_sibling_module_import_for_nested(tmp_path: Path) -> None:
    pkg = tmp_path / "examples" / "streamblocks"
    pkg.mkdir(parents=True)
    (tmp_path / "examples" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    cal = pkg / "cal_mlir_passes.py"
    cal.write_text(
        '''
from wfpy import workflow, connect


@workflow
def lower_to_llvm():
    x = X()
    connect("In", x.In)
    connect(x.Out, "Out")


@workflow
def cal_mlir_passes():
    l = lower_to_llvm()
    connect("In", l.In)
    connect(l.Out, "Out")
''',
        encoding="utf-8",
    )

    top = pkg / "test_passes_linux.py"
    top.write_text(
        '''
from wfpy import workflow, connect
from cal_mlir_passes import cal_mlir_passes


@workflow
def test_passes_linux():
    p = cal_mlir_passes()
    connect("In", p.In)
    connect(p.Out, "Out")
''',
        encoding="utf-8",
    )

    module, source_text, _rev = load_module(str(top))
    engine = RewriteEngine(str(top), module, source_text)
    graph = engine.export_workflow_graph(workflow="test_passes_linux")
    children = graph.get("children")
    assert isinstance(children, list)
    assert len(children) == 1
    assert children[0]["workflow"] == "cal_mlir_passes"
    child_graph = children[0]["graph"]
    assert child_graph["workflow"] == "cal_mlir_passes"
    grand_children = child_graph.get("children")
    assert isinstance(grand_children, list)
    assert len(grand_children) == 1
    assert grand_children[0]["workflow"] == "lower_to_llvm"


def test_export_workflow_graph_inlines_same_file_helper_calls(tmp_path: Path) -> None:
    source = '''
from wfpy import workflow, connect


def _wire(*, tries=3):
    a = A()
    b = B(tries=tries)
    connect("In", a.In)
    connect(a.Out, b.In)
    connect(b.Out, "Out")
    return a, b


@workflow
def demo():
    a, b = _wire(tries=7)
    _ = (a, b)
'''

    engine = _engine_from_source(tmp_path, source)
    graph = engine.export_workflow_graph(workflow="demo")

    assert graph["workflow"] == "demo"
    assert [node["id"] for node in graph["nodes"]] == ["a", "b"]
    assert graph["nodes"][1]["kwargs"] == {"tries": "7"}
    assert len(graph["edges"]) == 3
    assert engine.list_instance_names(workflow="demo") == ["a", "b"]


def test_export_workflow_graph_resolves_nested_factory_workflow_defaults(tmp_path: Path) -> None:
    source = '''
from wfpy import workflow, connect


def make_top(scale=1, mode="fast"):
    @workflow(inputs={"In": int}, outputs={"Out": int})
    def top():
        connect("In", "Out")

    return top
'''

    engine = _engine_from_source(tmp_path, source)
    graph = engine.export_workflow_graph(workflow="top")

    assert graph["workflow"] == "top"
    assert graph["factoryName"] == "make_top"
    assert graph["parameters"] == [
        {"name": "scale", "value": "1"},
        {"name": "mode", "value": '"fast"'},
    ]


def test_load_module_rejects_dynamic_workflow_name_mutation(tmp_path: Path) -> None:
    workflow_file = tmp_path / "dynamic_workflow.py"
    workflow_file.write_text(
        '''
from wfpy import workflow


def make_workflow():
    @workflow
    def _inner_workflow():
        pass

    _inner_workflow.__name__ = "inner_workflow"
    return _inner_workflow


inner_workflow = make_workflow()
''',
        encoding="utf-8",
    )

    with pytest.raises(RewriteError, match="Unsupported dynamic workflow naming"):
        load_module(str(workflow_file))


def test_create_node_in_nested_if_scope(tmp_path: Path) -> None:
    source = """
from wfpy import workflow, connect


@workflow
def demo():
    if flag:
        ctrl = wfpy.if_(flag)
        with ctrl.then:
            a = A()
            connect(a.out, b.inp)
"""
    engine = _engine_from_source(tmp_path, source)
    engine.create_node(workflow="demo", type="B", name="b", scope_control="ctrl", scope_branch="then")
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert "b = B()" in updated
    assert updated.index("a = A()") < updated.index("b = B()")


def test_connect_inserts_after_assigns(tmp_path: Path) -> None:
    source = """
from wfpy import workflow, connect


@workflow
def demo():
    a = A()
    b = B()
"""
    engine = _engine_from_source(tmp_path, source)
    engine.connect(workflow="demo", from_expr="a.out", to_expr="b.inp")
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert "connect(a.out, b.inp)" in updated
    assert updated.index("b = B()") < updated.index("connect(a.out, b.inp)")


def test_create_loop_removes_placeholder_pass(tmp_path: Path) -> None:
    source = """
from wfpy import workflow


@workflow
def demo():
    ctrl = wfpy.loop(items)
    with ctrl:
        pass
"""
    engine = _engine_from_source(tmp_path, source)
    engine.create_node(workflow="demo", type="A", name="a", scope_control="ctrl", scope_branch="body")
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert "pass" not in updated
    assert "a = A()" in updated


def test_delete_edge_matches_connect_expr(tmp_path: Path) -> None:
    source = """
from wfpy import workflow, connect


@workflow
def demo():
    a = A()
    b = B()
    connect(a.out, b.inp)
"""
    engine = _engine_from_source(tmp_path, source)
    engine.delete_edge(workflow="demo", from_expr="a.out", to_expr="b.inp")
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert "connect(a.out, b.inp)" not in updated


def test_unwrap_control_nested_in_if(tmp_path: Path) -> None:
    source = """
from wfpy import workflow


@workflow
def demo():
    if flag:
        ctrl = wfpy.if_(flag)
        with ctrl.then:
            a = A()
"""
    engine = _engine_from_source(tmp_path, source)
    engine.unwrap_control(workflow="demo", name="ctrl")
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert "with ctrl.then" not in updated
    assert "a = A()" in updated


def test_move_node_into_else_scope(tmp_path: Path) -> None:
    source = """
from wfpy import workflow


@workflow
def demo():
    ctrl = wfpy.if_(flag)
    with ctrl.then:
        pass
    with ctrl.else_:
        pass
    a = A()
"""
    engine = _engine_from_source(tmp_path, source)
    engine.move_node(workflow="demo", name="a", target_control="ctrl", target_scope="else")
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert "a = A()" in updated
    assert updated.index("with ctrl.else_:") < updated.index("a = A()")


def test_delete_node_scoped_to_workflow(tmp_path: Path) -> None:
    source = """
from wfpy import workflow


@workflow
def wf_a():
    n = A()


@workflow
def wf_b():
    n = B()
"""
    engine = _engine_from_source(tmp_path, source)
    engine.delete_node(workflow="wf_b", name="n")
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert "def wf_a" in updated and "n = A()" in updated
    assert "def wf_b" in updated and "n = B()" not in updated


def test_delete_node_removes_all_incident_edges(tmp_path: Path) -> None:
    source = """
from wfpy import workflow, connect


@workflow
def demo():
    a = A()
    b = B()
    c = C()
    connect("In", a.In)
    connect(c.Out, a.In)
    connect(a.Out, b.In)
    connect(a.Out, "Out")
    connect(c.Out, b.In)
"""
    engine = _engine_from_source(tmp_path, source)
    engine.delete_node(workflow="demo", name="a")
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert "a = A()" not in updated
    assert 'connect("In", a.In)' not in updated
    assert "connect(c.Out, a.In)" not in updated
    assert "connect(a.Out, b.In)" not in updated
    assert 'connect(a.Out, "Out")' not in updated
    assert "connect(c.Out, b.In)" in updated


def test_delete_edge_scoped_to_workflow(tmp_path: Path) -> None:
    source = """
from wfpy import workflow, connect


@workflow
def wf_a():
    a = A()
    b = B()
    connect(a.out, b.inp)


@workflow
def wf_b():
    a = C()
    b = D()
    connect(a.out, b.inp)
"""
    engine = _engine_from_source(tmp_path, source)
    engine.delete_edge(workflow="wf_b", from_expr="a.out", to_expr="b.inp")
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert updated.count("connect(a.out, b.inp)") == 1
    assert updated.index("def wf_a") < updated.index("connect(a.out, b.inp)") < updated.index("def wf_b")


def test_rename_node_scoped_and_targeted(tmp_path: Path) -> None:
    source = """
from lib import old
from wfpy import workflow, connect


@workflow
def wf_a():
    old = A()
    b = B()
    connect(old.out, b.inp)


@workflow
def wf_b():
    old = C()
    d = D()
    connect(old.out, d.inp)

value = old
"""
    engine = _engine_from_source(tmp_path, source)
    engine.rename_node(workflow="wf_b", old="old", new="renamed")
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert "from lib import old" in updated
    assert "value = old" in updated
    assert "def wf_a" in updated and "old = A()" in updated and "connect(old.out, b.inp)" in updated
    assert "def wf_b" in updated and "renamed = C()" in updated and "connect(renamed.out, d.inp)" in updated


def test_update_definition_annotation_normalizes_js_booleans_for_python(tmp_path: Path) -> None:
    source = """
from wfpy import agent


@agent(prompt="old")
class Analyze:
    pass
"""
    engine = _engine_from_source(tmp_path, source)
    engine.update_definition_annotation(
        entityType="Analyze",
        annotationName="agent",
        annotationText='@agent(prompt="new", useSkill=true, usePrompt=false, useClaudeAgent=false)'
    )
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert 'prompt="new"' in updated
    assert "useSkill=True" in updated
    assert "usePrompt=False" in updated
    assert "useClaudeAgent=False" in updated


def test_merge_definition_annotation_args_preserves_unlisted(tmp_path: Path) -> None:
    """Editing model via the GLSP panel must not drop outputValidators."""
    source = '''
from wfpy import agent


@agent(
    skill="pto-kernel-optimizer",
    model="openai/gpt-4o",
    provider="openrouter",
    timeoutMs=600000,
    outputValidators=[
        {
            "cmd": "bisheng",
            "args": ["-fsyntax-only", "{file}"],
            "ports": ["Optimized"],
            "maxRepairAttempts": 3,
        },
    ],
    mcpServers=[
        {"name": "pto-isa-mcp", "transport": "streamable-http", "url": "http://localhost:8080/mcp"},
    ],
)
class OptimizeAgent:
    pass
'''
    engine = _engine_from_source(tmp_path, source)
    engine.merge_definition_annotation_args(
        entityType="OptimizeAgent",
        annotationName="agent",
        argUpdates={"model": '"z-ai/glm-5-turbo"'},
    )
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    # Updated arg
    assert '"z-ai/glm-5-turbo"' in updated
    # Preserved args
    assert "outputValidators" in updated
    assert '"bisheng"' in updated
    assert '"maxRepairAttempts": 3' in updated
    assert "mcpServers" in updated
    assert '"pto-isa-mcp"' in updated
    assert 'skill="pto-kernel-optimizer"' in updated
    assert 'provider="openrouter"' in updated
    assert "timeoutMs=600000" in updated


def test_merge_definition_annotation_args_adds_new_arg(tmp_path: Path) -> None:
    """Merge can add new args that weren't in the original decorator."""
    source = '''
from wfpy import agent


@agent(prompt="do it", model="openai/gpt-4o")
class MyAgent:
    pass
'''
    engine = _engine_from_source(tmp_path, source)
    engine.merge_definition_annotation_args(
        entityType="MyAgent",
        annotationName="agent",
        argUpdates={"model": '"new-model"', "provider": '"openrouter"'},
    )
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert '"new-model"' in updated
    assert 'provider="openrouter"' in updated
    assert 'prompt="do it"' in updated


def test_merge_definition_annotation_args_normalizes_js_booleans(tmp_path: Path) -> None:
    source = '''
from wfpy import agent


@agent(model="old", useSkill=True)
class A:
    pass
'''
    engine = _engine_from_source(tmp_path, source)
    engine.merge_definition_annotation_args(
        entityType="A",
        annotationName="agent",
        argUpdates={"useSkill": "false", "usePrompt": "true"},
    )
    engine.save()
    updated = Path(engine.file_path).read_text(encoding="utf-8")
    assert "useSkill=False" in updated
    assert "usePrompt=True" in updated
    assert 'model="old"' in updated


class TestCreateStreamblocksType:
    """The sidecar has to write a node the file can actually run.

    Creating from the palette got as far as this op and was rejected, so the
    whole UI flow existed with nothing at the end of it.
    """

    SOURCE = (
        "from wfpy import workflow\n\n"
        "@workflow(inputs={}, outputs={})\n"
        "def top():\n"
        "    pass\n"
    )

    def test_a_design_needs_no_network(self, tmp_path):
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(kind="streamblocks", name="MyDesign", facade="design")
        code = engine.module.code

        assert '@streamblocks(facade = "design")' in code
        assert "class MyDesign:" in code

    def test_an_instance_carries_its_network(self, tmp_path):
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(
            kind="streamblocks", name="FirRun", facade="instance", network="designs/adder.py"
        )

        assert 'network = "designs/adder.py"' in engine.module.code

    def test_it_imports_what_the_class_needs(self, tmp_path):
        """The sidecar wrote the class, so the sidecar owes the imports —
        otherwise the author gets a NameError to fix by hand."""
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(kind="streamblocks", name="MyDesign", facade="design")
        code = engine.module.code

        assert "streamblocks" in code.split("\n")[1]  # folded into the wfpy import
        assert "Port" in code
        assert "from typing import Any" in code

    def test_an_instance_without_a_network_is_refused(self, tmp_path):
        """Caught here rather than written into the file for import time to
        report."""
        engine = _engine_from_source(tmp_path, self.SOURCE)
        with pytest.raises(RewriteError, match="needs network="):
            engine.create_task_type(kind="streamblocks", name="Bad", facade="instance")

    def test_a_missing_or_wrong_facade_is_refused(self, tmp_path):
        engine = _engine_from_source(tmp_path, self.SOURCE)
        with pytest.raises(RewriteError, match="facade="):
            engine.create_task_type(kind="streamblocks", name="Bad")
        with pytest.raises(RewriteError, match="facade="):
            engine.create_task_type(kind="streamblocks", name="Bad", facade="compile")

    def test_the_generated_class_actually_runs(self, tmp_path):
        """The point of all of the above: valid wfpy, not just valid Python."""
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(
            kind="streamblocks", name="FirRun", facade="instance", network="designs/adder.py"
        )
        generated = tmp_path / "generated.py"
        generated.write_text(engine.module.code, encoding="utf-8")

        import importlib.util

        spec = importlib.util.spec_from_file_location("generated_wf", generated)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        meta = module.FirRun._wfpy_meta
        assert meta.kind == "streamblocks"
        assert meta.annotations["streamblocks"] == {
            "facade": "instance",
            "network": "designs/adder.py",
        }


class TestCreateSourceType:
    """Writing a source from the diagram.

    A source is the one task shape with no inputs, so the generated class must
    not carry the input port every other kind gets — the ports body is written
    per kind and the default is `In` + `Out`.
    """

    SOURCE = (
        "from wfpy import workflow\n\n"
        "@workflow(inputs={}, outputs={})\n"
        "def top():\n"
        "    pass\n"
    )

    def test_it_writes_a_source_class(self, tmp_path):
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(kind="source", name="Frames")
        code = engine.module.code

        assert "class Frames:" in code
        assert "@source" in code

    def test_it_has_an_output_and_no_input(self, tmp_path):
        """A source with an input port is refused at import time, so writing
        one would produce a class that cannot be loaded."""
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(kind="source", name="Frames")
        code = engine.module.code

        assert 'direction = "out"' in code
        assert 'direction = "in"' not in code
        assert "In = " not in code

    def test_it_declares_the_path_parameter(self, tmp_path):
        """Annotated with no default — a default would make it state shared by
        every node, which the decorator refuses."""
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(kind="source", name="Frames")
        code = engine.module.code

        assert "path: File" in code
        assert "path: File =" not in code

    def test_it_carries_a_named_editor_when_given_one(self, tmp_path):
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(kind="source", name="Design", viewType="product.diagram")

        assert 'viewType = "product.diagram"' in engine.module.code

    def test_it_omits_the_editor_when_none_is_given(self, tmp_path):
        """No editor means the default one, which is a real choice — writing an
        empty viewType would ask the IDE to open with an editor named ''."""
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(kind="source", name="Frames")

        assert "viewType" not in engine.module.code

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_editor_is_the_same_as_none(self, tmp_path, blank):
        """An empty viewType is not an empty choice — written out it asks the
        IDE to open the resource with an editor named "", which matches
        nothing. The wizard produces one whenever the user skips the question."""
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(kind="source", name="Frames", viewType=blank)

        assert "viewType" not in engine.module.code
        assert "@source\n" in engine.module.code

    def test_it_imports_what_the_class_needs(self, tmp_path):
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(kind="source", name="Frames")
        code = engine.module.code

        assert "source" in code.split("\n")[1]
        assert "Port" in code
        assert "File" in code

    def test_the_written_class_actually_imports(self, tmp_path):
        """The point of the op. Every assertion above is about the text; this
        one runs it, which is what the author will do."""
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(kind="source", name="Frames")

        module = tmp_path / "written.py"
        module.write_text(engine.module.code)
        namespace: dict = {}
        exec(compile(module.read_text(), str(module), "exec"), namespace)

        assert namespace["Frames"]._wfpy_meta.kind == "source"


class TestListableKinds:
    """Every kind the sidecar can create, it can also list.

    These are two hand-maintained lists of kinds, and they had already drifted:
    `streamblocks` could be created but never listed, so the picker could not
    show a type the picker itself had written.
    """

    def _engine(self, tmp_path, body):
        return _engine_from_source(tmp_path, body)

    def test_every_creatable_kind_is_listable(self, tmp_path):
        from wfpy.rewrite import TASK_TYPE_KINDS

        for kind in TASK_TYPE_KINDS:
            engine = self._engine(tmp_path, "from wfpy import workflow\n")
            assert engine.list_task_types(kind=kind) == [], (
                f"{kind!r} is creatable but not listable"
            )

    def test_it_lists_a_bare_decorator(self, tmp_path):
        engine = self._engine(
            tmp_path,
            "from wfpy import source, File, Port\n\n@source\nclass Frames:\n    pass\n",
        )

        assert engine.list_task_types(kind="source") == ["Frames"]

    def test_it_lists_a_called_decorator(self, tmp_path):
        engine = self._engine(
            tmp_path,
            'from wfpy import source\n\n@source(viewType="x")\nclass Frames:\n    pass\n',
        )

        assert engine.list_task_types(kind="source") == ["Frames"]

    def test_it_lists_a_streamblocks_type(self, tmp_path):
        """Creatable since the node shipped, listable only now."""
        engine = self._engine(
            tmp_path,
            'from wfpy import streamblocks\n\n@streamblocks(facade="design")\nclass D:\n    pass\n',
        )

        assert engine.list_task_types(kind="streamblocks") == ["D"]

    def test_it_does_not_confuse_kinds(self, tmp_path):
        engine = self._engine(
            tmp_path,
            "from wfpy import source, viewer\n\n"
            "@source\nclass Frames:\n    pass\n\n"
            "@viewer\nclass Show:\n    pass\n",
        )

        assert engine.list_task_types(kind="source") == ["Frames"]
        assert engine.list_task_types(kind="viewer") == ["Show"]

    def test_an_unknown_kind_lists_nothing(self, tmp_path):
        engine = self._engine(tmp_path, "from wfpy import task\n\n@task\nclass T:\n    pass\n")

        assert engine.list_task_types(kind="nonsense") == []
