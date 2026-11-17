"""What the source says, where the graph export needs the written text.

The export describes a workflow from the objects its builder made: a node's
parameter is the value it received, an annotation's argument is what the
decorator stored. The diagram edits the source, though, and writing a value
back where the source wrote an expression (`File(path=TESTBENCH)`, a
factory's `commands=COMMANDS`) replaces the expression with whatever it
evaluated to on this machine. So beside each value the export carries the
text the source wrote for it, read here from the file -- statically: nothing
is imported or run that the build has not already run.

Every lookup answers ``None`` when the source cannot be found or read, and
the export then says nothing more than it did.
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import weakref
from typing import Any

_PARSED: dict[str, tuple[tuple[int, int], str, ast.Module | None]] = {}


def parsed(file: str | None) -> tuple[str, ast.Module] | None:
    """The text and tree of ``file``, read once per change of the file."""
    if not file:
        return None
    try:
        stat = os.stat(file)
    except OSError:
        return None
    key = (stat.st_mtime_ns, stat.st_size)
    hit = _PARSED.get(file)
    if hit is None or hit[0] != key:
        try:
            with open(file, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            return None
        try:
            tree: ast.Module | None = ast.parse(text)
        except (SyntaxError, ValueError):
            tree = None
        hit = (key, text, tree)
        _PARSED[file] = hit
    if hit[2] is None:
        return None
    return hit[1], hit[2]


def segment(text: str, node: ast.AST | None) -> str | None:
    if node is None:
        return None
    return ast.get_source_segment(text, node)


def python_literal(value: Any) -> str:
    """``value`` written as Python, strings in double quotes as the rest of the
    export writes them."""
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace("\r", "\\r")
            .replace("\n", "\\n")
            .replace("\t", "\\t")
            .replace('"', '\\"')
        )
        return f'"{escaped}"'
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(python_literal(item) for item in value) + "]"
    if isinstance(value, tuple):
        inner = ", ".join(python_literal(item) for item in value)
        return f"({inner},)" if len(value) == 1 else f"({inner})"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(f"{python_literal(k)}: {python_literal(v)}" for k, v in value.items())
            + "}"
        )
    return repr(value)


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# ── a class's decorators ─────────────────────────────────────────────────


_DECORATORS: weakref.WeakKeyDictionary[type, dict[str, dict[str, Any]] | None] = (
    weakref.WeakKeyDictionary()
)


def class_decorators(cls: type) -> dict[str, dict[str, Any]] | None:
    """The decorators written on ``cls``'s ``class`` statement, by name: for
    each, the source text of every keyword it was given (``keywords``) and
    whether it was also given ``**`` arguments the keywords do not show
    (``starred``). ``None`` when the class's source cannot be found."""
    try:
        return _DECORATORS[cls]
    except (KeyError, TypeError):
        pass
    found = _class_decorators(cls)
    try:
        _DECORATORS[cls] = found
    except TypeError:
        pass
    return found


def _class_decorators(cls: type) -> dict[str, dict[str, Any]] | None:
    try:
        file = inspect.getsourcefile(cls)
        line = inspect.getsourcelines(cls)[1]
    except (OSError, TypeError):
        return None
    found = parsed(file)
    if found is None:
        return None
    text, tree = found
    name = cls.__name__
    node = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
            and n.name == name
            and min([n.lineno, *(d.lineno for d in n.decorator_list)]) <= line <= n.lineno
        ),
        None,
    )
    if node is None:
        return None
    decorators: dict[str, dict[str, Any]] = {}
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        decorator_name = _callee_name(call.func if call is not None else decorator)
        if decorator_name is None:
            continue
        keywords: dict[str, str] = {}
        starred = False
        if call is not None:
            for keyword in call.keywords:
                if keyword.arg is None:
                    starred = True
                    continue
                written = segment(text, keyword.value)
                if written is not None:
                    keywords[keyword.arg] = written
            starred = starred or any(isinstance(arg, ast.Starred) for arg in call.args)
        decorators.setdefault(decorator_name, {"keywords": keywords, "starred": starred})
    return decorators


# ── the call that made an instance ───────────────────────────────────────


def _innermost_statement(tree: ast.Module, line: int) -> ast.stmt | None:
    best: ast.stmt | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return)):
            continue
        end = getattr(node, "end_lineno", None) or node.lineno
        if node.lineno <= line <= end:
            if best is None or (end - node.lineno) < (
                (best.end_lineno or best.lineno) - best.lineno
            ):
                best = node
    return best


def _in_a_workflow(tree: ast.Module, line: int) -> bool:
    """Whether ``line`` is a workflow's own: not in a function that one
    calls (``def _file(path): return FileSource(...)``), whose call is not
    what the workflow writes and whose text is not the node's."""
    enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= line <= (node.end_lineno or node.lineno):
            if enclosing is None or node.lineno > enclosing.lineno:
                enclosing = node
    if enclosing is None:
        return True
    return any(
        _callee_name(dec.func if isinstance(dec, ast.Call) else dec) == "workflow" for dec in enclosing.decorator_list
    )


def instantiation_call(
    source: dict[str, Any] | None, callee: str | None = None, factory: bool = False
) -> tuple[str, ast.Call] | None:
    """The call at ``source`` ({file, line}, where the builder made the
    instance) that made it: ``callee(...)``, or for a workflow a factory
    built, the factory's call in ``factory(...)()``."""
    if not source:
        return None
    found = parsed(source.get("file"))
    line = source.get("line")
    if found is None or not isinstance(line, int):
        return None
    text, tree = found
    statement = _innermost_statement(tree, line)
    if statement is None or not _in_a_workflow(tree, line):
        return None
    calls = [n for n in ast.walk(statement) if isinstance(n, ast.Call)]
    if factory:
        made = [c for c in calls if isinstance(c.func, ast.Call) and not c.args and not c.keywords]
        inner = [c.func for c in made if isinstance(c.func, ast.Call)]
        named = [c for c in inner if callee is None or _callee_name(c.func) == callee]
        chosen = named or inner
        return (text, chosen[0]) if len(chosen) == 1 else None
    named = [c for c in calls if callee is not None and _callee_name(c.func) == callee]
    if len(named) == 1:
        return text, named[0]
    value = getattr(statement, "value", None)
    if not named and isinstance(value, ast.Call):
        return text, value
    return None


