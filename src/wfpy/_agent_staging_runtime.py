"""Internal helpers for agent file input staging (CLI transports)."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from wfpy._agent_validation_runtime import _is_resource_type
from wfpy.types import Resource, infer_resource_kind

AGENT_FILE_INPUT_MAX_CHARS = 200_000
CLI_AGENT_STAGED_INPUT_DIRNAME = "agent-inputs"


def _dequeue_agent_inputs(
    actor: Any,
    input_ports: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Check input availability and dequeue if available.

    Returns:
        (has_inputs, input_values) — has_inputs is True when tokens were
        available on all input ports. input_values is empty when has_inputs
        is False.
    """
    has_inputs = True
    for port_name in input_ports:
        queues = actor.in_queues.get(port_name, [])
        if not queues or queues[0].size() < 1:
            has_inputs = False
            break

    input_values: dict[str, Any] = {}
    if has_inputs:
        for port_name in input_ports:
            q = actor.in_queues[port_name][0]
            input_values[port_name] = q.dequeue()

    return has_inputs, input_values


def _directory_listing(root: str, max_entries: int = 200, max_depth: int = 4) -> list[str]:
    """Return a flat list of relative paths under *root* (capped)."""
    entries: list[str] = []
    root_path = Path(root)
    try:
        for item in sorted(root_path.rglob("*")):
            if len(entries) >= max_entries:
                entries.append(f"... (truncated at {max_entries} entries)")
                break
            rel = item.relative_to(root_path)
            if len(rel.parts) > max_depth:
                continue
            suffix = "/" if item.is_dir() else ""
            entries.append(str(rel) + suffix)
    except OSError:
        pass
    return entries


def _resource_meta(value: Any) -> dict[str, Any] | None:
    """Build structured resource metadata for an agent input value."""
    locator = ""
    declared_kind = ""
    if isinstance(value, Resource):
        locator = str(value.path or str(value))
        declared_kind = str(value.kind or "").strip().lower()
    elif isinstance(value, Path):
        locator = str(value)
    elif isinstance(value, str):
        locator = value
    else:
        return None

    locator = locator.strip()
    kind = infer_resource_kind(value if isinstance(value, Resource) else locator)
    is_url_like = bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", locator))
    exists = False
    is_dir = False
    is_file = False
    if locator and not is_url_like:
        exists = os.path.exists(locator)
        is_dir = os.path.isdir(locator)
        is_file = os.path.isfile(locator)

    meta: dict[str, Any] = {
        "path": locator,
        "kind": kind,
        "exists": exists,
        "isDir": is_dir,
        "isFile": is_file,
    }
    if declared_kind and declared_kind != kind:
        meta["declaredKind"] = declared_kind
    # For local directories, include a recursive file listing
    if is_dir:
        meta["listing"] = _directory_listing(locator)
    return meta


def _build_file_input_meta(file_path: str) -> dict[str, Any] | None:
    """Read a file and return truncated content metadata for the agent payload."""
    try:
        raw = Path(file_path).read_text(errors="replace")
    except Exception:
        return None
    truncated = len(raw) > AGENT_FILE_INPUT_MAX_CHARS
    return {
        "path": file_path,
        "content": raw[:AGENT_FILE_INPUT_MAX_CHARS],
        "sizeBytes": os.path.getsize(file_path),
        "truncated": truncated,
    }


