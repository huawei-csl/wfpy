"""Internal runtime helpers for overlays, queue traces, and debug artifacts."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from wfpy.types import File, Resource


class _RuntimeJsonEncoder(json.JSONEncoder):
    """JSON encoder matching the TS ``runtimeJsonReplacer``."""

    def default(self, o: Any) -> Any:  # noqa: D401
        if isinstance(o, (set, frozenset)):
            return {"$wfType": "set", "values": list(o)}
        if isinstance(o, Resource):
            return o.path or str(o)
        if isinstance(o, Path):
            return str(o)
        if isinstance(o, bytes):
            return o.decode("utf-8", errors="replace")
        return super().default(o)


def _runtime_json_dumps(value: Any, *, indent: int | None = 2) -> str:
    """Pretty-print ``value`` using the wf-lang custom JSON encoder."""

    return json.dumps(value, cls=_RuntimeJsonEncoder, indent=indent)


def _runtime_json_dumps_line(value: Any) -> str:
    """Single-line JSON using the wf-lang custom JSON encoder."""

    return json.dumps(value, cls=_RuntimeJsonEncoder)


def _materialize_edge_token(plan: Any, q: Any, value: Any) -> Any:
    """Convert runtime edge tokens into viewer-openable values when needed."""

    if value is None:
        return None
    if isinstance(value, Resource):
        return value.path or str(value)
    if isinstance(value, File):
        return value.path or str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return value

    work_dir = (plan.work_dir or "").strip()
    if not work_dir:
        return value

    token_dir = Path(work_dir) / "edge-tokens"
    token_dir.mkdir(parents=True, exist_ok=True)
    safe_queue_id = re.sub(r"[^A-Za-z0-9._-]+", "_", q.id).strip("_") or "queue"
    token_path = token_dir / f"{safe_queue_id}.json"
    token_path.write_text(_runtime_json_dumps(value))
    return str(token_path)


def _record_edge_token(plan: Any, q: Any, value: Any) -> None:
    """Update the last-token map for a queue (used by viewer overlay)."""

    token_value = _materialize_edge_token(plan, q, value)
    plan.edge_last_token_by_queue_id[q.id] = token_value
    if q.overlay_ast_path:
        plan.edge_last_token_by_ast_path[q.overlay_ast_path] = token_value


def _collect_edge_overlay(plan: Any) -> dict[str, dict[str, Any]]:
    """Merge edge info + last tokens from *plan* and all sub-plans."""

    info: dict[str, dict[str, str]] = {}
    tokens: dict[str, Any] = {}
    queue_info: dict[str, dict[str, str]] = {}
    queue_tokens: dict[str, Any] = {}
    _collect_edges_recursive(plan, info, tokens, queue_info, queue_tokens)

    edges: dict[str, dict[str, Any]] = {}
    represented_queue_ids: set[str] = set()
    for ast_path, edge_info in info.items():
        entry: dict[str, Any] = {
            "queueId": edge_info["queueId"],
            "outPort": edge_info["outPort"],
            "inPort": edge_info["inPort"],
        }
        represented_queue_ids.add(edge_info["queueId"])
        if edge_info.get("fromEntity"):
            entry["fromEntity"] = edge_info["fromEntity"]
        if edge_info.get("toEntity"):
            entry["toEntity"] = edge_info["toEntity"]
        if ast_path in tokens:
            entry["lastToken"] = tokens[ast_path]
        edges[ast_path] = entry

    for queue_id, queue_details in queue_info.items():
        if queue_id in represented_queue_ids:
            continue
        queue_entry: dict[str, Any] = {
            "queueId": queue_details["queueId"],
            "outPort": queue_details["outPort"],
            "inPort": queue_details["inPort"],
        }
        if queue_details.get("fromEntity"):
            queue_entry["fromEntity"] = queue_details["fromEntity"]
        if queue_details.get("toEntity"):
            queue_entry["toEntity"] = queue_details["toEntity"]
        if queue_id in queue_tokens:
            queue_entry["lastToken"] = queue_tokens[queue_id]
        edges[f"queue:{queue_id}"] = queue_entry
    return edges


def _collect_edges_recursive(
    plan: Any,
    info: dict[str, dict[str, str]],
    tokens: dict[str, Any],
    queue_info: dict[str, dict[str, str]],
    queue_tokens: dict[str, Any],
) -> None:
    """Recursively gather edge info + tokens from *plan* and nested sub-plans."""

    info.update(plan.edge_info_by_ast_path)
    tokens.update(plan.edge_last_token_by_ast_path)
    queue_info.update(plan.edge_info_by_queue_id)
    queue_tokens.update(plan.edge_last_token_by_queue_id)
    for actor in plan.actors:
        if actor.kind == "workflow" and actor.sub_plan:
            _collect_edges_recursive(actor.sub_plan, info, tokens, queue_info, queue_tokens)


@dataclasses.dataclass
class _ViewerOverlayBase:
    """Immutable per-run metadata used to build viewer overlays."""

    run_id: str
    workflow_name: str
    source_path: str
    started_at: str
    out_dir: str


@dataclasses.dataclass
class _ViewerOverlayActive:
    """Currently-firing entity for viewer overlay."""

    entity_instance_name: str
    entity_instance_path: list[str]
    entity_kind: str
    fire_count: int


def _build_viewer_overlay_v1(
    plan: Any,
    base: _ViewerOverlayBase,
    active: list[_ViewerOverlayActive] | None,
    running: bool,
    *,
    finished_at: str | None = None,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the ``ViewerOverlayV1`` JSON structure."""

    overlay: dict[str, Any] = {
        "version": 1,
        "runId": base.run_id,
        "workflowName": base.workflow_name,
        "sourcePath": base.source_path,
        "startedAt": base.started_at,
        "outDir": base.out_dir,
        "running": running,
    }
    if finished_at is not None:
        overlay["finishedAt"] = finished_at
    if active:
        overlay["active"] = [
            {
                "entityInstanceName": a.entity_instance_name,
                "entityInstancePath": list(a.entity_instance_path),
                "entityKind": a.entity_kind,
                "fireCount": a.fire_count,
            }
            for a in active
        ]
    if error is not None:
        overlay["error"] = error
    overlay["edges"] = _collect_edge_overlay(plan)
    return overlay


