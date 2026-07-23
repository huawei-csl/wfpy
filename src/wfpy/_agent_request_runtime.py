"""Internal helpers for agent provider request building and retry history."""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from wfpy._agent_io_runtime import _build_agent_runtime_instruction, ChatMessage
from wfpy._agent_tools_runtime import (
    LoadedAgentToolRegistry,
    _ask_user_declaration,
    _provider_tool_declarations,
)


def _resolve_provider_model(spec: Any) -> tuple[str, str]:
    """Split ``spec.model`` into ``(provider, model)`` when encoded as ``provider/model``."""

    model = spec.model
    provider = spec.provider
    if "/" in model and provider is None:
        provider, model = model.split("/", 1)
    if provider is None:
        provider = "openai"
    return provider, model


def _is_anthropic_model(model: str) -> bool:
    """Return *True* if *model* refers to an Anthropic Claude model."""

    model_lower = model.lower()
    return "anthropic/" in model_lower or "claude" in model_lower


def _build_agent_request(
    provider: str,
    model: str,
    api_key: str,
    user_prompt: str,
    payload_text: str,
    *,
    prior_messages: list[dict[str, Any]] | None = None,
    enable_tools: bool = False,
    disable_think: bool = False,
    streaming: bool = False,
    endpoint: str = "",
    output_ports: dict[str, Any] | None = None,
    runtime_instruction_extra_rules: list[str] | None = None,
    tool_registry: LoadedAgentToolRegistry | None = None,
    mcp_server_filter: list[str] | None = None,
    include_ask_user: bool = False,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Build the HTTP headers + JSON body for an agent request.

    Returns ``(headers, body)``.
    """

    del endpoint

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        if provider == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    if provider == "openrouter":
        referer = os.environ.get("OPENROUTER_HTTP_REFERER", "")
        if referer:
            headers["HTTP-Referer"] = referer
        title = os.environ.get("OPENROUTER_X_TITLE", "")
        if title:
            headers["X-Title"] = title

    runtime_instruction = _build_agent_runtime_instruction(
        output_ports,
        extra_rules=runtime_instruction_extra_rules,
    )
    if enable_tools:
        tools = _provider_tool_declarations(
            provider, tool_registry, mcp_server_filter, include_ask_user=include_ask_user
        )
    elif include_ask_user:
        # ask_user is decoupled from the tool-auth gate: under deny-all we expose
        # ONLY the ask_user tool, never the python/MCP tools.
        tools = [_ask_user_declaration(provider)]
    else:
        tools = None

    _cache_openrouter = provider == "openrouter" and _is_anthropic_model(model)

    if prior_messages is not None:
        effective_messages = prior_messages
    elif provider == "anthropic":
        effective_messages = [{"role": "user", "content": payload_text}]
    else:
        effective_messages = [
            {"role": "system", "content": user_prompt},
            {"role": "system", "content": runtime_instruction},
            {"role": "user", "content": payload_text},
        ]

    if provider == "anthropic":
        if tools:
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        headers["anthropic-beta"] = "prompt-caching-2024-07-31"
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "system": [
                {"type": "text", "text": user_prompt},
                {
                    "type": "text",
                    "text": runtime_instruction,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            "messages": effective_messages,
            **({"stream": True} if streaming else {}),
            **({"tools": tools} if tools else {}),
        }
    elif provider == "ollama":
        body = {
            "model": model,
            "messages": effective_messages,
            "stream": streaming,
            **({"think": True} if not disable_think else {}),
            **({"tools": tools} if tools else {"format": "json"}),
        }
    else:
        body = {
            "model": model,
            "messages": effective_messages,
            **({"stream": True} if streaming else {}),
            **({"tools": tools} if tools else {}),
        }
        if _cache_openrouter:
            body["cache_control"] = {"type": "ephemeral"}

    return headers, body


def _compact_retry_payload_json(content: str) -> str:
    """Compact large JSON payloads carried into retry history.

    Primarily trims large folder listings in ``resourceInputs`` while keeping
    path/existence metadata intact so repair rounds still retain essential facts.
    """

    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return content
    if not isinstance(payload, dict):
        return content

    resource_inputs = payload.get("resourceInputs")
    if isinstance(resource_inputs, dict):
        for _port, meta in resource_inputs.items():
            if not isinstance(meta, dict):
                continue
            listing = meta.get("listing")
            if isinstance(listing, list) and len(listing) > 20:
                head = listing[:20]
                head.append(f"... (truncated {len(listing) - 20} entries)")
                meta["listing"] = head

    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return content


def _compact_prior_history_for_openrouter_retry(
    prior_history: list[ChatMessage],
    payload_text: str,
    model: str = "",
) -> list[ChatMessage]:
    """Shrink retry history for OpenRouter repair calls.

    For Anthropic-backed models, strip tool_use / tool_result messages that
    can trigger provider-side validation errors.  For other models, keep full
    conversation history but compact large payloads.
    """

    repair_prefix = "Your previous response was invalid and must be repaired."
    if not payload_text.startswith(repair_prefix):
        return prior_history
    if not prior_history:
        return prior_history

    is_anthropic = _is_anthropic_model(model)

    if not is_anthropic:
        compacted: list[ChatMessage] = copy.deepcopy(prior_history)
        for msg in compacted:
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    msg["content"] = _compact_retry_payload_json(content)
        return compacted

    compacted = []
    for msg in copy.deepcopy(prior_history):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "")).strip().lower()

        if role == "tool":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            has_tool_result = any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            )
            if has_tool_result:
                continue

        if role == "assistant":
            if isinstance(content, list):
                text_blocks = [
                    block
                    for block in content
                    if isinstance(block, dict) and block.get("type") != "tool_use"
                ]
                text_only = "\n".join(
                    block.get("text", "")
                    for block in text_blocks
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                )
                if not text_only.strip():
                    continue
                msg["content"] = text_only
            elif isinstance(content, str):
                try:
                    parsed_content = json.loads(content)
                    if isinstance(parsed_content, dict) and "tool_calls" in parsed_content:
                        text = parsed_content.get("content", "")
                        if not text or (isinstance(text, str) and not text.strip()):
                            continue
                        msg["content"] = text if isinstance(text, str) else str(text)
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
            if not str(msg.get("content", "")).strip():
                continue

        if role == "user" and isinstance(msg.get("content"), str):
            msg["content"] = _compact_retry_payload_json(msg["content"])

        compacted.append(msg)

    return compacted if compacted else prior_history[:1]


def _sanitize_prior_history_for_request(
    provider: str,
    prior_history: list[ChatMessage],
) -> list[ChatMessage]:
    """Normalize prior history before sending provider requests.

    OpenRouter (notably with Anthropic-backed models) may reject empty
    assistant/tool messages with HTTP 400 in continuation requests.
    """

    cleaned: list[ChatMessage] = []
    for msg in prior_history:
        if not isinstance(msg, dict):
            continue
        normalized = copy.deepcopy(msg)
        normalized.pop("thinking", None)
        normalized.pop("replyTimeMs", None)
        role = str(normalized.get("role", "")).strip().lower()
        content = normalized.get("content")
        if provider == "openrouter" and role in {"assistant", "tool"}:
            text = content if isinstance(content, str) else ""
            if not text.strip():
                continue
        cleaned.append(normalized)
    return cleaned


def _lookup_openrouter_cost(
    generation_ids: list[str],
    usage_meta: dict[str, Any],
) -> float | None:
    """Best-effort OpenRouter cost lookup via Generation Stats API.

    Mutates ``usage_meta["total_cost_usd"]`` on success.
    Returns the total cost or ``None`` if lookup fails.
    """
    if not generation_ids:
        return None
    try:
        import httpx as _httpx

        from wfpy._agent_tools_runtime import _resolve_api_key

        or_key = _resolve_api_key("openrouter")
        total_cost = 0.0
        for gid in generation_ids:
            resp = _httpx.get(
                f"https://openrouter.ai/api/v1/generation?id={gid}",
                headers={"Authorization": f"Bearer {or_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                cost = data.get("total_cost") or data.get("usage", 0)
                if isinstance(cost, (int, float)) and cost > 0:
                    total_cost += float(cost)
        if total_cost > 0:
            cost_rounded = round(total_cost, 6)
            usage_meta["total_cost_usd"] = cost_rounded
            return cost_rounded
    except Exception:
        pass  # cost lookup is best-effort
    return None
