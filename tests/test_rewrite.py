from __future__ import annotations

from pathlib import Path
from typing import Any

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


class B:
    pass


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


A = object


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


_MCP_AGENT = '''
import os

from wfpy import agent


@agent(
    prompt="do it",
    mcpServers=[
        "github",
        {"name": "docs", "transport": "stdio", "command": "npx", "env": {"DOCS_TOKEN": os.environ["DOCS_TOKEN"]}},
        {"name": "web", "url": WEB_URL},
    ],
)
class McpAgent:
    pass
'''


def _merge_agent_args(tmp_path: Path, source: str, **merge: Any) -> str:
    engine = _engine_from_source(tmp_path, source)
    engine.merge_definition_annotation_args(entityType=next(
        line.split()[1].rstrip(":") for line in source.splitlines() if line.startswith("class ")
    ), annotationName="agent", **merge)
    engine.save()
    return Path(engine.file_path).read_text(encoding="utf-8")


def test_merge_keeps_a_configured_mcp_server_the_ide_names(tmp_path: Path) -> None:
    """An IDE writes a configured server it did not edit as its name: the file's
    dict is kept as written, what it computes included; a registry name stays one."""
    updated = _merge_agent_args(tmp_path, _MCP_AGENT, argUpdates={"mcpServers": '["github", "docs", "web", "fs"]'})
    assert '"github",' in updated or '["github",' in updated
    assert '{"name": "docs", "transport": "stdio", "command": "npx", "env": {"DOCS_TOKEN": os.environ["DOCS_TOKEN"]}}' in updated
    assert '{"name": "web", "url": WEB_URL}' in updated
    assert '"fs"]' in updated
    compile(updated, "wf.py", "exec")


def test_merge_keeps_the_env_of_a_configured_mcp_server_the_ide_edits(tmp_path: Path) -> None:
    """An edited server comes without its env, which the IDE is not given: the file's is kept."""
    updated = _merge_agent_args(tmp_path, _MCP_AGENT, argUpdates={
        "mcpServers": '["github", {"name": "docs", "transport": "stdio", "command": "uvx"}, "web"]'
    })
    assert '{"name": "docs", "transport": "stdio", "command": "uvx", "env": {"DOCS_TOKEN": os.environ["DOCS_TOKEN"]}}' in updated
    assert '"npx"' not in updated
    assert "WEB_URL" in updated
    compile(updated, "wf.py", "exec")


def test_merge_leaves_out_a_configured_mcp_server_the_ide_does_not_name(tmp_path: Path) -> None:
    updated = _merge_agent_args(tmp_path, _MCP_AGENT, argUpdates={"mcpServers": '["github", "web"]'})
    assert "docs" not in updated
    assert "WEB_URL" in updated


def test_merge_moves_mcp_server_configs_into_mcp_servers(tmp_path: Path) -> None:
    """Servers the file gives as mcpServerConfigs are kept as written when the IDE
    names them in mcpServers and takes mcpServerConfigs out."""
    source = '''
import os

from wfpy import agent


@agent(prompt="do it", mcpServers=["github"], mcp_server_configs=[{"name": "docs", "command": "npx", "env": {"T": os.environ["T"]}}])
class McpAgent:
    pass
'''
    updated = _merge_agent_args(tmp_path, source, argUpdates={"mcpServers": '["github", "docs"]'}, argRemovals=["mcpServerConfigs"])
    assert 'mcpServers=["github", {"name": "docs", "command": "npx", "env": {"T": os.environ["T"]}}]' in updated
    assert "mcp_server_configs" not in updated


def test_merge_refuses_mcp_servers_the_file_computes(tmp_path: Path) -> None:
    source = '''
from wfpy import agent


@agent(prompt="do it", mcpServers=SERVERS)
class McpAgent:
    pass
'''
    with pytest.raises(RewriteError, match="computes mcpServers"):
        _merge_agent_args(tmp_path, source, argUpdates={"mcpServers": '["github", "docs"]'})
    with pytest.raises(RewriteError, match="computes mcpServers"):
        _merge_agent_args(tmp_path, source.replace("SERVERS", "[*BASE, \"x\"]"), argUpdates={"mcpServers": '["github"]'})


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


