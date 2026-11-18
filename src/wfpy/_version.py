"""Which wfpy is running, in one line two commands can be compared by."""

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

__all__ = ["runtime_version"]


def runtime_version() -> str:
    """`wfpy <version> from <package directory> (python <major>.<minor>)`,
    the form pip reports itself in.

    Two commands that print the same line run the same wfpy. An IDE that
    lists an agent's skills with `wfpy-sidecar` and runs the workflow with
    `wfpy` compares the two (`wfpy --version`, and `runtimeVersion` in the
    sidecar's capabilities) before it trusts the list for the run.
    """

    try:
        version = metadata.version("wfpy")
    except metadata.PackageNotFoundError:
        version = "unknown"
    package = Path(__file__).resolve().parent
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f"wfpy {version} from {package} (python {python})"
