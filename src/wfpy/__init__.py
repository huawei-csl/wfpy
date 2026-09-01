"""wfpy — Pythonic agentic workflows on actor-dataflow semantics.

FIFO-queued dataflow scheduling with round-robin actor execution,
multi-action tasks with guards, and external tool / LLM agent integration.

Public API::

    from wfpy import task, workflow, connect, if_, loop, Port, Resource, File, Map
    from wfpy import action, guard, agent, tool, viewer, streamblocks
    from wfpy import streaming, pipeline, keep, config
    from wfpy import run
"""

from __future__ import annotations

# Types
from wfpy.types import (
    Port,
    Resource,
    File,
    Map,
    PortInstance,
    infer_resource_kind,
    is_url_resource,
    is_http_resource,
    is_folder_resource,
    is_file_resource,
)

# Decorators
from wfpy.core import (
    task,
    action,
    guard,
    workflow,
    agent,
    tool,
    viewer,
    streamblocks,
    context,
    streaming,
    pipeline,
    keep,
    config,
)

# Wiring
from wfpy.graph import connect, if_, loop

# Execution
from wfpy.runner import run

__all__ = [
    # Types
    "Port",
    "Resource",
    "File",
    "Map",
    "PortInstance",
    "infer_resource_kind",
    "is_url_resource",
    "is_http_resource",
    "is_folder_resource",
    "is_file_resource",
    # Decorators
    "task",
    "action",
    "guard",
    "workflow",
    "agent",
    "tool",
    "viewer",
    "streamblocks",
    "context",
    "streaming",
    "pipeline",
    "keep",
    "config",
    # Wiring
    "connect",
    "if_",
    "loop",
    # Execution
    "run",
]

__version__ = "0.1.0"
