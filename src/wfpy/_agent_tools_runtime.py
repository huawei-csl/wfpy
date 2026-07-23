"""Internal agent tool, transport, and provider helpers."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


def _parse_retry_after_ms(value: str | None) -> int | None:
    """Parse an HTTP ``Retry-After`` header value to milliseconds."""

    if not value:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    try:
        secs = int(trimmed)
        if secs >= 0:
            return secs * 1000
    except ValueError:
        pass

    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(trimmed)
        delta_ms = int((dt.timestamp() - time.time()) * 1000)
        return max(0, delta_ms)
    except (ValueError, TypeError):
        pass
    return None


AgentToolAuthMode = str  # "deny-all" | "allow-all" | "policy"


@dataclasses.dataclass
class AgentToolCall:
    """A tool call extracted from an LLM response."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclasses.dataclass
class AgentToolResult:
    """Result of executing a tool call."""

    call_id: str
    name: str
    stdout: str
    stderr: str
    exit_code: int
    error: str | None = None


@dataclasses.dataclass
class AgentToolSpec:
    """Specification for one agent tool (builtin or MCP)."""

    kind: str  # "builtin" | "mcp"
    name: str
    description: str = ""
    parameters: dict[str, Any] | None = None
    server: str | None = None
    tool: str | None = None
    timeout_ms: int | None = None


@dataclasses.dataclass
class AgentToolServerSpec:
    """MCP server configuration for bridge/stdio/http transport."""

    transport: str = "bridge"  # "bridge" | "stdio" | "http" | "streamable-http"
    command: str = ""
    args: list[str] = dataclasses.field(default_factory=list)
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    url: str = ""  # required when transport = "http"


@dataclasses.dataclass
class LoadedAgentToolRegistry:
    """Loaded registry of available agent tools."""

    source: str | None = None
    tools: dict[str, AgentToolSpec] = dataclasses.field(default_factory=dict)
    servers: dict[str, AgentToolServerSpec] = dataclasses.field(default_factory=dict)


_BUILTIN_PYTHON_SPEC = AgentToolSpec(
    kind="builtin",
    name="python",
    description="Execute Python code in a constrained runtime and return stdout/stderr.",
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source code to execute."}
        },
        "required": ["code"],
    },
)


ASK_USER_TOOL_NAME = "ask_user"  # literal tool name exposed to the LLM

_BUILTIN_ASK_USER_SPEC = AgentToolSpec(
    kind="builtin",
    name=ASK_USER_TOOL_NAME,
    description=(
        "Pause and ask the human operator a question, then wait for their reply. "
        "Use only when you genuinely need input you cannot obtain otherwise — a "
        "clarification, a decision, or a missing value. Returns the user's answer "
        "as text. In non-interactive runs the answer may be unavailable; if so, "
        "proceed with your best assumption and state it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to put to the user.",
            },
            "context": {
                "type": "string",
                "description": "Optional context explaining why the answer is needed.",
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of allowed answers; the user picks one.",
            },
        },
        "required": ["question"],
    },
)


def _default_agent_tool_registry() -> LoadedAgentToolRegistry:
    """Return a registry containing only the builtin python tool."""

    return LoadedAgentToolRegistry(tools={"python": dataclasses.replace(_BUILTIN_PYTHON_SPEC)})


def _load_agent_tool_registry(path_str: str) -> LoadedAgentToolRegistry:
    """Load an ``agent-tools.json`` file and merge with defaults."""

    raw = json.loads(Path(path_str).read_text())
    if not isinstance(raw, dict):
        raise RuntimeError(f"Agent tool registry '{path_str}' must be a JSON object.")

    registry = _default_agent_tool_registry()
    registry.source = path_str

    for name, spec_raw in raw.get("tools", {}).items():
        if not isinstance(spec_raw, dict):
            continue
        kind = spec_raw.get("kind", "builtin")
        registry.tools[name] = AgentToolSpec(
            kind=kind,
            name=name,
            description=spec_raw.get("description", ""),
            parameters=spec_raw.get("parameters"),
            server=spec_raw.get("server"),
            tool=spec_raw.get("tool"),
            timeout_ms=spec_raw.get("timeoutMs"),
        )

    for name, srv_raw in raw.get("servers", {}).items():
        if not isinstance(srv_raw, dict):
            continue
        registry.servers[name] = AgentToolServerSpec(
            transport=srv_raw.get("transport", "bridge"),
            command=srv_raw.get("command", ""),
            args=srv_raw.get("args", []),
            env=srv_raw.get("env", {}),
            url=srv_raw.get("url", ""),
        )

    return registry