def call_expressions(
    found: tuple[str, ast.Call] | None, positional_names: list[str] | None = None
) -> dict[str, str]:
    """The source text of each argument of the call, by parameter name:
    keywords by their own, positional ones by ``positional_names``."""
    if found is None:
        return {}
    text, call = found
    expressions: dict[str, str] = {}
    for index, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred):
            break
        if positional_names is not None and index < len(positional_names):
            written = _argument_text(text, arg)
            if written is not None:
                expressions[positional_names[index]] = written
    for keyword in call.keywords:
        if keyword.arg is None:
            continue
        written = _argument_text(text, keyword.value)
        if written is not None:
            expressions[keyword.arg] = written
    return expressions


def _argument_text(text: str, node: ast.expr) -> str | None:
    """An argument's text as the file has it, its later lines at the
    file's columns (the edit that writes it back reads them so). With its
    own parentheses where it is not an expression without them: ast leaves
    them out, and `"a"` over a line break to `"b"` needs them."""
    written = segment(text, node)
    if written is None or "\n" not in written or _is_expression(written):
        return written
    lines = re.split(r"\r\n|\r|\n", text)
    starts = [0]
    for line, ending in zip(lines, re.findall(r"\r\n|\r|\n", text)):
        starts.append(starts[-1] + len(line) + len(ending))

    def offset(lineno: int, col: int) -> int:
        return starts[lineno - 1] + len(lines[lineno - 1].encode("utf-8")[:col].decode("utf-8", errors="ignore"))

    if node.end_lineno is None or node.end_col_offset is None:
        return written
    start, end = offset(node.lineno, node.col_offset), offset(node.end_lineno, node.end_col_offset)
    for _ in range(8):
        before, after = text[:start].rstrip(), text[end:].lstrip()
        if not (before.endswith("(") and after.startswith(")")):
            return written
        start, end = len(before) - 1, len(text) - len(after) + 1
        if _is_expression(text[start:end]):
            return text[start:end]
    return written


def _is_expression(written: str) -> bool:
    try:
        ast.parse(written, mode="eval")
    except SyntaxError:
        return False
    return True


# ── the factory that builds a workflow ───────────────────────────────────


def _own_statements(body: list[ast.stmt]) -> list[ast.stmt]:
    """``body`` and the statements nested in its blocks, not in nested defs."""
    out: list[ast.stmt] = []
    for stmt in body:
        out.append(stmt)
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            out.extend(_own_statements(getattr(stmt, field, None) or []))
        for handler in getattr(stmt, "handlers", None) or []:
            out.extend(_own_statements(handler.body))
    return out


def _factory_function(
    definition: dict[str, Any] | None,
) -> tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, bool] | None:
    """The function whose body defines the workflow ``definition`` ({file,
    line} of its ``def``) names, its file's text, and whether it is a method
    (called on an instance or a class, which passes the first parameter)."""
    if not definition:
        return None
    found = parsed(definition.get("file"))
    line = definition.get("line")
    if found is None or not isinstance(line, int):
        return None
    text, tree = found
    methods: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and not any(
                    isinstance(d, ast.Name) and d.id == "staticmethod" for d in stmt.decorator_list
                ):
                    methods.add(id(stmt))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for stmt in _own_statements(node.body):
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            lines = [stmt.lineno, *(d.lineno for d in stmt.decorator_list)]
            if line in lines:
                return text, node, id(node) in methods
    return None


def factory_signature(definition: dict[str, Any] | None) -> list[dict[str, str]] | None:
    """The parameters, as written, of the function whose body defines the
    workflow ``definition`` ({file, line} of its ``def``) names: each with its
    ``name``, its annotation's text as ``type`` and its default's text as
    ``defaultValue``, the last two only when written."""
    found = _factory_function(definition)
    if found is None:
        return None
    text, fn, method = found
    return signature_parameters(text, fn, method=method)


def factory_positional_names(definition: dict[str, Any] | None) -> list[str] | None:
    """The names a call's positional arguments bind, in order, for the
    factory of ``definition``: its positional parameters, up to a
    ``*args`` (which takes the rest unnamed); never a keyword-only one."""
    found = _factory_function(definition)
    if found is None:
        return None
    _text, fn, method = found
    names = [arg.arg for arg in [*fn.args.posonlyargs, *fn.args.args]]
    return names[1:] if method else names


def signature_parameters(
    text: str, fn: ast.FunctionDef | ast.AsyncFunctionDef, method: bool = False
) -> list[dict[str, str]]:
    """A function's parameters as written: name, annotation, default. A
    method's first parameter (its `self` / `cls`) is not one a caller
    passes."""
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults.extend(args.defaults)
    entries: list[tuple[ast.arg, ast.expr | None]] = [
        *zip(positional, defaults),
        *zip(args.kwonlyargs, args.kw_defaults),
    ]
    if method and positional:
        entries = entries[1:]
    found: list[dict[str, str]] = []
    for arg, default in entries:
        entry = {"name": arg.arg}
        annotation = segment(text, arg.annotation)
        if annotation:
            entry["type"] = annotation
        default_text = segment(text, default)
        if default_text is not None:
            entry["defaultValue"] = default_text
        found.append(entry)
    return found
