"""Internal helpers for agent output validation and repair prompts."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from wfpy.core import AgentOutputValidator, AgentSpec, LspServerConfig
from wfpy.types import File, Resource

logger = logging.getLogger("wfpy")


def _is_resource_type(port_type: Any) -> bool:
    """Check if a port type descriptor represents a Resource-like value."""

    if port_type is Resource or port_type is File:
        return True
    if isinstance(port_type, type) and issubclass(port_type, Resource):
        return True
    if isinstance(port_type, Resource):
        return True
    type_str = str(port_type).lower()
    return "resource" in type_str or "file" in type_str


def _get_effective_lsp_configs(spec: AgentSpec) -> list[LspServerConfig]:
    """Resolve effective LSP validation configs from AgentSpec."""

    if spec.lsp_servers:
        return list(spec.lsp_servers)

    if spec.lsp_command:
        return [
            LspServerConfig(
                command=spec.lsp_command,
                args=spec.lsp_args or [],
                language_id=spec.lsp_language_id,
                extra_flags=spec.lsp_extra_flags,
                severity_threshold=spec.lsp_severity_threshold,
                max_repair_attempts=spec.lsp_max_repair_attempts,
            )
        ]

    return []


def _has_lsp_validation(spec: AgentSpec) -> bool:
    """Check if the agent has any LSP validation configured."""

    return bool(_get_effective_lsp_configs(spec))


def _max_validator_attempts(spec: AgentSpec) -> int:
    """Return max retry count across all configured validators."""

    max_attempts = 0
    for lsp_cfg in _get_effective_lsp_configs(spec):
        max_attempts = max(max_attempts, lsp_cfg.max_repair_attempts)
    if spec.output_validators:
        max_attempts = max(
            max_attempts,
            max(v.max_repair_attempts for v in spec.output_validators),
        )
    if max_attempts == 0:
        return 0
    return max_attempts + 1


def build_validator_command(
    validator: AgentOutputValidator,
    output_ports: dict[str, Any],
    work_dir: str,
    kernel_file: str = "kernel.cpp",
    agent_input_paths: dict[str, str] | None = None,
) -> str | None:
    """Build the exact command string for a validator.
    
    Returns None for LSP validators (they don't have commands).
    """
    if validator.kind == "lsp":
        return None
    
    # Build command args with placeholder substitution
    file_dir = str(Path(work_dir).resolve())
    file_path = str(Path(work_dir) / kernel_file)
    
    cmd_args: list[str] = []
    for a in validator.args:
        a = a.replace("{file}", file_path).replace("{file_dir}", file_dir)
        if agent_input_paths:
            for input_name, input_path in agent_input_paths.items():
                a = a.replace(f"{{input_{input_name}}}", input_path)
        cmd_args.append(a)
    
    full_cmd = [validator.cmd] + cmd_args
    return " ".join(full_cmd)


def construct_agent_validation_prompt(
    original_prompt: str,
    validators: list[AgentOutputValidator],
    max_attempts: int,
    output_ports: dict[str, Any],
    work_dir: str,
    agent_input_paths: dict[str, str] | None = None,
    kernel_file: str = "kernel.cpp",
) -> str:
    """Construct prompt that tells agent to validate and fix.
    
    This creates an enhanced prompt that instructs the agent to:
    1. Generate the kernel
    2. Run validators itself
    3. Fix if validation fails
    4. Repeat until success or max attempts
    """
    # Build validator commands
    validator_commands = []
    for validator in validators:
        cmd = build_validator_command(
            validator, output_ports, work_dir, kernel_file, agent_input_paths
        )
        if cmd:
            validator_commands.append(cmd)
    
    if not validator_commands:
        # No command validators, return original prompt
        return original_prompt
    
    # Construct enhanced prompt
    enhanced_prompt = f"""{original_prompt}

## Validation Instructions

After generating the kernel, you MUST validate it before returning.

### Validators to Run
{chr(10).join(f"- {cmd}" for cmd in validator_commands)}

### Validation Process
1. Generate the kernel
2. Save the kernel to a file (e.g., `{kernel_file}`)
3. Run ALL validators listed above
4. Save validation output to a log file (e.g., `validation_attempt_1.log`)
5. If ANY validator fails:
   - Read the validation log file carefully
   - Fix the kernel to address the issues
   - Save the fixed kernel
   - Run ALL validators again
   - Save validation output to a new log file (e.g., `validation_attempt_2.log`)
   - Repeat until ALL validators pass OR you've tried {max_attempts} times
6. Return the final kernel (even if validation still fails after {max_attempts} attempts)

### Important
- You MUST run the validators yourself using the bash tool
- You MUST read the validation output to understand what failed
- You MUST fix the kernel and re-validate if there are errors
- Do NOT return a kernel without validating it first
- If validation fails after {max_attempts} attempts, return the best kernel you have
- Log all validation attempts to separate files (validation_attempt_1.log, validation_attempt_2.log, etc.)
"""
    
    return enhanced_prompt


def _run_agent_output_validators(
    validators: list[AgentOutputValidator],
    materialized: dict[str, Any],
    output_ports: dict[str, Any],
    actor_name: str,
    verbose: bool,
    plan: Any | None = None,
    agent_input_paths: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Run configured output validators against materialized files.

    If ``agent_input_paths`` is provided, ``{input_<PortName>}`` placeholders
    in validator args are replaced with the staged file path for that input port.
    """

    errors: list[dict[str, Any]] = []
    for validator in validators:
        target_ports = validator.ports or [
            p for p, pd in output_ports.items() if _is_resource_type(pd.port_type)
        ]
        for port_name in target_ports:
            file_path = materialized.get(port_name)
            if file_path is None or not os.path.isfile(str(file_path)):
                continue

            if validator.kind == "lsp":
                _run_lsp_validator(
                    validator,
                    port_name,
                    str(file_path),
                    actor_name,
                    verbose,
                    errors,
                )
            else:
                _run_cmd_validator(
                    validator,
                    port_name,
                    str(file_path),
                    actor_name,
                    verbose,
                    errors,
                    plan,
                    agent_input_paths=agent_input_paths,
                )
    return errors


def _run_cmd_validator(
    v: AgentOutputValidator,
    port_name: str,
    file_path: str,
    actor_name: str,
    verbose: bool,
    errors: list[dict[str, Any]],
    plan: Any | None = None,
    agent_input_paths: dict[str, str] | None = None,
) -> None:
    """Run a command-based validator (kind=cmd).

    Supports the following placeholder substitutions in ``v.args``:
    - ``{file}`` → the materialized output file path
    - ``{file_dir}`` → the parent directory of the output file
    - ``{input_<PortName>}`` → the staged file path of the named agent input port
      (only when ``agent_input_paths`` is provided and the port is present)
    """

    file_dir = str(Path(file_path).resolve().parent)
    cmd_args: list[str] = []
    for a in v.args:
        a = a.replace("{file}", file_path).replace("{file_dir}", file_dir)
        if agent_input_paths:
            for input_name, input_path in agent_input_paths.items():
                a = a.replace(f"{{input_{input_name}}}", input_path)
        cmd_args.append(a)
    full_cmd = [v.cmd] + cmd_args
    if verbose:
        logger.info(
            "[wfpy][validator:cmd] %s port=%s: %s",
            actor_name,
            port_name,
            " ".join(full_cmd),
        )

    run_env: dict[str, str] = {**os.environ}
    if plan:
        plan_env = getattr(plan, "env", None)
        if isinstance(plan_env, dict):
            run_env.update(plan_env)
        search_paths = getattr(plan, "search_paths", None)
        if isinstance(search_paths, list) and search_paths:
            prefix = os.pathsep.join(str(p) for p in search_paths)
            existing = run_env.get("PATH", "")
            run_env["PATH"] = f"{prefix}{os.pathsep}{existing}" if existing else prefix
    if v.env:
        run_env.update(v.env)

    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=v.timeout_ms / 1000,
            env=run_env,
            cwd=file_dir,
        )
    except FileNotFoundError:
        errors.append(
            {
                "port": port_name,
                "cmd": " ".join(full_cmd),
                "kind": "cmd",
                "stderr": f"Command not found: {v.cmd}",
                "returncode": -1,
            }
        )
        return
    except subprocess.TimeoutExpired:
        errors.append(
            {
                "port": port_name,
                "cmd": " ".join(full_cmd),
                "kind": "cmd",
                "stderr": f"Validator timed out after {v.timeout_ms}ms",
                "returncode": -1,
            }
        )
        return

    if result.returncode != 0:
        stderr_text = (result.stderr or result.stdout or "").strip()
        parsed_diags = _parse_compiler_diagnostics(stderr_text, file_path)
        err_entry: dict[str, Any] = {
            "port": port_name,
            "cmd": " ".join(full_cmd),
            "kind": "cmd",
            "stderr": stderr_text,
            "returncode": result.returncode,
        }
        if parsed_diags:
            err_entry["diagnostics"] = parsed_diags
            err_entry["kind"] = "lsp"
        errors.append(err_entry)
        if verbose:
            logger.info(
                "[wfpy][validator:cmd] %s port=%s FAILED (rc=%d): %s",
                actor_name,
                port_name,
                result.returncode,
                stderr_text[:300],
            )
    elif verbose:
        logger.info("[wfpy][validator:cmd] %s port=%s PASSED", actor_name, port_name)


