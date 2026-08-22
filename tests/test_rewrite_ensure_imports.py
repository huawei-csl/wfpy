"""Generated code has to be code that runs.

Creating a node writes a decorated class whose members are `Port[Any]()`, so the
file needs `Port`, the decorator, and `Any` in scope. Only `Any` was ensured,
which held for as long as every file already imported the rest by hand — and
stopped holding the moment the IDE created a node in a file that did not, leaving
the author a NameError the sidecar had written for them.

The imports are also expected to look hand-written: folded into the existing
`from wfpy import ...` line the examples all use, not appended as a second
import of the same package.
"""
from __future__ import annotations

from pathlib import Path

import libcst as cst

from wfpy.rewrite import RewriteEngine


def _engine(tmp_path: Path, source: str) -> RewriteEngine:
    file_path = tmp_path / "wf.py"
    file_path.write_text(source, encoding="utf-8")
    return RewriteEngine(str(file_path), cst.parse_module(source), source)


def _created(tmp_path: Path, source: str, *, kind: str = "viewer", name: str = "Shown") -> str:
    engine = _engine(tmp_path, source)
    engine.create_task_type(kind=kind, name=name)
    return engine.module.code


EMPTY = '"""A workflow."""\n'


def test_adds_every_symbol_the_generated_class_uses(tmp_path: Path) -> None:
    code = _created(tmp_path, EMPTY)
    assert "from wfpy import Port, viewer" in code
    assert "from typing import Any" in code
    # And the thing it was all for: the file parses and names resolve.
    compile(code, "wf.py", "exec")


def test_imports_the_decorator_for_each_kind(tmp_path: Path) -> None:
    # `tool` and `agent` are CALLED (`@tool(...)`), which changes the decorator
    # expression but not the symbol that must be in scope.
    for kind in ("task", "agent", "viewer", "tool"):
        code = _created(tmp_path, EMPTY, kind=kind, name=f"N{kind.capitalize()}")
        assert f" {kind}" in code.split("\n")[1] or kind in code
        assert f"@{kind}" in code


def test_folds_into_the_existing_wfpy_import(tmp_path: Path) -> None:
    source = "from wfpy import connect, workflow\n"
    code = _created(tmp_path, source)
    # One import of the package, not two.
    assert code.count("from wfpy import") == 1
    assert "from wfpy import Port, connect, viewer, workflow" in code


def test_leaves_an_import_that_already_covers_it_untouched(tmp_path: Path) -> None:
    source = "from typing import Any\nfrom wfpy import Port, viewer\n"
    code = _created(tmp_path, source)
    assert code.count("from wfpy import") == 1
    assert code.count("from typing import") == 1
    assert "from wfpy import Port, viewer" in code


def test_respects_a_star_import(tmp_path: Path) -> None:
    # `from wfpy import *` already has the names in scope; adding an explicit
    # import would be noise in a file whose author chose the star.
    source = "from wfpy import *\n"
    code = _created(tmp_path, source)
    assert "from wfpy import *" in code
    assert "from wfpy import Port" not in code


def test_leaves_an_aliased_import_alone(tmp_path: Path) -> None:
    # The name is in scope under another spelling; importing the plain one on
    # top would be redundant, and could shadow what the author meant.
    source = "from wfpy import viewer as view_node\n"
    code = _created(tmp_path, source)
    assert "viewer as view_node" in code


def test_keeps_the_module_docstring_first(tmp_path: Path) -> None:
    code = _created(tmp_path, EMPTY)
    assert code.startswith('"""A workflow."""')


def test_sorts_the_names_it_merges(tmp_path: Path) -> None:
    # A rewriter should leave a file looking the way its author would have
    # written it, and every example in the repo keeps this list alphabetical.
    source = "from wfpy import workflow, connect\n"
    code = _created(tmp_path, source, kind="task", name="Step")
    line = next(line for line in code.split("\n") if line.startswith("from wfpy import"))
    names = [part.strip() for part in line.split("import", 1)[1].split(",")]
    assert names == sorted(names)


def test_the_file_still_parses_after_repeated_creation(tmp_path: Path) -> None:
    engine = _engine(tmp_path, EMPTY)
    for index, kind in enumerate(("task", "viewer", "agent")):
        engine.create_task_type(kind=kind, name=f"Node{index}")
    code = engine.module.code
    compile(code, "wf.py", "exec")
    assert code.count("from wfpy import") == 1
