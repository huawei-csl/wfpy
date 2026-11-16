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
            self._trees[path] = tree
        return self._trees[path]

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
        self, file: Path, name: str, _seen: frozenset[tuple[str, str]] = frozenset()
    ) -> Definition | External | None:
        """What ``name``, bound at the top level of module ``file``, is."""
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
        for stmt in _module_statements(tree.body):
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
                    inner = self.binding(source, alias.name, seen)
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
                    found = self.binding(source, name, seen)
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


def _module_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """The top-level statements of a module, those under a top-level ``if`` /
    ``try`` (an optional import, a ``TYPE_CHECKING`` block) included."""
    for stmt in body:
        if isinstance(stmt, (ast.If, ast.Try)):
            yield from _module_statements(stmt.body)
            yield from _module_statements(stmt.orelse)
            if isinstance(stmt, ast.Try):
                for handler in stmt.handlers:
                    yield from _module_statements(handler.body)
                yield from _module_statements(stmt.finalbody)
            continue
        yield stmt


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


def _signature_parameters(text: str, fn: ast.FunctionDef) -> list[dict[str, str]]:
    """A function's parameters as written: name, annotation, default."""
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(
        args.defaults
    )
    entries: list[tuple[ast.arg, ast.expr | None]] = [
        *zip(positional, defaults),
        *zip(args.kwonlyargs, args.kw_defaults),
    ]
    found: list[dict[str, str]] = []
    for arg, default in entries:
        if arg.arg in ("self", "cls"):
            continue
        entry = {"name": arg.arg}
        annotation = _segment(text, arg.annotation)
        if annotation:
            entry["type"] = annotation
        default_text = _segment(text, default)
        if default_text is not None:
            entry["defaultValue"] = default_text
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
        parameters = _signature_parameters(text, node)
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
