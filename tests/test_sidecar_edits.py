"""Diagram edits on a project laid out as real ones are.

A package of task classes re-exported from its `__init__.py`, a workflow
factory (`def pair(...): @workflow def pair(): ...; return pair`), and a flow
that imports both: what the IDE edits every day. Each test sends the request
the IDE sends, reads the file the sidecar wrote, and checks it still plans.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wfpy.sidecar import FEATURES, SUPPORTED_OPS, _handle_request

REPO = Path(__file__).resolve().parents[1]

FILES = {
    "compile/__init__.py": "",
    "compile/agentic/__init__.py": "",
    "compile/workflows/__init__.py": "",
    "compile/other/__init__.py": "",
    "compile/sources/__init__.py": (
        'from .text import Text\nfrom .join import Join\n\n__all__ = ["Join", "Text"]\n'
    ),
    "compile/sources/text.py": '''from wfpy import Port, action, guard, task


@task
class Text:
    """A string, emitted once."""

    text: str              # parameter
    emitted: bool = False  # state

    class Ports:
        Out = Port[str](direction="out")

    @action(consumes={}, produces={"Out": 1})
    @guard(lambda self: not self.emitted)
    def emit(self) -> dict:
        self.emitted = True
        return {"Out": self.text}
''',
    "compile/sources/join.py": """from wfpy import Port, action, task


@task
class Join:
    class Ports:
        Left = Port[str](direction="in")
        Right = Port[str](direction="in")
        Out = Port[str](direction="out")

    @action(consumes={"Left": 1, "Right": 1}, produces={"Out": 1})
    def join(self, left, right) -> dict:
        return {"Out": left + right}
""",
    "compile/other/text.py": '''from wfpy import Port, connect, task, workflow


@task
class Text:
    """Another class of the same name."""

    class Ports:
        Out = Port[str](direction="out")


@workflow(outputs={"Out": str})
def other():
    t = Text()
    connect(t.Out, "Out")
''',
    "compile/agentic/gate.py": """from wfpy import Port, action, task


@task
class Gate:
    class Ports:
        In = Port[str](direction="in")

    @action(consumes={"In": 1}, produces={})
    def hold(self, value) -> None:
        pass
