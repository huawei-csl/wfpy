"""wfpy.rewrite — libcst rewrite primitives for diagram edits."""

from __future__ import annotations

import dataclasses
import hashlib
import keyword
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

import libcst as cst
import libcst.matchers as m

from wfpy.workflow_identity import (
    detect_dynamic_workflow_identity_issues,
    format_dynamic_workflow_identity_error,
)

if TYPE_CHECKING:
    from wfpy._project_index import ProjectIndex

__all__ = [
    "RewriteError",
    "RewriteEngine",
    "load_module",
]


def _expr_to_code(node: cst.CSTNode | None) -> str:
    if node is None:
        return ""
    return cst.Module([]).code_for_node(node).strip()


def _dotted_name(node: cst.BaseExpression) -> str:
    """`a.b.c` for the module of a `from a.b.c import ...`."""
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        return f"{_dotted_name(node.value)}.{node.attr.value}"
    return ""


def _parse_user_expression(text: Any, what: str) -> cst.BaseExpression:
    """`text` parsed as a Python expression, or a refusal naming it.

    Everything the IDE hands over as source — a parameter's value, a port's
    type, an annotation's argument — goes through here, so a value that is
    not Python is told as that (`invalid_expression`) rather than surfacing
    as the parser's crash.
    """
    source = str(text if text is not None else "").strip()
    try:
        if not source:
            raise ValueError("empty")
        return cst.parse_expression(source)
    except (cst.ParserSyntaxError, ValueError) as exc:
        detail = str(exc).strip().splitlines()
        raise RewriteError(
            message=f"{what} is not a Python expression: {source!r}",
            diagnostic={
                "code": "invalid_expression",
                "expression": source,
                **({"details": detail[0]} if detail and detail[0] != "empty" else {}),
            },
        ) from None


def _free_names(expr: cst.BaseExpression) -> list[str]:
    """The names `expr` reads from its scope, in order, once each: not an
    attribute (`File.path`'s `path`), a keyword (`File(path=...)`), or a
    name the expression binds itself (a lambda's parameter, a
    comprehension's variable, a `:=` target)."""
    read: list[str] = []
    own: set[str] = set()

    class _Names(cst.CSTVisitor):
        def visit_Attribute(self, node: cst.Attribute) -> bool:
            node.value.visit(self)
            return False

        def visit_Arg(self, node: cst.Arg) -> bool:
            node.value.visit(self)
            return False

        def visit_Param(self, node: cst.Param) -> bool:
            own.add(node.name.value)
            if node.default is not None:
                node.default.visit(self)
            return False

        def visit_CompFor(self, node: cst.CompFor) -> None:
            for target in _target_names(node.target):
                own.add(target)

        def visit_NamedExpr(self, node: cst.NamedExpr) -> None:
            for target in _target_names(node.target):
                own.add(target)

        def visit_Name(self, node: cst.Name) -> None:
            if node.value not in read:
                read.append(node.value)

    expr.visit(_Names())
    return [name for name in read if name not in own and name not in ("True", "False", "None")]


def _keyword_arg(name: str, value: cst.BaseExpression) -> cst.Arg:
    """`name=value`, as a keyword argument is written by hand."""
    return cst.Arg(
        keyword=cst.Name(name),
        value=value,
        equal=cst.AssignEqual(
            whitespace_before=cst.SimpleWhitespace(""),
            whitespace_after=cst.SimpleWhitespace(""),
        ),
    )


def _with_inserted(items: list[Any], at: int, new: Any, opening: Any) -> list[Any]:
    """`items` (call arguments or dict elements, each carrying its comma) with
    `new` at index `at`, the commas laid out as the neighbours' are: one per
    line in a list written one per line, `, ` in one written on a line.
    `opening` is the whitespace after the opening bracket, which tells a
    one-item list's layout."""
    items = list(items)
    if not items:
        return [new.with_changes(comma=cst.MaybeSentinel.DEFAULT)]
    if len(items) >= 2:
        separator = items[min(max(at - 1, 0), len(items) - 2)].comma
        if not isinstance(separator, cst.Comma):
            separator = cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
    elif isinstance(opening, cst.ParenthesizedWhitespace):
        separator = cst.Comma(whitespace_after=opening.deep_clone())
    else:
        separator = cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
    if at >= len(items):
        trailing = items[-1].comma
        items[-1] = items[-1].with_changes(comma=separator.deep_clone())
        items.append(new.with_changes(comma=trailing))
    else:
        items.insert(at, new.with_changes(comma=separator.deep_clone()))
    return items


def _with_removed(items: list[Any], at: int) -> list[Any]:
    """`items` without the one at `at`; the last keeps what the list ended with."""
    items = list(items)
    removed = items.pop(at)
    if items and at == len(items):
        items[-1] = items[-1].with_changes(comma=removed.comma)
    return items


def _string_value(node: cst.CSTNode | None) -> str | None:
    """The value of a plain string literal (`"x"`, `'x'`), else None."""
    if isinstance(node, cst.SimpleString):
        value = node.evaluated_value
        return value if isinstance(value, str) else None
    return None


def _quoted_like(original: cst.SimpleString, value: str) -> cst.SimpleString:
    """A string literal of `value` in the quotes `original` is written in."""
    quote = original.quote
    prefix = original.prefix
    if quote in value or "\\" in value:
        return cst.SimpleString(repr(value))
    return cst.SimpleString(f"{prefix}{quote}{value}{quote}")


def _decorator_name(dec: cst.Decorator) -> str | None:
    """`task` for `@task`, `@task(...)`, `@wfpy.task` and `@wfpy.task(...)`."""
    target = dec.decorator
    if isinstance(target, cst.Call):
        target = target.func
    if isinstance(target, cst.Name):
        return target.value
    if isinstance(target, cst.Attribute):
        return target.attr.value
    return None


def _is_connect_call(node: cst.CSTNode) -> bool:
    if not isinstance(node, cst.Call):
        return False
    func = node.func
    return (isinstance(func, cst.Name) and func.value == "connect") or (
        isinstance(func, cst.Attribute) and func.attr.value == "connect"
    )


def _block_statements(body: Sequence[cst.BaseStatement]) -> Iterable[cst.BaseStatement]:
    """The statements of `body` and of the blocks of its compound
    statements (`if`, `try`, `with`, loops), not of the functions and
    classes it defines."""
    for stmt in body:
        if _is_main_guard(stmt):
            continue  # run as a script only, never when the module is imported
        yield stmt
        if isinstance(stmt, (cst.FunctionDef, cst.ClassDef)):
            continue
        blocks: list[cst.BaseSuite] = []
        for field in ("body", "orelse", "finalbody"):
            value = getattr(stmt, field, None)
            if isinstance(value, (cst.Else, cst.Finally)):
                value = value.body
            elif isinstance(value, cst.If):
                yield from _block_statements([value])
                continue
            if isinstance(value, cst.BaseSuite):
                blocks.append(value)
        for handler in getattr(stmt, "handlers", ()) or ():
            blocks.append(handler.body)
        for block in blocks:
            if isinstance(block, cst.IndentedBlock):
                yield from _block_statements(block.body)
            elif isinstance(block, cst.SimpleStatementSuite):
                yield from _block_statements([cst.SimpleStatementLine(body=block.body)])


def _is_main_guard(stmt: cst.CSTNode) -> bool:
    """`if __name__ == "__main__":`"""
    if not isinstance(stmt, cst.If) or not isinstance(stmt.test, cst.Comparison):
        return False
    test = stmt.test
    if len(test.comparisons) != 1 or not isinstance(test.comparisons[0].operator, cst.Equal):
        return False
    sides = [test.left, test.comparisons[0].comparator]
    return any(isinstance(x, cst.Name) and x.value == "__name__" for x in sides) and any(
        _string_value(x) == "__main__" for x in sides
    )


def _target_names(target: cst.BaseExpression) -> list[str]:
    if isinstance(target, cst.Name):
        return [target.value]
    if isinstance(target, (cst.Tuple, cst.List)):
        return [n for element in target.elements for n in _target_names(element.value)]
    if isinstance(target, cst.StarredElement):
        return _target_names(target.value)
    return []


def _block_bindings(body: Sequence[cst.BaseStatement]) -> dict[str, tuple[str, cst.CSTNode]]:
    """Each name `body` binds (its compound statements' blocks included):
    what binds it -- "function", "class", "import", "assignment" -- and the
    statement."""
    names: dict[str, tuple[str, cst.CSTNode]] = {}
    for stmt in _block_statements(body):
        if isinstance(stmt, (cst.FunctionDef, cst.ClassDef)):
            names.setdefault(stmt.name.value, ("function" if isinstance(stmt, cst.FunctionDef) else "class", stmt))
        elif isinstance(stmt, cst.For):
            for bound in _target_names(stmt.target):
                names.setdefault(bound, ("assignment", stmt))
        elif isinstance(stmt, cst.With):
            for item in stmt.items:
                if item.asname is not None:
                    for bound in _target_names(item.asname.name):
                        names.setdefault(bound, ("assignment", stmt))
        elif isinstance(stmt, cst.SimpleStatementLine):
            for small in stmt.body:
                if isinstance(small, (cst.Import, cst.ImportFrom)) and not isinstance(small.names, cst.ImportStar):
                    for alias in small.names:
                        bound_name = alias.asname.name if alias.asname else alias.name
                        head: cst.BaseExpression = bound_name
                        if isinstance(small, cst.Import):
                            while isinstance(head, cst.Attribute):
                                head = head.value
                        if isinstance(head, cst.Name):
                            names.setdefault(head.value, ("import", stmt))
                elif isinstance(small, cst.Assign):
                    for target in small.targets:
                        for bound in _target_names(target.target):
                            names.setdefault(bound, ("assignment", stmt))
                elif isinstance(small, (cst.AnnAssign, cst.AugAssign)):
                    for bound in _target_names(small.target):
                        names.setdefault(bound, ("assignment", stmt))
    return names


_EXTERNAL_EXPORTS: dict[str, frozenset[str] | None] = {}


def _external_exports(module: str) -> frozenset[str] | None:
    """The names `from module import *` binds, for a module outside the
    project: from the module itself when it is already imported (wfpy), else
    read from its source without running it -- its `__all__`, or its public
    top-level names. None when it cannot be read."""
    if module in _EXTERNAL_EXPORTS:
        return _EXTERNAL_EXPORTS[module]
    found: frozenset[str] | None = None
    loaded = sys.modules.get(module)
    if loaded is not None:
        names = getattr(loaded, "__all__", None)
        found = frozenset(str(n) for n in names) if names is not None else frozenset(
            n for n in vars(loaded) if not n.startswith("_")
        )
    else:
        source = _external_module_source(module)
        if source is not None:
            found = _static_exports(source)
    _EXTERNAL_EXPORTS[module] = found
    return found


def _external_module_source(module: str) -> Path | None:
    """The `.py` file of an installed module, found the way the import
    system finds it, without importing it or its packages."""
    import importlib.machinery

    parts = [part for part in module.split(".") if part]
    if not parts:
        return None
    search: list[str] | None = None
    for position, part in enumerate(parts):
        spec = importlib.machinery.PathFinder.find_spec(part, search)
        if spec is None:
            return None
        if position == len(parts) - 1:
            origin = spec.origin
            return Path(origin) if origin and origin.endswith(".py") else None
        locations = spec.submodule_search_locations
        if not locations:
            return None
        search = list(locations)
    return None


def _static_exports(source: Path) -> frozenset[str] | None:
    try:
        tree = cst.parse_module(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, cst.ParserSyntaxError):
        return None
    for stmt in _block_statements(tree.body):
        if isinstance(stmt, cst.SimpleStatementLine):
            for small in stmt.body:
                if (
                    isinstance(small, cst.Assign)
                    and any(isinstance(t.target, cst.Name) and t.target.value == "__all__" for t in small.targets)
                    and isinstance(small.value, (cst.List, cst.Tuple))
                ):
                    values = [_string_value(element.value) for element in small.value.elements]
                    if all(value is not None for value in values):
                        return frozenset(str(value) for value in values)
                    return None
    if any(
        isinstance(small, cst.ImportFrom) and isinstance(small.names, cst.ImportStar)
        for stmt in _block_statements(tree.body)
        if isinstance(stmt, cst.SimpleStatementLine)
        for small in stmt.body
    ):
        return None  # its own star import is not followed
    return frozenset(name for name in _block_bindings(tree.body) if not name.startswith("_"))


def _port_name_strings(node: cst.CSTNode, name: str, everywhere: bool = False) -> list[cst.SimpleString]:
    """The strings `"name"` under `node` used as a port's name: the second
    argument of a `getattr`, a dict's key, a subscript's index, an item of
    what a `for` loops over. With `everywhere`, `node` is an expression
    that is itself a port's name, and each string in it counts but a
    keyword's value. A keyword's value elsewhere (`direction="out"`) or a
    docstring is not a port's name."""
    found: list[cst.SimpleString] = []

    def keep(expr: cst.CSTNode | None) -> None:
        if isinstance(expr, cst.SimpleString) and _string_value(expr) == name:
            found.append(expr)

    def items(expr: cst.BaseExpression) -> None:
        if isinstance(expr, (cst.Tuple, cst.List, cst.Set)):
            for element in expr.elements:
                keep(element.value)
        else:
            keep(expr)

    class _Find(cst.CSTVisitor):
        def visit_Arg(self, arg: cst.Arg) -> bool:
            # In an expression that is a port's name, a keyword's value
            # (`make(path="x")`) names something else.
            return arg.keyword is None or not everywhere

        def visit_SimpleString(self, string: cst.SimpleString) -> None:
            if everywhere:
                keep(string)

        def visit_Call(self, call: cst.Call) -> None:
            if not everywhere and isinstance(call.func, cst.Name) and call.func.value == "getattr" and len(call.args) >= 2:
                keep(call.args[1].value)

        def visit_DictElement(self, element: cst.DictElement) -> None:
            if not everywhere:
                keep(element.key)

        def visit_SubscriptElement(self, element: cst.SubscriptElement) -> None:
            if not everywhere and isinstance(element.slice, cst.Index):
                keep(element.slice.value)

        def visit_For(self, loop: cst.For) -> None:
            if not everywhere:
                items(loop.iter)

    node.visit(_Find())
    unique: list[cst.SimpleString] = []
    for string in found:
        if not any(string is seen for seen in unique):
            unique.append(string)
    return unique


class _ReplaceNode(cst.CSTTransformer):
    """Puts `replacement` where `target` (that very node) is."""

    def __init__(self, target: cst.CSTNode, replacement: cst.CSTNode) -> None:
        super().__init__()
        self._target = target
        self._replacement = replacement
        self.done = False

    def on_visit(self, node: cst.CSTNode) -> bool:
        return not self.done

    def on_leave(self, original_node: cst.CSTNode, updated_node: cst.CSTNode) -> Any:
        if original_node is self._target:
            self.done = True
            return self._replacement
        return updated_node


class _WorkflowBodyTransformer(cst.CSTTransformer):
    """A transformer run on one workflow def: it edits that def's own body
    and never a function or class defined inside it (a helper's locals are
    not the workflow's nodes)."""

    def __init__(self, root: cst.FunctionDef) -> None:
        super().__init__()
        self._root = root

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        return node is self._root

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        return False


def _source_revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_port_value(value: cst.BaseExpression) -> dict[str, str] | None:
    """Extract `{direction, type}` from a `Port[<type>](direction="…")` expression."""
    call = value if isinstance(value, cst.Call) else None
    subscript = call.func if call and isinstance(call.func, cst.Subscript) else (
        value if isinstance(value, cst.Subscript) else None
    )
    if subscript is None:
        return None
    port_type = ""
    if subscript.slice:
        sl = subscript.slice[0].slice
        if isinstance(sl, cst.Index):
            port_type = cst.Module([]).code_for_node(sl.value).strip()
    direction = "in"
    if call is not None:
        for arg in call.args:
            if arg.keyword is not None and arg.keyword.value == "direction":
                raw = cst.Module([]).code_for_node(arg.value).strip().strip("\"'")
                direction = raw or "in"
    return {"direction": direction, "type": port_type}


def _graph_port_id(endpoint: dict[str, Any]) -> str:
    """Canonical port id for a graph edge endpoint (contract v2)."""
    if endpoint.get("kind") == "node":
        return f"n:{endpoint.get('nodeId', '')}:p:{endpoint.get('port', '')}"
    return f"wf:{endpoint.get('port', '')}"


def _with_scope_id(stmt: cst.With, parent: str) -> str:
    """Scope id for a wfpy control `with` block, nested under `parent`:
    `with ctrl.then:` → `<parent>/ctrl:then` (`else_` → `:else`); `with loop:` →
    `<parent>/loop:body`. Returns `parent` if the item shape isn't recognized."""
    for item in stmt.items:
        target = item.item
        if isinstance(target, cst.Attribute) and isinstance(target.value, cst.Name):
            attr = target.attr.value
            branch = "then" if attr == "then" else ("else" if attr == "else_" else "body")
            seg = f"{target.value.value}:{branch}"
        elif isinstance(target, cst.Name):
            seg = f"{target.value}:body"
        else:
            continue
        return f"scope:{seg}" if parent == "scope:root" else f"{parent}/{seg}"
    return parent


