"""A static view of a workflow project, read from its source and never run.

The sidecar edits one file, but what it edits is named across the project: a
task class declared in one module is instantiated by workflows in others, under
whatever name each imports it by. Three questions follow, and this module
answers them by reading the files (``ast``), so a script that only *runs*
workflows is never executed to find out:

- where a name bound in a module is defined, following ``from ... import`` and
  the re-exports of packages (``compile.sources`` re-exports ``Text`` from
  ``compile/sources/text.py``);
- which definitions of a task or workflow by that name the project has, and the
  shortest module each can be imported from;
- which workflows of the project instantiate a given class -- that class, by its
  definition, not another of the same name -- and by which instance names.

The project is the importable root of the file: the folder above its outermost
package (the folder itself for a file in no package), which is the root
``wfpy plan`` puts on ``sys.path`` to import it.
"""

from __future__ import annotations

import ast
import dataclasses
import os
from pathlib import Path
from typing import Iterator

from wfpy._source_repair import SourceFault, repair_source
from wfpy._source_text import signature_parameters

# Folders that hold no project source: environments, caches, build output.
_SKIP_DIRS = {
    "__pycache__",
    "node_modules",
    "site-packages",
    "venv",
    "env",
    "build",
    "dist",
}

# The decorators that make a class a node.
TASK_DECORATORS = {"task", "tool", "agent", "viewer", "streamblocks", "source"}


def project_root(file_path: str | Path) -> Path:
    """The folder above the outermost package holding ``file_path``."""
    folder = Path(file_path).resolve().parent
    while (folder / "__init__.py").is_file() and folder.parent != folder:
        folder = folder.parent
    return folder


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def is_workflow_function(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
        _decorator_name(dec) == "workflow" for dec in node.decorator_list
    )


def is_task_class(node: ast.AST) -> bool:
    return isinstance(node, ast.ClassDef) and any(
        _decorator_name(dec) in TASK_DECORATORS for dec in node.decorator_list
    )


def factory_workflow(node: ast.AST) -> ast.FunctionDef | None:
    """The ``@workflow`` def a module-level function builds, if it is a factory."""
    if not isinstance(node, ast.FunctionDef) or is_workflow_function(node):
        return None
    for inner in _walk_body(node.body):
        if isinstance(inner, ast.FunctionDef) and is_workflow_function(inner):
            return inner
    return None