""",
    "compile/workflows/pair.py": '''"""A workflow factory."""
from wfpy import connect, workflow

from compile.sources import Join, Text


def pair(label: str, times: int = 2):
    """Builds `pair` for a label."""

    @workflow(
        inputs={"In": str},
        outputs={"Out": str},
    )
    def pair():
        """The inner workflow."""
        first = Text(text=label)
        joined = Join()

        connect("In", joined.Left)
        connect(first.Out, joined.Right)
        connect(joined.Out, "Out")

    return pair
''',
    "compile/flow.py": '''"""A flow, run by hand."""

import os

from wfpy import connect, workflow

from compile.sources import Text
from compile.workflows.pair import pair

LABEL = os.environ.get("LABEL", "hello")


@workflow(outputs={"Out": str, "Log": str,
                   "Extra": str})
def flow():
    """The flow."""
    front = Text(text=LABEL)
    back = Text(text="tail")
    spare = Text(text="spare")
    duo = pair(label=LABEL, times=3)()

    connect(front.Out, duo.In)
    connect(duo.Out, "Out")
    connect(back.Out, "Log")
    for name in ("Extra",):
        connect(spare.Out, name)
''',
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    for name, text in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _send(file: Path, op: str, **args: object) -> dict:
    response = _handle_request({"file": str(file), "op": f"wfpy.{op}", "args": args})
    return {
        "status": response.status,
        "message": response.message,
        "diagnostic": response.diagnostic or {},
        "changedFiles": response.changedFiles,
    }


def _plans(root: Path, file: str, workflow: str | None = None, hierarchy: bool = False) -> dict:
    """`wfpy plan <file> --format graph`, as the IDE runs it, in a fresh interpreter."""
    command = [sys.executable, "-m", "wfpy.cli", "plan", file, "--format", "graph"]
    if workflow:
        command += ["--workflow", workflow]
    if hierarchy:
        command += ["--hierarchy"]
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    done = subprocess.run(command, cwd=root, capture_output=True, text=True, env=env, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def _text(root: Path, name: str) -> str:
    return (root / name).read_text(encoding="utf-8")


def test_the_project_plans_as_written(project: Path) -> None:
    _plans(project, "compile/flow.py")
    _plans(project, "compile/other/text.py")


# ── S1 createPort ──────────────────────────────────────────────────────


def test_create_port_adds_to_the_decorator_and_nothing_else(project: Path) -> None:
    before = _text(project, "compile/flow.py")
    answer = _send(
        project / "compile/flow.py",
        "createPort",
        workflow="flow",
        direction="input",
        portName="Program",
        portType="str",
    )
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/flow.py")
    assert '@workflow(inputs={"Program": str}, outputs={"Out": str, "Log": str,' in after
    # The rest of the module is as it was.
    assert after.replace('inputs={"Program": str}, ', "") == before
    _plans(project, "compile/flow.py")


def test_create_port_keeps_a_dict_written_one_per_line(project: Path) -> None:
    answer = _send(
        project / "compile/workflows/pair.py",
        "createPort",
        workflow="pair",
        direction="output",
        portName="Count",
        portType="int",
    )
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/workflows/pair.py")
    assert 'outputs={"Out": str, "Count": int},' in after
    assert "    def pair():" in after and "return pair" in after
    _plans(project, "compile/flow.py")


def test_create_port_refuses_a_name_already_declared(project: Path) -> None:
    answer = _send(
        project / "compile/flow.py",
        "createPort",
        workflow="flow",
        direction="output",
        portName="Log",
        portType="str",
    )
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "port_exists"


# ── S2 deletePort ──────────────────────────────────────────────────────


def test_delete_port_takes_it_and_its_connections_out(project: Path) -> None:
    answer = _send(
        project / "compile/flow.py",
        "deletePort",
        workflow="flow",
        direction="output",
        portName="Log",
    )
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/flow.py")
    assert '"Log"' not in after
    assert 'connect(back.Out, "Log")' not in after
    assert '@workflow(outputs={"Out": str, "Extra": str})' in after
    _plans(project, "compile/flow.py")


def test_delete_port_refuses_a_port_used_by_name(project: Path) -> None:
    before = _text(project, "compile/flow.py")
    answer = _send(
        project / "compile/flow.py",
        "deletePort",
        workflow="flow",
        direction="output",
        portName="Extra",
    )
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "port_used_by_name"
    assert "line 25" in answer["message"]
    assert _text(project, "compile/flow.py") == before


def test_delete_port_still_knows_the_assigned_form(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(
        "from wfpy import workflow\n\n\n"
        "@workflow\n"
        "def demo():\n"
        "    pass\n\n\n"
        'demo.outputs = {"A": int, "B": int}\n',
        encoding="utf-8",
    )
    answer = _send(src, "deletePort", workflow="demo", direction="output", portName="A")
    assert answer["status"] == "ok", answer
    assert 'demo.outputs = {"B": int}' in src.read_text(encoding="utf-8")


# ── S3 renaming a workflow's own port (sent as renameNode) ────────────


def test_rename_a_workflow_port_follows_its_connections(project: Path) -> None:
    answer = _send(
        project / "compile/workflows/pair.py", "renameNode", workflow="pair", old="In", new="Feed"
    )
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/workflows/pair.py")
    assert 'inputs={"Feed": str},' in after
    assert 'connect("Feed", joined.Left)' in after
    assert '"In"' not in after


def test_rename_a_workflow_port_used_by_name_is_refused_whole(project: Path) -> None:
    before = _text(project, "compile/flow.py")
    answer = _send(
        project / "compile/flow.py", "renameNode", workflow="flow", old="Extra", new="More"
    )
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "port_used_by_name"
    assert (
        "is used by name at line 25, which this rename cannot follow: rename it there"
        in answer["message"]
    )
    assert _text(project, "compile/flow.py") == before


def test_rename_an_instance_still_renames_the_instance(project: Path) -> None:
    answer = _send(
        project / "compile/flow.py", "renameNode", workflow="flow", old="back", new="rear"
    )
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/flow.py")
    assert 'rear = Text(text="tail")' in after and 'connect(rear.Out, "Log")' in after
    _plans(project, "compile/flow.py")


# ── S4 renamePort / updatePortType with the IDE's arguments ───────────


def test_update_port_type_takes_the_ides_arguments(project: Path) -> None:
    answer = _send(
        project / "compile/sources/join.py",
        "updatePortType",
        entity="Join",
        portDirection="output",
        portName="Out",
        newValue="int",
    )
    assert answer["status"] == "ok", answer
    assert 'Out = Port[int](direction="out")' in _text(project, "compile/sources/join.py")


def test_rename_port_takes_the_ides_arguments_and_follows_the_class(project: Path) -> None:
    answer = _send(
        project / "compile/sources/text.py",
        "renamePort",
        entity="Text",
        portDirection="output",
        portName="Out",
        newValue="Value",
    )
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/sources/text.py")
    assert 'Value = Port[str](direction="out")' in after
    assert 'produces={"Value": 1}' in after
    assert 'return {"Value": self.text}' in after


def test_rename_port_to_a_name_taken_is_refused(project: Path) -> None:
    answer = _send(
        project / "compile/sources/join.py",
        "renamePort",
        entity="Join",
        portDirection="input",
        portName="Left",
        newValue="Right",
    )
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "port_exists"


# ── S5 ports that say their direction ─────────────────────────────────


def test_create_entity_port_input_says_in(project: Path) -> None:
    answer = _send(
        project / "compile/sources/join.py",
        "createEntityPort",
        entityType="Join",
        portDirection="input",
        portName="Extra",
        portType="str",
    )
    assert answer["status"] == "ok", answer
    assert 'Extra = Port[str](direction="in")' in _text(project, "compile/sources/join.py")


def test_create_task_type_says_each_ports_direction(project: Path) -> None:
    answer = _send(project / "compile/flow.py", "createTaskType", kind="task", name="Splitter")
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/flow.py")
    assert 'In = Port[Any](direction="in")' in after
    assert 'Out = Port[Any](direction="out")' in after
    assert "\n\n\n@task\nclass Splitter:\n" in after
    assert '        Out = Port[Any](direction="out")\n\n\n@workflow(' in after
    # The imports where imports are: `typing` beside `os`, after the docstring's blank line.
    assert after.startswith(
        '"""A flow, run by hand."""\n\nimport os\nfrom typing import Any\n\nfrom wfpy import Port, connect, task, workflow\n'
    )
    _plans(project, "compile/flow.py")


# ── S6 / S7 the uses of a class's port across the project ─────────────


def test_port_uses_finds_the_classs_instances_by_definition(project: Path) -> None:
    assert "portUses" in SUPPORTED_OPS
    assert FEATURES["portUsesAcrossFiles"] is True and FEATURES["portUsesQuery"] is True
    answer = _send(project / "compile/sources/text.py", "portUses", entity="Text")
    assert answer["status"] == "ok", answer
    used_in = answer["diagnostic"]["usedIn"]
    assert used_in == [
        {
            "file": str((project / "compile/flow.py").resolve()),
            "workflow": "flow",
            "instances": ["front", "back", "spare"],
        },
        {
            "file": str((project / "compile/workflows/pair.py").resolve()),
            "workflow": "pair",
            "instances": ["first"],
        },
    ]
    # compile/other/text.py's own `Text` is another class.
    assert answer["diagnostic"]["unchecked"] == []


def test_port_uses_tells_a_file_that_does_not_parse(project: Path) -> None:
    (project / "compile/broken.py").write_text(
        "from wfpy import workflow\nText(\n", encoding="utf-8"
    )
    answer = _send(project / "compile/sources/text.py", "portUses", entity="Text")
    assert [entry["file"] for entry in answer["diagnostic"]["unchecked"]] == [
        str(project / "compile/broken.py")
    ]
    assert answer["diagnostic"]["unchecked"][0]["message"]


def _uses(project: Path) -> list[dict]:
    return _send(project / "compile/sources/text.py", "portUses", entity="Text")["diagnostic"][
        "usedIn"
    ]


def test_rename_port_follows_every_file_it_is_told(project: Path) -> None:
    answer = _send(
        project / "compile/sources/text.py",
        "renamePort",
        entity="Text",
        portDirection="output",
        portName="Out",
        newValue="Value",
        usedIn=_uses(project),
    )
    assert answer["status"] == "ok", answer
    assert "connect(front.Value, duo.In)" in _text(project, "compile/flow.py")
    assert "connect(spare.Value, name)" in _text(project, "compile/flow.py")
    assert "connect(first.Value, joined.Right)" in _text(project, "compile/workflows/pair.py")
    # Another class's port of the same name is its own.
    assert "connect(t.Out" in _text(project, "compile/other/text.py")
    assert sorted(Path(entry["file"]).name for entry in answer["changedFiles"]) == [
        "flow.py",
        "pair.py",
    ]
    _plans(project, "compile/flow.py")


def test_delete_entity_port_refuses_a_connected_port(project: Path) -> None:
    before = _text(project, "compile/sources/join.py")
    uses = [
        {
            "file": str(project / "compile/workflows/pair.py"),
            "workflow": "pair",
            "instances": ["joined"],
        }
    ]
    answer = _send(
        project / "compile/sources/join.py",
        "deleteEntityPort",
        entityType="Join",
        portDirection="output",
        portName="Out",
        usedIn=uses,
    )
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "port_in_use"
    assert _text(project, "compile/sources/join.py") == before


def test_delete_entity_port_refuses_a_port_an_action_reads(project: Path) -> None:
    answer = _send(
        project / "compile/sources/join.py",
        "deleteEntityPort",
        entityType="Join",
        portDirection="input",
        portName="Left",
    )
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "port_in_use"
    assert "read by action 'join'" in answer["message"]


def test_delete_entity_port_names_the_workflow_that_connects_it(project: Path) -> None:
    (project / "compile/sources/join.py").write_text(
        _text(project, "compile/sources/join.py").replace(
            '        Out = Port[str](direction="out")\n',
            '        Out = Port[str](direction="out")\n        Spare = Port[str](direction="out")\n',
        ),
        encoding="utf-8",
    )
    (project / "compile/workflows/pair.py").write_text(
        _text(project, "compile/workflows/pair.py").replace(
            '        connect(joined.Out, "Out")\n',
            '        connect(joined.Out, "Out")\n        connect(joined.Spare, "Out")\n',
        ),
        encoding="utf-8",
    )
    uses = [
        {
            "file": str(project / "compile/workflows/pair.py"),
            "workflow": "pair",
            "instances": ["joined"],
        }
    ]
    answer = _send(
        project / "compile/sources/join.py",
        "deleteEntityPort",
        entityType="Join",
        portDirection="output",
        portName="Spare",
        usedIn=uses,
    )
    assert answer["status"] == "error"
    assert (
        answer["message"]
        == "port 'Spare' of 'joined' is connected in workflow 'pair': delete its connections first"
    )


def test_delete_entity_port_that_nothing_uses(project: Path) -> None:
    _send(
        project / "compile/sources/join.py",
        "createEntityPort",
        entityType="Join",
        portDirection="input",
        portName="Extra",
        portType="str",
    )
    answer = _send(
        project / "compile/sources/join.py",
        "deleteEntityPort",
        entityType="Join",
        portDirection="input",
        portName="Extra",
        usedIn=[],
    )
    assert answer["status"] == "ok", answer
    assert "Extra" not in _text(project, "compile/sources/join.py")


# ── S8 createNode of a type the module does not import ────────────────


def test_create_node_imports_from_the_package_that_re_exports_it(project: Path) -> None:
    flow = project / "compile/flow.py"
    flow.write_text(
        _text(project, "compile/flow.py").replace(
            "from compile.sources import Text\n", "from compile.sources import Text\n"
        ),
        encoding="utf-8",
    )
    answer = _send(flow, "createNode", workflow="flow", type="Join", name="joined")
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/flow.py")
    assert "from compile.sources import Join, Text\n" in after
    assert "    joined = Join()\n" in after
    _plans(project, "compile/flow.py")


def test_create_node_imports_from_the_defining_module(project: Path) -> None:
    answer = _send(
        project / "compile/flow.py", "createNode", workflow="flow", type="Gate", name="gate"
    )
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/flow.py")
    assert (
        "from compile.workflows.pair import pair\nfrom compile.agentic.gate import Gate\n" in after
    )
    _plans(project, "compile/flow.py")


def test_create_node_refuses_a_type_the_project_does_not_have(project: Path) -> None:
    answer = _send(
        project / "compile/flow.py", "createNode", workflow="flow", type="Nowhere", name="n"
    )
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "type_not_found"


def test_create_node_refuses_a_type_defined_twice(project: Path) -> None:
    answer = _send(project / "compile/workflows/__init__.py", "createWorkflowType", name="holder")
    assert answer["status"] == "ok"
    answer = _send(
        project / "compile/workflows/__init__.py",
        "createNode",
        workflow="holder",
        type="Text",
        name="t",
    )
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "type_ambiguous"


# ── S9 the workflow a factory builds ──────────────────────────────────


def test_rename_node_in_a_factory_workflow(project: Path) -> None:
    answer = _send(
        project / "compile/workflows/pair.py",
        "renameNode",
        workflow="pair",
        old="first",
        new="head",
    )
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/workflows/pair.py")
    assert "        head = Text(text=label)\n" in after
    assert "connect(head.Out, joined.Right)" in after
    _plans(project, "compile/flow.py")


def test_delete_node_in_a_factory_workflow(project: Path) -> None:
    answer = _send(
        project / "compile/workflows/pair.py", "deleteNode", workflow="pair", name="first"
    )
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/workflows/pair.py")
    assert "first" not in after
    assert "    return pair\n" in after


def test_create_node_and_connect_in_a_factory_workflow(project: Path) -> None:
    pair = project / "compile/workflows/pair.py"
    assert (
        _send(
            pair,
            "createNode",
            workflow="pair",
            type="Text",
            name="second",
            expressions={"text": "label"},
        )["status"]
        == "ok"
    )
    answer = _send(pair, "connect", workflow="pair", from_expr="second.Out", to_expr='"Out"')
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/workflows/pair.py")
    inner = after.split("    def pair():", 1)[1]
    assert "        second = Text(text=label)\n" in inner
    assert '        connect(second.Out, "Out")\n' in inner
    assert after.split("    def pair():", 1)[0].count("second") == 0


def test_update_node_parameter_of_a_factory_made_instance(project: Path) -> None:
    answer = _send(
        project / "compile/flow.py",
        "updateNodeParameter",
        workflow="flow",
        entity="duo",
        parameterName="times",
        newValue="4",
    )
    assert answer["status"] == "ok", answer
    assert "duo = pair(label=LABEL, times=4)()" in _text(project, "compile/flow.py")
    _plans(project, "compile/flow.py")


# ── S10 a class task's parameter is a field without a default ─────────


def test_definition_parameter_of_a_class_task_is_a_field(project: Path) -> None:
    answer = _send(
        project / "compile/sources/text.py",
        "updateDefinitionParameter",
        entityType="Text",
        parameterName="prefix",
        parameterText="prefix: str",
    )
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/sources/text.py")
    assert (
        "    text: str              # parameter\n    prefix: str  # parameter\n    emitted: bool = False"
        in after
    )
    assert "__init__" not in after


def test_definition_parameter_of_a_class_task_refuses_a_default(project: Path) -> None:
    before = _text(project, "compile/sources/text.py")
    answer = _send(
        project / "compile/sources/text.py",
        "updateDefinitionParameter",
        entityType="Text",
        parameterName="prefix",
        parameterText="prefix: str = ''",
    )
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "parameter_has_default"
    assert _text(project, "compile/sources/text.py") == before


def test_definition_parameter_retypes_an_existing_field(project: Path) -> None:
    answer = _send(
        project / "compile/sources/text.py",
        "updateDefinitionParameter",
        entityType="Text",
        parameterName="text",
        parameterText="text: bytes",
    )
    assert answer["status"] == "ok", answer
    assert "    text: bytes              # parameter\n" in _text(project, "compile/sources/text.py")


# ── S11 a decorator the module does not import ────────────────────────


def test_update_definition_annotation_imports_a_wfpy_decorator(project: Path) -> None:
    answer = _send(
        project / "compile/sources/join.py",
        "updateDefinitionAnnotation",
        entityType="Join",
        annotationName="tool",
        annotationText='@tool(\n    cmd="bash",\n    inheritStdio=True\n)',
    )
    assert answer["status"] == "ok", answer
    after = _text(project, "compile/sources/join.py")
    assert after.startswith("from wfpy import Port, action, task, tool\n")


# ── S15 a value that is not Python ────────────────────────────────────


@pytest.mark.parametrize(
    "op, args",
    [
        (
            "updateNodeParameter",
            {"workflow": "flow", "entity": "front", "parameterName": "text", "newValue": "td=on'"},
        ),
        (
            "createPort",
            {"workflow": "flow", "direction": "input", "portName": "P", "portType": "list["},
        ),
        (
            "createNode",
            {"workflow": "flow", "type": "Text", "name": "t2", "expressions": {"text": "'a"}},
        ),
        (
            "updateDefinitionAnnotation",
            {"entityType": "Text", "annotationName": "tool", "annotationText": "@tool(cmd=)"},
        ),
    ],
)
def test_a_value_that_is_not_python_is_refused_by_name(project: Path, op: str, args: dict) -> None:
    before = _text(project, "compile/flow.py")
    answer = _send(project / "compile/flow.py", op, **args)
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "invalid_expression", answer
    assert "is not a Python expression" in answer["message"]
    assert _text(project, "compile/flow.py") == before


# ── A1 typeParameters / A2 createNode expressions and names ───────────


def test_type_parameters_of_a_class_task(project: Path) -> None:
    assert "typeParameters" in SUPPORTED_OPS and FEATURES["typeParameters"] is True
    answer = _send(project / "compile/flow.py", "typeParameters", type="Text")
    assert answer["status"] == "ok", answer
    assert answer["diagnostic"]["parameters"] == [{"name": "text", "type": "str"}]
    assert answer["diagnostic"]["kind"] == "task"


def test_type_parameters_of_a_factory(project: Path) -> None:
    answer = _send(project / "compile/flow.py", "typeParameters", type="pair")
    assert answer["diagnostic"]["kind"] == "factory"
    assert answer["diagnostic"]["parameters"] == [
        {"name": "label", "type": "str"},
        {"name": "times", "type": "int", "defaultValue": "2"},
    ]


def test_type_parameters_of_an_unknown_type(project: Path) -> None:
    answer = _send(project / "compile/flow.py", "typeParameters", type="Nowhere")
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "type_not_found"


def test_create_node_writes_expressions_verbatim_after_params(project: Path) -> None:
    assert FEATURES["createNodeExpressions"] is True
    answer = _send(
        project / "compile/flow.py",
        "createNode",
        workflow="flow",
        type="Text",
        name="again",
        expressions={"text": "LABEL"},
    )
    assert answer["status"] == "ok", answer
    assert "    again = Text(text=LABEL)\n" in _text(project, "compile/flow.py")
    _plans(project, "compile/flow.py")


@pytest.mark.parametrize("name", ["Text", "LABEL", "os", "pair", "flow"])
def test_create_node_refuses_a_name_that_shadows(project: Path, name: str) -> None:
    answer = _send(
        project / "compile/flow.py", "createNode", workflow="flow", type="Text", name=name
    )
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "name_shadows", answer


@pytest.mark.parametrize(("name", "line"), [("back", 18), ("name", 25)])
def test_create_node_refuses_a_name_the_workflow_has(project: Path, name: str, line: int) -> None:
    before = _text(project, "compile/flow.py")
    answer = _send(project / "compile/flow.py", "createNode", workflow="flow", type="Text", name=name)
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "name_exists", answer
    assert answer["diagnostic"]["line"] == line
    assert _text(project, "compile/flow.py") == before


@pytest.mark.parametrize(
    ("new", "code"),
    [("Text", "name_shadows"), ("LABEL", "name_shadows"), ("pair", "name_shadows"), ("back", "name_exists"), ("name", "name_exists")],
)
def test_rename_node_refuses_a_name_the_workflow_has_or_reads(project: Path, new: str, code: str) -> None:
    before = _text(project, "compile/flow.py")
    answer = _send(project / "compile/flow.py", "renameNode", workflow="flow", old="spare", new=new)
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == code, answer
    assert _text(project, "compile/flow.py") == before


def test_rename_node_to_its_own_name_changes_nothing(project: Path) -> None:
    before = _text(project, "compile/flow.py")
    answer = _send(project / "compile/flow.py", "renameNode", workflow="flow", old="spare", new="spare")
    assert answer["status"] == "ok", answer
    assert _text(project, "compile/flow.py") == before


def test_rename_node_refuses_a_name_a_factory_parameter_has(project: Path) -> None:
    answer = _send(project / "compile/workflows/pair.py", "renameNode", workflow="pair", old="first", new="label")
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "name_shadows"


@pytest.mark.parametrize("text", ["MISSING", "LABEL + MISSING", "str(object=MISSING)", "[x for x in MISSING]"])
def test_create_node_refuses_an_expression_that_reads_an_unbound_name(project: Path, text: str) -> None:
    before = _text(project, "compile/flow.py")
    answer = _send(project / "compile/flow.py", "createNode", workflow="flow", type="Text", name="again", expressions={"text": text})
    assert answer["status"] == "error"
    assert answer["diagnostic"] == {"code": "name_not_bound", "name": "MISSING"}, answer
    assert _text(project, "compile/flow.py") == before


@pytest.mark.parametrize(
    "text",
    ['LABEL + "!"', "os.environ.get('X', LABEL)", "str(len(LABEL))", "[c for c in LABEL]", "(lambda v: v)(LABEL)", "front.Out"],
)
def test_create_node_takes_an_expression_of_names_in_scope(project: Path, text: str) -> None:
    answer = _send(project / "compile/flow.py", "createNode", workflow="flow", type="Text", name="again", expressions={"text": text})
    assert answer["status"] == "ok", answer


def test_create_node_refuses_an_expression_of_a_name_bound_below_it(project: Path) -> None:
    """The node lands above the connects; `name`, the loop's variable
    below them, is the workflow's local and not bound yet there
    (UnboundLocalError)."""
    before = _text(project, "compile/flow.py")
    answer = _send(project / "compile/flow.py", "createNode", workflow="flow", type="Text", name="again", expressions={"text": "name"})
    assert answer["status"] == "error"
    assert answer["diagnostic"] == {"code": "name_not_bound", "name": "name", "line": 25}, answer
    assert _text(project, "compile/flow.py") == before


def test_a_factory_workflows_node_reads_the_factorys_parameters(project: Path) -> None:
    answer = _send(project / "compile/workflows/pair.py", "createNode", workflow="pair", type="Text", name="third", expressions={"text": "label * times"})
    assert answer["status"] == "ok", answer
    refused = _send(project / "compile/workflows/pair.py", "updateNodeParameter", workflow="pair", entity="third", parameterName="text", newValue="LABEL")
    assert refused["diagnostic"] == {"code": "name_not_bound", "name": "LABEL"}, refused
    _plans(project, "compile/flow.py")


def test_create_node_refuses_a_name_a_factory_parameter_has(project: Path) -> None:
    answer = _send(
        project / "compile/workflows/pair.py",
        "createNode",
        workflow="pair",
        type="Text",
        name="label",
    )
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "name_shadows"


# ── A workflow's own port, used by the workflows that instantiate it ──


def test_port_uses_of_a_factory_workflow(project: Path) -> None:
    answer = _send(project / "compile/workflows/pair.py", "portUses", entity="pair")
    assert answer["status"] == "ok", answer
    assert answer["diagnostic"]["usedIn"] == [
        {"file": str((project / "compile/flow.py").resolve()), "workflow": "flow", "instances": ["duo"]},
    ]


def test_rename_a_workflow_port_follows_the_workflows_using_it(project: Path) -> None:
    uses = _send(project / "compile/workflows/pair.py", "portUses", entity="pair")["diagnostic"]["usedIn"]
    answer = _send(project / "compile/workflows/pair.py", "renameNode",
                   workflow="pair", old="In", new="Feed", usedIn=uses)
    assert answer["status"] == "ok", answer
    assert "connect(front.Out, duo.Feed)" in _text(project, "compile/flow.py")
    assert [Path(entry["file"]).name for entry in answer["changedFiles"]] == ["flow.py"]
    _plans(project, "compile/flow.py")


def test_delete_a_workflow_port_a_parent_connects_is_refused(project: Path) -> None:
    uses = _send(project / "compile/workflows/pair.py", "portUses", entity="pair")["diagnostic"]["usedIn"]
    before = _text(project, "compile/workflows/pair.py")
    answer = _send(project / "compile/workflows/pair.py", "deletePort",
                   workflow="pair", direction="input", portName="In", usedIn=uses)
    assert answer["status"] == "error"
    assert answer["message"] == "port 'In' of 'duo' is connected in workflow 'flow': delete its connections first"
    assert _text(project, "compile/workflows/pair.py") == before


# ── S12-S14 what the export says the source wrote ─────────────────────

NET = """from wfpy import File, Port, source, streamblocks, viewer

