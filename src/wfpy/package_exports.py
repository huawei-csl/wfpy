"""Keep a package's `__init__.py` in step with a class the IDE just created.

A node created into `viewers/html.py` is reachable as `viewers.html.Shown`, but a
package that re-exports its contents is asking to be imported as
`viewers.Shown` — and a file that quietly does not follow its own package's
convention is a small mess the author has to notice and fix. So when the created
file sits in a package, the class is added to that package's `__init__.py`.

Only the IMMEDIATE package. Walking the whole `__init__.py` chain up to the
repository root would re-export a leaf class from every level, which no one
writes by hand and which turns one node into four edited files.

Both edits are conditional and additive: an import is added only if the name is
not already exported, and `__all__` is touched only if it already exists —
inventing one changes what `from package import *` means for every other name in
the module, which is not a decision a node creation gets to make.

`libcst` throughout, so a file keeps its formatting and comments.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import libcst as cst
import libcst.matchers as m


@dataclass(frozen=True)
class PackageExport:
    """Where a created class should be re-exported, and under what name."""

    init_path: str
    """Absolute path of the package's `__init__.py`."""
    module_name: str
    """The created file's module name within the package, e.g. `html`."""
    class_name: str


def find_package_export(file_path: str, class_name: str) -> PackageExport | None:
    """The `__init__.py` beside `file_path`, or None when there is no package.

    A file with no `__init__.py` next to it is a plain module — nothing to keep
    in step — and `__init__.py` itself exports its own contents directly, so
    neither case has anything to do here.
    """
    directory = os.path.dirname(os.path.abspath(file_path))
    module_name = os.path.splitext(os.path.basename(file_path))[0]
    if module_name == "__init__":
        return None
    init_path = os.path.join(directory, "__init__.py")
    if not os.path.isfile(init_path):
        return None
    return PackageExport(init_path=init_path, module_name=module_name, class_name=class_name)


def _already_imported(module: cst.Module, export: PackageExport) -> bool:
    """Is the name already brought in, under any spelling this package uses?"""
    for line in module.body:
        if not isinstance(line, cst.SimpleStatementLine):
            continue
        for stmt in line.body:
            if not isinstance(stmt, cst.ImportFrom):
                continue
            if isinstance(stmt.names, cst.ImportStar):
                # `from .html import *` already carries the class.
                if _relative_module_name(stmt) == export.module_name:
                    return True
                continue
            for alias in stmt.names:
                if isinstance(alias, cst.ImportAlias) and isinstance(alias.name, cst.Name):
                    if alias.name.value == export.class_name:
                        return True
    return False


def _relative_module_name(stmt: cst.ImportFrom) -> str | None:
    """`html` for `from .html import X`; None for anything not one level down."""
    if len(stmt.relative) != 1:
        return None
    if isinstance(stmt.module, cst.Name):
        return stmt.module.value
    return None


def _with_import(module: cst.Module, export: PackageExport) -> cst.Module:
    """Add `from .<module> import <Class>`, next to the imports already there."""
    new_import = cst.SimpleStatementLine([
        cst.ImportFrom(
            module=cst.Name(export.module_name),
            names=[cst.ImportAlias(name=cst.Name(export.class_name))],
            relative=[cst.Dot()],
        )
    ])
    body = list(module.body)
    # After the last existing import, so the block stays a block; failing that,
    # after a module docstring, so it stays first.
    insert_at = 0
    for index, line in enumerate(body):
        if isinstance(line, cst.SimpleStatementLine) and any(
            isinstance(stmt, (cst.Import, cst.ImportFrom)) for stmt in line.body
        ):
            insert_at = index + 1
        elif insert_at == 0 and index == 0 and _is_docstring(line):
            insert_at = 1
    return module.with_changes(body=[*body[:insert_at], new_import, *body[insert_at:]])


def _is_docstring(line: cst.BaseStatement) -> bool:
    if not isinstance(line, cst.SimpleStatementLine) or len(line.body) != 1:
        return False
    stmt = line.body[0]
    return isinstance(stmt, cst.Expr) and isinstance(stmt.value, cst.SimpleString)


class _AllAppender(cst.CSTTransformer):
    """Append the name to an existing `__all__`, leaving its formatting alone."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.found = False
        self.already = False

    def leave_Assign(self, original: cst.Assign, updated: cst.Assign) -> cst.Assign:
        if not m.matches(updated.targets[0].target, m.Name("__all__")):
            return updated
        if not isinstance(updated.value, (cst.List, cst.Tuple)):
            return updated
        self.found = True
        entries = list(updated.value.elements)
        for element in entries:
            if isinstance(element.value, cst.SimpleString):
                if element.value.evaluated_value == self.name:
                    self.already = True
                    return updated
        appended = [
            *[element.with_changes(comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" ")))
              if element.comma is cst.MaybeSentinel.DEFAULT else element
              for element in entries],
            cst.Element(value=cst.SimpleString(f'"{self.name}"')),
        ]
        return updated.with_changes(value=updated.value.with_changes(elements=appended))


def apply_package_export(source: str, export: PackageExport) -> str:
    """The `__init__.py` source with the class exported. Unchanged if it already is.

    Returning the source rather than writing it keeps this testable and leaves
    the write — and its expected-revision check — with the caller that owns it.
    """
    module = cst.parse_module(source)
    updated = module
    if not _already_imported(module, export):
        updated = _with_import(updated, export)

    appender = _AllAppender(export.class_name)
    updated = updated.visit(appender)
    # `__all__` is only extended, never created: inventing one would change what
    # `from package import *` means for every other name in the module.
    return updated.code
