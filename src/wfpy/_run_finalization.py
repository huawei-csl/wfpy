"""Internal helpers for run finalization (overlays, artifacts, run record)."""

from __future__ import annotations

import dataclasses
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wfpy._plan_outputs_runtime import _serialize_non_file_outputs, _serialize_value
from wfpy._run_event_stream import RunEventStream
from wfpy._run_artifacts import (
    _AgentContextWriter,
    _build_agent_context_overlay,
    _build_context_overlay,
    _build_viewer_overlay_v1,
    _ContextLiveWriter,
    _plan_has_agents,
    _runtime_json_dumps,
    _runtime_json_dumps_line,
    _rewrite_viewer_overlay_tokens,
    _ViewerOverlayBase,
    _ViewerOverlayWriter,
)

logger = logging.getLogger("wfpy")


@dataclasses.dataclass(frozen=True)
class RunWriters:
    """Container for run artifact writers."""

    overlay_writer: _ViewerOverlayWriter
    agent_ctx_writer: _AgentContextWriter | None
    context_live_writer: _ContextLiveWriter | None
    overlay_base: _ViewerOverlayBase
    event_stream: RunEventStream | None = None


def _default_run_id() -> str:
    """Generate a run ID matching the TS runtime format.

    Format: ``YYYY-MM-DDTHH-MM-SS-mmmZ_<8hex>``
    """
    ts = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-")
        + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )
    suffix = uuid.uuid4().hex[:8]
    return f"{ts}_{suffix}"


def _sanitize_run_id(run_id: str) -> str:
    """Replace characters unsafe for directory names."""
    import re

    return re.sub(r"[^a-zA-Z0-9_\-]", "-", run_id)


def _format_bytes(n: int) -> str:
    """Human-readable byte size (matches TS ``formatBytes``)."""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    else:
        return f"{n / (1024 * 1024):.1f} MB"


def _setup_run_writers(
    plan: Any,
    rid: str,
    wf_def: Any,
    run_out_dir: Path,
    base_out_dir: Path,
    started_at: datetime,
) -> RunWriters:
    """Create overlay, agent-context, and context-live writers.

    Returns:
        RunWriters container with all writer instances.
    """
    overlay_base = _ViewerOverlayBase(
        run_id=rid,
        workflow_name=wf_def.name or "",
        source_path=plan.source_path,
        started_at=started_at.isoformat(),
        out_dir=str(run_out_dir),
    )
    live_overlay_path = base_out_dir / "run.wf-viewer.live.json"
    overlay_writer = _ViewerOverlayWriter(plan, overlay_base, live_overlay_path)
    plan.overlay_writer = overlay_writer
    overlay_writer.mark_running()

    has_agents = _plan_has_agents(plan)
    agent_ctx_writer: _AgentContextWriter | None = None
    live_agent_ctx_path = base_out_dir / "run.wf-agent-context.live.json"
    event_stream: RunEventStream | None = None
    if has_agents:
        agent_ctx_writer = _AgentContextWriter(
            plan,
            rid,
            wf_def.name or "",
            plan.source_path,
            live_agent_ctx_path,
        )
        plan.agent_context_writer = agent_ctx_writer

        # Live SSE stream of agent message deltas for read-only observers (the IDE).
        # Publish its port in a discovery file next to the other live files; the
        # observer reads the port once and streams. Best-effort: if the server can't
        # start, the run proceeds unaffected (the live files remain the fallback).
        try:
            event_stream = RunEventStream(run_id=rid)
            port = event_stream.start()
            stream_path = base_out_dir / "run.wf-stream.live.json"
            stream_path.write_text(_runtime_json_dumps({
                "version": 1,
                "runId": rid,
                "workflowName": wf_def.name or "",
                "sourcePath": plan.source_path,
                "host": "127.0.0.1",
                "port": port,
                # Per-run bearer token required on every request to the stream. The file
                # lives in the run output dir (same trust boundary as the run itself).
                "token": event_stream.token,
                "eventsUrl": f"http://127.0.0.1:{port}/events",
            }))
            plan.event_stream = event_stream
            event_stream.publish({"type": "run.started", "runId": rid, "workflowName": wf_def.name or ""})
        except Exception as exc:  # never let observation break the run
            logger.warning("Run event stream unavailable: %s", exc)
            event_stream = None
            plan.event_stream = None

    context_live_writer: _ContextLiveWriter | None = None
    live_context_path = base_out_dir / "run.wf-context.live.json"
    if str(plan.context_config.get("mode", "scoped")).lower() != "off":
        context_live_writer = _ContextLiveWriter(
            plan,
            rid,
            wf_def.name or "",
            plan.source_path,
            live_context_path,
        )
        plan.context_live_writer = context_live_writer
        context_live_writer.write()

    return RunWriters(
        overlay_writer=overlay_writer,
        agent_ctx_writer=agent_ctx_writer,
        context_live_writer=context_live_writer,
        overlay_base=overlay_base,
        event_stream=event_stream,
    )