NETWORK = "designs/top.py"


@streamblocks(facade="instance", network=NETWORK)
class Net:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")


@source(viewType="vscode.markdown.preview.editor")
class Doc:
    path: File

    class Ports:
        Out = Port[File](direction="out")


@viewer(inputs=["In"])
class Show:
    class Ports:
        In = Port[str](direction="in")
"""

NET_FLOW = """from wfpy import File, connect, workflow

from compile.sources.net import Doc, Net, Show

README = "README.md"


@workflow(inputs={"In": str})
def net_flow():
    doc = Doc(path=File(path=README))
    net = Net()
    show = Show()
    connect("In", net.In)
    connect(net.Out, show.In)
"""


def _node(exported: dict, label: str) -> dict:
    return next(n for n in exported["graph"]["nodes"] if n["label"] == label)


def _annotation(node: dict, name: str) -> dict:
    return next(a for a in node["meta"]["definitionAnnotations"] if a["name"] == name)


def test_annotation_values_are_python_and_say_what_wfpy_works_out(project: Path) -> None:
    (project / "compile/sources/net.py").write_text(NET, encoding="utf-8")
    (project / "compile/net_flow.py").write_text(NET_FLOW, encoding="utf-8")
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    exported = _plans(project, "compile/net_flow.py")

    net = _node(exported, "net")
    # As the decorator writes them: `NETWORK`, not its value; "instance" quoted.
    assert _annotation(net, "streamblocks") == {
        "name": "streamblocks",
        "arguments": [{"name": "facade", "value": '"instance"'}, {"name": "network", "value": "NETWORK"}],
    }
    # The opener a streamblocks class gets, which the class does not write.
    assert _annotation(net, "viewer")["derived"] is True

    doc = _node(exported, "doc")
    assert _annotation(doc, "viewer")["derived"] is True
    assert _annotation(doc, "source") == {
        "name": "source",
        "arguments": [{"name": "port", "value": '"Out"', "derived": True}],
    }

    show = _annotation(_node(exported, "show"), "viewer")
    assert "derived" not in show
    assert show["arguments"] == [
        {"name": "action", "value": '"open"', "derived": True},
        {"name": "inputs", "value": '["In"]'},
    ]


def test_a_parameter_carries_the_text_the_call_wrote(project: Path) -> None:
    (project / "compile/sources/net.py").write_text(NET, encoding="utf-8")
    (project / "compile/net_flow.py").write_text(NET_FLOW, encoding="utf-8")
    exported = _plans(project, "compile/net_flow.py")
    (path,) = _node(exported, "doc")["meta"]["parameters"]
    assert path["expression"] == "File(path=README)"
    assert path["value"].startswith("File(path='README.md'")

    front = _node(_plans(project, "compile/flow.py"), "front")
    assert front["meta"]["parameters"] == [
        {"name": "text", "kind": "value", "type": "str", "value": "'hello'", "expression": "LABEL"},
    ]


def test_a_node_of_a_factory_made_workflow_carries_the_factory_call(project: Path) -> None:
    duo = _node(_plans(project, "compile/flow.py"), "duo")
    assert duo["meta"]["parameters"] == [
        # What the factory received, and the text of the call.
        {"name": "label", "kind": "value", "type": "str", "value": "'hello'", "expression": "LABEL"},
        # `times` is not read by the workflow, so wfpy has no value for it.
        {"name": "times", "kind": "value", "type": "int", "expression": "3"},
    ]
    assert duo["meta"]["definitionParams"] == [
        {"name": "label", "kind": "value", "type": "str"},
        {"name": "times", "kind": "value", "type": "int", "defaultValue": "2"},
    ]


def test_a_nested_workflows_parameters_tell_a_value_from_a_default(project: Path) -> None:
    pair = project / "compile/workflows/pair.py"
    pair.write_text(
        _text(project, "compile/workflows/pair.py").replace("Text(text=label)", "Text(text=label * times)"),
        encoding="utf-8",
    )
    exported = _plans(project, "compile/flow.py", hierarchy=True)
    (child,) = exported["hierarchy"]["children"]
    assert child["path"] == ["duo"]
    assert child["graph"]["graph"]["meta"] == {
        "factoryName": "pair",
        "parameters": [
            {"name": "label", "value": "hello", "type": "str", "required": True},
            {"name": "times", "value": 3, "type": "int", "defaultValue": "2"},
        ],
    }
    first = _node(child["graph"], "first")
    assert first["meta"]["parameters"][0]["expression"] == "label * times"
    positional = _plans(project, "compile/flow.py")
    assert _node(positional, "duo")["meta"]["parameters"][1]["value"] == "3"


def test_a_factory_called_with_positional_arguments(project: Path) -> None:
    flow = project / "compile/flow.py"
    flow.write_text(
        _text(project, "compile/flow.py").replace("pair(label=LABEL, times=3)()", "pair(LABEL, 3)()"),
        encoding="utf-8",
    )
    duo = _node(_plans(project, "compile/flow.py"), "duo")
    assert [p.get("expression") for p in duo["meta"]["parameters"]] == ["LABEL", "3"]


# ── A save across files ───────────────────────────────────────────────


def test_a_save_across_files_is_all_or_none(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A companion changed under the edit: no file is written."""
    from wfpy import rewrite

    uses = _send(project / "compile/sources/text.py", "portUses", entity="Text")["diagnostic"]["usedIn"]
    before = {name: _text(project, name) for name in ("compile/sources/text.py", "compile/flow.py", "compile/workflows/pair.py")}
    original = rewrite.load_module

    def load_then_touch(path: str) -> tuple:
        loaded = original(path)
        if path.endswith("pair.py"):
            Path(path).write_text(Path(path).read_text(encoding="utf-8") + "# touched\n", encoding="utf-8")
            before["compile/workflows/pair.py"] += "# touched\n"
        return loaded

    monkeypatch.setattr(rewrite, "load_module", load_then_touch)
    answer = _send(project / "compile/sources/text.py", "renamePort",
                   entity="Text", portDirection="output", portName="Out", newValue="Value", usedIn=uses)
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "concurrent_source_modification"
    assert {name: _text(project, name) for name in before} == before


