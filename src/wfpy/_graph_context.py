"""Shared graph context variable — avoids circular import between core and graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wfpy.graph import WorkflowGraph

# Module-level "current graph" — set by the runner when executing a workflow
# builder function inside a context manager.
_current_graph: WorkflowGraph | None = None
