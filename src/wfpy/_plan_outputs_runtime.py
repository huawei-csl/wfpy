"""Internal helpers for output materialization and plan JSON export."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from wfpy._agent_validation_runtime import _is_resource_type
from wfpy.core import TaskMeta
from wfpy.graph import ControlNodeRecord

logger = logging.getLogger("wfpy")


def _serialize_value(value: Any) -> Any:
    """Make a value JSON-serializable."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _materialize_outputs(
    plan: Any,
    out_dir: Path,
) -> dict[str, list[str]]:
    """Copy final File outputs from workDir to runOutDir.

    Naming convention (matching TS runtime):
    - Single File token:   ``{portName}{ext}``
    - Multiple File tokens: ``{portName}__{i}{ext}``
    - Non-File tokens:     ``{portName}.json``
    """

    materialized: dict[str, list[str]] = {}
    for port_name, queues in plan.wf_output_queues.items():
        values: list[Any] = []
        for queue in queues:
            if queue.size() > 0:
                values.extend(list(queue.items))
        if not values:
            continue
        port_desc = plan.wf_output_ports.get(port_name)
        ext = port_desc.ext if port_desc and port_desc.ext else ""
        is_file = _is_resource_type(port_desc.port_type) if port_desc else False

        if not is_file and all(isinstance(v, str) and os.path.isfile(v) for v in values):
            is_file = True

        if is_file:
            materialized[port_name] = []
            if len(values) == 1:
                dst = out_dir / f"{port_name}{ext}"
                _copy_file_safe(str(values[0]), str(dst))
                materialized[port_name].append(str(dst))
            else:
                for i, val in enumerate(values):
                    dst = out_dir / f"{port_name}__{i}{ext}"
                    _copy_file_safe(str(val), str(dst))
                    materialized[port_name].append(str(dst))

    return materialized


def _serialize_non_file_outputs(
    plan: Any,
    outputs: dict[str, Any],
    out_dir: Path,
) -> None:
    """Write non-File output values as ``{portName}.json`` in *out_dir*.

    This matches the TS runtime behaviour where every workflow output
    port is persisted to disk: File outputs are copied by
    ``_materialize_outputs`` and non-File outputs are serialized here.

    The *outputs* dict is **not** mutated — callers of ``run()`` still
    receive raw in-memory values for programmatic use.
    """

    for port_name, value in outputs.items():
        if value is None:
            continue
        port_desc = plan.wf_output_ports.get(port_name)
        is_file = _is_resource_type(port_desc.port_type) if port_desc else False
        if is_file:
            continue
        values = value if isinstance(value, list) else [value]
        if all(isinstance(v, str) and os.path.isfile(v) for v in values):
            continue
        dst = out_dir / f"{port_name}.json"
        dst.write_text(json.dumps(value, default=str, indent=2))


def _copy_file_safe(src: str, dst: str) -> None:
    """Copy a file, creating parent dirs. Skip if src == dst or src missing."""

    if src == dst:
        return
    if not os.path.isfile(src):
        logger.warning("Cannot materialize output — source missing: %s", src)
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def export_plan_json(plan: Any) -> dict[str, Any]:
    """Export a FifoPlan as a JSON-serializable dict (shared IR)."""

    actors_json = []
    control_by_name: dict[str, ControlNodeRecord] = {
        record.name: record for record in plan.control_nodes.values()
    }
    for actor in plan.actors:
        actor_info: dict[str, Any] = {
            "name": actor.name,
            "kind": actor.kind,
            "type": type(actor.instance).__name__,
        }
        if isinstance(actor.meta, TaskMeta):
            actor_info["ports"] = {
                attr: {
                    "name": port_desc.name,
                    "type": str(port_desc.port_type),
                    "direction": port_desc.direction,
                }
                for attr, port_desc in actor.meta.ports.items()
            }
            actor_info["parameters"] = {
                key: str(val) for key, val in actor.meta.parameters.items()
            }
            if actor.meta.tool_spec:
                actor_info["tool"] = {
                    "cmd": actor.meta.tool_spec.cmd,
                    "args": actor.meta.tool_spec.args,
                }
            if actor.meta.agent_spec:
                actor_info["agent"] = {
                    "prompt": actor.meta.agent_spec.prompt,
                    "model": actor.meta.agent_spec.model,
                }
        elif actor.kind.startswith("control-"):
            if actor.kind == "control-if":
                actor_info["ports"] = {
                    "cond": {"name": "cond", "type": "bool", "direction": "in"},
                    "out": {"name": "out", "type": "any", "direction": "out"},
                }
            elif actor.kind == "control-loop":
                actor_info["ports"] = {
                    "iter": {"name": "iter", "type": "iterable", "direction": "in"},
                    "item": {"name": "item", "type": "any", "direction": "out"},
                    "out": {"name": "out", "type": "any", "direction": "out"},
                }
            record = control_by_name.get(actor.name)
            if record:
                actor_info["control"] = {
                    "id": record.node_id,
                    "scopes": dict(record.scopes),
                }
        actors_json.append(actor_info)

    connections_json = [
        {
            "id": queue.id,
            "from": f"{queue.from_actor}.{queue.from_port}",
            "to": f"{queue.to_actor}.{queue.to_port}",
        }
        for queue in plan.all_queues
    ]

    scopes_json = [
        {
            "id": scope.id,
            "kind": scope.kind,
            "parent": scope.parent_id,
            "controlNodeId": scope.control_node_id,
        }
        for scope in plan.scopes.values()
    ]

    control_json = [
        {
            "id": record.node_id,
            "kind": record.kind,
            "name": record.name,
            "parentScope": record.parent_scope_id,
            "scopes": dict(record.scopes),
        }
        for record in plan.control_nodes.values()
    ]

    return {
        "name": plan.name,
        "actors": actors_json,
        "connections": connections_json,
        "inputs": list(plan.wf_input_queues.keys()),
        "outputs": list(plan.wf_output_queues.keys()),
        "scopes": scopes_json,
        "controlNodes": control_json,
    }
