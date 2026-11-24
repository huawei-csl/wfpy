"""A port declared without ``direction="in"`` or ``direction="out"`` is
deprecated. The runtime warns of it at the line that declares it and runs the
workflow as it did. The sidecar says the same on its graph, in the runtime's
words, and holds such a port to the side its name gives, which is the side
the runtime takes it for and the direction the warning says to write.

When wfpy refuses such a port, this is the file to turn round.
"""

from __future__ import annotations

import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest

from wfpy import Port, run, task, workflow
from wfpy.cli import _load_module
from wfpy.sidecar import _handle_request
from wfpy.types import port_side_by_name, port_without_direction_notice

# `Out` of `Up` is written on line 8, with no direction.
FLOW = '''\
from wfpy import Port, action, connect, task, workflow


@task
class Up:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str]()

    @action(consumes={"In": 1}, produces={"Out": 1})
    def up(self, In: str) -> str:
        return In.upper()


@task
class Bang:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def bang(self, In: str) -> str:
        return In + "!"


@workflow(inputs={"Text": str}, outputs={"Result": str})
def flow():
    a = Up()
    b = Bang()
    connect("Text", a.In)
    connect(a.Out, b.In)
    connect(b.Out, "Result")
'''
EDGE = "    connect(a.Out, b.In)\n"

# Each port used against its name: `Data`, an input by its name, sends, and
# `Result`, an output by its name, receives. An action names each, and that
# is all that makes this run.
AGAINST_THE_NAMES = '''\
from wfpy import Port, action, connect, task, workflow


@task
class Up:
    class Ports:
        In = Port[str](direction="in")
        Data = Port[str]()

    @action(consumes={"In": 1}, produces={"Data": 1})
    def up(self, In: str) -> str:
        return In.upper()


@task
class Bang:
    class Ports:
        Result = Port[str]()
        Done = Port[str](direction="out")

    @action(consumes={"Result": 1}, produces={"Done": 1})
    def bang(self, Result: str) -> str:
        return Result + "!"


@workflow(inputs={"Text": str}, outputs={"Final": str})
def flow():
    a = Up()
    b = Bang()
    connect("Text", a.In)
    connect(a.Data, b.Result)
    connect(b.Done, "Final")
'''


