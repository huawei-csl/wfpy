"""A workflow and every workflow nested under it, exported in one go.

``wfpy plan <file> --format graph --hierarchy`` exports the root workflow's
graph as ``--format graph`` does, plus a ``hierarchy``: one entry per nested
workflow *instance*, at its path from the root, carrying its own graph.

Each nested graph is elaborated from the instance the parent built -- the
workflow definition that instance holds, factory parameters and all -- which is
what a run does (``runner.build_plan``) and what a standalone
``plan --workflow <name>`` does not. An IDE navigating the hierarchy reads every
view from one export instead of starting a ``wfpy plan`` per drill-down, and
builds its outline from the same tree.
"""

from __future__ import annotations

import inspect
from typing import Any

from wfpy.core import WorkflowDef

# Deeper than any real hierarchy; a guard against a workflow that nests itself.
MAX_DEPTH = 32


def _definition_file(wf_def: WorkflowDef) -> str | None:
    target = wf_def.builder_fn if wf_def.builder_fn is not None else wf_def.cls
    try:
        return inspect.getsourcefile(target) if target is not None else None
    except TypeError:
        return None


def _count_nodes(exported: dict[str, Any]) -> int:
    """The workflow's own nodes -- its actors and control nodes, not its ports."""
    nodes = (exported.get("graph") or {}).get("nodes") or []
    return sum(1 for node in nodes if node.get("kind") not in ("wf-input", "wf-output"))


def _nested_instances(graph: Any) -> list[tuple[str, Any]]:
    """The nested workflow instances in *graph*: (instance name, its record).

    A workflow instance (`child(instance=...)`) answered by a past run is not
    elaborated: it runs nothing, and its graph is not part of this run.
    """
    found = []
    for name, rec in graph.actors.items():
        if not isinstance(rec.meta, WorkflowDef):
            continue
        if getattr(rec.instance, "_wfpy_workflow_instance", None) is not None:
            continue
        found.append((name, rec))
    return found


def _export_instance(
    wf_def: WorkflowDef,
    path: list[str],
    node_id: str | None,
    source_path: str | None,
    lineage: tuple[str, ...],
) -> dict[str, Any]:
    from wfpy.graph import export_graph_json
    from wfpy.runner import _build_workflow_graph

    entry: dict[str, Any] = {
        "path": path,
        "workflowName": wf_def.name,
        **({"sourcePath": source_path} if source_path else {}),
        **({"nodeId": node_id} if node_id else {}),
        "children": [],
    }
    try:
        graph = _build_workflow_graph(wf_def)
        exported = export_graph_json(graph)
    except Exception as exc:  # an instance that does not elaborate is reported, not fatal
        entry["error"] = f"{type(exc).__name__}: {exc}"
        return entry

    entry["graph"] = exported
    entry["nodeCount"] = _count_nodes(exported)
    for name, rec in _nested_instances(graph):
        child_def: WorkflowDef = rec.meta
        child_path = [*path, name]
        definition = getattr(rec.instance, "_wfpy_definition", None) or {}
        child_source = definition.get("file") or _definition_file(child_def)
        child_node_id = f"node:{rec.scope_id}:{name}"
        if len(child_path) > MAX_DEPTH or child_def.name in lineage:
            entry["children"].append(
                {
                    "path": child_path,
                    "workflowName": child_def.name,
                    **({"sourcePath": child_source} if child_source else {}),
                    "nodeId": child_node_id,
                    "children": [],
                    "truncated": True,
                }
            )
            continue
        entry["children"].append(
            _export_instance(
                child_def, child_path, child_node_id, child_source, (*lineage, child_def.name)
            )
        )
    return entry


def export_hierarchy(wf_def: WorkflowDef, source_path: str | None = None) -> dict[str, Any]:
    """The root's graph export, plus ``hierarchy``: the root and every nested instance.

    ``hierarchy`` is a tree. Each entry has its ``path`` of instance names from
    the root (``[]`` for the root), ``workflowName``, ``sourcePath`` (the file
    defining it), ``nodeId`` (its node in the parent's graph), ``nodeCount``,
    ``graph`` (its own ``--format graph`` export; absent on the root, whose
    graph is the export's), and ``children``. An instance that fails to
    elaborate has ``error`` instead of a graph; one past the depth guard, or a
    workflow nesting itself, has ``truncated``.
    """
    root = _export_instance(
        wf_def, [], None, source_path or _definition_file(wf_def), (wf_def.name,)
    )
    exported = root.pop("graph", None)
    if exported is None:
        raise RuntimeError(root.get("error") or f"workflow {wf_def.name!r} did not elaborate")
    return {**exported, "hierarchy": root}