def _merged(tmp_path: Path, source: str, entity: str, updates: dict[str, str],
            removals: list[str] | None = None) -> str:
    engine = _engine_from_source(tmp_path, source)
    engine.merge_definition_annotation_args(
        entityType=entity, annotationName="agent", argUpdates=updates, argRemovals=removals
    )
    engine.save()
    return Path(engine.file_path).read_text(encoding="utf-8")


def test_merge_replaces_an_arg_written_in_the_other_spelling(tmp_path: Path) -> None:
    """The panel names args in camelCase; a file written in snake_case keeps
    its spelling, and gets no second copy of the arg."""
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x", use_skill=True, timeout_ms=1000)
class A:
    pass
''', "A", {"useSkill": "false", "timeoutMs": "2000"})
    assert '@agent(prompt="x", use_skill=False, timeout_ms=2000)' in updated
    assert "useSkill" not in updated and "timeoutMs" not in updated


def test_merge_leaves_an_arg_it_does_not_change_as_written(tmp_path: Path) -> None:
    """Two spellings of an arg the save does not touch both stay: dropping
    one would change which value the decorator reads."""
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x", use_skill=True, useSkill=False)
class A:
    pass
''', "A", {"model": '"m"'})
    assert '@agent(prompt="x", use_skill=True, useSkill=False, model="m")' in updated


def test_merge_refuses_one_arg_given_twice(tmp_path: Path) -> None:
    engine = _engine_from_source(tmp_path, '''
from wfpy import agent


@agent(prompt="x")
class A:
    pass
''')
    with pytest.raises(RewriteError, match="same argument"):
        engine.merge_definition_annotation_args(
            entityType="A", annotationName="agent", argUpdates={"useSkill": "false", "use_skill": "true"})


def test_merge_leaves_a_bare_decorator_alone_when_nothing_is_added(tmp_path: Path) -> None:
    source = '''
from wfpy import agent


@agent
class A:
    pass
'''
    assert "@agent\nclass A:" in _merged(tmp_path, source, "A", {}, ["mode"])


def test_merge_writes_ask_permissions_as_the_decorator_takes_it(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x", transport="acp")
class A:
    pass
''', "A", {"askPermissions": "true"})
    assert '@agent(prompt="x", transport="acp", askPermissions=True)' in updated
    namespace: dict[str, object] = {}
    exec(compile(updated, "a.py", "exec"), namespace)
    assert namespace["A"]._wfpy_meta.agent_spec.ask_permissions is True  # type: ignore[attr-defined]


def test_merge_leaves_one_spelling_of_an_arg_written_twice(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x", use_skill=True, model="m", useSkill=True)
class A:
    pass
''', "A", {"useSkill": "false"})
    assert '@agent(prompt="x", use_skill=False, model="m")' in updated


def test_merge_takes_reasoning_effort_for_variant(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x", variant="high")
class A:
    pass
''', "A", {"reasoningEffort": '"max"'})
    assert '@agent(prompt="x", variant="max")' in updated


def test_merge_removes_args_in_either_spelling(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x", mode="plan", timeout_ms=5, connector="claude")
class A:
    pass
''', "A", {}, ["mode", "timeoutMs"])
    assert '@agent(prompt="x", connector="claude")' in updated


def test_merge_appends_to_a_call_written_one_arg_per_line(tmp_path: Path) -> None:
    """Appended args are indented as the others, and the closing parenthesis
    stays on its own line, after a trailing comma as before."""
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(
    prompt="x",  # what it does
    stateful=True,
)
class A:
    pass
''', "A", {"askUser": "true", "mode": '"plan"'})
    assert '''@agent(
    prompt="x",  # what it does
    stateful=True,
    askUser=True,
    mode="plan",
)
class A:''' in updated


def test_merge_keeps_each_comment_with_its_arg(tmp_path: Path) -> None:
    """A comment ending the last arg's line stays on it when args are
    appended after it, and on an arg that becomes the last."""
    appended = _merged(tmp_path, '''
from wfpy import agent


@agent(
    prompt="x",
    stateful=True,  # keeps its history
)
class A:
    pass
''', "A", {"mode": '"plan"'})
    assert '''    stateful=True,  # keeps its history
    mode="plan",
)''' in appended
    removed = _merged(tmp_path, '''
