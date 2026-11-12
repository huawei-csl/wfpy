"""What this interpreter imports a workflow file with.

The IDE's Python language server resolves imports from its own configuration,
which may name another interpreter or miss a project's import roots; then a
workflow that runs is drawn with every import underlined. The sidecar is run
by the interpreter that imports the workflow, so it is the one to say where
that interpreter looks: the IDE configures the language server from the
answer instead of asking the user to.
"""

from __future__ import annotations

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
        return any(_within(path, root) for root in stdlib) and not any(_within(path, root) for root in site)

    found: list[Path] = []
    candidates = [Path(entry) for entry in sys.path if entry]
    candidates.append(Path(__file__).resolve().parent.parent)
    for candidate in candidates:
        try:
            path = candidate.resolve()
        except OSError:
            continue
        if not path.is_dir() or is_stdlib(path) or path in found or path in roots:
            continue
        found.append(path)
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
