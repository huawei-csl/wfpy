"""`reorderEntityPorts`: the ports of a definition in the order a diagram
draws them, written into its source: the lines of `class Ports`, or the
entries of a `@workflow` decorator's `inputs=` / `outputs=` dicts. The
order travels with each port's comments; nothing else of the file moves."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from wfpy.sidecar import SUPPORTED_OPS, _handle_request

FLOW = '''from wfpy import Port, action, task, workflow


@task
class Round:
    """Builds one program."""

    class Ports:
        """Its ports."""

        # The image to start from.
        Image = Port[str](direction="in")
        Model = Port[str](direction="in")  # the model

        Network = Port[str](direction="out")
        Program = Port[str](direction="out")

    @action(consumes={"Image": 1, "Model": 1}, produces={"Network": 1, "Program": 1})
    def run(self, Image: str, Model: str) -> dict:
        return {"Network": Image, "Program": Model}


@workflow(
    inputs={
        "Main": str,  # the main file
        "Start": str,
    },
    outputs={"Image": str, "Elf": str},
)
def image():
    pass
'''


def _send(file: Path, op: str, **args: object) -> dict:
    response = _handle_request({"file": str(file), "op": f"wfpy.{op}", "args": args})
    return {"status": response.status, "message": response.message, "diagnostic": response.diagnostic or {}}


def _reordered(tmp_path: Path, entity: str, order: object, source: str = FLOW) -> tuple[dict, str]:
    file = tmp_path / "flow.py"
    file.write_text(source, encoding="utf-8")
    answer = _send(file, "reorderEntityPorts", entity=entity, order=order)
    return answer, file.read_text(encoding="utf-8")


def _class_ports(text: str, cls: str) -> list[str]:
    tree = ast.parse(text)
    (klass,) = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls]
    (ports,) = [n for n in klass.body if isinstance(n, ast.ClassDef) and n.name == "Ports"]
    return [t.id for s in ports.body if isinstance(s, ast.Assign) for t in s.targets if isinstance(t, ast.Name)]


def _workflow_ports(text: str, fn: str) -> dict[str, list[str]]:
    tree = ast.parse(text)
    (func,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn]
    (call,) = [d for d in func.decorator_list if isinstance(d, ast.Call)]
    return {
        k.arg: [key.value for key in k.value.keys]  # type: ignore[union-attr]
        for k in call.keywords
        if k.arg in ("inputs", "outputs")
    }


def test_the_op_is_listed() -> None:
    assert "reorderEntityPorts" in SUPPORTED_OPS


def test_a_class_s_ports_take_the_order_with_their_comments(tmp_path: Path) -> None:
    answer, text = _reordered(tmp_path, "Round", ["Model", "Image", "Program", "Network"])
    assert answer["status"] == "ok", answer
    assert _class_ports(text, "Round") == ["Model", "Image", "Program", "Network"]
    assert text.replace(FLOW, "") != ""  # changed
    ports = text[text.index("class Ports:"):text.index("@action")]
    assert ports == '''class Ports:
        """Its ports."""

        Model = Port[str](direction="in")  # the model
        # The image to start from.
        Image = Port[str](direction="in")

        Program = Port[str](direction="out")
        Network = Port[str](direction="out")

    '''
    # Only the Ports lines changed.
    assert text[: text.index("class Ports:")] == FLOW[: FLOW.index("class Ports:")]
    assert text[text.index("@action"):] == FLOW[FLOW.index("@action"):]


def test_an_order_the_ports_have_changes_nothing(tmp_path: Path) -> None:
    answer, text = _reordered(tmp_path, "Round", ["Image", "Model", "Network", "Program"])
    assert answer["status"] == "ok", answer
    assert text == FLOW


def test_a_workflow_s_dicts_take_the_order_each_its_own(tmp_path: Path) -> None:
    answer, text = _reordered(tmp_path, "image", ["Elf", "Start", "Image", "Main"])
    assert answer["status"] == "ok", answer
    assert _workflow_ports(text, "image") == {"inputs": ["Start", "Main"], "outputs": ["Elf", "Image"]}
    assert '''    inputs={
        "Start": str,  # the main file
        "Main": str,
    },''' not in text
    assert '''    inputs={
        "Start": str,
        "Main": str,  # the main file
    },''' in text
    assert '    outputs={"Elf": str, "Image": str},' in text
    ast.parse(text)


def test_a_workflow_with_an_input_and_an_output_of_one_name(tmp_path: Path) -> None:
    source = FLOW.replace('outputs={"Image": str, "Elf": str}', 'outputs={"Main": str, "Elf": str}')
    answer, text = _reordered(tmp_path, "image", ["Start", "Elf", "Main"], source)
    assert answer["status"] == "ok", answer
    assert _workflow_ports(text, "image") == {"inputs": ["Start", "Main"], "outputs": ["Elf", "Main"]}


@pytest.mark.parametrize(
    ("order", "code"),
    [
        (["Image", "Model", "Network"], "port_order_mismatch"),
        (["Image", "Model", "Network", "Program", "Extra"], "port_order_mismatch"),
        (["Image", "Image", "Network", "Program"], "invalid_port_order"),
        ("Image", "invalid_port_order"),
    ],
)
def test_an_order_that_is_not_the_ports_is_refused(tmp_path: Path, order: object, code: str) -> None:
    answer, text = _reordered(tmp_path, "Round", order)
    assert answer["status"] == "error", answer
    assert answer["diagnostic"]["code"] == code
    assert text == FLOW


def test_ports_sharing_a_line_are_refused(tmp_path: Path) -> None:
    source = FLOW.replace(
        '        Network = Port[str](direction="out")\n        Program = Port[str](direction="out")\n',
        '        Network = Port[str](direction="out"); Program = Port[str](direction="out")\n',
    )
    answer, text = _reordered(tmp_path, "Round", ["Image", "Model", "Program", "Network"], source)
    assert answer["status"] == "error", answer
    assert answer["diagnostic"]["code"] == "ports_share_a_line"
    assert text == source


def test_a_definition_the_file_does_not_have_is_refused(tmp_path: Path) -> None:
    answer, text = _reordered(tmp_path, "Nothing", ["A"])
    assert answer["status"] == "error", answer
    assert answer["diagnostic"]["code"] == "definition_not_found"
    assert text == FLOW