def test_a_save_that_fails_midway_puts_back_what_it_wrote(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uses = _send(project / "compile/sources/text.py", "portUses", entity="Text")["diagnostic"]["usedIn"]
    names = ("compile/sources/text.py", "compile/flow.py", "compile/workflows/pair.py")
    before = {name: _text(project, name) for name in names}
    original = Path.replace
    calls: list[str] = []

    def failing(self: Path, target: str | Path) -> Path:
        calls.append(str(target))
        if str(target).endswith("pair.py") and len(calls) <= 3:
            raise OSError("disk full")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", failing)
    answer = _send(project / "compile/sources/text.py", "renamePort",
                   entity="Text", portDirection="output", portName="Out", newValue="Value", usedIn=uses)
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "write_failed"
    assert answer["diagnostic"]["changedFiles"] == []
    assert {name: _text(project, name) for name in names} == before
    assert not [p for p in project.rglob("tmp*") if p.is_file()]


def test_a_factory_called_past_its_star_args(project: Path) -> None:
    pair = project / "compile/workflows/pair.py"
    pair.write_text(
        _text(project, "compile/workflows/pair.py").replace(
            "def pair(label: str, times: int = 2):", "def pair(label: str, *rest: int, times: int = 2):"
        ),
        encoding="utf-8",
    )
    flow = project / "compile/flow.py"
    flow.write_text(
        _text(project, "compile/flow.py").replace("pair(label=LABEL, times=3)()", "pair(LABEL, 7, 8)()"),
        encoding="utf-8",
    )
    duo = _node(_plans(project, "compile/flow.py"), "duo")
    expressions = {p["name"]: p.get("expression") for p in duo["meta"]["parameters"]}
    assert expressions == {"label": "LABEL", "times": None}


# ── What a workflow's node can be made of ─────────────────────────────


def _module(tmp_path: Path, text: str, name: str = "m.py") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


SCOPES = '''from typing import TYPE_CHECKING

from wfpy import workflow

try:
    from stages import Fast
except ImportError:
    Fast = None

if TYPE_CHECKING:
    from stages import Typed


def make(Stage):
    Local = Stage

    @workflow
    def built():
        pass

    return built


if __name__ == "__main__":
    from stages import Script
'''


@pytest.mark.parametrize(
    "type_name",
    ["Stage", "Local", "Fast"],
)
def test_create_node_of_a_type_the_workflows_scopes_bind(tmp_path: Path, type_name: str) -> None:
    src = _module(tmp_path, SCOPES)
    answer = _send(src, "createNode", workflow="built", type=type_name, name="node")
    assert answer["status"] == "ok", answer
    after = src.read_text(encoding="utf-8")
    assert f"        node = {type_name}()\n" in after
    assert after.count("import") == SCOPES.count("import")


@pytest.mark.parametrize("type_name", ["Script", "Typed"])
def test_a_name_bound_only_when_run_as_a_script_or_type_checked_is_not_bound(tmp_path: Path, type_name: str) -> None:
    src = _module(tmp_path, SCOPES)
    answer = _send(src, "createNode", workflow="built", type=type_name, name="node")
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "type_not_found"


STAR = '''from typing import Any

from wfpy import *


@workflow
def flow():
    pass
'''


def test_create_node_of_a_name_a_star_import_from_wfpy_binds(tmp_path: Path) -> None:
    src = _module(tmp_path, STAR)
    # The project has a class of the name too: the star import is what binds it here.
    _module(tmp_path, "from wfpy import task\n\n\n@task\nclass File:\n    pass\n", "mine.py")
    answer = _send(src, "createNode", workflow="flow", type="File", name="f")
    assert answer["status"] == "ok", answer
    after = src.read_text(encoding="utf-8")
    assert "    f = File()\n" in after
    assert "import File" not in after


def test_create_node_of_a_name_a_star_import_from_wfpy_does_not_bind(tmp_path: Path) -> None:
    src = _module(tmp_path, STAR)
    answer = _send(src, "createNode", workflow="flow", type="Nowhere", name="n")
    assert answer["diagnostic"]["code"] == "type_not_found"


def test_create_node_reads_the_project_once(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wfpy import _project_index

    built: list[Path] = []
    original = _project_index.ProjectIndex.__init__

    def counting(self: _project_index.ProjectIndex, root: Path) -> None:
        built.append(root)
        original(self, root)

    monkeypatch.setattr(_project_index.ProjectIndex, "__init__", counting)
    flow = project / "compile/flow.py"
    flow.write_text(_text(project, "compile/flow.py").replace("from wfpy import", "from os.path import *\nfrom wfpy import"), encoding="utf-8")
    answer = _send(flow, "createNode", workflow="flow", type="Join", name="joined")
    assert answer["status"] == "ok", answer
    assert len(built) == 1


# ── Port edits that find what they edit ───────────────────────────────


TWO_A = '''from typing import Any

from wfpy import Port, connect, task, workflow


@task
class A:
    class Ports:
        In = Port[Any](direction="in")
        Out = Port[Any](direction="inout")
        Typed: Port[Any] = Port[Any](direction="out")


@workflow
def flow():
    a = A()
    b = A()
    connect(a.Out, b.In)
    connect(a.Typed, b.In)
'''


@pytest.mark.parametrize("direction", ["output", "inout", ""])
def test_rename_port_finds_the_declaration_by_its_name(tmp_path: Path, direction: str) -> None:
    """An `inout` port renamed as the IDE's "output", or with no direction:
    the declaration is renamed with its uses, never the uses alone."""
    src = _module(tmp_path, TWO_A)
    args = {"entity": "A", "portName": "Out", "newValue": "Res"}
    if direction:
        args["portDirection"] = direction
    answer = _send(src, "renamePort", **args)
    assert answer["status"] == "ok", answer
    after = src.read_text(encoding="utf-8")
    assert 'Res = Port[Any](direction="inout")' in after
    assert "connect(a.Res, b.In)" in after
    assert "Out" not in after


def test_rename_port_written_with_an_annotation(tmp_path: Path) -> None:
    src = _module(tmp_path, TWO_A)
    answer = _send(src, "renamePort", entity="A", portDirection="output", portName="Typed", newValue="Shaped")
    assert answer["status"] == "ok", answer
    after = src.read_text(encoding="utf-8")
    assert 'Shaped: Port[Any] = Port[Any](direction="out")' in after
    assert "connect(a.Shaped, b.In)" in after


def test_update_port_type_written_with_an_annotation(tmp_path: Path) -> None:
    src = _module(tmp_path, TWO_A)
    answer = _send(src, "updatePortType", entity="A", portDirection="output", portName="Typed", newValue="int")
    assert answer["status"] == "ok", answer
    assert 'Typed: Port[int] = Port[int](direction="out")' in src.read_text(encoding="utf-8")


def test_rename_port_named_like_a_direction(tmp_path: Path) -> None:
    """`direction="out"` is not a use of a port named `out`."""
    src = _module(tmp_path, TWO_A.replace("Out", "out"))
    answer = _send(src, "renamePort", entity="A", portDirection="output", portName="out", newValue="res")
    assert answer["status"] == "ok", answer
    after = src.read_text(encoding="utf-8")
    assert 'res = Port[Any](direction="inout")' in after and "connect(a.res, b.In)" in after


def test_rename_port_still_refuses_a_name_read_by_getattr(tmp_path: Path) -> None:
    src = _module(
        tmp_path,
        TWO_A.replace(
            "        Typed: Port[Any] = Port[Any](direction=\"out\")\n",
            "        Typed: Port[Any] = Port[Any](direction=\"out\")\n\n"
            "    def peek(self):\n"
            "        return getattr(self, \"Out\")\n",
        ),
    )
    before = src.read_text(encoding="utf-8")
    answer = _send(src, "renamePort", entity="A", portDirection="output", portName="Out", newValue="Res")
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "port_used_by_name"
    assert src.read_text(encoding="utf-8") == before


NESTED = '''from typing import Any

from wfpy import File, Port, connect, task, workflow


@task
class A:
    class Ports:
        In = Port[Any](direction="in")
        Out = Port[Any](direction="out")


@workflow(
    inputs={"x": Any},
    outputs={"y": Any},
)
def inner():
    """Reads x, writes y."""
    a = A()
    connect("x", a.In)
    connect(a.Out, "y")


def make(label):
    @workflow(outputs={"path": Any})
    def built():
        a = A()
        default = File(path="path")
        connect(a.Out, "path")

    return built


@workflow
def outer():
    s = inner()
    m = make("one")()
    b = A()
    connect(s.y, b.In)
    connect(m.path, b.In)
'''


def test_rename_a_workflow_port_follows_this_files_workflows(tmp_path: Path) -> None:
    src = _module(tmp_path, NESTED)
    answer = _send(src, "renameNode", workflow="inner", old="y", new="z")
    assert answer["status"] == "ok", answer
    after = src.read_text(encoding="utf-8")
    assert 'outputs={"z": Any},' in after and 'connect(a.Out, "z")' in after
    assert "connect(s.z, b.In)" in after


def test_rename_a_factory_workflow_port_past_a_string_that_is_not_it(tmp_path: Path) -> None:
    """`File(path="path")` is a keyword's value, not the port `path`; the
    instance of the factory's workflow in this file follows."""
    src = _module(tmp_path, NESTED)
    answer = _send(src, "renameNode", workflow="built", old="path", new="where")
    assert answer["status"] == "ok", answer
    after = src.read_text(encoding="utf-8")
    assert '@workflow(outputs={"where": Any})' in after
    assert 'default = File(path="path")' in after
    assert "connect(m.where, b.In)" in after


def test_delete_a_workflow_port_this_file_connects_is_refused(tmp_path: Path) -> None:
    src = _module(tmp_path, NESTED)
    before = src.read_text(encoding="utf-8")
    answer = _send(src, "deletePort", workflow="inner", direction="output", portName="y")
    assert answer["status"] == "error"
    assert answer["message"] == "port 'y' of 's' is connected in workflow 'outer': delete its connections first"
    assert src.read_text(encoding="utf-8") == before


TWO_FACTORIES = '''from typing import Any

from wfpy import Port, connect, task, workflow


@task
class A:
    class Ports:
        In = Port[Any](direction="in")
        Out = Port[Any](direction="out")


def first():
    @workflow(outputs={"Y": Any})
    def flow():
        a = A()
        connect(a.Out, "Y")

    return flow


def second():
    @workflow(outputs={"Y": Any})
    def flow():
        a = A()
        connect(a.Out, "Y")

    return flow
'''


def test_rename_port_follows_each_workflow_of_a_shared_name(tmp_path: Path) -> None:
    src = _module(tmp_path, TWO_FACTORIES)
    answer = _send(src, "renamePort", entity="A", portDirection="output", portName="Out", newValue="Res")
    assert answer["status"] == "ok", answer
    after = src.read_text(encoding="utf-8")
    assert after.count('connect(a.Res, "Y")') == 2 and "a.Out" not in after


def test_rename_port_follows_usedIn_naming_a_shared_name(tmp_path: Path) -> None:
    """The IDE names a workflow by its name only: every `@workflow def` of
    the name in that file follows, each once."""
    lib = _module(tmp_path, TWO_FACTORIES.split("\n\n\ndef first")[0] + "\n", "lib.py")
    flows = _module(
        tmp_path,
        "from typing import Any\n\nfrom wfpy import connect, workflow\n\nfrom lib import A\n\n\ndef first"
        + TWO_FACTORIES.split("\n\n\ndef first", 1)[1],
        "flows.py",
    )
    uses = [{"file": str(flows), "workflow": "flow", "instances": ["a"]}]
    answer = _send(lib, "renamePort", entity="A", portDirection="output", portName="Out", newValue="Res", usedIn=uses)
    assert answer["status"] == "ok", answer
    after = flows.read_text(encoding="utf-8")
    assert after.count('connect(a.Res, "Y")') == 2 and "a.Out" not in after
    assert 'Res = Port[Any](direction="out")' in lib.read_text(encoding="utf-8")


# ── An edit that answers ok leaves every workflow building ────────────


STAGES = '''from wfpy import Port, action, connect, task, workflow

NAME = "n.txt"


@task
class Reader:
    class Ports:
        Out = Port[str](direction="out")

    @action(consumes={}, produces={"Out": 1})
    def emit(self) -> dict:
        return {"Out": "x"}


@task
class Source:
    path: str

    class Ports:
        Out = Port[str](direction="out")

    @action(consumes={}, produces={"Out": 1})
    def emit(self) -> dict:
        return {"Out": self.path}


@task
class Fan:
    class Ports:
        Out = Port[str](direction="out")
        Extra = Port[str](direction="out")

    @action(consumes={}, produces={"Out": 1})
    def emit(self) -> dict:
        return {"Out": "x"}


@task
class Writer:
    class Ports:
        In = Port[str](direction="in")

    @action(consumes={"In": 1}, produces={})
    def take(self, value) -> None:
        pass


@workflow(outputs={"Out": str})
def filter():
    r = Reader()
    connect(r.Out, "Out")
'''


def _flow(tmp_path: Path, text: str) -> Path:
    """`stages.py` and the `flow.py` of `text`, which builds as written."""
    _module(tmp_path, STAGES, "stages.py")
    src = _module(tmp_path, text, "flow.py")
    _plans(tmp_path, "flow.py")
    return src


EMPTY = '''from wfpy import workflow


@workflow
def flow():
    pass
'''


def test_create_node_of_a_project_workflow_named_like_a_builtin(tmp_path: Path) -> None:
    """`filter` is a builtin, but the project's `@workflow def filter` is
    the type meant: it is imported, then bound."""
    src = _flow(tmp_path, EMPTY)
    answer = _send(src, "createNode", workflow="flow", type="filter", name="f")
    assert answer["status"] == "ok", answer
    after = src.read_text(encoding="utf-8")
    assert "from stages import filter\n" in after and "    f = filter()\n" in after
    again = _send(src, "createNode", workflow="flow", type="filter", name="g")
    assert again["status"] == "ok", again
    assert src.read_text(encoding="utf-8").count("import") == after.count("import")
    _plans(tmp_path, "flow.py")


TYPE_CHECKED = '''from typing import TYPE_CHECKING

from wfpy import workflow

if TYPE_CHECKING:
    from stages import NAME, Reader
else:
    from stages import Source


@workflow
def flow():
    s = Source(path="x")
'''


def test_a_type_imported_for_the_type_checker_only_is_imported_to_run(tmp_path: Path) -> None:
    src = _flow(tmp_path, TYPE_CHECKED)
    answer = _send(src, "createNode", workflow="flow", type="Reader", name="r")
    assert answer["status"] == "ok", answer
    after = src.read_text(encoding="utf-8")
    assert "\nfrom stages import Reader\n" in after and "    r = Reader()\n" in after
    _plans(tmp_path, "flow.py")


def test_a_name_imported_for_the_type_checker_only_is_not_bound(tmp_path: Path) -> None:
    src = _flow(tmp_path, TYPE_CHECKED)
    answer = _send(src, "updateNodeParameter", workflow="flow", entity="s", parameterName="path", newValue="NAME")
    assert answer["diagnostic"] == {"code": "name_not_bound", "name": "NAME"}, answer
    assert src.read_text(encoding="utf-8") == TYPE_CHECKED


LOCALS = '''from wfpy import connect, workflow

from stages import NAME, Source, Writer


@workflow
def flow():
    path = "a.txt"
    s = Source(path="x")
    w = Writer()
    connect(s.Out, w.In)
    later = "c.txt"
    NAME = "d.txt"
'''


@pytest.mark.parametrize(("value", "line"), [("path", None), ("later", 12), ("NAME", 13), ("s.path", 9)])
def test_update_node_parameter_reads_a_local_bound_above_the_node(tmp_path: Path, value: str, line: int | None) -> None:
    """A name the workflow binds is its local all through its body: it has
    to be bound above the node, even one the module binds too (`NAME`), and
    the node's own name is bound only once the call has run."""
    src = _flow(tmp_path, LOCALS)
    answer = _send(src, "updateNodeParameter", workflow="flow", entity="s", parameterName="path", newValue=value)
    if line is None:
        assert answer["status"] == "ok", answer
        assert '    s = Source(path=path)\n' in src.read_text(encoding="utf-8")
        _plans(tmp_path, "flow.py")
    else:
        assert answer["diagnostic"] == {"code": "name_not_bound", "name": value.split(".")[0], "line": line}, answer
        assert src.read_text(encoding="utf-8") == LOCALS


MODULE_NAMES = '''import os

from wfpy import workflow

from stages import Source


@workflow
def flow(path: str = "a.txt"):
    s = Source(path=path)
'''


@pytest.mark.parametrize(
    "value",
    ["os.path.join(os.path.dirname(__file__), 'data.txt')", "__name__", "path + '.bak'"],
)
def test_a_node_reads_the_modules_dunders_and_the_workflows_parameters(tmp_path: Path, value: str) -> None:
    src = _flow(tmp_path, MODULE_NAMES)
    answer = _send(src, "updateNodeParameter", workflow="flow", entity="s", parameterName="path", newValue=value)
    assert answer["status"] == "ok", answer
    created = _send(src, "createNode", workflow="flow", type="Source", name="t", expressions={"path": value})
    assert created["status"] == "ok", created
    _plans(tmp_path, "flow.py")


STAR_READS = '''from wfpy import connect, workflow

from stages import *


@workflow
def flow():
    a = Reader()
    b = Writer()
    connect(a.Out, b.In)
'''

BUILTIN_READS = '''from wfpy import connect, workflow

from stages import Reader, Writer


@workflow
def flow():
    a = Reader()
    for i in range(2):
        b = Writer()
        connect(a.Out, b.In)
'''

FACTORY_READS = '''from wfpy import connect, workflow

from stages import Reader, Writer


def make():
    Drain = Writer

    @workflow
    def built():
        a = Reader()
        b = Drain()
        connect(a.Out, b.In)

    return built


@workflow
def flow():
    m = make()()
'''


@pytest.mark.parametrize(
    ("text", "workflow", "name"),
    [(STAR_READS, "flow", "Writer"), (BUILTIN_READS, "flow", "range"), (FACTORY_READS, "built", "Drain")],
)
def test_a_node_is_not_named_after_what_the_workflow_reads(tmp_path: Path, text: str, workflow: str, name: str) -> None:
    """A star import's name, a builtin, a factory's local: each read by the
    body, each hidden by a node of its name."""
    src = _flow(tmp_path, text)
    renamed = _send(src, "renameNode", workflow=workflow, old="a", new=name)
    assert renamed["diagnostic"].get("code") == "name_shadows", renamed
    created = _send(src, "createNode", workflow=workflow, type="Reader", name=name)
    assert created["diagnostic"].get("code") == "name_shadows", created
    assert src.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("renameNode", {"workflow": "flow", "old": "spare", "new": "class"}),
        ("createNode", {"workflow": "flow", "type": "Text", "name": "None"}),
        ("createPort", {"workflow": "flow", "direction": "output", "portName": "for", "portType": "str"}),
        ("renameNode", {"workflow": "flow", "old": "Log", "new": "True"}),
    ],
)
def test_a_keyword_is_not_a_name(project: Path, op: str, args: dict) -> None:
    before = _text(project, "compile/flow.py")
    answer = _send(project / "compile/flow.py", op, **args)
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "invalid_identifier", answer
    assert _text(project, "compile/flow.py") == before


