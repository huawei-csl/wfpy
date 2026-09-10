"""Internal helpers for external (tool/subprocess) actor execution."""

from __future__ import annotations

import collections
import copy
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wfpy._agent_validation_runtime import _is_resource_type
from wfpy._context_runtime import _actor_context_policy, _apply_context_patch
from wfpy._run_artifacts import _record_edge_token
from wfpy._validation_runtime import run_port_validators
from wfpy.core import TaskMeta

logger = logging.getLogger("wfpy")

_PLACEHOLDER_RE = re.compile(r"\{(in|out|param)(?:\.(\w+))?\}")

#: Lines of each stream a failing tool's message keeps.
_TAIL_LINES = 60


def _run_streaming(
    cmd: str | list[str],
    *,
    cwd: str,
    env: dict[str, str],
    shell: bool,
) -> tuple[int, str, str]:
    """Run a tool with its output streamed to ours as it comes.

    The last lines of each stream are kept for the failure message: a tool
    that inherited our stdio used to fail with "(no stderr)", hiding the one
    thing a person needs to see.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        bufsize=1,
    )
    out_tail: collections.deque[str] = collections.deque(maxlen=_TAIL_LINES)
    err_tail: collections.deque[str] = collections.deque(maxlen=_TAIL_LINES)

    def pump(stream: Any, sink: Any, tail: collections.deque[str]) -> None:
        for line in stream:
            sink.write(line)
            sink.flush()
            tail.append(line)
        stream.close()

    pumps = [
        threading.Thread(target=pump, args=(proc.stdout, sys.stdout, out_tail), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, sys.stderr, err_tail), daemon=True),
    ]
    for thread in pumps:
        thread.start()
    returncode = proc.wait()
    for thread in pumps:
        thread.join()
    return returncode, "".join(out_tail), "".join(err_tail)


def _failure_output(stdout: str | None, stderr: str | None) -> str:
    """What a failing tool said: its stderr, or its stdout when that is where
    it wrote the error (yosys, export-rtl), cut to the last lines."""
    text = (stderr or "").strip() or (stdout or "").strip()
    if not text:
        return "(no output)"
    return "\n".join(text.splitlines()[-_TAIL_LINES:])


def _substitute_tool_placeholders(
    template: str,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    params: dict[str, Any],
) -> str:
    """Replace ``{in.port}``, ``{out.port}``, ``{param.name}`` in tool args.

    Also supports bare ``{in}`` / ``{out}`` which resolve to the sole
    input/output port (errors if there are multiple).
    """

    def replacer(m: re.Match[str]) -> str:
        kind = m.group(1)
        name = m.group(2)  # None for bare {in}/{out}
        if kind == "in":
            if name is None:
                if len(inputs) != 1:
                    raise ValueError(
                        f"Placeholder {{in}} requires exactly one input port. "
                        f"Use {{in.<port>}}.  Ports: {list(inputs.keys())}"
                    )
                return str(next(iter(inputs.values())))
            return str(inputs.get(name, m.group(0)))
        elif kind == "out":
            if name is None:
                if len(outputs) != 1:
                    raise ValueError(
                        f"Placeholder {{out}} requires exactly one output port. "
                        f"Use {{out.<port>}}.  Ports: {list(outputs.keys())}"
                    )
                return str(next(iter(outputs.values())))
            return str(outputs.get(name, m.group(0)))
        else:  # param
            if name is None:
                return m.group(0)
            return str(params.get(name, m.group(0)))

    return _PLACEHOLDER_RE.sub(replacer, template)


def _step_external(
    actor: Any,
    out_dir: Path,
    plan: Any,
    verbose: bool,
) -> bool:
    """Fire an external tool actor if all input ports have tokens."""
    meta: TaskMeta = actor.meta
    tool_spec = meta.tool_spec
    if tool_spec is None:
        return False

    search_paths = plan.search_paths
    wf_env = plan.env
    source_path = plan.source_path

    policy = _actor_context_policy(actor)

    # Check all input ports have ≥ 1 token
    for port_name in meta.input_ports:
        queues = actor.in_queues.get(port_name, [])
        if not queues or queues[0].size() < 1:
            return False

    # Dequeue one token per input port
    input_values: dict[str, Any] = {}
    for port_name in meta.input_ports:
        q = actor.in_queues[port_name][0]
        input_values[port_name] = q.dequeue()

    # Check input files exist
    for port_name, val in input_values.items():
        if isinstance(val, str) and not os.path.isfile(val):
            logger.warning(
                "Input file for %s.%s does not exist: %s",
                actor.name,
                port_name,
                val,
            )

    # ── Pre-validation: validate File inputs before subprocess ────────
    if plan.validator_registry:
        for port_name, val in input_values.items():
            pd = meta.input_ports.get(port_name)
            if pd and _is_resource_type(pd.port_type) and isinstance(val, str):
                run_port_validators(
                    plan.validator_registry,
                    actor,
                    port_name,
                    pd,
                    val,
                    "input",
                    out_dir=out_dir,
                    verbose=verbose,
                )

    # Build parameters dict
    params: dict[str, Any] = {}
    for pname in meta.parameters:
        params[pname] = getattr(actor.instance, pname, "")

    tool_meta_patch = {
        "baseVersion": plan.context_version,
        "ops": [
            {
                "op": "append",
                "path": f"tools.calls.{actor.name}",
                "value": {
                    "phase": "request",
                    "tool": tool_spec.cmd,
                    "inputs": copy.deepcopy(input_values),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
        ],
    }
    _apply_context_patch(plan, actor, policy, tool_meta_patch, source="external-tool")

    # Prepare output file paths — TS pattern: {instance}__{port}__{fireCount}{ext}
    output_values: dict[str, str] = {}
    for port_name, pd in meta.output_ports.items():
        ext = pd.ext or ""
        out_file = out_dir / f"{actor.name}__{port_name}__{actor.fire_count}{ext}"
        output_values[port_name] = str(out_file)

    # Substitute placeholders in cmd and args
    cmd = _substitute_tool_placeholders(tool_spec.cmd, input_values, output_values, params)
    args = [
        _substitute_tool_placeholders(a, input_values, output_values, params)
        for a in tool_spec.args
    ]

    if verbose:
        logger.info("External tool inputs (%s): %s", actor.name, input_values)
        logger.info("External tool outputs (%s): %s", actor.name, output_values)

    # Find executable — check workflow search_paths first, then system PATH
    executable = None
    if search_paths:
        for sp in search_paths:
            candidate = os.path.join(sp, cmd)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                executable = candidate
                break
    if executable is None:
        executable = shutil.which(cmd)
    if executable is None:
        searched = (search_paths or []) + os.environ.get("PATH", "").split(os.pathsep)
        raise FileNotFoundError(
            f"External tool {cmd!r} not found for actor {actor.name!r}. Searched: {searched}"
        )

    # Run subprocess
    full_cmd = [executable] + args
    run_cmd: str | list[str] = full_cmd
    if tool_spec.shell:
        run_cmd = shlex.join(full_cmd)
    if verbose:
        logger.info("Running: %s", run_cmd if isinstance(run_cmd, str) else " ".join(run_cmd))

    # CWD: @tool(cwd=...) → resolved against source dir; default = source file dir
    source_dir = os.path.dirname(os.path.abspath(source_path)) if source_path else ""
    if tool_spec.cwd:
        cwd = (
            tool_spec.cwd
            if os.path.isabs(tool_spec.cwd)
            else os.path.join(source_dir, tool_spec.cwd)
            if source_dir
            else tool_spec.cwd
        )
    elif source_dir:
        cwd = source_dir
    else:
        cwd = str(out_dir)

    # Merge environment: OS env ← workflow @config(env=) ← per-tool env
    env: dict[str, str] = {**os.environ}
    if wf_env:
        env.update(wf_env)
    if tool_spec.env:
        env.update(tool_spec.env)
    # Prepend search_paths to PATH
    if search_paths:
        prefix = os.pathsep.join(search_paths)
        existing = env.get("PATH", "")
        env["PATH"] = f"{prefix}{os.pathsep}{existing}" if existing else prefix

    tool_started = time.perf_counter()
    if tool_spec.inherit_stdio:
        # Streamed to our own output as it comes, with the last lines kept:
        # a failure then says what the tool said, not "(no stderr)".
        returncode, stdout_text, stderr_text = _run_streaming(
            run_cmd, cwd=cwd, env=env, shell=tool_spec.shell
        )
    else:
        proc = subprocess.run(
            run_cmd,
            cwd=cwd,
            env=env,
            shell=tool_spec.shell,
            capture_output=True,
            text=True,
            errors="replace",
        )
        returncode, stdout_text, stderr_text = proc.returncode, proc.stdout, proc.stderr
    duration_ms = int((time.perf_counter() - tool_started) * 1000)
    print(
        f"[wfpy][tool] {actor.name} finished: duration={duration_ms}ms, exit_code={returncode}",
        flush=True,
    )
    if returncode != 0:
        stderr = _failure_output(stdout_text, stderr_text)
        try:
            _apply_context_patch(
                plan,
                actor,
                policy,
                {
                    "baseVersion": plan.context_version,
                    "ops": [
                        {
                            "op": "append",
                            "path": f"tools.calls.{actor.name}",
                            "value": {
                                "phase": "result",
                                "status": "failed",
                                "exitCode": returncode,
                                "stderr": stderr,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        }
                    ],
                },
                source="external-tool",
            )
        except Exception:
            pass
        raise RuntimeError(
            f"External tool {cmd!r} (actor {actor.name!r}) exited with code "
            f"{returncode}:\n{stderr}"
        )

    # Enqueue output values
    for port_name, file_path in output_values.items():
        for q in actor.out_queues.get(port_name, []):
            q.enqueue(file_path)
            _record_edge_token(plan, q, file_path)

    # ── Post-validation: validate File outputs after subprocess ───────
    if plan.validator_registry:
        for port_name, file_path in output_values.items():
            pd = meta.output_ports.get(port_name)
            if pd and _is_resource_type(pd.port_type):
                run_port_validators(
                    plan.validator_registry,
                    actor,
                    port_name,
                    pd,
                    file_path,
                    "output",
                    out_dir=out_dir,
                    verbose=verbose,
                )

    _apply_context_patch(
        plan,
        actor,
        policy,
        {
            "baseVersion": plan.context_version,
            "ops": [
                {
                    "op": "append",
                    "path": f"tools.calls.{actor.name}",
                    "value": {
                        "phase": "result",
                        "status": "ok",
                        "outputs": copy.deepcopy(output_values),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                }
            ],
        },
        source="external-tool",
    )

    actor.fire_count += 1
    return True
