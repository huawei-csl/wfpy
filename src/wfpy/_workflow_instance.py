"""Workflow instances: a nested workflow answered by a run that already happened.

A workflow that was run and has output boundary ports leaves its outputs in
``run.wf-run.json``. Nesting it with ``child(instance="wf-out/<run>")`` hands
the parent those outputs instead of running the child again, so the node is a
source: it has nothing to wait for and emits each recorded token once.

A child with input boundary ports cannot be an instance — what it emits would
depend on what the parent sends it, and only running it again answers that.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

RUN_RECORD = "run.wf-run.json"


@dataclasses.dataclass(frozen=True)
class WorkflowInstance:
    """The recorded outputs of one run of a workflow, port → tokens."""

    run_dir: Path
    outputs: dict[str, list[Any]]


def load_workflow_instance(wf_def: Any, path: str | Path) -> WorkflowInstance:
    """Read the run of *wf_def* at *path* (its run directory or its run record)."""

    name = wf_def.name
    if wf_def.input_names:
        raise TypeError(
            f"{name}(instance=...): an instance has no inputs, but {name} declares "
            f"{', '.join(sorted(wf_def.input_names))}. A workflow fed by its parent "
            "must run again."
        )

    record_path = Path(path).expanduser().resolve()
    if record_path.is_dir():
        record_path = record_path / RUN_RECORD
    if not record_path.is_file():
        raise FileNotFoundError(f"{name}(instance=...): no {RUN_RECORD} at {record_path}")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{name}(instance=...): cannot read {record_path}: {exc}") from exc

    if record.get("error"):
        raise ValueError(f"{name}(instance=...): the run at {record_path.parent} failed")
    recorded_name = record.get("workflowName")
    if recorded_name != name:
        raise ValueError(
            f"{name}(instance=...): the run at {record_path.parent} is of "
            f"{recorded_name!r}, not {name!r}"
        )

    recorded = record.get("outputs") or {}
    outputs: dict[str, list[Any]] = {}
    for port in wf_def.output_names:
        value = recorded.get(port)
        if value is None:
            continue  # the run emitted nothing there
        outputs[port] = value if isinstance(value, list) else [value]
    return WorkflowInstance(run_dir=record_path.parent, outputs=outputs)


# ── Discovery: the runs in a project that can be nested as an instance ──────

_SKIPPED_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build", "out"}
MAX_DISCOVERED_RUNS = 500


def _declared_workflow_ports(source: Path, name: str) -> tuple[list[str], list[str]] | None:
    """The input and output ports ``@workflow(...)`` declares on *name* in *source*."""
    import ast

    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != name:
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            target = call.func if call is not None else decorator
            label = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if label != "workflow":
                continue
            ports: dict[str, list[str]] = {"inputs": [], "outputs": []}
            for keyword in call.keywords if call is not None else []:
                if keyword.arg in ports and isinstance(keyword.value, ast.Dict):
                    ports[keyword.arg] = [
                        key.value
                        for key in keyword.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    ]
            return ports["inputs"], ports["outputs"]
    return None


def import_module_for(target: Path, from_file: Path) -> str | None:
    """The module *from_file* imports *target* as, on the path ``wfpy run`` sets up.

    ``None`` when it is the same file, or when no root on that path holds it.
    """
    from wfpy.cli import _module_search_paths

    target = target.resolve()
    if target == from_file.resolve():
        return None
    for root in _module_search_paths(from_file):
        try:
            parts = target.with_suffix("").relative_to(root).parts
        except ValueError:
            continue
        if parts and all(part.isidentifier() for part in parts):
            return ".".join(parts)
    return None


def _scan_run_records(root: Path) -> list[Path]:
    found: list[Path] = []
    stack = [root]
    while stack and len(found) < MAX_DISCOVERED_RUNS:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if not entry.name.startswith(".") and entry.name not in _SKIPPED_DIRS:
                    stack.append(entry)
            elif entry.name == RUN_RECORD:
                found.append(entry)
    return found


def discover_workflow_instances(from_file: str | Path) -> list[dict[str, Any]]:
    """Every run in *from_file*'s project that can be nested in it as an instance.

    A run qualifies when it finished, the workflow it ran is still defined
    where it ran, and that workflow takes no inputs. Each candidate names the
    workflow (``type``), the run as *from_file* would write it (``value``,
    relative to its directory) and what to import (``nodeArgs.importFrom``).
    Newest first.
    """
    import os

    from wfpy.cli import _discover_project_root

    edited = Path(from_file).resolve()
    root = _discover_project_root(edited.parent) or edited.parent
    candidates: list[tuple[str, dict[str, Any]]] = []
    for record_path in _scan_run_records(root):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("error"):
            continue
        name = record.get("workflowName")
        source = record.get("sourcePath")
        if not isinstance(name, str) or not isinstance(source, str) or not source:
            continue
        source_path = Path(source)
        if not source_path.is_absolute():
            source_path = Path(str(record.get("cwd") or root)) / source_path
        declared = _declared_workflow_ports(source_path, name)
        if declared is None:
            continue
        inputs, outputs = declared
        if inputs:
            continue

        run_dir = record_path.parent
        recorded = record.get("outputs") or {}
        produced = [port for port in outputs if recorded.get(port) is not None]
        finished = str(record.get("finishedAt") or "")
        module = import_module_for(source_path, edited)
        candidate: dict[str, Any] = {
            "label": name,
            "description": f"{run_dir.name}  {finished[:19].replace('T', ' ')}".strip(),
            "detail": "outputs: " + (", ".join(produced) if produced else "none recorded"),
            "type": name,
            "value": Path(os.path.relpath(run_dir, edited.parent)).as_posix(),
        }
        if module:
            candidate["nodeArgs"] = {"importFrom": module}
        candidates.append((finished, candidate))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _finished, candidate in candidates]