def _run_mcp_bridge(
    registry: LoadedAgentToolRegistry,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str,
    timeout_ms: int = 30_000,
) -> AgentToolResult:
    """Execute an MCP tool call via the bridge subprocess protocol."""

    server_spec = registry.servers.get(server_name)
    if not server_spec:
        return AgentToolResult(
            call_id=call_id,
            name=f"{server_name}.{tool_name}",
            stdout="",
            stderr=f"MCP server '{server_name}' not found in registry.",
            exit_code=1,
            error=f"Unknown server '{server_name}'",
        )
    if not server_spec.command:
        return AgentToolResult(
            call_id=call_id,
            name=f"{server_name}.{tool_name}",
            stdout="",
            stderr=f"MCP server '{server_name}' has no command configured.",
            exit_code=1,
            error=f"No command for server '{server_name}'",
        )

    request_json = json.dumps(
        {
            "toolCallId": call_id,
            "server": server_name,
            "tool": tool_name,
            "arguments": arguments,
            "timeoutMs": timeout_ms,
        }
    )

    try:
        cmd = [server_spec.command, *server_spec.args]
        env = {**os.environ, **server_spec.env} if server_spec.env else None
        result = subprocess.run(
            cmd,
            input=request_json,
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000,
            env=env,
        )
        if result.returncode != 0:
            return AgentToolResult(
                call_id=call_id,
                name=f"{server_name}.{tool_name}",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error=f"Bridge exited with code {result.returncode}",
            )

        try:
            bridge_response = json.loads(result.stdout)
        except json.JSONDecodeError:
            return AgentToolResult(
                call_id=call_id,
                name=f"{server_name}.{tool_name}",
                stdout=result.stdout,
                stderr="Bridge returned non-JSON stdout.",
                exit_code=1,
                error="Invalid bridge JSON response",
            )

        ok = bridge_response.get("ok", False)
        return AgentToolResult(
            call_id=call_id,
            name=f"{server_name}.{tool_name}",
            stdout=str(bridge_response.get("result", "")),
            stderr=str(bridge_response.get("error", "")),
            exit_code=0 if ok else 1,
            error=None if ok else bridge_response.get("error"),
        )
    except subprocess.TimeoutExpired:
        return AgentToolResult(
            call_id=call_id,
            name=f"{server_name}.{tool_name}",
            stdout="",
            stderr=f"MCP bridge timeout after {timeout_ms}ms",
            exit_code=124,
            error=f"Timeout after {timeout_ms}ms",
        )
    except Exception as exc:  # noqa: BLE001
        return AgentToolResult(
            call_id=call_id,
            name=f"{server_name}.{tool_name}",
            stdout="",
            stderr=str(exc),
            exit_code=1,
            error=str(exc),
        )


