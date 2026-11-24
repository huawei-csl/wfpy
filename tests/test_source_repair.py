"""A file with syntax faults parses again, so the rest of it can be drawn.

`wfpy._source_repair.repair_source` replaces what the parser refuses and
reports each replacement. Three groups of tests:

1. what is true of every result, whatever the text (the line count, the lines
   outside the faults, `parsed`, no exception, a bound on the parses);
2. one test a kind of fault, on a small file of two tasks and a workflow, with
   the exact text that comes back;
3. every tutorial and advanced example, broken in every line in six ways
   (`test_single_line_faults_over_the_examples`).

The repaired text is never run here either: it is parsed, and its definitions
are counted.
"""

from __future__ import annotations

import ast
import random
import re
import warnings
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import pytest

from wfpy import _source_repair
from wfpy._source_repair import RepairedSource, SourceFault, repair_source

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
BOM = chr(0xFEFF)
SURROGATE = chr(0xD800)

TEXT = '''\
from wfpy import Port, action, connect, task, workflow


@task
class Scale:
    """Multiply each token by a factor."""

    scale: int

    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def apply(self, x: int) -> int:
        if x < 0:
            return 0
        return self.scale * x


@task
class Add:
    """Read one token from each input, emit their sum."""

    class Ports:
        A = Port[int](direction="in")
        B = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"A": 1, "B": 1}, produces={"Out": 1})
    def add(self, a: int, b: int) -> int:
        return a + b


@workflow(inputs={"A": int, "B": int}, outputs={"Out": int})
def scaled_sum() -> None:
    """Scale both inputs, then add them."""
    scale_a = Scale(scale=2)
    scale_b = Scale(scale=3)
    add = Add()
    connect("A", scale_a.In)
    connect("B", scale_b.In)
    connect(scale_a.Out, add.A)
    connect(
        scale_b.Out,
        add.B,
    )
    connect(add.Out, "Out")
'''

BRANCHES = """\
def read(path, mode):
    try:
        text = open(path).read()
    except OSError as err:
        text = ""
    else:
        text = text.strip()
    finally:
        done()
    if mode < 0:
        return None
    elif mode == 0:
        return text
    else:
        text = text.lower()
    match mode:
        case 1:
            return text
        case _:
            return ""
    for part in text:
        use(part)
    while text:
        text = text[1:]
    with open(path) as handle:
        handle.read()
"""

PROMPT = '''\
from wfpy import Port, agent

PROMPT = """You are a careful reviewer.

Read the draft, and say what's wrong with it.
Answer in one line.
"""


@agent(prompt=PROMPT)
class Review:
    """Review one draft."""

    Draft = Port[str](direction="in")
    Out = Port[str](direction="out")
'''


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LINE_END = re.compile(r"(\r\n|\r|\n)")


def _rows(text: str) -> list[tuple[str, str]]:
    """The lines of `text`, each with its line end, as the tokenizer splits them."""
    parts = _LINE_END.split(text)
    return list(zip(parts[0::2], parts[1::2] + [""]))


