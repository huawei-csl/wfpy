"""An edit leaves a file that Python reads.

The text an IDE gives as a value is written where one value stands: an
argument, the value of a dict's key, a decorator. A text that is a Python
expression but not one value there (`"a", "b"` is a tuple, and as an
argument it is two) was written as given, and the file no longer parsed, or
the call had an argument nobody asked for. The merge of a decorator's
arguments wrote any name as well, one that wfpy's decorator refuses
included: that file parsed, and failed as it was imported. Each test sends
the request the IDE sends and reads the file the sidecar wrote.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from wfpy.sidecar import _handle_request

FLOW = """import wfpy
from wfpy import Port, action, agent, connect, task, workflow


@task
class Source:
    path: str

    class Ports:
        Out = Port[str](direction="out")

    @action(consumes={}, produces={"Out": 1})
    def emit(self) -> dict:
        return {"Out": self.path}


@task
class Sink:
    class Ports:
        In = Port[str](direction="in")

    @action(consumes={"In": 1}, produces={})
    def take(self, In: str) -> dict:
        return {}


@agent(prompt="x")
class Helper:
    class Ports:
        In = Port[str](direction="in")


def made(label):
    @workflow
    def made():
        inner = Source(path=label)

    return made


ITEMS = ["a", "b"]


@workflow(inputs={"Start": str})
def flow():
    single = Source(path="a.txt")
    sink = Sink()
    placed = made("x")()
"""


def _send(file: Path, op: str, **args: object) -> dict:
    response = _handle_request({"file": str(file), "op": f"wfpy.{op}", "args": args})
    return {
        "status": response.status,
        "message": response.message,
        "diagnostic": response.diagnostic or {},
    }


def _edited(tmp_path: Path, op: str, source: str = FLOW, **args: object) -> tuple[dict, str]:
    """The sidecar's answer to `op` on a file of `source`, and the file after it."""
    file = tmp_path / "flow.py"
    file.write_text(source, encoding="utf-8")
    answer = _send(file, op, **args)
    return answer, file.read_text(encoding="utf-8")


def _added(text: str, source: str = FLOW) -> list[str]:
    """The lines of `text` that `source` does not have."""
    before = source.splitlines()
    return [line for line in text.splitlines() if line not in before]


def _call(text: str, callee: str) -> ast.Call:
    """The one call of `callee` in `text` that `FLOW` does not make."""
    made = {ast.dump(node) for node in ast.walk(ast.parse(FLOW)) if isinstance(node, ast.Call)}
    (call,) = [
        node
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == callee
        and ast.dump(node) not in made
    ]
    return call


# ── the merge ──────────────────────────────────────────────────────────


def _merge(tmp_path: Path, updates: dict[str, str], source: str = FLOW) -> tuple[dict, str]:
    return _edited(
        tmp_path,
        "mergeDefinitionAnnotationArgs",
        source,
        entityType="Helper",
        annotationName="agent",
        argUpdates=updates,
    )


def test_merge_writes_a_tuple_with_its_parentheses(tmp_path: Path) -> None:
    """`mcpServers="a", "b"` is a keyword and a positional after it: no Python."""
    answer, text = _merge(tmp_path, {"mcpServers": '"a", "b"'})
    assert answer["status"] == "ok", answer
    assert _added(text) == ['@agent(prompt="x", mcpServers=("a", "b"))']
    call = _call(text, "agent")
    assert not call.args and [k.arg for k in call.keywords] == ["prompt", "mcpServers"]


def test_merge_keeps_a_tuple_with_a_star_one_argument(tmp_path: Path) -> None:
    """`model="m", *rest` parses, and gives the decorator a positional
    argument that the edit was not asked for."""
    answer, text = _merge(tmp_path, {"model": '"m", *ITEMS'})
    assert answer["status"] == "ok", answer
    assert _added(text) == ['@agent(prompt="x", model=("m", *ITEMS))']
    call = _call(text, "agent")
    assert not call.args and [k.arg for k in call.keywords] == ["prompt", "model"]


def test_merge_writes_a_tuple_of_one_as_a_tuple(tmp_path: Path) -> None:
    """`"a",` kept its comma as the argument's own: the value became the
    string, and the argument written after it stood behind two commas."""
    answer, text = _merge(tmp_path, {"model": '"a",', "provider": '"p"'})
    assert answer["status"] == "ok", answer
    assert _added(text) == ['@agent(prompt="x", model=("a",), provider="p")']
    ast.parse(text)


