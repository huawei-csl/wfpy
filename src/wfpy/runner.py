"""wfpy.runner — FIFO round-robin dataflow scheduler.

Implements the same execution semantics as the wf-lang TypeScript runtime:
- Unbounded FIFO queues on every connection
- Round-robin actor visiting in declaration order
- First-match action selection with guards
- Quiescence-based termination
- Nested workflow recursive execution
- Temporary workDir for intermediate outputs (cleaned up after run)
- Run-ID based output directory structure (``baseOutDir/<runId>/``)
- Run log (``run-log.jsonl``) and run record (``run.wf-run.json``)
"""

from __future__ import annotations

import collections
import concurrent.futures
import copy
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import types
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import wfpy._agent_cli_runtime as _agent_cli_runtime
import wfpy._agent_io_runtime as _agent_io_runtime
import wfpy._agent_prompt_runtime as _agent_prompt_runtime
import wfpy._agent_request_runtime as _agent_request_runtime
import wfpy._agent_tools_runtime as _agent_tools_runtime
import wfpy._agent_validation_runtime as _agent_val_runtime
import wfpy._context_runtime as _ctx_runtime
import wfpy._plan_outputs_runtime as _plan_outputs_runtime
import wfpy._run_artifacts as _art_runtime
import wfpy._validation_runtime as _val_runtime
import wfpy._agent_staging_runtime as _staging_runtime
import wfpy._step_external_runtime as _ext_runtime
from wfpy._step_streamblocks_runtime import _step_streamblocks_instance
import wfpy._action_runtime as _action_runtime
import wfpy._invoke_runtime as _invoke_runtime
import wfpy._run_finalization as _run_finalization
from wfpy.core import (
    ActionDef,
    AgentSpec,
    TaskMeta,
    WorkflowDef,
    _active_wf_builder_depth,
    _active_wf_config,
    _WorkflowEnvConfig,
)
from wfpy.graph import (
    ControlNodeRecord,
    ControlPortInstance,
    ScopeRecord,
    WorkflowGraph,
)
from wfpy.types import File, PortDescriptor, PortInstance, Resource, infer_resource_kind

__all__ = ["run", "build_plan", "execute_plan", "FifoPlan"]

logger = logging.getLogger("wfpy")

MAX_STEPS = 100_000

# ── Re-exports from extracted runtime modules ─────────────────────────
# Only symbols actually used within runner.py function bodies are aliased here.
# Tests that need other symbols should import them from the source module.

_ActionContextFacade = _ctx_runtime._ActionContextFacade
_action_accepts_ctx = _ctx_runtime._action_accepts_ctx
_actor_context_policy = _ctx_runtime._actor_context_policy
_apply_context_patch = _ctx_runtime._apply_context_patch
_build_context_view = _ctx_runtime._build_context_view
_merge_dict_deep = _ctx_runtime._merge_dict_deep

ValidationResult = _val_runtime.ValidationResult
LoadedValidatorRegistry = _val_runtime.LoadedValidatorRegistry
load_validator_registry = _val_runtime.load_validator_registry
_get_validator_ids_from_port = _val_runtime._get_validator_ids_from_port
_execute_validator_spec = _val_runtime._execute_validator_spec
run_port_validators = _val_runtime.run_port_validators

_runtime_json_dumps = _art_runtime._runtime_json_dumps
_runtime_json_dumps_line = _art_runtime._runtime_json_dumps_line
_record_edge_token = _art_runtime._record_edge_token
_ViewerOverlayBase = _art_runtime._ViewerOverlayBase
_build_viewer_overlay_v1 = _art_runtime._build_viewer_overlay_v1
_ViewerOverlayWriter = _art_runtime._ViewerOverlayWriter
_plan_has_agents = _art_runtime._plan_has_agents
_build_agent_context_overlay = _art_runtime._build_agent_context_overlay
_AgentContextWriter = _art_runtime._AgentContextWriter
_build_context_overlay = _art_runtime._build_context_overlay
_ContextLiveWriter = _art_runtime._ContextLiveWriter
_rewrite_viewer_overlay_tokens = _art_runtime._rewrite_viewer_overlay_tokens
QueueTraceCollector = _art_runtime.QueueTraceCollector
_agent_debug_enabled = _art_runtime._agent_debug_enabled
_write_agent_debug_artifact = _art_runtime._write_agent_debug_artifact

_is_resource_type = _agent_val_runtime._is_resource_type
_get_effective_lsp_configs = _agent_val_runtime._get_effective_lsp_configs
_max_validator_attempts = _agent_val_runtime._max_validator_attempts
_run_agent_output_validators = _agent_val_runtime._run_agent_output_validators
_run_lsp_validation = _agent_val_runtime._run_lsp_validation
_build_validation_repair_prompt = _agent_val_runtime._build_validation_repair_prompt

AgentToolResult = _agent_tools_runtime.AgentToolResult
AgentToolSpec = _agent_tools_runtime.AgentToolSpec
AgentToolServerSpec = _agent_tools_runtime.AgentToolServerSpec
LoadedAgentToolRegistry = _agent_tools_runtime.LoadedAgentToolRegistry
_default_agent_tool_registry = _agent_tools_runtime._default_agent_tool_registry
_load_agent_tool_registry = _agent_tools_runtime._load_agent_tool_registry
_dispatch_mcp_tool = _agent_tools_runtime._dispatch_mcp_tool
_normalize_tool_auth_mode = _agent_tools_runtime._normalize_tool_auth_mode
_authorize_tool_call = _agent_tools_runtime._authorize_tool_call
_run_python_tool = _agent_tools_runtime._run_python_tool
_sanitize_tool_name = _agent_tools_runtime._sanitize_tool_name
_extract_tool_calls = _agent_tools_runtime._extract_tool_calls
_build_tool_result_messages = _agent_tools_runtime._build_tool_result_messages
_extract_assistant_message = _agent_tools_runtime._extract_assistant_message
_accumulate_stream_response = _agent_tools_runtime._accumulate_stream_response
_PROVIDER_DEFAULTS = _agent_tools_runtime._PROVIDER_DEFAULTS
_resolve_api_key = _agent_tools_runtime._resolve_api_key
_parse_retry_after_ms = _agent_tools_runtime._parse_retry_after_ms

_effective_max_tool_rounds = _agent_prompt_runtime._effective_max_tool_rounds
_run_skill_hook = _agent_prompt_runtime._run_skill_hook
_skill_hook_name = _agent_prompt_runtime._skill_hook_name
_build_effective_agent_prompt = _agent_prompt_runtime._build_effective_agent_prompt

ChatMessage = _agent_io_runtime.ChatMessage
trim_chat_history = _agent_io_runtime.trim_chat_history
summarize_chat_history = _agent_io_runtime.summarize_chat_history
restore_chat_histories = _agent_io_runtime.restore_chat_histories
_read_agent_config = _agent_io_runtime._read_agent_config
_build_agent_runtime_instruction = _agent_io_runtime._build_agent_runtime_instruction
_normalize_agent_response_text = _agent_io_runtime._normalize_agent_response_text
_parse_agent_outputs = _agent_io_runtime._parse_agent_outputs
_single_output_plain_text_fallback_allowed = _agent_io_runtime._single_output_plain_text_fallback_allowed
_extract_agent_context_patch = _agent_io_runtime._extract_agent_context_patch
_build_agent_repair_prompt = _agent_io_runtime._build_agent_repair_prompt
_print_agent_usage_summary = _agent_io_runtime._print_agent_usage_summary
_materialize_agent_file_outputs = _agent_io_runtime._materialize_agent_file_outputs
_stamp_agent_file_output_provenance = _agent_io_runtime._stamp_agent_file_output_provenance
_observed_mcp_tool_names = _agent_io_runtime._observed_mcp_tool_names

_resolve_provider_model = _agent_request_runtime._resolve_provider_model
_is_anthropic_model = _agent_request_runtime._is_anthropic_model
_build_agent_request = _agent_request_runtime._build_agent_request
_compact_prior_history_for_openrouter_retry = (
    _agent_request_runtime._compact_prior_history_for_openrouter_retry
)
_sanitize_prior_history_for_request = _agent_request_runtime._sanitize_prior_history_for_request

_normalize_agent_transport = _agent_cli_runtime._normalize_agent_transport
_normalize_cli_tools_mode = _agent_cli_runtime._normalize_cli_tools_mode
_invoke_agent_opencode_cli = _agent_cli_runtime._invoke_agent_opencode_cli
_invoke_agent_claude_cli = _agent_cli_runtime._invoke_agent_claude_cli
_invoke_agent_codex_cli = _agent_cli_runtime._invoke_agent_codex_cli

_serialize_value = _plan_outputs_runtime._serialize_value
_materialize_outputs = _plan_outputs_runtime._materialize_outputs
_serialize_non_file_outputs = _plan_outputs_runtime._serialize_non_file_outputs
_copy_file_safe = _plan_outputs_runtime._copy_file_safe
export_plan_json = _plan_outputs_runtime.export_plan_json

_directory_listing = _staging_runtime._directory_listing
_resource_meta = _staging_runtime._resource_meta
_build_file_input_meta = _staging_runtime._build_file_input_meta
_build_input_collections = _staging_runtime._build_input_collections
_resolve_cli_staged_dir = _staging_runtime._resolve_cli_staged_dir
_stage_cli_inputs = _staging_runtime._stage_cli_inputs
_dequeue_agent_inputs = _staging_runtime._dequeue_agent_inputs
AGENT_FILE_INPUT_MAX_CHARS = _staging_runtime.AGENT_FILE_INPUT_MAX_CHARS
CLI_AGENT_STAGED_INPUT_DIRNAME = _staging_runtime.CLI_AGENT_STAGED_INPUT_DIRNAME

_step_external = _ext_runtime._step_external
_substitute_tool_placeholders = _ext_runtime._substitute_tool_placeholders

_ordered_internal_actions = _action_runtime._ordered_internal_actions
_try_fire_action = _action_runtime._try_fire_action

# Agent invoke subsystem (moved to _invoke_runtime.py; re-exported for test monkeypatching)
_invoke_agent = _invoke_runtime._invoke_agent
_record_agent_chat_history = _invoke_runtime._record_agent_chat_history
_agent_retry_backoff_seconds = _invoke_runtime._agent_retry_backoff_seconds
_is_transient_agent_error = _invoke_runtime._is_transient_agent_error
AGENT_MAX_TOOL_ROUNDS = _invoke_runtime.AGENT_MAX_TOOL_ROUNDS
AGENT_RETRY_BASE_DELAY_MS = _invoke_runtime.AGENT_RETRY_BASE_DELAY_MS
AGENT_MAX_INTERACTIVE_BACKOFF_MS = _invoke_runtime.AGENT_MAX_INTERACTIVE_BACKOFF_MS
AGENT_MAX_RETRIES = _invoke_runtime.AGENT_MAX_RETRIES
AGENT_FAIL_FAST_RETRY_AFTER_MS = _invoke_runtime.AGENT_FAIL_FAST_RETRY_AFTER_MS
AGENT_MAX_OUTPUT_REPAIR_ATTEMPTS = _invoke_runtime.AGENT_MAX_OUTPUT_REPAIR_ATTEMPTS
AGENT_MAX_REPAIR_TOOL_ROUNDS = _invoke_runtime.AGENT_MAX_REPAIR_TOOL_ROUNDS
CLI_AGENT_PROMPT_MAX_CHARS = _invoke_runtime.CLI_AGENT_PROMPT_MAX_CHARS

# Minimal effective prompt used for CLI session repair — the session already
# has the full skill/instructions loaded from the initial invocation.
AGENT_REPAIR_EFFECTIVE_PROMPT = (
    "You are fixing an error in your previous output. "
    "Use the conversation context and error details below. "
    "Return the corrected content in the same structured format as before."
)

# Empty output detection & reprompting
EMPTY_OUTPUT_REPROMPT_MSG = (
    "Your C++ output file is empty or nearly empty — it does not contain valid source code. "
    "You did not follow the guidance provided. "
    "Please generate the complete, correct source file and write it to the output."
)
MAX_EMPTY_OUTPUT_REPROMPTS = 6

# Run finalization helpers (moved to _run_finalization.py)
_default_run_id = _run_finalization._default_run_id
_sanitize_run_id = _run_finalization._sanitize_run_id
_format_bytes = _run_finalization._format_bytes

# ═══════════════════════════════════════════════════════════════════════════


