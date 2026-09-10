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
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .core import TaskMeta
from .types import Resource, infer_resource_kind

#: Flag carrying the stimulus when a node does not say otherwise.
DEFAULT_INPUT_FLAG = "--input"

#: Executable providing `calpy run`. Overridable for a venv that has it.
CALPY_COMMAND_ENV = "WFPY_CALPY_COMMAND"


def _calpy_command() -> str:
    return os.environ.get(CALPY_COMMAND_ENV, "").strip() or "calpy"


def _port_is_folder(port_desc: Any) -> bool:
    """Whether a port carries a directory rather than a file.

    The Resource itself is handed to `infer_resource_kind`, not its `kind`
    string: that function takes a Resource OR a locator, so passing the bare
    word "folder" made it parse it as a path and answer something else. The
    Resource path also normalises `dir` and `directory` for free.
    """
    port_type = getattr(port_desc, "port_type", None)
    if not isinstance(port_type, Resource):
        return False
    return infer_resource_kind(port_type) == "folder"


def _hand_on_network(actor: Any, network: str) -> bool:
    """Fire a ``run=False`` instance: pass its network on, run nothing.

    Such a node stands for the design in a flow that lowers it rather than
    running it. It fires once per token on its connected inputs and, with
    none connected, once.
    """
    meta: TaskMeta = actor.meta
    connected = [name for name in meta.input_ports if actor.in_queues.get(name)]
    if connected:
        if any(q.size() == 0 for name in connected for q in actor.in_queues[name]):
            return False
        for name in connected:
            for q in actor.in_queues[name]:
                q.dequeue()
    elif actor.fire_count > 0:
        return False

    for port_name in meta.output_ports:
        for q in actor.out_queues.get(port_name, []):
            q.enqueue(network)
    actor.fire_count += 1
    return True


def _step_streamblocks_instance(
    actor: Any,
    out_dir: Path,
    plan: Any,
    verbose: bool,
) -> bool:
    """Fire a StreamBlocks instance if every input port has a token."""
    meta: TaskMeta = actor.meta
    annotation = meta.annotations.get("streamblocks") or {}
    # The decorator's `network=`, or this node's own `network` parameter.
    network = str(
        annotation.get("network") or getattr(actor.instance, "network", "") or ""
    ).strip()
    if not network:
        # The decorator refuses this, so reaching it means the annotation was
        # built by something else. Refusing to fire beats running `calpy run`
        # with no network and reporting whatever it says about the argument.
        raise RuntimeError(
            f"StreamBlocks instance {actor.name!r} has no network= to run"
        )
    if annotation.get("run") is False:
        return _hand_on_network(actor, network)

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
        env=_environment(plan, annotation),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"calpy run (actor {actor.name!r}, network {network!r}) exited with "
            f"code {proc.returncode}:\n{proc.stderr}"
        )

    # Collect what the build left into this firing's own directory, so the next
    # pass cannot overwrite it.
    if artifact_port is not None:
        _collect_artifacts(proc.stdout, Path(output_values[artifact_port]), actor.name)

    for port_name, value in output_values.items():
        for q in actor.out_queues.get(port_name, []):
            q.enqueue(value)

    actor.fire_count += 1
    return True


def _environment(plan: Any, annotation: dict[str, Any]) -> dict[str, str]:
    """The environment `calpy run` is given.

    The same contract an external tool gets — OS env, then the workflow's
    `@config(env=)`, then anything the node itself declares, with the
    workflow's search paths ahead of PATH. This step used to pass no
    environment at all, so it inherited the OS one and a workflow could not
    reach it: `@config(env={"CALPY_CLANG": ...})` was silently ignored, which is
    exactly the knob someone reaches for when the default clang is too old.
    """
    env: dict[str, str] = {**os.environ}
    workflow_env = getattr(plan, "env", None)
    if workflow_env:
        env.update({str(k): str(v) for k, v in workflow_env.items()})
    node_env = annotation.get("env")
    if isinstance(node_env, dict):
        env.update({str(k): str(v) for k, v in node_env.items()})

    search_paths = getattr(plan, "search_paths", None)
    if search_paths:
        prefix = os.pathsep.join(str(p) for p in search_paths)
        existing = env.get("PATH", "")
        env["PATH"] = f"{prefix}{os.pathsep}{existing}" if existing else prefix
    return env


def _collect_artifacts(stdout: str, destination: Path, actor_name: str) -> None:
    """Move a run's kept artifacts into this firing's own folder.

    Where they are cannot be guessed. `--keep-artifacts` leaves the build in a
    temp directory with a random suffix — `calpy_native_<random>` — chosen by
    `mkdtemp` at compile time, and no flag selects it. What CAN be relied on is
    that `calpy run` prints the binary it produced, and the binary sits in that
    directory:

        bin_path = work_dir / "decoder_native"   # compile.py
        print(f"  binary: {binary}")             # run.py

    so the parent of the printed path is the directory to collect.

    An earlier version guessed at `calpy-out` beside the network. That folder is
    never created, so the copy silently did nothing and the port still handed
    on a path to an empty directory — a failure that looks exactly like success.
    Hence raising rather than returning quietly when the line is absent.
    """
    match = re.search(r"^\s*binary:\s*(.+?)\s*$", stdout, re.MULTILINE)
    if match is None:
        raise RuntimeError(
            f"StreamBlocks instance {actor_name!r} asked for build artifacts, but "
            f"`calpy run` printed no `binary:` line to locate them. Without it "
            f"there is nothing to collect, and handing on an empty folder would "
            f"look like it had worked."
        )

    work_dir = Path(match.group(1)).parent
    if not work_dir.is_dir():
        raise RuntimeError(
            f"StreamBlocks instance {actor_name!r}: the build directory "
            f"{work_dir} does not exist. `calpy run` needs --keep-artifacts for "
            f"it to survive the run."
        )

    destination.mkdir(parents=True, exist_ok=True)
    for entry in work_dir.iterdir():
        target = destination / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)