def test_merge_replaces_a_value_by_a_tuple_in_its_place(tmp_path: Path) -> None:
    source = FLOW.replace('@agent(prompt="x")', '@agent(model="m", prompt="x")')
    answer, text = _merge(tmp_path, {"model": '"a", "b"'}, source)
    assert answer["status"] == "ok", answer
    assert _added(text, source) == ['@agent(model=("a", "b"), prompt="x")']
    ast.parse(text)


def test_merge_refuses_a_value_no_parentheses_make_one(tmp_path: Path) -> None:
    """`model=*ITEMS` is no Python, and `(*ITEMS)` is none either."""
    answer, text = _merge(tmp_path, {"model": "*ITEMS"})
    assert answer["status"] == "error", answer
    assert answer["diagnostic"]["code"] == "invalid_expression", answer
    assert answer["diagnostic"]["expression"] == "*ITEMS"
    assert answer["message"] == "the value of model on @agent is not a Python expression: '*ITEMS'"
    assert text == FLOW


@pytest.mark.parametrize("name", ["class", "None", "a b", "1x", "a-b", ""])
def test_merge_refuses_a_name_no_argument_can_have(tmp_path: Path, name: str) -> None:
    """`class=1` is no Python whatever the decorator takes; a name that is
    not an identifier was the parser's crash (`internal_error`)."""
    answer, text = _merge(tmp_path, {name: "1"})
    assert answer["status"] == "error", answer
    assert answer["diagnostic"] == {
        "code": "invalid_argument_name",
        "annotation": "agent",
        "argument": name,
    }
    assert repr(name) in answer["message"] and "@agent" in answer["message"]
    assert text == FLOW


# ── a node's parameter ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("entity", "parameter", "written"),
    [
        ("single", "path", '    single = Source(path=("a", "b"))'),  # a keyword the call has
        ("single", "mode", '    single = Source(path="a.txt", mode=("a", "b"))'),  # one it gets
        ("placed", "label", '    placed = made(("a", "b"))()'),  # one given by position
    ],
)
def test_a_parameter_is_given_a_tuple_as_one_argument(
    tmp_path: Path, entity: str, parameter: str, written: str
) -> None:
    answer, text = _edited(
        tmp_path,
        "updateNodeParameter",
        workflow="flow",
        entity=entity,
        parameterName=parameter,
        newValue='"a", "b"',
    )
    assert answer["status"] == "ok", answer
    assert _added(text) == [written]
    ast.parse(text)


@pytest.mark.parametrize(
    ("entity", "parameter"), [("single", "path"), ("single", "mode"), ("placed", "label")]
)
def test_a_parameter_is_not_given_a_starred_value(
    tmp_path: Path, entity: str, parameter: str
) -> None:
    """By position it parses, `made(*ITEMS)`, and is as many arguments as
    `ITEMS` has items."""
    answer, text = _edited(
        tmp_path,
        "updateNodeParameter",
        workflow="flow",
        entity=entity,
        parameterName=parameter,
        newValue="*ITEMS",
    )
    assert answer["status"] == "error", answer
    assert answer["diagnostic"]["code"] == "invalid_expression", answer
    assert (
        answer["message"]
        == f"the value of {entity}.{parameter} is not a Python expression: '*ITEMS'"
    )
    assert text == FLOW


# ── every edit that writes a value ─────────────────────────────────────