def test_rename_a_class_port_is_not_a_keyword(project: Path) -> None:
    before = _text(project, "compile/sources/text.py")
    answer = _send(project / "compile/sources/text.py", "renamePort", entity="Text", portDirection="output", portName="Out", newValue="def")
    assert answer["diagnostic"]["code"] == "invalid_identifier", answer
    assert _text(project, "compile/sources/text.py") == before


PORT_BY_VARIABLE = '''from wfpy import connect, workflow

from stages import Reader


@workflow(outputs={"Out": str, "Log": str})
def flow():
    a = Reader()
    b = Reader()
'''


@pytest.mark.parametrize(
    "lines",
    [
        '    for i, dst in enumerate(("Out", "Log")):\n        connect((a, b)[i].Out, dst)\n',
        '    outs = ("Out", "Log")\n    for dst in outs:\n        connect(a.Out, dst)\n',
        '    dst, log = "Out", "Log"\n    connect(a.Out, dst)\n    connect(b.Out, log)\n',
    ],
)
@pytest.mark.parametrize("op", ["renameNode", "deletePort"])
def test_a_workflow_port_named_through_a_variable_is_not_edited(tmp_path: Path, lines: str, op: str) -> None:
    text = PORT_BY_VARIABLE + lines
    src = _flow(tmp_path, text)
    if op == "renameNode":
        answer = _send(src, op, workflow="flow", old="Out", new="Res")
    else:
        answer = _send(src, op, workflow="flow", direction="output", portName="Out")
    assert answer["diagnostic"].get("code") == "port_used_by_name", answer
    assert src.read_text(encoding="utf-8") == text


