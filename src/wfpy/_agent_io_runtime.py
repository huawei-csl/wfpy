"""Internal helpers for agent chat history, config, and IO parsing."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from wfpy._agent_tools_runtime import _resolve_api_key
from wfpy._agent_validation_runtime import _is_resource_type

logger = logging.getLogger("wfpy")

_RE_TRAILING_BACKTICKS = re.compile(r"\n?```\s*$")

SUMMARIZATION_PROMPT = (
    "You are a conversation summarizer. Condense the following conversation "
    "history into a concise summary that preserves all important facts, "
    "decisions, and context. Output ONLY the summary text — no preamble, "
    "no markdown fences, no JSON wrapping."
)

ChatMessage = dict[str, Any]
"""Type alias for persisted chat messages and provider continuation payloads."""


def trim_chat_history(history: list[ChatMessage], budget: int) -> list[ChatMessage]:
    """Keep at most *budget* messages, dropping oldest first.

    After trimming, drops leading non-user messages to avoid orphaned
    assistant/tool messages at the start.  Mutates *history* in-place.
    """

    if len(history) > budget:
        del history[: len(history) - budget]
    while history and history[0].get("role") != "user":
        history.pop(0)
    return history


def _build_summarization_payload(messages: list[ChatMessage]) -> str:
    """Render messages into a text block suitable for summarization."""

    parts: list[str] = []
    for message in messages:
        role = message.get("role", "unknown")
        content = message.get("content", "")
        parts.append(f"[{role}]: {content}")
    return "\n".join(parts)


def summarize_chat_history(
    history: list[ChatMessage],
    budget: int,
    provider: str,
    model: str,
    endpoint: str,
    api_key: str,
    timeout_ms: int = 30_000,
    verbose: bool = False,
) -> list[ChatMessage]:
    """Summarize older messages, keeping *budget* - 1 recent ones.

    Makes a real HTTP request to the configured model. Falls back to
    ``trim_chat_history`` on failure.  Mutates *history* in-place.
    """

    if len(history) <= budget:
        return history

    try:
        import httpx
    except ImportError:
        return trim_chat_history(history, budget)

    recent_count = max(budget - 1, 1)
    split_index = len(history) - recent_count
    old_messages = history[:split_index]
    recent_messages = history[split_index:]

    payload_text = _build_summarization_payload(old_messages)

    try:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            if provider == "anthropic":
                headers["x-api-key"] = api_key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {api_key}"

        if provider == "anthropic":
            body: dict[str, Any] = {
                "model": model,
                "max_tokens": 1024,
                "system": SUMMARIZATION_PROMPT,
                "messages": [{"role": "user", "content": payload_text}],
            }
        else:
            body = {
                "model": model,
                "max_tokens": 1024,
                "messages": [
                    {"role": "system", "content": SUMMARIZATION_PROMPT},
                    {"role": "user", "content": payload_text},
                ],
            }

        resp = httpx.post(
            endpoint,
            json=body,
            headers=headers,
            timeout=timeout_ms / 1000,
        )
        resp.raise_for_status()
        data = resp.json()
        if provider == "anthropic":
            summary_text = data["content"][0]["text"]
        else:
            summary_text = data["choices"][0]["message"]["content"]

        history.clear()
        history.append(
            {
                "role": "user",
                "content": f"[Summary of prior conversation]\n{summary_text}",
            }
        )
        history.extend(recent_messages)
        if verbose:
            logger.info(
                "Chat history summarized (%d old -> 1 summary + %d recent)",
                len(old_messages),
                len(recent_messages),
            )
    except Exception as exc:  # noqa: BLE001
        if verbose:
            logger.warning(
                "Summarization failed (%s), falling back to sliding trim",
                exc,
            )
        trim_chat_history(history, budget)

    return history


def restore_chat_histories(
    plan: Any,
    prior_record: dict[str, Any],
) -> int:
    """Restore stateful agent chat histories from a prior run record.

    Returns the number of actors whose history was restored.
    """

    history_by_name: dict[str, list[ChatMessage]] = {}
    cli_sessions_by_name: dict[str, dict[str, str]] = {}
    for entry in prior_record.get("actors", []):
        instance_name = str(entry.get("instanceName", "")).strip()
        if not instance_name:
            continue
        chat_history = entry.get("chatHistory")
        if isinstance(chat_history, list) and len(chat_history) > 0:
            history_by_name[instance_name] = chat_history
        session_map = entry.get("agentCliSessionIds")
        if isinstance(session_map, dict):
            normalized_sessions: dict[str, str] = {}
            for key, value in session_map.items():
                key_text = str(key).strip()
                if not key_text:
                    continue
                value_text = str(value).strip() if isinstance(value, str) else ""
                if value_text:
                    normalized_sessions[key_text] = value_text
            if normalized_sessions:
                cli_sessions_by_name[instance_name] = normalized_sessions

    def _session_id_from_history(messages: list[ChatMessage]) -> str | None:
        for message in reversed(messages):
            role = str(message.get("role", "")).strip().lower()
            if role != "assistant":
                continue
            session_id = message.get("opencodeSessionID")
            if isinstance(session_id, str) and session_id.strip():
                return session_id.strip()
        return None

    restored = 0
    for actor in plan.actors:
        if actor.kind == "agent":
            meta = actor.meta
            if meta.agent_spec and meta.agent_spec.stateful:
                prior = history_by_name.get(actor.name)
                if prior:
                    actor.chat_history.extend(prior)
                    trim_chat_history(
                        actor.chat_history,
                        meta.agent_spec.context_budget,
                    )
                    restored += 1
                prior_sessions = cli_sessions_by_name.get(actor.name)
                if isinstance(prior_sessions, dict) and prior_sessions:
                    actor.agent_cli_session_ids.update(prior_sessions)
                elif not actor.agent_cli_session_ids:
                    inferred_opencode = _session_id_from_history(actor.chat_history)
                    if inferred_opencode:
                        actor.agent_cli_session_ids["opencode"] = inferred_opencode
        elif actor.kind == "workflow" and actor.sub_plan:
            restored += restore_chat_histories(actor.sub_plan, prior_record)

    return restored


def _read_agent_config(preferred_provider: str | None = None) -> dict[str, str | None]:
    """Read ``~/.config/wf-lang/config.json`` and merge with env vars.

    Returns ``{"provider", "token", "endpoint", "model"}`` — any may be None.
    Cascade (highest priority first): annotation → env → config → defaults.
    """

    file_cfg: dict[str, str] = {}
    provider_cfgs: dict[str, dict[str, str]] = {}

    cfg_path = Path.home() / ".config" / "wf-lang" / "config.json"
    try:
        raw = cfg_path.read_text()
        parsed = json.loads(raw)
        agent_block = parsed.get("agent") or {}
        for key in ("provider", "token", "endpoint", "model"):
            if key in agent_block:
                file_cfg[key] = str(agent_block[key])
        for name, block in (parsed.get("agents") or {}).items():
            provider_cfgs[name] = {
                key: str(value)
                for key, value in block.items()
                if key in ("provider", "token", "endpoint", "model")
            }
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    env_provider = os.environ.get("WF_AGENT_PROVIDER")
    provider = preferred_provider or env_provider or file_cfg.get("provider") or "openai"

    provider_cfg = provider_cfgs.get(provider, {})

    token = (
        os.environ.get("WF_AGENT_TOKEN")
        or os.environ.get("AGENT_API_KEY")
        or _resolve_api_key(provider)
        or provider_cfg.get("token")
        or file_cfg.get("token")
    )

    endpoint = (
        os.environ.get("WF_AGENT_ENDPOINT")
        or provider_cfg.get("endpoint")
        or file_cfg.get("endpoint")
    )

    model = os.environ.get("WF_AGENT_MODEL") or provider_cfg.get("model") or file_cfg.get("model")

    return {
        "provider": provider,
        "token": token or None,
        "endpoint": endpoint or None,
        "model": model or None,
    }


def _build_agent_runtime_instruction(
    output_ports: dict[str, Any] | None = None,
    extra_rules: list[str] | None = None,
) -> str:
    """Build the system prompt that instructs the LLM on output format."""

    schema_hint = ""
    if output_ports:
        port_descs = []
        for port_name, desc in output_ports.items():
            ext = desc.ext if desc and desc.ext else ""
            if ext:
                port_descs.append(f"{port_name} (format: {ext.lstrip('.')})")
            else:
                port_descs.append(port_name)
        names = ", ".join(port_descs)
        schema_hint = (
            f" Declared output ports are: {names}. "
            "Respect the declared format for each port — e.g. a .json port must contain valid JSON content, a .md port must contain Markdown. "
            "Return exactly one JSON object with top-level key 'outputs' and one value for each declared output port."
        )
    extra = ""
    if extra_rules:
        extra = " " + " ".join(str(rule).strip() for rule in extra_rules if str(rule).strip())
    return (
        "You are executing a WorkflowLang @agent task. "
        "Use only the JSON payload provided by the user message as task input context. "
        "Do not use @tool placeholders like {in.port} or {param.name}; those are not expanded for @agent tasks. "
        "Read input tokens from inputs.<port> and task parameters from parameters.<name>. "
        "For Resource/File ports, use resourceInputs.<port> for path/kind/existence metadata when present. "
        "When resourceInputs.<port>.originalPath or fileInputs.<port>.originalPath is present, treat path as the staged working copy and originalPath as read-only provenance metadata. "
        "For folder inputs, resourceInputs.<port>.listing contains the directory tree — use it to understand available files without needing a tool call. "
        "If the task skill references scripts, those paths are pre-resolved to absolute paths in the skill prompt. "
        "For File inputs, prefer fileInputs.<port>.content as text content "
        "and fileInputs.<port>.path as source metadata when present. "
        'Return only strict JSON in the shape {"outputs": {"<port>": <value>}}. '
        "Do not include markdown fences, explanations, commentary, or any text before or after the JSON. "
        "Do not return an empty response. "
        "Use exactly one value for each declared output port and no extra keys or prose. "
        f"{schema_hint}"
        "For File/Resource output ports produce the actual text content inline as the value - "
        "do NOT return file paths pointing to pre-existing files in the input directory. "
        "If a multi-output task intentionally does not emit a File/Resource artifact for one port on this turn, still include that port and set its value to an empty string. "
        "Run extraction scripts or tools when needed and include the resulting content directly in your JSON output. "
        "If the task has exactly one File output, plain text is accepted as a fallback output value."
        f"{extra}"
    )


def _parse_json_from_text(text: str | None, ctx: str) -> dict[str, Any]:
    """Extract a JSON object from *text*, trying full parse then first/last braces."""

    if text is None:
        raise ValueError(f"Could not parse {ctx} as JSON object (empty response).")
    if not isinstance(text, str):
        text = str(text)

    def _try(s: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    def _close_truncated_json_object(s: str) -> dict[str, Any] | None:
        candidate = s.strip()
        if not candidate.startswith("{"):
            return None
        stack: list[str] = []
        in_string = False
        escaped = False
        for ch in candidate:
            if in_string:
                if escaped:
                    escaped = False
                    continue
                if ch == "\\":
                    escaped = True
                    continue
                if ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                stack.append("}")
                continue
            if ch == "[":
                stack.append("]")
                continue
            if ch in "}]":
                if not stack or stack[-1] != ch:
                    return None
                stack.pop()
        if in_string:
            return None
        if not stack:
            return None
        repaired = candidate + "".join(reversed(stack))
        return _try(repaired)

    def _try_raw_dict_with_closer_trailing(s: str) -> dict[str, Any] | None:
        candidate = s.strip()
        if not candidate.startswith("{"):
            return None
        decoder = json.JSONDecoder()
        try:
            parsed, end = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        trailing = candidate[end:].strip()
        if not trailing or set(trailing) == {"}"}:
            return parsed
        return None

    result = _try(text.strip())
    if result is not None:
        return result
    result = _close_truncated_json_object(text)
    if result is not None:
        return result
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0:
        result = _try_raw_dict_with_closer_trailing(text[start:])
        if result is not None:
            return result
    if start >= 0 and end > start:
        result = _try(text[start : end + 1])
        if result is not None:
            return result
    raise ValueError(f"Could not parse {ctx} as JSON object.")


def _normalize_agent_response_text(
    content: str | None,
    output_ports: dict[str, Any],
) -> str | None:
    """Repair one narrow malformed multi-output JSON shape.

    Some CLI agent turns occasionally emit a valid ``outputs`` object but place
    one or more declared output ports at the top level, sometimes followed only
    by stray closing braces. When the top-level extras are all declared outputs,
    normalize back to the canonical ``{"outputs": {...}}`` shape.
    """

    if content is None:
        return None

    content_text = str(content)
    stripped = content_text.strip()

    # Strip trailing markdown backtick fences leaked from agent output.
    stripped = _RE_TRAILING_BACKTICKS.sub("", stripped).rstrip()
    if stripped != content_text.strip():
        content_text = stripped
    if not stripped:
        return content_text

    if len(output_ports) == 1:
        single_port_name, single_desc = next(iter(output_ports.items()))
        ext = str(getattr(single_desc, "ext", "") or "").strip().lower()
        if ext in {".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".cu"}:
            candidate_text = stripped
            if '"outputs"' in content_text and f'"{single_port_name}"' in content_text:
                extracted = _extract_wrapped_single_output_text(content_text, single_port_name)
                if isinstance(extracted, str) and extracted.strip():
                    candidate_text = extracted.strip()
            escaped_line_markers = [
                r"\n#include",
                r"\nextern",
                r"\nAICORE ",
                r"\ntemplate <",
                r"\nusing namespace ",
                r"\nnamespace ",
                r"\n#if",
                r"\n#ifdef",
            ]
            looks_like_json_wrapper = candidate_text.startswith("{") or candidate_text.startswith("[")
            if (
                not looks_like_json_wrapper
                and "\n" not in candidate_text
                and "\\n" in candidate_text
                and any(marker in candidate_text for marker in escaped_line_markers)
            ):
                try:
                    decoded_candidate = json.loads(f'"{candidate_text}"')
                except (json.JSONDecodeError, ValueError):
                    decoded_candidate = (
                        candidate_text.replace("\\n", "\n")
                        .replace("\\t", "\t")
                        .replace('\\"', '"')
                        .replace("\\\\", "\\")
                    )
                if isinstance(decoded_candidate, str) and "\n" in decoded_candidate:
                    candidate_text = decoded_candidate.strip()

            first_code_pos = _find_first_cpp_code_line_start(candidate_text)
            if first_code_pos is not None:
                if first_code_pos > 0:
                    prefix = candidate_text[:first_code_pos]
                    if "\n" in prefix and not _has_only_comments_before_code(prefix):
                        comment_preamble = _extract_trailing_comment_preamble(prefix)
                        if comment_preamble:
                            return comment_preamble.lstrip() + candidate_text[first_code_pos:]
                        return candidate_text[first_code_pos:].lstrip()
                if candidate_text != stripped:
                    return candidate_text
        return content_text

    if len(output_ports) < 2:
        return content_text

    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        decoder = json.JSONDecoder()
        try:
            parsed, end = decoder.raw_decode(stripped)
        except json.JSONDecodeError:
            return content_text
        trailing = stripped[end:].strip()
        if trailing and set(trailing) != {"}"}:
            return content_text
    else:
        trailing = ""

    if not isinstance(parsed, dict):
        return content_text

    outputs_val = parsed.get("outputs")
    if not isinstance(outputs_val, dict):
        return content_text

    declared_set = set(output_ports.keys())
    misplaced_keys = [key for key in parsed if key in declared_set]
    if not misplaced_keys:
        return content_text

    allowed_top_level = {"outputs", "contextPatch", *declared_set}
    if any(key not in allowed_top_level for key in parsed):
        return content_text

    merged_outputs = dict(outputs_val)
    for key in misplaced_keys:
        value = parsed[key]
        if key in merged_outputs and merged_outputs[key] != value:
            return content_text
        merged_outputs[key] = value

    normalized: dict[str, Any] = {"outputs": merged_outputs}
    if "contextPatch" in parsed:
        normalized["contextPatch"] = parsed["contextPatch"]
    return json.dumps(normalized, separators=(",", ":"), default=str)


def _extract_wrapped_single_output_text(content_text: str, port_name: str) -> str | None:
    """Extract single-output text from malformed wrapped JSON-like payload.

    Handles common broken shapes such as:
    - {"outputs": {"Port": "..."}} with truncation outside string payload
    - nested escaped JSON text that still contains "outputs" / port marker
    """

    match = re.search(rf'"{re.escape(port_name)}"\s*:\s*"', content_text)
    if match is None:
        return None

    payload = content_text[match.end() :]
    chars: list[str] = []
    escaped = False
    for ch in payload:
        if escaped:
            chars.append(ch)
            escaped = False
            continue
        if ch == "\\":
            chars.append(ch)
            escaped = True
            continue
        if ch == '"':
            break
        chars.append(ch)

    raw = "".join(chars).strip()
    if not raw:
        return None

    try:
        decoded = json.loads(f'"{raw}"')
    except (json.JSONDecodeError, ValueError):
        decoded = (
            raw.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")
        )

    if not isinstance(decoded, str):
        return None
    return decoded if decoded.strip() else None


def _has_only_comments_before_code(prefix: str) -> bool:
    for line in prefix.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("//", "/*", "*", "*/")):
            continue
        return False
    return True


def _extract_trailing_comment_preamble(prefix: str) -> str:
    """Preserve a trailing comment banner when stripping leading prose."""

    lines = prefix.splitlines(keepends=True)
    start = len(lines)
    saw_comment = False

    for idx in range(len(lines) - 1, -1, -1):
        stripped = lines[idx].strip()
        if not stripped:
            if saw_comment:
                start = idx
            continue
        if stripped.startswith(("//", "/*", "*", "*/")):
            start = idx
            saw_comment = True
            continue
        break

    if start >= len(lines):
        return ""
    suffix = "".join(lines[start:])
    return suffix if _has_only_comments_before_code(suffix) else ""


def _find_first_cpp_code_line_start(text: str) -> int | None:
    """Return the first line-start that looks like actual C/C++ source."""

    code_line_patterns = (
        re.compile(r"^\s*#include\b"),
        re.compile(r'^\s*extern\s+"C"\b'),
        re.compile(r"^\s*AICORE\b"),
        re.compile(r"^\s*template\b"),
        re.compile(r"^\s*using\s+namespace\b"),
        re.compile(r"^\s*namespace\b"),
        re.compile(r"^\s*#if\b"),
        re.compile(r"^\s*#ifdef\b"),
        re.compile(r"^\s*#pragma\b"),
    )

    offset = 0
    for line in text.splitlines(keepends=True):
        if any(pattern.search(line) for pattern in code_line_patterns):
            return offset
        offset += len(line)

    if text and any(pattern.search(text) for pattern in code_line_patterns):
        return 0
    return None


def _single_output_plain_text_fallback_allowed(output_ports: dict[str, Any]) -> bool:
    """Return whether a single File output may fall back to raw text.

    Single-output non-file values and text artifacts such as ``.cpp`` and
    ``.md`` still accept raw text when the agent skips the wrapper. Structured
    ``.json`` file outputs do not, because falling back to raw text bypasses
    parse-repair and writes malformed JSON directly to disk.
    """

    if len(output_ports) != 1:
        return False
    port_name, desc = next(iter(output_ports.items()))
    del port_name
    if not _is_resource_type(desc.port_type):
        return True
    ext = str(getattr(desc, "ext", "") or "").strip().lower()
    return ext != ".json"


def _sample_agent_output_value(desc: Any) -> Any:
    """Return an explicit placeholder value for repair prompt examples."""

    ext = str(getattr(desc, "ext", "") or "").strip().lower()
    if ext == ".json":
        return {"replace_with_real_output": True}
    if ext == ".md":
        return "# Replace with real content\n"
    if ext in {".cpp", ".cc", ".cxx", ".c", ".h", ".hpp"}:
        return "// Replace with real content\n"

    port_type = getattr(desc, "port_type", None)
    if port_type is dict:
        return {"replace_with_real_output": True}
    if port_type is list:
        return ["replace_with_real_output"]
    if port_type is bool:
        return True
    if port_type is int:
        return 0
    if port_type is float:
        return 0.0
    if _is_resource_type(port_type):
        return "<replace with real content>"
    return "<replace with real value>"


def _matches_repair_example_outputs(
    outputs: dict[str, Any],
    output_ports: dict[str, Any],
) -> bool:
    """Detect exact echoing of the repair template example for multi-output agents."""

    if len(output_ports) < 2:
        return False
    for port_name, desc in output_ports.items():
        if port_name not in outputs:
            return False
        if outputs[port_name] != _sample_agent_output_value(desc):
            return False
    return True


def _validate_agent_output_keys(
    outputs: dict[str, Any],
    output_ports: dict[str, Any],
    *,
    required_ports: set[str] | None = None,
) -> dict[str, Any]:
    """Validate declared output keys and reject template echo payloads."""

    declared = list(output_ports.keys())
    declared_set = set(declared)
    single = declared[0] if len(declared) == 1 else None

    for key in list(outputs.keys()):
        if key not in declared_set:
            if single and len(outputs) == 1:
                return {single: outputs[key]}
            raise ValueError(f"Agent returned undeclared output '{key}'.")

    if required_ports is None:
        required_ports = declared_set
    missing = [key for key in declared if key in required_ports and key not in outputs]
    if missing:
        raise ValueError("Agent response is missing declared output(s): " + ", ".join(missing) + ".")

    if _matches_repair_example_outputs(outputs, output_ports):
        raise ValueError("Agent response echoed repair example outputs instead of real content.")
    return outputs


def _parse_agent_outputs(
    content: str | None,
    output_ports: dict[str, Any],
    *,
    required_ports: set[str] | None = None,
) -> dict[str, Any]:
    """Parse LLM response text into a {portName: value} dict.

    Resolution order (matching TS runtime):
    1. Try JSON parse -> ``{"outputs": {"port": val}}``
    2. Top-level key matching declared port name (single output)
    3. Single top-level key -> remap to declared output
    4. Whole parsed object as value (single output)
    5. Top-level keys intersecting declared names (multi output)
    Fallback A: plain text -> single File-typed output
    """

    declared = list(output_ports.keys())
    declared_set = set(declared)
    single = declared[0] if len(declared) == 1 else None
    normalized = _normalize_agent_response_text(content, output_ports)
    content_text = "" if normalized is None else str(normalized)

    try:
        parsed = _parse_json_from_text(content_text, "agent response")
    except ValueError:
        if single and _single_output_plain_text_fallback_allowed(output_ports):
            extracted = _extract_wrapped_single_output_text(content_text, single)
            if extracted is not None:
                return {single: extracted}
            return {single: content_text}
        raise

    outputs_val = parsed.get("outputs")
    if isinstance(outputs_val, dict):
        out = _validate_agent_output_keys(outputs_val, output_ports, required_ports=required_ports)
    elif single:
        if single in parsed:
            return {single: parsed[single]}
        top = list(parsed.keys())
        if len(top) == 1:
            return {single: parsed[top[0]]}
        return {single: parsed}
    else:
        if any(key in declared_set for key in parsed):
            out = _validate_agent_output_keys(parsed, output_ports, required_ports=required_ports)
        else:
            raise ValueError(
                "Agent response must contain top-level 'outputs' or "
                f"keys matching declared ports ({', '.join(declared)})."
            )
    return out


def _extract_agent_context_patch(response_text: str) -> dict[str, Any] | None:
    """Extract optional context patch from raw agent response text."""

    try:
        parsed = _parse_json_from_text(response_text, "agent response")
    except ValueError:
        return None
    patch = parsed.get("contextPatch")
    if isinstance(patch, dict):
        return patch
    return None


def _build_agent_repair_prompt(
    response_text: str | None,
    output_ports: dict[str, Any],
    exc: Exception,
) -> str:
    """Build retry instruction for malformed/invalid agent output."""

    port_names = list(output_ports.keys())
    response_text_norm = "" if response_text is None else str(response_text)
    bad_preview = response_text_norm[:1000]
    if len(response_text_norm) > 1000:
        bad_preview += "... (truncated)"
    port_example = ", ".join(
        '"{0}": {1}'.format(
            port_name,
            json.dumps(_sample_agent_output_value(output_ports[port_name])),
        )
        for port_name in port_names
    )
    return (
        "Your previous response was invalid and must be repaired.\n\n"
        f"Validation error: {exc}\n\n"
        "Preserve the same factual analysis from the previous attempt; only fix the output format.\n"
        "Do not change the decision, candidate family/class, requested evidence, proof obligations, validation plan, or optimized code content.\n"
        "Do not add new reasoning about malformed output, repair-only turns, or prior-response recovery.\n"
        "Return ONLY one valid JSON object.\n"
        "Do not include markdown fences, commentary, or any text before or after the JSON.\n"
        f"Required output ports: {', '.join(port_names)}\n"
        "Every declared output port is required. Do not omit ports and do not substitute placeholder/example values.\n"
        "Use this exact top-level shape:\n"
        '{"outputs": {' + port_example + "}}\n\n"
        "Previous invalid response:\n"
        f"{bad_preview}"
    )


def _print_agent_usage_summary(
    response_debug_meta: dict[str, Any],
    actor_name: str,
    provider: str,
) -> None:
    """Print a human-readable usage/cost summary for an agent invocation."""
    usage_meta = response_debug_meta.get("usage")
    if not isinstance(usage_meta, dict):
        return

    tok = usage_meta.get("total_tokens", 0)
    pt = usage_meta.get("prompt_tokens", 0)
    ct = usage_meta.get("completion_tokens", 0)
    nr = usage_meta.get("num_requests", 0)
    tr = usage_meta.get("tool_rounds", 0)
    cost = usage_meta.get("total_cost_usd")

    # Try to fetch cost from OpenRouter Generation Stats API
    if cost is None and provider == "openrouter":
        from wfpy._agent_request_runtime import _lookup_openrouter_cost

        gen_ids = response_debug_meta.get("openrouter_generation_ids")
        if isinstance(gen_ids, list) and gen_ids:
            cost = _lookup_openrouter_cost(gen_ids, usage_meta)

    cost_str = f", cost=${cost:.4f}" if cost is not None else ""
    tool_round_str = f", tool_rounds={tr}" if tr else ""
    cache_write = usage_meta.get("cache_creation_input_tokens", 0)
    cache_read = usage_meta.get("cache_read_input_tokens", 0)
    cache_str = (
        f", cache write={cache_write} read={cache_read}"
        if cache_write or cache_read
        else ""
    )
    print(
        f"[wfpy][agent] {actor_name} usage: "
        f"{tok} tokens ({pt} prompt + {ct} completion), "
        f"{nr} requests{tool_round_str}{cache_str}{cost_str}"
    )


# ── Agent file-output provenance helpers ──────────────────────────────


def _tool_belongs_to_server(tool_name: str, server_names: Iterable[str]) -> bool:
    """Whether ``tool_name`` is a tool exposed by one of ``server_names``.

    Backends qualify MCP tool names with the server they came from, using
    either ``<server>.<tool>`` or ``<server>_<tool>``.
    """
    name = str(tool_name).strip()
    if not name:
        return False
    return any(
        name.startswith(f"{server}.") or name.startswith(f"{server}_")
        for server in (str(s).strip() for s in server_names)
        if server
    )


def _agent_mcp_verification_status(
    response_debug_meta: dict[str, Any],
    agent_spec: Any,
) -> str:
    """Summarize observable MCP usage for generated-code provenance headers."""
    configured = sorted(
        {str(name).strip() for name in (agent_spec.mcp_servers or []) if str(name).strip()}
    )
    cli_tools_mode = str(response_debug_meta.get("cliToolsMode") or "").strip().lower()
    used_servers = sorted(
        {
            str(name).strip()
            for name in (response_debug_meta.get("opencodeMcpServers") or [])
            if str(name).strip()
        }
    )
    tool_names = sorted(
        {
            str(name).strip()
            for name in (response_debug_meta.get("opencodeToolNames") or [])
            if str(name).strip()
        }
    )

    if used_servers:
        return f"used ({', '.join(used_servers)})"
    if configured and any(
        _tool_belongs_to_server(name, configured) for name in tool_names
    ):
        return f"used ({', '.join(configured)})"
    if configured:
        if cli_tools_mode == "native":
            return f"usage unknown ({', '.join(configured)} configured via native tools)"
        return f"not used ({', '.join(configured)} configured)"
    return "not configured"


def _observed_mcp_tool_names(
    response_debug_meta: dict[str, Any], agent_spec: Any
) -> list[str]:
    """Return observed MCP tool names from agent debug metadata.

    Only tools qualified with one of the agent's configured MCP servers count;
    unqualified backend-native tool names are ignored.
    """
    configured_names = {
        str(getattr(item, "name", "")).strip()
        for item in (agent_spec.mcp_server_configs or [])
        if str(getattr(item, "name", "")).strip()
    }
    configured_names.update(
        str(name).strip()
        for name in (agent_spec.mcp_servers or [])
        if isinstance(name, str) and str(name).strip()
    )
    observed = [
        str(name).strip()
        for name in response_debug_meta.get("opencodeToolNames") or []
        if _tool_belongs_to_server(str(name).strip(), configured_names)
    ]
    return sorted(set(observed))


def _stamp_agent_file_output_provenance(
    file_path: Path,
    response_debug_meta: dict[str, Any],
    agent_spec: Any,
) -> None:
    """Replace template MCP provenance comments with actual runtime status.

    Opt-in per output file: only files whose template already carries the
    ``// MCP verification:`` marker line are rewritten.
    """
    marker = "// MCP verification:"
    try:
        text = file_path.read_text()
    except OSError:
        return
    if marker not in text:
        return

    status = _agent_mcp_verification_status(response_debug_meta, agent_spec)
    updated_lines: list[str] = []
    replaced = False
    for line in text.splitlines():
        if line.startswith(marker):
            updated_lines.append(f"// MCP verification: {status}.")
            replaced = True
        else:
            updated_lines.append(line)
    if not replaced:
        return

    new_text = "\n".join(updated_lines)
    if text.endswith("\n"):
        new_text += "\n"
    if new_text != text:
        file_path.write_text(new_text)


# ── Agent file-output materialization ─────────────────────────────────


def _looks_like_code(text: str, ext: str) -> bool:
    """Check if text looks like actual code vs a summary.
    
    Returns True if the text appears to be code, False if it looks like
    a summary or description.
    """
    if not text or len(text) < 10:
        return False
    
    # Check first few lines for code indicators
    lines = text.split('\n')[:5]
    first_lines = '\n'.join(lines).strip()
    
    # Code indicators for C/C++
    if ext in [".cpp", ".c", ".h", ".hpp"]:
        code_indicators = [
            '//',           # Comment
            '#include',     # Include directive
            '#define',      # Define directive
            '/*',           # Block comment
            'int ',         # Type declaration
            'void ',        # Type declaration
            'float ',       # Type declaration
            'double ',      # Type declaration
            'class ',       # Class declaration
            'struct ',      # Struct declaration
            'namespace ',   # Namespace declaration
            'template',     # Template declaration
            'using ',       # Using declaration
        ]
        return any(indicator in first_lines for indicator in code_indicators)
    
    # For other extensions, assume it's code if it's not empty
    return True


def _find_kernel_in_work_dir(work_dir: Path, ext: str) -> Path | None:
    """Search for kernel files in the work directory.
    
    Looks for files with the given extension that might be the actual kernel.
    Returns the first matching file, or None if not found.
    """
    if not work_dir.exists():
        return None
    
    # Common kernel file names
    kernel_names = [
        "kernel.cpp",
        "kernel.c",
        "kernel.h",
        "kernel.hpp",
        "main.cpp",
        "main.c",
    ]
    
    # First, try common names
    for name in kernel_names:
        if name.endswith(ext):
            candidate = work_dir / name
            if candidate.exists() and candidate.stat().st_size > 100:
                return candidate
    
    # If not found, look for any .cpp/.c file that's not the output file
    for candidate in work_dir.glob(f"*{ext}"):
        # Skip files that look like output files (contain __)
        if '__' not in candidate.name and candidate.stat().st_size > 100:
            return candidate
    
    return None


def _materialize_agent_file_outputs(
    output_ports: dict[str, Any],
    parsed_outputs: dict[str, Any],
    response_text: str,
    actor_name: str,
    fire_count: int,
    out_dir: Path,
    response_debug_meta: dict[str, Any],
    agent_spec: Any,
    *,
    is_multi_output: bool,
    preserve_existing: bool = False,
    existing_materialized: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write File-typed agent outputs to disk.

    Returns a ``{port_name: output_value}`` mapping where file-typed values
    are replaced with their on-disk paths.

    Args:
        output_ports: Port descriptors keyed by port name.
        parsed_outputs: Parsed agent response outputs.
        response_text: Raw response text (used as fallback for single-output).
        actor_name: Name of the actor producing the outputs.
        fire_count: Current fire count for unique file naming.
        out_dir: Directory to write materialized files.
        response_debug_meta: Debug metadata for provenance stamping.
        agent_spec: Agent specification for provenance stamping.
        is_multi_output: True when the agent has more than one output port.
        preserve_existing: When True (validation repair), skip empty-string
            outputs for multi-output agents to preserve last materialized file.
        existing_materialized: Prior materialized dict to merge with when
            preserve_existing is True. Ports not re-materialized keep their
            prior value.
    """
    materialized: dict[str, Any] = {}
    for port_name, pd in output_ports.items():
        if port_name not in parsed_outputs and is_multi_output:
            # Port absent from response — keep prior value if preserving
            if preserve_existing and existing_materialized and port_name in existing_materialized:
                materialized[port_name] = existing_materialized[port_name]
            continue
        value = parsed_outputs.get(port_name, response_text)
        ext = pd.ext or ".txt"
        is_file = _is_resource_type(pd.port_type)

        # Multi-output agents use empty-string file outputs to mean
        # "port intentionally omitted". When preserve_existing is set
        # (validation repair), skip these to keep the last materialized file.
        if is_multi_output and is_file and isinstance(value, str) and value == "":
            if preserve_existing and existing_materialized and port_name in existing_materialized:
                materialized[port_name] = existing_materialized[port_name]
            continue

        if is_file and isinstance(value, (dict, list)):
            out_file = out_dir / f"{actor_name}__{port_name}__{fire_count}{ext}"
            out_file.write_text(json.dumps(value, indent=2, default=str))
            _stamp_agent_file_output_provenance(
                out_file, response_debug_meta, agent_spec
            )
            materialized[port_name] = str(out_file)
        elif is_file and isinstance(value, str) and not os.path.isfile(value):
            # Check if response text looks like valid code
            actual_content = value
            if ext in [".cpp", ".c", ".h", ".hpp"] and not _looks_like_code(value, ext):
                # Response is a summary, search for actual kernel file in work directory
                kernel_file = _find_kernel_in_work_dir(out_dir, ext)
                if kernel_file:
                    actual_content = kernel_file.read_text()
            
            out_file = out_dir / f"{actor_name}__{port_name}__{fire_count}{ext}"
            out_file.write_text(actual_content)
            _stamp_agent_file_output_provenance(
                out_file, response_debug_meta, agent_spec
            )
            materialized[port_name] = str(out_file)
        elif is_file and isinstance(value, str):
            out_file = out_dir / f"{actor_name}__{port_name}__{fire_count}{ext}"
            shutil.copy2(value, out_file)
            _stamp_agent_file_output_provenance(
                out_file, response_debug_meta, agent_spec
            )
            materialized[port_name] = str(out_file)
        else:
            materialized[port_name] = value

    return materialized