from wfpy import agent


@agent(
    prompt="x",  # what it does
    mode="plan",  # read only
)
class B:
    pass
''', "B", {}, ["mode"])
    assert '''@agent(
    prompt="x",  # what it does
)
class B:''' in removed


def test_merge_keeps_the_comment_of_an_arg_that_becomes_the_last_on_the_closing_line(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x",  # c
       model="m")
class A:
    pass
''', "A", {}, ["model"])
    assert '@agent(prompt="x"  # c\n)\nclass A:' in updated


def test_merge_keeps_the_break_before_the_arg_after_a_removed_one(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x", model="m",
       mode="plan")
class A:
    pass
''', "A", {}, ["model"])
    assert '@agent(prompt="x",\n       mode="plan")' in updated


def test_merge_keeps_the_comments_when_it_removes_every_arg(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(  # note
    mode="plan",
    # keep me
)
class A:
    pass
''', "A", {}, ["mode"])
    assert "@agent(  # note\n    # keep me\n)\nclass A:" in updated


def test_merge_moves_the_comment_lines_above_an_arg_with_it(tmp_path: Path) -> None:
    source = '''
from wfpy import agent


@agent(
    prompt="x",
    # the model
    model="m",
    # trailing note
)
class A:
    pass
'''
    assert '''@agent(
    prompt="x",
    # trailing note
)''' in _merged(tmp_path, source, "A", {}, ["model"])
    assert '''@agent(
    # the model
    model="m",
    # trailing note
)''' in _merged(tmp_path, source, "A", {}, ["prompt"])
    assert '''@agent(
    prompt="x",
    # the model
    model="m",
    provider="p",
    # trailing note
)''' in _merged(tmp_path, source, "A", {"provider": '"p"'})


def test_merge_appends_to_a_lone_arg_on_its_own_line(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(
    prompt="x"
)
class A:
    pass
''', "A", {"model": '"m"'})
    assert '''@agent(
    prompt="x",
    model="m"
)
class A:''' in updated


def test_merge_appends_to_a_call_on_one_line(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x", model="m")
class A:
    pass
''', "A", {"provider": '"p"'})
    assert '@agent(prompt="x", model="m", provider="p")' in updated


def test_merge_removing_the_last_arg_keeps_the_call_s_ending(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(
    prompt="x",
    model="m",
    mode="plan",
)
class A:
    pass
''', "A", {}, ["mode"])
    assert '''@agent(
    prompt="x",
    model="m",
)
class A:''' in updated


def test_merge_removing_every_arg_leaves_an_empty_call(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(
    mode="plan",
)
class A:
    pass
''', "A", {}, ["mode"])
    assert "@agent()\nclass A:" in updated


def test_merge_keeps_the_place_of_a_line_whose_first_arg_is_removed(tmp_path: Path) -> None:
    """The arg after the removed one stays on its line: it went to the
    decorator's column when the line above ended with a comment, and up onto
    that line when it did not."""
    source = '''
from wfpy import agent


@agent(
    prompt="x",{comment}
    model="m", mode="plan",
)
class A:
    pass
'''
    for comment in ("  # the task", ""):
        updated = _merged(tmp_path, source.replace("{comment}", comment), "A", {}, ["model"])
        assert f'@agent(\n    prompt="x",{comment}\n    mode="plan",\n)\nclass A:' in updated


def test_merge_keeps_the_place_of_a_line_whose_first_arg_was_a_second_spelling(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(
    use_skill=True,  # which skill
    useSkill=False, model="m",
)
class A:
    pass
''', "A", {"useSkill": "false"})
    assert '@agent(\n    use_skill=False,  # which skill\n    model="m",\n)\nclass A:' in updated


def test_merge_indents_an_arg_appended_after_a_comment_as_the_call_does(tmp_path: Path) -> None:
    """The comment keeps its arg's line, so the appended arg opens a line: at
    the indentation of the call's args, not at the decorator's column."""
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(
    prompt="x", model="m",
    mode="plan",  # read only
)
class A:
    pass
''', "A", {"provider": '"p"'})
    assert '    mode="plan",  # read only\n    provider="p",\n)\nclass A:' in updated

    hanging = _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x", model="m",
       mode="plan",  # read only
)
class A:
    pass
