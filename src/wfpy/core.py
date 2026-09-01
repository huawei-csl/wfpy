"""wfpy.core — Decorators and metaclass machinery for tasks, workflows, agents, and tools."""

from __future__ import annotations

import copy
import contextvars as _contextvars
import dataclasses
import inspect
import functools
import os as _os
import re as _re
import subprocess as _subprocess
from typing import Any, Callable

from wfpy.types import (
    PortDescriptor,
    PortInstance,
    extract_ports,
)

__all__ = [
    "task",
    "action",
    "guard",
    "workflow",
    "agent",
    "tool",
    "streaming",
    "pipeline",
    "keep",
    "config",
    "viewer",
    # Internal helpers exposed for the runner
    "TaskMeta",
    "WorkflowDef",
    "AgentSpec",
    "ToolSpec",
    "ActionDef",
    "McpServerInlineConfig",
    "LspServerConfig",
    "AgentOutputValidator",
    "_active_wf_config",
    "_active_wf_builder_depth",
    "_WorkflowEnvConfig",
]


_SKIP_FACTORY_PARAMETER = object()


def _normalize_factory_parameter_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)):
        normalized_items: list[Any] = []
        for item in value:
            normalized = _normalize_factory_parameter_value(item)
            if normalized is _SKIP_FACTORY_PARAMETER:
                return _SKIP_FACTORY_PARAMETER
            normalized_items.append(normalized)
        return normalized_items
    if isinstance(value, dict):
        normalized_dict: dict[str, Any] = {}
        for key, item in value.items():
            normalized = _normalize_factory_parameter_value(item)
            if normalized is _SKIP_FACTORY_PARAMETER:
                return _SKIP_FACTORY_PARAMETER
            normalized_dict[str(key)] = normalized
        return normalized_dict
    if inspect.isclass(value) or inspect.ismodule(value) or callable(value):
        return _SKIP_FACTORY_PARAMETER
    return _SKIP_FACTORY_PARAMETER


def _extract_factory_parameters(target: Callable[..., Any]) -> list[dict[str, Any]]:
    qualname = getattr(target, "__qualname__", "")
    freevar_names = list(getattr(target.__code__, "co_freevars", ()))
    if "<locals>" not in qualname or not freevar_names:
        return []

    closure_values = inspect.getclosurevars(target).nonlocals
    params: list[dict[str, Any]] = []
    for name in freevar_names:
        if name not in closure_values:
            continue
        raw_value = closure_values[name]
        normalized_value = _normalize_factory_parameter_value(raw_value)
        if normalized_value is _SKIP_FACTORY_PARAMETER:
            continue
        params.append(
            {
                "name": name,
                "value": normalized_value,
                "type": type(raw_value).__name__,
            }
        )
    return params


def _extract_factory_name(target: Callable[..., Any]) -> str | None:
    qualname = getattr(target, "__qualname__", "")
    if "<locals>" not in qualname:
        return None
    prefix = qualname.rsplit(".<locals>", 1)[0].strip()
    if not prefix:
        return None
    return prefix.rsplit(".", 1)[-1] or None


def _make_workflow_proxy(wf_def: WorkflowDef) -> Any:
    """Create a lightweight instance proxy for nested workflow composition."""

    class WorkflowInstanceProxy:
        def __getattr__(self, attr: str) -> Any:
            if attr in wf_def.input_names:
                return PortInstance(
                    actor_instance=self,
                    port_descriptor=PortDescriptor(
                        name=attr,
                        port_type=wf_def.input_names[attr],
                        direction="in",
                        ext="",
                        validate=[],
                        attr_name=attr,
                    ),
                )
            if attr in wf_def.output_names:
                return PortInstance(
                    actor_instance=self,
                    port_descriptor=PortDescriptor(
                        name=attr,
                        port_type=wf_def.output_names[attr],
                        direction="out",
                        ext="",
                        validate=[],
                        attr_name=attr,
                    ),
                )
            raise AttributeError(f"{wf_def.name!s} has no port '{attr}'")

    proxy = WorkflowInstanceProxy()
    proxy._wfpy_workflow = wf_def  # type: ignore[attr-defined]
    proxy._wfpy_instance_name = ""  # type: ignore[attr-defined]
    proxy._wfpy_class_name = wf_def.name  # type: ignore[attr-defined]
    return proxy


# ═══════════════════════════════════════════════════════════════════════════
# Action / Guard descriptors (decorated methods inside a @task class)
# ═══════════════════════════════════════════════════════════════════════════


@dataclasses.dataclass
class ActionDef:
    """Metadata for one @action-decorated method inside a task."""

    name: str
    fn: Callable[..., Any]
    consumes: dict[str, int] | None  # None = auto-infer, {} = explicit zero-input
    produces: dict[str, int] | None  # None = auto-infer
    guard_fn: Callable[..., bool] | None = None
    # source order index — used for priority
    order: int = 0


@dataclasses.dataclass(frozen=True)
class ScheduleTransition:
    """One transition in a task-local action scheduler."""

    state: str
    action: str
    next_state: str


@dataclasses.dataclass
class ScheduleDef:
    """Declarative action scheduler metadata for a task."""

    initial: str
    transitions: list[ScheduleTransition]
    by_state: dict[str, dict[str, str]]


@dataclasses.dataclass
class PriorityDef:
    """Declarative action-priority metadata for a task."""

    rules: list[list[str]]
    rank: dict[str, tuple[int, int]]


def action(
    fn: Callable[..., Any] | None = None,
    *,
    consumes: dict[str, int] | None = None,
    produces: dict[str, int] | None = None,
) -> Any:
    """Decorator marking a task method as a named *action*.

    When used without arguments on a task that has a single ``action`` method,
    the token consumption/production is inferred from the method signature and
    return annotation.

    Usage::

        @action
        def my_action(self, x: int) -> int:
            ...

        @action(consumes={"In": 2}, produces={"Out": 1})
        def pair_sum(self, a: int, b: int) -> int:
            ...
    """

    def decorator(method: Callable[..., Any]) -> Callable[..., Any]:
        # Attach metadata; the @task decorator will collect these later.
        method._wfpy_action = ActionDef(  # type: ignore[attr-defined]
            name=method.__name__,
            fn=method,
            consumes=consumes,
            produces=produces,
        )
        return method

    if fn is not None:
        # @action without parens
        return decorator(fn)
    return decorator


