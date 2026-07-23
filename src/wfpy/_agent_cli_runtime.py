"""Internal helpers for CLI-based agent transports."""

from __future__ import annotations

import errno
import json
import os
import queue
import shlex
import subprocess
import tempfile
import threading
import time
from typing import Any

from wfpy.core import AgentSpec


def _normalize_agent_transport(spec: AgentSpec) -> str:
    """Normalize and validate the agent transport selection."""

    raw = str(spec.transport or "http").strip().lower()
    aliases = {
        "http": "http",
        "openai": "http",
        "openai-http": "http",
        "opencode": "opencode-acp",
        "opencode-cli": "opencode-cli",
        "opencode-acp": "opencode-acp",
        "claude": "claude-cli",
        "claude-code": "claude-cli",
        "claude-cli": "claude-cli",
        "codex": "codex-cli",
        "codex-cli": "codex-cli",
        # Offline: fires the actor and emits port-shaped values without a model.
        "mock": "mock",
        "offline": "mock",
    }
    normalized = aliases.get(raw)
    if normalized is None:
        raise ValueError(
            f"Unsupported agent transport '{spec.transport}'. "
            "Supported values: http, opencode-cli, opencode-acp, claude-cli, "
            "codex-cli, mock."
        )
    return normalized


def _normalize_cli_tools_mode(spec: AgentSpec, options: dict[str, Any] | None = None) -> str:
    """Normalize CLI tool mode for non-http transports.

    Modes:
    - ``wfpy-none``: keep wfpy deterministic single-response behavior
    - ``native``: allow backend-native tool execution where supported
    """

    opts = options or {}
    raw = (
        str(opts.get("agent_cli_tools_mode") or spec.cli_tools_mode or "wfpy-none").strip().lower()
    )
    aliases = {
        "wfpy-none": "wfpy-none",
        "none": "wfpy-none",
        "disabled": "wfpy-none",
        "off": "wfpy-none",
        "native": "native",
        "on": "native",
        "enabled": "native",
    }
    normalized = aliases.get(raw)
    if normalized is None:
        raise ValueError(
            f"Unsupported cli_tools_mode '{raw}'. Supported values: wfpy-none, native."
        )
    return normalized


def _expand_command(raw: str, default_executable: str) -> list[str]:
    """Expand a configurable command string into argv."""

    text = str(raw).strip()
    if not text:
        return [default_executable]
    expanded = shlex.split(text)
    return expanded or [default_executable]


def _extract_text_from_json_output(raw_stdout: str) -> str | None:
    """Best-effort extraction of textual content from JSON CLI output."""

    text = raw_stdout.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict):
        for key in ("text", "content", "output", "response"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value
        message = parsed.get("message")
        if isinstance(message, str) and message.strip():
            return message
        if isinstance(message, dict):
            message_text = message.get("content") or message.get("text")
            if isinstance(message_text, str) and message_text.strip():
                return message_text
        return text
    if isinstance(parsed, list):
        parts: list[str] = []
        for item in parsed:
            if isinstance(item, str) and item.strip():
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value)
        if parts:
            return "\n".join(parts)
        return text
    return text


def _build_cli_prompt(effective_prompt: str, payload_text: str) -> str:
    """Build a deterministic prompt for CLI transports."""

    return f"{effective_prompt}\n\nRuntime payload (strict JSON):\n{payload_text}"


def _build_cli_prompt_with_tools_mode(
    effective_prompt: str,
    payload_text: str,
    cli_tools_mode: str,
) -> str:
    """Build prompt and enforce deterministic JSON contract for wfpy-none mode."""

    base = _build_cli_prompt(effective_prompt, payload_text)
    if cli_tools_mode == "native":
        return base
    return (
        f"{base}\n\n"
        "Tool policy: wfpy-none. Do not use external tools, shell commands, or MCP. "
        "Return only final strict JSON output."
    )


def _is_argument_list_too_long(exc: OSError) -> bool:
    """Return True when a CLI spawn failed because argv exceeded OS limits."""

    if getattr(exc, "errno", None) == errno.E2BIG:
        return True
    return "argument list too long" in str(exc).lower()


def _opencode_prompt_file_message(filename: str) -> str:
    """Return the short fallback instruction used with attached prompt files."""

    return (
        f"The full wfpy task instructions are attached in `{filename}`. "
        "Read that attached file completely and follow it exactly. "
        "Return only the final response requested by that file."
    )