_COMPILER_DIAG_RE = re.compile(
    r"^(?P<file>[^:\s]+):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<sev>error|warning|note|info|fatal error):\s*(?P<msg>.+)$",
    re.MULTILINE,
)


def _parse_compiler_diagnostics(stderr: str, file_path: str) -> list[Any]:
    """Parse compiler diagnostics into LspDiagnostic-compatible objects."""

    from wfpy._lsp_client import LspDiagnostic

    del file_path
    diags: list[LspDiagnostic] = []
    for match in _COMPILER_DIAG_RE.finditer(stderr):
        severity = match.group("sev")
        if severity == "fatal error":
            severity = "error"
        elif severity == "note":
            severity = "info"
        diags.append(
            LspDiagnostic(
                line=int(match.group("line")),
                col=int(match.group("col")),
                severity=severity,
                message=match.group("msg").strip(),
                source="compiler",
            )
        )
    return diags


_LSP_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2, "hint": 3}


def _run_lsp_validator(
    v: AgentOutputValidator,
    port_name: str,
    file_path: str,
    actor_name: str,
    verbose: bool,
    errors: list[dict[str, Any]],
) -> None:
    """Run an LSP-based validator (kind=lsp)."""

    from wfpy._lsp_client import validate_file_with_lsp

    if verbose:
        logger.info(
            "[wfpy][validator:lsp] %s port=%s: %s %s (lang=%s)",
            actor_name,
            port_name,
            v.cmd,
            " ".join(v.args),
            v.language_id,
        )

    diagnostics = validate_file_with_lsp(
        server_cmd=v.cmd,
        file_path=file_path,
        language_id=v.language_id,
        server_args=v.args or None,
        root_uri=v.root_uri,
        initialization_options=v.initialization_options,
        timeout_ms=v.timeout_ms,
        extra_flags=v.extra_flags,
    )

    threshold_rank = _LSP_SEVERITY_RANK.get(v.severity_threshold, 0)
    failing = [d for d in diagnostics if _LSP_SEVERITY_RANK.get(d.severity, 0) <= threshold_rank]

    if not failing:
        if verbose:
            logger.info(
                "[wfpy][validator:lsp] %s port=%s PASSED (%d diagnostics, none at threshold '%s')",
                actor_name,
                port_name,
                len(diagnostics),
                v.severity_threshold,
            )
        return

    diag_lines = [d.format(file_path) for d in failing[:30]]
    if len(failing) > 30:
        diag_lines.append(f"  ... and {len(failing) - 30} more diagnostics")
    stderr_text = "\n".join(diag_lines)

    errors.append(
        {
            "port": port_name,
            "cmd": f"{v.cmd} (LSP)",
            "kind": "lsp",
            "stderr": stderr_text,
            "returncode": len(failing),
            "diagnostics": failing,
        }
    )

    if verbose:
        logger.info(
            "[wfpy][validator:lsp] %s port=%s FAILED (%d errors):\n%s",
            actor_name,
            port_name,
            len(failing),
            stderr_text[:500],
        )