def _run_mcp_native(
    registry: LoadedAgentToolRegistry,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str,
    timeout_ms: int = 30_000,
) -> AgentToolResult:
    """Execute an MCP tool call via native MCP SDK transport."""

    import asyncio

    server_spec = registry.servers.get(server_name)
    if not server_spec:
        return AgentToolResult(
            call_id=call_id,
            name=f"{server_name}.{tool_name}",
            stdout="",
            stderr=f"MCP server '{server_name}' not found in registry.",
            exit_code=1,
            error=f"Unknown server '{server_name}'",
        )

    try:
        from wfpy._mcp_client import call_mcp_tool
    except ImportError:
        return AgentToolResult(
            call_id=call_id,
            name=f"{server_name}.{tool_name}",
            stdout="",
            stderr=(
                "The 'mcp' package is required for native MCP transport. "
                "Install with: pip install 'wfpy[agent]'"
            ),
            exit_code=1,
            error="mcp package not installed",
        )

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    asyncio.run,
                    call_mcp_tool(
                        server_name=server_name,
                        transport=server_spec.transport,
                        tool_name=tool_name,
                        arguments=arguments,
                        command=server_spec.command,
                        args=server_spec.args,
                        env=server_spec.env or None,
                        url=server_spec.url,
                        timeout_ms=timeout_ms,
                    ),
                ).result(timeout=timeout_ms / 1000)
        else:
            result = loop.run_until_complete(
                call_mcp_tool(
                    server_name=server_name,
                    transport=server_spec.transport,
                    tool_name=tool_name,
                    arguments=arguments,
                    command=server_spec.command,
                    args=server_spec.args,
                    env=server_spec.env or None,
                    url=server_spec.url,
                    timeout_ms=timeout_ms,
                )
            )
    except RuntimeError:
        result = asyncio.run(
            call_mcp_tool(
                server_name=server_name,
                transport=server_spec.transport,
                tool_name=tool_name,
                arguments=arguments,
                command=server_spec.command,
                args=server_spec.args,
                env=server_spec.env or None,
                url=server_spec.url,
                timeout_ms=timeout_ms,
            )
        )

    return AgentToolResult(
        call_id=call_id,
        name=f"{server_name}.{tool_name}",
        stdout=result.result,
        stderr=result.error,
        exit_code=0 if result.ok else 1,
        error=None if result.ok else result.error,
    )


def _dispatch_mcp_tool(
    registry: LoadedAgentToolRegistry,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str,
    timeout_ms: int = 30_000,
) -> AgentToolResult:
    """Route an MCP tool call to the appropriate transport."""

    server_spec = registry.servers.get(server_name)
    if server_spec and server_spec.transport in ("stdio", "http", "streamable-http"):
        return _run_mcp_native(
            registry,
            server_name,
            tool_name,
            arguments,
            call_id,
            timeout_ms,
        )
    return _run_mcp_bridge(
        registry,
        server_name,
        tool_name,
        arguments,
        call_id,
        timeout_ms,
    )


def _normalize_tool_auth_mode(options: dict[str, Any]) -> AgentToolAuthMode:
    """Resolve agent tool authorization mode from options/env."""

    raw = str(
        options.get("agent_tool_auth")
        or os.environ.get("WF_AGENT_TOOL_AUTH", "deny-all")
    ).strip().lower()
    if raw in ("deny-all", "allow-all", "policy"):
        return raw
    raise ValueError(
        f"Invalid agent tool auth mode '{raw}'. Expected: deny-all, allow-all, policy."
    )


def _load_tool_policy(path_str: str) -> dict[str, Any]:
    """Load the JSON agent-tool-policy file."""

    try:
        result: dict[str, Any] = json.loads(Path(path_str).read_text())
        return result
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Cannot load agent tool policy '{path_str}': {exc}") from exc


def _authorize_tool_call(options: dict[str, Any], call: AgentToolCall) -> tuple[bool, str]:
    """Check whether *call* is authorized. Returns ``(allowed, reason)``."""

    # The builtin ``ask_user`` tool is always authorized: asking a human for
    # input is consent-based and runs no code, so it is exempt from the tool
    # auth gate (which exists to guard code execution / MCP side effects).
    if _sanitize_tool_name(call.name) == ASK_USER_TOOL_NAME:
        return True, "ask_user is always allowed (human-in-the-loop)"

    mode = _normalize_tool_auth_mode(options)
    if mode == "deny-all":
        return False, "mode=deny-all"
    if mode == "allow-all":
        return True, "mode=allow-all"

    policy_path = options.get("agent_tool_policy") or os.environ.get("WF_AGENT_TOOL_POLICY")
    if not policy_path:
        return False, "mode=policy but no policy path provided"
    policy = _load_tool_policy(policy_path)
    allowed_names = set(policy.get("allow", []))
    if call.name in allowed_names:
        return True, f"allowed by policy '{policy_path}'"
    return False, f"tool '{call.name}' not listed in policy '{policy_path}'"