def _resolve_opencode_prompt_stage_dir(options: dict[str, Any] | None = None) -> str | None:
    """Choose a workspace-local directory for attached OpenCode prompts when possible."""

    opts = options or {}
    candidate_roots: list[str] = []

    debug_dir = str(opts.get("agent_debug_dir") or "").strip()
    if debug_dir:
        candidate_roots.append(os.path.join(debug_dir, "work", "_opencode_prompts"))

    run_out_dir = str(os.environ.get("WF_RUN_OUT_DIR") or "").strip()
    if run_out_dir:
        candidate_roots.append(os.path.join(run_out_dir, "work", "_opencode_prompts"))

    for candidate in candidate_roots:
        try:
            os.makedirs(candidate, exist_ok=True)
        except OSError:
            continue
        return candidate

    return None


# ── Streaming opencode JSONL event processor ───────────────────────────


def _process_opencode_event(
    line: str,
    *,
    text_parts: list[str],
    debug_meta: dict[str, Any],
    emit_prefix: str = "[wfpy][opencode]",
) -> None:
    """Process one JSONL event line from opencode's stdout.

    Updates *text_parts* and *debug_meta* in-place and emits real-time
    output for thinking, tool-call, and tool-result events.
    """

    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        debug_meta["opencodeParseErrors"] = debug_meta.get("opencodeParseErrors", 0) + 1
        return
    if not isinstance(event, dict):
        return

    debug_meta["_opencodeParsedCount"] = debug_meta.get("_opencodeParsedCount", 0) + 1

    event_type = str(event.get("type", "")).strip().lower()
    if event_type:
        event_type_counts: dict[str, int] = debug_meta.setdefault("opencodeEventTypes", {})
        event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1

    part = event.get("part")
    event_text: str | None = None
    if isinstance(part, dict):
        part_text = part.get("text")
        if isinstance(part_text, str) and part_text:
            event_text = part_text
    if event_text is None:
        top_text = event.get("text")
        if isinstance(top_text, str) and top_text:
            event_text = top_text

    # ── Real-time emission ──────────────────────────────────────────
    if event_type in ("thinking", "reasoning") and event_text:
        preview = event_text[:200]
        print(f"{emit_prefix} thinking: {preview}", flush=True)
    elif event_type in ("tool_call", "tool_use", "mcp_call"):
        tool_name = ""
        if isinstance(part, dict):
            tool_name = str(
                part.get("name") or part.get("tool") or part.get("toolName") or ""
            ).strip()
        if not tool_name:
            tool_name = str(
                event.get("name") or event.get("tool") or event.get("toolName") or ""
            ).strip()
        server = ""
        if isinstance(part, dict):
            server = str(
                part.get("server") or part.get("mcpServer") or part.get("serverName") or ""
            ).strip()
        label = f"{server}.{tool_name}" if server and tool_name else (tool_name or event_type)
        print(f"{emit_prefix} tool: {label}", flush=True)
    elif event_type in ("tool_result", "result") and event_text:
        preview = event_text[:120]
        print(f"{emit_prefix} result: {preview}", flush=True)

    # ── Accumulate text for final response ──────────────────────────
    if event_text:
        text_parts.append(event_text)

    # ── Tokens / usage from step_finish ─────────────────────────────
    if event_type == "step_finish" and isinstance(part, dict):
        tokens = part.get("tokens")
        if isinstance(tokens, dict):
            debug_meta.setdefault("usage", {
                "total_tokens": int(tokens.get("total", 0) or 0),
                "prompt_tokens": int(tokens.get("input", 0) or 0),
                "completion_tokens": int(tokens.get("output", 0) or 0),
                "num_requests": 1,
                "tool_rounds": 0,
            })

    # ── Tool / MCP metadata for debug ───────────────────────────────
    for container in (event, part):
        if not isinstance(container, dict):
            continue

        # session ID
        session_value = (
            container.get("sessionID")
            or container.get("sessionId")
            or container.get("session_id")
        )
        session_obj = container.get("session")
        if not session_value and isinstance(session_obj, dict):
            session_value = (
                session_obj.get("id")
                or session_obj.get("sessionID")
                or session_obj.get("sessionId")
            )
        if isinstance(session_value, str) and session_value.strip():
            debug_meta["opencodeSessionID"] = session_value.strip()

        # tool name
        evt_tool_name = container.get("name") or container.get("tool") or container.get("toolName")
        if isinstance(evt_tool_name, str) and evt_tool_name.strip():
            tool_names: set[str] = debug_meta.setdefault("_tool_names", set())
            tool_names.add(evt_tool_name.strip())

        # server name
        server_name = (
            container.get("server") or container.get("mcpServer") or container.get("serverName")
        )
        if isinstance(server_name, str) and server_name.strip():
            servers: set[str] = debug_meta.setdefault("_mcp_servers", set())
            servers.add(server_name.strip())

    # track tool/mcp events
    if event_type.startswith("tool") or "tool" in event_type or "mcp" in event_type:
        debug_meta["opencodeToolEvents"] = debug_meta.get("opencodeToolEvents", 0) + 1


