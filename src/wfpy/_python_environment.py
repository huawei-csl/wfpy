"""What this interpreter imports a workflow file with.

The IDE's Python language server resolves imports from its own configuration,
which may name another interpreter or miss a project's import roots; then a
workflow that runs is drawn with every import underlined. The sidecar is run
by the interpreter that imports the workflow, so it is the one to say where
that interpreter looks: the IDE configures the language server from the
answer instead of asking the user to.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path
from typing import Any


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _library_paths(roots: list[Path]) -> list[Path]:
    """The folders installed packages are imported from.

    ``sys.path`` without the standard library (the language server has its
    own copy of that), and the folder of wfpy itself, which an editable
    install that uses an import hook does not put on ``sys.path``.
    """
    scheme = sysconfig.get_paths()
    stdlib = [Path(scheme[key]).resolve() for key in ("stdlib", "platstdlib") if scheme.get(key)]
    site = [Path(scheme[key]).resolve() for key in ("purelib", "platlib") if scheme.get(key)]

    def is_stdlib(path: Path) -> bool:
        # site-packages sits under the standard library's folder on most layouts.
        return any(_within(path, root) for root in stdlib) and not any(
            _within(path, root) for root in site
        )

    # Not where packages are: the folder of the script that started this
    # process and the folder it was started in (`sys.path[0]`), and the
    # interpreter's own folders (on Windows the prefix and its DLLs are on
    # `sys.path`).
    not_packages: list[Path] = []
    try:
        started_in = os.getcwd()
    except OSError:  # the folder is gone
        started_in = ""
    for entry in (
        started_in,
        os.path.dirname(os.path.abspath(sys.argv[0]))
        if started_in and sys.argv and sys.argv[0]
        else "",
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        sys.base_exec_prefix,
        os.path.join(sys.base_prefix, "DLLs"),
    ):
        if entry:
            try:
                not_packages.append(Path(entry).resolve())
            except OSError:
                pass

    found: list[Path] = []
    own = Path(__file__).resolve().parent.parent
    candidates = [Path(entry) for entry in sys.path if entry]
    for candidate in candidates:
        try:
            path = candidate.resolve()
        except OSError:
            continue
        if (
            not path.is_dir()
            or is_stdlib(path)
            or path in not_packages
            or path in found
            or path in roots
        ):
            continue
        found.append(path)
    # After the filter: an editable install is wherever its checkout is.
    if own.is_dir() and own not in found and own not in roots:
        found.append(own)
    return found


def python_environment(file_path: str) -> dict[str, Any]:
    """The interpreter, and where it finds what ``file_path`` imports.

    ``roots`` are the import roots of the file's own project, the ones
    ``wfpy plan`` and ``wfpy run`` put first on ``sys.path`` to load it;
    ``libraryPaths`` are where installed packages come from.
    """
    # Imported here: the CLI imports the sidecar's neighbours, not the reverse.
    from wfpy.cli import _module_search_paths

    path = Path(file_path).resolve()
    roots = [root.resolve() for root in _module_search_paths(path)]
    return {
        "interpreter": sys.executable,
        "prefix": sys.prefix,
        "roots": [str(root) for root in roots],
        "libraryPaths": [str(entry) for entry in _library_paths(roots)],
    }