def _run_python_tool(
    code: str,
    work_dir: str | None,
    timeout_ms: int = 30_000,
) -> AgentToolResult:
    """Execute Python code in a subprocess and return stdout/stderr."""

    wd = work_dir or tempfile.gettempdir()
    script = Path(wd) / f"_agent_tool_{uuid.uuid4().hex[:8]}.py"
    script.write_text(code)

    candidates = ["python3", "python"]
    last_err: Exception | None = None
    for cmd in candidates:
        try:
            result = subprocess.run(
                [cmd, str(script)],
                cwd=wd,
                capture_output=True,
                text=True,
                timeout=timeout_ms / 1000,
            )
            return AgentToolResult(
                call_id="",
                name="python",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
            )
        except FileNotFoundError:
            last_err = FileNotFoundError(f"{cmd} not found")
            continue
        except subprocess.TimeoutExpired:
            return AgentToolResult(
                call_id="",
                name="python",
                stdout="",
                stderr="",
                exit_code=124,
                error=f"Timeout after {timeout_ms}ms",
            )
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue

    return AgentToolResult(
        call_id="",
        name="python",
        stdout="",
        stderr="",
        exit_code=1,
        error=f"Failed to run Python: {last_err}",
    )


def _sanitize_tool_name(name: str) -> str:
    """Sanitize a tool name for LLM API compatibility."""

    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _provider_tool_declarations(
    provider: str,
    registry: LoadedAgentToolRegistry | None = None,
    mcp_server_filter: list[str] | None = None,
    *,
    include_ask_user: bool = False,
) -> list[dict[str, Any]]:
    """Return provider-formatted ``tools`` declaration array."""

    reg = registry or _default_agent_tool_registry()
    declarations: list[dict[str, Any]] = []

    for name, spec in reg.tools.items():
        if spec.kind == "mcp":
            if mcp_server_filter is None:
                continue
            want_all = "*" in mcp_server_filter
            if not want_all and spec.server and spec.server not in mcp_server_filter:
                continue

        params = spec.parameters or (
            {
                "type": "object",
                "properties": {
                    "input": {"type": "string", "description": "Input data for the MCP tool."}
                },
                "required": ["input"],
            }
            if spec.kind == "mcp"
            else None
        )
        if not params:
            continue

        safe_name = _sanitize_tool_name(name)
        if provider == "anthropic":
            declarations.append(
                {
                    "name": safe_name,
                    "description": spec.description or f"{spec.kind} tool '{name}'",
                    "input_schema": params,
                }
            )
        else:
            declarations.append(
                {
                    "type": "function",
                    "function": {
                        "name": safe_name,
                        "description": spec.description or f"{spec.kind} tool '{name}'",
                        "parameters": params,
                    },
                }
            )

    if include_ask_user and not any(
        _declaration_name(d) == ASK_USER_TOOL_NAME for d in declarations
    ):
        declarations.append(_ask_user_declaration(provider))

    return declarations


def _ask_user_declaration(provider: str) -> dict[str, Any]:
    """Provider-formatted declaration for the builtin ``ask_user`` tool."""

    spec = _BUILTIN_ASK_USER_SPEC
    safe_name = _sanitize_tool_name(spec.name)
    if provider == "anthropic":
        return {
            "name": safe_name,
            "description": spec.description,
            "input_schema": spec.parameters,
        }
    return {
        "type": "function",
        "function": {
            "name": safe_name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def _declaration_name(declaration: dict[str, Any]) -> str:
    """Extract the tool name from a provider declaration (anthropic or openai shape)."""

    if "name" in declaration:
        return str(declaration.get("name", ""))
    fn = declaration.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name", ""))
    return ""


def _extract_tool_calls(provider: str, response_json: dict[str, Any]) -> list[AgentToolCall]:
    """Parse tool calls from an LLM response (non-streaming)."""

    calls: list[AgentToolCall] = []
    if provider == "anthropic":
        for block in response_json.get("content", []):
            if block.get("type") == "tool_use":
                inp = block.get("input", {})
                calls.append(
                    AgentToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=inp if isinstance(inp, dict) else {},
                    )
                )
    else:
        for choice in response_json.get("choices", []):
            msg = choice.get("message", {})
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                args_raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except json.JSONDecodeError:
                    args = {"code": args_raw}
                calls.append(
                    AgentToolCall(
                        id=tc.get("id", ""),
                        name=fn.get("name", ""),
                        arguments=args if isinstance(args, dict) else {},
                    )
                )
    return calls


def _build_tool_result_payload(result: AgentToolResult) -> str:
    """Format tool execution result as a string for the LLM."""

    parts: list[str] = []
    if result.stdout:
        parts.append(f"stdout:\n{result.stdout}")
    if result.stderr:
        parts.append(f"stderr:\n{result.stderr}")
    if result.error:
        parts.append(f"error: {result.error}")
    if result.exit_code != 0:
        parts.append(f"exit_code: {result.exit_code}")
    return "\n".join(parts) or "(no output)"


def _build_tool_result_messages(
    provider: str,
    calls: list[AgentToolCall],
    results: list[AgentToolResult],
) -> list[dict[str, Any]]:
    """Build provider-specific tool-result messages for the next round."""

    if provider == "anthropic":
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": _build_tool_result_payload(result),
            }
            for call, result in zip(calls, results)
        ]
        return [{"role": "user", "content": blocks}]

    if provider == "ollama":
        return [
            {"role": "tool", "content": _build_tool_result_payload(result)} for result in results
        ]

    return [
        {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": _build_tool_result_payload(result),
        }
        for call, result in zip(calls, results)
    ]