def _build_input_collections(
    input_values: dict[str, Any],
    input_ports: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Build file_inputs, resource_inputs, and inputs_payload from dequeued values.

    Returns:
        (file_inputs, resource_inputs, inputs_payload)
    """
    file_inputs: dict[str, dict[str, Any]] = {}
    resource_inputs: dict[str, dict[str, Any]] = {}
    inputs_payload: dict[str, Any] = {}

    for port_name, value in input_values.items():
        pd = input_ports.get(port_name)
        is_file = pd and _is_resource_type(pd.port_type)
        if is_file:
            rmeta = _resource_meta(value)
            if rmeta is not None:
                resource_inputs[port_name] = rmeta
        # Resolve File/Resource objects to their string path for content reading
        str_value: str | None = None
        if isinstance(value, str):
            str_value = value
        elif isinstance(value, Resource):
            str_value = value.path or None
        if str_value and os.path.isfile(str_value):
            is_file = True
        if is_file and str_value and os.path.isfile(str_value):
            file_input_meta = _build_file_input_meta(str_value)
            if file_input_meta is not None:
                file_inputs[port_name] = file_input_meta
            else:
                inputs_payload[port_name] = value
        else:
            inputs_payload[port_name] = value

    return file_inputs, resource_inputs, inputs_payload


# ── CLI file staging ──────────────────────────────────────────────────


def _resolve_cli_staged_dir(
    transport: str,
    actor_name: str,
    out_dir: Path,
) -> Path | None:
    """Resolve the CLI staging directory for a given transport.

    Returns None if the transport is not a CLI transport.
    """
    if transport not in {"opencode-cli", "opencode-acp", "claude-cli", "codex-cli"}:
        return None
    run_out = os.environ.get("WF_RUN_OUT_DIR")
    cli_root = Path(run_out) / "work" if run_out else out_dir
    staged_dir = cli_root / CLI_AGENT_STAGED_INPUT_DIRNAME / actor_name
    staged_dir.mkdir(parents=True, exist_ok=True)
    return staged_dir


def _safe_stage_name(
    port_name: str,
    original_path: str,
    input_ports: dict[str, Any],
) -> str:
    """Generate a filesystem-safe staged filename for a CLI agent input."""
    src = Path(original_path)
    port_desc = input_ports.get(port_name)
    suffix = src.suffix or (port_desc.ext if port_desc and port_desc.ext else ".txt")
    stem = src.stem or port_name
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("_") or port_name
    return f"{port_name}__{safe_stem}{suffix}"


def _stage_cli_file_input(
    port_name: str,
    original_path: str,
    cli_staged_dir: Path,
    input_ports: dict[str, Any],
) -> str:
    """Copy a file to the CLI staging directory and return the staged path."""
    staged_path = cli_staged_dir / _safe_stage_name(port_name, original_path, input_ports)
    if not staged_path.exists() or os.path.realpath(original_path) != os.path.realpath(
        str(staged_path)
    ):
        shutil.copy2(original_path, staged_path)
    support_dir = Path(original_path).parent / "include"
    if support_dir.is_dir():
        staged_support_dir = cli_staged_dir / "include"
        staged_support_dir.mkdir(parents=True, exist_ok=True)
        for item in support_dir.iterdir():
            dst = staged_support_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst)
    return str(staged_path)


def _stage_cli_json_path(
    path_label: str,
    original_path: str,
    *,
    cli_staged_dir: Path,
    input_ports: dict[str, Any],
    staged_path_by_original: dict[str, str],
    relative_to: Path | None = None,
) -> str | None:
    """Stage a file referenced inside a JSON input and return the staged path."""
    candidate = original_path.strip()
    if not candidate or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", candidate):
        return None
    if relative_to is not None:
        candidate_path = Path(candidate)
        if not candidate_path.is_absolute():
            candidate = str((relative_to / candidate_path).resolve())
    staged_existing = staged_path_by_original.get(candidate)
    if staged_existing is not None:
        return staged_existing
    if not os.path.isfile(candidate):
        return None
    staged_path = _stage_cli_file_input(path_label, candidate, cli_staged_dir, input_ports)
    staged_path_by_original[candidate] = staged_path
    return staged_path


def _rewrite_staged_json_paths(
    value: Any,
    field_name: str | None = None,
    path_label: str = "Context",
    *,
    cli_staged_dir: Path,
    input_ports: dict[str, Any],
    staged_path_by_original: dict[str, str],
    relative_to: Path | None = None,
) -> tuple[Any, bool]:
    """Recursively rewrite path fields in a JSON value to staged paths."""
    if isinstance(value, dict):
        changed = False
        rewritten: dict[str, Any] = {}
        for key, item in value.items():
            child_key = str(key)
            child_label = f"{path_label}_{child_key}"
            new_item, item_changed = _rewrite_staged_json_paths(
                item,
                child_key,
                child_label,
                cli_staged_dir=cli_staged_dir,
                input_ports=input_ports,
                staged_path_by_original=staged_path_by_original,
                relative_to=relative_to,
            )
            rewritten[key] = new_item
            changed = changed or item_changed
        return rewritten, changed
    if isinstance(value, list):
        changed = False
        rewritten_items: list[Any] = []
        for idx, item in enumerate(value):
            child_label = f"{path_label}_{idx}"
            new_item, item_changed = _rewrite_staged_json_paths(
                item,
                field_name,
                child_label,
                cli_staged_dir=cli_staged_dir,
                input_ports=input_ports,
                staged_path_by_original=staged_path_by_original,
                relative_to=relative_to,
            )
            rewritten_items.append(new_item)
            changed = changed or item_changed
        return rewritten_items, changed
    if isinstance(value, str) and field_name:
        normalized_name = field_name.lower()
        if normalized_name == "path" or normalized_name.endswith("_path"):
            staged_path = _stage_cli_json_path(
                path_label,
                value,
                cli_staged_dir=cli_staged_dir,
                input_ports=input_ports,
                staged_path_by_original=staged_path_by_original,
                relative_to=relative_to,
            )
            if staged_path is not None:
                return staged_path, True
    return value, False


def _rewrite_staged_json_file(
    staged_path: str,
    *,
    staged_path_by_original: dict[str, Any],
    cli_staged_dir: Path,
    input_ports: dict[str, Any],
    original_path: str | None = None,
) -> None:
    """Rewrite path references inside a staged JSON file."""
    staged_file = Path(staged_path)
    if staged_file.suffix.lower() != ".json" or not staged_path_by_original:
        return
    try:
        payload_obj = json.loads(staged_file.read_text(errors="replace"))
    except Exception:
        return
    relative_to = staged_file.parent
    if original_path:
        try:
            relative_to = Path(original_path).resolve().parent
        except OSError:
            relative_to = staged_file.parent
    rewritten_obj, changed = _rewrite_staged_json_paths(
        payload_obj,
        cli_staged_dir=cli_staged_dir,
        input_ports=input_ports,
        staged_path_by_original=staged_path_by_original,
        relative_to=relative_to,
    )
    if not changed:
        return
    staged_file.write_text(json.dumps(rewritten_obj, indent=2) + "\n")


def _refresh_file_input_content(meta_entry: dict[str, Any]) -> None:
    """Re-read file content after staging rewrites have modified the file."""
    staged_path = str(meta_entry.get("path") or "").strip()
    if not staged_path or not os.path.isfile(staged_path):
        return
    refreshed = _build_file_input_meta(staged_path)
    if refreshed is None:
        return
    meta_entry["content"] = refreshed["content"]
    meta_entry["sizeBytes"] = refreshed["sizeBytes"]
    meta_entry["truncated"] = refreshed["truncated"]


def _stage_cli_inputs(
    file_inputs: dict[str, dict[str, Any]],
    resource_inputs: dict[str, dict[str, Any]],
    cli_staged_dir: Path,
    input_ports: dict[str, Any],
) -> dict[str, str]:
    """Stage file inputs for CLI transports.

    Copies files to the staging directory and rewrites JSON path references.

    Returns:
        staged_path_by_original mapping.
    """
    staged_path_by_original: dict[str, str] = {}

    for port_name, meta_entry in resource_inputs.items():
        if not isinstance(meta_entry, dict):
            continue
        original_path = str(meta_entry.get("path") or "").strip()
        if original_path and os.path.isfile(original_path):
            staged_path = _stage_cli_file_input(
                port_name, original_path, cli_staged_dir, input_ports
            )
            staged_path_by_original.setdefault(original_path, staged_path)
            meta_entry["path"] = staged_path
            meta_entry["originalPath"] = original_path

    for port_name, meta_entry in file_inputs.items():
        if not isinstance(meta_entry, dict):
            continue
        original_path = str(meta_entry.get("path") or "").strip()
        if original_path and os.path.isfile(original_path):
            staged_path = _stage_cli_file_input(
                port_name, original_path, cli_staged_dir, input_ports
            )
            staged_path_by_original.setdefault(original_path, staged_path)
            meta_entry["path"] = staged_path
            meta_entry["originalPath"] = original_path

    for meta_entry in file_inputs.values():
        if not isinstance(meta_entry, dict) or "originalPath" not in meta_entry:
            continue
        staged_path = str(meta_entry.get("path") or "").strip()
        if staged_path and os.path.isfile(staged_path):
            _rewrite_staged_json_file(
                staged_path,
                staged_path_by_original=staged_path_by_original,
                cli_staged_dir=cli_staged_dir,
                input_ports=input_ports,
                original_path=str(meta_entry.get("originalPath") or "").strip() or None,
            )
            _refresh_file_input_content(meta_entry)

    return staged_path_by_original