def _finalise_opencode_debug_meta(debug_meta: dict[str, Any]) -> None:
    """Convert internal tracking sets to sorted lists in *debug_meta*."""

    tool_names: set[str] = debug_meta.pop("_tool_names", set())
    mcp_servers: set[str] = debug_meta.pop("_mcp_servers", set())
    debug_meta.pop("_opencodeParsedCount", None)

    if tool_names:
        debug_meta["opencodeToolNames"] = sorted(tool_names)
    if mcp_servers:
        debug_meta["opencodeMcpServers"] = sorted(mcp_servers)


# ── Low-level streaming Popen wrapper ──────────────────────────────────


def _run_opencode_process(
    cmd: list[str],
    *,
    timeout_s: float,
    env: dict[str, str],
    emit_prefix: str = "[wfpy][opencode]",
) -> tuple[str, dict[str, Any], int, str]:
    """Spawn *open code*, stream JSONL events, return (text, debug, rc, stderr).

    Real-time thinking, tool-call, and tool-result events are printed to stdout
    with *emit_prefix*.
    """

    text_parts: list[str] = []
    debug_meta: dict[str, Any] = {}
    stderr_chunks: list[str] = []

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        raise
    except OSError:
        raise

    line_queue: queue.Queue[str | None] = queue.Queue()
    stderr_queue: queue.Queue[str | None] = queue.Queue()

    def _reader(pipe: Any, target_queue: queue.Queue[str | None]) -> None:
        try:
            for line in pipe:
                target_queue.put(line)
        except (ValueError, OSError):
            pass
        finally:
            target_queue.put(None)

    reader = threading.Thread(target=_reader, args=(proc.stdout, line_queue), daemon=True)
    reader.start()
    stderr_reader = threading.Thread(
        target=_reader, args=(proc.stderr, stderr_queue), daemon=True
    )
    stderr_reader.start()

    deadline = time.monotonic() + timeout_s
    text_done = False
    stderr_done = False

    try:
        while not (text_done and stderr_done):
            if not text_done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout_s)
                try:
                    raw_line = line_queue.get(timeout=min(remaining, 0.2))
                except queue.Empty:
                    continue
                if raw_line is None:
                    text_done = True
                else:
                    line = raw_line.rstrip("\n\r")
                    if line:
                        _process_opencode_event(
                            line,
                            text_parts=text_parts,
                            debug_meta=debug_meta,
                            emit_prefix=emit_prefix,
                        )

            if not stderr_done:
                try:
                    raw_line = stderr_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if raw_line is None:
                    stderr_done = True
                else:
                    stderr_chunks.append(raw_line)

        reader.join(timeout=2)
        stderr_reader.join(timeout=2)
        proc.wait(timeout=5)

    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            reader.join(timeout=2)
        except RuntimeError:
            pass
        raise

    returncode = proc.returncode
    stderr_text = "".join(stderr_chunks)

    if returncode < 0 and returncode > -256:
        raise OSError(-returncode, os.strerror(-returncode))

    _finalise_opencode_debug_meta(debug_meta)

    content = "\n".join(part for part in text_parts if part.strip()).strip()
    parsed_count = debug_meta.get("_opencodeParsedCount", 0)
    parse_errors = debug_meta.get("opencodeParseErrors", 0)
    if not content:
        debug_meta.pop("opencodeParseErrors", None)
        if parsed_count == 0 or parse_errors > 0:
            # fall back to raw — but with streaming there's no saved raw text
            debug_meta["opencodeRawFallback"] = True
        else:
            debug_meta["opencodeNoTextEvent"] = True

    return content, debug_meta, returncode, stderr_text