def _parse(text: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # an invalid escape in a damaged string
        return ast.parse(text)


def _holds(text: str, result: RepairedSource) -> ast.Module | None:
    """What is true of every result, whatever the text. Gives the tree of one that parses."""
    before = _rows(text[1:] if text.startswith(BOM) else text)
    after = _rows(result.text)
    assert len(after) == len(before)
    replaced: set[int] = set()
    end = 0
    for fault in result.faults:
        # in line order, never overlapping, and the fault's line is in its span
        assert end < fault.first_line <= fault.line <= fault.last_line <= len(before)
        assert fault.column is None or fault.column >= 1
        user = before[fault.first_line - 1 : fault.last_line]
        assert fault.source + user[-1][1] == "".join(line + ending for line, ending in user)
        replaced.update(range(fault.first_line, fault.last_line + 1))
        end = fault.last_line
    for number, (old, new) in enumerate(zip(before, after), 1):
        assert new == old or number in replaced
        assert new[1] == old[1]  # a line end is never touched
    return _parse(result.text) if result.parsed else None


def _repaired(text: str, max_rounds: int = 40) -> RepairedSource:
    # `repair_source` gives the text back when the repair itself fails; the
    # function under it does not, so a fault in the repair fails the test.
    result = _source_repair._repair(text, max_rounds)
    assert repair_source(text, max_rounds=max_rounds) == result
    _holds(text, result)
    return result


def _one_fault(broken: str) -> tuple[RepairedSource, SourceFault]:
    result = _repaired(broken)
    assert result.parsed
    assert len(result.faults) == 1, result.faults
    return result, result.faults[0]


def _break(old: str, new: str, text: str = TEXT) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def _line(needle: str, text: str) -> int:
    """The 1-based number of the one line of `text` that holds `needle`."""
    found = [n for n, line in enumerate(text.split("\n"), 1) if needle in line]
    assert len(found) == 1, (needle, found)
    return found[0]


def _put(text: str, lines: dict[int, str]) -> str:
    rows = text.split("\n")
    for number, new in lines.items():
        rows[number - 1] = new
    return "\n".join(rows)


def _span(fault: SourceFault) -> tuple[int, int, int, str]:
    return fault.line, fault.first_line, fault.last_line, fault.repair


def _definitions(text: str) -> list[str]:
    """Every class and function of `text`, nested ones too."""
    kinds = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return sorted(node.name for node in ast.walk(_parse(text)) if isinstance(node, kinds))


def _example_files() -> list[Path]:
    files = sorted(EXAMPLES_DIR.glob("*.py")) + sorted(EXAMPLES_DIR.glob("advanced/*.py"))
    assert len(files) >= 12, EXAMPLES_DIR
    return files


# ---------------------------------------------------------------------------
# What is true of every result
# ---------------------------------------------------------------------------


def test_text_that_parses_comes_back_as_it_is() -> None:
    for text in [TEXT, BRANCHES, PROMPT, "", "\n", "x = 1"]:
        assert _repaired(text) == RepairedSource(text=text, faults=(), parsed=True)
    for path in _example_files():
        text = path.read_text(encoding="utf-8")
        assert repair_source(text) == RepairedSource(text=text, faults=(), parsed=True), path


def test_no_line_is_added_or_removed_and_the_rest_is_untouched() -> None:
    broken = _break("return a + b", "return a +")
    broken = _break("        scale_b.Out,\n", "        scale_b.Out\n", broken)
    result = _repaired(broken)

    was, now = broken.split("\n"), result.text.split("\n")
    assert len(now) == len(was)
    replaced = {
        number for fault in result.faults for number in range(fault.first_line, fault.last_line + 1)
    }
    connect = _line("scale_b.Out", broken) - 1
    assert replaced == {_line("return a +", broken), *range(connect, connect + 4)}
    for number, (old, new) in enumerate(zip(was, now), 1):
        assert (new == old) != (number in replaced)


def test_parsed_says_whether_the_text_parses() -> None:
    broken = "".join(f"x{n} = {n} +\n" for n in range(6))

    whole = _repaired(broken)
    assert whole.parsed
    assert whole.text == "pass\n" * 6
    assert [fault.line for fault in whole.faults] == [1, 2, 3, 4, 5, 6]

    # out of rounds: the last attempt, with the faults repaired so far
    part = _repaired(broken, max_rounds=3)
    assert not part.parsed
    assert part.text == "pass\npass\npass\nx3 = 3 +\nx4 = 4 +\nx5 = 5 +\n"
    assert [fault.line for fault in part.faults] == [1, 2, 3]
    with pytest.raises(SyntaxError):
        ast.parse(part.text)


ODD_TEXTS = {
    "empty": "",
    "blank": "   \n\t\n  ",
    "form feed": "\f\n\fx = (\n",
    "every byte": bytes(range(256)).decode("latin-1"),
    "null byte": "x = 1\n\0\ny = (\n",
    "only null bytes": "\0\0\0",
    "lone surrogate": "x = '" + SURROGATE + "'\ny = (\n",
    "tabs and spaces": "if x:\n\ty = 1\n        z = 2\n  \tw = 3\n",
    "crlf": "x = (\r\ny = 2 +\r\n",
    "lone cr": "x = (\ry = 2 +\r",
    "no final newline": "def f(:\n    x = (",
    "byte order mark": BOM + "x = (\n",
    "only a byte order mark": BOM,
    "two byte order marks": BOM + BOM + "x = 1\n",
    "long line": "x = [" + "1, " * 50_000,
    "long string": 'x = "' + "a" * 200_000,
    "deep brackets": "(" * 5000,
    "deep brackets, closed": "(" * 300 + ")" * 300,
    "deep expression": "1+" * 20_000 + "1",
    "deep blocks": "".join(" " * n + "if x:\n" for n in range(150)),
    "headers only": "if x:\n" * 50,
    "a backslash": "\\",
    "a backslash at the end": "x = 1 + \\",
    "quotes only": '"""' * 7 + "'''" * 3 + "\"'",
    "closing brackets only": ")]}\n" * 30,
    "other line separators": "x = 1" + chr(0x2028) + "y = (" + chr(0x85) + "\x0b\x1c\x1e z",
    "not python": "<html>\n  <body class='x'>It's not Python: 10% of {files} aren't.</body>\n",
}


@pytest.mark.parametrize("name", sorted(ODD_TEXTS))
def test_never_raises(name: str) -> None:
    result = _repaired(ODD_TEXTS[name])
    assert isinstance(result.parsed, bool)


def test_random_damage_never_raises() -> None:
    """400 texts, each `TEXT` after one to three random edits; the seed is fixed."""
    rng = random.Random(20240)
    alphabet = ["(", ")", "[", "]", "{", "}", ":", ",", ".", "'", '"', '"""', "=", "+", " ", "\t"]
    alphabet += ["@", "#", "\\", "\n", "\r", "\0", "    ", "def ", "class ", "else:", "f'{", BOM]
    repaired = 0
    for _ in range(400):
        text = TEXT
        for _ in range(rng.randint(1, 3)):
            at = rng.randrange(len(text))
            edit = rng.randrange(4)
            if edit == 0:
                text = text[:at] + text[at + 1 :]
            elif edit == 1:
                text = text[:at] + rng.choice(alphabet) + text[at:]
            elif edit == 2:
                text = text[:at] + text[at + rng.randrange(1, 40) :]
            else:
                lines = text.split("\n")
                first, second = rng.randrange(len(lines)), rng.randrange(len(lines))
                lines[first], lines[second] = lines[second], lines[first]
                text = "\n".join(lines)
        repaired += _repaired(text).parsed
    assert repaired >= 396  # measured: 400 on Python 3.11 and 3.14


def test_one_round_is_one_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    parses: list[str] = []
    real = ast.parse

    def counted(text: str) -> ast.Module:
        parses.append(text)
        return real(text)

    monkeypatch.setattr(ast, "parse", counted)
    broken = "".join(f"x{n} = {n} +\n" for n in range(6))
    # max_rounds -> parses: one a repair, and the one that finds nothing left
    for max_rounds, expected in ((0, 1), (-3, 1), (1, 2), (3, 4), (6, 7), (40, 7)):
        parses.clear()
        result = repair_source(broken, max_rounds=max_rounds)
        assert len(parses) == expected, max_rounds
        assert result.parsed == (max_rounds >= 6)
        assert len(result.faults) == min(max(max_rounds, 0), 6)

    parses.clear()
    assert repair_source(TEXT).parsed
    assert len(parses) == 1


def test_a_large_file_with_one_fault_is_parsed_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    body = TEXT.split("\n", 3)[3]
    copies = [body.replace("Scale", f"Scale{n}").replace("Add", f"Add{n}") for n in range(150)]
    text = "".join(copies)
    broken = _break("class Add75:", "class Add75(:", text)
    assert broken.count("\n") > 6000

    parses: list[str] = []
    real = ast.parse
    monkeypatch.setattr(ast, "parse", lambda source: parses.append(source) or real(source))
    result = repair_source(broken)

    assert result.parsed
    assert len(parses) == 2
    assert result.text == text
    assert [(fault.repair, fault.source) for fault in result.faults] == [
        ("stubbed", "class Add75(:")
    ]


def test_source_holds_what_the_user_wrote() -> None:
    _, fault = _one_fault(_break("add = Add()", "add = Add(]  # no"))
    assert fault.source == "    add = Add(]  # no"

    # several lines: every line of the span, the line ends between them too
    broken = _break("        scale_b.Out,\n", "        scale_b.Out\n").replace("\n", "\r\n")
    _, fault = _one_fault(broken)
    assert fault.source == "    connect(\r\n        scale_b.Out\r\n        add.B,\r\n    )"


def test_faults_come_in_line_order() -> None:
    broken = _break("connect(scale_a.Out, add.A)", "connect(scale_a.Out add.A)")
    broken = _break("return a + b", "return a +", broken)
    broken = _break("class Scale:", "class Scale", broken)
    result = _repaired(broken)

    assert result.parsed
    assert [fault.line for fault in result.faults] == [
        _line("class Scale", broken),
        _line("return a +", broken),
        _line("connect(scale_a.Out add.A)", broken),
    ]
    assert [fault.repair for fault in result.faults] == ["stubbed", "masked", "masked"]
    assert _definitions(result.text) == _definitions(TEXT)


def test_line_ends_are_kept() -> None:
    broken = _break("add = Add()", "add = Add(")
    line = _line("add = Add(", broken)
    for ending in ("\r\n", "\r"):
        result, fault = _one_fault(broken.replace("\n", ending))
        assert result.text == _put(broken, {line: "    pass"}).replace("\n", ending)
        assert _span(fault) == (line, line, line, "masked")

    result, fault = _one_fault("x = 1\ny = (")  # no line end after the last line
    assert result.text == "x = 1\npass"
    assert fault.source == "y = ("

    # A blanked line between a lone "\r" and a "\n" is not left empty: the two
    # would read as one line end, and every line after it would be one off.
    result, fault = _one_fault("x = f(\r1 2,\n)\ny = 3\n")
    assert result.text == "pass\r \n\ny = 3\n"
    assert _span(fault) == (2, 1, 3, "masked")


def test_a_byte_order_mark_is_dropped() -> None:
    # Python reads a file without it; `ast.parse` refuses it in a str
    assert _repaired(BOM + TEXT) == RepairedSource(text=TEXT, faults=(), parsed=True)

    broken = "x = (\ny = 2\n"
    result, fault = _one_fault(BOM + broken)
    assert result.text == "pass\ny = 2\n"
    assert (fault.line, fault.source) == (1, "x = (")


def test_a_warning_of_the_parser_is_no_fault() -> None:
    text = 'pattern = "\\d+"\n'  # an invalid escape: a warning, an error under -W error
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("error")
        assert repair_source(text) == RepairedSource(text=text, faults=(), parsed=True)
    assert not seen


def test_a_fault_in_the_repair_gives_the_text_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def fails(text: str, max_rounds: int) -> RepairedSource:
        raise RuntimeError("a fault in the repair")

    monkeypatch.setattr(_source_repair, "_repair", fails)
    assert repair_source("x = (") == RepairedSource(text="x = (", faults=(), parsed=False)


# ---------------------------------------------------------------------------
# One kind of fault each
# ---------------------------------------------------------------------------


def test_bracket_not_closed() -> None:
    broken = _break("scale_a = Scale(scale=2)", "scale_a = Scale(scale=2")
    result, fault = _one_fault(broken)

    line = _line("scale_a = Scale(", broken)
    assert result.text == _put(broken, {line: "    pass"})
    assert _span(fault) == (line, line, line, "masked")
    assert fault.source == "    scale_a = Scale(scale=2"
    assert fault.column is not None and fault.source[fault.column - 1] == "("
    assert _definitions(result.text) == _definitions(TEXT)


def test_bracket_not_closed_takes_the_lines_it_holds() -> None:
    # nothing says where the statement ends: the deeper lines under it go too
    broken = _break("        add.B,\n    )\n", "        add.B,\n\n")
    result, fault = _one_fault(broken)

    first = _line("scale_b.Out", broken) - 1
    assert result.text == _put(broken, {first: "    pass", first + 1: "", first + 2: ""})
    assert _span(fault) == (first, first, first + 2, "masked")
    assert _definitions(result.text) == _definitions(TEXT)


@pytest.mark.parametrize(
    ("old", "new", "stub"),
    [
        ("class Scale:", "class Scale", "class Scale:"),
        ("class Scale:", "class Scale(Base:", "class Scale:"),
        ("class Scale:", "class Scale(Base, metaclass=):", "class Scale:"),
    ],
)
def test_broken_class_header_keeps_name_and_body(old: str, new: str, stub: str) -> None:
    broken = _break(old, new)
    result, fault = _one_fault(broken)

    line = _line(new, broken)
    assert result.text == _put(broken, {line: stub}) == TEXT
    assert _span(fault) == (line, line, line, "stubbed")
    assert fault.source == new


@pytest.mark.parametrize(
    ("new", "stub"),
    [
        ("def apply(self, x: int) -> int", "def apply(*_a, **_k):"),
        ("def apply(self, x: int -> int:", "def apply(*_a, **_k):"),
        ("def apply(self x: int) -> int:", "def apply(*_a, **_k):"),
        ("async def apply(self, x: int) -> :", "async def apply(*_a, **_k):"),
        ("async   def  apply(self, x: int = ) -> int:", "async def apply(*_a, **_k):"),
    ],
)
def test_broken_def_header_keeps_name_and_body(new: str, stub: str) -> None:
    broken = _break("def apply(self, x: int) -> int:", new)
    result, fault = _one_fault(broken)

    line = _line(new, broken)
    assert result.text == _put(broken, {line: "    " + stub})
    assert _span(fault) == (line, line, line, "stubbed")
    assert _definitions(result.text) == _definitions(TEXT)
    # the body is under it still, and its decorator above it
    scale = _parse(result.text).body[1]
    assert isinstance(scale, ast.ClassDef)
    apply = scale.body[-1]
    assert isinstance(apply, (ast.FunctionDef, ast.AsyncFunctionDef))
    assert (apply.name, len(apply.body), len(apply.decorator_list)) == ("apply", 2, 1)


def test_header_at_the_end_of_the_text_gets_a_body() -> None:
    result, fault = _one_fault(TEXT + "\n\nclass Last(\n")
    assert result.text == TEXT + "\n\nclass Last: ...\n"
    assert fault.repair == "stubbed"
    assert "Last" in _definitions(result.text)


@pytest.mark.parametrize(
    ("old", "new", "stub"),
    [
        ('    @action(consumes={"In": 1}, produces={"Out": 1})', None, "    @action"),
        ("@task\nclass Scale:", "@task(\nclass Scale:", "@task"),
        ("@task\nclass Add:", "@wfpy . core.task(name=)\nclass Add:", "@wfpy.core.task"),
        ("@workflow(inputs={", "@workflow(inputs{", "@workflow"),
    ],
)
def test_broken_decorator_does_not_take_the_definition_away(
    old: str, new: str | None, stub: str
) -> None:
    broken = _break(old, old[:-1] if new is None else new)
    result, fault = _one_fault(broken)

    assert result.text == _put(broken, {fault.line: stub})
    assert fault.repair == "stubbed"
    assert fault.first_line == fault.last_line
    assert _definitions(result.text) == _definitions(TEXT)
    decorated = [
        node.name
        for node in ast.walk(_parse(result.text))
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.decorator_list
    ]
    assert sorted(decorated) == ["Add", "Scale", "add", "apply", "scaled_sum"]


def test_header_without_a_name_keeps_its_body() -> None:
    # no name to keep: the block stays, and its decorator has nothing to stand on
    broken = _break("def apply(self, x: int) -> int:", "def (self, x: int) -> int:")
    result, fault = _one_fault(broken)

    line = _line("def (self", broken)
    assert result.text == _put(broken, {line - 1: "", line: "    if True:"})
    assert _span(fault) == (line, line - 1, line, "stubbed")
    names = _definitions(TEXT)
    names.remove("apply")
    assert _definitions(result.text) == names


@pytest.mark.parametrize(
    ("old", "new", "stub"),
    [
        ("if x < 0:", "if x < 0", "if True:"),
        ("if x < 0:", "if x < :", "if True:"),
    ],
)
def test_colon_missing(old: str, new: str, stub: str) -> None:
    broken = _break(old, new)
    result, fault = _one_fault(broken)

    line = _line(new, broken)
    assert result.text == _put(broken, {line: "        " + stub})
    assert _span(fault) == (line, line, line, "stubbed")
    assert fault.source == "        " + new


@pytest.mark.parametrize(
    ("old", "new", "stub"),
    [
        ("except OSError as err:", "except OSError as:", "    except Exception:"),
        ("else:\n        text = text.strip()", "else\n        text = text.strip()", "    else:"),
        ("finally:", "finally x:", "    finally:"),
        ("elif mode == 0:", "elif mode == :", "    elif True:"),
        (
            "else:\n        text = text.lower()",
            "else :: \n        text = text.lower()",
            "    else:",
        ),
        ("match mode:", "match mode +:", "    match 0:"),
        ("case 1:", "case 1 +:", "        case _:"),
        ("for part in text:", "for part in:", "    if True:"),
        ("while text:", "while text", "    if True:"),
        ("with open(path) as handle:", "with open(path as handle:", "    if True:"),
    ],
)
def test_broken_clause_is_replaced_by_one_of_its_kind(old: str, new: str, stub: str) -> None:
    # an `else` stays an `else`, so the statement it belongs to stays whole
    broken = _break(old, new, BRANCHES)
    result, fault = _one_fault(broken)

    line = broken[: broken.index(new)].count("\n") + 1
    assert result.text == _put(broken, {line: stub})
    assert _span(fault) == (line, line, line, "stubbed")


def test_try_without_a_handler() -> None:
    # the parser names the statement after the block, which has no fault
    handlers = BRANCHES[BRANCHES.index("    except") : BRANCHES.index("    if mode")]
    broken = _break(handlers, "", BRANCHES)
    result, fault = _one_fault(broken)

    assert result.text == _put(broken, {2: "    if True:"})
    assert _span(fault) == (2, 2, 2, "stubbed")
    assert fault.source == "    try:"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("    else:\n        text = text.strip()", "        else:\n        text = text.strip()"),
        ("    except OSError as err:", "        except OSError as err:"),
        ("    elif mode == 0:", "elif mode == 0:"),
        ("    finally:", "  finally:"),
    ],
)
def test_clause_at_the_wrong_indent(old: str, new: str) -> None:
    broken = _break(old, new, BRANCHES)
    result, fault = _one_fault(broken)

    assert result.text == BRANCHES
    assert fault.repair == "reindented"
    assert fault.source == new.split("\n")[0]