class _ViewerOverlayWriter:
    """Manages the live overlay file during execution."""

    def __init__(
        self,
        plan: Any,
        base: _ViewerOverlayBase,
        live_path: Path,
    ) -> None:
        self._plan = plan
        self._base = base
        self._live_path = live_path
        self._lock = threading.Lock()
        self._active_map: dict[str, _ViewerOverlayActive] = {}
        self._last_active_key: str | None = None
        self._running = True

    @property
    def active_entries(self) -> list[_ViewerOverlayActive]:
        """All currently-active entities (may be multiple in parallel)."""
        return list(self._active_map.values())

    def mark_running(self) -> None:
        """Write initial overlay with ``running=True``."""

        with self._lock:
            self._write(active=None, running=True)

    def set_active(self, actor: Any | None) -> None:
        """Update the active entity (``None`` = idle between firings)."""

        self.set_active_with_prefix(actor, [])

    def set_active_with_prefix(self, actor: Any | None, path_prefix: list[str]) -> None:
        """Add *actor* to the active set (or clear all if ``None``)."""

        with self._lock:
            if actor is None:
                self._active_map.clear()
                self._write(active=None, running=True)
                return

            instance_path = [part for part in [*path_prefix, actor.name] if isinstance(part, str) and part.strip()]
            key = ".".join(instance_path) if instance_path else actor.name
            self._active_map[key] = _ViewerOverlayActive(
                entity_instance_name=actor.name,
                entity_instance_path=instance_path,
                entity_kind=actor.kind,
                fire_count=actor.fire_count,
            )
            self._write(active=list(self._active_map.values()), running=True)

    def remove_active_with_prefix(self, actor: Any | None, path_prefix: list[str]) -> None:
        """Remove *actor* from the active set (or clear all if ``None``)."""

        with self._lock:
            if actor is None:
                self._active_map.clear()
                self._write(active=None, running=True)
                return

            instance_path = [part for part in [*path_prefix, actor.name] if isinstance(part, str) and part.strip()]
            key = ".".join(instance_path) if instance_path else actor.name
            self._active_map.pop(key, None)
            self._write(active=list(self._active_map.values()) if self._active_map else None, running=True)

    def mark_stopped(self) -> None:
        """Write a final stopped live overlay with no active nodes."""

        with self._lock:
            self._active_map.clear()
            self._write(active=None, running=False)

    def cleanup(self) -> None:
        """Delete the live overlay file."""

        try:
            self._live_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _write(
        self,
        active: list[_ViewerOverlayActive] | None,
        running: bool,
    ) -> None:
        if active:
            key = ",".join(
                f"{a.entity_instance_name}:{a.entity_kind}:{a.fire_count}"
                for a in active
            )
        else:
            key = "none"
        if key == self._last_active_key and running == self._running:
            return

        self._last_active_key = key
        self._running = running
        overlay = _build_viewer_overlay_v1(self._plan, self._base, active, running)
        try:
            self._live_path.write_text(_runtime_json_dumps(overlay))
        except OSError:
            pass