PORT_OF_AN_INSTANCE = '''from wfpy import Port, action, connect, task, workflow


@task
class Reader:
    class Ports:
        result = Port[str](direction="out")

    @action(consumes={}, produces={"result": 1})
    def emit(self) -> dict:
        return {"result": "x"}


@workflow(outputs={"result": str})
def flow():
    a = Reader()
    connect(getattr(a, "result"), "result")
'''


def test_rename_a_workflow_port_past_the_port_of_an_instance_of_its_name(tmp_path: Path) -> None:
    """`getattr(a, "result")` names the port of `a`, not the workflow's."""
    src = _module(tmp_path, PORT_OF_AN_INSTANCE, "flow.py")
    answer = _send(src, "renameNode", workflow="flow", old="result", new="final")
    assert answer["status"] == "ok", answer
    after = src.read_text(encoding="utf-8")
    assert '@workflow(outputs={"final": str})' in after
    assert 'connect(getattr(a, "result"), "final")' in after
    _plans(tmp_path, "flow.py")


def test_rename_a_workflow_port_to_its_own_name_changes_nothing(tmp_path: Path) -> None:
    src = _module(tmp_path, PORT_OF_AN_INSTANCE, "flow.py")
    answer = _send(src, "renameNode", workflow="flow", old="result", new="result")
    assert answer["status"] == "ok", answer
    assert src.read_text(encoding="utf-8") == PORT_OF_AN_INSTANCE