# The op, its arguments but the value, the argument the value is given as,
# and the line a value of `"a", "b"` is written as.
VALUE_EDITS = [
    pytest.param(
        "mergeDefinitionAnnotationArgs",
        {"entityType": "Helper", "annotationName": "agent"},
        "argUpdates.model",
        '@agent(prompt="x", model=("a", "b"))',
        id="mergeDefinitionAnnotationArgs",
    ),
    pytest.param(
        "updateNodeParameter",
        {"workflow": "flow", "entity": "single", "parameterName": "path"},
        "newValue",
        '    single = Source(path=("a", "b"))',
        id="updateNodeParameter",
    ),
    pytest.param(
        "createNode",
        {"workflow": "flow", "name": "other", "type": "Source"},
        "expressions.path",
        '    other = Source(path=("a", "b"))',
        id="createNode",
    ),
    pytest.param(
        "connect",
        {"workflow": "flow", "to_expr": "sink.In"},
        "from_expr",
        '    connect(("a", "b"), sink.In)',
        id="connect-source",
    ),
    pytest.param(
        "connect",
        {"workflow": "flow", "from_expr": "single.Out"},
        "to_expr",
        '    connect(single.Out, ("a", "b"))',
        id="connect-target",
    ),
    pytest.param(
        "createIf",
        {"workflow": "flow", "name": "choice"},
        "condition_expr",
        '    choice = wfpy.if_(("a", "b"))',
        id="createIf",
    ),
    pytest.param(
        "createLoop",
        {"workflow": "flow", "name": "each"},
        "iterable_expr",
        '    each = wfpy.loop(("a", "b"))',
        id="createLoop",
    ),
    pytest.param(
        "wrapInIf",
        {"workflow": "flow", "node_names": ["single"]},
        "condition_expr",
        '    if_1 = wfpy.if_(("a", "b"))',
        id="wrapInIf",
    ),
    pytest.param(
        "wrapInLoop",
        {"workflow": "flow", "node_names": ["single"]},
        "iterable_expr",
        '    loop_1 = wfpy.loop(("a", "b"))',
        id="wrapInLoop",
    ),
    pytest.param(
        "createPort",
        {"workflow": "flow", "direction": "input", "portName": "Extra"},
        "portType",
        '@workflow(inputs={"Start": str, "Extra": ("a", "b")})',
        id="createPort",
    ),
    pytest.param(
        "updateDefinitionAnnotation",
        {"entityType": "Helper", "annotationName": "agent"},
        "annotationText",
        '@("a", "b")',
        id="updateDefinitionAnnotation",
    ),
]


def _with_value(args: dict, given_as: str, value: str) -> dict:
    """`args` and `value` as the argument `given_as`: `newValue`, or
    `expressions.path` for `{"expressions": {"path": value}}`."""
    outer, _, inner = given_as.partition(".")
    return {**args, outer: {inner: value} if inner else value}


@pytest.mark.parametrize(("op", "args", "given_as", "written"), VALUE_EDITS)
def test_the_file_parses_after_an_edit_with_a_tuple(
    tmp_path: Path, op: str, args: dict, given_as: str, written: str
) -> None:
    """A tuple written without its parentheses is one value, in them."""
    answer, text = _edited(tmp_path, op, **_with_value(args, given_as, '"a", "b"'))
    assert answer["status"] == "ok", answer
    ast.parse(text)
    assert written in _added(text), _added(text)


@pytest.mark.parametrize(("op", "args", "given_as", "written"), VALUE_EDITS)
def test_no_edit_writes_a_starred_value(
    tmp_path: Path, op: str, args: dict, given_as: str, written: str
) -> None:
    """Where it parses (`connect(*ITEMS, sink.In)`, `wfpy.loop(*ITEMS)`) it
    is as many arguments as `ITEMS` has items; elsewhere it is no Python."""
    answer, text = _edited(tmp_path, op, **_with_value(args, given_as, "*ITEMS"))
    assert answer["status"] == "error", answer
    assert answer["diagnostic"]["code"] == "invalid_expression", answer
    assert answer["diagnostic"]["expression"] == "*ITEMS"
    assert answer["message"].endswith("is not a Python expression: '*ITEMS'")
    assert text == FLOW


def test_a_port_type_is_one_value_in_a_dict_and_a_tuple_in_a_subscript(tmp_path: Path) -> None:
    """`Port[str, int]` is Python as written, and stays so; the same text as
    a workflow port's type is the value of a dict's key, `"Extra": str, int`,
    which is not."""
    answer, text = _edited(
        tmp_path,
        "createPort",
        workflow="flow",
        direction="output",
        portName="Extra",
        portType="str, int",
    )
    assert answer["status"] == "ok", answer
    assert _added(text) == ['@workflow(inputs={"Start": str}, outputs={"Extra": (str, int)})']
    ast.parse(text)

    answer, text = _edited(
        tmp_path,
        "createEntityPort",
        entityType="Sink",
        portDirection="input",
        portName="More",
        portType="str, int",
    )
    assert answer["status"] == "ok", answer
    assert _added(text) == ['        More = Port[str, int](direction="in")']

    answer, text = _edited(
        tmp_path,
        "updatePortType",
        entity="Sink",
        portDirection="input",
        portName="In",
        newValue="str, int",
    )
    assert answer["status"] == "ok", answer
    assert _added(text) == ['        In = Port[str, int](direction="in")']