def _run_lsp_validation(
    lsp_configs: list[LspServerConfig],
    materialized: dict[str, Any],
    output_ports: dict[str, Any],
    actor_name: str,
    verbose: bool,
) -> list[dict[str, Any]]:
    """Run first-class LSP validation against materialized File outputs."""

    from wfpy._lsp_client import validate_file_with_lsp

    errors: list[dict[str, Any]] = []
    for cfg in lsp_configs:
        target_ports = cfg.ports or [
            p for p, pd in output_ports.items() if _is_resource_type(pd.port_type)
        ]
        for port_name in target_ports:
            file_path = materialized.get(port_name)
            if file_path is None or not os.path.isfile(str(file_path)):
                continue

            if verbose:
                logger.info(
                    "[wfpy][lsp] %s port=%s: %s %s (lang=%s)",
                    actor_name,
                    port_name,
                    cfg.command,
                    " ".join(cfg.args),
                    cfg.language_id,
                )

            diagnostics = validate_file_with_lsp(
                server_cmd=cfg.command,
                file_path=str(file_path),
                language_id=cfg.language_id,
                server_args=cfg.args or None,
                root_uri=cfg.root_uri,
                initialization_options=cfg.initialization_options,
                timeout_ms=cfg.timeout_ms,
                extra_flags=cfg.extra_flags,
            )

            threshold_rank = _LSP_SEVERITY_RANK.get(cfg.severity_threshold, 0)
            failing = [
                d for d in diagnostics if _LSP_SEVERITY_RANK.get(d.severity, 0) <= threshold_rank
            ]

            if not failing:
                if verbose:
                    logger.info(
                        "[wfpy][lsp] %s port=%s PASSED (%d diagnostics, none at threshold '%s')",
                        actor_name,
                        port_name,
                        len(diagnostics),
                        cfg.severity_threshold,
                    )
                continue

            diag_lines = [d.format(str(file_path)) for d in failing[:30]]
            if len(failing) > 30:
                diag_lines.append(f"  ... and {len(failing) - 30} more diagnostics")
            stderr_text = "\n".join(diag_lines)

            errors.append(
                {
                    "port": port_name,
                    "cmd": f"{cfg.command} (LSP)",
                    "kind": "lsp",
                    "stderr": stderr_text,
                    "returncode": len(failing),
                    "diagnostics": failing,
                }
            )

            if verbose:
                logger.info(
                    "[wfpy][lsp] %s port=%s FAILED (%d errors):\n%s",
                    actor_name,
                    port_name,
                    len(failing),
                    stderr_text[:500],
                )
    return errors