def _persist_run_record(
    plan: Any,
    rid: str,
    wf_def: Any,
    run_out_dir: Path,
    base_out_dir: Path,
    started_at: datetime,
    finished_at: datetime,
    outputs: dict[str, Any],
    *,
    inputs: dict[str, Any] | None = None,
    has_external: bool = False,
    wdir: Path | None = None,
    error: dict[str, str] | None = None,
) -> None:
    """Write run.wf-run.json and append to run-log.jsonl."""
    run_record = {
        "runId": rid,
        "workflowName": wf_def.name,
        "sourcePath": plan.source_path,
        "cwd": os.getcwd(),
        "startedAt": started_at.isoformat(),
        "finishedAt": finished_at.isoformat(),
        "outDir": str(run_out_dir),
        "baseOutDir": str(base_out_dir),
        "hasExternal": has_external,
        "workDir": str(wdir) if wdir else None,
        "keptWorkDir": True,
        "inputs": {k: _serialize_value(v) for k, v in (inputs or {}).items()},
        "outputs": {k: _serialize_value(v) for k, v in outputs.items()},
        "actors": [
            {
                "instanceName": a.name,
                "entityName": type(a.instance).__name__,
                "entityKind": a.kind,
                "fireCount": a.fire_count,
                **({"chatHistory": a.chat_history} if a.chat_history else {}),
                **(
                    {"agentCliSessionIds": a.agent_cli_session_ids}
                    if a.agent_cli_session_ids
                    else {}
                ),
            }
            for a in plan.actors
        ],
        "context": {
            "mode": plan.context_config.get("mode", "scoped"),
            "contextVersion": plan.context_version,
            "journalSize": len(plan.context_journal),
            "contextStore": plan.context_store,
        },
    }
    if error is not None:
        run_record["error"] = error

    run_record_path = run_out_dir / "run.wf-run.json"
    try:
        run_record_path.write_text(_runtime_json_dumps(run_record))
    except OSError:
        pass

    log_entry = {
        "runId": rid,
        "workflowName": wf_def.name,
        "sourcePath": plan.source_path,
        "cwd": os.getcwd(),
        "startedAt": started_at.isoformat(),
        "finishedAt": finished_at.isoformat(),
        "outDir": str(run_out_dir),
        "inputs": {k: _serialize_value(v) for k, v in (inputs or {}).items()},
        "outputs": {k: _serialize_value(v) for k, v in outputs.items()},
        "hasExternal": has_external,
    }
    if error is not None:
        log_entry["error"] = error

    run_log_path = base_out_dir / "run-log.jsonl"
    try:
        with open(run_log_path, "a") as f:
            f.write(_runtime_json_dumps_line(log_entry) + "\n")
    except OSError:
        pass


def _write_error_overlay(
    plan: Any,
    overlay_base: _ViewerOverlayBase,
    overlay_writer: _ViewerOverlayWriter,
    run_out_dir: Path,
    error_info: dict[str, str],
    finished_at: datetime,
) -> None:
    """Write error overlay to run.wf-viewer.json."""
    error_overlay = _build_viewer_overlay_v1(
        plan,
        overlay_base,
        None,
        False,
        finished_at=finished_at.isoformat(),
        error=error_info,
    )
    try:
        viewer_path = run_out_dir / "run.wf-viewer.json"
        viewer_path.write_text(_runtime_json_dumps(error_overlay))
    except OSError:
        pass


def _finalize_run(
    plan: Any,
    rid: str,
    wf_def: Any,
    run_out_dir: Path,
    writers: RunWriters,
    outputs: dict[str, Any],
    *,
    wdir: Path | None = None,
    verbose: bool = False,
) -> None:
    """Write final overlays, queue trace, and run record on success."""
    # Serialize non-File outputs as JSON files in runOutDir (matching TS runtime)
    _serialize_non_file_outputs(plan, outputs, run_out_dir)

    finished_at = datetime.now(timezone.utc)

    # Rewrite edge tokens if intermediates were kept
    if wdir and wdir.is_dir():
        _rewrite_viewer_overlay_tokens(plan, str(wdir), str(wdir))
    final_overlay = _build_viewer_overlay_v1(
        plan,
        writers.overlay_base,
        writers.overlay_writer.active_entries or None,  # static green glow on last actors
        False,
        finished_at=finished_at.isoformat(),
    )
    viewer_final_path = run_out_dir / "run.wf-viewer.json"
    try:
        viewer_final_path.write_text(_runtime_json_dumps(final_overlay))
    except OSError:
        pass

    # Final agent context overlay
    if _plan_has_agents(plan):
        agent_ctx = _build_agent_context_overlay(
            plan,
            rid,
            wf_def.name or "",
            plan.source_path,
        )
        agent_ctx_path = run_out_dir / "run.wf-agent-context.json"
        try:
            agent_ctx_path.write_text(_runtime_json_dumps(agent_ctx))
        except OSError:
            pass

    if str(plan.context_config.get("mode", "scoped")).lower() != "off":
        context_overlay = _build_context_overlay(
            plan,
            rid,
            wf_def.name or "",
            plan.source_path,
        )
        context_path = run_out_dir / "run.wf-context.json"
        context_journal_path = run_out_dir / "run.wf-context-journal.jsonl"
        try:
            context_path.write_text(_runtime_json_dumps(context_overlay))
        except OSError:
            pass
        try:
            with context_journal_path.open("w", encoding="utf-8") as jf:
                for event in plan.context_journal:
                    jf.write(_runtime_json_dumps_line(event) + "\n")
        except OSError:
            pass

    # Write queue trace (run.wf-queues.json)
    if plan.queue_trace and plan.queue_trace.steps:
        trace_json = plan.queue_trace.build(finished_at.isoformat(), plan)
        qt_path = run_out_dir / "run.wf-queues.json"
        qt_path.write_text(_runtime_json_dumps(trace_json))
        if verbose:
            logger.info("Queue trace: %d steps → %s", len(plan.queue_trace.steps), qt_path)

    if verbose:
        logger.info("Run %s finished. Output: %s", rid, run_out_dir)