# ── an argument the decorator does not take ────────────────────────────

HELPER = """
@agent(prompt="x")
class Helper:
    pass
"""


def _merge_into(
    tmp_path: Path, source: str, name: str = "agent", **args: object
) -> tuple[dict, str]:
    return _edited(
        tmp_path,
        "mergeDefinitionAnnotationArgs",
        source,
        entityType="Helper",
        annotationName=name,
        **args,
    )


@pytest.mark.parametrize("name", ["useLsp", "useMcp", "lspServerConfigs"])
def test_merge_refuses_a_name_the_export_shows_and_agent_does_not_take(
    tmp_path: Path, name: str
) -> None:
    """The graph export lists these among an agent's arguments, for an IDE
    to show. Written back, the file fails as it is imported: `agent() got an
    unexpected keyword argument`."""
    answer, text = _merge(tmp_path, {name: "True"})
    assert answer["status"] == "error", answer
    assert answer["diagnostic"] == {
        "code": "unknown_argument",
        "annotation": "agent",
        "argument": name,
    }
    assert answer["message"] == f"@agent: wfpy's agent() takes no argument {name!r}"
    assert text == FLOW


def test_merge_takes_a_name_in_either_spelling_the_decorator_takes(tmp_path: Path) -> None:
    source = "from wfpy import agent\n" + HELPER
    for name in ("use_skill", "useSkill", "lspServers", "reasoningEffort"):
        answer, text = _merge_into(tmp_path, source, argUpdates={name: "None"})
        assert answer["status"] == "ok", answer
        assert _added(text, source) == [f'@agent(prompt="x", {name}=None)']
    # No spelling of an argument `agent()` takes.
    answer, text = _merge_into(tmp_path, source, argUpdates={"effort": '"high"'})
    assert answer["diagnostic"].get("code") == "unknown_argument", answer
    assert text == source


def _imports(text: str) -> bool:
    """Whether the decorators of `text` take what they are given."""
    exec(compile(text, "edited.py", "exec"), {"__name__": "edited"})
    return True


TOOL = """from wfpy import tool


@tool(cmd="echo", inherit_stdio=False)
class Helper:
    pass
"""


def test_merge_takes_an_argument_in_a_spelling_the_decorator_does_not(tmp_path: Path) -> None:
    """The export names `@tool`'s `inherit_stdio` `inheritStdio`. Sent back,
    it is the argument the file has, in the file's spelling. Refusing the
    name would refuse a Save that worked."""
    answer, text = _merge_into(tmp_path, TOOL, "tool", argUpdates={"inheritStdio": "True"})
    assert answer["status"] == "ok", answer
    assert _added(text, TOOL) == ['@tool(cmd="echo", inherit_stdio=True)']
    assert _imports(text)


def test_merge_writes_a_new_argument_in_the_spelling_the_decorator_takes(tmp_path: Path) -> None:
    """`inheritStdio=True` was appended as given, and `tool()` refuses it."""
    source = TOOL.replace(", inherit_stdio=False", "")
    answer, text = _merge_into(tmp_path, source, "tool", argUpdates={"inheritStdio": "True"})
    assert answer["status"] == "ok", answer
    assert _added(text, source) == ['@tool(cmd="echo", inherit_stdio=True)']
    assert _imports(text)

    # `reasoning_effort` is `variant`, as `@agent` reads it.
    source = "from wfpy import agent\n" + HELPER
    answer, text = _merge_into(tmp_path, source, argUpdates={"reasoning_effort": '"high"'})
    assert answer["status"] == "ok", answer
    assert _added(text, source) == ['@agent(prompt="x", variant="high")']

    for given, taken in (("actionName", "action_name"), ("view_type", "viewType")):
        source = 'from wfpy import viewer\n\n\n@viewer(command="x")\nclass Helper:\n    pass\n'
        answer, text = _merge_into(tmp_path, source, "viewer", argUpdates={given: '"v"'})
        assert answer["status"] == "ok", answer
        assert _added(text, source) == [f'@viewer(command="x", {taken}="v")']
        assert _imports(text)