def test_string_not_closed() -> None:
    broken = _break('In = Port[int](direction="in")', 'In = Port[int](direction="in)')
    result, fault = _one_fault(broken)

    line = _line('direction="in)', broken)
    assert result.text == _put(broken, {line: "        pass"})
    assert _span(fault) == (line, line, line, "masked")
    assert _definitions(result.text) == _definitions(TEXT)


def test_triple_quoted_string_not_closed_is_reported_at_its_quote() -> None:
    # The parser pairs every quote after it the wrong way round and reports
    # the last one of the text, in another definition.
    broken = _break('"""Multiply each token by a factor."""', '"""Multiply each token by a factor.')
    line = _line('"""Multiply each token', broken)
    with pytest.raises(SyntaxError) as refusal:
        ast.parse(broken)
    assert refusal.value.lineno != line

    result, fault = _one_fault(broken)
    assert result.text == _put(broken, {line: "    pass"})
    assert _span(fault) == (line, line, line, "masked")
    assert fault.message.startswith("unterminated triple-quoted string literal")
    assert fault.column == 5
    assert _definitions(result.text) == _definitions(TEXT)


def test_text_of_an_open_string_is_part_of_its_fault() -> None:
    # the sentences of the string are read as code now: they are no faults of their own
    broken = _break('Answer in one line.\n"""\n', "Answer in one line.\n\n", PROMPT)
    result, fault = _one_fault(broken)

    assert result.text == _put(broken, {3: "pass", 5: "", 6: ""})
    assert _span(fault) == (3, 3, 6, "masked")
    assert fault.source.startswith('PROMPT = """You are') and fault.source.endswith("one line.")
    assert _definitions(result.text) == ["Review"]


