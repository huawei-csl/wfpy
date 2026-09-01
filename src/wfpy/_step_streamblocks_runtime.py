"""Firing a StreamBlocks `instance` node.

The `design` facade is an ordinary task and never reaches here; `runner.py`
sends it to `_step_internal`, because a design node declares its own ports and
actions and wfpy imposes no shape on it.

An `instance` is a typed facade over `calpy run`. CalPy already ships the main —
``calpy run <file.py> --input <file>`` compiles and runs a network in one step —
so nothing here drives a network. It maps the node's ports onto that command's
flags, runs it, and turns what came back into tokens.

Artifacts follow the naming the external-tool step already uses,
``{actor}__{port}__{fire_count}``, which is what makes a design that is rewritten
inside one run keep every version instead of overwriting itself: in dataflow each
firing already produces its own token, so a folder-typed port simply makes that
token a directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .core import TaskMeta
from .types import infer_resource_kind

#: Flag carrying the stimulus when a node does not say otherwise.
DEFAULT_INPUT_FLAG = "--input"

#: Executable providing `calpy run`. Overridable for a venv that has it.
CALPY_COMMAND_ENV = "WFPY_CALPY_COMMAND"


def _calpy_command() -> str:
    return os.environ.get(CALPY_COMMAND_ENV, "").strip() or "calpy"


def _port_is_folder(port_desc: Any) -> bool:
    """Whether a port carries a directory rather than a file."""
    kind = getattr(getattr(port_desc, "port_type", None), "kind", "")
    return infer_resource_kind_safe(kind) == "folder"


def infer_resource_kind_safe(value: Any) -> str:
    try:
        return infer_resource_kind(value)
    except Exception:
        return ""


def _step_streamblocks_instance(
    actor: Any,
    out_dir: Path,
    plan: Any,
    verbose: bool,
) -> bool:
    """Fire a StreamBlocks instance if every input port has a token."""
    meta: TaskMeta = actor.meta
    annotation = meta.annotations.get("streamblocks") or {}
    network = str(annotation.get("network") or "").strip()
    if not network:
        # The decorator refuses this, so reaching it means the annotation was
        # built by something else. Refusing to fire beats running `calpy run`
        # with no network and reporting whatever it says about the argument.
        raise RuntimeError(
            f"StreamBlocks instance {actor.name!r} has no network= to run"
        )

    # Every input must have a token, as for any other actor.
    for port_name in meta.input_ports:
        queues = actor.in_queues.get(port_name) or []
        if not queues or any(q.size() == 0 for q in queues):
            return False

    input_values: dict[str, str] = {}
    for port_name in meta.input_ports:
        for q in actor.in_queues.get(port_name, []):
            input_values[port_name] = str(q.dequeue())

    # One directory per firing for anything folder-shaped, so a rewritten design
    # keeps its earlier versions instead of overwriting them.
    output_values: dict[str, str] = {}
    for port_name, port_desc in meta.output_ports.items():
        ext = getattr(port_desc, "ext", "") or ""
        target = out_dir / f"{actor.name}__{port_name}__{actor.fire_count}{ext}"
        output_values[port_name] = str(target)

    flags: dict[str, str] = dict(annotation.get("flags") or {})
    argv: list[str] = [_calpy_command(), "run", network]

    # An input maps to the flag the node named for it. With nothing said and a
    # single input, it is the stimulus — `--input` is required by `calpy run`.
    for port_name, value in input_values.items():
        flag = flags.get(port_name)
        if flag is None and len(input_values) == 1:
            flag = DEFAULT_INPUT_FLAG
        if flag:
            argv += [flag, value]

    # A folder output asks calpy to keep what it built; anything else is a file
    # the command writes where it is told.
    artifact_port: str | None = None
    for port_name, port_desc in meta.output_ports.items():
        flag = flags.get(port_name)
        if flag:
            argv += [flag, output_values[port_name]]
        elif _port_is_folder(port_desc) and artifact_port is None:
            artifact_port = port_name
            argv.append("--keep-artifacts")

    if verbose:
        print(f"[wfpy][streamblocks] {actor.name}: {' '.join(argv)}", flush=True)

    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=str(getattr(plan, "source_dir", None) or Path.cwd()),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"calpy run (actor {actor.name!r}, network {network!r}) exited with "
            f"code {proc.returncode}:\n{proc.stderr}"
        )

    # `--keep-artifacts` leaves the build beside the network; collect it into
    # this firing's own directory so the next pass cannot overwrite it.
    if artifact_port is not None:
        _collect_artifacts(network, Path(output_values[artifact_port]))

    for port_name, value in output_values.items():
        for q in actor.out_queues.get(port_name, []):
            q.enqueue(value)

    actor.fire_count += 1
    return True


def _collect_artifacts(network: str, destination: Path) -> None:
    """Move a run's kept artifacts into this firing's own folder."""
    produced = Path(network).resolve().parent / "calpy-out"
    destination.mkdir(parents=True, exist_ok=True)
    if not produced.is_dir():
        return
    for entry in produced.iterdir():
        target = destination / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)