def _build_agent_context_entry(actor: Any) -> dict[str, Any]:
    """Build one ``AgentContextEntry`` dict for an agent actor."""

    agent_spec = actor.meta.agent_spec
    entry = {
        "instanceName": actor.name,
        "entityName": type(actor.instance).__name__,
        "fireCount": actor.fire_count,
        "model": agent_spec.model or None,
        "stateful": bool(agent_spec.stateful),
        "askUser": bool(getattr(agent_spec, "ask_user", False)),
        "contextBudget": agent_spec.context_budget,
        "truncationStrategy": agent_spec.truncation_strategy,
        "chatHistory": list(actor.chat_history),
    }
    session_ids = getattr(actor, "agent_cli_session_ids", None)
    if isinstance(session_ids, dict) and session_ids:
        entry["agentCliSessionIds"] = dict(session_ids)
    return entry


def _collect_agent_entries(plan: Any) -> list[dict[str, Any]]:
    """Recursively collect agent context entries from *plan* and sub-plans."""

    entries: list[dict[str, Any]] = []
    for actor in plan.actors:
        if actor.kind == "agent" and actor.chat_history:
            entries.append(_build_agent_context_entry(actor))
        if actor.kind == "workflow" and actor.sub_plan:
            entries.extend(_collect_agent_entries(actor.sub_plan))
    return entries


def _plan_has_agents(plan: Any) -> bool:
    """Return ``True`` when *plan* contains any agent, including nested sub-plans."""

    for actor in plan.actors:
        if actor.kind == "agent":
            return True
        if actor.kind == "workflow" and actor.sub_plan and _plan_has_agents(actor.sub_plan):
            return True
    return False


def _build_agent_context_overlay(
    plan: Any,
    run_id: str,
    workflow_name: str,
    source_path: str,
) -> dict[str, Any]:
    """Build the ``AgentContextOverlayV1`` JSON structure."""

    return {
        "version": 1,
        "runId": run_id,
        "workflowName": workflow_name,
        "sourcePath": source_path,
        "agents": _collect_agent_entries(plan),
    }


class _AgentContextWriter:
    """Manages the live agent-context overlay file during execution."""

    def __init__(
        self,
        plan: Any,
        run_id: str,
        workflow_name: str,
        source_path: str,
        live_path: Path,
    ) -> None:
        self._plan = plan
        self._run_id = run_id
        self._workflow_name = workflow_name
        self._source_path = source_path
        self._live_path = live_path
        self._last_signature: str = ""

    def write(self) -> None:
        """Write the overlay if anything changed since last write."""

        sig = self._signature()
        if sig == self._last_signature:
            return
        self._last_signature = sig
        overlay = _build_agent_context_overlay(
            self._plan,
            self._run_id,
            self._workflow_name,
            self._source_path,
        )
        try:
            self._live_path.write_text(_runtime_json_dumps(overlay))
        except OSError:
            pass

    def cleanup(self) -> None:
        """Delete the live file."""

        try:
            self._live_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _signature(self) -> str:
        parts: list[str] = []

        def collect(plan: Any, path_prefix: list[str]) -> None:
            for actor in plan.actors:
                actor_path = [*path_prefix, actor.name]
                if actor.kind == "agent" and actor.chat_history:
                    history = actor.chat_history
                    last_len = len(history[-1].get("content", "")) if history else 0
                    parts.append(
                        f"{'/'.join(actor_path)}:{actor.fire_count}:{len(history)}:{last_len}"
                    )
                if actor.kind == "workflow" and actor.sub_plan:
                    collect(actor.sub_plan, actor_path)

        collect(self._plan, [])
        return "|".join(parts)


def _build_context_overlay(
    plan: Any,
    run_id: str,
    workflow_name: str,
    source_path: str,
) -> dict[str, Any]:
    """Build the ``ContextOverlayV1`` JSON structure."""

    return {
        "version": 1,
        "runId": run_id,
        "workflowName": workflow_name,
        "sourcePath": source_path,
        "contextVersion": plan.context_version,
        "contextStore": copy.deepcopy(plan.context_store),
        "contextJournalSize": len(plan.context_journal),
    }


class _ContextLiveWriter:
    """Writes live shared-context overlay while run is active."""

    def __init__(
        self,
        plan: Any,
        run_id: str,
        workflow_name: str,
        source_path: str,
        live_path: Path,
    ) -> None:
        self._plan = plan
        self._run_id = run_id
        self._workflow_name = workflow_name
        self._source_path = source_path
        self._live_path = live_path
        self._last_sig: str = ""

    def write(self) -> None:
        sig = f"{self._plan.context_version}:{len(self._plan.context_journal)}"
        if sig == self._last_sig:
            return
        self._last_sig = sig
        overlay = _build_context_overlay(
            self._plan,
            self._run_id,
            self._workflow_name,
            self._source_path,
        )
        try:
            self._live_path.write_text(_runtime_json_dumps(overlay))
        except OSError:
            pass

    def cleanup(self) -> None:
        try:
            self._live_path.unlink(missing_ok=True)
        except OSError:
            pass