def test_closing_quote_without_its_opening_one() -> None:
    broken = _break('PROMPT = """You are', "PROMPT = You are", PROMPT)
    result, fault = _one_fault(broken)

    assert result.text == _put(broken, {3: "", 5: "", 6: "", 7: "pass"})
    assert _span(fault) == (7, 3, 7, "masked")
    assert _definitions(result.text) == ["Review"]


def test_text_of_an_open_string_that_is_python_is_read_as_code() -> None:
    """Not done: a string that holds a workflow as text gives definitions when left open.

    Nothing in the text says that these lines were meant as a string.
    """
    text = '''\
SOURCE = """
from wfpy import task

@task
class Inner:
    pass
"""


def outer():
    """Write the source."""
    return SOURCE
'''
    result, fault = _one_fault(_break('    pass\n"""\n', "    pass\n\n", text))
    assert _span(fault) == (1, 1, 1, "masked")
    assert _definitions(result.text) == ["Inner", "outer"]


def test_comma_missing() -> None:
    broken = _break("connect(scale_a.Out, add.A)", "connect(scale_a.Out add.A)")
    result, fault = _one_fault(broken)

    line = _line("connect(scale_a.Out add.A)", broken)
    assert result.text == _put(broken, {line: "    pass"})
    assert _span(fault) == (line, line, line, "masked")
    assert _definitions(result.text) == _definitions(TEXT)