''', "A", {"provider": '"p"'})
    assert '       mode="plan",  # read only\n       provider="p",\n)\nclass A:' in hanging

    lone = _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x"  # the task
)
class A:
    pass
''', "A", {"model": '"m"'})
    assert '@agent(prompt="x",  # the task\n    model="m"\n)\nclass A:' in lone


LEADING_COMMAS = '''
from wfpy import agent


@agent(
    prompt="x"  # the task
    # which model
    , model="m"  # the model
    , mode="plan"  # read only
)
class A:
    pass
'''


def test_merge_appends_to_a_call_whose_commas_open_the_lines(tmp_path: Path) -> None:
    """Each comment is before the comma that follows its arg there: it stays
    on its arg, and is not written again on the args that are appended."""
    updated = _merged(tmp_path, LEADING_COMMAS, "A", {"provider": '"p"', "connector": '"claude"'})
    assert (
        '@agent(\n'
        '    prompt="x"  # the task\n'
        '    # which model\n'
        '    , model="m"  # the model\n'
        '    , mode="plan"  # read only\n'
        '    , provider="p"\n'
        '    , connector="claude"\n'
        ')\nclass A:'
    ) in updated


def test_merge_removes_from_a_call_whose_commas_open_the_lines(tmp_path: Path) -> None:
    """A removed arg takes its own comments, and no other arg's."""
    middle = _merged(tmp_path, LEADING_COMMAS, "A", {}, ["model"])
    assert '@agent(\n    prompt="x"  # the task\n    , mode="plan"  # read only\n)\nclass A:' in middle

    last = _merged(tmp_path, LEADING_COMMAS, "A", {}, ["mode"])
    assert (
        '@agent(\n    prompt="x"  # the task\n    # which model\n    , model="m"  # the model\n)\nclass A:'
    ) in last

    first = _merged(tmp_path, LEADING_COMMAS, "A", {}, ["prompt"])
    assert '@agent(\n    # which model\n    model="m"  # the model\n    , mode="plan"  # read only\n)\nclass A:' in first


def test_merge_refuses_removals_that_are_not_a_list_of_names(tmp_path: Path) -> None:
    """A bare string would be read letter by letter: "mode" took out `m` and `o`."""
    source = '''
from wfpy import agent


@agent(prompt="x", mode="plan", m=1, o=2)
class A:
    pass
'''
    for removals in ("mode", ["mode", 3]):
        engine = _engine_from_source(tmp_path, source)
        with pytest.raises(RewriteError, match="list of argument names"):
            engine.merge_definition_annotation_args(
                entityType="A", annotationName="agent", argUpdates={}, argRemovals=removals)  # type: ignore[arg-type]
        assert Path(engine.file_path).read_text(encoding="utf-8") == source


def test_merge_updates_a_name_given_to_update_and_to_remove(tmp_path: Path) -> None:
    updated = _merged(tmp_path, '''
from wfpy import agent


@agent(prompt="x", mode="plan")
class A:
    pass
''', "A", {"mode": '"build"'}, ["mode"])
    assert '@agent(prompt="x", mode="build")\nclass A:' in updated


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

        assert '@streamblocks(facade="design")' in code
        assert "class MyDesign:" in code

    def test_an_instance_carries_its_network(self, tmp_path):
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(
            kind="streamblocks", name="FirRun", facade="instance", network="designs/adder.py"
        )

        assert 'network="designs/adder.py"' in engine.module.code

    def test_it_imports_what_the_class_needs(self, tmp_path):
        """The sidecar wrote the class, so the sidecar owes the imports —
        otherwise the author gets a NameError to fix by hand."""
        engine = _engine_from_source(tmp_path, self.SOURCE)
        engine.create_task_type(kind="streamblocks", name="MyDesign", facade="design")
        code = engine.module.code

        wfpy_import = next(line for line in code.split("\n") if line.startswith("from wfpy import"))
        assert "streamblocks" in wfpy_import  # folded into the wfpy import
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

        assert 'direction="out"' in code
        assert 'direction="in"' not in code
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

        assert 'viewType="product.diagram"' in engine.module.code

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

        wfpy_import = next(line for line in code.split("\n") if line.startswith("from wfpy import"))
        assert "source" in wfpy_import
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