def test_merge_respells_an_argument_the_file_spells_as_the_decorator_does_not(tmp_path: Path) -> None:
    """A file with `inheritStdio=` fails as it is imported. Given a value,
    the argument is written in the decorator's spelling; left alone, it
    stays as written."""
    source = TOOL.replace("inherit_stdio", "inheritStdio")
    for given in ("inheritStdio", "inherit_stdio"):
        answer, text = _merge_into(tmp_path, source, "tool", argUpdates={given: "True"})
        assert answer["status"] == "ok", answer
        assert _added(text, source) == ['@tool(cmd="echo", inherit_stdio=True)']
        assert _imports(text)
    answer, text = _merge_into(tmp_path, source, "tool", argUpdates={"shell": "True"})
    assert answer["status"] == "ok", answer
    assert _added(text, source) == ['@tool(cmd="echo", inheritStdio=False, shell=True)']


def test_merge_asks_a_decorator_written_with_wfpys_module(tmp_path: Path) -> None:
    """`@wfpy.agent` is wfpy's where `wfpy` is the module: under either name
    of the annotation, and under the name the file imports the module as."""
    helper = HELPER.replace("@agent", "@wfpy.agent")
    source = "import wfpy\n" + helper
    for name in ("agent", "wfpy.agent"):
        answer, text = _merge_into(tmp_path, source, name, argUpdates={"useMcp": "True"})
        assert answer["diagnostic"] == {"code": "unknown_argument", "annotation": name, "argument": "useMcp"}
        assert answer["message"] == f"@{name}: wfpy's agent() takes no argument 'useMcp'"
        assert text == source
        answer, text = _merge_into(tmp_path, source, name, argUpdates={"model": '"m"'})
        assert answer["status"] == "ok", answer
        assert _added(text, source) == ['@wfpy.agent(prompt="x", model="m")']

    source = "import wfpy as wf\n" + HELPER.replace("@agent", "@wf.agent")
    answer, text = _merge_into(tmp_path, source, argUpdates={"useMcp": "True"})
    assert answer["diagnostic"].get("code") == "unknown_argument", answer
    assert text == source

    # `import wfpy.core` binds `wfpy` as `import wfpy` does.
    source = "import wfpy.core\n" + helper
    answer, text = _merge_into(tmp_path, source, argUpdates={"useMcp": "True"})
    assert answer["diagnostic"].get("code") == "unknown_argument", answer


def test_merge_does_not_ask_a_decorator_of_another_module(tmp_path: Path) -> None:
    """The merge finds a decorator by its last name, so `@mylib.agent` is
    found as `agent`: it is not wfpy's, and takes what it takes."""
    (tmp_path / "mylib.py").write_text(
        "def agent(**settings):\n    return lambda cls: cls\n", encoding="utf-8"
    )
    others = [
        ("import mylib\n", "@mylib.agent"),
        ("import mylib as wfpy\n", "@wfpy.agent"),
        ("import wfpy\nimport mylib\n\nwfpy = mylib\n", "@wfpy.agent"),
        ("import mylib\n\nwfpy = mylib\n", "@wfpy.agent"),
        ("", "@wfpy.agent"),  # `wfpy` is bound by nothing: not for the merge to say
        ("import wfpy\n", "@wfpy.core.agent"),
    ]
    for head, written in others:
        source = head + HELPER.replace("@agent", written)
        answer, text = _merge_into(tmp_path, source, argUpdates={"useMcp": "True"})
        assert answer["status"] == "ok", (head, written, answer)
        assert _added(text, source) == [f'{written}(prompt="x", useMcp=True)']


def test_merge_names_the_definition_it_does_not_find_before_the_argument(tmp_path: Path) -> None:
    """Whether the name is taken depends on the decorator, so the decorator
    is looked for first."""
    source = "from wfpy import agent\n" + HELPER
    answer = _edited(
        tmp_path, "mergeDefinitionAnnotationArgs", source,
        entityType="Missing", annotationName="agent", argUpdates={"useMcp": "True"},
    )[0]
    assert answer["status"] == "error", answer
    assert "Definition not found for annotation merge: Missing" in answer["message"]