def guard(predicate: Callable[..., bool]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator attaching a guard predicate to an @action method.

    The guard receives the same arguments as the action (peeked tokens).
    All guards must return True for the action to fire.

    Usage::

        @action(consumes={"In": 2})
        @guard(lambda self, a, b: a < b)
        def sum_pair(self, a: int, b: int) -> int:
            ...
    """

    def decorator(method: Callable[..., Any]) -> Callable[..., Any]:
        # The method may or may not already have _wfpy_action set
        if hasattr(method, "_wfpy_action"):
            method._wfpy_action.guard_fn = predicate
        else:
            method._wfpy_guard = predicate  # type: ignore[attr-defined]
        return method

    return decorator


# ═══════════════════════════════════════════════════════════════════════════
# TaskMeta — the processed task class metadata
# ═══════════════════════════════════════════════════════════════════════════


@dataclasses.dataclass
class TaskMeta:
    """Compiled metadata for a @task-decorated class."""

    cls: type
    name: str
    kind: str  # "internal" | "external" | "agent" | "viewer"
    ports: dict[str, PortDescriptor]
    input_ports: dict[str, PortDescriptor]
    output_ports: dict[str, PortDescriptor]
    actions: list[ActionDef]
    parameters: dict[str, Any]  # name → annotation
    state_fields: dict[str, Any]  # name → default value
    schedule: ScheduleDef | None = None
    priority: PriorityDef | None = None
    # External-specific
    tool_spec: ToolSpec | None = None
    agent_spec: AgentSpec | None = None
    annotations: dict[str, Any] = dataclasses.field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
# ToolSpec / AgentSpec
# ═══════════════════════════════════════════════════════════════════════════


@dataclasses.dataclass
class ToolSpec:
    """Specification for an external tool (subprocess) task."""

    cmd: str = ""
    args: list[str] = dataclasses.field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    shell: bool = False
    inherit_stdio: bool = True


@dataclasses.dataclass
class McpServerInlineConfig:
    """Inline MCP server configuration for per-agent server definitions."""

    name: str
    transport: str = "streamable-http"  # "stdio" | "http" | "streamable-http"
    url: str = ""
    command: str = ""
    args: list[str] = dataclasses.field(default_factory=list)
    env: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class LspServerConfig:
    """Per-agent LSP server configuration for automatic output validation.

    When configured on an agent, the runtime automatically validates all File
    output ports by spawning the LSP server and collecting diagnostics.  If
    errors are found the agent is re-prompted with structured line-level
    feedback.

    Example::

        @agent(
            prompt="...",
            lspServers=[
                {"command": "clangd", "args": ["--log=error"],
                 "languageId": "cpp",
                 "extraFlags": ["-xcce", "--npu-arch=dav-2201", "-std=c++17"]},
            ],
        )
    """

    command: str
    args: list[str] = dataclasses.field(default_factory=list)
    language_id: str = "cpp"
    root_uri: str | None = None
    initialization_options: dict[str, Any] | None = None
    extra_flags: list[str] | None = None
    severity_threshold: str = "error"
    ports: list[str] | None = None  # None = all File ports
    max_repair_attempts: int = 2
    timeout_ms: int = 30_000


@dataclasses.dataclass
class AgentOutputValidator:
    """Per-agent output validation configuration.

    Two modes:

    **Command mode** (default, ``kind="cmd"``):
      Runs ``cmd`` with ``args`` against the output file.
      ``{file}`` in args is replaced with the materialized path.

    **LSP mode** (``kind="lsp"``):
      Spawns an LSP server, opens the file, collects structured diagnostics.
      Re-prompt includes line numbers, severity, and error messages.

    Examples::

        # Command mode (bisheng syntax check)
        {"cmd": "bisheng", "args": ["-fsyntax-only", "-xcce", "{file}"],
         "ports": ["Optimized"]}

        # LSP mode (clangd)
        {"kind": "lsp", "cmd": "clangd", "args": ["--log=error"],
         "languageId": "cpp",
         "extraFlags": ["-xcce", "--npu-arch=dav-2201", "-I/path/include"],
         "ports": ["Optimized"]}
    """

    cmd: str
    args: list[str] = dataclasses.field(default_factory=list)
    ports: list[str] | None = None  # None = all File ports; list = named ports
    max_repair_attempts: int = 2
    timeout_ms: int = 30_000
    kind: str = "cmd"  # "cmd" | "lsp"
    env: dict[str, str] | None = None  # per-validator env (merged on top of @config env)
    # LSP-specific fields
    language_id: str = "cpp"
    root_uri: str | None = None
    initialization_options: dict[str, Any] | None = None
    extra_flags: list[str] | None = None  # e.g. clangd fallback compiler flags
    severity_threshold: str = "error"  # only fail on this severity or worse


@dataclasses.dataclass
class AgentSpec:
    """Specification for an LLM agent task."""

    prompt: str
    claude_agent: str | None = None
    use_claude_agent: bool = False
    skill: str | None = None
    use_prompt: bool = True
    use_skill: bool = True
    use_skill_hooks: bool = True
    model: str = "openai/gpt-4o"
    provider: str | None = None
    endpoint: str | None = None
    timeout_ms: int = 120_000
    fireable_without_input: int = 0
    stateful: bool = False
    ask_user: bool = False  # allow the agent to pause mid-firing and ask the user (HTTP transport)
    context_budget: int = 50
    truncation_strategy: str = "sliding"  # "sliding" | "summarize"
    mcp_servers: list[str] | None = None  # per-agent MCP server filter
    mcp_server_configs: list[McpServerInlineConfig] | None = None  # per-agent inline server defs
    # LSP output validation — first-class, auto-validates File output ports
    lsp_servers: list[LspServerConfig] | None = None
    lsp_command: str | None = None  # shorthand: single LSP command (e.g. "clangd")
    lsp_args: list[str] | None = None
    lsp_language_id: str = "cpp"
    lsp_extra_flags: list[str] | None = None
    lsp_severity_threshold: str = "error"
    lsp_max_repair_attempts: int = 2
    # Legacy — kept for backward compatibility
    output_validators: list[AgentOutputValidator] | None = None  # per-agent output validation
    # Invocation backend transport
    transport: str = "http"  # "http" | "opencode-cli" | "claude-cli" | "codex-cli" | "mock"
    # CLI tool execution mode for non-http transports
    cli_tools_mode: str = "wfpy-none"  # "wfpy-none" | "native"
    # Reasoning effort for opencode CLI (--variant flag)
    variant: str = ""  # "max" | "high" | "minimal" | "" (default)
    # transport="mock" only: fixed {port: value} outputs. Ports left unlisted are
    # synthesized from their declared type.
    mock_outputs: dict[str, Any] | None = None


def _normalize_context_scopes(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def context(
    cls: type | None = None,
    *,
    read: list[str] | None = None,
    write: list[str] | None = None,
) -> Any:
    """Attach shared-context read/write policy metadata to a task class."""

    def decorator(klass: type) -> type:
        if not hasattr(klass, "_wfpy_meta"):
            klass = task(klass)
        meta: TaskMeta = klass._wfpy_meta  # type: ignore[attr-defined]
        meta.annotations["context"] = {
            "read": _normalize_context_scopes(read or []),
            "write": _normalize_context_scopes(write or []),
        }
        return klass

    if cls is not None:
        return decorator(cls)
    return decorator


# ═══════════════════════════════════════════════════════════════════════════
# @task — class decorator
# ═══════════════════════════════════════════════════════════════════════════


def _infer_direction(
    ports: dict[str, PortDescriptor],
) -> tuple[dict[str, PortDescriptor], dict[str, PortDescriptor]]:
    """Split ports into inputs/outputs. 'inout' ports are classified by name heuristics."""
    inputs: dict[str, PortDescriptor] = {}
    outputs: dict[str, PortDescriptor] = {}
    for attr, pd in ports.items():
        d = pd.direction
        if d == "in":
            inputs[attr] = pd
        elif d == "out":
            outputs[attr] = pd
        else:
            # Heuristic: common output names
            name_lower = (pd.name or attr).lower()
            if name_lower in ("out", "output", "result", "report", "summary"):
                outputs[attr] = pd
            else:
                inputs[attr] = pd
    return inputs, outputs


def _collect_actions(cls: type, ports: dict[str, PortDescriptor]) -> list[ActionDef]:
    """Collect @action-decorated methods from a class, or auto-wrap a single 'action' method."""
    actions: list[ActionDef] = []

    # Gather methods in definition order across the MRO so task subclasses can
    # inherit and selectively override action methods without re-declaring the
    # entire action set.
    order = 0
    action_positions: dict[str, int] = {}
    for klass in reversed(cls.__mro__):
        if klass is object:
            continue
        for name, val in vars(klass).items():
            if callable(val) and hasattr(val, "_wfpy_action"):
                adef = dataclasses.replace(getattr(val, "_wfpy_action"))
                # If guard was set separately (via @guard before @action), merge it
                if hasattr(val, "_wfpy_guard") and adef.guard_fn is None:
                    adef.guard_fn = getattr(val, "_wfpy_guard")
                existing_idx = action_positions.get(name)
                if existing_idx is not None:
                    adef.order = actions[existing_idx].order
                    actions[existing_idx] = adef
                    continue
                adef.order = order
                actions.append(adef)
                action_positions[name] = len(actions) - 1
                order += 1

    if actions:
        return actions

    # Check for a single method named "action"
    action_method = getattr(cls, "action", None)
    if action_method is not None and callable(action_method):
        # Auto-infer consumes/produces from signature
        sig = inspect.signature(action_method)
        params = [p for p in sig.parameters.values() if p.name != "self"]
        # Build default consumes: one token per input port, matched by param position
        input_ports_list = [
            pd
            for pd in ports.values()
            if pd.direction in ("in", "inout")
            and (pd.name or pd.attr_name).lower()
            not in ("out", "output", "result", "report", "summary")
        ]
        consumes: dict[str, int] = {}
        for i, param in enumerate(params):
            if i < len(input_ports_list):
                port_name = input_ports_list[i].name or input_ports_list[i].attr_name
                consumes[port_name] = 1

        # Produces: if method returns non-None, one token to first output port
        output_ports_list = [
            pd
            for pd in ports.values()
            if pd.direction in ("out",)
            or (pd.name or pd.attr_name).lower() in ("out", "output", "result", "report", "summary")
        ]
        produces: dict[str, int] = {}
        return_annotation = sig.return_annotation
        if return_annotation is not inspect.Parameter.empty and return_annotation is not None:
            for op in output_ports_list:
                produces[op.name or op.attr_name] = 1

        actions.append(
            ActionDef(
                name="action",
                fn=action_method,
                consumes=consumes,
                produces=produces,
                guard_fn=getattr(action_method, "_wfpy_guard", None),
                order=0,
            )
        )

    return actions


def _collect_schedule(cls: type, actions: list[ActionDef]) -> ScheduleDef | None:
    """Collect optional task Schedule metadata from an inner class."""

    schedule_cls = getattr(cls, "Schedule", None)
    if schedule_cls is None:
        return None

    initial = getattr(schedule_cls, "initial", None)
    if not isinstance(initial, str) or not initial.strip():
        raise TypeError(f"{cls.__name__}.Schedule.initial must be a non-empty string")

    transitions_raw = getattr(schedule_cls, "transitions", None)
    if not isinstance(transitions_raw, (list, tuple)):
        raise TypeError(f"{cls.__name__}.Schedule.transitions must be a list of 3-tuples")

    action_names = {adef.name for adef in actions}
    transitions: list[ScheduleTransition] = []
    by_state: dict[str, dict[str, str]] = {}
    seen_pairs: set[tuple[str, str]] = set()

    for idx, item in enumerate(transitions_raw):
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise TypeError(
                f"{cls.__name__}.Schedule.transitions[{idx}] must be a 3-tuple "
                "of (state, action, next_state)"
            )
        state, action_name, next_state = item
        if not isinstance(state, str) or not state.strip():
            raise TypeError(
                f"{cls.__name__}.Schedule.transitions[{idx}][0] must be a non-empty string"
            )
        if not isinstance(action_name, str) or not action_name.strip():
            raise TypeError(
                f"{cls.__name__}.Schedule.transitions[{idx}][1] must be a non-empty string"
            )
        if not isinstance(next_state, str) or not next_state.strip():
            raise TypeError(
                f"{cls.__name__}.Schedule.transitions[{idx}][2] must be a non-empty string"
            )

        normalized_state = state.strip()
        normalized_action = action_name.strip()
        normalized_next_state = next_state.strip()
        if normalized_action not in action_names:
            raise TypeError(
                f"{cls.__name__}.Schedule references unknown action {normalized_action!r}"
            )
        state_action = (normalized_state, normalized_action)
        if state_action in seen_pairs:
            raise TypeError(
                f"{cls.__name__}.Schedule has duplicate transition for {state_action!r}"
            )
        seen_pairs.add(state_action)
        transitions.append(
            ScheduleTransition(
                state=normalized_state,
                action=normalized_action,
                next_state=normalized_next_state,
            )
        )
        by_state.setdefault(normalized_state, {})[normalized_action] = normalized_next_state

    return ScheduleDef(initial=initial.strip(), transitions=transitions, by_state=by_state)


def _collect_priority(cls: type, actions: list[ActionDef]) -> PriorityDef | None:
    """Collect optional task Priority metadata from an inner class."""

    priority_cls = getattr(cls, "Priority", None)
    if priority_cls is None:
        return None

    rules_raw = getattr(priority_cls, "rules", None)
    if not isinstance(rules_raw, (list, tuple)):
        raise TypeError(f"{cls.__name__}.Priority.rules must be a list of ordered action groups")

    action_names = {adef.name for adef in actions}
    rules: list[list[str]] = []
    rank: dict[str, tuple[int, int]] = {}

    for group_idx, group in enumerate(rules_raw):
        if not isinstance(group, (list, tuple)):
            raise TypeError(
                f"{cls.__name__}.Priority.rules[{group_idx}] must be a list of action names"
            )
        normalized_group: list[str] = []
        for item_idx, action_name in enumerate(group):
            if not isinstance(action_name, str) or not action_name.strip():
                raise TypeError(
                    f"{cls.__name__}.Priority.rules[{group_idx}][{item_idx}] must be a non-empty string"
                )
            normalized_action = action_name.strip()
            if normalized_action not in action_names:
                raise TypeError(
                    f"{cls.__name__}.Priority references unknown action {normalized_action!r}"
                )
            if normalized_action in rank:
                raise TypeError(
                    f"{cls.__name__}.Priority contains duplicate action {normalized_action!r}"
                )
            normalized_group.append(normalized_action)
            rank[normalized_action] = (group_idx, item_idx)
        rules.append(normalized_group)

    return PriorityDef(rules=rules, rank=rank)


def _classify_fields(
    cls: type, ports: dict[str, PortDescriptor]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate class annotations into parameters vs. state fields.

    Convention:
    - Fields with a **class-level default** and a type annotation are *state* fields
      (they persist across firings).
    - Fields with a type annotation but **no default** are *parameters* (provided at
      instantiation in the workflow).
    - Fields starting with _ are always state.
    """
    port_attrs = set(ports.keys())
    annotations = {}
    for klass in reversed(cls.__mro__):
        if hasattr(klass, "__annotations__"):
            annotations.update(klass.__annotations__)

    parameters: dict[str, Any] = {}
    state_fields: dict[str, Any] = {}

    for field_name, field_type in annotations.items():
        if field_name.startswith("__") or field_name == "Ports" or field_name in port_attrs:
            continue
        has_default = hasattr(cls, field_name)
        default_val = getattr(cls, field_name, None)

        if field_name.startswith("_"):
            # Private → always state
            state_fields[field_name] = default_val
        elif has_default:
            # Has default → state
            state_fields[field_name] = default_val
        else:
            # No default → parameter
            parameters[field_name] = field_type

    return parameters, state_fields


def task(cls: type) -> type:
    """Decorator that registers a class as a wfpy task.

    Extracts ports, actions, parameters, and state from the class definition.
    The decorated class gains:
    - ``_wfpy_meta: TaskMeta`` attribute
    - A custom ``__init__`` that accepts parameters as keyword args
    - Port attribute access on instances returns ``PortInstance`` objects
    """
    ports = extract_ports(cls)
    input_ports, output_ports = _infer_direction(ports)
    actions = _collect_actions(cls, ports)
    parameters, state_fields = _classify_fields(cls, ports)
    schedule = _collect_schedule(cls, actions)
    priority = _collect_priority(cls, actions)

    meta = TaskMeta(
        cls=cls,
        name=cls.__name__,
        kind="internal",
        ports=ports,
        input_ports=input_ports,
        output_ports=output_ports,
        actions=actions,
        parameters=parameters,
        state_fields=state_fields,
        schedule=schedule,
        priority=priority,
    )
    cls._wfpy_meta = meta  # type: ignore[attr-defined]

    # Build a custom __init__
    original_init = vars(cls).get("__init__")

    def _try_capture_instance_name(self: Any) -> None:
        try:
            import inspect

            frame = inspect.currentframe()
            caller = frame.f_back if frame is not None else None
            if caller is None:
                return
            for key, val in caller.f_locals.items():
                if val is self and key and not key.startswith("_"):
                    if not getattr(self, "_wfpy_instance_name", ""):
                        self._wfpy_instance_name = key
                    try:
                        from wfpy import _graph_context

                        if _graph_context._current_graph is not None:
                            _graph_context._current_graph.rename_actor_instance(self, key)
                    except Exception:
                        pass
                    break
        except Exception:
            pass

    def __init__(self: Any, **kwargs: Any) -> None:
        unexpected = set(kwargs) - set(meta.parameters)
        if unexpected:
            names = ", ".join(repr(name) for name in sorted(unexpected))
            raise TypeError(f"{meta.name}() got unexpected parameter(s): {names}")

        # Set parameters from kwargs
        for pname in meta.parameters:
            if pname in kwargs:
                setattr(self, pname, kwargs[pname])
            else:
                raise TypeError(f"{meta.name}() missing required parameter {pname!r}")

        # Initialize state fields with copies of defaults
        for sname, sdefault in meta.state_fields.items():
            setattr(self, sname, copy.deepcopy(sdefault))

        # Instance name (set later by workflow builder)
        self._wfpy_instance_name = ""
        self._wfpy_meta = meta
        if meta.schedule is not None:
            self._wfpy_schedule_state = meta.schedule.initial

        try:
            from wfpy import _graph_context

            if _graph_context._current_graph is not None:
                _graph_context._current_graph.register_actor(self)
        except Exception:
            pass

        _try_capture_instance_name(self)

        if original_init is not None:
            original_init(self, **kwargs)

    cls.__init__ = __init__  # type: ignore[misc]

    # Port access: instance.PortAttr → PortInstance
    def __getattr__(self: Any, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        ports_map = self._wfpy_meta.ports
        if name in ports_map:
            port_instance = PortInstance(actor_instance=self, port_descriptor=ports_map[name])
            _try_capture_instance_name(self)
            return port_instance
        raise AttributeError(f"{type(self).__name__!r} has no port or attribute {name!r}")

    cls.__getattr__ = __getattr__  # type: ignore[attr-defined]

    return cls


# ═══════════════════════════════════════════════════════════════════════════
# @tool — class decorator for external subprocess tasks
#         + function decorator for subprocess-backed callables
# ═══════════════════════════════════════════════════════════════════════════

# Placeholder pattern: {kw_name} — simple single-level substitution for @tool functions
_TOOL_FN_PLACEHOLDER_RE = _re.compile(r"\{(\w+)\}")


@dataclasses.dataclass
class _WorkflowEnvConfig:
    """Snapshot of @config(env=, path=) for the active workflow."""

    env: dict[str, str] = dataclasses.field(default_factory=dict)
    search_paths: list[str] = dataclasses.field(default_factory=list)


# Set by the runner before executing an action so that function-style
# @tool wrappers can pick up the workflow's @config(env=, path=) settings.
_active_wf_config: _contextvars.ContextVar[_WorkflowEnvConfig | None] = _contextvars.ContextVar(
    "_active_wf_config", default=None
)


# Set while executing a workflow builder body. Nested workflow function calls
# inside an active builder are treated as workflow actor composition.
_active_wf_builder_depth: _contextvars.ContextVar[int] = _contextvars.ContextVar(
    "_active_wf_builder_depth", default=0
)


def tool(
    cls_or_fn: type | None = None,
    *,
    cmd: str = "",
    args: list[str] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    shell: bool = False,
    inherit_stdio: bool = False,
    timeout: int | None = None,
) -> Any:
    """Decorator for external-tool tasks (class) or subprocess-backed callables (function).

    On a **class**: marks it as an external-tool task (same as before).

    On a **function**: replaces the body with a subprocess invocation.
    The function's kwargs are substituted into ``{name}`` placeholders
    in *cmd* and *args*, then the subprocess is executed. The function
    need not have a body (use ``...`` or ``pass``).

    Class usage::

        @tool(cmd="zip", args=["-j", "{out.zip}", "{in.file}"])
        class ZipOne:
            class Ports:
                file = Port[File](direction="in")
                zip  = Port[File](direction="out", ext=".zip")

    Function usage::

        @tool(cmd="python", args=["scripts/run.py", "--no-cleanup", "{so_path}"])
        def run_validation(so_path: str, num_tests: int = 5) -> subprocess.CompletedProcess:
            ...
    """
    _args = args or []
    _cwd = cwd
    _env = env or {}
    _timeout = timeout

    def _wrap_function(fn: Any) -> Any:
        """Create a subprocess-invoking wrapper for a @tool-decorated function."""
        import inspect

        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*call_args: Any, **call_kwargs: Any) -> _subprocess.CompletedProcess:  # type: ignore[type-arg]
            bound = sig.bind(*call_args, **call_kwargs)
            bound.apply_defaults()
            kw = dict(bound.arguments)

            def _sub(template: str) -> str:
                return _TOOL_FN_PLACEHOLDER_RE.sub(
                    lambda m: str(kw[m.group(1)]) if m.group(1) in kw else m.group(0),
                    template,
                )

            resolved_cmd = _sub(cmd)
            resolved_args = [_sub(a) for a in _args]

            # Merge environment: OS env ← workflow @config(env=) ← per-tool env
            run_env: dict[str, str] = {**_os.environ}
            wf_cfg = _active_wf_config.get()
            if wf_cfg:
                if wf_cfg.env:
                    run_env.update(wf_cfg.env)
                if wf_cfg.search_paths:
                    prefix = _os.pathsep.join(wf_cfg.search_paths)
                    existing = run_env.get("PATH", "")
                    run_env["PATH"] = f"{prefix}{_os.pathsep}{existing}" if existing else prefix
            if _env:
                run_env.update(_env)

            result = _subprocess.run(
                [resolved_cmd, *resolved_args],
                cwd=_cwd,
                env=run_env,
                capture_output=not inherit_stdio,
                text=True,
                shell=shell,
                timeout=_timeout,
            )
            return result

        wrapper._tool_spec = ToolSpec(  # type: ignore[attr-defined]
            cmd=cmd,
            args=_args,
            cwd=_cwd,
            env=_env,
            shell=shell,
            inherit_stdio=inherit_stdio,
        )
        return wrapper

    def _wrap_class(klass: type) -> type:
        """Mark a class as an external-tool task."""
        klass = task(klass)
        meta: TaskMeta = klass._wfpy_meta  # type: ignore[attr-defined]
        meta.kind = "external"
        meta.tool_spec = ToolSpec(
            cmd=cmd,
            args=_args,
            cwd=_cwd,
            env=_env,
            shell=shell,
            inherit_stdio=inherit_stdio,
        )
        return klass

    def decorator(cls_or_fn_inner: Any) -> Any:
        if isinstance(cls_or_fn_inner, type):
            return _wrap_class(cls_or_fn_inner)
        if callable(cls_or_fn_inner):
            return _wrap_function(cls_or_fn_inner)
        raise TypeError("@tool can only decorate a class or a function")

    if cls_or_fn is not None:
        # @tool without arguments — need at least cmd=
        raise TypeError("@tool requires at least cmd= argument")
    return decorator


# ═══════════════════════════════════════════════════════════════════════════
# @agent — class decorator for LLM agent tasks
# ═══════════════════════════════════════════════════════════════════════════


def agent(
    cls: type | None = None,
    *,
    prompt: str = "",
    transport: str = "http",
    cli_tools_mode: str = "wfpy-none",
    claude_agent: str | None = None,
    use_claude_agent: bool = False,
    skill: str | None = None,
    use_prompt: bool = True,
    use_skill: bool = True,
    use_skill_hooks: bool = True,
    model: str = "openai/gpt-4o",
    provider: str | None = None,
    endpoint: str | None = None,
    timeout_ms: int = 120_000,
    fireable_without_input: int = 0,
    stateful: bool = False,
    ask_user: bool = False,
    context_budget: int = 50,
    truncation_strategy: str = "sliding",
    mcp_servers: list[str | dict[str, Any]] | None = None,
    mcp_server_configs: list[dict[str, Any]] | None = None,
    # LSP output validation — first-class params
    lsp_servers: list[dict[str, Any]] | None = None,
    lsp_command: str | None = None,
    lsp_args: list[str] | None = None,
    lsp_language_id: str = "cpp",
    lsp_extra_flags: list[str] | None = None,
    lsp_severity_threshold: str = "error",
    lsp_max_repair_attempts: int = 2,
    # Legacy
    output_validators: list[dict[str, Any]] | None = None,
    # Compatibility aliases used by GUI/editor integrations.
    claudeAgent: str | None = None,
    useClaudeAgent: bool | None = None,
    usePrompt: bool | None = None,
    useSkill: bool | None = None,
    useSkillHooks: bool | None = None,
    askUser: bool | None = None,
    timeoutMs: int | None = None,
    fireableWithoutInput: int | None = None,
    contextBudget: int | None = None,
    truncationStrategy: str | None = None,
    mcpServers: list[str | dict[str, Any]] | None = None,
    mcpServerConfigs: list[dict[str, Any]] | None = None,
    lspServers: list[dict[str, Any]] | None = None,
    lspCommand: str | None = None,
    lspArgs: list[str] | None = None,
    lspLanguageId: str | None = None,
    lspExtraFlags: list[str] | None = None,
    lspSeverityThreshold: str | None = None,
    lspMaxRepairAttempts: int | None = None,
    outputValidators: list[dict[str, Any]] | None = None,
    cliToolsMode: str | None = None,
    variant: str | None = None,
    reasoningEffort: str | None = None,
    mock_outputs: dict[str, Any] | None = None,
    mockOutputs: dict[str, Any] | None = None,
) -> Any:
    """Decorator marking a class as an LLM agent task.

    Usage::

        @agent(prompt="Analyze the document.", model="openai/gpt-4o")
        class AnalyzeDocument:
            class Ports:
                inp    = Port[File](direction="in")
                report = Port[File](direction="out", ext=".md")

    Per-agent MCP server config::

        @agent(
            prompt="...",
            mcpServers=[
                {"name": "my-mcp", "transport": "streamable-http", "url": "http://localhost:8080/mcp"},
            ],
        )
        class AgentWithMcp:
            class Ports:
                In = Port[File](direction="in")
                Out = Port[File](direction="out", ext=".md")

    Per-agent LSP output validation (auto-validates File outputs)::

        @agent(
            prompt="...",
            lspServers=[
                {"command": "clangd", "args": ["--log=error"],
                 "languageId": "cpp",
                 "extraFlags": ["-xcce", "--npu-arch=dav-2201"]},
            ],
        )
        class AgentWithLsp:
            class Ports:
                In = Port[File](direction="in")
                CodeOut = Port[File](direction="out", ext=".cpp")

    Simple LSP (single command shorthand)::

        @agent(prompt="...", lspCommand="clangd", lspLanguageId="cpp")
        class AgentSimpleLsp:
            class Ports:
                In = Port[File](direction="in")
                Out = Port[File](direction="out", ext=".cpp")
    """

    def decorator(klass: type) -> type:
        klass = task(klass)
        meta: TaskMeta = klass._wfpy_meta  # type: ignore[attr-defined]
        meta.kind = "agent"

        # Parse configurations using extracted helpers
        from wfpy._agent_decorator import (
            _normalize_agent_kwargs,
            _parse_mcp_configs,
            _parse_lsp_configs,
            _parse_output_validators,
        )

        normalized = _normalize_agent_kwargs(
            claude_agent=claude_agent,
            use_claude_agent=use_claude_agent,
            use_prompt=use_prompt,
            use_skill=use_skill,
            use_skill_hooks=use_skill_hooks,
            timeout_ms=timeout_ms,
            fireable_without_input=fireable_without_input,
            context_budget=context_budget,
            truncation_strategy=truncation_strategy,
            cli_tools_mode=cli_tools_mode,
            ask_user=ask_user,
            claudeAgent=claudeAgent,
            useClaudeAgent=useClaudeAgent,
            usePrompt=usePrompt,
            useSkill=useSkill,
            useSkillHooks=useSkillHooks,
            timeoutMs=timeoutMs,
            fireableWithoutInput=fireableWithoutInput,
            contextBudget=contextBudget,
            truncationStrategy=truncationStrategy,
            cliToolsMode=cliToolsMode,
            askUser=askUser,
        )

        server_name_filters, inline_configs = _parse_mcp_configs(
            mcp_servers, mcp_server_configs, mcpServers, mcpServerConfigs
        )

        parsed_lsp_servers, lsp_shorthand = _parse_lsp_configs(
            lsp_servers,
            lsp_command,
            lsp_args,
            lsp_language_id,
            lsp_extra_flags,
            lsp_severity_threshold,
            lsp_max_repair_attempts,
            lspServers,
            lspCommand,
            lspArgs,
            lspLanguageId,
            lspExtraFlags,
            lspSeverityThreshold,
            lspMaxRepairAttempts,
        )

        parsed_validators = _parse_output_validators(output_validators, outputValidators)

        # Normalize variant/reasoningEffort
        effective_variant = str(variant if reasoningEffort is None else reasoningEffort).strip()

        meta.agent_spec = AgentSpec(
            prompt=prompt,
            transport=transport,
            cli_tools_mode=normalized["cli_tools_mode"],
            claude_agent=normalized["claude_agent"],
            use_claude_agent=normalized["use_claude_agent"],
            skill=skill,
            use_prompt=normalized["use_prompt"],
            use_skill=normalized["use_skill"],
            use_skill_hooks=normalized["use_skill_hooks"],
            model=model,
            provider=provider,
            endpoint=endpoint,
            timeout_ms=normalized["timeout_ms"],
            fireable_without_input=normalized["fireable_without_input"],
            stateful=stateful,
            ask_user=normalized["ask_user"],
            context_budget=normalized["context_budget"],
            truncation_strategy=normalized["truncation_strategy"],
            mcp_servers=server_name_filters,
            mcp_server_configs=inline_configs or None,
            lsp_servers=parsed_lsp_servers,
            lsp_command=lsp_shorthand["lsp_command"],
            lsp_args=lsp_shorthand["lsp_args"],
            lsp_language_id=lsp_shorthand["lsp_language_id"],
            lsp_extra_flags=lsp_shorthand["lsp_extra_flags"],
            lsp_severity_threshold=lsp_shorthand["lsp_severity_threshold"],
            lsp_max_repair_attempts=lsp_shorthand["lsp_max_repair_attempts"],
            output_validators=parsed_validators,
            variant=effective_variant,
            mock_outputs=mock_outputs if mockOutputs is None else mockOutputs,
        )
        return klass

    if cls is not None:
        raise TypeError("@agent requires at least prompt= argument: @agent(prompt='...')")
    return decorator


# ═══════════════════════════════════════════════════════════════════════════
# @viewer — class decorator for viewer/sink tasks
# ═══════════════════════════════════════════════════════════════════════════


def viewer(
    cls: type | None = None,
    *,
    action_name: str = "open",
    inputs: list[str] | None = None,
    viewType: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    command_name: str | None = None,
    command_args: list[str] | None = None,
) -> Any:
    """Decorator marking a class as a viewer (sink-only, no output).

    ``command`` / ``args`` are the canonical command-mode parameters.
    ``command_name`` / ``command_args`` are accepted as Python-friendly aliases.
    Canonical names take precedence when both forms are provided.
    """

    def decorator(klass: type) -> type:
        klass = task(klass)
        meta: TaskMeta = klass._wfpy_meta  # type: ignore[attr-defined]
        meta.kind = "viewer"
        normalized_command = command if command is not None else command_name
        normalized_args = args if args is not None else command_args
        if action_name == "command" and (
            not isinstance(normalized_command, str) or normalized_command.strip() == ""
        ):
            raise TypeError(
                "@viewer(action_name='command') requires command= or command_name="
            )
        viewer_annotation: dict[str, Any] = {
            "action": action_name,
            "inputs": inputs or [],
        }
        if isinstance(viewType, str) and viewType.strip() != "":
            viewer_annotation["viewType"] = viewType
        if isinstance(normalized_command, str) and normalized_command.strip() != "":
            viewer_annotation["command"] = normalized_command
        if isinstance(normalized_args, list) and normalized_args:
            viewer_annotation["args"] = [str(item) for item in normalized_args]
        meta.annotations["viewer"] = viewer_annotation
        return klass

    if cls is not None:
        return decorator(cls)
    return decorator


def streamblocks(
    cls: type | None = None,
    *,
    facade: str = "design",
    network: str | None = None,
    inputs: list[str] | None = None,
) -> Any:
    """Decorator marking a class as a StreamBlocks/CalPy design node.

    Two facades, chosen per class:

    ``design``
        An ordinary task. You declare the ports and write the actions; wfpy
        imposes no shape, so a plain producer and the four-port feedback form
        are equally available. Double-clicking it in the diagram opens whatever
        it last produced, which means after a run.

    ``instance``
        A typed facade over ``calpy run`` for the network named by ``network=``.
        CalPy already ships the main — ``calpy run <file.py> --input <file>``
        compiles and runs in one step — so nothing here drives it beyond mapping
        ports onto flags. Double-clicking opens the DECLARED file, so it works
        before anything has run and while a long compile is still going.

    ``network=`` is required for ``instance``: without one there is nothing to
    compile, run or open, which is a static error rather than a run-time
    failure. It stays optional for ``design``, where having produced nothing yet
    is a legitimate state.

    The double-click is not new machinery. It rides the same ``viewer``
    annotation the IDE already reads, with ``source`` saying whether the target
    is the declared path or the last token.
    """

    if facade not in {"design", "instance"}:
        raise TypeError(
            f"@streamblocks(facade={facade!r}): expected 'design' or 'instance'"
        )
    if facade == "instance" and (not isinstance(network, str) or network.strip() == ""):
        raise TypeError(
            "@streamblocks(facade='instance') requires network='path/to/network.py' — "
            "an instance with no network has nothing to compile, run or open"
        )

    def decorator(klass: type) -> type:
        klass = task(klass)
        meta: TaskMeta = klass._wfpy_meta  # type: ignore[attr-defined]
        meta.kind = "streamblocks"

        streamblocks_annotation: dict[str, Any] = {"facade": facade}
        if isinstance(network, str) and network.strip() != "":
            streamblocks_annotation["network"] = network
        meta.annotations["streamblocks"] = streamblocks_annotation

        # Reuse the viewer annotation rather than inventing a second way to open
        # something: the IDE keys on the annotation, not on the node's kind. An
        # instance resolves its target from the declared path; a design has no
        # path to declare, so it resolves from the last token it produced.
        viewer_annotation: dict[str, Any] = {
            "action": "openWith",
            "viewType": "calpy.networkDiagram",
            "source": "declared" if facade == "instance" else "token",
        }
        if isinstance(network, str) and network.strip() != "":
            viewer_annotation["path"] = network
        if inputs:
            viewer_annotation["inputs"] = list(inputs)
        elif facade == "design":
            # Fall back to the outputs, since a design's target is what it made.
            viewer_annotation["inputs"] = list(meta.output_ports.keys())
        meta.annotations["viewer"] = viewer_annotation
        return klass

    if cls is not None:
        return decorator(cls)
    return decorator


# ═══════════════════════════════════════════════════════════════════════════
# Simple annotation decorators
# ═══════════════════════════════════════════════════════════════════════════


def streaming(cls_or_fn: Any) -> Any:
    """Mark a task or workflow as streaming."""
    if hasattr(cls_or_fn, "_wfpy_meta"):
        cls_or_fn._wfpy_meta.annotations["streaming"] = True
    else:
        cls_or_fn._wfpy_streaming = True
    return cls_or_fn


def pipeline(cls_or_fn: Any) -> Any:
    """Mark a workflow as a pipeline (layout hint for diagram)."""
    if hasattr(cls_or_fn, "_wfpy_meta"):
        cls_or_fn._wfpy_meta.annotations["pipeline"] = True
    else:
        cls_or_fn._wfpy_pipeline = True
    return cls_or_fn


def keep(cls_or_fn: Any) -> Any:
    """Mark task outputs as kept (not cleaned up after run)."""
    if hasattr(cls_or_fn, "_wfpy_meta"):
        cls_or_fn._wfpy_meta.annotations["keep"] = True
    else:
        cls_or_fn._wfpy_keep = True
    return cls_or_fn


def config(
    *,
    path: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> Callable[[Any], Any]:
    """Workflow-level configuration: tool search paths and environment variables.

    Usage::

        @config(path=["/opt/tools/bin"], env={"TURNUS_HOME": "/opt/turnus"})
        @workflow
        def my_pipeline():
            ...
    """

    def decorator(cls_or_fn: Any) -> Any:
        if hasattr(cls_or_fn, "_wfpy_workflow"):
            wf: WorkflowDef = getattr(cls_or_fn, "_wfpy_workflow")
            if path:
                wf.search_paths = path
            if env:
                wf.env = env
        else:
            if path:
                setattr(cls_or_fn, "_wfpy_search_paths", path)
            if env:
                setattr(cls_or_fn, "_wfpy_env", env)
        return cls_or_fn

    return decorator


# ═══════════════════════════════════════════════════════════════════════════
# @workflow — function or class decorator
# ═══════════════════════════════════════════════════════════════════════════


@dataclasses.dataclass
class WorkflowDef:
    """Compiled metadata for a @workflow-decorated function or class."""

    name: str
    builder_fn: Callable[..., Any] | None  # the decorated function body
    cls: type | None  # the decorated class, if class-based
    input_names: dict[str, Any]  # port_name → type
    output_names: dict[str, Any]  # port_name → type
    search_paths: list[str] = dataclasses.field(default_factory=list)
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    annotations: dict[str, Any] = dataclasses.field(default_factory=dict)
    ports: dict[str, PortDescriptor] = dataclasses.field(default_factory=dict)
    max_workers: int | None = None  # None → ThreadPoolExecutor default
    factory_name: str | None = None
    factory_parameters: list[dict[str, Any]] = dataclasses.field(default_factory=list)


def workflow(
    fn_or_cls: Any = None,
    *,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    max_workers: int | None = None,
) -> Any:
    """Decorator for defining a workflow.

    Can be applied to a function (builder style) or a class.

    Function style::

        @workflow(inputs={"Input": int}, outputs={"Output": int})
        def my_pipeline():
            d = Doubler(factor=2)
            connect("Input", d.In)
            connect(d.Out, "Output")

    Class style::

        @workflow
        class MyPipeline:
            class Ports:
                inp = Port[int](direction="in")
                out = Port[int](direction="out")
            ...

    *max_workers* controls the size of the thread pool for parallel execution.
    ``None`` (the default) uses the ``ThreadPoolExecutor`` default (typically
    ``min(32, os.cpu_count()+4)``).  Set to ``1`` for sequential execution.
    """

    def decorator(target: Any) -> Any:
        if inspect.isclass(target):
            return _process_workflow_class(target, inputs, outputs, max_workers)
        elif callable(target):
            return _process_workflow_function(target, inputs, outputs, max_workers)
        else:
            raise TypeError(f"@workflow must be applied to a class or function, got {type(target)}")

    if fn_or_cls is not None:
        return decorator(fn_or_cls)
    return decorator


def _process_workflow_function(
    fn: Any,
    inputs: dict[str, Any] | None,
    outputs: dict[str, Any] | None,
    max_workers: int | None = None,
) -> Any:
    """Process a function-based @workflow definition."""
    wf_def = WorkflowDef(
        name=fn.__name__,
        builder_fn=fn,
        cls=None,
        input_names=inputs or {},
        output_names=outputs or {},
        max_workers=max_workers,
    )
    wf_def.factory_name = _extract_factory_name(fn)
    wf_def.factory_parameters = _extract_factory_parameters(fn)
    # Transfer any pre-set annotations
    search_paths = getattr(fn, "_wfpy_search_paths", None)
    if search_paths:
        wf_def.search_paths = search_paths
    env = getattr(fn, "_wfpy_env", None)
    if env:
        wf_def.env = env
    if hasattr(fn, "_wfpy_pipeline"):
        wf_def.annotations["pipeline"] = True
    if hasattr(fn, "_wfpy_streaming"):
        wf_def.annotations["streaming"] = True

    setattr(fn, "_wfpy_workflow", wf_def)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from wfpy import _graph_context

        if _graph_context._current_graph is not None and _active_wf_builder_depth.get() > 0:
            proxy = _make_workflow_proxy(wf_def)
            if wf_def.factory_name:
                proxy._wfpy_factory_name = wf_def.factory_name
            if wf_def.factory_parameters:
                proxy._wfpy_factory_parameters = copy.deepcopy(wf_def.factory_parameters)
            _graph_context._current_graph.register_actor(proxy)
            return proxy

        if _graph_context._current_graph is not None:
            if wf_def.factory_name:
                _graph_context._current_graph.factory_name = wf_def.factory_name
            if wf_def.factory_parameters:
                _graph_context._current_graph.factory_parameters = copy.deepcopy(wf_def.factory_parameters)

        token = _active_wf_builder_depth.set(_active_wf_builder_depth.get() + 1)
        try:
            return fn(*args, **kwargs)
        finally:
            _active_wf_builder_depth.reset(token)

    setattr(wrapper, "_wfpy_workflow", wf_def)
    # Preserve annotations set before/after @workflow
    if search_paths:
        setattr(wrapper, "_wfpy_search_paths", search_paths)
    if env:
        setattr(wrapper, "_wfpy_env", env)
    if hasattr(fn, "_wfpy_pipeline"):
        setattr(wrapper, "_wfpy_pipeline", True)
    if hasattr(fn, "_wfpy_streaming"):
        setattr(wrapper, "_wfpy_streaming", True)

    return wrapper


def _process_workflow_class(
    cls: type,
    inputs: dict[str, Any] | None,
    outputs: dict[str, Any] | None,
    max_workers: int | None = None,
) -> type:
    """Process a class-based @workflow definition."""
    ports = extract_ports(cls)

    # Derive inputs/outputs from Ports class if not explicitly provided
    in_names = inputs or {}
    out_names = outputs or {}
    if not in_names and not out_names:
        for attr, pd in ports.items():
            name = pd.name or attr
            if pd.direction == "in":
                in_names[name] = pd.port_type
            elif pd.direction == "out":
                out_names[name] = pd.port_type
            else:
                # Heuristic
                if name.lower() in ("out", "output", "result", "report", "summary"):
                    out_names[name] = pd.port_type
                else:
                    in_names[name] = pd.port_type

    wf_def = WorkflowDef(
        name=cls.__name__,
        builder_fn=None,
        cls=cls,
        input_names=in_names,
        output_names=out_names,
        ports=ports,
        max_workers=max_workers,
    )
    cls._wfpy_workflow = wf_def  # type: ignore[attr-defined]
    return cls