def _run_opencode_with_attached_prompt(
    cmd_base: list[str],
    full_prompt: str,
    *,
    timeout_s: float,
    env: dict[str, str],
    prompt_stage_dir: str | None = None,
) -> tuple[str, dict[str, Any], int, str, list[str]]:
    """Retry OpenCode with the prompt staged to a temp file (streaming)."""

    prompt_path: str | None = None
    prompt_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        prompt_dir = tempfile.TemporaryDirectory(
            prefix="wfpy_opencode_prompt_",
            dir=prompt_stage_dir,
        )
        prompt_path = os.path.join(prompt_dir.name, "wfpy_prompt.txt")
        with open(prompt_path, "w", encoding="utf-8") as tmp:
            tmp.write(full_prompt)
        fallback_cmd = [
            *cmd_base,
            "--file",
            prompt_path,
            "--",
            _opencode_prompt_file_message(os.path.basename(prompt_path)),
        ]
        content, debug, rc, stderr_text = _run_opencode_process(
            fallback_cmd,
            timeout_s=timeout_s,
            env=env,
        )
        return content, debug, rc, stderr_text, fallback_cmd
    finally:
        if prompt_dir is not None:
            try:
                prompt_dir.cleanup()
            except OSError:
                pass


# ── Public entry points ───────────────────────────────────────────────────


def _parse_opencode_json_events(raw_stdout: str) -> tuple[str, dict[str, Any]]:
    """Parse JSONL events emitted by `opencode run --format json`."""

    text_parts: list[str] = []
    usage: dict[str, Any] = {}
    parse_errors = 0
    parsed_event_count = 0
    event_type_counts: dict[str, int] = {}
    tool_event_count = 0
    tool_names: set[str] = set()
    mcp_server_names: set[str] = set()
    opencode_session_id: str | None = None
    lines = [line for line in raw_stdout.splitlines() if line.strip()]

    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if not isinstance(event, dict):
            continue
        parsed_event_count += 1

        event_type = str(event.get("type", "")).strip().lower()
        if event_type:
            event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
        part = event.get("part")
        event_text: str | None = None
        if isinstance(part, dict):
            part_text = part.get("text")
            if isinstance(part_text, str) and part_text:
                event_text = part_text
        if event_text is None:
            top_text = event.get("text")
            if isinstance(top_text, str) and top_text:
                event_text = top_text
        if event_text:
            text_parts.append(event_text)

        if event_type == "step_finish" and isinstance(part, dict):
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                usage = {
                    "total_tokens": int(tokens.get("total", 0) or 0),
                    "prompt_tokens": int(tokens.get("input", 0) or 0),
                    "completion_tokens": int(tokens.get("output", 0) or 0),
                    "num_requests": 1,
                    "tool_rounds": 0,
                }

        if event_type.startswith("tool") or "tool" in event_type or "mcp" in event_type:
            tool_event_count += 1

        for container in (event, part):
            if not isinstance(container, dict):
                continue
            session_value = (
                container.get("sessionID")
                or container.get("sessionId")
                or container.get("session_id")
            )
            session_obj = container.get("session")
            if not session_value and isinstance(session_obj, dict):
                session_value = (
                    session_obj.get("id")
                    or session_obj.get("sessionID")
                    or session_obj.get("sessionId")
                )
            if isinstance(session_value, str) and session_value.strip():
                opencode_session_id = session_value.strip()
            tool_name = container.get("name") or container.get("tool") or container.get("toolName")
            if isinstance(tool_name, str) and tool_name.strip():
                tool_names.add(tool_name.strip())
            server_name = (
                container.get("server") or container.get("mcpServer") or container.get("serverName")
            )
            if isinstance(server_name, str) and server_name.strip():
                mcp_server_names.add(server_name.strip())

    debug_meta: dict[str, Any] = {}
    if usage:
        debug_meta["usage"] = usage
    if parse_errors:
        debug_meta["opencodeParseErrors"] = parse_errors
    if event_type_counts:
        debug_meta["opencodeEventTypes"] = event_type_counts
    if tool_event_count:
        debug_meta["opencodeToolEvents"] = tool_event_count
    if tool_names:
        debug_meta["opencodeToolNames"] = sorted(tool_names)
    if mcp_server_names:
        debug_meta["opencodeMcpServers"] = sorted(mcp_server_names)
    if opencode_session_id:
        debug_meta["opencodeSessionID"] = opencode_session_id

    content = "\n".join(part for part in text_parts if part.strip()).strip()
    if not content and raw_stdout.strip():
        if parsed_event_count == 0 or parse_errors > 0:
            content = raw_stdout.strip()
            debug_meta["opencodeRawFallback"] = True
        else:
            debug_meta["opencodeNoTextEvent"] = True

    return content, debug_meta