def _build_validation_repair_prompt(
    response_text: str,
    val_errors: list[dict[str, Any]],
    output_ports: dict[str, Any],
) -> str:
    """Build a prompt asking the agent to fix output that failed validation."""

    port_names = list(output_ports.keys())
    is_single_code_file = False
    if len(output_ports) == 1:
        single_desc = next(iter(output_ports.values()))
        ext = str(getattr(single_desc, "ext", "") or "").strip().lower()
        is_single_code_file = ext in {".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".cu"}

    has_lsp = any(e.get("kind") == "lsp" for e in val_errors)
    if has_lsp:
        return _build_lsp_repair_prompt(
            response_text,
            val_errors,
            output_ports,
            is_single_code_file=is_single_code_file,
        )

    error_summary = "\n".join(
        f"- Port '{e['port']}': command `{e['cmd']}` failed (exit {e['returncode']}):\n"
        f"  {e['stderr'][:800]}"
        for e in val_errors
    )
    preview = response_text[:2000]
    if len(response_text) > 2000:
        preview += "\n... (truncated)"
    output_instruction = (
        "Return ONLY the corrected raw file content for the failing port(s). "
        "Do not wrap it in JSON, markdown, XML, quotes, or commentary."
        if is_single_code_file
        else "Return the corrected content for the failing port(s)."
    )
    banner_instruction = (
        "If the previous file started with a required leading comment banner or other required "
        "non-code preamble before the first include or declaration, preserve it unless the "
        "validator errors explicitly require changing that banner.\n"
        if is_single_code_file
        else ""
    )
    return (
        "Your previous output failed compilation/validation checks and must be fixed.\n\n"
        "CRITICAL: Do NOT explain the error, do NOT analyze it, do NOT wrap the output in JSON, "
        "do NOT add markdown fences, XML tags, or commentary. Output ONLY the corrected code.\n"
        "Start the first line with code. No preamble whatsoever — the first character "
        "must be code or a comment.\n\n"
        "## Validation errors\n"
        f"{error_summary}\n\n"
        "## Instructions\n"
        "Fix ONLY the issues identified by the validator.\n"
        "Preserve the previously valid content for every non-failing output port; do not blank, omit, "
        "or replace sibling ports while repairing one port.\n"
        f"{banner_instruction}"
        f"{output_instruction}\n"
        f"Output ports: {', '.join(port_names)}\n\n"
        "## Previous (failing) response\n"
        f"{preview}"
    )


def _build_lsp_repair_prompt(
    response_text: str,
    val_errors: list[dict[str, Any]],
    output_ports: dict[str, Any],
    *,
    is_single_code_file: bool = False,
) -> str:
    """Build a structured LSP-diagnostic repair prompt with line context."""

    lines = response_text.splitlines()
    port_names = list(output_ports.keys())
    sections: list[str] = []
    sections.append(
        "Your previous output has compilation/syntax errors detected by the language server.  "
        "CRITICAL: Do NOT explain the errors, do NOT analyze them, do NOT wrap in JSON. "
        "Output ONLY the corrected code. Start with code on the first line — "
        "no preamble, no commentary, no markdown fences."
    )

    non_lsp_errors = [err for err in val_errors if err.get("kind") != "lsp"]
    if non_lsp_errors:
        sections.append("\n### Additional validator failures")
        for err in non_lsp_errors:
            sections.append(
                f"- Port '{err.get('port', '?')}': `{err.get('cmd', '?')}` failed "
                f"(exit {err.get('returncode', -1)}): {str(err.get('stderr', ''))[:800]}"
            )

    for err in val_errors:
        diags = err.get("diagnostics", [])
        port = err.get("port", "?")
        cmd = err.get("cmd", "?")

        if not diags:
            sections.append(
                f"\n### Port '{port}' — `{cmd}` failed (exit {err.get('returncode', -1)})\n"
                f"```\n{err.get('stderr', '')[:800]}\n```"
            )
            continue

        sections.append(f"\n### Port '{port}' — {len(diags)} diagnostic(s) from {cmd}")

        for diag in diags[:20]:
            sev = getattr(diag, "severity", "error")
            msg = getattr(diag, "message", "")
            line_no = getattr(diag, "line", 0)
            col = getattr(diag, "col", 0)
            code = getattr(diag, "code", "")

            code_str = f" [{code}]" if code else ""
            sections.append(f"\n**Line {line_no}, col {col}** — {sev}{code_str}: {msg}")

            if 1 <= line_no <= len(lines):
                ctx_start = max(0, line_no - 4)
                ctx_end = min(len(lines), line_no + 3)
                ctx_lines: list[str] = []
                for i in range(ctx_start, ctx_end):
                    marker = ">>>" if (i + 1) == line_no else "   "
                    ctx_lines.append(f"  {marker} {i + 1:4d} | {lines[i]}")
                sections.append("```")
                sections.append("\n".join(ctx_lines))
                sections.append("```")

        if len(diags) > 20:
            sections.append(f"\n... and {len(diags) - 20} more diagnostics (fix the above first)")

    sections.append(f"\nOutput ports: {', '.join(port_names)}")
    sections.append(
        "Preserve the previously valid content for every non-failing output port; do not blank, "
        "omit, or replace sibling ports while repairing one port."
    )
    if is_single_code_file:
        sections.append(
            "If the previous file started with a required leading comment banner or other required "
            "non-code preamble before the first include or declaration, preserve it unless the "
            "diagnostics explicitly require changing that banner."
        )
        sections.append(
            "\nReturn the complete corrected raw file content for the failing port(s). "
            "Do not wrap it in JSON, markdown, XML, quotes, or commentary."
        )
    else:
        sections.append("\nReturn the complete corrected code for the failing port(s).")
    return "\n".join(sections)
