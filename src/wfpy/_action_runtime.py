"""Internal helpers for internal (dataflow) action execution."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from wfpy._agent_validation_runtime import _is_resource_type
from wfpy._context_runtime import (
    _ActionContextFacade,
    _action_accepts_ctx,
    _actor_context_policy,
    _apply_context_patch,
    _build_context_view,
)
from wfpy._run_artifacts import _record_edge_token
from wfpy.core import ActionDef, _active_wf_config, _WorkflowEnvConfig
from wfpy.types import File, Resource

logger = logging.getLogger("wfpy")


def _ordered_internal_actions(actor: Any) -> list[ActionDef]:
    """Return candidate internal actions after schedule / priority filtering."""
    meta = actor.meta
    actions = list(meta.actions)

    schedule = meta.schedule
    if schedule is not None:
        current_state = getattr(actor.instance, "_wfpy_schedule_state", schedule.initial)
        if not isinstance(current_state, str) or not current_state.strip():
            current_state = schedule.initial
            setattr(actor.instance, "_wfpy_schedule_state", current_state)
        allowed = schedule.by_state.get(current_state, {})
        if not allowed:
            return []
        actions = [adef for adef in actions if adef.name in allowed]

    priority = meta.priority
    if priority is None:
        return actions

    max_group = len(priority.rules)
    max_rank = sum(len(rule) for rule in priority.rules)

    def _priority_key(adef: ActionDef) -> tuple[int, int, int]:
        group_rank = priority.rank.get(adef.name)
        if group_rank is None:
            return (max_group, max_rank, adef.order)
        return (group_rank[0], group_rank[1], adef.order)

    return sorted(actions, key=_priority_key)


def _try_fire_action(
    actor: Any,
    adef: ActionDef,
    out_dir: Path,
    plan: Any,
    verbose: bool,
) -> tuple[bool, Any | None]:
    """Attempt to fire a single action. Returns (fired, result)."""
    # Determine required tokens per input port
    consumes = adef.consumes
    if consumes is None:
        # Auto-infer: one token per input port that has a queue
        consumes = {}
        for port_name, queues in actor.in_queues.items():
            consumes[port_name] = 1

    # Check token availability (peek)
    peeked_values: dict[str, list[Any]] = {}
    for port_name, count in consumes.items():
        queues = actor.in_queues.get(port_name, [])
        if not queues:
            return False, None  # no queue for this port → can't fire
        q = queues[0]  # take from first queue for this port
        if q.size() < count:
            return False, None  # not enough tokens
        peeked_values[port_name] = q.peek(count)

    # Flatten peeked values for function args
    flat_args: list[Any] = []
    for port_name in consumes:
        flat_args.extend(peeked_values[port_name])

    consumed_values = [value for values in peeked_values.values() for value in values]

    # Check guard
    if adef.guard_fn is not None:
        try:
            guard_result = adef.guard_fn(actor.instance, *flat_args)
        except Exception:
            logger.warning(
                "Guard %s.%s raised; treating as not fireable",
                actor.name,
                adef.name,
                exc_info=True,
            )
            return False, None
        if not guard_result:
            return False, None

    # Guard passed — commit consumption (dequeue tokens)
    for port_name, count in consumes.items():
        queues = actor.in_queues.get(port_name, [])
        if queues:
            q = queues[0]
            for _ in range(count):
                q.dequeue()

    policy = _actor_context_policy(actor)
    view = _build_context_view(plan, actor, policy)
    ctx_facade = _ActionContextFacade(plan, actor, policy, view)

    # Expose workflow @config env/path so function-style @tool wrappers
    # can merge them into their subprocess environment.
    _wf_token = _active_wf_config.set(
        _WorkflowEnvConfig(env=dict(plan.env), search_paths=list(plan.search_paths))
    )

    # Execute the action
    try:
        if _action_accepts_ctx(adef.fn, len(flat_args)):
            result = adef.fn(actor.instance, *flat_args, ctx_facade)
        else:
            result = adef.fn(actor.instance, *flat_args)
    except Exception as e:
        logger.error("Action %s.%s raised: %s", actor.name, adef.name, e)
        raise
    finally:
        _active_wf_config.reset(_wf_token)

    patch = ctx_facade.context_patch()
    if patch is not None:
        _apply_context_patch(plan, actor, policy, patch, source="internal-action")

    schedule = actor.meta.schedule
    schedule_state_changed = False
    if schedule is not None:
        current_state = getattr(actor.instance, "_wfpy_schedule_state", schedule.initial)
        next_state = schedule.by_state.get(current_state, {}).get(adef.name)
        if next_state is not None:
            setattr(actor.instance, "_wfpy_schedule_state", next_state)
            schedule_state_changed = next_state != current_state

    def _is_within(base: Path, candidate: Path) -> bool:
        try:
            candidate.resolve().relative_to(base.resolve())
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    def _remember_preserved_resource_path(locator: str) -> None:
        if not locator:
            return
        try:
            plan.wf_input_resource_paths.add(str(Path(locator).resolve()))
        except OSError:
            plan.wf_input_resource_paths.add(locator)

    def _normalize_internal_output(port_name: str, value: Any) -> Any:
        if value is None:
            return None
        pd = actor.meta.output_ports.get(port_name)
        if pd is None or not _is_resource_type(pd.port_type):
            return value

        locator: str | None = None
        if isinstance(value, (File, Resource)):
            locator = value.path or None
        elif isinstance(value, str):
            locator = value
        if not locator:
            return value

        # Preserve pass-through file/resource tokens that already came from an
        # upstream queue. Only materialize genuinely new outputs produced by the
        # current action.
        for consumed in consumed_values:
            if consumed is value:
                return value
            if isinstance(consumed, (File, Resource)) and consumed.path == locator:
                return value
            if isinstance(consumed, str) and consumed == locator:
                return value

        # Preserve configured file/resource paths that tasks re-emit directly,
        # such as FileSource outputs. Re-materializing them into the run workdir
        # can break sibling-relative includes/imports for source files.
        actor_path = getattr(actor.instance, "path", None)
        if isinstance(actor_path, (File, Resource)) and actor_path.path == locator:
            _remember_preserved_resource_path(locator)
            return value
        if isinstance(actor_path, str) and actor_path == locator:
            _remember_preserved_resource_path(locator)
            return value

        path = Path(locator)
        try:
            resolved_locator = str(path.resolve())
        except OSError:
            resolved_locator = locator
        if resolved_locator in plan.wf_input_resource_paths:
            _remember_preserved_resource_path(locator)
            return value
        if not path.exists() or _is_within(out_dir, path):
            return value

        ext = path.suffix or (pd.ext or "")
        target = out_dir / f"{actor.name}__{port_name}__{actor.fire_count}{ext}"
        target.parent.mkdir(parents=True, exist_ok=True)

        if path.is_dir():
            shutil.copytree(path, target, dirs_exist_ok=True)
        else:
            shutil.copy2(path, target)

        if isinstance(value, File):
            return File(str(target), ext=value.ext, validate=list(value.validate))
        if isinstance(value, Resource):
            return Resource(
                str(target),
                ext=value.ext,
                validate=list(value.validate),
                kind=value.kind,
            )
        return str(target)

    effective_produces = adef.produces
    if effective_produces is None:
        effective_produces = {
            port_name: 1
            for port_name, pd in actor.meta.output_ports.items()
            if pd.direction in ("out",)
            or (pd.name or pd.attr_name).lower() in ("out", "output", "result", "report", "summary")
        }

    # Enqueue outputs (fan-out to all connected queues)
    if result is not None:
        produces = adef.produces
        if produces is None:
            # Auto: push to all output ports
            for port_name, queues in actor.out_queues.items():
                normalized = _normalize_internal_output(port_name, result)
                for q in queues:
                    q.enqueue(normalized)
                    _record_edge_token(plan, q, normalized)
        else:
            if isinstance(result, dict) and len(produces) > 1:
                # Dict result → distribute by port name
                for port_name in produces:
                    val = _normalize_internal_output(port_name, result.get(port_name))
                    if val is not None:
                        for q in actor.out_queues.get(port_name, []):
                            q.enqueue(val)
                            _record_edge_token(plan, q, val)
            elif isinstance(result, (tuple, list)) and len(produces) > 1:
                # Positional result → distribute by index
                port_names = list(produces.keys())
                for i, port_name in enumerate(port_names):
                    if i < len(result):
                        val = _normalize_internal_output(port_name, result[i])
                        for q in actor.out_queues.get(port_name, []):
                            q.enqueue(val)
                            _record_edge_token(plan, q, val)
            elif isinstance(result, dict) and len(produces) == 1:
                # Single declared output may still be returned as {port_name: value}
                port_name = next(iter(produces))
                val = _normalize_internal_output(port_name, result.get(port_name))
                if val is not None:
                    for q in actor.out_queues.get(port_name, []):
                        q.enqueue(val)
                        _record_edge_token(plan, q, val)
            else:
                # Single output → all produced ports
                for port_name in produces:
                    val = _normalize_internal_output(port_name, result)
                    for q in actor.out_queues.get(port_name, []):
                        q.enqueue(val)
                        _record_edge_token(plan, q, val)

    # If the action consumed input tokens or advanced schedule state, it already
    # made observable progress even when it intentionally emits no output.
    fired = (
        result is not None
        or not bool(effective_produces)
        or bool(consumed_values)
        or schedule_state_changed
    )
    return fired, result