def _invoke_agent_opencode_cli(
    spec: AgentSpec,
    payload_text: str,
    verbose: bool,
    *,
    effective_prompt: str,
    plan_options: dict[str, Any] | None = None,
    session_id: str | None = None,
    continue_session: bool = False,
) -> tuple[str, list[dict[str, str]], Exception | None, dict[str, Any]]:
    """Invoke agent through OpenCode CLI non-interactively (streaming)."""

    del verbose

    options = plan_options or {}
    cli_tools_mode = _normalize_cli_tools_mode(spec, options)
    timeout_s = spec.timeout_ms / 1000
    model = str(spec.model or "").strip()
    cmd_base = [
        *_expand_command(str(options.get("agent_cli_opencode_command", "")).strip(), "opencode"),
        "run",
        "--format",
        "json",
    ]
    if model:
        cmd_base.extend(["--model", model])
    variant = str(getattr(spec, 'variant', '') or '').strip()
    if variant:
        cmd_base.extend(["--variant", variant])
    configured_agent = str(options.get("agent_cli_opencode_agent", "")).strip()
    if configured_agent:
        cmd_base.extend(["--agent", configured_agent])
    extra_args_raw = str(options.get("agent_cli_opencode_args", "")).strip()
    if extra_args_raw:
        cmd_base.extend(shlex.split(extra_args_raw))

    normalized_session_id = str(session_id or "").strip()
    if normalized_session_id:
        cmd_base.extend(["--session", normalized_session_id])
    elif continue_session:
        cmd_base.append("--continue")

    if cli_tools_mode == "native":
        native_args_raw = str(options.get("agent_cli_opencode_native_args", "")).strip()
        if native_args_raw:
            cmd_base.extend(shlex.split(native_args_raw))

    full_prompt = _build_cli_prompt_with_tools_mode(
        effective_prompt,
        payload_text,
        cli_tools_mode,
    )
    cmd = [*cmd_base, full_prompt]
    prompt_stage_dir = _resolve_opencode_prompt_stage_dir(options)

    env = os.environ.copy()
    if spec.endpoint:
        env["OPENCODE_BASE_URL"] = spec.endpoint
    if spec.provider:
        env["OPENCODE_PROVIDER"] = spec.provider

    # Merge workflow @config env so agent subprocesses inherit CANN_HOME, etc.
    wf_env = options.get("_wf_env")
    if isinstance(wf_env, dict):
        env.update(wf_env)

    dispatched_cmd = cmd
    prompt_dispatch = "argv"
    response_text = ""
    debug_meta: dict[str, Any] = {}
    stderr_text = ""
    returncode = -1

    try:
        response_text, debug_meta, returncode, stderr_text = _run_opencode_process(
            cmd,
            timeout_s=timeout_s,
            env=env,
        )
    except FileNotFoundError as exc:
        err = RuntimeError("Agent transport 'opencode-cli' requires 'opencode' executable on PATH.")
        return (
            "",
            [{"role": "user", "content": payload_text}],
            err,
            {
                "transport": "opencode-cli",
                "command": cmd,
                "error": str(exc),
            },
        )
    except OSError as exc:
        if _is_argument_list_too_long(exc):
            try:
                response_text, debug_meta, returncode, stderr_text, dispatched_cmd = (
                    _run_opencode_with_attached_prompt(
                        cmd_base,
                        full_prompt,
                        timeout_s=timeout_s,
                        env=env,
                        prompt_stage_dir=prompt_stage_dir,
                    )
                )
                prompt_dispatch = "attached-file"
            except FileNotFoundError as retry_exc:
                err = RuntimeError(
                    "Agent transport 'opencode-cli' requires 'opencode' executable on PATH."
                )
                return (
                    "",
                    [{"role": "user", "content": payload_text}],
                    err,
                    {
                        "transport": "opencode-cli",
                        "command": cmd_base,
                        "error": str(retry_exc),
                        "promptDispatch": "attached-file",
                    },
                )
            except OSError as retry_exc:
                err = RuntimeError(
                    f"OpenCode CLI invocation failed before launch: {retry_exc}. "
                    "The effective prompt likely exceeded the OS argument limit."
                )
                return (
                    "",
                    [{"role": "user", "content": payload_text}],
                    err,
                    {
                        "transport": "opencode-cli",
                        "command": cmd_base,
                        "error": str(retry_exc),
                        "promptDispatch": "attached-file",
                    },
                )
            except subprocess.TimeoutExpired as retry_exc:
                err = RuntimeError(f"OpenCode CLI timed out after {spec.timeout_ms}ms.")
                return (
                    "",
                    [{"role": "user", "content": payload_text}],
                    err,
                    {
                        "transport": "opencode-cli",
                        "command": cmd_base,
                        "error": str(retry_exc),
                        "promptDispatch": "attached-file",
                    },
                )
        else:
            err = RuntimeError(
                f"OpenCode CLI invocation failed before launch: {exc}. "
                "The effective prompt likely exceeded the OS argument limit."
            )
            return (
                "",
                [{"role": "user", "content": payload_text}],
                err,
                {
                    "transport": "opencode-cli",
                    "command": cmd,
                    "error": str(exc),
                },
            )
    except subprocess.TimeoutExpired as exc:
        err = RuntimeError(f"OpenCode CLI timed out after {spec.timeout_ms}ms.")
        return (
            "",
            [{"role": "user", "content": payload_text}],
            err,
            {
                "transport": "opencode-cli",
                "command": cmd,
                "error": str(exc),
            },
        )

    debug_meta["transport"] = "opencode-cli"
    debug_meta["cliToolsMode"] = cli_tools_mode
    debug_meta["command"] = dispatched_cmd
    debug_meta["exitCode"] = returncode
    debug_meta["promptDispatch"] = prompt_dispatch
    if normalized_session_id:
        debug_meta["opencodeSessionIDRequested"] = normalized_session_id
    elif continue_session:
        debug_meta["opencodeContinueSession"] = True
    if stderr_text:
        debug_meta["stderr"] = stderr_text

    firing_messages = [
        {"role": "user", "content": payload_text},
        {"role": "assistant", "content": response_text},
    ]

    if returncode != 0:
        err_msg = stderr_text.strip() if stderr_text.strip() else f"exit code {returncode}"
        err = RuntimeError(f"OpenCode CLI invocation failed: {err_msg}")
        return response_text, firing_messages, err, debug_meta

    if not response_text.strip():
        err = RuntimeError("OpenCode CLI returned empty response content")
        return response_text, firing_messages, err, debug_meta

    return response_text, firing_messages, None, debug_meta