def _extract_assistant_message(provider: str, response_json: dict[str, Any]) -> dict[str, Any]:
    """Extract the assistant message for conversation continuation."""

    if provider == "anthropic":
        return {"role": "assistant", "content": response_json.get("content", [])}
    msg = response_json.get("choices", [{}])[0].get("message", {})
    return dict(msg)


def _parse_sse_events(raw: str) -> list[dict[str, Any]]:
    """Parse SSE event stream text into a list of JSON data objects."""

    events: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        line = line.strip()
        if line == "data: [DONE]":
            break
        if not line.startswith("data: "):
            continue
        try:
            events.append(json.loads(line[6:]))
        except json.JSONDecodeError:
            continue
    return events


def _accumulate_stream_response(provider: str, raw_text: str) -> tuple[str, str]:
    """Accumulate a streaming response into ``(content, thinking)`` strings."""

    content = ""
    thinking = ""

    if provider == "ollama":
        for line in raw_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            if chunk.get("done") is True:
                msg = chunk.get("message") or {}
                if isinstance(msg.get("thinking"), str):
                    thinking += msg["thinking"]
                if isinstance(msg.get("content"), str):
                    content += msg["content"]
                break
            msg = chunk.get("message") or {}
            if isinstance(msg.get("thinking"), str):
                thinking += msg["thinking"]
            if isinstance(msg.get("content"), str):
                content += msg["content"]
    elif provider == "anthropic":
        for line in raw_text.split("\n"):
            line = line.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            etype = event.get("type", "")
            if etype == "content_block_delta":
                delta = event.get("delta", {})
                dtype = delta.get("type", "")
                if dtype == "thinking_delta" and isinstance(delta.get("thinking"), str):
                    thinking += delta["thinking"]
                elif dtype == "text_delta" and isinstance(delta.get("text"), str):
                    content += delta["text"]
            elif etype == "message_stop":
                break
    else:
        for line in raw_text.split("\n"):
            line = line.strip()
            if line == "data: [DONE]":
                break
            if not line.startswith("data: "):
                continue
            try:
                chunk = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            if isinstance(delta.get("reasoning_content"), str):
                thinking += delta["reasoning_content"]
            if isinstance(delta.get("content"), str):
                content += delta["content"]

    return content, thinking


_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openai": {
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o",
    },
    "anthropic": {
        "endpoint": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-20250514",
    },
    "ollama": {
        "endpoint": "http://localhost:11434/v1/chat/completions",
        "model": "llama3",
    },
    "github": {
        "endpoint": "https://models.github.ai/inference/chat/completions",
        "model": "openai/gpt-5-mini",
    },
    "openrouter": {
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "model": "openai/gpt-4o-mini",
    },
}

_API_KEY_ENV: dict[str, list[str]] = {
    "openai": ["OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "github": ["GITHUB_MODELS_TOKEN", "GITHUB_TOKEN", "COPILOT_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
}


def _resolve_api_key(provider: str) -> str:
    """Look up the API key for *provider* from environment variables."""

    candidates = _API_KEY_ENV.get(provider, [f"{provider.upper()}_API_KEY"])
    for var in candidates:
        val = os.environ.get(var, "")
        if val:
            return val
    return ""