def _file(tmp_path: Path, text: str, name: str = "flow.py") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _loaded(path: Path) -> tuple[Any, list[warnings.WarningMessage]]:
    """The module of a workflow file, loaded as `wfpy run` loads it, and what
    it was warned of for its ports."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        module = _load_module(str(path))
    return module, [w for w in caught if "a port declared without direction" in str(w.message)]


def _ask(path: Path, op: str, **args: object) -> Any:
    return _handle_request({"file": str(path), "op": f"wfpy.{op}", "args": args})


def _graph(path: Path) -> dict[str, Any]:
    answer = _ask(path, "exportWorkflowGraph", workflow="flow")
    assert answer.status == "ok", answer
    return (answer.diagnostic or {})["graph"]


def _notices(node: dict[str, Any]) -> list[dict[str, Any]]:
    return list((node.get("metadata") or {}).get("diagnostics") or [])


# --- the runtime ----------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "port_name", "side", "to_write"),
    [
        pytest.param("Out = Port[str]()", "Out", "an output", "out", id="no direction"),
        pytest.param("Out = Port[str]", "Out", "an output", "out", id="not called"),
        pytest.param(
            'Out = Port[str](direction="inout")', "Out", "an output", "out", id="inout"
        ),
        pytest.param("Verdict = Port[str]()", "Verdict", "an input", "in", id="another name"),
        pytest.param(
            'Verdict = Port[str](direction="output")',
            "Verdict",
            "an input",
            "in",
            id="a direction that is neither",
        ),
        pytest.param(
            'Verdict = Port[str]("Report")', "Report", "an output", "out", id="its own name"
        ),
    ],
)
def test_the_runtime_warns_at_the_line_that_declares_it(
    tmp_path: Path, written: str, port_name: str, side: str, to_write: str
) -> None:
    path = _file(
        tmp_path,
        "from wfpy import Port, task\n"
        "\n"
        "\n"
        "@task\n"
        "class Up:\n"
        "    class Ports:\n"
        '        In = Port[str](direction="in")\n'
        f"        {written}\n",
    )
    _module, warned = _loaded(path)
    (one,) = warned
    attr = written.split(" ", 1)[0]
    assert one.category is FutureWarning
    assert (one.filename, one.lineno) == (str(path), 8)
    text = str(one.message)
    assert port_side_by_name(port_name) == to_write
    assert text == port_without_direction_notice(f"Up.Ports.{attr}", to_write)
    assert text.startswith(f"Up.Ports.{attr}: ")
    assert f"wfpy takes this one for {side}, by its name" in text
    assert f'write direction="{to_write}" to keep that' in text
    assert "wfpy 2.0 will refuse it" in text


def test_a_port_that_states_its_direction_is_not_warned_of() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")

        @task
        class Stated:
            class Ports:
                In = Port[str](direction="in")
                Out = Port[str]("Text", direction="out")

    assert set(Stated._wfpy_meta.output_ports) == {"Out"}  # type: ignore[attr-defined]


def test_the_workflow_runs_as_it_did(tmp_path: Path) -> None:
    module, warned = _loaded(_file(tmp_path, FLOW))
    assert len(warned) == 1
    assert run(module.flow, {"Text": "hello"}, out_dir=tmp_path / "out") == {"Result": ["HELLO!"]}


def test_wfpy_run_shows_the_warning_to_the_author(tmp_path: Path) -> None:
    """Python hides a `DeprecationWarning` of a module that is not
    `__main__`, which a workflow file under `wfpy run` is not."""
    path = _file(tmp_path, FLOW)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONWARNINGS"}
    ran = subprocess.run(
        [sys.executable, "-m", "wfpy.cli", "run", str(path), "--input", "Text=hello",
         "--out-dir", str(tmp_path / "out")],
        capture_output=True, text=True, cwd=tmp_path, env=env, check=False,
    )
    assert ran.returncode == 0, ran.stderr
    assert "HELLO!" in ran.stdout
    assert f"{path}:8: FutureWarning: Up.Ports.Out: a port declared without" in ran.stderr
    assert "Out = Port[str]()" in ran.stderr  # the line itself, under the warning


def test_a_class_whose_source_cannot_be_read_is_warned_of_all_the_same() -> None:
    scope: dict[str, Any] = {}
    with pytest.warns(FutureWarning, match=r"Up\.Ports\.Out: a port declared without"):
        exec(  # noqa: S102 - a class with no file behind it
            "from wfpy import Port, task\n"
            "@task\n"
            "class Up:\n"
            "    class Ports:\n"
            "        Out = Port[str]()\n",
            scope,
        )
    assert set(scope["Up"]._wfpy_meta.output_ports) == {"Out"}


def test_an_inherited_port_is_said_of_the_class_and_at_the_line_that_write_it(
    tmp_path: Path,
) -> None:
    """Whichever class is defined first, and once for the declaration."""
    path = _file(
        tmp_path,
        "from wfpy import Port, connect, task, workflow\n"
        "\n"
        "\n"
        "class Base:\n"
        "    class Ports:\n"
        '        In = Port[str](direction="in")\n'
        "        Out = Port[str]()\n"
        "\n"
        "\n"
        "@task\n"
        "class Wider(Base):\n"
        "    class Ports(Base.Ports):\n"
        '        More = Port[str](direction="out")\n'
        "\n"
        "\n"
        "@task\n"
        "class Plain(Base):\n"
        "    pass\n"
        "\n"
        "\n"
        "@workflow\n"
        "def flow():\n"
        "    p = Plain()\n"
        "    w = Wider()\n"
        "    connect(p.Out, w.In)\n",
    )
    _module, (warned,) = _loaded(path)
    said = port_without_direction_notice("Base.Ports.Out", "out")
    assert str(warned.message) == said
    assert (warned.filename, warned.lineno) == (str(path), 7)
    # The graph has the same words and the same place on the nodes of both.
    nodes = {n["id"]: n for n in _graph(path)["nodes"]}
    for name in ("p", "w"):
        (notice,) = _notices(nodes[name])
        assert notice["message"] == said
        assert notice["location"] == {"file": str(path), "line": 7}


def test_a_source_is_warned_of_once(tmp_path: Path) -> None:
    """`@source` reads the ports and hands the class to `@task`, which reads
    them again."""
    path = _file(
        tmp_path,
        "from wfpy import File, Port, source\n"
        "\n"
        "\n"
        "@source()\n"
        "class Feed:\n"
        "    path: File\n"
        "\n"
        "    class Ports:\n"
        "        Out = Port[File]()\n",
    )
    _module, (warned,) = _loaded(path)
    assert (warned.filename, warned.lineno) == (str(path), 9)


def test_two_ports_of_one_line_are_each_at_that_line(tmp_path: Path) -> None:
    path = _file(
        tmp_path,
        "from wfpy import Port, task\n"
        "\n"
        "\n"
        "@task\n"
        "class Up:\n"
        "    class Ports:\n"
        "        A = Port[str](); B = Port[str]()\n",
    )
    _module, warned = _loaded(path)
    assert [(w.lineno, str(w.message).split(":")[0]) for w in warned] == [
        (7, "Up.Ports.A"),
        (7, "Up.Ports.B"),
    ]


def test_ports_given_as_another_class_are_said_of_that_class(tmp_path: Path) -> None:
    path = _file(
        tmp_path,
        "from wfpy import Port, task\n"
        "\n"
        "\n"
        "class Shared:\n"
        "    A = Port[str]()\n"
        "\n"
        "\n"
        "@task\n"
        "class Uses:\n"
        "    Ports = Shared\n",
    )
    _module, (warned,) = _loaded(path)
    assert str(warned.message) == port_without_direction_notice("Shared.A", "in")
    assert (warned.filename, warned.lineno) == (str(path), 5)


def test_ports_that_are_no_class_are_read_as_they_were() -> None:
    """`Ports` need not be a class: a namespace of ports is read too, and one
    that states its directions is not warned of."""
    import types

    with warnings.catch_warnings():
        warnings.simplefilter("error")

        @task
        class Stated:
            Ports = types.SimpleNamespace(
                In=Port[int](direction="in"), Out=Port[int](direction="out")
            )

    meta = Stated._wfpy_meta  # type: ignore[attr-defined]
    assert (set(meta.input_ports), set(meta.output_ports)) == ({"In"}, {"Out"})

    with pytest.warns(FutureWarning, match=r"^Open\.Ports\.Out: a port declared without"):

        @task
        class Open:
            Ports = types.SimpleNamespace(Out=Port[int]())


def test_a_workflow_class_with_its_outputs_stated_is_not_told_to_go_by_the_name() -> None:
    """With `inputs=` or `outputs=` the name of a port is not read: `Data` is
    an output here, and a port that is in neither has no side to keep."""
    with pytest.warns(FutureWarning) as warned:

        @workflow(outputs={"Data": str})
        class Flow:
            class Ports:
                Data = Port[str]()
                Spare = Port[str]()

            connections: list[Any] = []

    said = {str(w.message).split(":")[0]: str(w.message) for w in warned}
    assert said["Flow.Ports.Data"] == port_without_direction_notice(
        "Flow.Ports.Data", "out", "by the workflow's inputs= and outputs="
    )
    assert 'write direction="out" to keep that' in said["Flow.Ports.Data"]
    assert said["Flow.Ports.Spare"] == port_without_direction_notice("Flow.Ports.Spare", None)
    assert "to keep that" not in said["Flow.Ports.Spare"]


# --- the sidecar ----------------------------------------------------------


def test_the_graph_says_it_on_the_node_in_the_runtime_s_words(tmp_path: Path) -> None:
    path = _file(tmp_path, FLOW)
    _module, (warned,) = _loaded(path)
    graph = _graph(path)
    nodes = {n["id"]: n for n in graph["nodes"]}

    (notice,) = _notices(nodes["a"])
    assert notice == {
        "severity": "warning",
        "code": "port_without_direction",
        "message": str(warned.message),
        "location": {"file": str(path), "line": warned.lineno},
    }
    # A warning is no fault: nothing is errored and the graph is whole.
    assert nodes["a"]["metadata"] == {"diagnostics": [notice]}
    assert all("metadata" not in n for name, n in nodes.items() if name != "a")
    assert "partial" not in graph
    assert "errors" not in graph
    assert all("meta" not in edge and "metadata" not in edge for edge in graph["edges"])
    # The port is still drawn on the side its name gives it.
    out = next(p for p in nodes["a"]["ports"] if p["name"] == "Out")
    assert out["direction"] == "out"


def test_every_node_of_the_class_has_it(tmp_path: Path) -> None:
    text = FLOW.replace("    b = Bang()\n", "    b = Bang()\n    c = Up()\n")
    nodes = {n["id"]: n for n in _graph(_file(tmp_path, text))["nodes"]}
    assert [len(_notices(nodes[name])) for name in ("a", "b", "c")] == [1, 0, 1]


def test_a_class_of_another_file_is_said_at_its_place_there(tmp_path: Path) -> None:
    head, _, body = FLOW.partition("@workflow")
    tasks = _file(tmp_path, head, "tasks.py")
    top = _file(
        tmp_path,
        "from wfpy import connect, workflow\nfrom tasks import Bang, Up\n\n\n@workflow" + body,
        "top.py",
    )
    nodes = {n["id"]: n for n in _graph(top)["nodes"]}
    (notice,) = _notices(nodes["a"])
    assert notice["message"] == port_without_direction_notice("Up.Ports.Out", "out")
    assert notice["location"] == {"file": str(tasks.resolve()), "line": 8}


def test_a_connection_out_of_a_port_that_is_an_output_by_its_name_is_accepted(
    tmp_path: Path,
) -> None:
    """It was refused as `invalid_source_direction`: a port with no direction
    was taken for an input whatever its name, where the runtime takes `Out`
    for an output."""
    assert EDGE in FLOW
    path = _file(tmp_path, FLOW.replace(EDGE, ""))

    checked = _ask(path, "checkConnection", workflow="flow", source="a.Out", target="b.In")
    assert checked.status == "ok", checked
    assert (checked.diagnostic or {}).get("code") == "connection_ok"

    made = _ask(path, "connect", workflow="flow", from_expr="a.Out", to_expr="b.In")
    assert made.status == "ok", made
    # The line is written, last in the workflow, and what was drawn runs.
    assert path.read_text(encoding="utf-8") == FLOW.replace(EDGE, "") + EDGE
    module, _warned = _loaded(path)
    assert run(module.flow, {"Text": "hello"}, out_dir=tmp_path / "out") == {"Result": ["HELLO!"]}


def _refusal(path: Path, source: str, target: str) -> str | None:
    answer = _ask(path, "checkConnection", workflow="flow", source=source, target=target)
    return None if answer.status == "ok" else (answer.diagnostic or {}).get("code")


def test_such_a_port_is_held_to_the_side_its_name_gives(tmp_path: Path) -> None:
    """The side the runtime takes it for, the side it is drawn on, and the
    direction the warning says to write. The runtime itself checks no
    direction of a connection: one between two outputs is wired and never
    fires, which is what an IDE must not let a user draw."""
    both_open = FLOW.replace('Out = Port[str](direction="out")', "Out = Port[str]()")
    assert both_open != FLOW
    path = _file(tmp_path, both_open.replace(EDGE, ""))
    assert _refusal(path, "a.Out", "b.In") is None
    assert _refusal(path, "a.Out", "b.Out") == "invalid_target_direction"
    assert _refusal(path, "a.In", "b.In") == "invalid_source_direction"

    wired = _file(tmp_path, both_open.replace(EDGE, "    connect(a.Out, b.Out)\n"), "wired.py")
    module, _warned = _loaded(wired)
    assert run(module.flow, {"Text": "hello"}, out_dir=tmp_path / "out") == {"Result": None}
    edge = next(e for e in _graph(wired)["edges"] if e["to"].get("port") == "Out")
    assert edge["meta"]["diagnostics"][0]["code"] == "invalid_target_direction"


def test_a_port_used_against_its_name_is_refused_though_an_action_makes_it_run(
    tmp_path: Path,
) -> None:
    """As before this change. The warning names the side of each, so the
    author is told what to state."""
    edge = "    connect(a.Data, b.Result)\n"
    assert edge in AGAINST_THE_NAMES
    whole = _file(tmp_path, AGAINST_THE_NAMES)
    module, warned = _loaded(whole)
    assert [str(w.message).split(":")[0] for w in warned] == ["Up.Ports.Data", "Bang.Ports.Result"]
    assert "for an input, by its name" in str(warned[0].message)
    assert "for an output, by its name" in str(warned[1].message)
    assert run(module.flow, {"Text": "hello"}, out_dir=tmp_path / "out") == {"Final": ["HELLO!"]}

    cut = _file(tmp_path, AGAINST_THE_NAMES.replace(edge, ""), "cut.py")
    assert _refusal(cut, "a.Data", "b.Result") == "invalid_source_direction"

    # Stated as the actions use them, the connection is accepted.
    stated = AGAINST_THE_NAMES.replace(
        "Data = Port[str]()", 'Data = Port[str](direction="out")'
    ).replace("Result = Port[str]()", 'Result = Port[str](direction="in")')
    stated_cut = _file(tmp_path, stated.replace(edge, ""), "stated.py")
    assert _refusal(stated_cut, "a.Data", "b.Result") is None


def test_a_stated_direction_is_still_held(tmp_path: Path) -> None:
    path = _file(tmp_path, FLOW.replace(EDGE, ""))
    assert _refusal(path, "b.In", "a.In") == "invalid_source_direction"
    assert _refusal(path, "a.Out", "b.Out") == "invalid_target_direction"


def test_a_direction_stated_by_a_name_is_not_taken_for_none(tmp_path: Path) -> None:
    """`direction=OUT` is stated: the runtime does not warn. The sidecar does
    not follow the name, so it says nothing of the port and refuses no
    connection for it."""
    text = (
        'OUT = "out"\n'
        + FLOW.replace("Out = Port[str]()", "Data = Port[str](direction=OUT)")
        .replace('produces={"Out": 1})\n    def up', 'produces={"Data": 1})\n    def up')
        .replace(EDGE, "    connect(a.Data, b.In)\n")
    )
    path = _file(tmp_path, text)
    module, warned = _loaded(path)
    assert warned == []
    assert run(module.flow, {"Text": "hello"}, out_dir=tmp_path / "out") == {"Result": ["HELLO!"]}

    graph = _graph(path)
    assert all(_notices(node) == [] for node in graph["nodes"])
    assert "partial" not in graph
    assert all("meta" not in edge for edge in graph["edges"])
    cut = _file(tmp_path, text.replace("    connect(a.Data, b.In)\n", ""), "cut.py")
    assert _refusal(cut, "a.Data", "b.In") is None


def test_a_port_named_by_its_own_name_is_held_to_that_name(tmp_path: Path) -> None:
    """`Port[T]("Result")` is an output by the name it gives, not by the
    attribute it is bound to."""
    text = FLOW.replace("Out = Port[str]()", 'Sent = Port[str]("Result")').replace(
        EDGE, ""
    )
    path = _file(tmp_path, text)
    assert _refusal(path, "a.Sent", "b.In") is None
    assert _refusal(path, "b.Out", "a.Sent") == "invalid_target_direction"


def test_the_op_that_adds_a_port_writes_a_side(tmp_path: Path) -> None:
    """Asked for `inout`, it does not write a declaration wfpy warns of: it
    writes the side the runtime would take the port for."""
    path = _file(tmp_path, FLOW)
    for name, written in (("Summary", "out"), ("Extra", "in")):
        added = _ask(
            path, "createEntityPort",
            entityType="Bang", portDirection="inout", portName=name, portType="str",
        )
        assert added.status == "ok", added
        assert f'{name} = Port[str](direction="{written}")' in path.read_text(encoding="utf-8")
    _module, warned = _loaded(path)
    assert [str(w.message).split(":")[0] for w in warned] == ["Up.Ports.Out"]