def _invoke_agent_opencode_acp(
    spec: AgentSpec,
    payload_text: str,
    verbose: bool,
    *,
    effective_prompt: str,
    plan_options: dict[str, Any] | None = None,
    session_id: str | None = None,
    continue_session: bool = False,
    validators: list[Any] | None = None,
    max_validation_attempts: int = 3,
    output_ports: dict[str, Any] | None = None,
    work_dir: str | None = None,
    agent_input_paths: dict[str, str] | None = None,
) -> tuple[str, list[dict[str, str]], Exception | None, dict[str, Any]]:
    """Invoke agent through OpenCode ACP protocol with stuck detection.
    
    Falls back to CLI mode if ACP fails.
    
    If validators are provided, constructs an enhanced prompt that instructs
    the agent to validate and fix its output.
    """
    import asyncio
    import logging
    
    logger = logging.getLogger(__name__)
    
    del verbose
    
    options = plan_options or {}
    cli_tools_mode = _normalize_cli_tools_mode(spec, options)
    # Live event sink for read-only observers (the IDE). Already tagged with the agent
    # instance by the runner; None when no run event stream is active.
    _publish = options.get("_wf_event_publish")
    on_event = _publish if callable(_publish) else None

    # Build the full prompt (with validation instructions if validators provided)
    if validators and output_ports and work_dir:
        from wfpy._agent_validation_runtime import construct_agent_validation_prompt
        
        logger.info(f"Constructing validation prompt with {len(validators)} validators")
        enhanced_prompt = construct_agent_validation_prompt(
            original_prompt=effective_prompt,
            validators=validators,
            max_attempts=max_validation_attempts,
            output_ports=output_ports,
            work_dir=work_dir,
            agent_input_paths=agent_input_paths,
        )
        logger.info(f"Validation prompt constructed: {len(enhanced_prompt)} chars (original: {len(effective_prompt)} chars)")
        has_validation = "Validation Instructions" in enhanced_prompt
        logger.info(f"Validation instructions present: {has_validation}")
    else:
        logger.info(f"No validators provided, using original prompt (validators={validators is not None}, output_ports={output_ports is not None}, work_dir={work_dir is not None})")
        enhanced_prompt = effective_prompt
    
    full_prompt = _build_cli_prompt_with_tools_mode(
        enhanced_prompt,
        payload_text,
        cli_tools_mode,
    )
    
    logger.info(f"Full prompt length: {len(full_prompt)} chars, payload length: {len(payload_text)} chars")
    
    # Log first 1000 chars of the prompt for debugging
    logger.info(f"Full prompt preview (first 1000 chars):\n{full_prompt[:1000]}")
    
    # Get opencode command
    opencode_command = _expand_command(
        str(options.get("agent_cli_opencode_command", "")).strip(),
        "opencode"
    )[0]
    
    # Get working directory
    cwd = options.get("work_dir", ".")
    
    # Build environment variables for ACP subprocess (model configuration)
    acp_env: dict[str, str] = {}
    if spec.model:
        acp_env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"model": spec.model})
        logger.info(f"Setting model via OPENCODE_CONFIG_CONTENT: {spec.model}")
    
    debug_meta: dict[str, Any] = {
        "transport": "opencode-acp",
        "cliToolsMode": cli_tools_mode,
    }
    
    try:
        # Import ACP client
        from wfpy.acp_client import invoke_opencode_acp
        
        logger.info("Attempting to invoke opencode via ACP protocol")

        if on_event is not None:
            on_event({"type": "agent.message.start"})

        # Run ACP invocation
        response = asyncio.run(
            invoke_opencode_acp(
                prompt=full_prompt,
                cwd=cwd,
                stuck_timeout=300,  # 5 minutes
                max_retries=5,
                opencode_command=opencode_command,
                env=acp_env if acp_env else None,
                session_id=session_id if continue_session else None,
                on_event=on_event,
            )
        )

        # Extract response text
        response_text = response.get("text", "")
        if on_event is not None:
            on_event({"type": "agent.message.end", "stopReason": response.get("stop_reason")})
        debug_meta["acpSuccess"] = True
        debug_meta["acpStopReason"] = response.get("stop_reason")
        debug_meta["opencodeSessionID"] = response.get("session_id", "")
        
        logger.info(f"ACP invocation succeeded: {response.get('stop_reason')} (text length: {len(response_text)}, session: {response.get('session_id', 'N/A')})")
        
        firing_messages = [
            {"role": "user", "content": payload_text},
            {"role": "assistant", "content": response_text},
        ]
        
        return response_text, firing_messages, None, debug_meta
        
    except Exception as acp_exc:
        logger.warning(f"ACP invocation failed: {acp_exc}. Falling back to CLI mode.")
        debug_meta["acpError"] = str(acp_exc)
        debug_meta["acpFallbackToCli"] = True
        
        # Fall back to CLI mode
        return _invoke_agent_opencode_cli(
            spec,
            payload_text,
            verbose=False,
            effective_prompt=effective_prompt,
            plan_options=plan_options,
            session_id=session_id,
            continue_session=continue_session,
        )