def test_fault_in_a_statement_of_several_lines_takes_the_statement() -> None:
    broken = _break("        scale_b.Out,\n", "        scale_b.Out\n")
    result, fault = _one_fault(broken)

    named = _line("scale_b.Out", broken)
    first = named - 1
    assert result.text == _put(broken, {first: "    pass", named: "", named + 1: "", named + 2: ""})
    assert _span(fault) == (named, first, first + 3, "masked")
    assert fault.source == "    connect(\n        scale_b.Out\n        add.B,\n    )"


def test_operator_left_hanging() -> None:
    broken = _break("return a + b", "return a +")
    result, fault = _one_fault(broken)

    line = _line("return a +", broken)
    assert result.text == _put(broken, {line: "        pass"})
    assert _span(fault) == (line, line, line, "masked")
    assert fault.source == "        return a +"
    assert _definitions(result.text) == _definitions(TEXT)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("    scale_b = Scale(scale=3)", "   scale_b = Scale(scale=3)"),
        ("    scale_b = Scale(scale=3)", "     scale_b = Scale(scale=3)"),
        ("    scale_b = Scale(scale=3)", "        scale_b = Scale(scale=3)"),
        ("        return self.scale * x", "      return self.scale * x"),
        ("    scale: int", "   scale: int"),
        ('    connect(add.Out, "Out")', '  connect(add.Out, "Out")'),
        # the first line of a block: every line after it is the odd one to the parser
        (
            '    """Scale both inputs, then add them."""',
            '   """Scale both inputs, then add them."""',
        ),
        ('    """Multiply each token by a factor."""', '   """Multiply each token by a factor."""'),
        ("        A = Port[int]", "         A = Port[int]"),
    ],
)
def test_indent_off(old: str, new: str) -> None:
    broken = _break(old, new)
    result, fault = _one_fault(broken)

    line = _line(new.strip(), broken)
    assert result.text == TEXT
    assert _span(fault) == (line, line, line, "reindented")
    assert fault.source == broken.split("\n")[line - 1]


