"""Keeping a package's `__init__.py` in step with a class the IDE created.

A node created into `viewers/html.py` is reachable as `viewers.html.Shown`, but
a package that re-exports its contents wants `viewers.Shown` too — and a file
that quietly does not follow its own package's convention is a small mess the
author has to find and fix.

Everything here is about restraint as much as about the edit: only the immediate
package, only an import that is missing, and `__all__` extended but never
invented.
"""
from __future__ import annotations

from pathlib import Path

from wfpy.package_exports import PackageExport, apply_package_export, find_package_export


def _export(tmp_path: Path, *, package: bool = True, name: str = "Shown") -> PackageExport | None:
    pkg = tmp_path / "viewers"
    pkg.mkdir()
    (pkg / "html.py").write_text("", encoding="utf-8")
    if package:
        (pkg / "__init__.py").write_text("", encoding="utf-8")
    return find_package_export(str(pkg / "html.py"), name)


def test_finds_the_init_beside_the_created_file(tmp_path: Path) -> None:
    export = _export(tmp_path)
    assert export is not None
    assert export.module_name == "html"
    assert export.class_name == "Shown"
    assert export.init_path.endswith("viewers/__init__.py")


def test_says_nothing_when_the_directory_is_not_a_package(tmp_path: Path) -> None:
    # A plain module has nothing to keep in step.
    assert _export(tmp_path, package=False) is None


def test_says_nothing_for_a_class_created_in_the_init_itself(tmp_path: Path) -> None:
    pkg = tmp_path / "viewers"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    assert find_package_export(str(pkg / "__init__.py"), "Shown") is None


EXPORT = PackageExport(init_path="/w/viewers/__init__.py", module_name="html", class_name="Shown")


def test_adds_the_relative_import() -> None:
    assert "from .html import Shown" in apply_package_export("", EXPORT)


def test_puts_it_with_the_imports_already_there() -> None:
    source = '"""Viewers."""\n\nfrom .table import Table\n\nVERSION = "1"\n'
    result = apply_package_export(source, EXPORT)
    lines = [line for line in result.split("\n") if line.strip()]
    assert lines.index("from .html import Shown") == lines.index("from .table import Table") + 1


def test_keeps_a_docstring_first_when_there_are_no_imports() -> None:
    result = apply_package_export('"""Viewers."""\n', EXPORT)
    assert result.startswith('"""Viewers."""')
    assert "from .html import Shown" in result


def test_does_not_import_a_name_that_is_already_there() -> None:
    source = "from .html import Shown\n"
    assert apply_package_export(source, EXPORT).count("import Shown") == 1


def test_respects_a_star_import_of_the_same_module() -> None:
    # `from .html import *` already carries the class.
    source = "from .html import *\n"
    result = apply_package_export(source, EXPORT)
    assert "from .html import Shown" not in result


def test_extends_an_existing_all() -> None:
    source = 'from .table import Table\n\n__all__ = ["Table"]\n'
    result = apply_package_export(source, EXPORT)
    assert '"Shown"' in result.split("__all__")[1]
    assert '"Table"' in result.split("__all__")[1]


def test_does_not_add_to_all_twice() -> None:
    source = 'from .html import Shown\n\n__all__ = ["Shown"]\n'
    result = apply_package_export(source, EXPORT)
    assert result.count('"Shown"') == 1


def test_never_invents_an_all() -> None:
    # Creating `__all__` changes what `from package import *` means for every
    # other name in the module — not a decision a node creation gets to make.
    result = apply_package_export("from .table import Table\n", EXPORT)
    assert "__all__" not in result


def test_leaves_the_rest_of_the_file_alone() -> None:
    source = '"""Docs."""\n\n# a comment worth keeping\nfrom .table import Table\n\nX = 1  # trailing\n'
    result = apply_package_export(source, EXPORT)
    assert "# a comment worth keeping" in result
    assert "X = 1  # trailing" in result


def test_result_is_valid_python() -> None:
    source = 'from .table import Table\n\n__all__ = ["Table"]\n'
    compile(apply_package_export(source, EXPORT), "__init__.py", "exec")