def _invoke_agent_claude_cli(
    spec: AgentSpec,
    payload_text: str,
    verbose: bool,
    *,
    effective_prompt: str,
    plan_options: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, str]], Exception | None, dict[str, Any]]:
    """Invoke agent through Claude Code CLI non-interactively."""

    del verbose

    options = plan_options or {}
    cli_tools_mode = _normalize_cli_tools_mode(spec, options)
    timeout_s = spec.timeout_ms / 1000
    model = str(spec.model or "").strip()
    cmd = [
        *_expand_command(str(options.get("agent_cli_claude_command", "")).strip(), "claude"),
        "--print",
        "--output-format",
        "text",
    ]
    if model:
        cmd.extend(["--model", model])
    configured_agent = str(options.get("agent_cli_claude_agent", "")).strip()
    if configured_agent:
        cmd.extend(["--agent", configured_agent])
    extra_args_raw = str(options.get("agent_cli_claude_args", "")).strip()
    if extra_args_raw:
        cmd.extend(shlex.split(extra_args_raw))

    if cli_tools_mode == "native":
        native_args_raw = str(options.get("agent_cli_claude_native_args", "")).strip()
        if native_args_raw:
            cmd.extend(shlex.split(native_args_raw))

    full_prompt = _build_cli_prompt_with_tools_mode(
        effective_prompt,
        payload_text,
        cli_tools_mode,
    )
    cmd.append(full_prompt)

    env = os.environ.copy()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except FileNotFoundError as exc:
        err = RuntimeError("Agent transport 'claude-cli' requires 'claude' executable on PATH.")
        return (
            "",
            [{"role": "user", "content": payload_text}],
            err,
            {
                "transport": "claude-cli",
                "command": cmd,
                "error": str(exc),
            },
        )
    except OSError as exc:
        err = RuntimeError(
            f"Claude CLI invocation failed before launch: {exc}. "
            "The effective prompt likely exceeded the OS argument limit."
        )
        return (
            "",
            [{"role": "user", "content": payload_text}],
            err,
            {
                "transport": "claude-cli",
                "command": cmd,
                "error": str(exc),
            },
        )
    except subprocess.TimeoutExpired as exc:
        err = RuntimeError(f"Claude CLI timed out after {spec.timeout_ms}ms.")
        return (
            "",
            [{"role": "user", "content": payload_text}],
            err,
            {
                "transport": "claude-cli",
                "command": cmd,
                "error": str(exc),
            },
        )

    debug_meta: dict[str, Any] = {
        "transport": "claude-cli",
        "cliToolsMode": cli_tools_mode,
        "command": cmd,
        "exitCode": proc.returncode,
    }
    if proc.stderr:
        debug_meta["stderr"] = proc.stderr

    response_text = proc.stdout.strip()
    json_text = _extract_text_from_json_output(proc.stdout)
    if json_text is not None:
        response_text = json_text.strip()
        if response_text != proc.stdout.strip():
            debug_meta["jsonOutputExtracted"] = True

    firing_messages = [
        {"role": "user", "content": payload_text},
        {"role": "assistant", "content": response_text},
    ]

    if proc.returncode != 0:
        err_msg = proc.stderr.strip() if proc.stderr.strip() else f"exit code {proc.returncode}"
        err = RuntimeError(f"Claude CLI invocation failed: {err_msg}")
        return response_text, firing_messages, err, debug_meta

    if not response_text.strip():
        err = RuntimeError("Claude CLI returned empty response content")
        return response_text, firing_messages, err, debug_meta

    return response_text, firing_messages, None, debug_meta