def test_header_with_nothing_under_it() -> None:
    broken = _break("            return 0", "            # nothing yet")
    result, fault = _one_fault(broken)

    line = _line("if x < 0:", broken)
    assert result.text == _put(broken, {line: "        if x < 0: ..."})
    assert _span(fault) == (line, line, line, "filled")
    assert fault.source == "        if x < 0:"

    result, fault = _one_fault(TEXT + "\n\ndef last():\n")
    assert result.text == TEXT + "\n\ndef last(): ...\n"
    assert fault.repair == "filled"


def test_tabs_and_spaces() -> None:
    broken = _break("    scale_b = Scale(scale=3)", "\tscale_b = Scale(scale=3)")
    with pytest.raises(TabError):
        ast.parse(broken)
    result, fault = _one_fault(broken)

    line = _line("scale_b = Scale", broken)
    assert result.text == TEXT
    assert _span(fault) == (line, line, line, "reindented")
    assert fault.source == "\tscale_b = Scale(scale=3)"


@pytest.mark.parametrize("unreadable", ["\0", SURROGATE])
def test_what_the_parser_cannot_read(unreadable: str) -> None:
    # refused before the first line is read, with no line number
    broken = _break("    scale: int\n", "    scale: int = '" + unreadable + "'\n")
    result, fault = _one_fault(broken)

    line = _line("    scale: int", broken)
    assert result.text == _put(broken, {line: "    pass"})
    assert _span(fault) == (line, line, line, "masked")
    assert fault.column is None
    assert _definitions(result.text) == _definitions(TEXT)