def _rewrite_viewer_overlay_tokens(
    plan: Any,
    work_dir: str,
    intermediates_dir: str,
) -> None:
    """Rewrite file-path tokens so they point to the persisted intermediates."""

    prefix = work_dir if work_dir.endswith(os.sep) else work_dir + os.sep
    replacement = intermediates_dir if intermediates_dir.endswith(os.sep) else intermediates_dir + os.sep
    _rewrite_tokens_recursive(plan, prefix, replacement)


def _rewrite_tokens_recursive(
    plan: Any,
    prefix: str,
    replacement: str,
) -> None:
    for ast_path, token in list(plan.edge_last_token_by_ast_path.items()):
        if isinstance(token, str) and token.startswith(prefix):
            plan.edge_last_token_by_ast_path[ast_path] = replacement + token[len(prefix) :]
    for queue_id, token in list(plan.edge_last_token_by_queue_id.items()):
        if isinstance(token, str) and token.startswith(prefix):
            plan.edge_last_token_by_queue_id[queue_id] = replacement + token[len(prefix) :]
    for actor in plan.actors:
        if actor.kind == "workflow" and actor.sub_plan:
            _rewrite_tokens_recursive(actor.sub_plan, prefix, replacement)


@dataclasses.dataclass
class QueueTraceStep:
    """One firing step in the queue trace."""

    step: int
    workflow_name: str
    actor_instance_name: str
    actor_kind: str
    actor_fire_count: int
    queue_sizes: list[dict[str, Any]]


@dataclasses.dataclass
class QueueTraceCollector:
    """Accumulates queue snapshots during execution for post-run analysis."""

    enabled: bool
    workflow_name: str
    steps: list[QueueTraceStep] = dataclasses.field(default_factory=list)

    def record_fire(self, plan: Any, actor: Any) -> None:
        """Record a queue snapshot after an actor fires."""

        if not self.enabled:
            return
        sizes = _collect_queue_snapshot(plan)
        self.steps.append(
            QueueTraceStep(
                step=len(self.steps) + 1,
                workflow_name=self.workflow_name,
                actor_instance_name=actor.name,
                actor_kind=actor.kind,
                actor_fire_count=actor.fire_count,
                queue_sizes=sizes,
            )
        )

    def build(self, finished_at: str, plan: Any) -> dict[str, Any]:
        """Build the final queue trace JSON."""

        leftovers: list[dict[str, Any]] = []
        for q in plan.all_queues:
            if q.size() > 0 and all(q not in queues for queues in plan.wf_output_queues.values()):
                leftovers.append(
                    {
                        "queueId": q.id,
                        "count": q.size(),
                    }
                )

        return {
            "version": 1,
            "workflowName": self.workflow_name,
            "finishedAt": finished_at,
            "stepCount": len(self.steps),
            "steps": [
                {
                    "step": step.step,
                    "workflowName": step.workflow_name,
                    "actorInstanceName": step.actor_instance_name,
                    "actorKind": step.actor_kind,
                    "actorFireCount": step.actor_fire_count,
                    "queueSizes": step.queue_sizes,
                }
                for step in self.steps
            ],
            "leftovers": leftovers,
        }


def _collect_queue_snapshot(plan: Any) -> list[dict[str, Any]]:
    """Snapshot all queue sizes in a plan (including nested sub-plans)."""

    sizes: list[dict[str, Any]] = []
    for q in plan.all_queues:
        entry: dict[str, Any] = {
            "scope": plan.name,
            "queueId": q.id,
            "size": q.size(),
        }
        if q.id in plan.edge_last_token_by_queue_id:
            entry["lastToken"] = plan.edge_last_token_by_queue_id[q.id]
        sizes.append(entry)
    for actor in plan.actors:
        if actor.kind == "workflow" and actor.sub_plan:
            sizes.extend(_collect_queue_snapshot(actor.sub_plan))
    return sizes


def _agent_debug_enabled(options: dict[str, Any]) -> bool:
    """Check if agent debug output is enabled."""

    if "agent_debug" in options:
        return bool(options.get("agent_debug"))
    raw = os.environ.get("WF_AGENT_DEBUG", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _write_agent_debug_artifact(
    out_dir: Path,
    actor_name: str,
    label: str,
    content: Any,
    *,
    ext: str = ".json",
) -> None:
    """Write an agent debug artifact file."""

    try:
        debug_dir = out_dir / "agent-debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    filename = f"{actor_name}__{label}{ext}"
    filepath = debug_dir / filename
    if ext == ".json":
        filepath.write_text(json.dumps(content, indent=2, default=str))
    else:
        filepath.write_text(str(content))