def test_merge_asks_only_a_decorator_that_is_wfpys(tmp_path: Path) -> None:
    """`agent` is wfpy's where the file imports it from wfpy, or binds it not
    at all (the edit then imports it). One the file defines, or takes from
    elsewhere, may take what wfpy's does not."""
    (tmp_path / "mylib.py").write_text(
        "def agent(**settings):\n    return lambda cls: cls\n", encoding="utf-8"
    )
    wfpys = [
        "from wfpy import agent\n",
        "from wfpy import Port, agent as agent\n",
        "from wfpy import *\n",
        "",
        "try:\n    from wfpy import agent\nexcept ImportError:\n    agent = None\n",
    ]
    for head in wfpys:
        answer, text = _merge_into(tmp_path, head + HELPER, argUpdates={"useMcp": "True"})
        assert answer["diagnostic"].get("code") == "unknown_argument", (head, answer)
        assert text == head + HELPER
    its_own = [
        "def agent(**settings):\n    return lambda cls: cls\n",
        "from mylib import agent\n",
        "from mylib import *\n",
        "from wfpy import *\nfrom mylib import *\n",
        "from wfpy import agent\n\nagent = print\n",
        "from wfpy import tool as agent\n",
    ]
    for head in its_own:
        answer, text = _merge_into(tmp_path, head + HELPER, argUpdates={"useMcp": "True"})
        assert answer["status"] == "ok", (head, answer)
        assert _added(text, head + HELPER) == ['@agent(prompt="x", useMcp=True)']


def test_merge_reads_the_signature_the_decorator_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a list kept beside it: a parameter `agent()` gets is taken from
    then on, and a decorator that takes `**kwargs` takes any name."""
    import wfpy

    source = "from wfpy import agent\n" + HELPER
    answer, text = _merge_into(tmp_path, source, argUpdates={"budget": "3"})
    assert answer["diagnostic"].get("code") == "unknown_argument", answer
    assert text == source

    def with_budget(cls: type | None = None, *, prompt: str = "", budget: int = 0) -> object:
        return cls

    monkeypatch.setattr(wfpy, "agent", with_budget)
    answer, text = _merge_into(tmp_path, source, argUpdates={"budget": "3"})
    assert answer["status"] == "ok", answer
    assert _added(text, source) == ['@agent(prompt="x", budget=3)']
    answer, text = _merge_into(tmp_path, source, argUpdates={"model": '"m"'})
    assert answer["diagnostic"].get("code") == "unknown_argument", answer

    def with_any(cls: type | None = None, **settings: object) -> object:
        return cls

    monkeypatch.setattr(wfpy, "agent", with_any)
    answer, text = _merge_into(tmp_path, source, argUpdates={"anything": "3"})
    assert answer["status"] == "ok", answer
    assert _added(text, source) == ['@agent(prompt="x", anything=3)']


def test_merge_takes_out_an_argument_the_decorator_refuses(tmp_path: Path) -> None:
    """A file that has one fails already. Giving it a new value keeps it
    failing; taking it out is the repair, so a name to remove is not checked."""
    source = "from wfpy import agent\n" + HELPER.replace('prompt="x"', 'prompt="x", useMcp=True')
    answer, text = _merge_into(tmp_path, source, argUpdates={"useMcp": "False"})
    assert answer["diagnostic"].get("code") == "unknown_argument", answer
    assert text == source

    answer, text = _merge_into(
        tmp_path, source, argUpdates={"model": '"m"'}, argRemovals=["useMcp"]
    )
    assert answer["status"] == "ok", answer
    assert _added(text, source) == ['@agent(prompt="x", model="m")']


@pytest.mark.parametrize(
    ("decorator", "name"),
    [
        ('tool(cmd="x")', "timeout_ms"),  # `timeout`
        ("viewer", "title"),
        ('context(read=["a"])', "scope"),
        ("task", "prompt"),  # takes the class, and nothing else
    ],
)
def test_merge_asks_each_of_wfpys_decorators(tmp_path: Path, decorator: str, name: str) -> None:
    called = decorator.partition("(")[0]
    source = f"from wfpy import {called}\n\n\n@{decorator}\nclass Helper:\n    pass\n"
    answer, text = _merge_into(tmp_path, source, called, argUpdates={name: "1"})
    assert answer["status"] == "error", answer
    assert answer["diagnostic"] == {
        "code": "unknown_argument",
        "annotation": called,
        "argument": name,
    }
    assert answer["message"] == f"@{called}: wfpy's {called}() takes no argument {name!r}"
    assert text == source
