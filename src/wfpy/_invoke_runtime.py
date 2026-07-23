"""Internal helpers for agent LLM invocation, retry, and chat history."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import wfpy._agent_cli_runtime as _agent_cli_runtime
from wfpy._agent_cli_runtime import (
    _invoke_agent_claude_cli,
    _invoke_agent_codex_cli,
    _invoke_agent_opencode_acp,
    _invoke_agent_opencode_cli,
    _normalize_agent_transport,
    _normalize_cli_tools_mode,
)
from wfpy._agent_io_runtime import (
    ChatMessage,
    _build_agent_runtime_instruction,
    _read_agent_config,
    summarize_chat_history,
    trim_chat_history,
)
from wfpy._agent_request_runtime import (
    _build_agent_request,
    _compact_prior_history_for_openrouter_retry,
    _is_anthropic_model,
    _resolve_provider_model,
    _sanitize_prior_history_for_request,
)
from wfpy._agent_tools_runtime import (
    ASK_USER_TOOL_NAME,
    AgentToolCall,
    AgentToolResult,
    AgentToolSpec,
    LoadedAgentToolRegistry,
    _PROVIDER_DEFAULTS,
    _accumulate_stream_response,
    _authorize_tool_call,
    _build_tool_result_messages,
    _dispatch_mcp_tool,
    _extract_assistant_message,
    _extract_tool_calls,
    _normalize_tool_auth_mode,
    _parse_retry_after_ms,
    _resolve_api_key,
    _run_python_tool,
    _sanitize_tool_name,
)
from wfpy.core import AgentSpec

if TYPE_CHECKING:  # import cycle: runner imports this module at runtime
    from wfpy.runner import RuntimeActor

logger = logging.getLogger("wfpy")


AGENT_MAX_TOOL_ROUNDS = 6
"""Maximum tool-call continuation rounds per single agent firing."""

AGENT_RETRY_BASE_DELAY_MS = 1200
"""Base delay (ms) for exponential backoff on agent HTTP failures."""

AGENT_MAX_INTERACTIVE_BACKOFF_MS = 15_000
"""Maximum backoff (ms) for any single agent retry wait."""

AGENT_MAX_RETRIES = 4
"""Maximum transient retry attempts per single agent firing."""

AGENT_FAIL_FAST_RETRY_AFTER_MS = 60_000
"""If a Retry-After header exceeds this, fail immediately instead of waiting."""

AGENT_MAX_OUTPUT_REPAIR_ATTEMPTS = 2
"""Maximum re-prompt attempts when agent output cannot be parsed as JSON."""

AGENT_MAX_REPAIR_TOOL_ROUNDS = 2
"""Maximum tool-call rounds allowed during an output-repair re-prompt."""

CLI_AGENT_PROMPT_MAX_CHARS = 120_000
"""Maximum chars for the single prompt argument passed to CLI agents."""


def _agent_retry_backoff_seconds(attempt: int) -> float:
    """Compute exponential backoff delay in seconds for *attempt*."""

    return min(
        AGENT_RETRY_BASE_DELAY_MS * (2.0**attempt) / 1000,
        AGENT_MAX_INTERACTIVE_BACKOFF_MS / 1000,
    )


def _is_transient_agent_error(err: Exception) -> bool:
    """Return ``True`` when *err* should be retried transiently."""

    if not isinstance(err, RuntimeError):
        return False
    message = str(err).strip().lower()
    if not message:
        return False
    return "returned empty response content" in message or "returned empty content" in message


def _run_ask_user_tool(call: AgentToolCall, options: dict[str, Any]) -> AgentToolResult:
    """Dispatch a builtin ``ask_user`` tool call to the elicitation closure.

    Reads the actor-scoped ``_wf_elicit`` closure injected by
    ``runner._step_agent``. A declined/unavailable answer still returns
    ``exit_code=0`` with a graceful instruction so the model can proceed; strict
    mode raises ``ElicitationUnavailableError`` from inside the closure, which
    propagates out of the tool loop and fails the firing.
    """

    ask = options.get("_wf_elicit")
    args = call.arguments if isinstance(call.arguments, dict) else {}
    question = str(args.get("question", "")).strip()
    context = args.get("context")
    choices = args.get("choices")
    if not isinstance(choices, list):
        choices = None

    if not callable(ask):
        return AgentToolResult(
            call_id=call.id,
            name=ASK_USER_TOOL_NAME,
            stdout="No user is available to answer; proceed with your best "
            "assumption and state it.",
            stderr="",
            exit_code=0,
        )

    resp = ask(question, context=context, choices=choices)
    if resp.declined or resp.answer is None:
        reason = resp.reason or "no answer"
        return AgentToolResult(
            call_id=call.id,
            name=ASK_USER_TOOL_NAME,
            stdout=f"[no answer: {reason}] Proceed with your best assumption and state it.",
            stderr="",
            exit_code=0,
        )
    return AgentToolResult(
        call_id=call.id,
        name=ASK_USER_TOOL_NAME,
        stdout=resp.answer,
        stderr="",
        exit_code=0,
    )


# ── Agent chat/config/io helpers (moved to _agent_io_runtime.py) ─────────


# ── Agent output-validator helpers (moved to _agent_validation_runtime.py) ──


# ── Agent tooling/runtime helpers (moved to _agent_tools_runtime.py) ─────


# ── Agent prompt/skill runtime helpers (moved to _agent_prompt_runtime.py) ─


# ── Agent request/retry helpers (moved to _agent_request_runtime.py) ─────


# ── Agent invocation core ────────────────────────────────────────────────


def _invoke_agent(
    spec: AgentSpec,
    payload_text: str,
    verbose: bool,
    *,
    effective_prompt: str,
    max_tool_rounds: int = AGENT_MAX_TOOL_ROUNDS,
    plan_options: dict[str, Any] | None = None,
    prior_history: list[ChatMessage] | None = None,
    cli_session_id: str | None = None,
    cli_continue_session: bool = False,
    work_dir: str | None = None,
    output_ports: dict[str, Any] | None = None,
    runtime_instruction_extra_rules: list[str] | None = None,
    tool_registry: LoadedAgentToolRegistry | None = None,
    mcp_server_filter: list[str] | None = None,
    validators: list[Any] | None = None,
    max_validation_attempts: int = 3,
    agent_input_paths: dict[str, str] | None = None,
) -> tuple[str, list[ChatMessage], Exception | None, dict[str, Any]]:
    """Call an LLM provider with tool-calling, streaming, and retry support.

    Returns ``(response_content, firing_messages, error, debug_meta)`` where
    *firing_messages* are the messages produced during this single firing
    (user + tool rounds + assistant) — used for stateful chat history.
    """
    transport = _normalize_agent_transport(spec)
    if transport in {"opencode-cli", "opencode-acp", "claude-cli", "codex-cli"}:
        cli_tools_mode = _normalize_cli_tools_mode(spec, plan_options)
        active_cli_session_id = str(cli_session_id or "").strip()
        active_cli_continue_session = bool(cli_continue_session and not active_cli_session_id)
        effective_prompt_cli = effective_prompt
        runtime_instruction = _build_agent_runtime_instruction(
            output_ports,
            extra_rules=runtime_instruction_extra_rules,
        )
        if runtime_instruction:
            effective_prompt_cli = f"{effective_prompt}\n\n{runtime_instruction}"
        has_cli_session = transport in {"opencode-cli", "opencode-acp"} and (
            bool(active_cli_session_id) or active_cli_continue_session
        )
        if prior_history and not has_cli_session:
            base_prompt = _agent_cli_runtime._build_cli_prompt_with_tools_mode(
                effective_prompt_cli,
                payload_text,
                cli_tools_mode,
            )
            history_prefix = "\n\nConversation history:\n"
            history_budget = CLI_AGENT_PROMPT_MAX_CHARS - len(base_prompt) - len(history_prefix)
            if history_budget > 0:
                history_lines: list[str] = []
                truncated_history = False
                total_history_chars = 0
                for message in reversed(prior_history):
                    role = str(message.get("role", "unknown"))
                    content = message.get("content", "")
                    if isinstance(content, str):
                        content_text = content
                    else:
                        try:
                            content_text = json.dumps(content, default=str)
                        except Exception:
                            content_text = str(content)
                    line = f"[{role}] {content_text}"
                    additional = len(line) + 1
                    if history_lines and total_history_chars + additional > history_budget:
                        truncated_history = True
                        break
                    if not history_lines and additional > history_budget:
                        truncated_history = True
                        continue
                    history_lines.append(line)
                    total_history_chars += additional
                if history_lines:
                    history_lines.reverse()
                    history_text = "\n".join(history_lines)
                    if truncated_history:
                        history_text = (
                            "[earlier CLI history omitted to fit transport limits]\n" + history_text
                        )
                    effective_prompt_cli = (
                        f"{effective_prompt_cli}\n\nConversation history:\n{history_text}"
                    )
        def _invoke_cli_transport() -> tuple[str, list[ChatMessage], Exception | None, dict[str, Any]]:
            if transport == "opencode-acp":
                return _invoke_agent_opencode_acp(
                    spec,
                    payload_text,
                    verbose,
                    effective_prompt=effective_prompt_cli,
                    plan_options=plan_options,
                    session_id=active_cli_session_id,
                    continue_session=active_cli_continue_session,
                    validators=validators,
                    max_validation_attempts=max_validation_attempts,
                    output_ports=output_ports,
                    work_dir=work_dir,
                    agent_input_paths=agent_input_paths,
                )
            if transport == "opencode-cli":
                return _invoke_agent_opencode_cli(
                    spec,
                    payload_text,
                    verbose,
                    effective_prompt=effective_prompt_cli,
                    plan_options=plan_options,
                    session_id=active_cli_session_id,
                    continue_session=active_cli_continue_session,
                )
            if transport == "claude-cli":
                return _invoke_agent_claude_cli(
                    spec,
                    payload_text,
                    verbose,
                    effective_prompt=effective_prompt_cli,
                    plan_options=plan_options,
                )
            return _invoke_agent_codex_cli(
                spec,
                payload_text,
                verbose,
                effective_prompt=effective_prompt_cli,
                plan_options=plan_options,
            )

        attempt = 0
        response_text = ""
        firing_messages: list[ChatMessage] = [{"role": "user", "content": payload_text}]
        response_debug: dict[str, Any] = {}
        last_error: Exception | None = None

        while attempt < AGENT_MAX_RETRIES:
            response_text, firing_messages, last_error, response_debug = _invoke_cli_transport()
            if transport in {"opencode-cli", "opencode-acp"}:
                returned_session_id = str(response_debug.get("opencodeSessionID") or "").strip()
                if returned_session_id:
                    active_cli_session_id = returned_session_id
                    active_cli_continue_session = False
            if last_error is None:
                if attempt > 0:
                    response_debug["retryAttempts"] = attempt
                return response_text, firing_messages, None, response_debug
            if not _is_transient_agent_error(last_error):
                if attempt > 0:
                    response_debug["retryAttempts"] = attempt
                return response_text, firing_messages, last_error, response_debug

            attempt += 1
            response_debug["retryAttempts"] = attempt
            if attempt >= AGENT_MAX_RETRIES:
                return response_text, firing_messages, last_error, response_debug

            backoff = _agent_retry_backoff_seconds(attempt)
            if verbose:
                logger.warning(
                    "Agent %s transient empty-response error; retrying (%d/%d) after %.1fs",
                    transport,
                    attempt,
                    AGENT_MAX_RETRIES - 1,
                    backoff,
                )
            time.sleep(backoff)

        return response_text, firing_messages, last_error, response_debug

    try:
        import httpx
    except ImportError:
        raise ImportError("Agent support requires httpx. Install with: pip install wfpy[agent]")

    options = plan_options or {}
    provider, model = _resolve_provider_model(spec)

    # Apply config-file cascade
    cfg = _read_agent_config(provider)
    if spec.endpoint is None:
        endpoint = (
            cfg.get("endpoint")
            or _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS["openai"])["endpoint"]
        )
    else:
        endpoint = spec.endpoint
    api_key = cfg.get("token") or _resolve_api_key(provider)
    if cfg.get("model") and spec.model == "openai/gpt-4o":
        # Only use config model if spec has the default
        model = cfg["model"] or model

    _prompt_caching = provider == "anthropic" or (
        provider == "openrouter" and _is_anthropic_model(model)
    )

    if verbose:
        cache_note = " (prompt-caching)" if _prompt_caching else ""
        logger.info(
            "Agent calling %s model=%s endpoint=%s%s", provider, model, endpoint, cache_note
        )

    timeout_s = spec.timeout_ms / 1000

    # Determine tool calling
    auth_mode = _normalize_tool_auth_mode(options)
    enable_tools = auth_mode != "deny-all"
    # The builtin ask_user tool is exposed independently of the tool-auth gate
    # (asking a human runs no code). When on, the tool loop runs even under
    # deny-all, but only the ask_user tool is declared (see _build_agent_request).
    ask_user_enabled = bool(getattr(spec, "ask_user", False))
    process_tools = enable_tools or ask_user_enabled

    # Streaming is only used for the first round (no tools)
    attempt_streaming = options.get("agent_stream", False) and not process_tools

    # Build initial request
    initial_messages: list[dict[str, Any]] | None = None
    if prior_history:
        effective_prior_history = prior_history
        if provider == "openrouter":
            effective_prior_history = _compact_prior_history_for_openrouter_retry(
                prior_history, payload_text, model=model
            )
        effective_prior_history = _sanitize_prior_history_for_request(
            provider, effective_prior_history
        )
        # Prepend system prompts + history + new user message
        runtime_instruction = _build_agent_runtime_instruction(output_ports)
        if provider == "anthropic":
            initial_messages = [
                *effective_prior_history,
                {"role": "user", "content": payload_text},
            ]
        else:
            initial_messages = [
                {"role": "system", "content": effective_prompt},
                {"role": "system", "content": runtime_instruction},
                *effective_prior_history,
                {"role": "user", "content": payload_text},
            ]

    headers, body = _build_agent_request(
        provider,
        model,
        api_key,
        effective_prompt,
        payload_text,
        prior_messages=initial_messages,
        enable_tools=enable_tools,
        include_ask_user=ask_user_enabled,
        streaming=attempt_streaming,
        endpoint=endpoint,
        output_ports=output_ports,
        runtime_instruction_extra_rules=runtime_instruction_extra_rules,
        tool_registry=tool_registry,
        mcp_server_filter=mcp_server_filter,
    )

    think_disabled = False
    tool_round = 0
    firing_messages = [{"role": "user", "content": payload_text}]
    response_debug = {}

    # ── Usage / cost accumulation ────────────────────────────────────
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_tokens = 0
    total_cache_creation_tokens = 0
    total_cache_read_tokens = 0
    total_cost: float | None = None  # OpenRouter-specific
    num_requests = 0

    # ── Retry + tool-calling loop ────────────────────────────────────
    # `attempt` counts transient-error retries (separate from tool rounds).
    # Tool rounds are counted by `tool_round` and do NOT consume retry slots.
    last_error = None
    attempt = 0
    while attempt < AGENT_MAX_RETRIES:
        try:
            if attempt_streaming:
                # Streaming path: read raw text and accumulate
                with httpx.stream(
                    "POST",
                    endpoint,
                    json=body,
                    headers=headers,
                    timeout=timeout_s,
                ) as stream_resp:
                    stream_resp.raise_for_status()
                    raw_text = stream_resp.text
                content, _thinking = _accumulate_stream_response(provider, raw_text)
                if not content or not content.strip():
                    raise RuntimeError("Agent returned empty content")
                if _thinking.strip():
                    response_debug["thinking"] = _thinking
                firing_messages.append({"role": "assistant", "content": content})
                return content, firing_messages, None, response_debug
            else:
                resp = httpx.post(
                    endpoint,
                    json=body,
                    headers=headers,
                    timeout=timeout_s,
                )

                # Think-mode fallback (Ollama)
                if (
                    resp.status_code == 400
                    and "does not support thinking" in resp.text
                    and not think_disabled
                ):
                    think_disabled = True
                    if verbose:
                        logger.warning("Model does not support thinking; retrying without")
                    headers, body = _build_agent_request(
                        provider,
                        model,
                        api_key,
                        effective_prompt,
                        payload_text,
                        prior_messages=initial_messages,
                        enable_tools=enable_tools,
                        include_ask_user=ask_user_enabled,
                        disable_think=True,
                        endpoint=endpoint,
                        output_ports=output_ports,
                        runtime_instruction_extra_rules=runtime_instruction_extra_rules,
                        tool_registry=tool_registry,
                        mcp_server_filter=mcp_server_filter,
                    )
                    continue  # retry same attempt (think-mode fallback, not a retry)

                # Retry-After handling
                if resp.status_code in (429, 503):
                    retry_ms = _parse_retry_after_ms(resp.headers.get("retry-after"))
                    if retry_ms is not None and retry_ms > AGENT_FAIL_FAST_RETRY_AFTER_MS:
                        raise RuntimeError(
                            f"Agent rate-limited; Retry-After {retry_ms // 1000}s "
                            f"exceeds threshold. Retry later or reduce volume."
                        )
                    if retry_ms is not None:
                        backoff = min(retry_ms / 1000, AGENT_MAX_INTERACTIVE_BACKOFF_MS / 1000)
                    else:
                        backoff = min(
                            AGENT_RETRY_BASE_DELAY_MS * (2**attempt) / 1000,
                            AGENT_MAX_INTERACTIVE_BACKOFF_MS / 1000,
                        )
                    attempt += 1
                    if attempt < AGENT_MAX_RETRIES:
                        time.sleep(backoff)
                        continue
                    resp.raise_for_status()

                if resp.status_code >= 400:
                    body_preview = ""
                    try:
                        body_preview = resp.text[:2000]
                    except Exception:
                        body_preview = ""
                    if body_preview:
                        raise RuntimeError(
                            f"Provider HTTP {resp.status_code} error: {body_preview}"
                        )
                resp.raise_for_status()
                data = resp.json()

                # Every successful POST is one billable API request,
                # regardless of whether the response includes usage data.
                num_requests += 1

                # Accumulate usage/cost from this response
                _usage = data.get("usage") or {}
                if _usage:
                    total_prompt_tokens += int(_usage.get("prompt_tokens", 0))
                    total_completion_tokens += int(_usage.get("completion_tokens", 0))
                    total_tokens += int(_usage.get("total_tokens", 0))
                    # Cache metrics — direct Anthropic uses
                    # cache_creation_input_tokens / cache_read_input_tokens;
                    # OpenRouter uses prompt_tokens_details.cache_write_tokens
                    # / cached_tokens.
                    total_cache_creation_tokens += int(_usage.get("cache_creation_input_tokens", 0))
                    total_cache_read_tokens += int(_usage.get("cache_read_input_tokens", 0))
                    _ptd = _usage.get("prompt_tokens_details") or {}
                    if _ptd:
                        total_cache_creation_tokens += int(_ptd.get("cache_write_tokens", 0))
                        total_cache_read_tokens += int(_ptd.get("cached_tokens", 0))
                    # OpenRouter may include cost directly
                    _cost = _usage.get("total_cost") or _usage.get("cost")
                    if _cost is not None:
                        total_cost = (total_cost or 0.0) + float(_cost)
                # Capture OpenRouter generation id for cost lookup
                _gen_id = data.get("id")
                if isinstance(_gen_id, str) and _gen_id.startswith("gen-"):
                    response_debug.setdefault("openrouter_generation_ids", []).append(_gen_id)

                # Check for tool calls
                tool_calls = _extract_tool_calls(provider, data) if process_tools else []

                if tool_calls and tool_round < max_tool_rounds:
                    # Execute tool calls
                    results: list[AgentToolResult] = []
                    tool_registry = options.get("_agent_tool_registry")
                    for call in tool_calls:
                        allowed, reason = _authorize_tool_call(options, call)
                        if not allowed:
                            results.append(
                                AgentToolResult(
                                    call_id=call.id,
                                    name=call.name,
                                    stdout="",
                                    stderr=f"Tool call denied: {reason}",
                                    exit_code=1,
                                    error=f"Denied: {reason}",
                                )
                            )
                            if verbose:
                                logger.info("Tool call '%s' denied: %s", call.name, reason)
                            continue

                        # Route to the appropriate executor
                        tool_timeout = int(options.get("agent_tool_timeout_ms", 30_000))
                        # Resolve tool spec: try direct name first, then reverse-map sanitized name
                        tool_spec = tool_registry.tools.get(call.name) if tool_registry else None
                        if tool_spec is None and tool_registry:
                            for _orig_name, _spec in tool_registry.tools.items():
                                if _sanitize_tool_name(_orig_name) == call.name:
                                    tool_spec = _spec
                                    break
                        if call.name == ASK_USER_TOOL_NAME:
                            result = _run_ask_user_tool(call, options)
                            response_debug.setdefault("elicitations", []).append(
                                {
                                    "question": call.arguments.get("question", ""),
                                    "answer": result.stdout,
                                    "choices": call.arguments.get("choices"),
                                }
                            )
                        elif (
                            tool_spec
                            and tool_spec.kind == "mcp"
                            and tool_spec.server
                            and tool_spec.tool
                        ):
                            result = _dispatch_mcp_tool(
                                tool_registry,  # type: ignore[arg-type]
                                tool_spec.server,
                                tool_spec.tool,
                                call.arguments,
                                call.id,
                                tool_spec.timeout_ms or tool_timeout,
                            )
                        else:
                            code = call.arguments.get("code", "")
                            result = _run_python_tool(code, work_dir, tool_timeout)
                        result.call_id = call.id
                        results.append(result)
                        if verbose:
                            logger.info(
                                "Tool '%s' exit=%d stdout=%d bytes",
                                call.name,
                                result.exit_code,
                                len(result.stdout),
                            )

                    # Build continuation messages
                    assistant_msg = _extract_assistant_message(provider, data)
                    tool_result_msgs = _build_tool_result_messages(
                        provider,
                        tool_calls,
                        results,
                    )
                    # Record for chat history
                    firing_messages.append(
                        {"role": "assistant", "content": json.dumps(assistant_msg)}
                    )
                    for trm in tool_result_msgs:
                        firing_messages.append(trm)

                    # Build next request preserving conversation
                    current_messages = body.get("messages", [])
                    next_messages = [*current_messages, assistant_msg, *tool_result_msgs]
                    headers, body = _build_agent_request(
                        provider,
                        model,
                        api_key,
                        effective_prompt,
                        payload_text,
                        prior_messages=next_messages,
                        enable_tools=enable_tools,
                        include_ask_user=ask_user_enabled,
                        disable_think=think_disabled,
                        endpoint=endpoint,
                        output_ports=output_ports,
                        runtime_instruction_extra_rules=runtime_instruction_extra_rules,
                        tool_registry=tool_registry,
                        mcp_server_filter=mcp_server_filter,
                    )
                    tool_round += 1
                    attempt = 0  # reset retry counter after successful tool round
                    continue  # does NOT consume an attempt slot

                # Tool-round cap reached: model still wants tools but we
                # exhausted max_tool_rounds.  Add synthetic tool_result
                # blocks (required by Anthropic) and send one final request
                # WITHOUT tools to force a text answer.
                if tool_calls and tool_round >= max_tool_rounds:
                    if verbose:
                        logger.info(
                            "Tool-round cap (%d) reached; requesting final answer without tools",
                            max_tool_rounds,
                        )
                    # Build synthetic "budget exhausted" tool results
                    cap_results: list[AgentToolResult] = [
                        AgentToolResult(
                            call_id=call.id,
                            name=call.name,
                            stdout="",
                            stderr="Tool budget exhausted — produce your final answer now.",
                            exit_code=1,
                            error="Tool round limit reached",
                        )
                        for call in tool_calls
                    ]
                    assistant_msg = _extract_assistant_message(provider, data)
                    cap_tool_result_msgs = _build_tool_result_messages(
                        provider,
                        tool_calls,
                        cap_results,
                    )
                    current_messages = body.get("messages", [])
                    next_messages = [*current_messages, assistant_msg, *cap_tool_result_msgs]
                    headers, body = _build_agent_request(
                        provider,
                        model,
                        api_key,
                        effective_prompt,
                        payload_text,
                        prior_messages=next_messages,
                        enable_tools=False,
                        disable_think=think_disabled,
                        endpoint=endpoint,
                        output_ports=output_ports,
                        runtime_instruction_extra_rules=runtime_instruction_extra_rules,
                        tool_registry=tool_registry,
                        mcp_server_filter=mcp_server_filter,
                    )
                    attempt = 0
                    enable_tools = False  # no more tool extraction after cap
                    continue

                # Extract final response text
                if provider == "anthropic":
                    content_blocks = data.get("content", [])
                    text_parts = [
                        b["text"] for b in content_blocks if b.get("type") == "text" and "text" in b
                    ]
                    content = "\n".join(text_parts) if text_parts else ""
                    thinking_parts = [
                        b.get("thinking", "")
                        for b in content_blocks
                        if b.get("type") == "thinking" and isinstance(b.get("thinking"), str)
                    ]
                    thinking_text = "\n".join([p for p in thinking_parts if p])
                else:
                    message = data.get("choices", [{}])[0].get("message", {})
                    content = message.get("content", "")
                    if content is None:
                        content = ""
                    elif isinstance(content, list):
                        parts: list[str] = []
                        for block in content:
                            if isinstance(block, str):
                                parts.append(block)
                            elif isinstance(block, dict):
                                text_block = block.get("text")
                                if isinstance(text_block, str):
                                    parts.append(text_block)
                        content = "\n".join(parts)
                    elif not isinstance(content, str):
                        content = str(content)
                    thinking_text = ""
                    reasoning_obj = message.get("reasoning")
                    if isinstance(reasoning_obj, str):
                        thinking_text = reasoning_obj
                    elif isinstance(reasoning_obj, dict):
                        thinking_text = str(reasoning_obj.get("content", "") or "")
                    if not thinking_text and isinstance(message.get("reasoning_content"), str):
                        thinking_text = message.get("reasoning_content", "")

                if isinstance(thinking_text, str) and thinking_text.strip():
                    response_debug["thinking"] = thinking_text

                if not isinstance(content, str) or not content.strip():
                    try:
                        response_debug["emptyResponsePayloadPreview"] = json.dumps(
                            data, ensure_ascii=False
                        )[:2000]
                    except Exception:
                        response_debug["emptyResponsePayloadPreview"] = str(data)[:2000]
                    content = ""

                # Store accumulated usage/cost in debug metadata
                if num_requests > 0:
                    usage_summary: dict[str, Any] = {
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                        "total_tokens": total_tokens,
                        "num_requests": num_requests,
                        "tool_rounds": tool_round,
                    }
                    if total_cache_creation_tokens or total_cache_read_tokens:
                        usage_summary["cache_creation_input_tokens"] = total_cache_creation_tokens
                        usage_summary["cache_read_input_tokens"] = total_cache_read_tokens
                    if total_cost is not None:
                        usage_summary["total_cost_usd"] = round(total_cost, 6)
                    response_debug["usage"] = usage_summary

                firing_messages.append({"role": "assistant", "content": content})
                return content, firing_messages, None, response_debug

        except httpx.HTTPStatusError as err:
            return "", firing_messages, err, response_debug
        except RuntimeError as err:
            if _is_transient_agent_error(err):
                last_error = err
                attempt += 1
                if attempt < AGENT_MAX_RETRIES:
                    backoff = _agent_retry_backoff_seconds(attempt)
                    if verbose:
                        logger.warning(
                            "Agent %s transient empty-response error; retrying (%d/%d) after %.1fs",
                            provider,
                            attempt,
                            AGENT_MAX_RETRIES - 1,
                            backoff,
                        )
                    time.sleep(backoff)
                    continue
            return "", firing_messages, err, response_debug
        except Exception as e:
            last_error = e
            attempt += 1
            if attempt < AGENT_MAX_RETRIES:
                backoff = _agent_retry_backoff_seconds(attempt)
                time.sleep(backoff)

    return "", firing_messages, last_error, response_debug


def _record_agent_chat_history(
    actor: RuntimeActor,
    agent_spec: AgentSpec,
    firing_messages: list[ChatMessage],
    *,
    verbose: bool,
) -> None:
    """Persist visible agent transcript history without changing stateless execution."""

    actor.chat_history.extend(firing_messages)
    if not agent_spec.stateful:
        return

    transport_mode = _normalize_agent_transport(agent_spec)
    if agent_spec.truncation_strategy == "summarize" and transport_mode == "http":
        provider, model = _resolve_provider_model(agent_spec)
        cfg = _read_agent_config(provider)
        endpoint = (
            agent_spec.endpoint
            or cfg.get("endpoint")
            or _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS["openai"])["endpoint"]
        )
        api_key = cfg.get("token") or _resolve_api_key(provider)
        summarize_chat_history(
            actor.chat_history,
            agent_spec.context_budget,
            provider,
            model,
            endpoint,
            api_key,
            timeout_ms=30_000,
            verbose=verbose,
        )
        return

    if agent_spec.truncation_strategy == "summarize" and transport_mode != "http" and verbose:
        logger.info(
            "Agent %s uses transport=%s; falling back to sliding chat truncation",
            actor.name,
            transport_mode,
        )
    trim_chat_history(actor.chat_history, agent_spec.context_budget)


# ── Agent step (fire one agent actor) ────────────────────────────────────