def _attach_agent_response_meta(
    messages: list[ChatMessage], response_debug_meta: dict[str, Any]
) -> None:
    """Attach non-content agent metadata to the last assistant message."""

    if not messages or not response_debug_meta:
        return
    for message in reversed(messages):
        if str(message.get("role", "")).strip().lower() != "assistant":
            continue
        thinking = response_debug_meta.get("thinking")
        if isinstance(thinking, str) and thinking.strip():
            message["thinking"] = thinking
        reply_time_ms = response_debug_meta.get("replyTimeMs")
        if isinstance(reply_time_ms, (int, float)):
            message["replyTimeMs"] = int(reply_time_ms)
        opencode_session_id = response_debug_meta.get("opencodeSessionID")
        if isinstance(opencode_session_id, str) and opencode_session_id.strip():
            message["opencodeSessionID"] = opencode_session_id.strip()
        return


# ═══════════════════════════════════════════════════════════════════════════
# Queue
# ═══════════════════════════════════════════════════════════════════════════


class Queue:
    """Unbounded FIFO token queue on one connection edge.  Thread-safe."""

    __slots__ = (
        "id",
        "items",
        "from_actor",
        "from_port",
        "to_actor",
        "to_port",
        "overlay_ast_path",
        "_lock",
    )

    def __init__(
        self,
        queue_id: str,
        from_actor: str = "",
        from_port: str = "",
        to_actor: str = "",
        to_port: str = "",
        overlay_ast_path: str | None = None,
    ) -> None:
        self.id = queue_id
        self.items: collections.deque[Any] = collections.deque()
        self.from_actor = from_actor
        self.from_port = from_port
        self.to_actor = to_actor
        self.to_port = to_port
        self.overlay_ast_path = overlay_ast_path
        self._lock = threading.Lock()

    def enqueue(self, value: Any) -> None:
        with self._lock:
            self.items.append(value)

    def dequeue(self) -> Any:
        with self._lock:
            return self.items.popleft()

    def peek(self, n: int = 1) -> list[Any]:
        """Return up to *n* items without removing them."""
        with self._lock:
            return list(self.items)[:n]

    def size(self) -> int:
        with self._lock:
            return len(self.items)

    def clear(self) -> None:
        with self._lock:
            self.items.clear()

    def try_dequeue(self, n: int = 1) -> list[Any] | None:
        """Atomically check availability and dequeue *n* items.

        Returns the dequeued items, or ``None`` if insufficient tokens.
        """
        with self._lock:
            if len(self.items) < n:
                return None
            return [self.items.popleft() for _ in range(n)]

    def __repr__(self) -> str:
        return f"Queue({self.id!r}, len={self.size()})"


# ═══════════════════════════════════════════════════════════════════════════
# Actor wrappers (runtime representation)
# ═══════════════════════════════════════════════════════════════════════════


class RuntimeActor:
    """Runtime wrapper around a task / workflow instance."""

    def __init__(
        self,
        name: str,
        instance: Any,
        meta: Any,
        kind: str,
        scope_id: str = "scope:root",
        control: ControlNodeRecord | None = None,
    ) -> None:
        self.name = name
        self.instance = instance
        self.meta = meta
        self.kind = kind  # "internal" | "external" | "agent" | "viewer" | "workflow"
        self.fire_count = 0
        self.scope_id = scope_id
        self.control = control

        # Control node runtime state
        self._control_initialized: bool = False
        self._control_running: bool = False

        # port_name → list[Queue]  (fan-out: one output port may feed multiple queues)
        self.in_queues: dict[str, list[Queue]] = {}
        self.out_queues: dict[str, list[Queue]] = {}

        # For workflow actors
        self.sub_plan: FifoPlan | None = None

        # Conversation history used for UI/debug overlays; only stateful agents
        # feed it back into subsequent requests.
        self.chat_history: list[dict[str, str]] = []

        # Transport session IDs for CLI backends with persistent server-side
        # conversations (for example, opencode --session).
        self.agent_cli_session_ids: dict[str, str] = {}

        # For agent actors with fireable_without_input budget
        self.agent_fire_budget: int | None = None
        if kind == "agent":
            agent_spec = getattr(meta, "agent_spec", None)
            if agent_spec is not None:
                self.agent_fire_budget = max(0, int(agent_spec.fireable_without_input))


# ═══════════════════════════════════════════════════════════════════════════
# FifoPlan — the master runtime structure
# ═══════════════════════════════════════════════════════════════════════════