def test_two_faults_in_two_definitions() -> None:
    broken = _break("return a + b", "return a +")
    broken = _break("connect(scale_a.Out, add.A)", "connect(scale_a.Out add.A)", broken)
    result = _repaired(broken)

    first, second = _line("return a +", broken), _line("connect(scale_a.Out add.A)", broken)
    assert result.parsed
    assert result.text == _put(broken, {first: "        pass", second: "    pass"})
    assert [_span(fault) for fault in result.faults] == [
        (first, first, first, "masked"),
        (second, second, second, "masked"),
    ]
    assert _definitions(result.text) == _definitions(TEXT)


# ---------------------------------------------------------------------------
# Every example, broken in every line
# ---------------------------------------------------------------------------


def _mutants(lines: list[str]) -> Iterator[tuple[str, int, str]]:
    """Six single-line faults: (kind, row, the line as broken)."""
    for row, line in enumerate(lines):
        code = line.rstrip()
        if not code.strip() or code.lstrip().startswith("#"):
            continue
        if code[-1] in ")]}":
            yield "bracket not closed", row, code[:-1]
        if code[-1] == ":":
            yield "colon missing", row, code[:-1]
        if code[-1] in "\"'":
            yield "string not closed", row, code[:-1]
        if "," in code:
            at = code.index(",")
            yield "comma missing", row, code[:at] + code[at + 1 :]
        if code[-1] not in "([{:,\\":
            yield "operator left hanging", row, code + " +"
        if code.startswith("    "):
            yield "indent off by one", row, code[1:]


