"""Offline agent transport.

``transport="mock"`` runs an ``@agent`` actor without a model, a network call,
or any credentials. The actor still fires under the normal dataflow rules, still
emits values on every declared output port, and still feeds downstream actors,
guards and validators — only the text generation is replaced.

That makes a graph's *wiring* testable on its own: fan-out and join, repair
feedback loops, guard routing and firing order can all be exercised in CI
without spending tokens or pinning behaviour to a model's wording.

Values come from ``mock_outputs`` when supplied, and are otherwise synthesised
from each port's declared type so downstream consumers receive something of the
shape they expect rather than a string they have to parse.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from wfpy._agent_validation_runtime import _is_resource_type
from wfpy.types import Map

logger = logging.getLogger("wfpy")

MOCK_TRANSPORT = "mock"


def _synthesize_port_value(port_name: str, port_descriptor: Any) -> Any:
    """Produce a deterministic stand-in value matching a port's declared type."""

    port_type = getattr(port_descriptor, "port_type", None)

    # File/Resource ports are materialized to disk, so hand back file *content*.
    if _is_resource_type(port_type):
        return f"mock output for {port_name}\n"

    # bool before int: bool is a subclass of int.
    if port_type is bool:
        return False
    if port_type is int:
        return 0
    if port_type is float:
        return 0.0
    if port_type is dict or port_type is Map:
        return {}
    if port_type is list:
        return []
    return f"mock:{port_name}"


def build_mock_outputs(
    spec: Any,
    output_ports: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve the ``{port: value}`` mapping this mock firing should emit.

    Explicit ``mock_outputs`` entries win; any declared port they do not cover is
    synthesised from its type. Entries naming a port the task does not declare
    are kept, so a mis-typed port name surfaces as a normal output-validation
    error instead of being silently dropped here.
    """

    declared = dict(output_ports or {})
    configured = getattr(spec, "mock_outputs", None)
    configured = dict(configured) if isinstance(configured, dict) else {}

    resolved: dict[str, Any] = {}
    for port_name, port_descriptor in declared.items():
        if port_name in configured:
            resolved[port_name] = configured[port_name]
        else:
            resolved[port_name] = _synthesize_port_value(port_name, port_descriptor)

    for port_name, value in configured.items():
        if port_name not in resolved:
            resolved[port_name] = value

    return resolved


def invoke_mock_agent(
    spec: Any,
    payload_text: str,
    verbose: bool = False,
    *,
    output_ports: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], None, dict[str, Any]]:
    """Return the standard ``_invoke_agent`` 4-tuple without calling a model."""

    outputs = build_mock_outputs(spec, output_ports)
    response_text = json.dumps({"outputs": outputs})

    firing_messages: list[dict[str, Any]] = [
        {"role": "user", "content": payload_text},
        {"role": "assistant", "content": response_text},
    ]
    debug_meta: dict[str, Any] = {
        "transport": MOCK_TRANSPORT,
        "mock": True,
        "replyTimeMs": 0,
        "mockPorts": sorted(outputs),
    }

    if verbose:
        logger.info("Mock agent transport emitted ports: %s", ", ".join(sorted(outputs)))

    return response_text, firing_messages, None, debug_meta