class FifoPlan:
    """The runtime execution plan for one workflow invocation.

    Contains the ordered list of actors, all queues, and workflow-level
    I/O mappings.
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.actors: list[RuntimeActor] = []
        self.all_queues: list[Queue] = []
        self.scopes: dict[str, ScopeRecord] = {}
        self.control_nodes: dict[str, ControlNodeRecord] = {}

        # Workflow-level I/O
        # wf input port name → list of queues that receive external tokens
        self.wf_input_queues: dict[str, list[Queue]] = {}
        # wf output port name → list of queues that collect output tokens
        self.wf_output_queues: dict[str, list[Queue]] = {}
        # wf output port descriptors (for materialization)
        self.wf_output_ports: dict[str, PortDescriptor] = {}
        # Stable external resource/file paths that should keep provenance when
        # later internal tasks simply re-emit them.
        self.wf_input_resource_paths: set[str] = set()

        # Extra directories to search for external tool executables
        self.search_paths: list[str] = []
        # Extra environment variables for external tool subprocesses
        self.env: dict[str, str] = {}

        # Workflow source file path (for CWD resolution)
        self.source_path: str = ""

        # Temporary working directory for intermediate outputs
        self.work_dir: str = ""

        # Runtime options (tool-auth mode, streaming, etc.)
        self.options: dict[str, Any] = {}

        # Maximum worker threads for parallel execution (None → executor default)
        self.max_workers: int | None = None

        # Validator registry (loaded before plan execution)
        self.validator_registry: LoadedValidatorRegistry | None = None

        # Queue trace collector (optional)
        self.queue_trace: QueueTraceCollector | None = None

        # Edge tracking for viewer overlay
        # overlay_ast_path → {queueId, fromEntity, toEntity, outPort, inPort}
        self.edge_info_by_ast_path: dict[str, dict[str, str]] = {}
        # overlay_ast_path → last value that traversed the edge
        self.edge_last_token_by_ast_path: dict[str, Any] = {}
        # queue_id → {queueId, fromEntity, toEntity, outPort, inPort}
        self.edge_info_by_queue_id: dict[str, dict[str, str]] = {}
        # queue_id → last value that traversed the edge
        self.edge_last_token_by_queue_id: dict[str, Any] = {}

        # Viewer overlay writer (set by run(), None in standalone execute_plan)
        self.overlay_writer: _ViewerOverlayWriter | None = None
        # Workflow-instance path prefix used by the shared overlay writer.
        self.overlay_path_prefix: list[str] = []
        # Agent context writer (set by run())
        self.agent_context_writer: _AgentContextWriter | None = None
        # Live SSE event stream for read-only observers (set by run()).
        self.event_stream: Any | None = None

        # Shared lock for concurrent writer access (overlay, agent ctx, queue trace)
        self._parallel_lock = threading.Lock()

        # Scheduler wakeups let nested workflows stream outputs to their parent
        # while the nested workflow actor is still running.
        self._scheduler_condition = threading.Condition()
        self._scheduler_wake_seq = 0
        self._scheduler_progress_seq = 0
        self.workflow_output_handler: Callable[[str, Any], None] | None = None
        self._streamed_output_counts: dict[str, int] = {}

        # Shared context primitives
        self.context_store: dict[str, Any] = {}
        self.context_journal: list[dict[str, Any]] = []
        self.context_version: int = 0
        self.context_commit_seq: int = 0
        self._context_lock = threading.Lock()
        self.context_config: dict[str, Any] = {
            "mode": "scoped",
            "budget": 1200,
            "summarize": True,
            "resume_from": None,
        }
        # Context live writer (set by run())
        self.context_live_writer: _ContextLiveWriter | None = None

    def notify_scheduler(self, *, progress: bool = False) -> None:
        """Wake the scheduler after completion or externally-visible queue progress."""

        with self._scheduler_condition:
            self._scheduler_wake_seq += 1
            if progress:
                self._scheduler_progress_seq += 1
            self._scheduler_condition.notify_all()


# ═══════════════════════════════════════════════════════════════════════════
# Plan builder
# ═══════════════════════════════════════════════════════════════════════════


def build_plan(
    graph: WorkflowGraph,
    wf_def: WorkflowDef | None = None,
) -> FifoPlan:
    """Convert a WorkflowGraph into a FifoPlan ready for execution."""
    plan = FifoPlan(name=graph.name)

    plan.scopes = dict(graph.scopes)
    plan.control_nodes = dict(graph.control_nodes)

    # 1. Create RuntimeActors from graph actors (preserving declaration order)
    actor_map: dict[str, RuntimeActor] = {}
    for entry_kind, entry_id in graph._creation_order:
        if entry_kind == "actor":
            rec = graph.actors[entry_id]
            meta = rec.meta
            if isinstance(meta, TaskMeta):
                kind = meta.kind
            elif isinstance(meta, WorkflowDef):
                kind = "workflow"
            else:
                kind = "internal"

            ra = RuntimeActor(
                name=rec.instance_name,
                instance=rec.instance,
                meta=meta,
                kind=kind,
                scope_id=rec.scope_id,
            )
            actor_map[rec.instance_name] = ra
            plan.actors.append(ra)
        elif entry_kind == "control":
            record = graph.control_nodes[entry_id]
            ra = RuntimeActor(
                name=record.name,
                instance=record,
                meta=record,
                kind=f"control-{record.kind}",
                scope_id=record.parent_scope_id,
                control=record,
            )
            actor_map[record.name] = ra
            plan.actors.append(ra)

    # 2. Wire connections → create queues
    for conn in graph.connections:
        src = conn.from_port
        tgt = conn.to_port

        # Resolve port names and actor names
        if isinstance(src, str):
            src_actor_name = ""
            src_port_name = src
        elif isinstance(src, ControlPortInstance):
            src_actor_name = src.control_name
            src_port_name = src.port_name
        elif isinstance(src, PortInstance):
            src_actor_name = src.actor_instance._wfpy_instance_name
            src_port_name = src.port_name
        else:
            raise ValueError(f"Invalid source: {src}")

        if isinstance(tgt, str):
            tgt_actor_name = ""
            tgt_port_name = tgt
        elif isinstance(tgt, ControlPortInstance):
            tgt_actor_name = tgt.control_name
            tgt_port_name = tgt.port_name
        elif isinstance(tgt, PortInstance):
            tgt_actor_name = tgt.actor_instance._wfpy_instance_name
            tgt_port_name = tgt.port_name
        else:
            raise ValueError(f"Invalid target: {tgt}")

        queue_id = (
            f"{src_actor_name or 'WF'}.{src_port_name}-->{tgt_actor_name or 'WF'}.{tgt_port_name}"
        )
        q = Queue(
            queue_id=queue_id,
            from_actor=src_actor_name,
            from_port=src_port_name,
            to_actor=tgt_actor_name,
            to_port=tgt_port_name,
        )
        plan.all_queues.append(q)
        qi: dict[str, str] = {
            "queueId": queue_id,
            "outPort": src_port_name,
            "inPort": tgt_port_name,
        }
        if src_actor_name:
            qi["fromEntity"] = src_actor_name
        if tgt_actor_name:
            qi["toEntity"] = tgt_actor_name
        plan.edge_info_by_queue_id[queue_id] = qi

        # Wire to source actor's out_queues (or workflow input)
        if src_actor_name and src_actor_name in actor_map:
            ra = actor_map[src_actor_name]
            ra.out_queues.setdefault(src_port_name, []).append(q)
        else:
            # Workflow-level input
            plan.wf_input_queues.setdefault(src_port_name, []).append(q)

        # Wire to target actor's in_queues (or workflow output)
        if tgt_actor_name and tgt_actor_name in actor_map:
            ra = actor_map[tgt_actor_name]
            ra.in_queues.setdefault(tgt_port_name, []).append(q)
        else:
            # Workflow-level output
            plan.wf_output_queues.setdefault(tgt_port_name, []).append(q)

    # 3. Copy config (search_paths + env) from the workflow definition
    if wf_def is not None:
        if wf_def.search_paths:
            plan.search_paths = list(wf_def.search_paths)
        if wf_def.env:
            plan.env = dict(wf_def.env)
        # Store output port descriptors for materialization
        for attr, pd in (wf_def.ports or {}).items():
            name = pd.name or attr
            if pd.direction == "out" or name.lower() in (
                "out",
                "output",
                "result",
                "report",
                "summary",
            ):
                plan.wf_output_ports[name] = pd

    # 4. Build sub-plans for nested workflow actors
    for ra in plan.actors:
        if ra.kind == "workflow" and isinstance(ra.meta, WorkflowDef):
            sub_graph = _build_workflow_graph(ra.meta)
            sub_plan = build_plan(sub_graph, ra.meta)
            # Inherit parent search_paths / env into sub-plans
            if plan.search_paths and not sub_plan.search_paths:
                sub_plan.search_paths = list(plan.search_paths)
            if plan.env:
                merged_env = dict(plan.env)
                merged_env.update(sub_plan.env)  # child overrides parent
                sub_plan.env = merged_env
            if plan.source_path and not sub_plan.source_path:
                sub_plan.source_path = plan.source_path
            ra.sub_plan = sub_plan

    return plan


def _build_workflow_graph(wf_def: WorkflowDef) -> WorkflowGraph:
    """Execute a @workflow builder function to produce its WorkflowGraph."""
    graph = WorkflowGraph(name=wf_def.name)
    graph.factory_name = wf_def.factory_name
    if wf_def.factory_parameters:
        graph.factory_parameters = copy.deepcopy(wf_def.factory_parameters)
    with graph:
        builder_fn = wf_def.builder_fn
        if builder_fn is not None:
            locals_snapshot: dict[str, Any] | None = None
            prev = None
            sys_mod = None
            depth_token = _active_wf_builder_depth.set(_active_wf_builder_depth.get() + 1)
            try:
                import sys

                sys_mod = sys

                def _trace(frame: types.FrameType, event: str, arg: Any) -> object:
                    nonlocal locals_snapshot
                    if event == "return" and frame.f_code is builder_fn.__code__:
                        locals_snapshot = dict(frame.f_locals)
                    return _trace

                prev = sys_mod.getprofile()
                sys_mod.setprofile(_trace)
                builder_fn()
            finally:
                try:
                    if sys_mod is not None:
                        sys_mod.setprofile(prev)
                except Exception:
                    pass
                _active_wf_builder_depth.reset(depth_token)
            if locals_snapshot:
                for name, val in locals_snapshot.items():
                    if not name or name.startswith("_"):
                        continue
                    try:
                        if hasattr(val, "_wfpy_meta") or hasattr(val, "_wfpy_workflow"):
                            graph.rename_actor_instance(val, name)
                    except Exception:
                        continue
        elif wf_def.cls is not None:
            # Class-based: look for connections attribute or a build() method
            connections = getattr(wf_def.cls, "connections", None)
            if connections is not None:
                for src_str, tgt_str in connections:
                    from wfpy.graph import connect

                    connect(src_str, tgt_str)
    return graph


# ═══════════════════════════════════════════════════════════════════════════
# ── Parallel-scheduler helpers ──────────────────────────────────────────────


def _is_actor_maybe_ready(actor: RuntimeActor) -> bool:
    """Quick check: does this actor *potentially* have enough tokens to fire?

    This is a loose (may-false-positive) check so the parallel phase doesn't
    waste thread-pool submissions on actors that definitely can't fire yet.
    The precise readiness test happens inside ``_step_actor``.
    """
    if actor.kind.startswith("control-"):
        return False

    # Agent with remaining fireable-without-input budget
    if actor.kind == "agent" and actor.agent_fire_budget is not None and actor.agent_fire_budget > 0:
        return True

    # No input ports → always fireable
    if not actor.in_queues:
        return True

    if actor.kind == "internal":
        meta = actor.meta
        if isinstance(meta, TaskMeta):
            for adef in _ordered_internal_actions(actor):
                consumes = adef.consumes
                if consumes is None:
                    continue
                if consumes == {}:
                    return True

    # At least one input queue has a token
    for queues in actor.in_queues.values():
        if queues and queues[0].size() > 0:
            return True

    return False


def _collect_ready_actors(
    plan: FifoPlan,
    active_scopes: set[str],
) -> list[RuntimeActor]:
    """Return non-control actors in *active_scopes* that look ready to fire."""
    ready: list[RuntimeActor] = []
    for actor in plan.actors:
        if actor.kind.startswith("control-"):
            continue
        if actor.scope_id not in active_scopes:
            continue
        if _is_actor_maybe_ready(actor):
            ready.append(actor)
    return ready


def _streamed_output_path(plan: FifoPlan, port_name: str, value: Any, out_dir: Path) -> Any:
    """Materialize one streamed workflow output token when it is file-like."""

    port_desc = plan.wf_output_ports.get(port_name)
    if isinstance(value, Resource):
        return value
    locator = str(value)
    resource_kind = infer_resource_kind(locator)
    is_file = resource_kind == "file"
    if port_desc and _is_resource_type(port_desc.port_type) and resource_kind not in {"folder", "url", "http"}:
        is_file = True
    if not is_file and locator and os.path.isfile(locator):
        is_file = True
    if not is_file:
        return value

    ext = port_desc.ext if port_desc and port_desc.ext else ""
    index = plan._streamed_output_counts.get(port_name, 0)
    plan._streamed_output_counts[port_name] = index + 1
    if index == 0:
        dst = out_dir / f"{port_name}{ext}"
    else:
        dst = out_dir / f"{port_name}__{index}{ext}"
    _copy_file_safe(locator, str(dst))
    return str(dst)


def _stream_workflow_outputs(plan: FifoPlan, out_dir: Path) -> bool:
    """Drain workflow-level outputs to a parent handler as soon as tokens appear."""

    handler = plan.workflow_output_handler
    if handler is None:
        return False

    streamed = False
    for port_name, queues in plan.wf_output_queues.items():
        for queue in queues:
            while True:
                tokens = queue.try_dequeue(1)
                if tokens is None:
                    break
                value = _streamed_output_path(plan, port_name, tokens[0], out_dir)
                handler(port_name, value)
                streamed = True
    return streamed


# ── Scheduler — FIFO round-robin (sequential) / dataflow-parallel ───────────
# ═══════════════════════════════════════════════════════════════════════════


def execute_plan(
    plan: FifoPlan,
    inputs: dict[str, Any] | None = None,
    *,
    max_workers: int | None = None,
    out_dir: str | Path | None = None,
    work_dir: str | Path | None = None,
    verbose: bool = False,
    report_leftovers: bool = True,
) -> dict[str, Any]:
    """Execute a FifoPlan using dataflow-parallel scheduling.

    When *max_workers* is ``None`` (the default), ready non-control actors
    execute concurrently via ``ThreadPoolExecutor`` (its default thread count,
    typically ``min(32, os.cpu_count()+4)``).  Set *max_workers=1* to restore
    the legacy sequential round-robin behaviour.  Control nodes (``if_`` /
    ``loop``) always run in a sequential phase before each parallel round.

    Returns a dict mapping workflow output port names to their final values.
    """
    if inputs:
        _seed_inputs(plan, inputs)

    if out_dir is None:
        out_dir = Path("./wf-out")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    has_external = any(a.kind == "external" for a in plan.actors)
    if work_dir is not None:
        wdir = Path(work_dir)
        wdir.mkdir(parents=True, exist_ok=True)
    elif has_external:
        wdir = out_dir / "work"
        wdir.mkdir(parents=True, exist_ok=True)
    else:
        wdir = out_dir
    plan.work_dir = str(wdir)
    plan.max_workers = max_workers

    steps = 0
    active_scopes: set[str] = {"scope:root"}
    worker_limit = max_workers if max_workers is not None else min(32, (os.cpu_count() or 1) + 4)

    # ── Build the parallel executor (None → ThreadPoolExecutor default) ──
    executor_ctx = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

    try:
        with executor_ctx as executor:
            in_flight: dict[concurrent.futures.Future[Any], RuntimeActor] = {}
            running_actors: set[RuntimeActor] = set()
            blocked_until_progress: dict[RuntimeActor, int] = {}

            def _record_actor_fire(actor: RuntimeActor) -> None:
                nonlocal steps
                steps += 1
                if plan.queue_trace:
                    plan.queue_trace.record_fire(plan, actor)
                if verbose:
                    logger.info(
                        "Step %d: %s fired (total fires: %d)",
                        steps,
                        actor.name,
                        actor.fire_count,
                    )
                if steps > MAX_STEPS:
                    raise RuntimeError(
                        f"Exceeded {MAX_STEPS} steps — possible infinite loop. "
                        f"Last actor: {actor.name}"
                    )

            def _run_control_phase() -> bool:
                control_fired = False
                for actor in plan.actors:
                    if not actor.kind.startswith("control-"):
                        continue
                    if plan.overlay_writer:
                        plan.overlay_writer.set_active_with_prefix(
                            actor, plan.overlay_path_prefix
                        )
                    fired = _step_actor(actor, plan, wdir, verbose, active_scopes)
                    if fired:
                        if plan.overlay_writer:
                            plan.overlay_writer.remove_active_with_prefix(
                                actor, plan.overlay_path_prefix
                            )
                        if plan.agent_context_writer and actor.kind in ("agent", "internal"):
                            plan.agent_context_writer.write()
                        control_fired = True
                        _record_actor_fire(actor)
                        plan.notify_scheduler(progress=True)
                    else:
                        # Clear the false-positive "maybe-ready" from the live viewer.
                        if plan.overlay_writer:
                            plan.overlay_writer.remove_active_with_prefix(
                                actor, plan.overlay_path_prefix
                            )
                return control_fired

            def _fire_one(actor: RuntimeActor) -> bool:
                """Fire *actor* (runs in thread-pool thread)."""

                # Mark actor active in viewer overlay (best-effort, cosmetic)
                if plan.overlay_writer:
                    plan.overlay_writer.set_active_with_prefix(
                        actor, plan.overlay_path_prefix
                    )
                fired = _step_actor(actor, plan, wdir, verbose, set(active_scopes))
                if fired:
                    with plan._parallel_lock:
                        if plan.overlay_writer:
                            plan.overlay_writer.remove_active_with_prefix(
                                actor, plan.overlay_path_prefix
                            )
                        if plan.agent_context_writer and actor.kind in ("agent", "internal"):
                            plan.agent_context_writer.write()
                        _record_actor_fire(actor)
                    plan.notify_scheduler(progress=True)
                    return True

                # Clear the false-positive "maybe-ready" from the live viewer.
                if plan.overlay_writer:
                    plan.overlay_writer.remove_active_with_prefix(
                        actor, plan.overlay_path_prefix
                    )
                return False

            def _wake_when_done(_future: concurrent.futures.Future[Any]) -> None:
                plan.notify_scheduler()

            def _submit_ready_actors() -> bool:
                submitted = False
                for actor in _collect_ready_actors(plan, active_scopes):
                    if len(in_flight) >= worker_limit:
                        break
                    if actor in running_actors:
                        continue
                    if blocked_until_progress.get(actor) == plan._scheduler_progress_seq:
                        continue
                    future = executor.submit(_fire_one, actor)
                    in_flight[future] = actor
                    running_actors.add(actor)
                    future.add_done_callback(_wake_when_done)
                    submitted = True
                return submitted

            while True:
                completed = [future for future in list(in_flight) if future.done()]
                for future in completed:
                    actor = in_flight.pop(future)
                    running_actors.discard(actor)
                    fired = future.result()  # propagate any exception
                    if fired:
                        blocked_until_progress.pop(actor, None)
                    else:
                        blocked_until_progress[actor] = plan._scheduler_progress_seq

                streamed = _stream_workflow_outputs(plan, out_dir)

                control_fired = False
                if not in_flight:
                    control_fired = _run_control_phase()
                    if control_fired:
                        streamed = _stream_workflow_outputs(plan, out_dir) or streamed

                submitted = _submit_ready_actors()

                if not in_flight:
                    if not control_fired and not submitted and not streamed:
                        break  # quiescence
                    continue

                with plan._scheduler_condition:
                    wake_seq = plan._scheduler_wake_seq
                    if not any(future.done() for future in in_flight):
                        plan._scheduler_condition.wait_for(
                            lambda: plan._scheduler_wake_seq != wake_seq
                            or any(future.done() for future in in_flight),
                            timeout=0.1,
                        )
    except BaseException:
        raise

    if verbose:
        logger.info("Quiescence reached after %d total steps.", steps)
        if report_leftovers:
            _report_leftover_tokens(plan)

    materialized = _materialize_outputs(plan, out_dir)

    outputs: dict[str, Any] = {}
    for port_name, queues in plan.wf_output_queues.items():
        if port_name in materialized:
            outputs[port_name] = materialized[port_name]
            continue

        values: list[Any] = []
        for q in queues:
            if q.size() > 0:
                values.extend(list(q.items))
        outputs[port_name] = values if values else None

    return outputs


def _seed_inputs(plan: FifoPlan, inputs: dict[str, Any]) -> None:
    """Seed workflow-level input queues with the provided values."""
    for port_name, value in inputs.items():
        queues = plan.wf_input_queues.get(port_name)
        if queues is None:
            available = list(plan.wf_input_queues.keys())
            raise ValueError(f"Unknown workflow input port {port_name!r}. Available: {available}")
        locator = ""
        if isinstance(value, (File, Resource)):
            locator = str(value.path or "").strip()
        elif isinstance(value, str):
            locator = value.strip()
        if locator and os.path.exists(locator):
            try:
                plan.wf_input_resource_paths.add(str(Path(locator).resolve()))
            except OSError:
                plan.wf_input_resource_paths.add(locator)
        for q in queues:
            q.enqueue(value)
            _record_edge_token(plan, q, value)


def _report_leftover_tokens(plan: FifoPlan) -> None:
    """Warn about tokens left in non-output queues."""
    for q in plan.all_queues:
        if q.size() > 0 and all(q not in qs for qs in plan.wf_output_queues.values()):
            logger.warning("Leftover tokens in queue %s: %d item(s)", q.id, q.size())


# ═══════════════════════════════════════════════════════════════════════════
# Actor stepping
# ═══════════════════════════════════════════════════════════════════════════


def _step_actor(
    actor: RuntimeActor,
    plan: FifoPlan,
    out_dir: Path,
    verbose: bool,
    active_scopes: set[str] | None = None,
) -> bool:
    """Attempt to fire one action on the given actor. Returns True if fired."""
    if active_scopes is not None and actor.scope_id not in active_scopes:
        return False
    if actor.kind.startswith("control-") and actor._control_running:
        return False
    if actor.kind == "internal":
        return _step_internal(actor, out_dir, plan, verbose)
    elif actor.kind == "external":
        return _step_external(actor, out_dir, plan, verbose)
    elif actor.kind == "agent":
        return _step_agent(actor, out_dir, plan, verbose)
    elif actor.kind == "workflow":
        return _step_workflow(actor, plan, out_dir, verbose)
    elif actor.kind == "viewer":
        return _step_viewer(actor, plan, verbose)
    elif actor.kind == "streamblocks":
        # A `design` facade IS an ordinary task — it declares its own ports and
        # actions, so it fires exactly like any other. Only an `instance` does
        # something different, and what it does is run `calpy run`.
        annotation = actor.meta.annotations.get("streamblocks") or {}
        if annotation.get("facade") == "instance":
            return _step_streamblocks_instance(actor, out_dir, plan, verbose)
        return _step_internal(actor, out_dir, plan, verbose)
    elif actor.kind == "source":
        # An ordinary task: the decorator wrote the emit action, so the normal
        # firing rules apply and the guard is what stops it after one token.
        return _step_internal(actor, out_dir, plan, verbose)
    elif actor.kind == "control-if":
        return _step_control_if(actor, plan, out_dir, verbose, active_scopes)
    elif actor.kind == "control-loop":
        return _step_control_loop(actor, plan, out_dir, verbose, active_scopes)
    else:
        raise ValueError(f"Unknown actor kind: {actor.kind}")


# ── Internal (dataflow) actors ───────────────────────────────────────────


def _step_internal(actor: RuntimeActor, out_dir: Path, plan: FifoPlan, verbose: bool) -> bool:
    """Try to fire one action on an internal (dataflow) actor.

    Iterates fireable actions in definition order by default, with optional
    task-level schedule / priority metadata narrowing or reordering the
    candidate set before guards are evaluated.
    """
    task_started = time.perf_counter()
    meta: TaskMeta = actor.meta
    actions = _ordered_internal_actions(actor)

    if not actions:
        return False

    for adef in actions:
        fired, _result = _try_fire_action(actor, adef, out_dir, plan, verbose)
        if fired:
            actor.fire_count += 1
            duration_ms = int((time.perf_counter() - task_started) * 1000)
            print(
                f"[wfpy][task] {actor.name} finished: duration={duration_ms}ms",
                flush=True,
            )
            return True

    return False


# ── Internal action execution (moved to _action_runtime.py) ────────────


# ── Control nodes ─────────────────────────────────────────────────────────


def _control_scopes(plan: FifoPlan, scope_id: str) -> set[str]:
    """Return a set containing scope_id and all descendant scopes."""
    scopes = {scope_id}
    added = True
    while added:
        added = False
        for scope in plan.scopes.values():
            if scope.parent_id in scopes and scope.id not in scopes:
                scopes.add(scope.id)
                added = True
    return scopes


def _execute_scope_until_quiescence(
    plan: FifoPlan,
    out_dir: Path,
    verbose: bool,
    active_scopes: set[str],
) -> bool:
    """Run a restricted scheduler loop over active_scopes once to quiescence."""
    fired_any = False
    while True:
        fired_this_round = False
        for actor in plan.actors:
            fired = _step_actor(actor, plan, out_dir, verbose, active_scopes)
            if fired:
                fired_this_round = True
                fired_any = True
        if not fired_this_round:
            break
    return fired_any


def _step_control_if(
    actor: RuntimeActor,
    plan: FifoPlan,
    out_dir: Path,
    verbose: bool,
    active_scopes: set[str] | None,
) -> bool:
    if actor.control is None:
        return False

    # Initialize control node I/O queues on first visit
    if not actor._control_initialized:
        record = actor.control
        # cond input: any incoming queue already wired via connect()
        # out output: existing outgoing queues already wired
        actor._control_initialized = True

    # Determine if we can fire (need a condition token or static condition)
    cond_value: Any | None = None
    cond_queues = actor.in_queues.get("cond", [])
    if cond_queues and cond_queues[0].size() >= 1:
        cond_value = cond_queues[0].dequeue()
    else:
        used = getattr(actor, "_control_condition_used", False)
        if actor.control.condition is not None and not used:
            cond_value = actor.control.condition
            actor._control_condition_used = True  # type: ignore[attr-defined]

    if cond_value is None:
        return False
    cond_truthy = bool(cond_value)

    record = actor.control
    if cond_truthy:
        branch_scope = record.scopes.get("then")
    else:
        branch_scope = record.scopes.get("else")

    if branch_scope is None:
        return False

    # Execute branch subgraph to quiescence (root remains active)
    base_scopes = active_scopes or {"scope:root"}
    branch_scopes = _control_scopes(plan, branch_scope)
    actor._control_running = True
    _execute_scope_until_quiescence(
        plan,
        out_dir,
        verbose,
        base_scopes | branch_scopes,
    )
    actor._control_running = False

    # Emit a token on out to signal completion (pass through cond)
    for q in actor.out_queues.get("out", []):
        q.enqueue(cond_value)
        _record_edge_token(plan, q, cond_value)

    actor.fire_count += 1
    return True


def _step_control_loop(
    actor: RuntimeActor,
    plan: FifoPlan,
    out_dir: Path,
    verbose: bool,
    active_scopes: set[str] | None,
) -> bool:
    if actor.control is None:
        return False

    record = actor.control

    # Initialize iterator once we have an iterable token (or static iterable)
    if not actor._control_initialized:
        iter_queues = actor.in_queues.get("iter", [])
        iterable_value: Any | None = None
        if iter_queues and iter_queues[0].size() >= 1:
            iterable_value = iter_queues[0].dequeue()
        else:
            used = getattr(actor, "_control_iter_used", False)
            if record.iterable is not None and not used:
                iterable_value = record.iterable
                actor._control_iter_used = True  # type: ignore[attr-defined]

        if iterable_value is None:
            return False
        try:
            actor._loop_iter = iter(iterable_value)  # type: ignore[attr-defined]
        except TypeError:
            return False
        actor._control_initialized = True

    loop_iter = getattr(actor, "_loop_iter", None)
    if loop_iter is None:
        return False

    try:
        item = next(loop_iter)
    except StopIteration:
        # Loop finished; emit completion token
        for q in actor.out_queues.get("out", []):
            q.enqueue(None)
            _record_edge_token(plan, q, None)
        actor._control_initialized = False
        if hasattr(actor, "_loop_iter"):
            delattr(actor, "_loop_iter")
        actor.fire_count += 1
        return True

    # Enqueue item to loop.item port
    for q in actor.out_queues.get("item", []):
        q.enqueue(item)
        _record_edge_token(plan, q, item)

    # Execute loop body subgraph to quiescence (root remains active)
    body_scope = record.scopes.get("body")
    if body_scope:
        base_scopes = active_scopes or {"scope:root"}
        body_scopes = _control_scopes(plan, body_scope)
        actor._control_running = True
        _execute_scope_until_quiescence(
            plan,
            out_dir,
            verbose,
            base_scopes | body_scopes,
        )
        actor._control_running = False

    actor.fire_count += 1
    return True


# ── Validators (moved to _validation_runtime.py) ──────────────────────


# ── External (tool/subprocess) actors (moved to _step_external_runtime.py) ──


# ── Agent (LLM) actors (moved to _invoke_runtime.py) ────────────────────


def _has_empty_file_outputs(
    materialized: dict[str, Any],
    output_ports: dict[str, Any],
    min_size: int = 100,
) -> bool:
    """Check if any File output is empty or too small to be valid code."""
    for port_name, file_path in materialized.items():
        if port_name not in output_ports:
            continue
        pd = output_ports[port_name]
        if not _is_resource_type(pd.port_type):
            continue
        if file_path and isinstance(file_path, str) and os.path.exists(file_path):
            if os.path.getsize(file_path) < min_size:
                return True
    return False


def _step_agent(
    actor: RuntimeActor,
    out_dir: Path,
    plan: FifoPlan,
    verbose: bool,
) -> bool:
    """Fire an agent actor if inputs are available."""
    agent_step_started = time.perf_counter()
    meta: TaskMeta = actor.meta
    agent_spec = meta.agent_spec
    if agent_spec is None:
        return False

    debug_out_dir = Path(str(plan.options.get("agent_debug_dir") or out_dir))

    policy = _actor_context_policy(actor)
    context_view = _build_context_view(plan, actor, policy)

    # Check input availability and dequeue
    has_inputs, input_values = _dequeue_agent_inputs(actor, meta.input_ports)

    if not has_inputs:
        # Check fireable_without_input budget
        remaining_budget = actor.agent_fire_budget
        if remaining_budget is None:
            remaining_budget = max(0, int(agent_spec.fireable_without_input))
            actor.agent_fire_budget = remaining_budget
        if remaining_budget <= 0:
            return False
        actor.agent_fire_budget = remaining_budget - 1

    # Build structured JSON payload (matching TS runtime)
    file_inputs, resource_inputs, inputs_payload = _build_input_collections(
        input_values, meta.input_ports
    )

    cli_transport = _normalize_agent_transport(agent_spec)
    cli_staged_dir = _resolve_cli_staged_dir(cli_transport, actor.name, out_dir)

    if cli_staged_dir is not None:
        _stage_cli_inputs(file_inputs, resource_inputs, cli_staged_dir, meta.input_ports)

    payload: dict[str, Any] = {
        "instance": actor.name,
        "parameters": {pname: getattr(actor.instance, pname, None) for pname in meta.parameters},
        "inputs": inputs_payload,
        "contextView": context_view,
        "contextVersion": plan.context_version,
        "contextPolicy": policy,
        "outputPorts": [
            {"name": pd.name or attr, "type": str(pd.port_type)}
            for attr, pd in meta.output_ports.items()
        ],
    }
    if file_inputs:
        payload["fileInputs"] = file_inputs
    if resource_inputs:
        payload["resourceInputs"] = resource_inputs

    payload_text = json.dumps(payload, default=str)

    # ── Print agent start info (always, matching TS runtime) ─────────
    output_names = ", ".join(meta.output_ports)
    provider, model = _resolve_provider_model(agent_spec)
    print(
        f"[wfpy][agent] {actor.name} ({meta.name}) start: "
        f"provider={provider}, model={model}, "
        f"timeout={agent_spec.timeout_ms}ms, outputs=[{output_names}]"
    )
    for port_name_fi, fi_meta in file_inputs.items():
        size_str = _format_bytes(fi_meta["sizeBytes"])
        trunc = " (truncated for payload)" if fi_meta.get("truncated") else ""
        print(f"[wfpy][agent] {actor.name}.{port_name_fi}: {size_str}{trunc}")

    # Resolve prior history for stateful agents
    prior_history = actor.chat_history if agent_spec.stateful else None
    transport_mode = _normalize_agent_transport(agent_spec)
    is_opencode_cli = transport_mode in {"opencode-cli", "opencode-acp"}
    opencode_session_id = ""
    if is_opencode_cli and agent_spec.stateful:
        opencode_session_id = str(actor.agent_cli_session_ids.get("opencode") or "").strip()
    prior_history_for_request = prior_history
    if is_opencode_cli and opencode_session_id:
        prior_history_for_request = None

    effective_prompt, skill_debug_meta = _build_effective_agent_prompt(agent_spec, plan)
    claude_profile = (
        skill_debug_meta.get("claudeAgentProfile")
        if isinstance(skill_debug_meta.get("claudeAgentProfile"), dict)
        else None
    )
    max_tool_rounds = _effective_max_tool_rounds(agent_spec, claude_profile)

    skill_name = str(agent_spec.skill or "").strip()
    skill_meta = (
        skill_debug_meta.get("skillMeta")
        if isinstance(skill_debug_meta.get("skillMeta"), dict)
        else {}
    )
    pre_hook_result: dict[str, Any] | None = None
    post_hook_result: dict[str, Any] | None = None
    hook_timeout_ms = int(plan.options.get("skill_hook_timeout_ms", 30_000))
    _mcp_available_servers: list[str] = []
    _mcp_discovered_tool_names: list[str] = []
    effective_prompt_with_runtime = effective_prompt

    if (
        agent_spec.use_skill
        and agent_spec.use_skill_hooks
        and skill_name
        and isinstance(skill_meta, dict)
    ):
        pre_hook = _skill_hook_name(skill_meta, "pre")
        if pre_hook:
            pre_hook_result = _run_skill_hook(
                skill_name=skill_name,
                hook_name=pre_hook,
                phase="pre",
                actor_name=actor.name,
                payload=payload,
                response_text=None,
                plan=plan,
                timeout_ms=hook_timeout_ms,
            )
            if pre_hook_result.get("ran") and (
                pre_hook_result.get("error") or int(pre_hook_result.get("exitCode", 0)) != 0
            ):
                raise RuntimeError(
                    f"Skill pre-hook failed for '{skill_name}:{pre_hook}': "
                    f"{pre_hook_result.get('error') or pre_hook_result.get('stderr') or 'non-zero exit'}"
                )

    # Invoke LLM
    if _agent_debug_enabled(plan.options):
        _write_agent_debug_artifact(
            debug_out_dir,
            actor.name,
            f"request__{actor.fire_count}",
            {
                "payload": payload_text,
                "agentSpec": {
                    "transport": agent_spec.transport,
                    "cliToolsMode": _normalize_cli_tools_mode(agent_spec, plan.options),
                    "cliToolsModeConfigured": agent_spec.cli_tools_mode,
                    "model": agent_spec.model,
                    "prompt": agent_spec.prompt,
                    "effectivePrompt": effective_prompt,
                    "effectivePromptWithRuntime": effective_prompt_with_runtime,
                    "endpoint": agent_spec.endpoint,
                    "provider": agent_spec.provider,
                    "claudeAgent": agent_spec.claude_agent,
                    "useClaudeAgent": agent_spec.use_claude_agent,
                    "skill": agent_spec.skill,
                    "usePrompt": agent_spec.use_prompt,
                    "useSkill": agent_spec.use_skill,
                    "useSkillHooks": agent_spec.use_skill_hooks,
                    "maxToolRounds": max_tool_rounds,
                    "skillDebug": {
                        **skill_debug_meta,
                        "preHook": pre_hook_result,
                    },
                },
            },
        )

    # Load agent tool registry (if configured)
    _registry_path = plan.options.get("agent_tool_registry")
    _agent_registry: LoadedAgentToolRegistry | None = None
    if _registry_path:
        try:
            _agent_registry = _load_agent_tool_registry(_registry_path)
            if verbose:
                logger.info(
                    "Loaded agent tool registry: %s (%d tools, %d servers)",
                    _registry_path,
                    len(_agent_registry.tools),
                    len(_agent_registry.servers),
                )
        except Exception as e:
            logger.warning("Failed to load agent tool registry '%s': %s", _registry_path, e)

    # Merge per-agent inline MCP server configs into the registry
    if agent_spec.mcp_server_configs:
        if _agent_registry is None:
            _agent_registry = _default_agent_tool_registry()
        for _inline_cfg in agent_spec.mcp_server_configs:
            _agent_registry.servers[_inline_cfg.name] = AgentToolServerSpec(
                transport=_inline_cfg.transport,
                command=_inline_cfg.command,
                args=_inline_cfg.args,
                env=_inline_cfg.env,
                url=_inline_cfg.url,
            )
            if verbose:
                logger.info(
                    "Registered inline MCP server '%s' (transport=%s, url=%s)",
                    _inline_cfg.name,
                    _inline_cfg.transport,
                    _inline_cfg.url or "(none)",
                )

    # Auto-discover tools from stdio/http/streamable-http MCP servers
    if _agent_registry:
        try:
            import asyncio as _asyncio

            from wfpy._mcp_client import list_mcp_tools as _list_mcp_tools

            for _srv_name, _srv_spec in _agent_registry.servers.items():
                if _srv_spec.transport not in ("stdio", "http", "streamable-http"):
                    continue
                try:
                    _discovered = _asyncio.run(
                        _list_mcp_tools(
                            server_name=_srv_name,
                            transport=_srv_spec.transport,
                            command=_srv_spec.command,
                            args=_srv_spec.args,
                            env=_srv_spec.env or None,
                            url=_srv_spec.url,
                        )
                    )
                    _new_count = 0
                    for _dt in _discovered:
                        _tool_id = f"{_srv_name}.{_dt.name}"
                        _mcp_discovered_tool_names.append(_tool_id)
                        if _tool_id not in _agent_registry.tools:
                            _agent_registry.tools[_tool_id] = AgentToolSpec(
                                kind="mcp",
                                name=_tool_id,
                                description=_dt.description,
                                parameters=_dt.input_schema,
                                server=_srv_name,
                                tool=_dt.name,
                            )
                            _new_count += 1
                    if _discovered:
                        _mcp_available_servers.append(_srv_name)
                    if verbose and _new_count:
                        logger.info(
                            "Discovered %d tools from MCP server '%s'", _new_count, _srv_name
                        )
                except Exception as _disc_err:
                    if verbose:
                        logger.warning(
                            "Tool discovery failed for server '%s': %s", _srv_name, _disc_err
                        )
        except ImportError:
            pass  # mcp package not installed — skip discovery

    # Store in options for tool dispatch in _invoke_agent
    plan.options["_agent_tool_registry"] = _agent_registry
    _mcp_filter = agent_spec.mcp_servers

    # wfpy injects no built-in runtime instruction rules: what an agent must do
    # beyond the generic output contract is a per-workflow concern, expressed in
    # the agent's own prompt.
    effective_prompt_for_invoke = effective_prompt
    invoke_runtime_instruction_rules: list[str] = []

    total_reply_time_ms = 0

    # ── Per-agent options: let agent_spec.cli_tools_mode override global CLI setting ──
    agent_options = dict(plan.options)
    if str(agent_spec.cli_tools_mode or "").strip().lower() in ("native", "wfpy-none", "none", "disabled", "off", "on", "enabled"):
        agent_options["agent_cli_tools_mode"] = agent_spec.cli_tools_mode
    agent_cli_tools_mode = _normalize_cli_tools_mode(agent_spec, agent_options)
    if agent_cli_tools_mode == "native" and not agent_options.get("agent_cli_opencode_native_args"):
        agent_options["agent_cli_opencode_native_args"] = "--dangerously-skip-permissions"

    # Inject workflow @config env so agent subprocesses (bash/bisheng/validation scripts)
    # inherit CANN_HOME, LD_LIBRARY_PATH, etc.
    if plan.env:
        agent_options.setdefault("_wf_env", {})
        agent_options["_wf_env"] = {**agent_options["_wf_env"], **plan.env}

    # Live event sink: when a run event stream is active, agents publish their message
    # deltas / tool calls to it (tagged with this instance) for read-only observers (IDE).
    if plan.event_stream is not None:
        def _publish_agent_event(
            event: dict[str, Any],
            _stream: Any = plan.event_stream,
            _instance: str = actor.name,
        ) -> None:
            _stream.publish({"instance": _instance, **event})
        agent_options["_wf_event_publish"] = _publish_agent_event

    # Human-in-the-loop: when this agent may pause to ask the user, inject the
    # actor-scoped elicitation closure (mirrors _wf_event_publish above). It is
    # invoked by the builtin ``ask_user`` tool inside _invoke_agent.
    if getattr(agent_spec, "ask_user", False):
        from wfpy._elicitation_runtime import build_elicit_closure

        agent_options["_wf_elicit"] = build_elicit_closure(
            plan.options,
            agent_name=actor.name,
            model=agent_spec.model,
            event_publish=agent_options.get("_wf_event_publish"),
        )

    def _maybe_record_opencode_session_id(debug_meta: dict[str, Any]) -> None:
        nonlocal opencode_session_id
        if not is_opencode_cli:
            return
        session_value = str(debug_meta.get("opencodeSessionID") or "").strip()
        if not session_value:
            return
        opencode_session_id = session_value
        if agent_spec.stateful:
            actor.agent_cli_session_ids["opencode"] = session_value

    def _invoke_agent_with_timing(*args: Any, **kwargs: Any) -> tuple[str, list[ChatMessage], Exception | None, dict[str, Any]]:
        started = time.perf_counter()
        response_text_local, firing_messages_local, agent_error_local, debug_meta_local = _invoke_agent(
            *args, **kwargs
        )
        debug_meta_local["replyTimeMs"] = int((time.perf_counter() - started) * 1000)
        return response_text_local, firing_messages_local, agent_error_local, debug_meta_local

    # ── Collect validators for ACP mode ──────────────────────────────────
    _lsp_configs = _get_effective_lsp_configs(agent_spec)
    _has_validators = bool(_lsp_configs) or bool(agent_spec.output_validators)
    _max_val_attempts = _max_validator_attempts(agent_spec) if _has_validators else 0
    
    # Build agent input file path map for {input_<Port>} validator substitution
    _agent_input_paths: dict[str, str] = {}
    for _port_name, _fi_meta in file_inputs.items():
        _path = _fi_meta.get("path") if isinstance(_fi_meta, dict) else None
        if _path and isinstance(_path, str) and os.path.isfile(_path):
            _agent_input_paths[_port_name] = _path
    
    # For ACP mode, pass validators to the agent so it can validate itself
    _acp_validators = None
    if is_opencode_cli and _has_validators and agent_spec.output_validators:
        _acp_validators = agent_spec.output_validators
    
    response_text, firing_messages, agent_error, response_debug_meta = _invoke_agent_with_timing(
        agent_spec,
        payload_text,
        verbose,
        effective_prompt=effective_prompt_for_invoke,
        max_tool_rounds=max_tool_rounds,
        plan_options=agent_options,
        prior_history=prior_history_for_request if prior_history_for_request else None,
        cli_session_id=opencode_session_id if opencode_session_id else None,
        work_dir=plan.work_dir,
        output_ports=meta.output_ports,
        runtime_instruction_extra_rules=invoke_runtime_instruction_rules,
        tool_registry=_agent_registry,
        mcp_server_filter=_mcp_filter,
        validators=_acp_validators,
        max_validation_attempts=_max_val_attempts,
        agent_input_paths=_agent_input_paths,
    )
    _maybe_record_opencode_session_id(response_debug_meta)
    total_reply_time_ms += int(response_debug_meta.get("replyTimeMs", 0) or 0)
    response_debug_meta["replyTimeMs"] = total_reply_time_ms
    _attach_agent_response_meta(firing_messages, response_debug_meta)
    if _agent_debug_enabled(plan.options):
        _write_agent_debug_artifact(
            debug_out_dir,
            actor.name,
            f"response_raw__{actor.fire_count}",
            {"response": response_text, **response_debug_meta},
        )

    # Print usage/cost summary
    _print_agent_usage_summary(response_debug_meta, actor.name, provider)

    if agent_error is not None:
        if _agent_debug_enabled(plan.options):
            _write_agent_debug_artifact(
                debug_out_dir,
                actor.name,
                f"error__{actor.fire_count}",
                {"error": str(agent_error), **(response_debug_meta or {})},
            )
        # Always print the error with context so failures are diagnosable
        _err_usage = (
            response_debug_meta.get("usage") if isinstance(response_debug_meta, dict) else None
        )
        _err_nr = _err_usage.get("num_requests", 0) if isinstance(_err_usage, dict) else 0
        print(
            f"[wfpy][agent] {actor.name} FAILED on fire #{actor.fire_count} "
            f"after {_err_nr} API request(s): {agent_error}"
        )
        raise agent_error

    print(
        f"[wfpy][agent] {actor.name} finished: reply_time={response_debug_meta.get('replyTimeMs', 0)}ms"
    )

    normalized_response_text = _normalize_agent_response_text(response_text, meta.output_ports)
    if normalized_response_text is not None and normalized_response_text != response_text:
        response_text = normalized_response_text
        response_debug_meta["responseNormalized"] = True

    _record_agent_chat_history(
        actor,
        agent_spec,
        firing_messages,
        verbose=verbose,
    )

    # Parse structured outputs (with repair re-prompts)
    parse_error: Exception | None = None
    parsed_outputs: dict[str, Any] = {}
    required_output_ports = {
        port_name
        for port_name, pd in meta.output_ports.items()
        if actor.out_queues.get(port_name) or not _is_resource_type(pd.port_type)
    }
    for _repair_attempt in range(AGENT_MAX_OUTPUT_REPAIR_ATTEMPTS + 1):
        try:
            parsed_outputs = _parse_agent_outputs(
                response_text,
                meta.output_ports,
                required_ports=required_output_ports,
            )
            parse_error = None
            break
        except (ValueError, json.JSONDecodeError) as exc:
            parse_error = exc
            # Fallback: only plain-text single-file outputs bypass parse repair.
            if _single_output_plain_text_fallback_allowed(meta.output_ports):
                single_port = next(iter(meta.output_ports))
                parsed_outputs = {single_port: response_text}
                parse_error = None
                break
            # Re-prompt the LLM to fix its output format
            if _repair_attempt < AGENT_MAX_OUTPUT_REPAIR_ATTEMPTS:
                repair_prompt = _build_agent_repair_prompt(response_text, meta.output_ports, exc)
                print(
                    f"[wfpy][agent] {actor.name} ({meta.name}) output parse failed, "
                    f"re-prompting (attempt {_repair_attempt + 1}/{AGENT_MAX_OUTPUT_REPAIR_ATTEMPTS})"
                )
                if _agent_debug_enabled(plan.options):
                    _write_agent_debug_artifact(
                        debug_out_dir,
                        actor.name,
                        f"validation_error__{actor.fire_count}",
                        {"error": str(exc), "response": response_text},
                    )
                repair_options = dict(agent_options)
                repair_tool_rounds = min(max_tool_rounds, AGENT_MAX_REPAIR_TOOL_ROUNDS)
                repair_prior_history: list[ChatMessage] | None = firing_messages
                repair_effective = effective_prompt_for_invoke
                if is_opencode_cli and opencode_session_id:
                    repair_prior_history = None
                    repair_effective = AGENT_REPAIR_EFFECTIVE_PROMPT
                response_text, repair_msgs, repair_error, repair_debug_meta = _invoke_agent_with_timing(
                    agent_spec,
                    repair_prompt,
                    verbose,
                    effective_prompt=repair_effective,
                    max_tool_rounds=repair_tool_rounds,
                    plan_options=repair_options,
                    prior_history=repair_prior_history if repair_prior_history else None,
                    cli_session_id=opencode_session_id if opencode_session_id else None,
                    work_dir=plan.work_dir,
                    output_ports=meta.output_ports,
                    runtime_instruction_extra_rules=invoke_runtime_instruction_rules,
                    tool_registry=_agent_registry,
                    mcp_server_filter=_mcp_filter,
                )
                _maybe_record_opencode_session_id(repair_debug_meta)
                total_reply_time_ms += int(repair_debug_meta.get("replyTimeMs", 0) or 0)
                response_debug_meta["replyTimeMs"] = total_reply_time_ms
                _attach_agent_response_meta(repair_msgs, repair_debug_meta)
                if isinstance(repair_debug_meta, dict) and repair_debug_meta.get("thinking"):
                    response_debug_meta["thinking"] = repair_debug_meta.get("thinking")
                # Merge repair usage into main usage summary
                _repair_usage = (
                    repair_debug_meta.get("usage") if isinstance(repair_debug_meta, dict) else None
                )
                if isinstance(_repair_usage, dict):
                    _main_usage = response_debug_meta.setdefault(
                        "usage",
                        {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                            "num_requests": 0,
                        },
                    )
                    for _k in (
                        "prompt_tokens",
                        "completion_tokens",
                        "total_tokens",
                        "num_requests",
                    ):
                        _main_usage[_k] = _main_usage.get(_k, 0) + _repair_usage.get(_k, 0)
                    if "total_cost_usd" in _repair_usage:
                        _main_usage["total_cost_usd"] = (
                            _main_usage.get("total_cost_usd", 0.0) + _repair_usage["total_cost_usd"]
                        )
                if repair_error is not None:
                    if _agent_debug_enabled(plan.options):
                        _write_agent_debug_artifact(
                            debug_out_dir,
                            actor.name,
                            f"error__{actor.fire_count}",
                            {"error": str(repair_error)},
                        )
                    raise repair_error
                firing_messages.extend(repair_msgs)
                _record_agent_chat_history(
                    actor,
                    agent_spec,
                    repair_msgs,
                    verbose=verbose,
                )
    if parse_error is not None:
        raise parse_error

    context_patch = _extract_agent_context_patch(response_text)
    if context_patch is not None:
        try:
            _apply_context_patch(plan, actor, policy, context_patch, source="agent")
        except Exception as exc:
            raise RuntimeError(f"Invalid contextPatch from agent '{actor.name}': {exc}") from exc

    # Route parsed outputs to queues.
    # Write File-typed outputs to the persistent run output dir so
    # viewer paths survive temp-workdir cleanup.
    _agent_out_dir = out_dir
    _run_out = plan.options.get("agent_debug_dir")
    if _run_out:
        _agent_out_dir = Path(_run_out) / "work"
        _agent_out_dir.mkdir(parents=True, exist_ok=True)

    # ── Materialize File-typed outputs to disk ───────────────────────
    _multi_output_agent = len(meta.output_ports) > 1
    materialized = _materialize_agent_file_outputs(
        meta.output_ports,
        parsed_outputs,
        response_text,
        actor.name,
        actor.fire_count,
        _agent_out_dir,
        response_debug_meta,
        agent_spec,
        is_multi_output=_multi_output_agent,
    )

    # ── Empty output detection & reprompting (ACP mode only) ──────────────
    if is_opencode_cli and opencode_session_id:
        for _empty_attempt in range(MAX_EMPTY_OUTPUT_REPROMPTS):
            if not _has_empty_file_outputs(materialized, meta.output_ports):
                break
            
            logger.warning(
                "[wfpy][agent] %s: empty output detected (attempt %d/%d), reprompting",
                actor.name, _empty_attempt + 1, MAX_EMPTY_OUTPUT_REPROMPTS,
            )
            
            # Log debug artifact
            if _agent_debug_enabled(plan.options):
                _write_agent_debug_artifact(
                    debug_out_dir,
                    actor.name,
                    f"empty_output_repair__{actor.fire_count}__{_empty_attempt}",
                    {"attempt": _empty_attempt, "materialized": {k: v for k, v in materialized.items()}},
                )
            
            # Reprompt in same session
            repair_options = dict(agent_options)
            repair_tool_rounds = min(max_tool_rounds, AGENT_MAX_REPAIR_TOOL_ROUNDS)
            response_text, repair_msgs, repair_error, repair_debug_meta = _invoke_agent_with_timing(
                agent_spec,
                EMPTY_OUTPUT_REPROMPT_MSG,
                verbose,
                effective_prompt=AGENT_REPAIR_EFFECTIVE_PROMPT,
                max_tool_rounds=repair_tool_rounds,
                plan_options=repair_options,
                prior_history=None,
                cli_session_id=opencode_session_id,
                work_dir=plan.work_dir,
                output_ports=meta.output_ports,
                runtime_instruction_extra_rules=invoke_runtime_instruction_rules,
                tool_registry=_agent_registry,
                mcp_server_filter=_mcp_filter,
            )
            
            # Merge usage/timing
            _maybe_record_opencode_session_id(repair_debug_meta)
            total_reply_time_ms += int(repair_debug_meta.get("replyTimeMs", 0) or 0)
            response_debug_meta["replyTimeMs"] = total_reply_time_ms
            _attach_agent_response_meta(repair_msgs, repair_debug_meta)
            if repair_error is not None:
                raise repair_error
            firing_messages.extend(repair_msgs)
            _record_agent_chat_history(
                actor,
                agent_spec,
                repair_msgs,
                verbose=verbose,
            )
            
            # Re-parse and re-materialize
            try:
                parsed_outputs = _parse_agent_outputs(
                    response_text,
                    meta.output_ports,
                    required_ports=required_output_ports,
                )
            except (ValueError, json.JSONDecodeError):
                if _single_output_plain_text_fallback_allowed(meta.output_ports):
                    single_port = next(iter(meta.output_ports))
                    parsed_outputs = {single_port: response_text}
                else:
                    continue
            
            materialized = _materialize_agent_file_outputs(
                meta.output_ports,
                parsed_outputs,
                response_text,
                actor.name,
                actor.fire_count,
                _agent_out_dir,
                response_debug_meta,
                agent_spec,
                is_multi_output=_multi_output_agent,
                preserve_existing=True,
                existing_materialized=materialized,
            )

    # ── Output validation re-prompt loop (LSP first-class + legacy validators) ──
    # For ACP mode, the agent has already been instructed to validate itself.
    # We implement a safety net: framework validates after agent returns,
    # and sends messages if validation fails.
    if _has_validators:
        for _val_attempt in range(_max_val_attempts):
            val_errors: list[dict[str, Any]] = []
            # First-class LSP validation
            if _lsp_configs:
                val_errors.extend(
                    _run_lsp_validation(
                        _lsp_configs,
                        materialized,
                        meta.output_ports,
                        actor.name,
                        verbose,
                    )
                )
            # Legacy output_validators (cmd + lsp kind)
            if agent_spec.output_validators:
                val_errors.extend(
                    _run_agent_output_validators(
                        agent_spec.output_validators,
                        materialized,
                        meta.output_ports,
                        actor.name,
                        verbose,
                        plan,
                        agent_input_paths=_agent_input_paths,
                    )
                )
            if not val_errors:
                break  # all clean
            if _val_attempt + 1 >= _max_val_attempts:
                error_summary = "\n".join(
                    f"  [{e['port']}] {str(e.get('stderr') or e.get('cmd') or 'validator reported failure')[:500]}"
                    for e in val_errors
                )
                logger.warning(
                    "[wfpy][agent] %s: output validation exhausted after %d attempt(s), "
                    "delivering best-effort output. Errors:\n%s",
                    actor.name,
                    _val_attempt + 1,
                    error_summary,
                )
                break
            
            # For ACP mode, send a message to the agent to fix (safety net)
            if is_opencode_cli and opencode_session_id:
                error_summary = "\n".join(
                    f"  [{e['port']}] {str(e.get('stderr') or e.get('cmd') or 'validator reported failure')[:500]}"
                    for e in val_errors
                )
                repair_prompt = (
                    f"Validation failed with errors:\n{error_summary}\n\n"
                    f"Please fix the kernel and re-validate. "
                    f"This is attempt {_val_attempt + 1} of {_max_val_attempts}."
                )
                print(
                    f"[wfpy][agent] {actor.name} ({meta.name}) output validation failed (safety net), "
                    f"sending fix message (attempt {_val_attempt + 1})"
                )
                if _agent_debug_enabled(plan.options):
                    _write_agent_debug_artifact(
                        debug_out_dir,
                        actor.name,
                        f"validation_repair__{actor.fire_count}__{_val_attempt}",
                        {"errors": val_errors, "response": response_text},
                    )
                repair_options = dict(agent_options)
                repair_tool_rounds = min(max_tool_rounds, AGENT_MAX_REPAIR_TOOL_ROUNDS)
                response_text, repair_msgs, repair_error, repair_debug_meta = _invoke_agent_with_timing(
                    agent_spec,
                    repair_prompt,
                    verbose,
                    effective_prompt=AGENT_REPAIR_EFFECTIVE_PROMPT,
                    max_tool_rounds=repair_tool_rounds,
                    plan_options=repair_options,
                    prior_history=None,  # Same session
                    cli_session_id=opencode_session_id,
                    work_dir=plan.work_dir,
                    output_ports=meta.output_ports,
                    runtime_instruction_extra_rules=invoke_runtime_instruction_rules,
                    tool_registry=_agent_registry,
                    mcp_server_filter=_mcp_filter,
                )
                _maybe_record_opencode_session_id(repair_debug_meta)
                total_reply_time_ms += int(repair_debug_meta.get("replyTimeMs", 0) or 0)
                response_debug_meta["replyTimeMs"] = total_reply_time_ms
                _attach_agent_response_meta(repair_msgs, repair_debug_meta)
                if repair_error is not None:
                    raise repair_error
                firing_messages.extend(repair_msgs)
                _record_agent_chat_history(
                    actor,
                    agent_spec,
                    repair_msgs,
                    verbose=verbose,
                )
                # Re-parse and re-materialize
                try:
                    parsed_outputs = _parse_agent_outputs(
                        response_text,
                        meta.output_ports,
                        required_ports=required_output_ports,
                    )
                except (ValueError, json.JSONDecodeError):
                    if _single_output_plain_text_fallback_allowed(meta.output_ports):
                        single_port = next(iter(meta.output_ports))
                        parsed_outputs = {single_port: response_text}
                    else:
                        continue  # let loop retry
                materialized = _materialize_agent_file_outputs(
                    meta.output_ports,
                    parsed_outputs,
                    response_text,
                    actor.name,
                    actor.fire_count,
                    _agent_out_dir,
                    response_debug_meta,
                    agent_spec,
                    is_multi_output=_multi_output_agent,
                    preserve_existing=True,
                    existing_materialized=materialized,
                )
            else:
                # Non-ACP mode: traditional repair loop
                repair_prompt = _build_validation_repair_prompt(
                    response_text,
                    val_errors,
                    meta.output_ports,
                )
                print(
                    f"[wfpy][agent] {actor.name} ({meta.name}) output validation failed, "
                    f"re-prompting (attempt {_val_attempt + 1})"
                )
                if _agent_debug_enabled(plan.options):
                    _write_agent_debug_artifact(
                        debug_out_dir,
                        actor.name,
                        f"validation_repair__{actor.fire_count}__{_val_attempt}",
                        {"errors": val_errors, "response": response_text},
                    )
                repair_options = dict(agent_options)
                repair_tool_rounds = min(max_tool_rounds, AGENT_MAX_REPAIR_TOOL_ROUNDS)
                repair_prior_history = firing_messages
                repair_effective = effective_prompt_for_invoke
                response_text, repair_msgs, repair_error, repair_debug_meta = _invoke_agent_with_timing(
                    agent_spec,
                    repair_prompt,
                    verbose,
                    effective_prompt=repair_effective,
                    max_tool_rounds=repair_tool_rounds,
                    plan_options=repair_options,
                    prior_history=repair_prior_history if repair_prior_history else None,
                    cli_session_id=opencode_session_id if opencode_session_id else None,
                    work_dir=plan.work_dir,
                    output_ports=meta.output_ports,
                    runtime_instruction_extra_rules=invoke_runtime_instruction_rules,
                    tool_registry=_agent_registry,
                    mcp_server_filter=_mcp_filter,
                )
                _maybe_record_opencode_session_id(repair_debug_meta)
                total_reply_time_ms += int(repair_debug_meta.get("replyTimeMs", 0) or 0)
                response_debug_meta["replyTimeMs"] = total_reply_time_ms
                _attach_agent_response_meta(repair_msgs, repair_debug_meta)
                if repair_error is not None:
                    raise repair_error
                firing_messages.extend(repair_msgs)
                _record_agent_chat_history(
                    actor,
                    agent_spec,
                    repair_msgs,
                    verbose=verbose,
                )
                # Re-parse and re-materialize
                try:
                    parsed_outputs = _parse_agent_outputs(
                        response_text,
                        meta.output_ports,
                        required_ports=required_output_ports,
                    )
                except (ValueError, json.JSONDecodeError):
                    if _single_output_plain_text_fallback_allowed(meta.output_ports):
                        single_port = next(iter(meta.output_ports))
                        parsed_outputs = {single_port: response_text}
                    else:
                        continue  # let loop retry
                materialized = _materialize_agent_file_outputs(
                    meta.output_ports,
                    parsed_outputs,
                    response_text,
                    actor.name,
                    actor.fire_count,
                    _agent_out_dir,
                    response_debug_meta,
                    agent_spec,
                    is_multi_output=_multi_output_agent,
                    preserve_existing=True,
                    existing_materialized=materialized,
                )

    if (
        agent_spec.use_skill
        and agent_spec.use_skill_hooks
        and skill_name
        and isinstance(skill_meta, dict)
    ):
        post_hook = _skill_hook_name(skill_meta, "post")
        if post_hook:
            _configured_mcp_servers = sorted(
                {
                    str(getattr(item, "name", "")).strip()
                    for item in (agent_spec.mcp_server_configs or [])
                    if str(getattr(item, "name", "")).strip()
                }
                | {
                    str(name).strip()
                    for name in (agent_spec.mcp_servers or [])
                    if isinstance(name, str) and str(name).strip()
                }
            )
            post_hook_result = _run_skill_hook(
                skill_name=skill_name,
                hook_name=post_hook,
                phase="post",
                actor_name=actor.name,
                payload={
                    **payload,
                    "runtime": {
                        "mcp": {
                            "configuredServers": _configured_mcp_servers,
                            "availableServers": sorted(set(_mcp_available_servers)),
                            "discoveredToolNames": sorted(set(_mcp_discovered_tool_names)),
                            "observedToolNames": _observed_mcp_tool_names(
                                response_debug_meta,
                                agent_spec,
                            ),
                        }
                    },
                },
                response_text=response_text,
                plan=plan,
                timeout_ms=hook_timeout_ms,
            )
            if post_hook_result.get("ran") and (
                post_hook_result.get("error") or int(post_hook_result.get("exitCode", 0)) != 0
            ):
                raise RuntimeError(
                    f"Skill post-hook failed for '{skill_name}:{post_hook}': "
                    f"{post_hook_result.get('error') or post_hook_result.get('stderr') or 'non-zero exit'}"
                )

    # ── Route materialized outputs to queues ─────────────────────────
    for port_name in meta.output_ports:
        output_val = materialized.get(port_name)
        if output_val is None:
            continue
        for q in actor.out_queues.get(port_name, []):
            q.enqueue(output_val)
            _record_edge_token(plan, q, output_val)

    # ── Post-validation: validate File outputs after agent invocation ─
    if plan.validator_registry:
        for port_name, pd in meta.output_ports.items():
            if _is_resource_type(pd.port_type):
                # All File outputs are now materialized into _agent_out_dir
                out_file = (
                    _agent_out_dir
                    / f"{actor.name}__{port_name}__{actor.fire_count}{pd.ext or '.txt'}"
                )
                file_path = str(out_file)
                if not out_file.exists():
                    continue
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

    # ── Agent debug: write request/response artifacts ─────────────────
    if _agent_debug_enabled(plan.options):
        _write_agent_debug_artifact(
            debug_out_dir,
            actor.name,
            f"request__{actor.fire_count}",
            {
                "payload": payload_text,
                "agentSpec": {
                    "transport": agent_spec.transport,
                    "cliToolsMode": _normalize_cli_tools_mode(agent_spec, plan.options),
                    "cliToolsModeConfigured": agent_spec.cli_tools_mode,
                    "model": agent_spec.model,
                    "prompt": agent_spec.prompt,
                    "effectivePrompt": effective_prompt,
                    "effectivePromptWithRuntime": effective_prompt_with_runtime,
                    "endpoint": agent_spec.endpoint,
                    "provider": agent_spec.provider,
                    "claudeAgent": agent_spec.claude_agent,
                    "useClaudeAgent": agent_spec.use_claude_agent,
                    "skill": agent_spec.skill,
                    "usePrompt": agent_spec.use_prompt,
                    "useSkill": agent_spec.use_skill,
                    "useSkillHooks": agent_spec.use_skill_hooks,
                    "maxToolRounds": max_tool_rounds,
                    "skillDebug": {
                        **skill_debug_meta,
                        "preHook": pre_hook_result,
                        "postHook": post_hook_result,
                    },
                },
            },
        )
        _write_agent_debug_artifact(
            debug_out_dir,
            actor.name,
            f"response__{actor.fire_count}",
            {
                "response": response_text,
                **response_debug_meta,
                "parsedOutputs": {k: str(v)[:500] for k, v in parsed_outputs.items()},
            },
        )
        if agent_spec.stateful and actor.chat_history:
            _write_agent_debug_artifact(
                debug_out_dir,
                actor.name,
                f"chat_history__{actor.fire_count}",
                actor.chat_history,
            )

    agent_step_duration_ms = int((time.perf_counter() - agent_step_started) * 1000)
    print(
        f"[wfpy][agent] {actor.name} step finished: total_duration={agent_step_duration_ms}ms",
        flush=True,
    )

    actor.fire_count += 1
    return True


# ── Workflow (nested) actors ─────────────────────────────────────────────


def _step_workflow(
    actor: RuntimeActor,
    parent_plan: FifoPlan,
    out_dir: Path,
    verbose: bool,
) -> bool:
    """Fire a nested workflow actor by running its sub-plan."""
    sub_plan = actor.sub_plan
    if sub_plan is None:
        return False

    # Check all input ports have tokens
    for port_name in actor.in_queues:
        queues = actor.in_queues[port_name]
        if not queues or queues[0].size() < 1:
            return False

    # Feed input tokens into the sub-plan's input queues
    for port_name in actor.in_queues:
        q = actor.in_queues[port_name][0]
        token = q.dequeue()
        locator = ""
        if isinstance(token, (File, Resource)):
            locator = str(token.path or "").strip()
        elif isinstance(token, str):
            locator = token.strip()
        if locator and os.path.exists(locator):
            try:
                sub_plan.wf_input_resource_paths.add(str(Path(locator).resolve()))
            except OSError:
                sub_plan.wf_input_resource_paths.add(locator)
        sub_queues = sub_plan.wf_input_queues.get(port_name, [])
        for sq in sub_queues:
            sq.enqueue(token)
            _record_edge_token(sub_plan, sq, token)

    # Sub-workflow workDir: parent_workDir/{instName}__wf/
    sub_work_dir: Path | None = None
    if parent_plan.work_dir:
        sub_work_dir = Path(parent_plan.work_dir) / f"{actor.name}__wf"
        sub_work_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(sub_work_dir, 0o700)
        except OSError:
            pass

    # Propagate source_path
    if parent_plan.source_path and not sub_plan.source_path:
        sub_plan.source_path = parent_plan.source_path

    sub_plan.overlay_writer = parent_plan.overlay_writer
    sub_plan.overlay_path_prefix = [*parent_plan.overlay_path_prefix, actor.name]
    sub_plan.agent_context_writer = parent_plan.agent_context_writer
    sub_plan.context_store = parent_plan.context_store
    sub_plan.context_journal = parent_plan.context_journal
    sub_plan.context_version = parent_plan.context_version
    sub_plan.context_commit_seq = parent_plan.context_commit_seq
    sub_plan._context_lock = parent_plan._context_lock
    sub_plan.context_config = parent_plan.context_config
    sub_plan.context_live_writer = parent_plan.context_live_writer

    def _forward_streamed_output(port_name: str, token: Any) -> None:
        forwarded = False
        for parent_q in actor.out_queues.get(port_name, []):
            with parent_plan._parallel_lock:
                parent_q.enqueue(token)
                _record_edge_token(parent_plan, parent_q, token)
            forwarded = True
        if forwarded:
            parent_plan.notify_scheduler(progress=True)

    previous_output_handler = sub_plan.workflow_output_handler
    previous_streamed_counts = sub_plan._streamed_output_counts
    sub_plan.workflow_output_handler = _forward_streamed_output
    sub_plan._streamed_output_counts = {}

    # Execute the sub-plan recursively. Workflow output tokens are forwarded to
    # the parent as soon as they reach child workflow output queues.
    try:
        sub_outputs = execute_plan(
            sub_plan,
            max_workers=parent_plan.max_workers,
            out_dir=out_dir,
            work_dir=sub_work_dir,
            verbose=verbose,
            report_leftovers=False,
        )
    finally:
        sub_plan.workflow_output_handler = previous_output_handler
        sub_plan._streamed_output_counts = previous_streamed_counts

    # Forward any fallback returned outputs. In the streaming path these are
    # usually empty because child workflow output queues have already drained.
    for port_name, value in sub_outputs.items():
        if value is None:
            continue
        tokens = value if isinstance(value, list) else [value]
        for token in tokens:
            for parent_q in actor.out_queues.get(port_name, []):
                with parent_plan._parallel_lock:
                    parent_q.enqueue(token)
                    _record_edge_token(parent_plan, parent_q, token)

    actor.fire_count += 1
    return True


# ── Viewer (sink) actors ─────────────────────────────────────────────────


def _step_viewer(actor: RuntimeActor, plan: FifoPlan, verbose: bool) -> bool:
    """Consume tokens from viewer inputs (sink — no output)."""
    fired = False
    for port_name, queues in actor.in_queues.items():
        for q in queues:
            while q.size() > 0:
                token = q.dequeue()
                if verbose:
                    logger.info("Viewer %s consumed: %s", actor.name, token)
                fired = True
    if fired:
        actor.fire_count += 1
    return fired


# ═══════════════════════════════════════════════════════════════════════════
# High-level run() API
# ═══════════════════════════════════════════════════════════════════════════


def run(
    target: Any,
    inputs: dict[str, Any] | None = None,
    *,
    max_workers: int | None = None,
    out_dir: str | Path | None = None,
    run_id: str | None = None,
    work_dir: str | Path | None = None,
    keep_work_dir: bool = False,
    verbose: bool = False,
    source_path: str | None = None,
    agent_tool_auth: str | None = None,
    agent_tool_policy: str | None = None,
    agent_tool_registry: str | None = None,
    agent_tool_timeout_ms: int = 30_000,
    agent_stream: bool = False,
    resume_chat_from: str | None = None,
    validate: str | None = None,
    queue_trace: bool = True,
    keep_intermediates: bool = False,
    agent_debug: bool = False,
    agent_cli_tools_mode: str | None = None,
    agent_cli_opencode_command: str | None = None,
    agent_cli_opencode_args: str | None = None,
    agent_cli_opencode_agent: str | None = None,
    agent_cli_opencode_native_args: str | None = None,
    agent_cli_claude_command: str | None = None,
    agent_cli_claude_args: str | None = None,
    agent_cli_claude_agent: str | None = None,
    agent_cli_claude_native_args: str | None = None,
    agent_cli_codex_command: str | None = None,
    agent_cli_codex_args: str | None = None,
    agent_cli_codex_subcommand: str | None = None,
    agent_cli_codex_native_args: str | None = None,
    skill_hook_auth: str | None = None,
    skill_hook_policy: str | None = None,
    context_mode: str | None = None,
    context_budget: int | None = None,
    context_summarize: str | bool | None = None,
    resume_context_from: str | None = None,
    context_seed: dict[str, Any] | None = None,
    elicitation_handler: Any = None,
    interactive: bool | None = None,
    elicit_timeout_ms: int = 600_000,
    elicit_default: str | None = None,
    elicit_require: bool = False,
) -> dict[str, Any]:
    """Run a @workflow-decorated function or class.

    Args:
        target: A @workflow-decorated function or class.
        inputs: Dict of workflow-level input port name → value.
        out_dir: Base output directory (default ``./wf-out``).
        run_id: Override auto-generated run ID.
        work_dir: Explicit temp working directory (not cleaned up).
        keep_work_dir: If True, keep auto-created temp dir after run.
        verbose: If True, log per-step details.
        source_path: Path to the workflow source file (for CWD resolution).
        agent_tool_auth: Agent tool authorization mode (deny-all/allow-all/policy).
        agent_tool_policy: Path to agent tool policy JSON file.
        agent_tool_registry: Path to agent-tools.json file for MCP tool discovery.
        agent_tool_timeout_ms: Timeout for agent tool execution (ms).
        agent_stream: Enable streaming for agent calls.
        resume_chat_from: Path to a prior run record to restore chat histories.
        validate: Validation mode: off, warn, or enforce (default: enforce).
        queue_trace: If True (default), write run.wf-queues.json after execution.
        keep_intermediates: If True, copy workDir to runOutDir/work/ before cleanup.
        agent_debug: If True, write request/response/chat-history to agent-debug/.
        agent_cli_tools_mode: CLI tools mode for non-http transports: wfpy-none or native.
        agent_cli_opencode_command: Override executable/command for OpenCode backend.
        agent_cli_opencode_args: Extra arguments appended to OpenCode backend command.
        agent_cli_opencode_agent: OpenCode agent profile passed via ``--agent``.
        agent_cli_opencode_native_args: Extra native-tool arguments for OpenCode when cli_tools_mode=native.
        agent_cli_claude_command: Override executable/command for Claude backend.
        agent_cli_claude_args: Extra arguments appended to Claude backend command.
        agent_cli_claude_agent: Claude agent profile passed via ``--agent``.
        agent_cli_claude_native_args: Extra native-tool arguments for Claude when cli_tools_mode=native.
        agent_cli_codex_command: Override executable/command for Codex backend.
        agent_cli_codex_args: Extra arguments appended to Codex backend command.
        agent_cli_codex_subcommand: Optional Codex subcommand inserted before prompt.
        agent_cli_codex_native_args: Extra native-tool arguments for Codex when cli_tools_mode=native.
        skill_hook_auth: Skill hook authorization mode (deny-all/allow-all/policy).
        skill_hook_policy: Path to skill hook policy JSON file.
        context_mode: Shared context mode: off, scoped, or full.
        context_budget: Approximate context budget for view packing.
        context_summarize: Shared context summarization mode (on/off or bool).
        resume_context_from: Path to prior run.wf-context.json for shared context restore.
        context_seed: Optional initial context seed object.

    Returns:
        A dict of output port name → list of output values.

    Example::

        from wfpy import run

        outputs = run(my_pipeline, inputs={"Input": 42})
        print(outputs["Output"])
    """
    wf_def: WorkflowDef | None = getattr(target, "_wfpy_workflow", None)
    if wf_def is None:
        raise TypeError(
            f"{target!r} is not a @workflow-decorated function or class. Apply @workflow first."
        )

    started_at = datetime.now(timezone.utc)

    # Resolve output directories
    base_out_dir = Path(out_dir) if out_dir else Path("./wf-out")
    base_out_dir = base_out_dir.resolve()
    base_out_dir.mkdir(parents=True, exist_ok=True)

    rid = _sanitize_run_id(run_id or _default_run_id())
    run_out_dir = base_out_dir / rid
    run_out_dir.mkdir(parents=True, exist_ok=True)

    # Build the graph by executing the workflow builder
    graph = _build_workflow_graph(wf_def)

    # Compile to a plan
    plan = build_plan(graph, wf_def)

    # Shared context configuration
    mode = (context_mode or "scoped").strip().lower()
    if mode not in ("off", "scoped", "full"):
        mode = "scoped"
    summarize_value = context_summarize
    if isinstance(summarize_value, str):
        summarize_value = summarize_value.strip().lower() in ("1", "true", "yes", "on")
    elif summarize_value is None:
        summarize_value = True
    budget_value = (
        int(context_budget) if isinstance(context_budget, int) and context_budget > 0 else 1200
    )
    plan.context_config = {
        "mode": mode,
        "budget": budget_value,
        "summarize": bool(summarize_value),
        "resume_from": resume_context_from,
    }
    if isinstance(context_seed, dict):
        plan.context_store = copy.deepcopy(context_seed)

    if resume_context_from:
        resume_path = Path(resume_context_from)
        if resume_path.is_file():
            try:
                resume_payload = json.loads(resume_path.read_text())
                resumed_store = resume_payload.get("contextStore")
                if isinstance(resumed_store, dict):
                    if isinstance(context_seed, dict):
                        _merge_dict_deep(plan.context_store, resumed_store)
                    else:
                        plan.context_store = resumed_store
                resumed_version = resume_payload.get("contextVersion")
                if isinstance(resumed_version, int) and resumed_version >= 0:
                    plan.context_version = resumed_version
            except (json.JSONDecodeError, OSError):
                pass

    # Set agent-related options on the plan
    plan.options = {
        k: v
        for k, v in {
            "agent_tool_auth": agent_tool_auth,
            "agent_tool_policy": agent_tool_policy,
            "agent_tool_registry": agent_tool_registry,
            "agent_tool_timeout_ms": agent_tool_timeout_ms,
            "agent_stream": agent_stream,
            "validate": validate,
            "agent_debug": agent_debug,
            "agent_debug_dir": str(run_out_dir),
            "agent_cli_tools_mode": agent_cli_tools_mode,
            "agent_cli_opencode_command": agent_cli_opencode_command,
            "agent_cli_opencode_args": agent_cli_opencode_args,
            "agent_cli_opencode_agent": agent_cli_opencode_agent,
            "agent_cli_opencode_native_args": agent_cli_opencode_native_args,
            "agent_cli_claude_command": agent_cli_claude_command,
            "agent_cli_claude_args": agent_cli_claude_args,
            "agent_cli_claude_agent": agent_cli_claude_agent,
            "agent_cli_claude_native_args": agent_cli_claude_native_args,
            "agent_cli_codex_command": agent_cli_codex_command,
            "agent_cli_codex_args": agent_cli_codex_args,
            "agent_cli_codex_subcommand": agent_cli_codex_subcommand,
            "agent_cli_codex_native_args": agent_cli_codex_native_args,
            "skill_hook_auth": skill_hook_auth,
            "skill_hook_policy": skill_hook_policy,
            "elicit_interactive": interactive,
            "elicit_timeout_ms": elicit_timeout_ms,
            "elicit_default": elicit_default,
            "elicit_require": elicit_require,
            "_elicitation_handler": elicitation_handler,
        }.items()
        if v is not None
    }

    # Set source_path on the plan for CWD resolution
    if source_path:
        plan.source_path = str(Path(source_path).resolve())
    elif wf_def.builder_fn is not None:
        # Try to get source from the builder function
        try:
            import inspect as _inspect

            src = _inspect.getfile(wf_def.builder_fn)
            plan.source_path = str(Path(src).resolve())
        except (TypeError, OSError):
            pass

    # Load validator registry
    plan.validator_registry = load_validator_registry(
        plan.source_path,
        plan.options,
    )

    # Initialize queue trace collector
    if queue_trace:
        plan.queue_trace = QueueTraceCollector(
            enabled=True,
            workflow_name=wf_def.name or "",
        )

    has_external = any(a.kind == "external" for a in plan.actors)

    # Resolve workDir
    if work_dir is not None:
        wdir = Path(work_dir).resolve()
        wdir.mkdir(parents=True, exist_ok=True)
    else:
        wdir = run_out_dir / "work"
        wdir.mkdir(parents=True, exist_ok=True)

    # Restore chat histories from prior run if requested
    if resume_chat_from:
        prior_path = Path(resume_chat_from)
        if prior_path.is_file():
            try:
                prior_record = json.loads(prior_path.read_text())
                restored = restore_chat_histories(plan, prior_record)
                if verbose:
                    logger.info("Restored %d chat histories from %s", restored, resume_chat_from)
            except (json.JSONDecodeError, OSError) as e:
                if verbose:
                    logger.warning("Failed to restore chat histories: %s", e)

    if verbose:
        logger.info("Run %s  out=%s  work=%s", rid, run_out_dir, wdir or "(none)")

    run_out_token = os.environ.get("WF_RUN_OUT_DIR")
    os.environ["WF_RUN_OUT_DIR"] = str(run_out_dir)

    # ── Viewer & agent-context overlay writers ────────────────────────
    writers = _run_finalization._setup_run_writers(
        plan, rid, wf_def, run_out_dir, base_out_dir, started_at
    )
    overlay_writer = writers.overlay_writer
    agent_ctx_writer = writers.agent_ctx_writer
    context_live_writer = writers.context_live_writer
    event_stream = writers.event_stream

    def _persist_run_record(
        finished_at_value: datetime,
        outputs_payload: dict[str, Any],
        *,
        error: dict[str, str] | None = None,
    ) -> None:
        _run_finalization._persist_run_record(
            plan, rid, wf_def, run_out_dir, base_out_dir,
            started_at, finished_at_value, outputs_payload,
            inputs=inputs, has_external=has_external, wdir=wdir, error=error,
        )

    # Resolve max_workers: run() arg → @workflow(...) → executor default
    _effective_max_workers = max_workers
    if _effective_max_workers is None and wf_def is not None:
        _effective_max_workers = wf_def.max_workers

    # Execute
    error_info: dict[str, str] | None = None
    try:
        outputs = execute_plan(
            plan,
            inputs=inputs,
            max_workers=_effective_max_workers,
            out_dir=run_out_dir,
            work_dir=wdir,
            verbose=verbose,
        )
    except BaseException as exc:
        # ── Error overlay ─────────────────────────────────────────────
        finished_at_err = datetime.now(timezone.utc)
        last_active_entries = overlay_writer.active_entries
        overlay_writer.mark_stopped()
        error_message = str(exc).strip() or type(exc).__name__
        error_info = {
            "message": error_message,
        }
        if last_active_entries:
            error_info["entityInstanceName"] = last_active_entries[-1].entity_instance_name
        _run_finalization._write_error_overlay(
            plan, writers.overlay_base, overlay_writer, run_out_dir, error_info, finished_at_err,
        )
        _persist_run_record(finished_at_err, {}, error=error_info)
        raise
    finally:
        if run_out_token is None:
            os.environ.pop("WF_RUN_OUT_DIR", None)
        else:
            os.environ["WF_RUN_OUT_DIR"] = run_out_token
        # Always clean up live overlay files
        overlay_writer.cleanup()
        if agent_ctx_writer:
            agent_ctx_writer.cleanup()
        if context_live_writer:
            context_live_writer.cleanup()
        if event_stream is not None:
            try:
                event_stream.publish({"type": "run.finished"})
                event_stream.stop()
            except Exception:
                pass
            try:
                (base_out_dir / "run.wf-stream.live.json").unlink(missing_ok=True)
            except OSError:
                pass

    # Finalize run: write overlays, queue trace, agent/context artifacts
    _run_finalization._finalize_run(
        plan, rid, wf_def, run_out_dir, writers, outputs,
        wdir=wdir, verbose=verbose,
    )

    finished_at = datetime.now(timezone.utc)
    _persist_run_record(finished_at, outputs)

    return outputs


# ═══════════════════════════════════════════════════════════════════════════
# Plan IR export (JSON)
# ═══════════════════════════════════════════════════════════════════════════
# Moved to _plan_outputs_runtime.py