def _top_level(tree: ast.Module) -> list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef]:
    kinds = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return [node for node in tree.body if isinstance(node, kinds)]


def _owner(tree: ast.Module, line: int) -> str:
    """The task or workflow that holds `line`, with its decorators; "" for none."""
    for node in _top_level(tree):
        first = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
        if first <= line <= (node.end_lineno or first):
            return node.name
    return ""


# kind: (mutants that are faults, at least;
#        not repaired, more than one fault reported, a definition lost,
#        fault in another definition; at most)
# The four counts are those of Python 3.11 and 3.14, which agree; over all
# kinds they came from 1902 mutants. A definition is lost only where a string
# that holds Python text is left open (see the test above).
SINGLE_LINE_FAULTS = {
    "bracket not closed": (300, 0, 0, 0, 0),
    "colon missing": (150, 0, 0, 0, 0),
    "comma missing": (200, 0, 0, 0, 0),
    "indent off by one": (400, 0, 0, 0, 0),
    "operator left hanging": (450, 0, 0, 0, 0),
    "string not closed": (60, 0, 0, 4, 0),
}


def test_single_line_faults_over_the_examples() -> None:
    """Every example, every line, six faults: the repair gives the rest of the file.

    The fault is looked up by `first_line`, as a caller should: `line` may be
    one that is blank after the repair.
    """
    mutants: dict[str, int] = defaultdict(int)
    misses: dict[tuple[str, int], list[tuple[str, int]]] = defaultdict(list)
    for path in _example_files():
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        names = [node.name for node in _top_level(tree)]
        lines = text.split("\n")
        for kind, row, new in _mutants(lines):
            broken = "\n".join(lines[:row] + [new] + lines[row + 1 :])
            result = _source_repair._repair(broken, 40)
            if result.parsed and not result.faults:
                continue  # it still parses: not a fault
            repaired = _holds(broken, result)
            mutants[kind] += 1
            where = (path.name, row + 1)
            if repaired is None:
                misses[kind, 0].append(where)
                continue
            if len(result.faults) != 1:
                misses[kind, 1].append(where)
            if [node.name for node in _top_level(repaired)] != names:
                misses[kind, 2].append(where)
            if _owner(repaired, result.faults[0].first_line) != _owner(tree, row + 1):
                misses[kind, 3].append(where)

    assert sorted(mutants) == sorted(SINGLE_LINE_FAULTS)
    for kind, (at_least, *at_most) in SINGLE_LINE_FAULTS.items():
        assert mutants[kind] >= at_least, kind
        for count, bound in enumerate(at_most):
            assert len(misses[kind, count]) <= bound, (kind, count, misses[kind, count])
