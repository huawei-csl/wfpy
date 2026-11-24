"""The text of a workflow file that does not parse, as a diagram can read it.

A file with a syntax fault has no tree, so it had no diagram: one bracket
left open took every node away. `drawable_source` repairs a copy of the text
(`_source_repair.repair_source`: the statements the parser refuses are
replaced, no line is added or removed) and then puts back what a diagram
needs of a statement the repair had to replace:

- the nodes a broken `name = Type(...` or `a, b = A(), B(...` makes, as
  `name = Type()` and `a, b = A(), B()`;
- the connection a broken `connect(a.Out, b.In` draws, as `connect(a.Out, b.In)`;
- the scope a broken `with stream` opens, as `with stream:`.

Only the start of such a statement can be trusted, so the arguments of a
node and whatever follows the two ends of a connection are left out.
`mark_faults` then says so on the graph: it is partial, each fault is an
error at the line Python names, and the node or the edge of a faulty line is
flagged.

The text is for drawing. It is never run and never written back to the
user's file.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wfpy._source_repair import SourceFault, repair_source

__all__ = [
    "DrawableSource",
    "drawable_source",
    "fault_errors",
    "fault_owner",
    "mark_faults",
    "owner_of",
    "syntax_fault",
]


@dataclass(frozen=True)
class DrawableSource:
    text: str  # parses, and has the lines of the file it was made from
    faults: tuple[SourceFault, ...]  # what was replaced, in line order


_NAME = r"[^\W\d]\w*"
_DOTTED = rf"{_NAME}(?:\.{_NAME})*"
# What stands between two things of a list: the comma, or the space a comma
# was left out of.
_BETWEEN = r"(?:[ \t]*,[ \t]*|[ \t]+)"
# An end of a connection: `node.Port`, or a port of the workflow by its name.
_END = rf"(?:{_DOTTED}|\"[^\"\\\n]*\"|'[^'\\\n]*')"
_NODES = re.compile(rf"(?P<names>{_NAME}(?:{_BETWEEN}{_NAME})*)[ \t]*=(?!=)[ \t]*(?P<calls>.*)")
_CALL = re.compile(rf"(?P<ref>{_DOTTED})[ \t]*\(")
_EDGE = re.compile(
    rf"(?P<call>(?:{_NAME}\.)*connect)[ \t]*\("
    rf"[ \t]*(?P<source>{_END}){_BETWEEN}(?P<target>{_END})[ \t]*(?=[,)\#]|$)"
)
_SCOPE = re.compile(rf"with[ \t]+(?P<subject>{_DOTTED})[ \t]*(?=[:\#]|$)")
# What `repair_source` leaves of a statement, and of a clause header.
_MASK = "pass"
_HEADER = "if True:"


def drawable_source(text: str) -> DrawableSource | None:
    """`text` with its faults repaired and its nodes and connections kept.

    None when there is nothing to repair (`text` parses as it is) or when the
    repair gave up: the caller then has no tree to draw from.
    """
    repaired = repair_source(text)
    if not repaired.parsed or not repaired.faults:
        return None
    lines = repaired.text.split("\n")
    for fault in repaired.faults:
        for offset, original in enumerate(fault.source.split("\n")):
            at = fault.first_line - 1 + offset
            if at >= len(lines):
                break
            left = lines[at].strip()
            if left == _MASK:
                kept = _kept_statement(original.strip())
            elif left == _HEADER:
                kept = _kept_header(original.strip())
            else:
                continue
            if kept is not None:
                lines[at] = lines[at][: len(lines[at]) - len(lines[at].lstrip())] + kept
    drawn = "\n".join(lines)
    try:
        ast.parse(drawn)
    except (SyntaxError, ValueError):
        # What was put back does not stand where it was put: the repair's own
        # text does.
        drawn = repaired.text
    return DrawableSource(text=drawn, faults=repaired.faults)


def _kept_statement(statement: str) -> str | None:
    """What a diagram needs of a replaced statement, or None when it makes no
    node and no connection, or when even its start cannot be read."""
    edge = _EDGE.match(statement)
    if edge:
        return _one_statement(f"{edge['call']}({edge['source']}, {edge['target']})")
    nodes = _NODES.match(statement)
    if not nodes:
        return None
    names = re.split(_BETWEEN, nodes["names"])
    calls = _calls(nodes["calls"])
    if len(calls) != len(names):
        return None  # not one call a name: which name is which is not known
    return _one_statement(f"{', '.join(names)} = {', '.join(ref + '()' for ref in calls)}")


def _calls(text: str) -> list[str]:
    """What each of the calls written side by side at the start of `text`
    calls: `A` and `b.B` for `A(1), b.B(x=(2,`. It stops at the first thing
    that is not a call, and a bracket left open ends the last one."""
    refs: list[str] = []
    at = 0
    while True:
        call = _CALL.match(text, at)
        if not call:
            return refs
        refs.append(call["ref"])
        depth, at = 1, call.end()
        while at < len(text) and depth:
            char = text[at]
            if char in "\"'":
                closing = text.find(char, at + 1)
                if closing < 0:
                    return refs
                at = closing
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            at += 1
        if depth:
            return refs
        between = re.compile(_BETWEEN).match(text, at)
        if between:
            at = between.end()


def _kept_header(header: str) -> str | None:
    """The scope a replaced `with` header opens: the connections under it
    are drawn in it."""
    scope = _SCOPE.match(header)
    return _one_statement(f"with {scope['subject']}:", body=" pass") if scope else None


def _one_statement(text: str, body: str = "") -> str | None:
    try:
        parsed = ast.parse(text + body).body
    except (SyntaxError, ValueError):
        return None  # a keyword where a name is expected
    return text if len(parsed) == 1 else None


def syntax_fault(text: str) -> tuple[int, int | None, str] | None:
    """Where Python stops in `text`, and why: line, column, message.

    None when it parses. This is the line `python file.py` names, which is
    not always the one another parser stops at.
    """
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return exc.lineno or 1, exc.offset or None, exc.msg or type(exc).__name__
    except ValueError as exc:  # a null byte
        return 1, None, str(exc)
    return None


def fault_errors(faults: tuple[SourceFault, ...], file_path: str) -> list[dict[str, Any]]:
    """The faults as file-level `syntax_error` entries of a graph."""
    errors = []
    for fault in faults:
        location: dict[str, Any] = {"file": file_path, "line": fault.line}
        if fault.column is not None:
            location["column"] = fault.column
        errors.append(
            {
                "severity": "error",
                "code": "syntax_error",
                "message": fault.message,
                "location": location,
            }
        )
    return errors


def mark_faults(
    graph: dict[str, Any],
    faults: tuple[SourceFault, ...],
    file_path: str,
    source_text: str | None = None,
) -> None:
    """Say on `graph`, drawn from a repaired text, what was repaired.

    The graph becomes `partial`, and `errors` gets a `syntax_error` for each
    fault, at the line and column Python names. What a fault belongs to is
    flagged (`isErrored`, `errorMessage`):

    - a node or an edge written on the lines of the fault, which is drawn
      from what was left of its statement;
    - every node of a class of this file the fault is in, when `source_text`
      (the repaired text) is given to find the class by.

    The flag is no second report of the fault, which `errors` places better
    than the start of a statement does. The graphs of the nested workflows
    of the same file are flagged alike; one of another file is not this
    file's to mark.
    """
    if not faults:
        return
    graph["partial"] = True
    graph.setdefault("errors", []).extend(fault_errors(faults, file_path))
    classes: dict[str, SourceFault] = {}
    if source_text is not None:
        try:
            tree = ast.parse(source_text)
        except (SyntaxError, ValueError):
            tree = None
        for fault in faults if tree is not None else ():
            owner = fault_owner(tree, fault) if tree is not None else None
            if owner is not None and owner[0] == "class":
                classes.setdefault(owner[1], fault)
    _flag_elements(graph, faults, _resolved(file_path), classes)


def owner_of(tree: ast.Module, line: int) -> tuple[str, str, int] | None:
    """What `line` of a module belongs to.

    `("class", name, line of its header)` for a line of a class written at
    the top of the module, its decorators included; `("function", name, line
    of the statement)` for a line of such a function, the statement being the
    innermost one of its body that holds the line. None for a line that is in
    neither.
    """
    for top in tree.body:
        decorators = getattr(top, "decorator_list", [])
        first = min([top.lineno, *(decorator.lineno for decorator in decorators)])
        if not first <= line <= (top.end_lineno or top.lineno):
            continue
        if isinstance(top, ast.ClassDef):
            return "class", top.name, top.lineno
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return "function", top.name, _statement_line(top.body, line) or top.lineno
        return None
    return None


def fault_owner(tree: ast.Module, fault: SourceFault) -> tuple[str, str, int] | None:
    """What a repaired fault belongs to, in the tree of the repaired text.

    The line the parser named may be one the repair left empty (the second
    line of a statement that is one line now), and the first line of the
    span may be a decorator that went with its statement. So the lines of
    the span are tried in order: the first one in a class decides, or the
    first one a statement of a function starts on.
    """
    inside: tuple[str, str, int] | None = None
    for line in range(fault.first_line, fault.last_line + 1):
        owner = owner_of(tree, line)
        if owner is None:
            continue
        if owner[0] == "class" or owner[2] == line:
            return owner
        inside = inside or owner
    return inside


def _statement_line(body: list[ast.stmt], line: int) -> int | None:
    for statement in body:
        if not statement.lineno <= line <= (statement.end_lineno or statement.lineno):
            continue
        blocks = [getattr(statement, name, None) for name in ("body", "orelse", "finalbody")]
        blocks += [handler.body for handler in getattr(statement, "handlers", [])]
        blocks += [case.body for case in getattr(statement, "cases", [])]
        for block in blocks:
            inner = _statement_line(block, line) if isinstance(block, list) else None
            if inner is not None:
                return inner
        return statement.lineno
    return None


def _flag_elements(
    graph: dict[str, Any],
    faults: tuple[SourceFault, ...],
    here: str,
    classes: dict[str, SourceFault],
) -> None:
    # A node is read by `metadata` and an edge by `meta` or `metadata`: the
    # keys a reader of this payload takes the state of each from.
    for key, elements in (("metadata", graph.get("nodes")), ("meta", graph.get("edges"))):
        for element in elements or []:
            if not isinstance(element, dict):
                continue
            fault = _fault_at(element.get("location"), faults, here)
            if fault is None and key == "metadata":
                # A class of this file is named as it is: a dotted name is
                # one of another module.
                fault = classes.get(str(element.get("ref", "")))
            if fault is None:
                continue
            meta = element.get(key)
            if not isinstance(meta, dict):
                meta = {}
                element[key] = meta
            meta["isErrored"] = True
            meta.setdefault("errorMessage", fault.message)
    for child in graph.get("children") or []:
        nested = child.get("graph") if isinstance(child, dict) else None
        if isinstance(nested, dict) and _resolved(str(nested.get("file", ""))) == here:
            _flag_elements(nested, faults, here, classes)


def _fault_at(location: Any, faults: tuple[SourceFault, ...], here: str) -> SourceFault | None:
    if not isinstance(location, dict) or not isinstance(location.get("line"), int):
        return None
    if _resolved(str(location.get("file", ""))) != here:
        return None
    line = location["line"]
    for fault in faults:
        if fault.first_line <= line <= fault.last_line:
            return fault
    return None


def _resolved(file_path: str) -> str:
    try:
        return str(Path(file_path).resolve())
    except OSError:
        return file_path