def _augment_graph_ids(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    """Attach canonical stable ids (`n:<name>`, `n:<name>:p:<port>`, `e:…`) to the
    graph in place. Purely additive — existing keys (`id`, `from`, `to`, …) are kept
    so current consumers and tests are unaffected."""
    for node in nodes:
        node_name = str(node.get("id", ""))
        if node_name and "nodeId" not in node:
            node["nodeId"] = f"n:{node_name}"
    for edge in edges:
        src = edge.get("from") if isinstance(edge.get("from"), dict) else None
        dst = edge.get("to") if isinstance(edge.get("to"), dict) else None
        if src is None or dst is None:
            continue
        src_pid = _graph_port_id(src)
        dst_pid = _graph_port_id(dst)
        src["portId"] = src_pid
        dst["portId"] = dst_pid
        edge["edgeId"] = f"e:{src_pid}->{dst_pid}"


def _atomic_write_if_unchanged(file_path: str, *, expected_revision: str, text: str) -> None:
    path = Path(file_path)
    current_text = path.read_text(encoding="utf-8")
    current_revision = _source_revision(current_text)
    if current_revision != expected_revision:
        raise RewriteError(
            message="source changed during rewrite",
            diagnostic={
                "code": "concurrent_source_modification",
                "file": file_path,
                "expectedRevision": expected_revision,
                "actualRevision": current_revision,
            },
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        handle.write(text)
        temp_name = handle.name
    Path(temp_name).replace(path)


def _staged_copy(file_path: str, text: str) -> str:
    """`text` written to a new file beside `file_path`, to be put in its place."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        handle.write(text)
    try:
        Path(handle.name).chmod(path.stat().st_mode & 0o7777)  # keep the file's own mode
    except OSError:
        pass
    return handle.name


def load_module(file_path: str) -> tuple[cst.Module, str, str]:
    raw_source = open(file_path, "r", encoding="utf-8").read()
    issues = detect_dynamic_workflow_identity_issues(raw_source)
    if issues:
        raise RewriteError(
            message=format_dynamic_workflow_identity_error(file_path, issues),
            diagnostic={
                "file": file_path,
                "issues": [dataclasses.asdict(issue) for issue in issues],
            },
        )

    source_text = cst.parse_module(raw_source).code
    revision = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    module = cst.parse_module(source_text)
    return module, source_text, revision


# The task kinds the sidecar can create and list. One list, because these are
# two hand-maintained sets that had already drifted: `streamblocks` was
# creatable but not listable, so the picker could not offer a type it had
# written itself. Each name is also the decorator's name, which is what lets
# one matcher serve every kind.
TASK_TYPE_KINDS = ("task", "tool", "agent", "viewer", "streamblocks", "source")

# The IDE's names of a port's direction → what `Port(direction=...)` takes.
_PORT_DIRECTIONS = {
    "input": "in",
    "output": "out",
    "inout": "inout",
    "in": "in",
    "out": "out",
}


@dataclasses.dataclass
class RewriteError(RuntimeError):
    message: str
    diagnostic: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


@dataclasses.dataclass
class _Use:
    """A workflow using a class or a workflow whose port an edit follows:
    the engine of its file, its name, its instances of the edited type, and
    which `@workflow def` of the name it is (None: every one of the name)."""

    engine: "RewriteEngine"
    workflow: str
    instances: list[str]
    ordinal: int | None = None


class RewriteEngine:
    def __init__(self, file_path: str, module: cst.Module, source_text: str) -> None:
        self.file_path = file_path
        self.module = module
        self.source_text = source_text
        self._updated = False
        self.revision = hashlib.sha256(source_text.encode("utf-8")).hexdigest()

    def save(self) -> None:
        """Write this file and every other the op edited, all or none.

        Each is checked unchanged on disk before any is written, and each
        is written to a file beside it first; only then are they put in
        place. Should putting one in place fail, those already in place get
        their old text back, and the refusal names any it could not restore
        (`changedFiles`), for the host to undo."""
        engines = ([self] if self._updated else []) + [e for e in self.changed_companions() if e is not self]
        for engine in engines:
            on_disk = _source_revision(Path(engine.file_path).read_text(encoding="utf-8"))
            if on_disk != engine.revision:
                raise RewriteError(
                    message="source changed during rewrite",
                    diagnostic={
                        "code": "concurrent_source_modification",
                        "file": engine.file_path,
                        "expectedRevision": engine.revision,
                        "actualRevision": on_disk,
                    },
                )
        staged: list[tuple[RewriteEngine, str, str]] = []
        try:
            for engine in engines:
                text = engine.module.code
                staged.append((engine, _staged_copy(engine.file_path, text), text))
        except OSError as exc:
            for _engine, temp, _text in staged:
                Path(temp).unlink(missing_ok=True)
            raise RewriteError(
                message=f"the edit could not be written: {exc}",
                diagnostic={"code": "write_failed", "file": self.file_path},
            ) from None
        placed: list[RewriteEngine] = []
        for position, (engine, temp, _text) in enumerate(staged):
            try:
                Path(temp).replace(engine.file_path)
            except OSError as exc:
                for _later, later_temp, _later_text in staged[position:]:
                    Path(later_temp).unlink(missing_ok=True)
                unrestored = []
                for done in placed:
                    try:
                        Path(_staged_copy(done.file_path, done.source_text)).replace(done.file_path)
                    except OSError:
                        unrestored.append(
                            {"file": done.file_path, "revision": _source_revision(done.module.code)}
                        )
                raise RewriteError(
                    message=f"{Path(engine.file_path).name} could not be written: {exc}",
                    diagnostic={
                        "code": "write_failed",
                        "file": engine.file_path,
                        "changedFiles": unrestored,
                    },
                ) from None
            placed.append(engine)
        for engine, _temp, text in staged:
            engine.source_text = text
            engine.revision = _source_revision(text)

    def _set_module(self, module: cst.Module) -> None:
        self.module = module
        self._updated = True

    def _resolve_module_file_path(self, module_name: str) -> str:
        """Resolve a Python module to a local file path.

        Relative resolution is based on ``self.file_path`` parent package.
        Supports both module files and package ``__init__.py``.
        """

        if not module_name.strip():
            raise RewriteError(message="Module name is empty")

        module_parts = [part for part in module_name.split(".") if part]
        if not module_parts:
            raise RewriteError(message=f"Invalid module name: {module_name}")

        search_root = Path(self.file_path).resolve().parent
        while True:
            candidate = search_root.joinpath(*module_parts)
            py_file = candidate.with_suffix(".py")
            init_file = candidate / "__init__.py"
            if py_file.is_file():
                return str(py_file)
            if init_file.is_file():
                return str(init_file)
            if search_root.parent == search_root:
                break
            search_root = search_root.parent

        raise RewriteError(
            message=f"Cannot resolve module path for '{module_name}'",
            diagnostic={
                "file": self.file_path,
                "module": module_name,
            },
        )

    def _resolve_nested_workflow_candidates(self, workflow_type: str) -> list[str]:
        """Return candidate module files that may define imported workflow_type."""

        candidates: list[str] = []
        seen: set[str] = set()

        for stmt in self.module.body:
            if not isinstance(stmt, cst.SimpleStatementLine):
                continue
            for simple in stmt.body:
                if not isinstance(simple, cst.ImportFrom):
                    continue
                if simple.relative:
                    continue
                module_expr = simple.module
                if module_expr is None:
                    continue
                module_name = _expr_to_code(module_expr)
                if not module_name:
                    continue

                if isinstance(simple.names, cst.ImportStar):
                    try:
                        module_file = self._resolve_module_file_path(module_name)
                    except RewriteError:
                        continue
                    if module_file not in seen:
                        seen.add(module_file)
                        candidates.append(module_file)
                    continue

                imported_items: list[tuple[str, str]] = []
                for alias in simple.names:
                    if not isinstance(alias, cst.ImportAlias):
                        continue
                    imported_name = _expr_to_code(alias.name)
                    as_name = alias.asname.name.value if alias.asname and isinstance(alias.asname.name, cst.Name) else imported_name
                    if imported_name and as_name:
                        imported_items.append((imported_name, as_name))

                matching = [item for item in imported_items if item[1] == workflow_type]
                if not matching:
                    continue

                imported_name = matching[0][0]
                try:
                    module_file = self._resolve_module_file_path(module_name)
                except RewriteError:
                    continue
                if module_file not in seen:
                    seen.add(module_file)
                    candidates.append(module_file)

                if "." not in module_name:
                    parent_name = Path(self.file_path).resolve().parent.name
                    if parent_name:
                        qualified_module = f"{parent_name}.{module_name}"
                        try:
                            qualified_file = self._resolve_module_file_path(qualified_module)
                        except RewriteError:
                            qualified_file = ""
                        if qualified_file and qualified_file not in seen:
                            seen.add(qualified_file)
                            candidates.append(qualified_file)

                del imported_name

        return candidates

    def _resolve_nested_workflow_graph(
        self,
        workflow_type: str,
        visited_workflows: set[str] | None = None,
    ) -> dict[str, Any]:
        """Resolve a workflow declared in another module and export its graph recursively."""

        visited = visited_workflows if visited_workflows is not None else set()

        module_files = self._resolve_nested_workflow_candidates(workflow_type)
        for module_file in module_files:
            nested_module, nested_source, _nested_rev = load_module(module_file)
            nested_engine = RewriteEngine(module_file, nested_module, nested_source)
            if workflow_type in visited:
                return {
                    "workflow": workflow_type,
                    "nodes": [],
                    "edges": [],
                    "children": [],
                    "file": module_file,
                    "recursiveRef": True,
                }
            next_visited = set(visited)
            next_visited.add(workflow_type)
            try:
                return nested_engine.export_workflow_graph(
                    workflow=workflow_type,
                    include_nested=True,
                    _visited_workflows=next_visited,
                )
            except RewriteError:
                continue

        raise RewriteError(
            message=f"Nested workflow '{workflow_type}' is not imported in this module",
            diagnostic={
                "file": self.file_path,
                "workflow": workflow_type,
            },
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _first_non_docstring_index(self, body: list[cst.BaseStatement]) -> int:
        if not body:
            return 0
        first = body[0]
        if isinstance(first, cst.SimpleStatementLine) and len(first.body) == 1:
            expr = first.body[0]
            if m.matches(expr, m.Expr(value=m.SimpleString())):
                return 1
        return 0

    def _find_insert_index(self, body: list[cst.BaseStatement], kind: str) -> int:
        first_idx = self._first_non_docstring_index(body)
        last_assign = -1
        last_connect = -1
        for idx, stmt in enumerate(body):
            if isinstance(stmt, cst.SimpleStatementLine):
                for s in stmt.body:
                    if m.matches(s, m.Assign()):
                        last_assign = idx
                    if m.matches(s, m.Expr(value=m.Call(func=m.Name("connect")))):
                        last_connect = idx
        if kind == "connect":
            if last_connect >= 0:
                return last_connect + 1
            if last_assign >= 0:
                return last_assign + 1
            return first_idx
        if last_assign >= 0:
            return last_assign + 1
        return first_idx

    def _strip_placeholder_pass(self, body: list[cst.BaseStatement]) -> list[cst.BaseStatement]:
        if len(body) <= 1:
            return body
        cleaned: list[cst.BaseStatement] = []
        removed = False
        for stmt in body:
            if (
                not removed
                and isinstance(stmt, cst.SimpleStatementLine)
                and len(stmt.body) == 1
                and isinstance(stmt.body[0], cst.Pass)
            ):
                removed = True
                continue
            cleaned.append(stmt)
        return cleaned if removed else body

    def _insert_statement(
        self,
        body: list[cst.BaseStatement],
        stmt: cst.BaseStatement,
        kind: str,
    ) -> list[cst.BaseStatement]:
        idx = self._find_insert_index(body, kind)
        updated = body[:idx] + [stmt] + body[idx:]
        return self._strip_placeholder_pass(updated)

    def _is_workflow_def(self, fn: cst.FunctionDef) -> bool:
        for dec in fn.decorators:
            target = dec.decorator
            if m.matches(target, m.Name("workflow")):
                return True
            if m.matches(target, m.Attribute(value=m.Name("wfpy"), attr=m.Name("workflow"))):
                return True
            if m.matches(target, m.Call(func=m.Name("workflow"))):
                return True
            if m.matches(target, m.Call(func=m.Attribute(value=m.Name("wfpy"), attr=m.Name("workflow")))):
                return True
        return False

    def _find_workflow_def(
        self,
        workflow_name: str | None,
    ) -> tuple[cst.FunctionDef, cst.IndentedBlock, list[cst.FunctionDef]]:
        if workflow_name is None:
            fn, body = self._get_workflow_body(None)
            return fn, body, []

        def visit_block(
            block: Sequence[cst.BaseStatement],
            parents: list[cst.FunctionDef],
        ) -> tuple[cst.FunctionDef, cst.IndentedBlock, list[cst.FunctionDef]] | None:
            for stmt in block:
                if not isinstance(stmt, cst.FunctionDef):
                    continue
                next_parents = parents + [stmt]
                if stmt.name.value == workflow_name and self._is_workflow_def(stmt):
                    body = stmt.body
                    if isinstance(body, cst.IndentedBlock):
                        return stmt, body, parents
                body = stmt.body
                if isinstance(body, cst.IndentedBlock):
                    nested = visit_block(body.body, next_parents)
                    if nested is not None:
                        return nested
            return None

        resolved = visit_block(self.module.body, [])
        if resolved is not None:
            return resolved

        raise RewriteError(
            message="Workflow function not found",
            diagnostic={"file": self.file_path, "workflow": workflow_name},
        )

    def _helper_default_parameters(self, fn: cst.FunctionDef) -> list[dict[str, Any]]:
        parameters: list[dict[str, Any]] = []
        positional_params = [*fn.params.posonly_params, *fn.params.params]
        for param in [*positional_params, *fn.params.kwonly_params]:
            if param.default is None:
                continue
            parameters.append(
                {
                    "name": param.name.value,
                    "value": self._resolve_expr_code(param.default),
                }
            )
        return parameters

    def _nested_workflow_defs(self, workflow_name: str) -> list[cst.FunctionDef]:
        """Every `@workflow def <workflow_name>` inside a function: what a
        factory (`def make(...): @workflow def name(): ...; return name`)
        builds, in source order."""
        found: list[cst.FunctionDef] = []

        def visit(block: Sequence[cst.BaseStatement], depth: int) -> None:
            for stmt in block:
                if isinstance(stmt, cst.FunctionDef):
                    if depth > 0 and stmt.name.value == workflow_name and self._is_workflow_def(stmt):
                        found.append(stmt)
                    if isinstance(stmt.body, cst.IndentedBlock):
                        visit(stmt.body.body, depth + 1)
                    continue
                for child in self._child_blocks(stmt):
                    visit(child.body, depth)

        visit(self.module.body, 0)
        return found

    def _resolve_workflow_def(self, workflow_name: str | None) -> cst.FunctionDef:
        """The def an op addressed to workflow `workflow_name` edits.

        A module-level `@workflow def` of that name first; else the
        `@workflow def` of that name a factory defines (the module-level
        function of the same name is then the factory, whose own body holds
        no nodes); else, as before, a module-level function of that name.
        With no name, the first module-level `@workflow def`.
        """
        top_level = [stmt for stmt in self.module.body if isinstance(stmt, cst.FunctionDef)]
        if workflow_name is None:
            for fn in top_level:
                if self._is_workflow_def(fn):
                    return fn
            if top_level:
                return top_level[0]
        else:
            for fn in top_level:
                if fn.name.value == workflow_name and self._is_workflow_def(fn):
                    return fn
            nested = self._nested_workflow_defs(workflow_name)
            if nested:
                return nested[0]
            for fn in top_level:
                if fn.name.value == workflow_name:
                    return fn
        raise RewriteError(
            message="Workflow function not found",
            diagnostic={"file": self.file_path, **({"workflow": workflow_name} if workflow_name else {})},
        )

    def _get_workflow_body(self, workflow_name: str | None) -> tuple[cst.FunctionDef, cst.IndentedBlock]:
        fn = self._resolve_workflow_def(workflow_name)
        body = fn.body
        if isinstance(body, cst.IndentedBlock):
            return fn, body
        raise RewriteError(
            message="Workflow function body not found",
            diagnostic={"file": self.file_path},
        )

    def _replace_node(self, target: cst.CSTNode, replacement: cst.CSTNode) -> None:
        """Put `replacement` where `target`, a node of this module, is."""
        if target is replacement:
            return
        replacer = _ReplaceNode(target, replacement)
        updated = self.module.visit(replacer)
        if not replacer.done:
            raise RewriteError(
                message="internal: the edited definition is no longer in the module",
                diagnostic={"file": self.file_path, "code": "internal_error"},
            )
        self._set_module(updated)

    def _edit_workflow(self, fn: cst.FunctionDef, transformer: cst.CSTTransformer) -> None:
        """Run `transformer` on workflow def `fn` (that node) and put the result back."""
        updated = fn.visit(transformer)
        if not isinstance(updated, cst.FunctionDef):
            raise RewriteError(message="internal: a workflow edit removed the workflow")
        self._replace_node(fn, updated)

    def _positions_of(self, node: cst.CSTNode) -> Any:
        """Code range of `node` (a node of this module), or None."""
        positions = self._source_positions()
        return positions.get(node) if positions else None

    def _line_of(self, node: cst.CSTNode) -> int | None:
        found = self._positions_of(node)
        return int(found.start.line) if found is not None else None

    def _get_local_function(self, function_name: str) -> cst.FunctionDef | None:
        for stmt in self.module.body:
            if isinstance(stmt, cst.FunctionDef) and stmt.name.value == function_name:
                return stmt
        return None

    def _resolve_expr_code(
        self,
        expr: cst.BaseExpression,
        bindings: dict[str, str] | None = None,
    ) -> str:
        if bindings and isinstance(expr, cst.Name) and expr.value in bindings:
            return bindings[expr.value]
        return _expr_to_code(expr)

    def _build_call_bindings(
        self,
        fn: cst.FunctionDef,
        call: cst.Call,
        outer_bindings: dict[str, str] | None = None,
    ) -> dict[str, str]:
        bindings: dict[str, str] = {}
        positional_params = [*fn.params.posonly_params, *fn.params.params]
        positional_args = [arg for arg in call.args if arg.keyword is None]
        keyword_args = {
            _expr_to_code(arg.keyword): self._resolve_expr_code(arg.value, outer_bindings)
            for arg in call.args
            if arg.keyword is not None
        }

        positional_index = 0
        for param in positional_params:
            name = param.name.value
            if positional_index < len(positional_args):
                bindings[name] = self._resolve_expr_code(positional_args[positional_index].value, outer_bindings)
                positional_index += 1
            elif name in keyword_args:
                bindings[name] = keyword_args[name]
            elif param.default is not None:
                bindings[name] = self._resolve_expr_code(param.default, outer_bindings)

        for param in fn.params.kwonly_params:
            name = param.name.value
            if name in keyword_args:
                bindings[name] = keyword_args[name]
            elif param.default is not None:
                bindings[name] = self._resolve_expr_code(param.default, outer_bindings)

        return bindings

    def _get_same_file_helper_call(self, call: cst.Call) -> tuple[cst.FunctionDef, dict[str, str]] | None:
        if not isinstance(call.func, cst.Name):
            return None

        helper_fn = self._get_local_function(call.func.value)
        if helper_fn is None or self._is_workflow_def(helper_fn):
            return None

        if not isinstance(helper_fn.body, cst.IndentedBlock):
            return None

        return helper_fn, self._build_call_bindings(helper_fn, call)

    def _collect_static_graph_items(
        self,
        body: cst.IndentedBlock,
        *,
        bindings: dict[str, str] | None = None,
        visited_helpers: set[str] | None = None,
        positions: Any | None = None,
        scope: str = "scope:root",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        instance_to_type: dict[str, str] = {}
        active_helpers = set(visited_helpers or set())

        def _loc(node: cst.CSTNode) -> dict[str, Any] | None:
            # Best-effort source location from libcst PositionProvider metadata.
            if not positions:
                return None
            cr = positions.get(node)
            if cr is None:
                return None
            return {"file": self.file_path, "line": cr.start.line, "column": cr.start.column + 1}

        for stmt in body.body:
            # Recurse into wfpy control-flow scopes (`with ctrl.then:` / `.else_:` /
            # `with loop:`), tagging the nodes/edges inside with the nested scope id.
            if isinstance(stmt, cst.With) and isinstance(stmt.body, cst.IndentedBlock):
                child_scope = _with_scope_id(stmt, scope)
                nested_nodes, nested_edges, nested_instances = self._collect_static_graph_items(
                    stmt.body,
                    bindings=bindings,
                    visited_helpers=active_helpers,
                    positions=positions,
                    scope=child_scope,
                )
                nodes.extend(nested_nodes)
                edges.extend(nested_edges)
                instance_to_type.update(nested_instances)
                continue
            if not isinstance(stmt, cst.SimpleStatementLine):
                continue
            for simple in stmt.body:
                if isinstance(simple, cst.Assign) and isinstance(simple.value, cst.Call):
                    helper_call = self._get_same_file_helper_call(simple.value)
                    if helper_call is not None:
                        helper_fn, helper_bindings = helper_call
                        helper_name = helper_fn.name.value
                        if helper_name not in active_helpers and isinstance(helper_fn.body, cst.IndentedBlock):
                            nested_nodes, nested_edges, nested_instances = self._collect_static_graph_items(
                                helper_fn.body,
                                bindings=helper_bindings,
                                visited_helpers={*active_helpers, helper_name},
                                positions=positions,
                                scope=scope,
                            )
                            nodes.extend(nested_nodes)
                            edges.extend(nested_edges)
                            instance_to_type.update(nested_instances)
                        continue

                    if len(simple.targets) != 1:
                        continue

                    target = simple.targets[0].target
                    if isinstance(target, cst.Name):
                        call = simple.value
                        callee = self._resolve_expr_code(call.func, bindings)
                        loc = _loc(simple)
                        node = {
                            "id": target.value,
                            "ref": callee,
                            "kind": "task",
                            "args": [self._resolve_expr_code(arg.value, bindings) for arg in call.args if arg.keyword is None],
                            "kwargs": {
                                _expr_to_code(arg.keyword): self._resolve_expr_code(arg.value, bindings)
                                for arg in call.args if arg.keyword is not None
                            },
                            "scope": scope,
                            **({"location": loc} if loc else {}),
                        }
                        instance_to_type[target.value] = callee
                        nodes.append(node)
                        continue

                if isinstance(simple, cst.Expr) and isinstance(simple.value, cst.Call):
                    helper_call = self._get_same_file_helper_call(simple.value)
                    if helper_call is not None:
                        helper_fn, helper_bindings = helper_call
                        helper_name = helper_fn.name.value
                        if helper_name not in active_helpers and isinstance(helper_fn.body, cst.IndentedBlock):
                            nested_nodes, nested_edges, nested_instances = self._collect_static_graph_items(
                                helper_fn.body,
                                bindings=helper_bindings,
                                visited_helpers={*active_helpers, helper_name},
                                positions=positions,
                                scope=scope,
                            )
                            nodes.extend(nested_nodes)
                            edges.extend(nested_edges)
                            instance_to_type.update(nested_instances)
                        continue

                    call = simple.value
                    if self._resolve_expr_code(call.func, bindings) != "connect" or len(call.args) != 2:
                        continue
                    left = self._resolve_expr_code(call.args[0].value, bindings)
                    right = self._resolve_expr_code(call.args[1].value, bindings)
                    if not left or not right:
                        continue

                    def parse_endpoint(expr: str) -> dict[str, Any]:
                        if "." in expr:
                            node_id, port = expr.split(".", 1)
                            return {
                                "kind": "node",
                                "nodeId": node_id,
                                "port": port,
                                "ref": instance_to_type.get(node_id, "")
                            }
                        raw = expr.strip()
                        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
                            raw = raw[1:-1]
                        return {
                            "kind": "workflow",
                            "port": raw
                        }

                    edge_loc = _loc(simple)
                    edges.append({
                        "id": f"e{len(edges) + 1}",
                        "from": parse_endpoint(left),
                        "to": parse_endpoint(right),
                        "scope": scope,
                        **({"location": edge_loc} if edge_loc else {}),
                    })

        return nodes, edges, instance_to_type

    def _matches_scope_with(self, stmt: cst.With, scope_control: str, scope_branch: str | None) -> bool:
        for item in stmt.items:
            target = item.item
            if isinstance(target, cst.Attribute) and isinstance(target.value, cst.Name):
                if target.value.value != scope_control:
                    continue
                if scope_branch == "then" and target.attr.value == "then":
                    return True
                if scope_branch == "else" and target.attr.value == "else_":
                    return True
            if isinstance(target, cst.Name):
                if target.value == scope_control and scope_branch in (None, "body"):
                    return True
        return False

    def _child_blocks(self, stmt: cst.BaseStatement) -> Iterable[cst.IndentedBlock]:
        if isinstance(stmt, cst.With):
            if isinstance(stmt.body, cst.IndentedBlock):
                yield stmt.body
            return
        if isinstance(stmt, (cst.If, cst.For, cst.While, cst.Try)):
            if isinstance(stmt.body, cst.IndentedBlock):
                yield stmt.body
            if isinstance(stmt, cst.If) and isinstance(stmt.orelse, cst.Else):
                if isinstance(stmt.orelse.body, cst.IndentedBlock):
                    yield stmt.orelse.body
            if isinstance(stmt, (cst.For, cst.While)) and isinstance(stmt.orelse, cst.Else):
                if isinstance(stmt.orelse.body, cst.IndentedBlock):
                    yield stmt.orelse.body
            if isinstance(stmt, cst.Try):
                for handler in stmt.handlers:
                    if isinstance(handler.body, cst.IndentedBlock):
                        yield handler.body
                if isinstance(stmt.orelse, cst.Else) and isinstance(stmt.orelse.body, cst.IndentedBlock):
                    yield stmt.orelse.body

    def _find_scope_block(
        self,
        body: cst.IndentedBlock,
        scope_control: str | None,
        scope_branch: str | None,
    ) -> cst.IndentedBlock:
        if not scope_control:
            return body
        for stmt in body.body:
            if isinstance(stmt, cst.With) and self._matches_scope_with(stmt, scope_control, scope_branch):
                if isinstance(stmt.body, cst.IndentedBlock):
                    return stmt.body
            for child in self._child_blocks(stmt):
                try:
                    return self._find_scope_block(child, scope_control, scope_branch)
                except RewriteError:
                    continue
        raise RewriteError(
            message="Scope not found",
            diagnostic={"file": self.file_path, "scope": scope_control},
        )

    def _matches_control_with_any(self, stmt: cst.With, name: str) -> bool:
        for item in stmt.items:
            target = item.item
            if isinstance(target, cst.Attribute) and isinstance(target.value, cst.Name):
                if target.value.value == name:
                    return True
            if isinstance(target, cst.Name) and target.value == name:
                return True
        return False

    def _update_scope_body(
        self,
        fn: cst.FunctionDef,
        scope_control: str | None,
        scope_branch: str | None,
        new_block: cst.IndentedBlock,
    ) -> None:
        if not scope_control:
            self._update_function_body(fn, new_block)
            return

        class _ScopeRewriter(_WorkflowBodyTransformer):
            def __init__(self, matcher: RewriteEngine, control: str, branch: str | None, body: cst.IndentedBlock) -> None:
                super().__init__(fn)
                self._matcher = matcher
                self._control = control
                self._branch = branch
                self._body = body
                self.updated = False

            def leave_With(self, original_node: cst.With, updated_node: cst.With) -> cst.With:
                if self.updated:
                    return updated_node
                if self._matcher._matches_scope_with(original_node, self._control, self._branch):
                    self.updated = True
                    return updated_node.with_changes(body=self._body)
                return updated_node

        rewriter = _ScopeRewriter(self, scope_control, scope_branch, new_block)
        updated = fn.visit(rewriter)
        if not rewriter.updated or not isinstance(updated, cst.FunctionDef):
            raise RewriteError(
                message="Scope not found",
                diagnostic={"file": self.file_path, "scope": scope_control},
            )
        self._replace_node(fn, updated)

    def _collect_assigned_names(self, fn: cst.FunctionDef) -> set[str]:
        names: set[str] = set()

        class _Visitor(cst.CSTVisitor):
            def visit_Assign(self, node: cst.Assign) -> None:
                for target in node.targets:
                    if isinstance(target.target, cst.Name):
                        names.add(target.target.value)

            def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
                if isinstance(node.target, cst.Name):
                    names.add(node.target.value)

        fn.visit(_Visitor())
        return names

    def _unique_name(self, base: str, taken: set[str]) -> str:
        if base not in taken:
            return base
        idx = 2
        while f"{base}_{idx}" in taken:
            idx += 1
        return f"{base}_{idx}"

    def _update_function_body(self, fn: cst.FunctionDef, new_body: cst.IndentedBlock) -> None:
        """Give workflow def `fn` (that node, not every def of its name: a
        factory and the workflow it builds are often named alike) `new_body`."""
        self._replace_node(fn, fn.with_changes(body=new_body))

    def _port_expr_from_spec(self, spec: Any) -> str:
        if isinstance(spec, str):
            return spec
        if isinstance(spec, dict):
            kind = spec.get("type")
            if kind == "workflow":
                name = str(spec.get("name", ""))
                if not name:
                    raise RewriteError(message="workflow port requires name")
                return repr(name)
            if kind == "actor":
                actor = str(spec.get("actor", ""))
                port = str(spec.get("port", ""))
                if not actor or not port:
                    raise RewriteError(message="actor port requires actor and port")
                return f"{actor}.{port}"
            if kind == "control":
                control = str(spec.get("control", ""))
                port = str(spec.get("port", ""))
                if not control or not port:
                    raise RewriteError(message="control port requires control and port")
                return f"{control}.{port}"
        raise RewriteError(message="Invalid port spec")

    def _ensure_identifier(self, name: str) -> None:
        if not name.isidentifier():
            raise RewriteError(message=f"Invalid identifier: {name}")

    # ── Operations ───────────────────────────────────────────────────────

    def create_node(
        self,
        *,
        workflow: str | None = None,
        type: str,
        name: str,
        params: dict[str, Any] | None = None,
        expressions: dict[str, Any] | None = None,
        scope_control: str | None = None,
        scope_branch: str | None = None,
        importFrom: str | None = None,
    ) -> None:
        """`name = type(...)` in the workflow: `params` as literals of their
        values, then `expressions` as written (`text=FRONT_OPTIONS`), in the
        order given. A type this file does not bind is imported: from
        `importFrom` when given, else from where the project defines it."""
        self._ensure_identifier(name)
        type_name = str(type).strip()
        if not type_name.isidentifier() or keyword.iskeyword(type_name):
            raise RewriteError(message=f"Invalid type name: {type_name}", diagnostic={"code": "invalid_type"})
        fn, body = self._get_workflow_body(workflow)
        self._refuse_shadowing(fn, name, type_name)
        self._refuse_taken(fn, name)
        args: list[cst.Arg] = []
        written: set[str] = set()
        for key, value in (params or {}).items():
            if not str(key).isidentifier() or keyword.iskeyword(str(key)):
                raise RewriteError(message=f"Invalid parameter name: {key}", diagnostic={"code": "invalid_parameter_name"})
            args.append(_keyword_arg(str(key), cst.parse_expression(repr(value))))
            written.add(str(key))
        for key, text in (expressions or {}).items():
            if not str(key).isidentifier() or keyword.iskeyword(str(key)):
                raise RewriteError(message=f"Invalid parameter name: {key}", diagnostic={"code": "invalid_parameter_name"})
            if str(key) in written:
                raise RewriteError(
                    message=f"parameter '{key}' is given twice, as a value and as an expression",
                    diagnostic={"code": "invalid_parameter_name"},
                )
            value = _parse_user_expression(text, f"the value of {name}.{key}")
            self._refuse_unbound(value, fn, f"the value of {name}.{key}")
            args.append(_keyword_arg(str(key), value))
            written.add(str(key))
        # Where the type comes from is settled before anything is written.
        import_from = str(importFrom).strip() if importFrom else ""
        bound = self._binds(type_name, fn)
        if not bound and not import_from:
            import_from = self._project_module_of(type_name)
        target_block = self._find_scope_block(body, scope_control, scope_branch)
        target_body = list(target_block.body)
        assign = cst.Assign(
            targets=[cst.AssignTarget(target=cst.Name(name))],
            value=cst.Call(func=cst.Name(type_name), args=args),
        )
        stmt = cst.SimpleStatementLine([assign])
        new_stmt_body = self._insert_statement(target_body, stmt, "node")
        new_block = target_block.with_changes(body=new_stmt_body)
        self._update_scope_body(fn, scope_control, scope_branch, new_block)
        # A type defined in another module, the way a workflow instance's is,
        # needs its import; one this file already binds does not.
        if import_from and not bound:
            self._set_module(self._ensure_import(self.module, import_from, [type_name]))

    def _module_bindings(self) -> dict[str, tuple[str, cst.CSTNode]]:
        """Each name the top level of this file binds: what binds it (a
        definition, an import, an assignment) and the statement -- one under
        a top-level `try` / `if` (an optional import, `if TYPE_CHECKING:`)
        included."""
        return _block_bindings(self.module.body)

    def _module_level_names(self) -> set[str]:
        """Names a top-level definition, import or assignment binds in this file."""
        return set(self._module_bindings())

    def _star_imports(self) -> list[cst.ImportFrom]:
        return [
            small
            for stmt in _block_statements(self.module.body)
            if isinstance(stmt, cst.SimpleStatementLine)
            for small in stmt.body
            if isinstance(small, cst.ImportFrom) and isinstance(small.names, cst.ImportStar)
        ]

    def _has_star_import(self) -> bool:
        return bool(self._star_imports())

    def _index(self) -> "ProjectIndex":
        """The project this file is in, read once per op (a create_node asks
        it whether a star import binds the type, then where it is defined)."""
        index = self.__dict__.get("_project_index")
        if index is None:
            from wfpy._project_index import ProjectIndex

            index = ProjectIndex.for_file(self.file_path)
            self.__dict__["_project_index"] = index
        return index

    def _scope_bindings(self, fn: cst.FunctionDef) -> set[str]:
        """The names workflow `fn`'s body can read besides the module's: its
        own locals, and the parameters and locals of the functions it is
        nested in (a factory's `def make(Stage): @workflow def built(): ...`)."""
        names: set[str] = set(_block_bindings(fn.body.body)) if isinstance(fn.body, cst.IndentedBlock) else set()
        for outer in self._enclosing_functions(fn):
            params = outer.params
            every = [*params.posonly_params, *params.params, *params.kwonly_params]
            if isinstance(params.star_arg, cst.Param):
                every.append(params.star_arg)
            if isinstance(params.star_kwarg, cst.Param):
                every.append(params.star_kwarg)
            names.update(p.name.value for p in every)
            if isinstance(outer.body, cst.IndentedBlock):
                names.update(_block_bindings(outer.body.body))
        return names

    def _binds(self, name: str, fn: cst.FunctionDef | None = None) -> bool:
        """Whether `name` is in scope in this file: at its top level (a star
        import's names included), a builtin, or -- with `fn`, a workflow --
        in the functions `fn` is nested in or in `fn` itself."""
        import builtins

        if name in self._module_level_names() or hasattr(builtins, name):
            return True
        if fn is not None and name in self._scope_bindings(fn):
            return True
        for star in self._star_imports():
            if self._star_binds(star, name):
                return True
        return False

    def _refuse_unbound(self, expr: cst.BaseExpression, fn: cst.FunctionDef | None, what: str) -> None:
        """Refuse an expression that reads a name nothing binds where it is
        written (`fn`'s body, or the module's top level): the file would be
        written, and the workflow then fail as it is built (NameError)."""
        for name in _free_names(expr):
            if not self._binds(name, fn):
                where = f"workflow '{fn.name.value}'" if fn is not None else "this file"
                raise RewriteError(
                    message=f"{what} reads '{name}', which nothing in {where} binds: define or import it first",
                    diagnostic={"code": "name_not_bound", "name": name},
                )

    def _star_binds(self, star: cst.ImportFrom, name: str) -> bool:
        """Whether `from module import *` binds `name`: for a module of the
        project, by reading it; for one outside it, by its `__all__` or its
        public names, read without running it (wfpy's from wfpy itself). A
        module that cannot be read is taken to bind it, as before any of
        this was checked."""
        relative = len(star.relative)
        module = _dotted_name(star.module) if star.module is not None else ""
        index = self._index()
        source: Path | None
        if relative:
            base = Path(self.file_path).resolve().parent
            for _ in range(relative - 1):
                base = base.parent
            parts = [part for part in module.split(".") if part]
            candidate = base.joinpath(*parts) if parts else base
            source = candidate.with_suffix(".py") if parts and candidate.with_suffix(".py").is_file() else candidate / "__init__.py"
            if not source.is_file():
                return True
        else:
            source = index.module_file(module, near=Path(self.file_path))
        if source is not None:
            return index.binding(source, name) is not None
        exported = _external_exports(module)
        return exported is None or name in exported

    def _refuse_shadowing(self, fn: cst.FunctionDef, name: str, type_name: str) -> None:
        """Refuse an instance name the workflow's body would make local over
        something it reads: the type it instantiates (`av1_refdec =
        av1_refdec()` reads the local before it is bound), a name the module
        binds, or a parameter of the factory around the workflow."""
        what: str | None = None
        line: int | None = None
        if name == type_name:
            what = f"the type '{type_name}' it instantiates: `{name} = {type_name}()` would read the local '{name}' before it is bound"
        else:
            bound = self._module_bindings().get(name)
            if bound is not None:
                kind, stmt = bound
                line = self._line_of(stmt)
                what = {
                    "function": f"the function '{name}' of this file",
                    "class": f"the class '{name}' of this file",
                    "import": f"the imported '{name}'",
                    "assignment": f"the module-level '{name}'",
                }[kind]
                if line is not None:
                    what += f" (line {line})"
            else:
                for outer in self._enclosing_functions(fn):
                    params = outer.params
                    every = [*params.posonly_params, *params.params, *params.kwonly_params]
                    if isinstance(params.star_arg, cst.Param):
                        every.append(params.star_arg)
                    if isinstance(params.star_kwarg, cst.Param):
                        every.append(params.star_kwarg)
                    if any(p.name.value == name for p in every):
                        what = f"the parameter '{name}' of '{outer.name.value}', which the workflow reads"
                        break
        if what is not None:
            raise RewriteError(
                message=f"a node named '{name}' would hide {what}: choose another name",
                diagnostic={"code": "name_shadows", "name": name, **({"line": line} if line is not None else {})},
            )

    def _refuse_taken(self, fn: cst.FunctionDef, name: str) -> None:
        """Refuse a node name workflow `fn` already binds: another instance,
        a loop variable, a parameter. A second `x = ...` in one body leaves
        one node where the diagram drew two, or rebinds what a `connect`
        reads."""
        bound = self._names_bound_in(fn)
        if name in bound:
            line = bound[name]
            raise RewriteError(
                message=f"'{name}' is already a name in this workflow"
                + (f" (line {line})" if line is not None else "")
                + ": choose another name",
                diagnostic={"code": "name_exists", "name": name, **({"line": line} if line is not None else {})},
            )

    def _names_bound_in(self, fn: cst.FunctionDef) -> dict[str, int | None]:
        """The names workflow `fn` binds itself, each with the line it is
        bound at: its parameters, and what its body assigns -- an instance,
        a loop or `with` variable, an import, a nested def or class. A
        nested scope's own names (a helper's locals, a comprehension's
        variable) are not the workflow's."""
        engine = self
        bound: dict[str, int | None] = {}
        params = fn.params
        every = [*params.posonly_params, *params.params, *params.kwonly_params]
        for star in (params.star_arg, params.star_kwarg):
            if isinstance(star, cst.Param):
                every.append(star)
        for param in every:
            bound.setdefault(param.name.value, engine._line_of(param))

        def bind(target: cst.BaseExpression, at: cst.CSTNode) -> None:
            if isinstance(target, cst.Name):
                bound.setdefault(target.value, engine._line_of(at))
            elif isinstance(target, (cst.Tuple, cst.List)):
                for element in target.elements:
                    bind(element.value, at)
            elif isinstance(target, cst.StarredElement):
                bind(target.value, at)

        class _Binds(cst.CSTVisitor):
            def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
                if node is fn:
                    return True
                bound.setdefault(node.name.value, engine._line_of(node))
                return False

            def visit_ClassDef(self, node: cst.ClassDef) -> bool:
                bound.setdefault(node.name.value, engine._line_of(node))
                return False

            def visit_Lambda(self, node: cst.Lambda) -> bool:
                return False

            def visit_ListComp(self, node: cst.ListComp) -> bool:
                return False

            def visit_SetComp(self, node: cst.SetComp) -> bool:
                return False

            def visit_DictComp(self, node: cst.DictComp) -> bool:
                return False

            def visit_GeneratorExp(self, node: cst.GeneratorExp) -> bool:
                return False

            def visit_AssignTarget(self, node: cst.AssignTarget) -> None:
                bind(node.target, node)

            def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
                bind(node.target, node)

            def visit_AugAssign(self, node: cst.AugAssign) -> None:
                bind(node.target, node)

            def visit_For(self, node: cst.For) -> None:
                bind(node.target, node)

            def visit_WithItem(self, node: cst.WithItem) -> None:
                if node.asname is not None:
                    bind(node.asname.name, node)

            def visit_NamedExpr(self, node: cst.NamedExpr) -> None:
                bind(node.target, node)

            def visit_ExceptHandler(self, node: cst.ExceptHandler) -> None:
                if node.name is not None:
                    bind(node.name.name, node)

            def visit_ImportAlias(self, node: cst.ImportAlias) -> None:
                if node.asname is not None:
                    bind(node.asname.name, node)
                    return
                head: cst.BaseExpression = node.name
                while isinstance(head, cst.Attribute):
                    head = head.value
                bind(head, node)

        fn.visit(_Binds())
        return bound

    def _instance_type_name(self, fn: cst.FunctionDef, name: str) -> str:
        """The name instance `name` of workflow `fn` is made from: `Text`
        for `name = Text(...)`, `pair` for `name = pair(...)()`; empty when
        it is not a call of a name."""
        found = ""

        class _Made(cst.CSTVisitor):
            def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
                return node is fn

            def visit_Assign(self, node: cst.Assign) -> None:
                nonlocal found
                if not any(isinstance(t.target, cst.Name) and t.target.value == name for t in node.targets):
                    return
                head: cst.BaseExpression = node.value
                while isinstance(head, cst.Call):
                    head = head.func
                while isinstance(head, cst.Attribute):
                    head = head.value
                if isinstance(head, cst.Name) and not found:
                    found = head.value

        fn.visit(_Made())
        return found

    def _enclosing_functions(self, fn: cst.FunctionDef) -> list[cst.FunctionDef]:
        """The defs `fn` is nested in, innermost first (a factory's)."""
        chain: list[cst.FunctionDef] = []

        def visit(block: Sequence[cst.BaseStatement], parents: list[cst.FunctionDef]) -> bool:
            for stmt in block:
                if stmt is fn:
                    chain.extend(reversed(parents))
                    return True
                if isinstance(stmt, cst.FunctionDef) and isinstance(stmt.body, cst.IndentedBlock):
                    if visit(stmt.body.body, [*parents, stmt]):
                        return True
                    continue
                for child in self._child_blocks(stmt):
                    if visit(child.body, parents):
                        return True
            return False

        visit(self.module.body, [])
        return chain

    def _project_module_of(self, type_name: str) -> str:
        """The module this file imports `type_name` from: where the project
        defines that task or workflow, by the shortest package re-exporting
        it, or one this file already imports from."""
        index = self._index()
        here = Path(self.file_path).resolve()
        found = [d for d in index.definitions_named(type_name) if d.file != here]
        if not found:
            raise RewriteError(
                message=(
                    f"'{type_name}' is not bound in {here.name}, and no task or workflow of that "
                    f"name is defined in the project ({index.root}): import it first"
                ),
                diagnostic={"code": "type_not_found", "type": type_name, "root": str(index.root)},
            )
        if len(found) > 1:
            where = ", ".join(str(d.file.relative_to(index.root)) for d in found)
            raise RewriteError(
                message=f"'{type_name}' is defined more than once in the project ({where}): import the one meant first",
                diagnostic={"code": "type_ambiguous", "type": type_name, "files": [str(d.file) for d in found]},
            )
        modules = index.import_modules(found[0])
        if not modules:
            raise RewriteError(
                message=f"'{type_name}' is defined in {found[0].file}, which is not importable from {index.root}",
                diagnostic={"code": "type_not_importable", "type": type_name, "file": str(found[0].file)},
            )
        already = self._imported_from_modules()
        for module in modules:
            if module in already:
                return module
        return modules[0]

    def _imported_from_modules(self) -> set[str]:
        modules: set[str] = set()
        for stmt in self.module.body:
            if isinstance(stmt, cst.SimpleStatementLine):
                for small in stmt.body:
                    if isinstance(small, cst.ImportFrom) and small.module is not None and not small.relative:
                        modules.add(_dotted_name(small.module))
        return modules

    # ── A workflow's own ports: `@workflow(inputs={...}, outputs={...})` ──

    def _workflow_decorator(self, fn: cst.FunctionDef) -> tuple[int, cst.Decorator] | None:
        for index, dec in enumerate(fn.decorators):
            if _decorator_name(dec) == "workflow":
                return index, dec
        return None

    def _workflow_port_dict(self, fn: cst.FunctionDef, ports_kw: str) -> cst.Dict | None:
        """The `inputs=` / `outputs=` dict literal of `fn`'s `@workflow(...)`."""
        found = self._workflow_decorator(fn)
        if found is None or not isinstance(found[1].decorator, cst.Call):
            return None
        for arg in found[1].decorator.args:
            if arg.keyword is not None and arg.keyword.value == ports_kw and isinstance(arg.value, cst.Dict):
                return arg.value
        return None

    def _workflow_port_names(self, fn: cst.FunctionDef) -> dict[str, list[str]]:
        """The names `fn`'s decorator declares, by keyword (`inputs`, `outputs`)."""
        names: dict[str, list[str]] = {"inputs": [], "outputs": []}
        for ports_kw in names:
            mapping = self._workflow_port_dict(fn, ports_kw)
            for element in mapping.elements if mapping is not None else []:
                if isinstance(element, cst.DictElement):
                    key = _string_value(element.key)
                    if key is not None:
                        names[ports_kw].append(key)
        return names

    def _with_workflow_decorator(self, fn: cst.FunctionDef, decorator: cst.BaseExpression) -> cst.FunctionDef:
        found = self._workflow_decorator(fn)
        assert found is not None
        decorators = list(fn.decorators)
        decorators[found[0]] = found[1].with_changes(decorator=decorator)
        return fn.with_changes(decorators=decorators)

    def _workflow_name_mentions(
        self, fn: cst.FunctionDef, name: str
    ) -> tuple[list[tuple[cst.Call, int]], list[cst.SimpleString]]:
        """Where workflow `fn`'s own body names its port `name` by a string:
        the `connect(...)` ends that do (the call and which end), which an
        edit of the port follows, and the strings that reach a `connect` end
        some other way it cannot follow -- a name a loop connects from
        (`for name in ("Metrics", ...): connect(..., name)`), a variable a
        connect reads, a string inside a connect's argument. A string that
        names something else (`File(path="path")`, a docstring, a nested
        helper's) is not one, nor a `getattr(x, "name")`: that names a port of
        the instance `x`."""
        ends: list[tuple[cst.Call, int]] = []
        others: list[cst.SimpleString] = []
        end_names: set[str] = set()
        loops: list[cst.For] = []
        assigned: list[tuple[str, cst.BaseExpression]] = []

        class _Read(cst.CSTVisitor):
            def __init__(self) -> None:
                self._getattr = 0

            def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
                return node is fn

            def visit_ClassDef(self, node: cst.ClassDef) -> bool:
                return False

            def visit_Lambda(self, node: cst.Lambda) -> bool:
                return False

            def visit_For(self, node: cst.For) -> None:
                loops.append(node)

            def visit_Assign(self, node: cst.Assign) -> None:
                for target in node.targets:
                    if isinstance(target.target, cst.Name):
                        assigned.append((target.target.value, node.value))

            def visit_Call(self, node: cst.Call) -> None:
                if not _is_connect_call(node):
                    return
                for index, arg in enumerate(node.args):
                    if arg.keyword is not None:
                        continue
                    if index < 2 and _string_value(arg.value) == name:
                        ends.append((node, index))
                    elif index < 2 and isinstance(arg.value, cst.Name):
                        end_names.add(arg.value.value)
                    elif not (index < 2 and isinstance(arg.value, cst.SimpleString)):
                        others.extend(_port_name_strings(arg.value, name, everywhere=True))

        fn.visit(_Read())
        for loop in loops:
            if isinstance(loop.target, cst.Name) and loop.target.value in end_names:
                others.extend(_port_name_strings(loop.iter, name, everywhere=True))
        for variable, value in assigned:
            if variable in end_names:
                others.extend(_port_name_strings(value, name, everywhere=True))
        return ends, others

    def _refuse_by_name(self, fn: cst.FunctionDef, name: str, others: list[cst.SimpleString], doing: str, fix: str) -> None:
        if not others:
            return
        line = self._line_of(others[0])
        where = f" at line {line}" if line is not None else ""
        raise RewriteError(
            message=(
                f"port '{name}' of workflow '{fn.name.value}' is used by name{where}, "
                f"which this {doing} cannot follow: {fix}"
            ),
            diagnostic={
                "code": "port_used_by_name",
                "workflow": fn.name.value,
                "port": name,
                **({"line": line} if line is not None else {}),
            },
        )

    def create_port(
        self,
        *,
        workflow: str | None = None,
        direction: str,
        portName: str,
        portType: str,
    ) -> None:
        """Declare a port on the workflow: `"Name": type` in its decorator's
        `inputs=` / `outputs=` dict, the keyword written when it is not
        there yet. Nothing else of the module changes."""
        if direction not in ("input", "output"):
            raise RewriteError(message=f"Invalid port direction: {direction}")
        self._ensure_identifier(portName)
        port_type = _parse_user_expression(portType, f"the type of port '{portName}'")
        fn, _body = self._get_workflow_body(workflow)
        found = self._workflow_decorator(fn)
        if found is None:
            raise RewriteError(
                message=f"'{fn.name.value}' has no @workflow decorator to declare port '{portName}' on",
                diagnostic={"code": "workflow_not_found", "workflow": fn.name.value},
            )
        declared = self._workflow_port_names(fn)
        if portName in declared["inputs"] or portName in declared["outputs"]:
            raise RewriteError(
                message=f"workflow '{fn.name.value}' has a port '{portName}' already",
                diagnostic={"code": "port_exists", "workflow": fn.name.value, "port": portName},
            )
        ports_kw = "inputs" if direction == "input" else "outputs"
        element = cst.DictElement(key=cst.SimpleString(f'"{portName}"'), value=port_type)
        decorator = found[1].decorator
        call = decorator if isinstance(decorator, cst.Call) else cst.Call(func=decorator, args=[])
        args = list(call.args)
        at = next(
            (i for i, arg in enumerate(args) if arg.keyword is not None and arg.keyword.value == ports_kw),
            None,
        )
        if at is not None and not isinstance(args[at].value, cst.Dict):
            raise RewriteError(
                message=(
                    f"the {ports_kw}= of workflow '{fn.name.value}' is not written as a dict, "
                    f"so port '{portName}' cannot be added to it here"
                ),
                diagnostic={"code": "unsupported_ports_form", "workflow": fn.name.value},
            )
        if at is not None:
            mapping = args[at].value
            assert isinstance(mapping, cst.Dict)
            elements = _with_inserted(list(mapping.elements), len(mapping.elements), element, mapping.lbrace.whitespace_after)
            args[at] = args[at].with_changes(value=mapping.with_changes(elements=elements))
        else:
            # Inputs before outputs, as they are read.
            outputs_at = next(
                (i for i, arg in enumerate(args) if arg.keyword is not None and arg.keyword.value == "outputs"),
                None,
            )
            position = outputs_at if ports_kw == "inputs" and outputs_at is not None else len(args)
            new_arg = _keyword_arg(ports_kw, cst.Dict(elements=[element]))
            args = _with_inserted(args, position, new_arg, call.whitespace_before_args)
        self._replace_node(fn, self._with_workflow_decorator(fn, call.with_changes(args=args)))
        if "Any" in portType:
            self._set_module(self._ensure_any_import(self.module))

    def delete_workflow_port_by_decorator(self, fn: cst.FunctionDef, direction: str, portName: str) -> bool:
        """Take port `portName` out of `fn`'s decorator dict and the
        `connect(...)` ends naming it (as a deleted node's connections go).
        False when the decorator does not declare it."""
        ports_kw = "inputs" if direction == "input" else "outputs"
        mapping = self._workflow_port_dict(fn, ports_kw)
        if mapping is None or portName not in self._workflow_port_names(fn)[ports_kw]:
            return False
        _ends, others = self._workflow_name_mentions(fn, portName)
        self._refuse_by_name(fn, portName, others, "delete", "take it out there first")
        found = self._workflow_decorator(fn)
        assert found is not None and isinstance(found[1].decorator, cst.Call)
        call = found[1].decorator
        args = list(call.args)
        at = next(i for i, arg in enumerate(args) if arg.keyword is not None and arg.keyword.value == ports_kw)
        index = next(
            i for i, el in enumerate(mapping.elements)
            if isinstance(el, cst.DictElement) and _string_value(el.key) == portName
        )
        elements = _with_removed(list(mapping.elements), index)
        if elements:
            args[at] = args[at].with_changes(value=mapping.with_changes(elements=elements))
        else:
            args = _with_removed(args, at)
        decorator: cst.BaseExpression = call.with_changes(args=args) if args else call.func
        end_index = 0 if direction == "input" else 1

        class _DropConnects(_WorkflowBodyTransformer):
            def leave_SimpleStatementLine(
                self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine
            ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
                kept = [
                    small for small in updated_node.body
                    if not (
                        isinstance(small, cst.Expr)
                        and _is_connect_call(small.value)
                        and isinstance(small.value, cst.Call)
                        and len(small.value.args) > end_index
                        and _string_value(small.value.args[end_index].value) == portName
                    )
                ]
                if len(kept) == len(updated_node.body):
                    return updated_node
                return updated_node.with_changes(body=kept) if kept else cst.RemovalSentinel.REMOVE

            def leave_IndentedBlock(
                self, original_node: cst.IndentedBlock, updated_node: cst.IndentedBlock
            ) -> cst.IndentedBlock:
                # A loop or a branch whose only statement was a connect taken out.
                if original_node.body and not updated_node.body:
                    return updated_node.with_changes(body=[cst.SimpleStatementLine([cst.Pass()])])
                return updated_node

        edited = self._with_workflow_decorator(fn, decorator)
        edited_fn = edited.visit(_DropConnects(edited))
        assert isinstance(edited_fn, cst.FunctionDef)
        self._replace_node(fn, edited_fn)
        return True

    def rename_workflow_port(self, fn: cst.FunctionDef, old: str, new: str) -> None:
        """Rename workflow `fn`'s own port `old`: its decorator key and every
        `connect(...)` end naming it, or nothing at all when it is named
        some other way the rename cannot follow."""
        declared = self._workflow_port_names(fn)
        if new in declared["inputs"] or new in declared["outputs"]:
            raise RewriteError(
                message=f"workflow '{fn.name.value}' has a port '{new}' already",
                diagnostic={"code": "port_exists", "workflow": fn.name.value, "port": new},
            )
        _ends, others = self._workflow_name_mentions(fn, old)
        self._refuse_by_name(fn, old, others, "rename", "rename it there")

        class _RenameKeys(cst.CSTTransformer):
            def leave_DictElement(self, original_node: cst.DictElement, updated_node: cst.DictElement) -> cst.DictElement:
                if isinstance(updated_node.key, cst.SimpleString) and _string_value(updated_node.key) == old:
                    return updated_node.with_changes(key=_quoted_like(updated_node.key, new))
                return updated_node

        class _RenameEnds(_WorkflowBodyTransformer):
            def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
                if not _is_connect_call(original_node):
                    return updated_node
                args = list(updated_node.args)
                for index, arg in enumerate(args[:2]):
                    if arg.keyword is None and isinstance(arg.value, cst.SimpleString) and _string_value(arg.value) == old:
                        args[index] = arg.with_changes(value=_quoted_like(arg.value, new))
                return updated_node.with_changes(args=args)

        edited = fn
        found = self._workflow_decorator(fn)
        if found is not None and isinstance(found[1].decorator, cst.Call):
            call = found[1].decorator
            args = [
                arg.with_changes(value=arg.value.visit(_RenameKeys()))
                if arg.keyword is not None and arg.keyword.value in ("inputs", "outputs") and isinstance(arg.value, cst.Dict)
                else arg
                for arg in call.args
            ]
            edited = self._with_workflow_decorator(fn, call.with_changes(args=args))
        renamed = edited.visit(_RenameEnds(edited))
        assert isinstance(renamed, cst.FunctionDef)
        self._replace_node(fn, renamed)

    def _is_workflow_port(self, fn: cst.FunctionDef, name: str) -> bool:
        declared = self._workflow_port_names(fn)
        if name in declared["inputs"] or name in declared["outputs"]:
            return True
        ends, _others = self._workflow_name_mentions(fn, name)
        return bool(ends)

    def list_task_types(self, *, kind: str) -> list[str]:
        """Every class in this file declared with the given kind's decorator.

        The kind IS the decorator name, so one matcher pair serves all of them.
        This was a ladder of one pair per kind, which is how `streamblocks`
        came to be creatable but not listable: the picker could not offer a
        type the picker itself had written.
        """
        if kind not in TASK_TYPE_KINDS:
            return []
        results: list[str] = []

        class _Visitor(cst.CSTVisitor):
            def visit_ClassDef(self, node: cst.ClassDef) -> None:
                for dec in node.decorators:
                    target = dec.decorator
                    # `@kind` and `@kind(...)` are the same declaration.
                    if m.matches(target, m.Name(kind)) or m.matches(
                        target, m.Call(func=m.Name(kind))
                    ):
                        results.append(node.name.value)
                        return

        self.module.visit(_Visitor())
        results.sort()
        return results

    def list_workflow_types(self) -> list[str]:
        results: list[str] = []

        class _Visitor(cst.CSTVisitor):
            def __init__(self, matcher: RewriteEngine) -> None:
                self._matcher = matcher

            def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
                if self._matcher._is_workflow_def(node):
                    results.append(node.name.value)

        self.module.visit(_Visitor(self))
        results.sort()
        return results

    def list_instance_names(self, *, workflow: str | None = None) -> list[str]:
        fn, body = self._get_workflow_body(workflow)
        nodes, _edges, _instance_to_type = self._collect_static_graph_items(body)
        return sorted({str(node.get("id", "")) for node in nodes if str(node.get("id", ""))})

    def instance_types(self, *, workflow: str | None = None) -> dict[str, str]:
        """Map each instance (node) name → its component type name in the workflow."""
        fn, body = self._get_workflow_body(workflow)
        _nodes, _edges, instance_to_type = self._collect_static_graph_items(body)
        return dict(instance_to_type)

    def instance_scopes(self, *, workflow: str | None = None) -> dict[str, str]:
        """Map each instance (node) name → its control-flow scope id in the workflow."""
        fn, body = self._get_workflow_body(workflow)
        nodes, _edges, _instances = self._collect_static_graph_items(body)
        return {str(n["id"]): str(n.get("scope", "scope:root")) for n in nodes if n.get("id")}

    def port_metadata(self, type_name: str, port_name: str) -> dict[str, str] | None:
        """Resolve a component port's `{direction, type}` from its type's `class Ports`.
        Returns None if the type/port cannot be found. Direction defaults to "in" when no
        `direction=` kwarg is present (matches the Port default)."""
        for stmt in self.module.body:
            if not (isinstance(stmt, cst.ClassDef) and stmt.name.value == type_name):
                continue
            ports = next(
                (c for c in stmt.body.body if isinstance(c, cst.ClassDef) and c.name.value == "Ports"),
                None,
            )
            if ports is None:
                return None
            for line in ports.body.body:
                if not isinstance(line, cst.SimpleStatementLine):
                    continue
                for s in line.body:
                    if isinstance(s, cst.Assign) and any(
                        isinstance(t.target, cst.Name) and t.target.value == port_name for t in s.targets
                    ):
                        return _parse_port_value(s.value)
            return None
        return None

    def _source_positions(self) -> Any | None:
        """Resolve libcst PositionProvider metadata for self.module (node → CodeRange),
        so graph elements can carry source `location`. Best-effort; None on failure."""
        cached = getattr(self, "_positions_cache", None)
        if cached is not None and cached[0] is self.module:
            return cached[1]
        try:
            from libcst.metadata import MetadataWrapper, PositionProvider

            positions = MetadataWrapper(self.module, unsafe_skip_copy=True).resolve(PositionProvider)
        except Exception:
            return None
        self._positions_cache = (self.module, positions)
        return positions

    def export_workflow_graph(
        self,
        *,
        workflow: str | None = None,
        include_nested: bool = True,
        _visited_workflows: set[str] | None = None,
    ) -> dict[str, Any]:
        fn, body, parents = self._find_workflow_def(workflow)
        workflow_name = fn.name.value
        nodes, edges, instance_to_type = self._collect_static_graph_items(
            body, positions=self._source_positions()
        )
        # Additively attach canonical, stable element ids (contract v2) without
        # touching the existing fields the diagram server / tests rely on.
        _augment_graph_ids(nodes, edges)
        visited = set(_visited_workflows or set())
        visited.add(workflow_name)

        graph: dict[str, Any] = {
            "workflow": workflow_name,
            "nodes": nodes,
            "edges": edges,
            "file": self.file_path,
        }
        if parents:
            factory_fn = parents[-1]
            helper_parameters = self._helper_default_parameters(factory_fn)
            if helper_parameters:
                graph["parameters"] = helper_parameters
            graph["factoryName"] = factory_fn.name.value

        if include_nested:
            children: list[dict[str, Any]] = []
            for node in nodes:
                ref = str(node.get("ref", "")).strip()
                if not ref:
                    continue
                if ref in visited:
                    continue
                try:
                    child_graph = self.export_workflow_graph(
                        workflow=ref,
                        include_nested=True,
                        _visited_workflows=set(visited),
                    )
                    children.append(
                        {
                            "instance": str(node.get("id", "")),
                            "workflow": ref,
                            "graph": child_graph,
                            "source": "same-file",
                        }
                    )
                    continue
                except RewriteError:
                    pass

                try:
                    child_graph = self._resolve_nested_workflow_graph(
                        ref,
                        visited_workflows=set(visited),
                    )
                    children.append(
                        {
                            "instance": str(node.get("id", "")),
                            "workflow": ref,
                            "graph": child_graph,
                            "source": "import",
                        }
                    )
                except RewriteError:
                    continue

            if children:
                graph["children"] = children

        return graph

    def create_task_type(
        self,
        *,
        kind: str,
        name: str,
        facade: str | None = None,
        network: str | None = None,
        viewType: str | None = None,
    ) -> None:
        self._ensure_identifier(name)
        if kind not in TASK_TYPE_KINDS:
            raise RewriteError(message=f"Invalid task kind: {kind}")
        if kind == "streamblocks":
            if facade not in ("design", "instance"):
                raise RewriteError(
                    message=(
                        "A streamblocks type needs facade='design' or "
                        f"facade='instance', not {facade!r}"
                    )
                )
            if facade == "instance" and not (network or "").strip():
                raise RewriteError(
                    message=(
                        "A streamblocks instance needs network='path/to/network.py' "
                        "— without one there is nothing to compile, run or open"
                    )
                )
        if kind == "tool":
            decorator: cst.BaseExpression = cst.Call(
                func=cst.Name("tool"),
                args=[_keyword_arg("cmd", cst.SimpleString('"echo"'))]
            )
        elif kind == "agent":
            decorator = cst.Call(
                func=cst.Name("agent"),
                args=[_keyword_arg("prompt", cst.SimpleString('"TODO"'))]
            )
        elif kind == "viewer":
            decorator = cst.Name("viewer")
        elif kind == "task":
            decorator = cst.Name("task")
        elif kind == "streamblocks":
            # The decorator refuses a bad combination too, but writing one into
            # the file and letting import time report it would be a poor way to
            # find out.
            sb_args = [
                _keyword_arg("facade", cst.SimpleString(f'"{facade}"'))
            ]
            if (network or "").strip():
                sb_args.append(
                    _keyword_arg("network", cst.SimpleString(f'"{network}"'))
                )
            decorator = cst.Call(func=cst.Name("streamblocks"), args=sb_args)
        elif kind == "source" and isinstance(viewType, str) and viewType.strip():
            decorator = cst.Call(
                func=cst.Name("source"),
                args=[_keyword_arg("viewType", cst.SimpleString(f'"{viewType.strip()}"'))],
            )
        else:
            # An empty viewType is not the same as none: it would ask the IDE
            # to open with an editor named "". No editor means the default one.
            decorator = cst.Name(kind)
        def port(name: str, type_name: str, direction: str) -> cst.SimpleStatementLine:
            # The direction is said, not left to `Port`'s default ("inout").
            return cst.SimpleStatementLine([
                cst.Assign(
                    targets=[cst.AssignTarget(target=cst.Name(name))],
                    value=cst.Call(
                        func=cst.Subscript(
                            value=cst.Name("Port"),
                            slice=[cst.SubscriptElement(slice=cst.Index(value=cst.Name(type_name)))],
                        ),
                        args=[_keyword_arg("direction", cst.SimpleString(f'"{direction}"'))],
                    ),
                )
            ])

        # A source is the mirror of a viewer: one output, no input. Writing the
        # default In + Out would produce a class the decorator refuses to load.
        if kind == "source":
            ports_body = [port("Out", "File", "out")]
        elif kind == "viewer":
            ports_body = [port("In", "Any", "in")]
        else:
            ports_body = [port("In", "Any", "in"), port("Out", "Any", "out")]
        ensure_import = True
        ports_class = cst.ClassDef(
            name=cst.Name("Ports"),
            body=cst.IndentedBlock(body=ports_body)
        )
        class_body: list[cst.BaseStatement] = [ports_class]
        if kind == "source":
            # Annotated with NO default. A class-level default would make this
            # state shared by every node rather than a per-node parameter, and
            # the decorator refuses that outright.
            class_body.insert(0, cst.SimpleStatementLine([
                cst.AnnAssign(
                    target=cst.Name("path"),
                    annotation=cst.Annotation(cst.Name("File")),
                    value=None,
                )
            ]))
            ports_class = ports_class.with_changes(leading_lines=[cst.EmptyLine(indent=False)])
            class_body[1] = ports_class
        class_def = cst.ClassDef(
            name=cst.Name(name),
            decorators=[cst.Decorator(decorator=decorator)],
            body=cst.IndentedBlock(body=class_body),
            # Two blank lines around a top-level definition.
            leading_lines=[cst.EmptyLine(), cst.EmptyLine()],
        )
        body = list(self.module.body)
        insert_at = len(body)
        for idx, stmt in enumerate(body):
            if isinstance(stmt, cst.FunctionDef) and self._is_workflow_def(stmt):
                insert_at = idx
                break
        if insert_at < len(body):
            following = body[insert_at]
            blank = 0
            for line in following.leading_lines:
                if line.comment is not None:
                    break
                blank += 1
            if blank < 2:
                body[insert_at] = following.with_changes(
                    leading_lines=[*[cst.EmptyLine()] * (2 - blank), *following.leading_lines]
                )
        new_body = body[:insert_at] + [class_def] + body[insert_at:]
        updated = self.module.with_changes(body=new_body)
        if ensure_import:
            # Everything the emitted class needs to RUN, not just `Any`: the
            # decorator that makes it a node and the `Port` its members are
            # built from. Adding one of the three was enough while every file
            # already imported the other two by hand; it stops being enough the
            # moment a node is created in a file that does not.
            if kind == "source":
                # Its ports and its parameter are typed `File`, not `Any`.
                updated = self._ensure_import(updated, "wfpy", ["File"])
            else:
                updated = self._ensure_any_import(updated)
            # `kind` IS the decorator's name for every kind — `tool` and
            # `agent` are called (`@tool(...)`), which changes the decorator
            # expression but not the symbol that has to be in scope.
            updated = self._ensure_import(updated, "wfpy", ["Port", kind])
        self._set_module(updated)

    def create_workflow_type(self, *, name: str) -> None:
        self._ensure_identifier(name)
        body = list(self.module.body)
        insert_at = len(body)
        for idx, stmt in enumerate(body):
            if isinstance(stmt, cst.FunctionDef) and self._is_workflow_def(stmt):
                insert_at = idx
                break
        fn = cst.FunctionDef(
            name=cst.Name(name),
            decorators=[cst.Decorator(decorator=cst.Name("workflow"))],
            params=cst.Parameters([]),
            body=cst.IndentedBlock(body=[cst.SimpleStatementLine([cst.Pass()])])
        )
        new_body = body[:insert_at] + [fn] + body[insert_at:]
        self._set_module(self.module.with_changes(body=new_body))

    def _ensure_any_import(self, module: cst.Module) -> cst.Module:
        return self._ensure_import(module, "typing", ["Any"])

    def _ensure_import(self, module: cst.Module, package: str, names: list[str]) -> cst.Module:
        """Make sure `from <package> import <names>` covers every name.

        Generated code has to be code that RUNS. A created task carries a
        `@task` decorator and `Port[Any]()` members, and dropping that into a
        file that imports neither leaves the author with a NameError to fix by
        hand — the sidecar wrote it, so the sidecar owes the import.

        Names are folded into an existing `from <package> import ...` rather
        than added as a second line, because that is what the examples and the
        hand-written workflows look like, and a rewriter should leave a file
        looking the way its author would have written it. Import order within
        the statement is kept alphabetical for the same reason.

        A star import already covers everything, and an aliased import
        (`from wfpy import task as t`) is left alone: the name is in scope under
        another spelling, and adding the plain one would be redundant at best
        and shadowing at worst.
        """
        wanted = [name for name in names if name]
        if not wanted:
            return module

        body = list(module.body)
        for index, line in enumerate(body):
            # Only a simple statement line can hold an import at module level;
            # a compound statement (a `try:` guarding an optional dependency,
            # say) is left alone rather than rewritten from underneath.
            if not isinstance(line, cst.SimpleStatementLine):
                continue
            statements = list(line.body)
            for position, stmt in enumerate(statements):
                if not isinstance(stmt, cst.ImportFrom):
                    continue
                if stmt.module is None or stmt.relative or _dotted_name(stmt.module) != package:
                    continue
                if isinstance(stmt.names, cst.ImportStar):
                    return module
                present = {
                    alias.name.value
                    for alias in stmt.names
                    if isinstance(alias, cst.ImportAlias) and isinstance(alias.name, cst.Name)
                }
                # An alias covers the name under a different spelling.
                aliased = {
                    alias.name.value
                    for alias in stmt.names
                    if isinstance(alias, cst.ImportAlias) and alias.asname is not None
                }
                missing = [name for name in wanted if name not in present and name not in aliased]
                if not missing:
                    return module
                merged = sorted(
                    [*stmt.names, *[cst.ImportAlias(name=cst.Name(name)) for name in missing]],
                    key=lambda alias: alias.name.value if isinstance(alias.name, cst.Name) else "",
                )
                # The last alias must not carry a trailing comma.
                merged = [alias.with_changes(comma=cst.MaybeSentinel.DEFAULT) for alias in merged]
                new_statements = list(statements)
                new_statements[position] = stmt.with_changes(names=merged)
                body[index] = line.with_changes(body=new_statements)
                return module.with_changes(body=body)

        package_expr = cst.parse_expression(package)
        assert isinstance(package_expr, (cst.Name, cst.Attribute))
        new_import = cst.SimpleStatementLine([
            cst.ImportFrom(
                module=package_expr,
                names=[cst.ImportAlias(name=cst.Name(name)) for name in sorted(wanted)],
            )
        ])
        return module.with_changes(body=self._placed_import(list(body), new_import, package))

    def _import_group(self, package: str, relative: bool = False) -> int:
        """0 the standard library, 1 an installed package, 2 the project's own."""
        if relative:
            return 2
        top = package.split(".", 1)[0]
        if top in getattr(sys, "stdlib_module_names", ()) or top == "__future__":
            return 0
        from wfpy._project_index import project_root

        root = project_root(self.file_path)
        if (root / top).is_dir() or (root / f"{top}.py").is_file():
            return 2
        return 1

    def _placed_import(
        self, body: list[cst.BaseStatement], new_import: cst.SimpleStatementLine, package: str
    ) -> list[cst.BaseStatement]:
        """`body` with `new_import` among the file's imports, as they are
        grouped: after the last import of its group (the standard library,
        installed packages, the project's own), else between the groups
        around it; after the docstring, one blank line on each side, when the
        file imports nothing yet."""
        start = 0
        if body:
            first = body[0]
            if isinstance(first, cst.SimpleStatementLine) and len(first.body) == 1:
                expr = first.body[0]
                if isinstance(expr, cst.Expr) and isinstance(expr.value, cst.SimpleString):
                    # Keep a module docstring first.
                    start = 1
        # The leading import block (`from __future__` included).
        imports: list[tuple[int, int]] = []  # (index, group)
        index = start
        while index < len(body):
            line = body[index]
            if not (
                isinstance(line, cst.SimpleStatementLine)
                and line.body
                and all(isinstance(stmt, (cst.Import, cst.ImportFrom)) for stmt in line.body)
            ):
                break
            stmt = line.body[0]
            if isinstance(stmt, cst.ImportFrom):
                name = _dotted_name(stmt.module) if stmt.module is not None else ""
                group = -1 if name == "__future__" else self._import_group(name, bool(stmt.relative))
            elif isinstance(stmt, cst.Import):
                group = self._import_group(_dotted_name(stmt.names[0].name))
            else:
                group = 1
            imports.append((index, group))
            index += 1
        group = self._import_group(package)
        if not imports:
            placed = new_import.with_changes(leading_lines=[cst.EmptyLine()] if start else [])
            rest = body[start:]
            if rest and not getattr(rest[0], "leading_lines", True):
                rest = [rest[0].with_changes(leading_lines=[cst.EmptyLine()]), *rest[1:]]
            return [*body[:start], placed, *rest]
        same = [i for i, g in imports if g == group]
        if same:
            at = same[-1] + 1
            return [*body[:at], new_import.with_changes(leading_lines=[]), *body[at:]]
        earlier = [i for i, g in imports if g < group]
        if earlier:
            at = earlier[-1] + 1
            return [*body[:at], new_import.with_changes(leading_lines=[cst.EmptyLine()]), *body[at:]]
        # Before the first import of a later group, which keeps its own lines
        # above the new one and gets a blank line between the two groups.
        at = imports[0][0]
        following = body[at]
        placed = new_import.with_changes(leading_lines=getattr(following, "leading_lines", []))
        return [*body[:at], placed, following.with_changes(leading_lines=[cst.EmptyLine()]), *body[at + 1:]]

    def connect(
        self,
        *,
        workflow: str | None = None,
        from_expr: str | None = None,
        to_expr: str | None = None,
        from_port: Any | None = None,
        to_port: Any | None = None,
        scope_control: str | None = None,
        scope_branch: str | None = None,
    ) -> None:
        fn, body = self._get_workflow_body(workflow)
        target_block = self._find_scope_block(body, scope_control, scope_branch)
        target_body = list(target_block.body)
        if from_expr is None:
            from_expr = self._port_expr_from_spec(from_port)
        if to_expr is None:
            to_expr = self._port_expr_from_spec(to_port)
        call = cst.Call(
            func=cst.Name("connect"),
            args=[
                cst.Arg(value=_parse_user_expression(from_expr, "the connection's source")),
                cst.Arg(value=_parse_user_expression(to_expr, "the connection's target")),
            ],
        )
        stmt = cst.SimpleStatementLine([cst.Expr(call)])
        new_stmt_body = self._insert_statement(target_body, stmt, "connect")
        new_block = target_block.with_changes(body=new_stmt_body)
        self._update_scope_body(fn, scope_control, scope_branch, new_block)

    def delete_node(self, *, workflow: str | None = None, name: str) -> None:
        fn, _ = self._get_workflow_body(workflow)

        class _RemoveNode(_WorkflowBodyTransformer):
            def __init__(self, fn_node: cst.FunctionDef, node_name: str) -> None:
                super().__init__(fn_node)
                self._node_name = node_name
                self._in_target_fn = True
                self.removed = False

            def _is_incident_edge_expr(self, expr: cst.BaseExpression) -> bool:
                code = _expr_to_code(expr)
                return code == self._node_name or code.startswith(f"{self._node_name}.")

            def leave_SimpleStatementLine(
                self,
                original_node: cst.SimpleStatementLine,
                updated_node: cst.SimpleStatementLine,
            ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
                if not self._in_target_fn:
                    return updated_node

                kept = []
                changed = False
                for stmt in updated_node.body:
                    if any(
                        isinstance(target.target, cst.Name) and target.target.value == self._node_name
                        for target in getattr(stmt, "targets", [])
                    ):
                        self.removed = True
                        changed = True
                        continue

                    if isinstance(stmt, cst.Expr) and isinstance(stmt.value, cst.Call):
                        expr = stmt.value
                        if (
                            isinstance(expr.func, cst.Name)
                            and expr.func.value == "connect"
                            and len(expr.args) == 2
                            and (
                                self._is_incident_edge_expr(expr.args[0].value)
                                or self._is_incident_edge_expr(expr.args[1].value)
                            )
                        ):
                            changed = True
                            continue

                    kept.append(stmt)

                if not changed:
                    return updated_node
                if not kept:
                    return cst.RemovalSentinel.REMOVE
                return updated_node.with_changes(body=kept)

        transformer = _RemoveNode(fn, name)
        updated = fn.visit(transformer)
        if not transformer.removed or not isinstance(updated, cst.FunctionDef):
            raise RewriteError(message=f"Node not found: {name}", diagnostic={"code": "node_not_found"})
        self._replace_node(fn, updated)

    def delete_edge(self, *, workflow: str | None = None, from_expr: str, to_expr: str) -> None:
        fn, _ = self._get_workflow_body(workflow)

        class _RemoveEdge(_WorkflowBodyTransformer):
            def __init__(self, fn_node: cst.FunctionDef) -> None:
                super().__init__(fn_node)
                self._in_target_fn = True
                self.removed = False

            def leave_SimpleStatementLine(
                self,
                original_node: cst.SimpleStatementLine,
                updated_node: cst.SimpleStatementLine,
            ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
                if not self._in_target_fn or self.removed:
                    return updated_node
                for s in updated_node.body:
                    if not isinstance(s, cst.Expr):
                        continue
                    if not m.matches(s, m.Expr(value=m.Call(func=m.Name("connect")))):
                        continue
                    expr = s.value
                    if not isinstance(expr, cst.Call):
                        continue
                    if len(expr.args) != 2:
                        continue
                    left = expr.args[0].value
                    right = expr.args[1].value
                    if left is None or right is None:
                        continue
                    if cst.Module([]).code_for_node(left) == from_expr and cst.Module([]).code_for_node(right) == to_expr:
                        self.removed = True
                        return cst.RemovalSentinel.REMOVE
                return updated_node

        transformer = _RemoveEdge(fn)
        updated = fn.visit(transformer)
        if not transformer.removed or not isinstance(updated, cst.FunctionDef):
            raise RewriteError(message="Edge not found", diagnostic={"code": "edge_not_found"})
        self._replace_node(fn, updated)

    def delete_port(
        self, *, workflow: str | None = None, direction: str, portName: str, usedIn: Any = None
    ) -> None:
        if direction not in ("input", "output"):
            raise RewriteError(message=f"Invalid port direction: {direction}")
        fn, body = self._get_workflow_body(workflow)
        # Refused while a workflow using this one -- of this file, or one the
        # IDE names (`usedIn`) -- still names it.
        self._refuse_port_in_use(self._workflow_port_uses(fn, usedIn), portName, entityType=fn.name.value)
        if self.delete_workflow_port_by_decorator(fn, direction, portName):
            return
        # The older form: `<workflow>.outputs = {...}` assigned beside it.
        ports_attr = "inputs" if direction == "input" else "outputs"

        class _RemovePort(cst.CSTTransformer):
            def __init__(self) -> None:
                self.removed = False

            def leave_SimpleStatementLine(
                self,
                original_node: cst.SimpleStatementLine,
                updated_node: cst.SimpleStatementLine,
            ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
                for stmt in updated_node.body:
                    if not isinstance(stmt, cst.Assign):
                        continue
                    if len(stmt.targets) != 1:
                        continue
                    target = stmt.targets[0].target
                    if not isinstance(target, cst.Attribute):
                        continue
                    if not isinstance(target.value, cst.Name):
                        continue
                    if target.value.value != fn.name.value or target.attr.value != ports_attr:
                        continue
                    if isinstance(stmt.value, cst.Dict):
                        elements = []
                        removed = False
                        for el in stmt.value.elements:
                            if not isinstance(el, cst.DictElement):
                                elements.append(el)
                                continue
                            if isinstance(el.key, cst.SimpleString) and el.key.value.strip('"') == portName:
                                removed = True
                                continue
                            elements.append(el)
                        if removed:
                            self.removed = True
                            if not elements:
                                return cst.RemovalSentinel.REMOVE
                            return updated_node.with_changes(body=[stmt.with_changes(value=stmt.value.with_changes(elements=elements))])
                return updated_node

        transformer = _RemovePort()
        updated = self.module.visit(transformer)
        if not transformer.removed:
            raise RewriteError(
                message=f"Port not found: {portName}",
                diagnostic={"code": "port_not_found", "workflow": fn.name.value, "port": portName},
            )
        self._set_module(updated)

    def update_node_parameter(
        self,
        *,
        workflow: str | None = None,
        entity: str,
        parameterName: str,
        newValue: str,
    ) -> None:
        """Set keyword `parameterName` of the call that makes instance
        `entity` of the workflow: `Cls(...)`, or the factory call of a
        workflow a factory makes (`flow = factory(...)()`)."""
        if not str(parameterName).isidentifier() or keyword.iskeyword(str(parameterName)):
            raise RewriteError(
                message=f"invalid parameter name: {parameterName}",
                diagnostic={"code": "invalid_parameter_name"},
            )
        value = _parse_user_expression(newValue, f"the value of {entity}.{parameterName}")
        fn, body = self._get_workflow_body(workflow)
        self._refuse_unbound(value, fn, f"the value of {entity}.{parameterName}")

        class _UpdateParam(_WorkflowBodyTransformer):
            def __init__(self) -> None:
                super().__init__(fn)
                self.updated = False

            def _set(self, call: cst.Call) -> cst.Call:
                args = list(call.args)
                for idx, arg in enumerate(args):
                    if arg.keyword and arg.keyword.value == parameterName:
                        args[idx] = arg.with_changes(value=value)
                        return call.with_changes(args=args)
                args = _with_inserted(args, len(args), _keyword_arg(parameterName, value), call.whitespace_before_args)
                return call.with_changes(args=args)

            def leave_Assign(self, original_node: cst.Assign, updated_node: cst.Assign) -> cst.Assign:
                if self.updated or len(updated_node.targets) != 1:
                    return updated_node
                target = updated_node.targets[0].target
                call = updated_node.value
                if not (isinstance(target, cst.Name) and target.value == entity and isinstance(call, cst.Call)):
                    return updated_node
                self.updated = True
                if isinstance(call.func, cst.Call) and not call.args:
                    # `factory(...)()`: the parameters are the factory's.
                    return updated_node.with_changes(value=call.with_changes(func=self._set(call.func)))
                return updated_node.with_changes(value=self._set(call))

        transformer = _UpdateParam()
        updated = fn.visit(transformer)
        if not transformer.updated or not isinstance(updated, cst.FunctionDef):
            raise RewriteError(
                message=f"Entity parameter not found: {entity}.{parameterName}",
                diagnostic={"code": "node_not_found"},
            )
        self._replace_node(fn, updated)

    def update_definition_annotation(
        self,
        *,
        entityType: str,
        annotationName: str,
        annotationText: str,
    ) -> None:
        def _normalize_python_literal_name(value: cst.BaseExpression) -> cst.BaseExpression:
            if isinstance(value, cst.Name):
                if value.value == "true":
                    return cst.Name("True")
                if value.value == "false":
                    return cst.Name("False")
                if value.value == "null":
                    return cst.Name("None")
            return value

        parsed_decorator = _parse_user_expression(str(annotationText).strip().lstrip('@'), f"the annotation @{annotationName}")
        if isinstance(parsed_decorator, cst.Call):
            normalized_args: list[cst.Arg] = []
            for arg in parsed_decorator.args:
                normalized_args.append(arg.with_changes(value=_normalize_python_literal_name(arg.value)))
            parsed_decorator = parsed_decorator.with_changes(args=normalized_args)

        class _UpdateAnnot(cst.CSTTransformer):
            def __init__(self) -> None:
                self.updated = False

            def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
                if self.updated or original_node.name.value != entityType:
                    return updated_node
                decorators = list(updated_node.decorators)
                new_decorator = cst.Decorator(decorator=parsed_decorator)
                replaced = False
                for idx, dec in enumerate(decorators):
                    name = None
                    if isinstance(dec.decorator, cst.Name):
                        name = dec.decorator.value
                    elif isinstance(dec.decorator, cst.Call) and isinstance(dec.decorator.func, cst.Name):
                        name = dec.decorator.func.value
                    if name == annotationName:
                        decorators[idx] = new_decorator
                        replaced = True
                        break
                if not replaced:
                    decorators.insert(0, new_decorator)
                self.updated = True
                return updated_node.with_changes(decorators=decorators)

        transformer = _UpdateAnnot()
        updated = self.module.visit(transformer)
        if not transformer.updated:
            raise RewriteError(message=f"Definition not found for annotation update: {entityType}")
        self._set_module(updated)
        self._ensure_wfpy_decorator_import(parsed_decorator)

    def merge_definition_annotation_args(
        self,
        *,
        entityType: str,
        annotationName: str,
        argUpdates: dict[str, str],
    ) -> None:
        """Merge *argUpdates* into an existing decorator, preserving unlisted args.

        ``argUpdates`` maps keyword-argument names to Python expression strings.
        Args already present in the decorator are replaced; new args are appended.
        Args **not** in *argUpdates* are left untouched, so complex parameters the
        UI does not manage (e.g. ``outputValidators``) survive editing.
        """

        def _normalize_python_literal_name(value: cst.BaseExpression) -> cst.BaseExpression:
            if isinstance(value, cst.Name):
                if value.value == "true":
                    return cst.Name("True")
                if value.value == "false":
                    return cst.Name("False")
                if value.value == "null":
                    return cst.Name("None")
            return value

        # Parse each update value as a CST expression
        parsed_updates: dict[str, cst.BaseExpression] = {}
        for name, expr_str in argUpdates.items():
            parsed = _parse_user_expression(expr_str, f"the value of {name} on @{annotationName}")
            parsed = _normalize_python_literal_name(parsed)
            parsed_updates[name] = parsed

        class _MergeArgs(cst.CSTTransformer):
            def __init__(self) -> None:
                self.updated = False

            def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
                if self.updated or original_node.name.value != entityType:
                    return updated_node
                decorators = list(updated_node.decorators)
                for idx, dec in enumerate(decorators):
                    dec_name = None
                    if isinstance(dec.decorator, cst.Name):
                        dec_name = dec.decorator.value
                    elif isinstance(dec.decorator, cst.Call) and isinstance(dec.decorator.func, cst.Name):
                        dec_name = dec.decorator.func.value
                    if dec_name != annotationName:
                        continue

                    call = dec.decorator
                    if not isinstance(call, cst.Call):
                        # bare @agent with no parens → create a Call with the updates
                        new_args = [
                            cst.Arg(
                                keyword=cst.Name(k),
                                value=v,
                                equal=cst.AssignEqual(
                                    whitespace_before=cst.SimpleWhitespace(""),
                                    whitespace_after=cst.SimpleWhitespace(""),
                                ),
                            )
                            for k, v in parsed_updates.items()
                        ]
                        # Add trailing comma + newline formatting
                        formatted = []
                        for i, a in enumerate(new_args):
                            comma = cst.Comma(whitespace_after=cst.ParenthesizedWhitespace(
                                indent=True,
                                last_line=cst.SimpleWhitespace("    "),
                            )) if i < len(new_args) - 1 else cst.MaybeSentinel.DEFAULT
                            formatted.append(a.with_changes(comma=comma))
                        new_call = cst.Call(func=cst.Name(dec_name), args=formatted)
                        decorators[idx] = cst.Decorator(decorator=new_call)
                        self.updated = True
                        break

                    # Merge into existing Call args
                    existing_args = list(call.args)
                    seen_keys: set[str] = set()
                    merged: list[cst.Arg] = []
                    for arg in existing_args:
                        key = arg.keyword.value if arg.keyword else None
                        if key and key in parsed_updates:
                            seen_keys.add(key)
                            merged.append(arg.with_changes(value=parsed_updates[key]))
                        else:
                            merged.append(arg)

                    # Append new args not in original
                    for k, v in parsed_updates.items():
                        if k not in seen_keys:
                            merged.append(cst.Arg(
                                keyword=cst.Name(k),
                                value=v,
                                equal=cst.AssignEqual(
                                    whitespace_before=cst.SimpleWhitespace(""),
                                    whitespace_after=cst.SimpleWhitespace(""),
                                ),
                            ))

                    # Fix trailing commas — ensure all but last have comma
                    final_args: list[cst.Arg] = []
                    for i, a in enumerate(merged):
                        if i < len(merged) - 1:
                            if isinstance(a.comma, cst.MaybeSentinel):
                                a = a.with_changes(comma=cst.Comma(whitespace_after=cst.ParenthesizedWhitespace(
                                    indent=True,
                                    last_line=cst.SimpleWhitespace("    "),
                                )))
                        final_args.append(a)

                    new_call = call.with_changes(args=final_args)
                    decorators[idx] = dec.with_changes(decorator=new_call)
                    self.updated = True
                    break

                return updated_node.with_changes(decorators=decorators)

        transformer = _MergeArgs()
        updated = self.module.visit(transformer)
        if not transformer.updated:
            raise RewriteError(message=f"Definition not found for annotation merge: {entityType}")
        self._set_module(updated)
        self._ensure_wfpy_decorator_import(cst.Name(annotationName))

    def _ensure_wfpy_decorator_import(self, decorator: cst.BaseExpression) -> None:
        """A decorator the edit wrote that is one of wfpy's (`@tool`,
        `@viewer`, ...) and that this file does not bind yet is imported from
        wfpy, as a created class's decorator is."""
        target = decorator.func if isinstance(decorator, cst.Call) else decorator
        if not isinstance(target, cst.Name):
            return  # `@wfpy.tool(...)` needs no name of its own
        import wfpy

        name = target.value
        if name in getattr(wfpy, "__all__", ()) and not self._binds(name):
            self._set_module(self._ensure_import(self.module, "wfpy", [name]))

    def remove_definition_annotation(
        self,
        *,
        entityType: str,
        annotationName: str,
    ) -> None:
        class _RemoveAnnot(cst.CSTTransformer):
            def __init__(self) -> None:
                self.updated = False

            def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
                if self.updated or original_node.name.value != entityType:
                    return updated_node
                decorators = list(updated_node.decorators)
                new_decorators: list[cst.Decorator] = []
                removed = False
                for dec in decorators:
                    name = None
                    if isinstance(dec.decorator, cst.Name):
                        name = dec.decorator.value
                    elif isinstance(dec.decorator, cst.Call) and isinstance(dec.decorator.func, cst.Name):
                        name = dec.decorator.func.value
                    if name == annotationName and not removed:
                        removed = True
                        continue
                    new_decorators.append(dec)
                if removed:
                    self.updated = True
                    return updated_node.with_changes(decorators=new_decorators)
                return updated_node

        transformer = _RemoveAnnot()
        updated = self.module.visit(transformer)
        if not transformer.updated:
            raise RewriteError(message=f"Annotation not found for removal: {entityType}.{annotationName}")
        self._set_module(updated)

    @staticmethod
    def _parse_parameter_text(text: str) -> cst.Param:
        """`name: type = default` (each part but the name optional) as a parameter."""
        source = str(text).strip()
        try:
            fn = cst.parse_statement(f"def _f({source}):\n    pass\n")
        except cst.ParserSyntaxError:
            fn = None
        params = fn.params if isinstance(fn, cst.FunctionDef) else None
        if (
            params is None
            or len(params.params) != 1
            or params.posonly_params
            or params.kwonly_params
            or isinstance(params.star_arg, cst.Param)
            or params.star_kwarg is not None
        ):
            raise RewriteError(
                message=f"the parameter is not written as `name: type = default`: {source!r}",
                diagnostic={"code": "invalid_expression", "expression": source},
            )
        param = params.params[0]
        if keyword.iskeyword(param.name.value):
            raise RewriteError(message=f"invalid parameter name: {param.name.value}", diagnostic={"code": "invalid_parameter_name"})
        return param

    def update_definition_parameter(
        self,
        *,
        entityType: str,
        parameterName: str,
        parameterText: str,
    ) -> None:
        """Add or change parameter `parameterName` of `entityType`, as
        `parameterText` (`name: type = default`) says.

        A class task's parameter is an annotated field with no default (one
        with a default is state, `core._classify_fields`): it is written as
        `name: type` in the class body, beside the class's other
        parameters, never as an `__init__`. A function's (a factory's, a
        workflow's) is a parameter of its signature."""
        param = self._parse_parameter_text(parameterText)
        top = {
            stmt.name.value: stmt
            for stmt in self.module.body
            if isinstance(stmt, (cst.FunctionDef, cst.ClassDef))
        }
        target = top.get(entityType)
        if isinstance(target, cst.ClassDef):
            if self._is_task_class(target):
                self._update_class_parameter(target, parameterName, param)
                return
            self._update_init_parameter(target, parameterName, param)
            return
        if isinstance(target, cst.FunctionDef):
            params = list(target.params.params)
            for idx, existing in enumerate(params):
                if existing.name.value == parameterName:
                    params[idx] = existing.with_changes(
                        name=param.name, annotation=param.annotation, default=param.default,
                        equal=param.equal,
                    )
                    break
            else:
                params.append(param.with_changes(comma=cst.MaybeSentinel.DEFAULT))
            self._replace_node(target, target.with_changes(params=target.params.with_changes(params=params)))
            return
        raise RewriteError(
            message=f"Definition parameter update failed: {entityType}.{parameterName}",
            diagnostic={"code": "definition_not_found", "entityType": entityType},
        )

    @staticmethod
    def _is_task_class(cls: cst.ClassDef) -> bool:
        return any(_decorator_name(dec) in TASK_TYPE_KINDS or _decorator_name(dec) == "context" for dec in cls.decorators)

    def _update_class_parameter(self, cls: cst.ClassDef, parameter_name: str, param: cst.Param) -> None:
        name = param.name.value
        if param.default is not None:
            raise RewriteError(
                message=(
                    f"'{name}' of '{cls.name.value}' would have a default, which makes a field of a task "
                    f"state, not a parameter: write `{name}: <type>` and give each node its value"
                ),
                diagnostic={"code": "parameter_has_default", "entityType": cls.name.value, "parameter": name},
            )
        annotation = param.annotation.annotation if param.annotation is not None else cst.Name("Any")
        body: list[cst.BaseStatement] = [stmt for stmt in cls.body.body if isinstance(stmt, cst.BaseStatement)]

        def field_of(stmt: cst.CSTNode) -> cst.AnnAssign | None:
            if isinstance(stmt, cst.SimpleStatementLine) and len(stmt.body) == 1 and isinstance(stmt.body[0], cst.AnnAssign):
                ann = stmt.body[0]
                return ann if isinstance(ann.target, cst.Name) else None
            return None

        parameters = [
            i for i, stmt in enumerate(body)
            if (f := field_of(stmt)) is not None and f.value is None and isinstance(f.target, cst.Name)
            and not f.target.value.startswith("_")
        ]
        # The end comment the class's parameters carry (`# parameter`), if they do.
        comment: cst.TrailingWhitespace | None = None
        for i in parameters:
            stmt = body[i]
            assert isinstance(stmt, cst.SimpleStatementLine)
            if stmt.trailing_whitespace.comment is not None:
                comment = cst.TrailingWhitespace(
                    whitespace=cst.SimpleWhitespace("  "),
                    comment=stmt.trailing_whitespace.comment,
                    newline=cst.Newline(),
                )
                break
        existing = next(
            (i for i, stmt in enumerate(body)
             if (f := field_of(stmt)) is not None and isinstance(f.target, cst.Name) and f.target.value == parameter_name),
            None,
        )
        if existing is not None:
            stmt = body[existing]
            assert isinstance(stmt, cst.SimpleStatementLine)
            old = stmt.body[0]
            assert isinstance(old, cst.AnnAssign)
            new_field = old.with_changes(
                target=cst.Name(name), annotation=cst.Annotation(annotation), value=None, equal=cst.MaybeSentinel.DEFAULT,
            )
            body[existing] = stmt.with_changes(body=[new_field])
        else:
            line = cst.SimpleStatementLine(
                [cst.AnnAssign(target=cst.Name(name), annotation=cst.Annotation(annotation), value=None)],
            )
            if comment is not None:
                line = line.with_changes(trailing_whitespace=comment)
            if parameters:
                at = parameters[-1] + 1
            else:
                at = self._first_non_docstring_index(body)
            body.insert(at, line)
            body = self._strip_placeholder_pass(body)
        self._replace_node(cls, cls.with_changes(body=cls.body.with_changes(body=body)))
        if param.annotation is None and "Any" not in self._module_level_names():
            self._set_module(self._ensure_any_import(self.module))

    def _update_init_parameter(self, cls: cst.ClassDef, parameter_name: str, param: cst.Param) -> None:
        """A plain class (no wfpy decorator) takes its parameters in `__init__`."""
        body: list[cst.BaseStatement] = [stmt for stmt in cls.body.body if isinstance(stmt, cst.BaseStatement)]
        init_idx = next(
            (i for i, stmt in enumerate(body) if isinstance(stmt, cst.FunctionDef) and stmt.name.value == "__init__"),
            None,
        )
        if init_idx is None:
            body.append(cst.FunctionDef(
                name=cst.Name("__init__"),
                params=cst.Parameters(params=[cst.Param(name=cst.Name("self")), param.with_changes(comma=cst.MaybeSentinel.DEFAULT)]),
                body=cst.IndentedBlock(body=[cst.SimpleStatementLine([cst.Pass()])]),
            ))
            body = self._strip_placeholder_pass(body)
        else:
            init_fn = body[init_idx]
            assert isinstance(init_fn, cst.FunctionDef)
            params = list(init_fn.params.params)
            for idx, existing in enumerate(params):
                if existing.name.value == parameter_name:
                    params[idx] = param.with_changes(comma=existing.comma)
                    break
            else:
                params.append(param.with_changes(comma=cst.MaybeSentinel.DEFAULT))
            body[init_idx] = init_fn.with_changes(params=init_fn.params.with_changes(params=params))
        self._replace_node(cls, cls.with_changes(body=cls.body.with_changes(body=body)))

    # ── A class's own ports: `class Ports:` of a task ────────────────────

    def _class_def(self, entity: str) -> cst.ClassDef | None:
        """The class `entity` of this file: at the top level, else the first
        of the name at any depth (a factory's)."""
        for stmt in self.module.body:
            if isinstance(stmt, cst.ClassDef) and stmt.name.value == entity:
                return stmt
        found: list[cst.ClassDef] = []

        class _Find(cst.CSTVisitor):
            def visit_ClassDef(self, node: cst.ClassDef) -> None:
                if node.name.value == entity and not found:
                    found.append(node)

        self.module.visit(_Find())
        return found[0] if found else None

    def _require_class(self, entity: str) -> cst.ClassDef:
        cls = self._class_def(entity)
        if cls is None:
            raise RewriteError(
                message=f"no class '{entity}' in {Path(self.file_path).name}",
                diagnostic={"code": "definition_not_found", "entityType": entity, "file": self.file_path},
            )
        return cls

    @staticmethod
    def _ports_class(cls: cst.ClassDef) -> cst.ClassDef | None:
        for stmt in cls.body.body:
            if isinstance(stmt, cst.ClassDef) and stmt.name.value == "Ports":
                return stmt
        return None

    def _class_port_names(self, cls: cst.ClassDef) -> list[str]:
        ports = self._ports_class(cls)
        names: list[str] = []
        for line in ports.body.body if ports is not None else []:
            if isinstance(line, cst.SimpleStatementLine):
                for small in line.body:
                    if isinstance(small, (cst.Assign, cst.AnnAssign)):
                        targets = [t.target for t in small.targets] if isinstance(small, cst.Assign) else [small.target]
                        names.extend(t.value for t in targets if isinstance(t, cst.Name))
        return names

    def _action_port_mentions(self, cls: cst.ClassDef, port: str) -> list[tuple[str, str, cst.CSTNode]]:
        """The `@action(consumes=..., produces=...)` of `cls` naming `port`:
        (action, keyword, the key)."""
        found: list[tuple[str, str, cst.CSTNode]] = []
        for stmt in cls.body.body:
            if not isinstance(stmt, cst.FunctionDef):
                continue
            for dec in stmt.decorators:
                if _decorator_name(dec) != "action" or not isinstance(dec.decorator, cst.Call):
                    continue
                for arg in dec.decorator.args:
                    if arg.keyword is None or arg.keyword.value not in ("consumes", "produces"):
                        continue
                    if isinstance(arg.value, cst.Dict):
                        for element in arg.value.elements:
                            if isinstance(element, cst.DictElement) and _string_value(element.key) == port:
                                found.append((stmt.name.value, arg.keyword.value, element.key))
        return found

    def _workflow_defs_in_file(self) -> list[cst.FunctionDef]:
        """Every `@workflow def` of this file, a factory's included."""
        found: list[cst.FunctionDef] = []
        matcher = self

        class _Find(cst.CSTVisitor):
            def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
                if matcher._is_workflow_def(node):
                    found.append(node)

        self.module.visit(_Find())
        return found

    def _workflow_defs_named(self, name: str) -> list[cst.FunctionDef]:
        """The `@workflow def name` of this file, in the order written (two
        factories may each build a workflow of the same name)."""
        return [fn for fn in self._workflow_defs_in_file() if fn.name.value == name]

    def _local_instances(self, fn: cst.FunctionDef, type_name: str, factory: bool = False) -> list[str]:
        """The names workflow `fn` binds instances of `type_name` to: `x =
        type_name(...)`, or with `factory`, `x = type_name(...)()` (a node of
        the workflow factory `type_name` builds)."""
        names: list[str] = []

        def callee(call: cst.BaseExpression) -> str | None:
            if not isinstance(call, cst.Call):
                return None
            func = call.func
            if factory:
                if call.args or not isinstance(func, cst.Call):
                    return None
                func = func.func
            return func.value if isinstance(func, cst.Name) else None

        class _Find(cst.CSTVisitor):
            def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
                return node is fn

            def visit_ClassDef(self, node: cst.ClassDef) -> bool:
                return False

            def visit_Assign(self, node: cst.Assign) -> None:
                if callee(node.value) == type_name:
                    for target in node.targets:
                        if isinstance(target.target, cst.Name) and target.target.value not in names:
                            names.append(target.target.value)

        fn.visit(_Find())
        return names

    def instance_port_mentions(
        self,
        workflow: str,
        instances: Iterable[str],
        port: str,
        new: str | None = None,
        ordinal: int | None = None,
    ) -> tuple[list[str], list[tuple[str, int | None]]]:
        """In workflow `workflow` -- the `ordinal`-th `@workflow def` of the
        name in this file, or every one of the name -- the mentions of port
        `port` of `instances` (`x.Port`, `x.Ports.Port`, `getattr(x,
        "Port")`): the instances a `connect` names it of, and those
        mentioning it otherwise (with the line). With `new`, every mention
        is renamed to it."""
        targets = self._workflow_defs_named(workflow)
        if ordinal is not None:
            targets = targets[ordinal : ordinal + 1]
        if not targets:
            raise RewriteError(
                message=f"no workflow '{workflow}' in {Path(self.file_path).name}",
                diagnostic={"code": "workflow_not_found", "workflow": workflow, "file": self.file_path},
            )
        names = set(instances)
        connected: list[str] = []
        used: list[tuple[str, int | None]] = []
        engine = self

        class _Mentions(_WorkflowBodyTransformer):
            def __init__(self, root: cst.FunctionDef, rename: bool) -> None:
                super().__init__(root)
                self._connects = 0
                self._rename = rename

            def _seen(self, name: str, node: cst.CSTNode) -> None:
                if self._rename:
                    return
                if self._connects > 0:
                    if name not in connected:
                        connected.append(name)
                elif name not in [n for n, _ in used]:
                    used.append((name, engine._line_of(node)))

            def visit_Call(self, node: cst.Call) -> None:
                if _is_connect_call(node):
                    self._connects += 1

            def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
                if _is_connect_call(original_node):
                    self._connects -= 1
                args = original_node.args
                if (
                    isinstance(original_node.func, cst.Name)
                    and original_node.func.value == "getattr"
                    and len(args) >= 2
                    and isinstance(args[0].value, cst.Name)
                    and args[0].value.value in names
                    and _string_value(args[1].value) == port
                ):
                    self._seen(args[0].value.value, original_node)
                    if self._rename and new is not None and isinstance(updated_node.args[1].value, cst.SimpleString):
                        renamed = list(updated_node.args)
                        renamed[1] = renamed[1].with_changes(value=_quoted_like(updated_node.args[1].value, new))
                        return updated_node.with_changes(args=renamed)
                return updated_node

            def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.Attribute:
                if original_node.attr.value != port:
                    return updated_node
                value = original_node.value
                instance: str | None = None
                if isinstance(value, cst.Name) and value.value in names:
                    instance = value.value
                elif (
                    isinstance(value, cst.Attribute)
                    and value.attr.value == "Ports"
                    and isinstance(value.value, cst.Name)
                    and value.value.value in names
                ):
                    instance = value.value.value
                if instance is None:
                    return updated_node
                self._seen(instance, original_node)
                if self._rename and new is not None:
                    return updated_node.with_changes(attr=cst.Name(new))
                return updated_node

        # Read on the module as it is, so each mention has its line.
        for target in targets:
            target.visit(_Mentions(target, rename=False))
        if new is not None and (connected or used):

            class _Apply(cst.CSTTransformer):
                def leave_FunctionDef(
                    self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
                ) -> cst.FunctionDef:
                    if not any(original_node is target for target in targets):
                        return updated_node
                    renamed = updated_node.visit(_Mentions(updated_node, rename=True))
                    assert isinstance(renamed, cst.FunctionDef)
                    return renamed

            self._set_module(self.module.visit(_Apply()))
        return connected, used

    def _companion(self, file_path: str) -> "RewriteEngine":
        """The engine of another file this op edits, saved with this one."""
        resolved = str(Path(file_path).resolve())
        if resolved == str(Path(self.file_path).resolve()):
            return self
        companions = self.__dict__.setdefault("_companions", {})
        if resolved not in companions:
            module, source_text, _revision = load_module(resolved)
            companions[resolved] = RewriteEngine(resolved, module, source_text)
        engine: RewriteEngine = companions[resolved]
        return engine

    def changed_companions(self) -> list["RewriteEngine"]:
        return [engine for engine in self.__dict__.get("_companions", {}).values() if engine._updated]

    def _port_uses(self, usedIn: Any) -> list[tuple[str, str, list[str]]]:
        """`usedIn` as the IDE sends it (`[{file, workflow, instances}]`)."""
        uses: list[tuple[str, str, list[str]]] = []
        for entry in usedIn or []:
            if not isinstance(entry, dict):
                continue
            file = str(entry.get("file", "")).strip()
            workflow = str(entry.get("workflow", "")).strip()
            instances = [str(n).strip() for n in entry.get("instances") or [] if str(n).strip().isidentifier()]
            if file and workflow and instances:
                uses.append((file, workflow, instances))
        return uses

    def _this_files_uses(
        self, type_name: str, factory: bool = False, skip: cst.FunctionDef | None = None
    ) -> list[_Use]:
        """This file's workflows with instances of `type_name` (a class, a
        workflow, or with `factory` the workflow factory `type_name`
        builds), each by its name and its place among those of the name."""
        uses: list[_Use] = []
        seen_names: dict[str, int] = {}
        for fn in self._workflow_defs_in_file():
            ordinal = seen_names.get(fn.name.value, 0)
            seen_names[fn.name.value] = ordinal + 1
            if fn is skip:
                continue
            names = self._local_instances(fn, type_name, factory)
            if names:
                uses.append(_Use(self, fn.name.value, names, ordinal))
        return uses

    def _merged_uses(
        self, own: list[_Use], usedIn: Any, skip: tuple[str, str] | None = None
    ) -> list[_Use]:
        """This file's uses (`own`) and those the IDE names (`usedIn`), each
        workflow once. `skip` is a (file, workflow) not to follow: the one
        being edited."""
        uses = list(own)
        here = str(Path(self.file_path).resolve())
        for file, workflow, names in self._port_uses(usedIn):
            resolved = str(Path(file).resolve())
            if skip is not None and (resolved, workflow) == (str(Path(skip[0]).resolve()), skip[1]):
                continue
            same = [
                use for use in uses
                if str(Path(use.engine.file_path).resolve()) == resolved and use.workflow == workflow
            ]
            if same:
                for use in same:
                    use.instances.extend(n for n in names if n not in use.instances)
                continue
            if resolved == here:
                uses.append(_Use(self, workflow, list(names), None))
                continue
            try:
                engine = self._companion(file)
            except (OSError, cst.ParserSyntaxError, RewriteError) as exc:
                raise RewriteError(
                    message=f"{Path(file).name} cannot be read, so workflow '{workflow}' cannot be followed: {exc}",
                    diagnostic={"code": "port_uses_unreadable", "file": file, "workflow": workflow},
                ) from None
            uses.append(_Use(engine, workflow, list(names), None))
        return uses

    def _all_port_uses(self, entity: str, usedIn: Any) -> list[_Use]:
        """Every workflow an edit of a port of class `entity` has to look
        into: this file's, and those the IDE names (`usedIn`), each once."""
        return self._merged_uses(self._this_files_uses(entity), usedIn)

    def _workflow_port_uses(self, fn: cst.FunctionDef, usedIn: Any) -> list[_Use]:
        """Every workflow using workflow `fn`: this file's (`x = fn()`, or
        `x = factory(...)()` for the workflow a factory builds), and those
        the IDE names (`usedIn`)."""
        enclosing = self._enclosing_functions(fn)
        if enclosing:
            own = self._this_files_uses(enclosing[0].name.value, factory=True, skip=fn)
        else:
            own = self._this_files_uses(fn.name.value, skip=fn)
        return self._merged_uses(own, usedIn, skip=(self.file_path, fn.name.value))

    def _refuse_port_in_use(self, uses: list[_Use], port: str, **diagnostic: Any) -> None:
        """Refuse while a workflow of `uses` names port `port` of its instances."""
        for use in uses:
            connected, used = use.engine.instance_port_mentions(use.workflow, use.instances, port, ordinal=use.ordinal)
            if not (connected or used):
                continue
            names = [*connected, *(n for n, _ in used if n not in connected)]
            listed = ", ".join(repr(n) for n in names)
            if used:
                line = used[0][1]
                where = f" (line {line} of {Path(use.engine.file_path).name})" if line else ""
                message = (
                    f"port '{port}' of {listed} is used in workflow '{use.workflow}'{where}: "
                    "take it out of the workflow first"
                )
            else:
                message = f"port '{port}' of {listed} is connected in workflow '{use.workflow}': delete its connections first"
            raise RewriteError(
                message=message,
                diagnostic={
                    "code": "port_in_use",
                    **diagnostic,
                    "portName": port,
                    "workflow": use.workflow,
                    "file": use.engine.file_path,
                    "instances": names,
                },
            )

    def create_entity_port(
        self,
        *,
        entityType: str,
        portDirection: str,
        portName: str,
        portType: str,
    ) -> None:
        self._ensure_identifier(portName)
        port_type = _parse_user_expression(portType, f"the type of port '{portName}'")
        direction = _PORT_DIRECTIONS.get(str(portDirection), None)
        if direction is None:
            raise RewriteError(message=f"Invalid port direction: {portDirection}", diagnostic={"code": "invalid_direction"})
        cls = self._require_class(entityType)
        if portName in self._class_port_names(cls):
            raise RewriteError(
                message=f"'{entityType}' has a port '{portName}' already",
                diagnostic={"code": "port_exists", "entityType": entityType, "port": portName},
            )
        port_call = cst.Call(
            func=cst.Subscript(
                value=cst.Name("Port"),
                slice=[cst.SubscriptElement(slice=cst.Index(value=port_type))],
            ),
            # Said, not left to `Port`'s default, which is "inout".
            args=[_keyword_arg("direction", cst.SimpleString(f'"{direction}"'))],
        )
        port_stmt = cst.SimpleStatementLine([
            cst.Assign(targets=[cst.AssignTarget(target=cst.Name(portName))], value=port_call)
        ])
        body = list(cls.body.body)
        ports_cls = self._ports_class(cls)
        if ports_cls is None:
            body.append(cst.ClassDef(
                name=cst.Name("Ports"),
                body=cst.IndentedBlock(body=[port_stmt]),
                leading_lines=[cst.EmptyLine()] if body else [],
            ))
        else:
            index = body.index(ports_cls)
            ports_body = self._strip_placeholder_pass([*ports_cls.body.body, port_stmt])
            body[index] = ports_cls.with_changes(body=ports_cls.body.with_changes(body=ports_body))
        self._replace_node(cls, cls.with_changes(body=cls.body.with_changes(body=body)))
        needed = ["Port"] if "Port" not in self._module_level_names() else []
        if needed:
            self._set_module(self._ensure_import(self.module, "wfpy", needed))
        if "Any" in portType:
            self._set_module(self._ensure_any_import(self.module))

    def delete_entity_port(
        self,
        *,
        entityType: str,
        portDirection: str = "",
        portName: str,
        usedIn: Any = None,
    ) -> None:
        """Delete port `portName` of class `entityType`: refused while an
        action of the class reads or writes it, or a workflow of this file
        or one the IDE names (`usedIn`) connects or otherwise names it."""
        cls = self._require_class(entityType)
        if portName not in self._class_port_names(cls):
            raise RewriteError(
                message=f"'{entityType}' has no port '{portName}'",
                diagnostic={"code": "port_not_found", "entityType": entityType, "port": portName},
            )
        actions = self._action_port_mentions(cls, portName)
        if actions:
            action_name, keyword_name, key = actions[0]
            line = self._line_of(key)
            verb = "read" if keyword_name == "consumes" else "written"
            raise RewriteError(
                message=(
                    f"port '{portName}' of '{entityType}' is {verb} by action '{action_name}'"
                    f"{f' (line {line})' if line else ''}: take it out of the action first"
                ),
                diagnostic={"code": "port_in_use", "entityType": entityType, "portName": portName, "action": action_name},
            )
        self._refuse_port_in_use(self._all_port_uses(entityType, usedIn), portName, entityType=entityType)
        ports_cls = self._ports_class(cls)
        assert ports_cls is not None
        kept: list[cst.BaseStatement] = []
        for line_stmt in ports_cls.body.body:
            if isinstance(line_stmt, cst.SimpleStatementLine):
                smalls = [
                    small for small in line_stmt.body
                    if not (
                        isinstance(small, (cst.Assign, cst.AnnAssign))
                        and any(
                            isinstance(t, cst.Name) and t.value == portName
                            for t in ([x.target for x in small.targets] if isinstance(small, cst.Assign) else [small.target])
                        )
                    )
                ]
                if not smalls:
                    continue
                if len(smalls) != len(line_stmt.body):
                    line_stmt = line_stmt.with_changes(body=smalls)
            if isinstance(line_stmt, cst.BaseStatement):
                kept.append(line_stmt)
        new_ports = ports_cls.with_changes(
            body=ports_cls.body.with_changes(body=kept or [cst.SimpleStatementLine([cst.Pass()])])
        )
        self._replace_node(ports_cls, new_ports)

    def _body_assigns(self, fn: cst.FunctionDef, name: str) -> bool:
        """Whether workflow `fn`'s body binds `name` (`name = ...`): an instance."""
        found = False

        class _Assigns(cst.CSTVisitor):
            def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
                return node is fn

            def visit_AssignTarget(self, node: cst.AssignTarget) -> None:
                nonlocal found
                if isinstance(node.target, cst.Name) and node.target.value == name:
                    found = True

        fn.visit(_Assigns())
        return found

    def rename_node(self, *, workflow: str | None = None, old: str, new: str, usedIn: Any = None) -> None:
        self._ensure_identifier(new)
        fn, _ = self._get_workflow_body(workflow)
        if not self._body_assigns(fn, old) and self._is_workflow_port(fn, old):
            # The diagram draws a workflow's own port as a node of its own,
            # and renames it as one. The workflows that use this one
            # (`usedIn`: their instances of it) have their ends follow.
            # The uses are found before the edit, on the module as it is;
            # the rename refuses (port_exists, port_used_by_name) before it
            # writes anything, and the uses follow only once it has.
            uses = self._workflow_port_uses(fn, usedIn)
            self.rename_workflow_port(fn, old, new)
            for use in uses:
                use.engine.instance_port_mentions(use.workflow, use.instances, old, new, ordinal=use.ordinal)
            return
        if new == old:
            return
        # The new name is the workflow's alone: not another of its names
        # (`front = ...` twice), not one it reads (`Text = Text(...)`).
        if self._body_assigns(fn, old):
            self._refuse_taken(fn, new)
            self._refuse_shadowing(fn, new, self._instance_type_name(fn, old))

        class _RenameNodeTransformer(_WorkflowBodyTransformer):
            def __init__(self, fn_node: cst.FunctionDef, old_name: str, new_name: str) -> None:
                super().__init__(fn_node)
                self._old = old_name
                self._new = new_name
                self._in_target_fn = True
                self.renamed = False

            def leave_Assign(self, original_node: cst.Assign, updated_node: cst.Assign) -> cst.Assign:
                if not self._in_target_fn:
                    return updated_node
                if not isinstance(updated_node.value, cst.Call):
                    return updated_node
                changed = False
                new_targets: list[cst.AssignTarget] = []
                for tgt in updated_node.targets:
                    target = tgt.target
                    if isinstance(target, cst.Name) and target.value == self._old:
                        target = target.with_changes(value=self._new)
                        changed = True
                    new_targets.append(tgt.with_changes(target=target))
                if changed:
                    self.renamed = True
                    return updated_node.with_changes(targets=new_targets)
                return updated_node

            def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.Attribute:
                if not self._in_target_fn:
                    return updated_node
                if isinstance(updated_node.value, cst.Name) and updated_node.value.value == self._old:
                    self.renamed = True
                    return updated_node.with_changes(value=updated_node.value.with_changes(value=self._new))
                return updated_node

            def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
                if not self._in_target_fn:
                    return updated_node
                if not isinstance(updated_node.func, cst.Name) or updated_node.func.value != "connect":
                    return updated_node
                changed = False
                new_args = list(updated_node.args)
                for idx in (0, 1):
                    if idx >= len(new_args):
                        continue
                    expr = new_args[idx].value
                    if isinstance(expr, cst.Name) and expr.value == self._old:
                        new_args[idx] = new_args[idx].with_changes(value=expr.with_changes(value=self._new))
                        changed = True
                    elif isinstance(expr, cst.Attribute) and isinstance(expr.value, cst.Name) and expr.value.value == self._old:
                        new_value = expr.value.with_changes(value=self._new)
                        new_args[idx] = new_args[idx].with_changes(value=expr.with_changes(value=new_value))
                        changed = True
                if changed:
                    self.renamed = True
                    return updated_node.with_changes(args=new_args)
                return updated_node

        transformer = _RenameNodeTransformer(fn, old, new)
        updated = fn.visit(transformer)
        if not transformer.renamed or not isinstance(updated, cst.FunctionDef):
            raise RewriteError(message=f"Node not found: {old}", diagnostic={"code": "node_not_found"})
        self._replace_node(fn, updated)

    def rename_port(
        self,
        *,
        entity: str,
        portDirection: str | None = None,
        portName: str | None = None,
        newValue: str | None = None,
        usedIn: Any = None,
        port_direction: str | None = None,
        port_name: str | None = None,
        new_value: str | None = None,
    ) -> None:
        """Rename port `portName` of class `entity` to `newValue`: its
        `class Ports` entry, the actions' `consumes` / `produces` keys and
        the keys of the dicts they return, and every mention of it on an
        instance in this file's workflows and in those the IDE names
        (`usedIn`, in other files too). Refused, leaving every file as it
        is, where a mention cannot be followed."""
        old = str(portName if portName is not None else port_name or "")
        new = str(newValue if newValue is not None else new_value or "").strip()
        direction = str(portDirection if portDirection is not None else port_direction or "")
        self._ensure_identifier(new)
        cls = self._require_class(entity)
        ports = self._class_port_names(cls)
        if old not in ports:
            raise RewriteError(
                message=f"'{entity}' has no port '{old}'",
                diagnostic={"code": "port_not_found", "entityType": entity, "port": old},
            )
        if new == old:
            return
        if new in ports:
            raise RewriteError(
                message=f"'{entity}' has a port '{new}' already",
                diagnostic={"code": "port_exists", "entityType": entity, "port": new},
            )
        uses = self._all_port_uses(entity, usedIn)
        class_strings = self._class_string_mentions(cls, old)
        if class_strings:
            line = self._line_of(class_strings[0])
            raise RewriteError(
                message=(
                    f"port '{old}' of '{entity}' is named by a string{f' at line {line}' if line else ''}, "
                    "which this rename cannot follow: rename it there"
                ),
                diagnostic={"code": "port_used_by_name", "entityType": entity, "port": old},
            )
        # The declaration first, found by its name (`direction` only tells
        # two of the name apart): a use renamed without it would name a port
        # the class no longer has.
        transformer = _PortRenameTransformer(cls, direction, old, new)
        renamed = self.module.visit(transformer)
        if not transformer.found:
            raise RewriteError(
                message=f"port '{old}' of '{entity}' is not declared in its `class Ports`, so it cannot be renamed here",
                diagnostic={"code": "port_not_found", "entityType": entity, "port": old},
            )
        self._set_module(renamed)
        for use in uses:
            use.engine.instance_port_mentions(use.workflow, use.instances, old, new, ordinal=use.ordinal)

    def _class_string_mentions(self, cls: cst.ClassDef, port: str) -> list[cst.SimpleString]:
        """Strings `"port"` class `cls` uses as a port's name and a rename
        does not follow: a `getattr(self, "port")`, a key of a dict or of a
        subscript (`tokens["port"]`), a name a `for` loops over. Its
        actions' `consumes` / `produces` keys and the keys of the dicts they
        return are followed; a string that is not a port's name (the
        `direction="out"` of a declaration, a docstring) is not one."""
        followed: set[int] = set()
        for stmt in cls.body.body:
            if not isinstance(stmt, cst.FunctionDef):
                continue
            is_action = any(_decorator_name(d) == "action" for d in stmt.decorators)
            for dec in stmt.decorators:
                if _decorator_name(dec) == "action" and isinstance(dec.decorator, cst.Call):
                    for arg in dec.decorator.args:
                        if arg.keyword is not None and arg.keyword.value in ("consumes", "produces") and isinstance(arg.value, cst.Dict):
                            followed.update(
                                id(el.key) for el in arg.value.elements if isinstance(el, cst.DictElement)
                            )
            if is_action:

                class _Returns(cst.CSTVisitor):
                    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
                        return node is stmt

                    def visit_Return(self, node: cst.Return) -> None:
                        if isinstance(node.value, cst.Dict):
                            followed.update(
                                id(el.key) for el in node.value.elements if isinstance(el, cst.DictElement)
                            )

                stmt.visit(_Returns())
        return [string for string in _port_name_strings(cls, port) if id(string) not in followed]

    def update_port_type(
        self,
        *,
        entity: str,
        portDirection: str | None = None,
        portName: str | None = None,
        newValue: str | None = None,
        usedIn: Any = None,
        port_direction: str | None = None,
        port_name: str | None = None,
        new_value: str | None = None,
    ) -> None:
        name = str(portName if portName is not None else port_name or "")
        value = str(newValue if newValue is not None else new_value or "")
        direction = str(portDirection if portDirection is not None else port_direction or "")
        _parse_user_expression(value, f"the type of port '{name}'")
        cls = self._require_class(entity)
        if name not in self._class_port_names(cls):
            raise RewriteError(
                message=f"'{entity}' has no port '{name}'",
                diagnostic={"code": "port_not_found", "entityType": entity, "port": name},
            )
        transformer = _PortTypeTransformer(cls, direction, name, value)
        updated = self.module.visit(transformer)
        if not transformer.found:
            raise RewriteError(
                message=f"port '{name}' of '{entity}' is not written as `Port[...](...)`, so its type cannot be changed here",
                diagnostic={"code": "unsupported_port_form", "entityType": entity, "port": name},
            )
        self._set_module(updated)
        if "Any" in value:
            self._set_module(self._ensure_any_import(self.module))

    def create_if(
        self,
        *,
        workflow: str | None = None,
        name: str,
        condition_expr: str,
        scope_control: str | None = None,
        scope_branch: str | None = None,
    ) -> None:
        self._ensure_identifier(name)
        fn, body = self._get_workflow_body(workflow)
        target_block = self._find_scope_block(body, scope_control, scope_branch)
        target_body = list(target_block.body)
        assign = cst.Assign(
            targets=[cst.AssignTarget(target=cst.Name(name))],
            value=cst.Call(func=cst.Attribute(value=cst.Name("wfpy"), attr=cst.Name("if_")), args=[cst.Arg(value=_parse_user_expression(condition_expr, "the condition"))]),
        )
        then_block = cst.With(
            items=[cst.WithItem(item=cst.Attribute(value=cst.Name(name), attr=cst.Name("then")))],
            body=cst.IndentedBlock(body=[cst.SimpleStatementLine([cst.Pass()])]),
        )
        else_block = cst.With(
            items=[cst.WithItem(item=cst.Attribute(value=cst.Name(name), attr=cst.Name("else_")))],
            body=cst.IndentedBlock(body=[cst.SimpleStatementLine([cst.Pass()])]),
        )
        stmts = [cst.SimpleStatementLine([assign]), then_block, else_block]
        new_stmt_body = self._insert_statement(target_body, stmts[0], "node")
        new_stmt_body = self._insert_statement(new_stmt_body, stmts[1], "node")
        new_stmt_body = self._insert_statement(new_stmt_body, stmts[2], "node")
        new_block = target_block.with_changes(body=new_stmt_body)
        self._update_scope_body(fn, scope_control, scope_branch, new_block)

    def create_loop(
        self,
        *,
        workflow: str | None = None,
        name: str,
        iterable_expr: str,
        scope_control: str | None = None,
        scope_branch: str | None = None,
    ) -> None:
        self._ensure_identifier(name)
        fn, body = self._get_workflow_body(workflow)
        target_block = self._find_scope_block(body, scope_control, scope_branch)
        target_body = list(target_block.body)
        assign = cst.Assign(
            targets=[cst.AssignTarget(target=cst.Name(name))],
            value=cst.Call(func=cst.Attribute(value=cst.Name("wfpy"), attr=cst.Name("loop")), args=[cst.Arg(value=_parse_user_expression(iterable_expr, "what the loop iterates over"))]),
        )
        loop_block = cst.With(
            items=[cst.WithItem(item=cst.Name(name))],
            body=cst.IndentedBlock(body=[cst.SimpleStatementLine([cst.Pass()])]),
        )
        new_stmt_body = self._insert_statement(target_body, cst.SimpleStatementLine([assign]), "node")
        new_stmt_body = self._insert_statement(new_stmt_body, loop_block, "node")
        new_block = target_block.with_changes(body=new_stmt_body)
        self._update_scope_body(fn, scope_control, scope_branch, new_block)

    def wrap_in_if(self, *, workflow: str | None = None, node_names: list[str], condition_expr: str) -> None:
        self._wrap_in_control("if", workflow, node_names, condition_expr)

    def wrap_in_loop(self, *, workflow: str | None = None, node_names: list[str], iterable_expr: str) -> None:
        self._wrap_in_control("loop", workflow, node_names, iterable_expr)

    def unwrap_control(self, *, workflow: str | None = None, name: str) -> None:
        fn, _ = self._get_workflow_body(workflow)

        class _Unwrap(_WorkflowBodyTransformer):
            def __init__(self, matcher: RewriteEngine) -> None:
                super().__init__(fn)
                self._matcher = matcher
                self.unwrapped = False

            def leave_With(
                self,
                original_node: cst.With,
                updated_node: cst.With,
            ) -> cst.BaseStatement | cst.FlattenSentinel[cst.BaseStatement]:
                if self._matcher._matches_control_with_any(original_node, name):
                    self.unwrapped = True
                    statements = [s for s in updated_node.body.body if isinstance(s, cst.BaseStatement)]
                    return cst.FlattenSentinel(statements)
                return updated_node

        transformer = _Unwrap(self)
        updated = fn.visit(transformer)
        if not transformer.unwrapped or not isinstance(updated, cst.FunctionDef):
            raise RewriteError(message=f"Control scope not found for {name}")
        self._replace_node(fn, updated)

    def move_node(self, *, workflow: str | None = None, name: str, target_control: str, target_scope: str) -> None:
        source_fn, _ = self._get_workflow_body(workflow)

        class _ExtractAssign(_WorkflowBodyTransformer):
            def __init__(self) -> None:
                super().__init__(source_fn)
                self.extracted: cst.BaseStatement | None = None

            def leave_SimpleStatementLine(
                self,
                original_node: cst.SimpleStatementLine,
                updated_node: cst.SimpleStatementLine,
            ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
                if self.extracted is not None:
                    return updated_node
                if any(
                    m.matches(s, m.Assign(targets=[m.AssignTarget(target=m.Name(name))]))
                    for s in updated_node.body
                ):
                    self.extracted = updated_node
                    return cst.RemovalSentinel.REMOVE
                return updated_node

        extractor = _ExtractAssign()
        updated = source_fn.visit(extractor)
        if extractor.extracted is None or not isinstance(updated, cst.FunctionDef):
            raise RewriteError(message=f"Node not found: {name}")
        self._replace_node(source_fn, updated)

        fn, body = self._get_workflow_body(workflow)
        target_block = self._find_scope_block(body, target_control, target_scope)
        target_body = list(target_block.body)
        new_stmt_body = self._insert_statement(target_body, extractor.extracted, "node")
        new_block = target_block.with_changes(body=new_stmt_body)
        self._update_scope_body(fn, target_control, target_scope, new_block)

    def _wrap_in_control(self, kind: str, workflow: str | None, node_names: list[str], expr: str) -> None:
        if not node_names:
            raise RewriteError(message="node_names is required")
        fn, body = self._get_workflow_body(workflow)
        extracted: list[cst.BaseStatement] = []
        new_body: list[cst.BaseStatement] = []
        for stmt in body.body:
            if isinstance(stmt, cst.SimpleStatementLine) and any(
                m.matches(s, m.Assign(targets=[m.AssignTarget(target=m.Name(n))]))
                for n in node_names
                for s in stmt.body
            ):
                extracted.append(stmt)
                continue
            new_body.append(stmt)

        if not extracted:
            raise RewriteError(message="No matching nodes found")

        ctrl_name = self._unique_name(f"{kind}_1", self._collect_assigned_names(fn))
        if kind == "if":
            assign = cst.Assign(
                targets=[cst.AssignTarget(target=cst.Name(ctrl_name))],
                value=cst.Call(
                    func=cst.Attribute(value=cst.Name("wfpy"), attr=cst.Name("if_")),
                    args=[cst.Arg(value=_parse_user_expression(expr, "the condition" if kind == "if" else "what the loop iterates over"))],
                ),
            )
            with_block = cst.With(
                items=[cst.WithItem(item=cst.Attribute(value=cst.Name(ctrl_name), attr=cst.Name("then")))],
                body=cst.IndentedBlock(body=extracted),
            )
            new_body.extend([cst.SimpleStatementLine([assign]), with_block])
        else:
            assign = cst.Assign(
                targets=[cst.AssignTarget(target=cst.Name(ctrl_name))],
                value=cst.Call(
                    func=cst.Attribute(value=cst.Name("wfpy"), attr=cst.Name("loop")),
                    args=[cst.Arg(value=_parse_user_expression(expr, "the condition" if kind == "if" else "what the loop iterates over"))],
                ),
            )
            with_block = cst.With(
                items=[cst.WithItem(item=cst.Name(ctrl_name))],
                body=cst.IndentedBlock(body=extracted),
            )
            new_body.extend([cst.SimpleStatementLine([assign]), with_block])

        self._update_function_body(fn, body.with_changes(body=new_body))


class _PortBaseTransformer(cst.CSTTransformer):
    """Edits the declaration of port `port_name` in the `class Ports` of the
    class `target` (the node itself: a file may have two classes of the
    name, each a factory's). The declaration is found by its name, as
    `Name = Port[...](...)` or `Name: ... = ...`; `direction` only tells two
    declarations of the name apart, and never skips the one there is."""

    def __init__(self, target: cst.ClassDef, direction: str, port_name: str) -> None:
        self.target = target
        self.entity = target.name.value
        self.direction = _PORT_DIRECTIONS.get(direction, direction) if direction else ""
        self.port_name = port_name
        self._in_entity = False
        self._in_ports_class = False
        self._action_depth = 0
        self.found = False
        self._chosen = self._declarations()

    def _declarations(self) -> set[int]:
        """The declarations of the port: by name; by direction too where the
        name is declared more than once."""
        ports = next(
            (stmt for stmt in self.target.body.body if isinstance(stmt, cst.ClassDef) and stmt.name.value == "Ports"),
            None,
        )
        declared: list[cst.BaseSmallStatement] = []
        for line in ports.body.body if ports is not None else []:
            if isinstance(line, cst.SimpleStatementLine):
                for small in line.body:
                    if self._declares(small):
                        declared.append(small)
        if len(declared) > 1 and self.direction:
            by_direction = [small for small in declared if self._direction_of(small) == self.direction]
            if by_direction:
                declared = by_direction
        return {id(small) for small in declared}

    def _declares(self, node: cst.CSTNode) -> bool:
        if isinstance(node, cst.Assign):
            targets = [t.target for t in node.targets]
        elif isinstance(node, cst.AnnAssign):
            targets = [node.target]
        else:
            return False
        return any(isinstance(t, cst.Name) and t.value == self.port_name for t in targets)

    @staticmethod
    def _direction_of(node: cst.BaseSmallStatement) -> str:
        value = getattr(node, "value", None)
        if isinstance(value, cst.Call):
            for arg in value.args:
                if arg.keyword is not None and arg.keyword.value == "direction":
                    return _string_value(arg.value) or ""
        return "inout"

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        if not self._in_entity:
            if node is self.target:
                self._in_entity = True
                return True
            return True  # the target may be nested (a factory's class)
        if node.name.value == "Ports":
            self._in_ports_class = True
            return True
        return False

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        if original_node.name.value == "Ports" and self._in_ports_class:
            self._in_ports_class = False
        if original_node is self.target:
            self._in_entity = False
        return updated_node

    def _matches_port_assign(self, node: cst.CSTNode) -> bool:
        return self._in_ports_class and id(node) in self._chosen


class _PortRenameTransformer(_PortBaseTransformer):
    def __init__(self, target: cst.ClassDef, direction: str, port_name: str, new_name: str) -> None:
        super().__init__(target, direction, port_name)
        self.new_name = new_name

    def leave_Assign(self, original_node: cst.Assign, updated_node: cst.Assign) -> cst.Assign:
        if not self._matches_port_assign(original_node):
            return updated_node
        self.found = True
        targets = [
            t.with_changes(target=cst.Name(self.new_name))
            if isinstance(t.target, cst.Name) and t.target.value == self.port_name
            else t
            for t in updated_node.targets
        ]
        return updated_node.with_changes(targets=targets)

    def leave_AnnAssign(self, original_node: cst.AnnAssign, updated_node: cst.AnnAssign) -> cst.AnnAssign:
        if not self._matches_port_assign(original_node):
            return updated_node
        self.found = True
        return updated_node.with_changes(target=cst.Name(self.new_name))

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        if self._in_entity and not self._in_ports_class and (
            any(_decorator_name(d) == "action" for d in node.decorators) or node.name.value == "action"
        ):
            self._action_depth += 1
        return True

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
        if self._in_entity and not self._in_ports_class and (
            any(_decorator_name(d) == "action" for d in original_node.decorators) or original_node.name.value == "action"
        ):
            self._action_depth -= 1
        return updated_node

    def leave_Decorator(self, original_node: cst.Decorator, updated_node: cst.Decorator) -> cst.Decorator:
        """`@action(consumes={"Old": 1}, produces=...)` of the class."""
        if not self._in_entity or _decorator_name(original_node) != "action":
            return updated_node
        call = updated_node.decorator
        if not isinstance(call, cst.Call):
            return updated_node
        args = [
            arg.with_changes(value=self._renamed_keys(arg.value))
            if arg.keyword is not None and arg.keyword.value in ("consumes", "produces") and isinstance(arg.value, cst.Dict)
            else arg
            for arg in call.args
        ]
        return updated_node.with_changes(decorator=call.with_changes(args=args))

    def leave_Return(self, original_node: cst.Return, updated_node: cst.Return) -> cst.Return:
        """`return {"Old": ...}` of an action of the class."""
        if self._in_entity and self._action_depth > 0 and isinstance(updated_node.value, cst.Dict):
            return updated_node.with_changes(value=self._renamed_keys(updated_node.value))
        return updated_node

    def _renamed_keys(self, mapping: cst.Dict) -> cst.Dict:
        elements = [
            element.with_changes(key=_quoted_like(element.key, self.new_name))
            if isinstance(element, cst.DictElement)
            and isinstance(element.key, cst.SimpleString)
            and _string_value(element.key) == self.port_name
            else element
            for element in mapping.elements
        ]
        return mapping.with_changes(elements=elements)

    def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.Attribute:
        """`Entity.Old`, read off the class."""
        if isinstance(original_node.value, cst.Name):
            if original_node.value.value == self.entity and original_node.attr.value == self.port_name:
                return updated_node.with_changes(attr=cst.Name(self.new_name))
        return updated_node


class _PortTypeTransformer(_PortBaseTransformer):
    def __init__(self, target: cst.ClassDef, direction: str, port_name: str, new_type: str) -> None:
        super().__init__(target, direction, port_name)
        self.new_type = new_type

    def _retyped(self, node: cst.BaseExpression | None) -> cst.BaseExpression | None:
        """`Port[T](...)` (or the annotation `Port[T]`) with the new type."""
        func = node.func if isinstance(node, cst.Call) else node
        if not isinstance(func, cst.Subscript) or _dotted_name(func.value).rsplit(".", 1)[-1] != "Port":
            return None
        new_func = func.with_changes(
            slice=[cst.SubscriptElement(slice=cst.Index(value=_parse_user_expression(self.new_type, f"the type of port '{self.port_name}'")))]
        )
        self.found = True
        return node.with_changes(func=new_func) if isinstance(node, cst.Call) else new_func

    def leave_Assign(self, original_node: cst.Assign, updated_node: cst.Assign) -> cst.Assign:
        if not self._matches_port_assign(original_node):
            return updated_node
        value = self._retyped(updated_node.value)
        return updated_node.with_changes(value=value) if value is not None else updated_node

    def leave_AnnAssign(self, original_node: cst.AnnAssign, updated_node: cst.AnnAssign) -> cst.AnnAssign:
        if not self._matches_port_assign(original_node):
            return updated_node
        annotation = self._retyped(updated_node.annotation.annotation)
        value = self._retyped(updated_node.value)
        changes: dict[str, Any] = {}
        if annotation is not None:
            changes["annotation"] = updated_node.annotation.with_changes(annotation=annotation)
        if value is not None:
            changes["value"] = value
        return updated_node.with_changes(**changes) if changes else updated_node