def _invoke_agent_codex_cli(
    spec: AgentSpec,
    payload_text: str,
    verbose: bool,
    *,
    effective_prompt: str,
    plan_options: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, str]], Exception | None, dict[str, Any]]:
    """Invoke agent through Codex CLI (best-effort, non-interactive)."""

    del verbose

    options = plan_options or {}
    cli_tools_mode = _normalize_cli_tools_mode(spec, options)
    timeout_s = spec.timeout_ms / 1000
    cmd = _expand_command(str(options.get("agent_cli_codex_command", "")).strip(), "codex")
    subcommand = str(options.get("agent_cli_codex_subcommand", "")).strip()
    if subcommand:
        cmd.append(subcommand)
    extra_args_raw = str(options.get("agent_cli_codex_args", "")).strip()
    if extra_args_raw:
        cmd.extend(shlex.split(extra_args_raw))

    if cli_tools_mode == "native":
        native_args_raw = str(options.get("agent_cli_codex_native_args", "")).strip()
        if native_args_raw:
            cmd.extend(shlex.split(native_args_raw))

    full_prompt = _build_cli_prompt_with_tools_mode(
        effective_prompt,
        payload_text,
        cli_tools_mode,
    )
    cmd.append(full_prompt)

    env = os.environ.copy()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except FileNotFoundError as exc:
        err = RuntimeError("Agent transport 'codex-cli' requires 'codex' executable on PATH.")
        return (
            "",
            [{"role": "user", "content": payload_text}],
            err,
            {
                "transport": "codex-cli",
                "command": cmd,
                "error": str(exc),
            },
        )
    except OSError as exc:
        err = RuntimeError(
            f"Codex CLI invocation failed before launch: {exc}. "
            "The effective prompt likely exceeded the OS argument limit."
        )
        return (
            "",
            [{"role": "user", "content": payload_text}],
            err,
            {
                "transport": "codex-cli",
                "command": cmd,
                "error": str(exc),
            },
        )
    except subprocess.TimeoutExpired as exc:
        err = RuntimeError(f"Codex CLI timed out after {spec.timeout_ms}ms.")
        return (
            "",
            [{"role": "user", "content": payload_text}],
            err,
            {
                "transport": "codex-cli",
                "command": cmd,
                "error": str(exc),
            },
        )

    debug_meta: dict[str, Any] = {
        "transport": "codex-cli",
        "cliToolsMode": cli_tools_mode,
        "command": cmd,
        "exitCode": proc.returncode,
    }
    if proc.stderr:
        debug_meta["stderr"] = proc.stderr

    response_text = proc.stdout.strip()
    json_text = _extract_text_from_json_output(proc.stdout)
    if json_text is not None:
        response_text = json_text.strip()
        if response_text != proc.stdout.strip():
            debug_meta["jsonOutputExtracted"] = True

    firing_messages = [
        {"role": "user", "content": payload_text},
        {"role": "assistant", "content": response_text},
    ]

    if proc.returncode != 0:
        err_msg = proc.stderr.strip() if proc.stderr.strip() else f"exit code {proc.returncode}"
        err = RuntimeError(f"Codex CLI invocation failed: {err_msg}")
        return response_text, firing_messages, err, debug_meta

    if not response_text.strip():
        err = RuntimeError("Codex CLI returned empty response content")
        return response_text, firing_messages, err, debug_meta

    return response_text, firing_messages, None, debug_meta