def test_rename_a_class_port_a_variable_of_the_class_names_is_refused(tmp_path: Path) -> None:
    text = STAGES.replace('        return {"Out": "x"}\n', '        key = "Out"\n        return {key: "x"}\n', 1)
    src = _module(tmp_path, text, "stages.py")
    answer = _send(src, "renamePort", entity="Reader", portDirection="output", portName="Out", newValue="Res")
    assert answer["diagnostic"] == {"code": "port_used_by_name", "entityType": "Reader", "port": "Out", "line": 13}, answer
    assert src.read_text(encoding="utf-8") == text


CLASS_PORT_BY_VARIABLE = '''from wfpy import connect, workflow

from stages import Fan, Reader, Writer


@workflow
def flow():
    a = Reader()
    b = Reader()
    f = Fan()
    w = Writer()
'''


@pytest.mark.parametrize(
    ("op", "args", "lines", "line"),
    [
        (
            "renamePort",
            {"entity": "Reader", "portDirection": "output", "portName": "Out", "newValue": "Res"},
            '    for p in ("Out",):\n        connect(getattr(a, p), w.In)\n',
            13,
        ),
        (
            "renamePort",
            {"entity": "Reader", "portDirection": "output", "portName": "Out", "newValue": "Res"},
            "    for i in (0,):\n        connect((a, b)[i].Out, w.In)\n",
            13,
        ),
        (
            "deleteEntityPort",
            {"entityType": "Fan", "portDirection": "output", "portName": "Extra"},
            '    for p in ("Extra",):\n        connect(getattr(f, p), w.In)\n',
            13,
        ),
    ],
)
def test_a_class_port_a_workflow_names_through_a_variable_is_not_edited(
    tmp_path: Path, op: str, args: dict, lines: str, line: int
) -> None:
    text = CLASS_PORT_BY_VARIABLE + lines
    src = _flow(tmp_path, text)
    stages = tmp_path / "stages.py"
    entity = args.get("entity") or args.get("entityType")
    uses = _send(stages, "portUses", entity=entity)["diagnostic"]["usedIn"]
    assert str(src.resolve()) in [use["file"] for use in uses]
    answer = _send(stages, op, usedIn=uses, **args)
    assert answer["diagnostic"].get("code") == "port_used_by_name", answer
    assert answer["diagnostic"]["file"] == str(src.resolve()) and answer["diagnostic"]["line"] == line
    assert "line 13 of flow.py" in answer["message"]
    assert src.read_text(encoding="utf-8") == text
    assert stages.read_text(encoding="utf-8") == STAGES


def test_a_workflow_port_its_user_names_through_a_variable_is_not_renamed(tmp_path: Path) -> None:
    text = NESTED.replace("    connect(s.y, b.In)\n", '    for p in ("y",):\n        connect(getattr(s, p), b.In)\n')
    src = _module(tmp_path, text)
    answer = _send(src, "renameNode", workflow="inner", old="y", new="z")
    assert answer["diagnostic"].get("code") == "port_used_by_name", answer
    assert answer["diagnostic"]["workflow"] == "outer"
    assert src.read_text(encoding="utf-8") == text


LOOPED_PORTS = NESTED.replace(
    "    connect(s.y, b.In)\n", '    for p in ("y",):\n        connect(getattr(s, p), b.In)\n    connect("x", s.x)\n'
).replace("@workflow\ndef outer():", '@workflow(inputs={"x": Any})\ndef outer():')


def test_a_port_a_loop_of_other_names_reaches_is_renamed(tmp_path: Path) -> None:
    """`for p in ("y",): getattr(s, p)` names `y` only: renaming `x`
    follows `s.x`; renaming `y` is refused."""
    src = _module(tmp_path, LOOPED_PORTS)
    _plans(tmp_path, "m.py", workflow="outer")
    answer = _send(src, "renameNode", workflow="inner", old="x", new="w")
    assert answer["status"] == "ok", answer
    after = src.read_text(encoding="utf-8")
    assert 'connect("x", s.w)' in after and "connect(getattr(s, p), b.In)" in after
    _plans(tmp_path, "m.py", workflow="outer")
    refused = _send(src, "renameNode", workflow="inner", old="y", new="z")
    assert refused["diagnostic"].get("code") == "port_used_by_name", refused


# ── a node renamed where Python reads it, an argument edited where it is written ──


READ_BARE = '''from wfpy import connect, workflow

from stages import Reader, Writer

DRY = False


@workflow(outputs={"Out": str})
def flow():
    a = Reader() if not DRY else Reader()
    b = Writer()
    for node in (a,):
        connect(getattr(a, "Out"), b.In)
    later = lambda: a.Out
    count = [a for a in range(2)]
    connect(later(), "Out")
'''


def test_rename_node_follows_every_read_of_the_local(tmp_path: Path) -> None:
    """`getattr(a, ...)`, a tuple a loop goes over, a closure, and an
    assignment that is not one call: each reads the local, each is renamed.
    A comprehension's own `a` is another variable, and stays."""
    src = _flow(tmp_path, READ_BARE)
    answer = _send(src, "renameNode", workflow="flow", old="a", new="first")
    assert answer["status"] == "ok", answer
    text = src.read_text(encoding="utf-8")
    assert "first = Reader() if not DRY else Reader()" in text
    assert "for node in (first,):" in text
    assert 'getattr(first, "Out")' in text
    assert "lambda: first.Out" in text
    assert "[a for a in range(2)]" in text
    _plans(tmp_path, "flow.py")


def test_rename_node_refuses_a_local_declared_nonlocal(tmp_path: Path) -> None:
    src = _flow(tmp_path, '''from wfpy import workflow

from stages import Reader


@workflow
def flow():
    a = Reader()

    def again():
        nonlocal a
        a = Reader()
''')
    before = src.read_text(encoding="utf-8")
    answer = _send(src, "renameNode", workflow="flow", old="a", new="first")
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "name_used_otherwise", answer
    assert src.read_text(encoding="utf-8") == before


def test_rename_node_says_a_node_made_in_a_loop_is_renamed_in_its_source(tmp_path: Path) -> None:
    src = _flow(tmp_path, '''from wfpy import workflow

from stages import Reader


@workflow
def flow():
    made = {}
    for k in ("x",):
        made[k] = Reader()
''')
    answer = _send(src, "renameNode", workflow="flow", old="x", new="y")
    assert answer["status"] == "error"
    assert answer["diagnostic"] == {"code": "node_not_found", "name": "x"}, answer