def _walk_body(statements: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Every statement of a body, at any depth of block, functions' included."""
    for stmt in statements:
        yield stmt
        for field in ("body", "orelse", "finalbody"):
            inner = getattr(stmt, field, None)
            if isinstance(inner, list):
                yield from _walk_body([s for s in inner if isinstance(s, ast.stmt)])
        for handler in getattr(stmt, "handlers", []) or []:
            yield from _walk_body(handler.body)


def _own_statements(statements: list[ast.stmt]) -> Iterator[ast.stmt]:
    """A function's own statements at any depth of block, not those of a def
    or class inside it."""
    for stmt in statements:
        yield stmt
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            inner = getattr(stmt, field, None)
            if isinstance(inner, list):
                yield from _own_statements([s for s in inner if isinstance(s, ast.stmt)])
        for handler in getattr(stmt, "handlers", []) or []:
            yield from _own_statements(handler.body)


@dataclasses.dataclass(frozen=True)
class Definition:
    """A top-level ``class`` / ``def`` (or assignment) of a project file."""

    file: Path
    name: str

    def key(self) -> tuple[str, str]:
        return (str(self.file), self.name)


@dataclasses.dataclass(frozen=True)
class External:
    """A name bound from a module outside the project (``wfpy``, ``os``)."""

    module: str
    name: str


class ProjectIndex:
    """Parsed files of one project, read once each."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._trees: dict[Path, ast.Module | None] = {}
        self._texts: dict[Path, str | None] = {}
        self.errors: dict[Path, str] = {}
        # For a reader that only draws: a file that does not parse is read
        # from a repaired copy of its text (`_source_repair`). An op that
        # edits leaves this off, so it never acts on what a repair left out.
        self.repair_unparsed = False
        # The faults of each file read from a repaired text (`repair_unparsed`).
        self.faults: dict[Path, tuple[SourceFault, ...]] = {}

    @classmethod
    def for_file(cls, file_path: str | Path) -> "ProjectIndex":
        return cls(project_root(file_path))

    # ── Files ──────────────────────────────────────────────────────────

    def files(self) -> list[Path]:
        found: list[Path] = []
        for folder, dirs, names in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS)
            for name in sorted(names):
                if name.endswith(".py"):
                    found.append(Path(folder) / name)
        return found

    def text(self, path: Path) -> str | None:
        path = path.resolve()
        if path not in self._texts:
            try:
                self._texts[path] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self._texts[path] = None
                self.errors[path] = str(exc)
        return self._texts[path]

    def use_text(self, path: str | Path, text: str) -> None:
        """Read ``path`` as ``text`` and not from disk: what the caller holds
        of the file, which may not be what is saved."""
        path = Path(path).resolve()
        self._texts[path] = text
        self._trees.pop(path, None)
        self.errors.pop(path, None)

    def tree(self, path: Path) -> ast.Module | None:
        path = path.resolve()
        if path not in self._trees:
            text = self.text(path)
            tree: ast.Module | None = None
            if text is not None:
                try:
                    tree = ast.parse(text, filename=str(path))
                except SyntaxError as exc:
                    self.errors[path] = f"{type(exc).__name__}: {exc}"
                    if self.repair_unparsed:
                        tree = self._repaired_tree(path, text)
            self._trees[path] = tree
        return self._trees[path]

    def _repaired_tree(self, path: Path, text: str) -> ast.Module | None:
        """The tree of a repaired copy of ``text``, which is then the text
        the places of that tree are read in. None when it cannot be made."""
        repaired = repair_source(text)
        if not repaired.parsed:
            return None
        try:
            tree = ast.parse(repaired.text, filename=str(path))
        except (SyntaxError, ValueError):
            return None
        self._texts[path] = repaired.text
        self.faults[path] = tuple(repaired.faults)
        return tree

    def module_name(self, path: Path) -> str | None:
        """The dotted name ``path`` is imported by from the project root."""
        try:
            relative = path.resolve().relative_to(self.root)
        except ValueError:
            return None
        parts = list(relative.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts or not all(part.isidentifier() for part in parts):
            return None
        return ".".join(parts)

    def module_file(self, dotted: str, near: Path | None = None) -> Path | None:
        """The project file of module ``dotted``: from the root, or from the
        folder of ``near`` (a script importing a sibling)."""
        parts = [part for part in dotted.split(".") if part]
        if not parts:
            return None
        bases = [self.root]
        if near is not None and near.resolve().parent != self.root:
            bases.append(near.resolve().parent)
        for base in bases:
            candidate = base.joinpath(*parts)
            if candidate.with_suffix(".py").is_file():
                return candidate.with_suffix(".py").resolve()
            if (candidate / "__init__.py").is_file():
                return (candidate / "__init__.py").resolve()
        return None

    def _import_source(self, file: Path, node: ast.ImportFrom) -> tuple[Path | None, str]:
        """The project file a ``from ... import`` reads from, and its dotted
        name (``None`` for a module outside the project)."""
        if node.level:
            base = file.resolve().parent
            for _ in range(node.level - 1):
                base = base.parent
            parts = (node.module or "").split(".") if node.module else []
            candidate = base.joinpath(*parts) if parts else base
            if parts and candidate.with_suffix(".py").is_file():
                target: Path | None = candidate.with_suffix(".py").resolve()
            elif (candidate / "__init__.py").is_file():
                target = (candidate / "__init__.py").resolve()
            else:
                target = None
            dotted = self.module_name(target) if target is not None else None
            return target, dotted or (node.module or "")
        module = node.module or ""
        return self.module_file(module, near=file), module

    # ── Bindings ───────────────────────────────────────────────────────

    def binding(
        self, file: Path, name: str, _seen: frozenset[tuple[str, str]] = frozenset(), runtime: bool = False
    ) -> Definition | External | None:
        """What ``name``, bound at the top level of module ``file``, is. With
        ``runtime``, only as the module runs: not by an ``if TYPE_CHECKING:``
        block's import."""
        file = file.resolve()
        key = (str(file), name)
        if key in _seen or len(_seen) > 32:
            return None
        seen = _seen | {key}
        tree = self.tree(file)
        if tree is None:
            return None
        found: Definition | External | None = None
        stars: list[ast.ImportFrom] = []
        for stmt in _module_statements(tree.body, runtime):
            if (
                isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and stmt.name == name
            ):
                found = Definition(file, name)
            elif isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    if alias.name == "*":
                        stars.append(stmt)
                        continue
                    if (alias.asname or alias.name) != name:
                        continue
                    source, dotted = self._import_source(file, stmt)
                    if source is None:
                        found = External(dotted, alias.name)
                        continue
                    inner = self.binding(source, alias.name, seen, runtime)
                    if inner is None and source.name == "__init__.py":
                        # `from package import module`.
                        found = None
                        continue
                    found = inner
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if (alias.asname or alias.name.split(".")[0]) == name:
                        found = External(alias.name, "")
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                    found = Definition(file, name)
        if found is None:
            for stmt in stars:
                source, _dotted = self._import_source(file, stmt)
                if source is not None:
                    found = self.binding(source, name, seen, runtime)
                    if found is not None:
                        break
        return found

    def definition_node(self, definition: Definition) -> ast.stmt | None:
        tree = self.tree(definition.file)
        if tree is None:
            return None
        node: ast.stmt | None = None
        for stmt in _module_statements(tree.body):
            if (
                isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and stmt.name == definition.name
            ):
                node = stmt
        return node

    # ── Definitions by name ────────────────────────────────────────────

    def definitions_named(self, name: str) -> list[Definition]:
        """Every top-level task class, workflow or workflow factory named ``name``."""
        found: list[Definition] = []
        for path in self.files():
            text = self.text(path)
            if text is None or (f"class {name}" not in text and f"def {name}" not in text):
                continue
            tree = self.tree(path)
            if tree is None:
                continue
            for stmt in _module_statements(tree.body):
                if getattr(stmt, "name", None) != name:
                    continue
                if (
                    is_task_class(stmt)
                    or is_workflow_function(stmt)
                    or factory_workflow(stmt) is not None
                ):
                    definition = Definition(path.resolve(), name)
                    if definition not in found:
                        found.append(definition)
        return found

    def import_modules(self, definition: Definition) -> list[str]:
        """The modules ``definition`` can be imported from by its name: its own
        and each enclosing package that re-exports it, shortest first."""
        own = self.module_name(definition.file)
        modules: list[str] = [own] if own else []
        folder = definition.file.parent
        while folder != self.root and (folder / "__init__.py").is_file():
            init = (folder / "__init__.py").resolve()
            if init != definition.file:
                bound = self.binding(init, definition.name)
                dotted = self.module_name(init)
                if bound == definition and dotted and dotted not in modules:
                    modules.append(dotted)
            folder = folder.parent
        return sorted(modules, key=lambda dotted: (dotted.count("."), len(dotted)))

    # ── Workflows and their instances ──────────────────────────────────

    def workflows(self, path: Path) -> list[ast.FunctionDef]:
        """Every ``@workflow`` def of the file, a factory's included."""
        tree = self.tree(path)
        if tree is None:
            return []
        return [
            stmt
            for stmt in _walk_body(tree.body)
            if isinstance(stmt, ast.FunctionDef) and is_workflow_function(stmt)
        ]

    def instances_of(self, path: Path, workflow: ast.FunctionDef, target: Definition) -> list[str]:
        """The names workflow ``workflow`` of ``path`` binds instances of ``target`` to."""
        names: list[str] = []
        local_classes = {
            stmt.name for stmt in _own_statements(workflow.body) if isinstance(stmt, ast.ClassDef)
        }
        for stmt in _own_statements(workflow.body):
            if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
                continue
            func = stmt.value.func
            if isinstance(func, ast.Call) and not stmt.value.args and not stmt.value.keywords:
                func = func.func  # `factory(...)()`: an instance of what the factory builds
            if not isinstance(func, ast.Name) or func.id in local_classes:
                continue
            if self.binding(path, func.id) != target:
                continue
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id not in names:
                    names.append(t.id)
        return names

    def uses_of(self, target: Definition) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        """Each workflow of the project with an instance of ``target`` (its
        file, name and instances), and the files that could not be read."""
        used_in: list[dict[str, object]] = []
        unchecked: list[dict[str, str]] = []
        for path in self.files():
            text = self.text(path)
            if text is None:
                unchecked.append(
                    {"file": str(path), "message": self.errors.get(path.resolve(), "unreadable")}
                )
                continue
            if target.name not in text or "workflow" not in text:
                continue
            tree = self.tree(path)
            if tree is None:
                unchecked.append(
                    {
                        "file": str(path),
                        "message": self.errors.get(path.resolve(), "does not parse"),
                    }
                )
                continue
            for workflow in self.workflows(path):
                instances = self.instances_of(path, workflow, target)
                if instances:
                    used_in.append(
                        {
                            "file": str(path.resolve()),
                            "workflow": workflow.name,
                            "instances": instances,
                        }
                    )
        return used_in, unchecked


def _module_statements(body: list[ast.stmt], runtime: bool = False) -> Iterator[ast.stmt]:
    """The top-level statements of a module, those under a top-level ``if`` /
    ``try`` (an optional import, a ``TYPE_CHECKING`` block) included -- with
    ``runtime``, not a ``TYPE_CHECKING`` block's, which never runs."""
    for stmt in body:
        if isinstance(stmt, (ast.If, ast.Try)):
            if not (runtime and _is_type_checking(stmt)):
                yield from _module_statements(stmt.body, runtime)
            yield from _module_statements(stmt.orelse, runtime)
            if isinstance(stmt, ast.Try):
                for handler in stmt.handlers:
                    yield from _module_statements(handler.body, runtime)
                yield from _module_statements(stmt.finalbody, runtime)
            continue
        yield stmt


def _is_type_checking(stmt: ast.stmt) -> bool:
    """``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:``."""
    if not isinstance(stmt, ast.If):
        return False
    test = stmt.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


# ── The sidecar's queries ──────────────────────────────────────────────


def port_uses(file_path: str, entity: str) -> dict[str, object]:
    """Where class ``entity``, declared in ``file_path``, is instantiated:
    each workflow of the project with an instance of it -- by its file, its
    name (a factory's inner ``@workflow`` by its own) and the instances'
    names. A class of the same name declared elsewhere is another class. A
    file that does not parse is told in ``unchecked``: its workflows are not
    followed.

    ``entity`` may also be a workflow of the file, or the workflow a factory
    of the file builds: its instances are the nodes whose ports are the
    workflow's own."""
    from wfpy.rewrite import RewriteError

    index = ProjectIndex.for_file(file_path)
    target = Definition(Path(file_path).resolve(), entity)
    node = index.definition_node(target)
    if node is None:
        tree = index.tree(target.file)
        for stmt in _module_statements(tree.body if tree is not None else []):
            inner = factory_workflow(stmt)
            if inner is not None and inner.name == entity and isinstance(stmt, ast.FunctionDef):
                target, node = Definition(target.file, stmt.name), stmt
                break
    if not isinstance(node, (ast.ClassDef, ast.FunctionDef)):
        raise RewriteError(
            message=f"no class or workflow '{entity}' in {Path(file_path).name}",
            diagnostic={
                "code": "definition_not_found",
                "entityType": entity,
                "file": str(file_path),
            },
        )
    used_in, unchecked = index.uses_of(target)
    return {"code": "port_uses_ok", "entity": entity, "usedIn": used_in, "unchecked": unchecked}


def _segment(text: str, node: ast.AST | None) -> str | None:
    if node is None:
        return None
    return ast.get_source_segment(text, node)


def _class_parameters(
    index: ProjectIndex, definition: Definition, node: ast.ClassDef, depth: int = 0
) -> list[dict[str, str]]:
    """A class task's parameters: its annotated fields with no default
    (``core._classify_fields``), its bases' first."""
    text = index.text(definition.file) or ""
    found: list[dict[str, str]] = []
    if depth < 8:
        for base in node.bases:
            if not isinstance(base, ast.Name):
                continue
            bound = index.binding(definition.file, base.id)
            if isinstance(bound, Definition):
                base_node = index.definition_node(bound)
                if isinstance(base_node, ast.ClassDef):
                    for entry in _class_parameters(index, bound, base_node, depth + 1):
                        if entry["name"] not in [f["name"] for f in found]:
                            found.append(entry)
    ports = {
        target.id
        for stmt in node.body
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Ports"
        for inner in stmt.body
        if isinstance(inner, ast.Assign)
        for target in inner.targets
        if isinstance(target, ast.Name)
    }
    defaults = {
        target.id
        for stmt in node.body
        if isinstance(stmt, ast.Assign)
        for target in stmt.targets
        if isinstance(target, ast.Name)
    }
    for stmt in node.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        name = stmt.target.id
        found = [f for f in found if f["name"] != name]
        if stmt.value is not None or name.startswith("_") or name in ports or name in defaults:
            continue  # state: a field with a default, or private
        entry = {"name": name}
        annotation = _segment(text, stmt.annotation)
        if annotation:
            entry["type"] = annotation
        found.append(entry)
    return found


def resolve_type(file_path: str, type_name: str) -> tuple[ProjectIndex, Definition, ast.stmt]:
    """The task or workflow ``type_name`` names in ``file_path``: the one the
    module binds, else the one the project defines (one, or a refusal)."""
    from wfpy.rewrite import RewriteError

    index = ProjectIndex.for_file(file_path)
    here = Path(file_path).resolve()
    bound = index.binding(here, type_name)
    candidates: list[Definition]
    if isinstance(bound, Definition):
        candidates = [bound]
    elif isinstance(bound, External):
        raise RewriteError(
            message=f"'{type_name}' is imported from {bound.module}, outside the project: its parameters are not known here",
            diagnostic={"code": "type_not_found", "type": type_name},
        )
    else:
        candidates = index.definitions_named(type_name)
    if not candidates:
        raise RewriteError(
            message=f"no task or workflow named '{type_name}' in {here.name} or the project ({index.root})",
            diagnostic={"code": "type_not_found", "type": type_name, "root": str(index.root)},
        )
    if len(candidates) > 1:
        where = ", ".join(str(d.file.relative_to(index.root)) for d in candidates)
        raise RewriteError(
            message=f"'{type_name}' is defined more than once in the project ({where})",
            diagnostic={
                "code": "type_ambiguous",
                "type": type_name,
                "files": [str(d.file) for d in candidates],
            },
        )
    node = index.definition_node(candidates[0])
    if node is None:
        raise RewriteError(
            message=f"'{type_name}' in {candidates[0].file.name} is not a class or a function",
            diagnostic={"code": "type_not_found", "type": type_name},
        )
    return index, candidates[0], node


def type_parameters(file_path: str, type_name: str) -> dict[str, object]:
    """The parameters an instance of ``type_name`` takes: a class task's
    annotated fields with no default; a workflow's or a workflow factory's
    signature, each parameter with the source of its default when it has
    one (``defaultValue``)."""
    from wfpy.rewrite import RewriteError

    index, definition, node = resolve_type(file_path, type_name)
    text = index.text(definition.file) or ""
    if isinstance(node, ast.ClassDef):
        kind = "task"
        parameters = _class_parameters(index, definition, node)
    elif isinstance(node, ast.FunctionDef):
        kind = (
            "workflow"
            if is_workflow_function(node)
            else "factory"
            if factory_workflow(node)
            else "function"
        )
        parameters = signature_parameters(text, node)
    else:
        raise RewriteError(
            message=f"'{type_name}' is not a task or a workflow",
            diagnostic={"code": "type_not_found", "type": type_name},
        )
    return {
        "type": type_name,
        "kind": kind,
        "file": str(definition.file),
        "parameters": parameters,
    }


# ── Ports, as the source declares them ─────────────────────────────────

# A port of no declared direction is an output by these names and an input by
# any other (`core._infer_direction`).
_OUTPUT_PORT_NAMES = {"out", "output", "result", "report", "summary"}

# wfpy's control constructs, as the runtime graph draws them
# (`_graph_export.export_graph_json`): the kind the runtime names one by, and
# its ports by name, direction, type.
_CONTROL_PORTS: dict[str, tuple[str, tuple[tuple[str, str, str], ...]]] = {
    "if_": ("if", (("cond", "in", "bool"), ("out", "out", "any"))),
    "loop": ("loop", (("iter", "in", "iterable"), ("item", "out", "any"), ("out", "out", "any"))),
}


def declared_ports(
    index: ProjectIndex, file: str | Path, ref: str
) -> list[dict[str, object]] | None:
    """The ports of what ``ref`` names in module ``file``, read from the
    source and in the order written: a task class's ``class Ports:`` (a
    base's when it has none of its own), the ``inputs`` and ``outputs`` of a
    workflow, or of the workflow a factory builds when ``ref`` is the call of
    one (``make(3)`` of ``flow = make(3)()``), a control construct's own.

    Each port is its attribute (``attr``, what ``connect(node.<attr>, ...)``
    writes), its ``name``, a ``direction`` of ``in`` or ``out`` (one the
    source leaves open is settled by name, as the runtime settles it), its
    ``type`` as written, a ``role``, and the ``file`` and ``line`` it is
    declared at. A control construct's say which one it is (``control``:
    ``if`` or ``loop``) and no place: wfpy declares them.

    ``None`` when ``ref`` is not followed to a definition the project holds:
    a class of an installed package, a call that builds no node. That is not
    "no ports": the caller knows only what the connections name."""
    bound, called = _ref_binding(index, file, ref)
    if isinstance(bound, External):
        if called or bound.module.split(".")[0] != "wfpy":
            return None
        return _control_ports(bound.name)
    if not isinstance(bound, Definition):
        return None
    node = index.definition_node(bound)
    text = index.text(bound.file) or ""
    if isinstance(node, ast.ClassDef) and not called:
        return _class_ports(index, bound, node) or []
    if isinstance(node, ast.FunctionDef):
        workflow = factory_workflow(node) if called else node
        if workflow is not None and is_workflow_function(workflow):
            return workflow_ports(text, bound.file, workflow)
    return None


def declared_as(index: ProjectIndex, file: str | Path, ref: str) -> tuple[str, Definition] | None:
    """What ``ref`` names in module ``file``, by the decorator it is written
    under, and where it is defined: ``task``, ``agent``, ``tool``, ... for a
    class, ``workflow`` for a workflow or the call of a factory of one.

    ``None`` when ``ref`` is not followed to such a definition of the
    project."""
    bound, called = _ref_binding(index, file, ref)
    if not isinstance(bound, Definition):
        return None
    node = index.definition_node(bound)
    if isinstance(node, ast.ClassDef) and not called:
        for decorator in node.decorator_list:
            if _decorator_name(decorator) in TASK_DECORATORS:
                return _decorator_name(decorator), bound
    if isinstance(node, ast.FunctionDef):
        workflow = factory_workflow(node) if called else node
        if workflow is not None and is_workflow_function(workflow):
            return "workflow", bound
    return None


def _ref_binding(
    index: ProjectIndex, file: str | Path, ref: str
) -> tuple[Definition | External | None, bool]:
    """What the name ``ref`` is written with is bound to in module ``file``,
    and whether ``ref`` is a call of it (``make(3)`` of ``flow = make(3)()``:
    what is instantiated is then what the call returns)."""
    here = Path(file).resolve()
    try:
        written: ast.expr = ast.parse(ref.strip(), mode="eval").body
    except (SyntaxError, ValueError):
        return None, False
    called = isinstance(written, ast.Call)
    if isinstance(written, ast.Call):
        written = written.func
    parts: list[str] = []
    while isinstance(written, ast.Attribute):
        parts.append(written.attr)
        written = written.value
    if not isinstance(written, ast.Name):
        return None, called
    parts.append(written.id)
    name, module = parts[0], ".".join(reversed(parts[1:]))
    if not module:
        return index.binding(here, name), called
    home = _dotted_module(index, here, module)
    if home is not None:
        return index.binding(home, name), called
    # `wfpy.loop`: a name of a module the project does not hold.
    head = index.binding(here, module.split(".")[0])
    return (External(head.module, name) if isinstance(head, External) else None), called


def workflow_ports(text: str, file: Path, fn: ast.FunctionDef) -> list[dict[str, object]]:
    """The ports ``@workflow(inputs={...}, outputs={...})`` declares on
    ``fn``: its inputs, then its outputs."""
    ports: list[dict[str, object]] = []
    for decorator in fn.decorator_list:
        if not isinstance(decorator, ast.Call) or _decorator_name(decorator) != "workflow":
            continue
        for wanted, direction in (("inputs", "in"), ("outputs", "out")):
            for keyword in decorator.keywords:
                if keyword.arg != wanted or not isinstance(keyword.value, ast.Dict):
                    continue
                for key, value in zip(keyword.value.keys, keyword.value.values):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        ports.append(
                            {
                                "attr": key.value,
                                "name": key.value,
                                "direction": direction,
                                "type": _segment(text, value) or "any",
                                "role": "data",
                                "file": str(file),
                                "line": key.lineno,
                            }
                        )
    return ports


def _control_ports(name: str) -> list[dict[str, object]] | None:
    if name not in _CONTROL_PORTS:
        return None
    control, ports = _CONTROL_PORTS[name]
    return [
        {
            "attr": port,
            "name": port,
            "direction": direction,
            "type": kind,
            "role": "control",
            "control": control,
        }
        for port, direction, kind in ports
    ]


def _dotted_module(index: ProjectIndex, file: Path, dotted: str) -> Path | None:
    """The project file ``dotted`` names in ``file`` after ``import pkg.mod``
    or ``import pkg.mod as dotted``."""
    head = dotted.split(".")[0]
    bound = index.binding(file, head)
    if not isinstance(bound, External) or bound.name:
        return None
    for candidate in (dotted, bound.module + dotted[len(head) :], bound.module):
        found = index.module_file(candidate, near=file)
        if found is not None:
            return found
    return None


def _class_ports(
    index: ProjectIndex, definition: Definition, node: ast.ClassDef, depth: int = 0
) -> list[dict[str, object]] | None:
    """The ports of a class: those of its ``class Ports:``, after those of the
    ``Ports`` that one extends (``class Ports(Base.Ports):``). A class with no
    ``Ports`` has its first base's that has one, as ``getattr(cls, "Ports")``
    finds it. ``None`` when neither it nor a base has any."""
    block = next(
        (stmt for stmt in node.body if isinstance(stmt, ast.ClassDef) and stmt.name == "Ports"),
        None,
    )
    bases: list[ast.expr] = (
        list(node.bases)
        if block is None
        else [b.value for b in block.bases if isinstance(b, ast.Attribute) and b.attr == "Ports"]
    )
    inherited: list[dict[str, object]] = []
    if depth < 8:
        for base in bases:
            if not isinstance(base, ast.Name):
                continue
            bound = index.binding(definition.file, base.id)
            base_node = index.definition_node(bound) if isinstance(bound, Definition) else None
            if not isinstance(bound, Definition) or not isinstance(base_node, ast.ClassDef):
                continue
            found = _class_ports(index, bound, base_node, depth + 1)
            if found is None:
                continue
            if block is None:
                return found
            inherited.extend(entry for entry in found if entry not in inherited)
    if block is None:
        return None
    text = index.text(definition.file) or ""
    ports = inherited
    for stmt in block.body:
        entry = _port_entry(text, definition.file, stmt)
        if entry is None:
            continue
        # The class whose `Ports` writes it, which a subclass's node shares.
        entry["owner"] = node.name
        ports = [port for port in ports if port["attr"] != entry["attr"]]
        ports.append(entry)
    return ports


def _port_entry(text: str, file: Path, stmt: ast.stmt) -> dict[str, object] | None:
    """``Name = Port[T](...)`` (or the bare ``Port[T]``) as a port; ``None`` for
    any other statement of a ``Ports`` block, and for a private name, which the
    runtime does not take as a port either (`types.extract_ports`)."""
    value: ast.expr | None
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target, value = stmt.targets[0], stmt.value
    elif isinstance(stmt, ast.AnnAssign):
        target, value = stmt.target, stmt.value
    else:
        return None
    if not isinstance(target, ast.Name) or target.id.startswith("_") or value is None:
        return None
    call = value if isinstance(value, ast.Call) else None
    factory = call.func if call is not None else value
    if not isinstance(factory, ast.Subscript) or _decorator_name(factory.value) != "Port":
        return None
    name = target.id
    direction = "inout"
    if call is not None:
        first = call.args[0] if call.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            name = first.value
        for keyword in call.keywords:
            given = keyword.value
            if not isinstance(given, ast.Constant) or not isinstance(given.value, str):
                continue
            if keyword.arg == "name":
                name = given.value
            elif keyword.arg == "direction":
                direction = given.value
    # `declared` is the direction as it is written (`inout` when it is not),
    # which the runtime's graph gives; `direction` is the side it is drawn on.
    declared = direction
    if direction not in ("in", "out"):
        direction = "out" if name.lower() in _OUTPUT_PORT_NAMES else "in"
    return {
        "attr": target.id,
        "name": name,
        "direction": direction,
        "declared": declared,
        "type": _segment(text, factory.slice) or "any",
        "role": "data",
        "file": str(file),
        "line": stmt.lineno,
    }