PORT_AND_NODE = '''from wfpy import connect, workflow

from stages import Reader


@workflow(outputs={"Out": str})
def flow():
    r = Reader()
    connect(r.Out, "Out")
'''


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("createNode", {"type": "Reader", "name": "Out"}),
        ("renameNode", {"old": "r", "new": "Out"}),
        ("createPort", {"direction": "output", "portName": "r", "portType": "str"}),
        ("renameNode", {"old": "Out", "new": "r"}),
    ],
)
def test_a_node_and_a_port_of_one_workflow_are_not_named_alike(tmp_path: Path, op: str, args: dict) -> None:
    """Renamed by its name alone, the node would be renamed for the port."""
    src = _flow(tmp_path, PORT_AND_NODE)
    answer = _send(src, op, workflow="flow", **args)
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "name_exists", answer
    assert src.read_text(encoding="utf-8") == PORT_AND_NODE


MADE_BY_A_HELPER = '''from wfpy import connect, workflow

from stages import Source, Writer


def _source(path):
    return Source(path=path + ".txt")


@workflow
def flow():
    s = _source("a")
    w = Writer()
    connect(s.Out, w.In)
'''


def test_an_argument_of_a_node_a_helper_makes_is_not_written_on_the_helper(tmp_path: Path) -> None:
    """`s = _source("a")`: the `path` the node shows is the one `_source`
    gives `Source`; `_source(..., path=...)` would give `_source` two."""
    src = _flow(tmp_path, MADE_BY_A_HELPER)
    answer = _send(src, "updateNodeParameter", workflow="flow", entity="s", parameterName="path", newValue='"b.txt"')
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "arguments_elsewhere", answer
    assert answer["diagnostic"]["line"] == 12
    assert src.read_text(encoding="utf-8") == MADE_BY_A_HELPER


def test_a_node_a_helper_makes_exports_no_source_text_of_the_helper(tmp_path: Path) -> None:
    _flow(tmp_path, MADE_BY_A_HELPER)
    exported = _plans(tmp_path, "flow.py")
    (path,) = [p for p in _node(exported, "s")["meta"]["parameters"] if p["name"] == "path"]
    assert path["value"] == "'a.txt'"
    assert "expression" not in path


FACTORY_BY_POSITION = '''from wfpy import connect, workflow

from stages import Source, Writer


def make(path, label="x", **rest):
    @workflow(outputs={"Out": str})
    def made():
        s = Source(path=path)
        connect(s.Out, "Out")

    return made


OPTIONS = {"label": "y"}


@workflow
def flow():
    m = make("a.txt")()
    n = make("b.txt", **OPTIONS)()
    w = Writer()
    connect(m.Out, w.In)
'''


def test_an_argument_given_by_position_is_set_there(tmp_path: Path) -> None:
    src = _flow(tmp_path, FACTORY_BY_POSITION)
    answer = _send(src, "updateNodeParameter", workflow="flow", entity="m", parameterName="path", newValue='"c.txt"')
    assert answer["status"] == "ok", answer
    assert 'm = make("c.txt")()' in src.read_text(encoding="utf-8")
    _plans(tmp_path, "flow.py")


def test_an_argument_a_splat_may_give_is_not_added(tmp_path: Path) -> None:
    src = _flow(tmp_path, FACTORY_BY_POSITION)
    answer = _send(src, "updateNodeParameter", workflow="flow", entity="n", parameterName="label", newValue='"z"')
    assert answer["status"] == "error"
    assert answer["diagnostic"]["code"] == "arguments_elsewhere", answer
    assert "**OPTIONS" in answer["message"]
    assert src.read_text(encoding="utf-8") == FACTORY_BY_POSITION


def test_an_argument_of_a_node_no_one_call_makes_is_refused_with_why(tmp_path: Path) -> None:
    src = _flow(tmp_path, READ_BARE)
    answer = _send(src, "updateNodeParameter", workflow="flow", entity="a", parameterName="path", newValue='"x"')
    assert answer["status"] == "error"
    assert answer["diagnostic"] == {"code": "arguments_elsewhere", "entity": "a", "parameter": "path"}, answer
    assert src.read_text(encoding="utf-8") == READ_BARE


# ── an argument written over several lines, written back as it is ──


OVER_LINES = '''from wfpy import connect, workflow

from stages import Source, Writer

BASE = "b"


@workflow
def flow():
    joined = Source(path=" ".join([
        BASE,  # the base
        "x",
    ]))
    quoted = Source(path="""one
    two""")
    nested = make("t")()
    wrapped = (
        Source(path=" ".join([
            BASE,
        ]))
    )
    slashed = Source(path="x" + \\
        BASE)
    grouped = Source(path=("a"
                           "b"))
    FIELD
    margin = Source(path=" ".join([
        BASE,
# "y",
    ]))
    single = Source(path=("a"))
    concatenated = Source(
        path="a "
        "b",
    )
    commented = Source(path=(  # why
        "a "
        "b"
    ))
    w = Writer()
    connect(joined.Out, w.In)


def make(tail):
    @workflow
    def made():
        deep = Source(path=" ".join([
            BASE,
            tail,
        ]))
        w = Writer()
        connect(deep.Out, w.In)

    return made
'''


# An f-string's field over lines parses from Python 3.12 on (PEP 701).
FIELD_OVER_LINES = sys.version_info >= (3, 12)
OVER_LINES = OVER_LINES.replace(
    "    FIELD\n",
    '    field = Source(path=f"{\n        BASE\n    }x")\n' if FIELD_OVER_LINES else "",
)


@pytest.mark.parametrize(
    ("workflow", "entity", "shown"),
    [
        ("flow", "joined", '" ".join([\n        BASE,  # the base\n        "x",\n    ])'),
        ("flow", "quoted", '"""one\n    two"""'),
        ("made", "deep", '" ".join([\n            BASE,\n            tail,\n        ])'),
        ("flow", "wrapped", '" ".join([\n            BASE,\n        ])'),
        ("flow", "slashed", '"x" + \\\n        BASE'),
        # Its own parentheses, without which it is no expression.
        ("flow", "grouped", '("a"\n                           "b")'),
        pytest.param(
            "flow", "field", 'f"{\n        BASE\n    }x"',
            marks=pytest.mark.skipif(not FIELD_OVER_LINES, reason="an f-string field over lines needs 3.12"),
        ),
        ("flow", "margin", '" ".join([\n        BASE,\n# "y",\n    ])'),
        # On one line, as ast reads it: without its parentheses, which stay.
        ("flow", "single", '"a"'),
        # Over lines in the call's parentheses, none of its own: given some.
        ("flow", "concatenated", '("a "\n        "b")'),
        # Its own, a comment after the first: with them, and the comment.
        ("flow", "commented", '(  # why\n        "a "\n        "b"\n    )'),
    ],
)
def test_an_argument_over_lines_is_shown_as_the_file_has_it_and_written_back_as_it_is(
    tmp_path: Path, workflow: str, entity: str, shown: str
) -> None:
    """Its later lines at the file's columns, whatever writes them (brackets,
    a string, a backslash, an f-string's field, a comment at the margin)."""
    src = _flow(tmp_path, OVER_LINES)
    exported = _plans(tmp_path, "flow.py", hierarchy=True)
    graph = exported if workflow == "flow" else exported["hierarchy"]["children"][0]["graph"]
    (path,) = [p for p in _node(graph, entity)["meta"]["parameters"] if p["name"] == "path"]
    assert path["expression"] == shown
    answer = _send(src, "updateNodeParameter", workflow=workflow, entity=entity, parameterName="path", newValue=shown)
    assert answer["status"] == "ok", answer
    assert src.read_text(encoding="utf-8") == OVER_LINES


@pytest.mark.parametrize("workflow", ["flow", "made"])
def test_an_argument_over_lines_lands_at_the_columns_it_is_written_at(tmp_path: Path, workflow: str) -> None:
    """Edited, or given to a new node (a paste), its later lines stand where
    the text puts them, as the panel shows them: not indented by the block
    once more, nor taken back to it."""
    src = _flow(tmp_path, OVER_LINES)
    entity = "joined" if workflow == "flow" else "deep"
    edited = '" ".join([\n        BASE,  # the base\n        "z",\n    ])'
    answer = _send(src, "updateNodeParameter", workflow=workflow, entity=entity, parameterName="path", newValue=edited)
    assert answer["status"] == "ok", answer
    assert f"{entity} = Source(path={edited})" in src.read_text(encoding="utf-8")
    answer = _send(src, "createNode", workflow=workflow, type="Source", name="again", expressions={"path": edited})
    assert answer["status"] == "ok", answer
    assert f"again = Source(path={edited})" in src.read_text(encoding="utf-8")
    _plans(tmp_path, "flow.py", hierarchy=True)


@pytest.mark.parametrize("given", ['f"{BASE = }"', 'f"{BASE =}"', 'f"{BASE}  x"'])
def test_a_field_of_an_f_string_differs_by_its_blanks(tmp_path: Path, given: str) -> None:
    """`{BASE = }` is not `{BASE=}`: the blanks are its value, and the edit is written."""
    src = _flow(tmp_path, OVER_LINES.replace('single = Source(path=("a"))', 'single = Source(path=f"{BASE=}")'))
    answer = _send(src, "updateNodeParameter", workflow="flow", entity="single", parameterName="path", newValue=given)
    assert answer["status"] == "ok", answer
    assert f"single = Source(path={given})" in src.read_text(encoding="utf-8")
