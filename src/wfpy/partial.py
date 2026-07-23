from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

from wfpy.graph import ActorRecord
from wfpy.graph import export_graph_json
from wfpy.graph import WorkflowGraph


@dataclass
class PartialError:
    message: str
    file: str
    line: int | None = None
    column: int | None = None


def _format_exc(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def build_partial_graph(file_path: str, workflow_name: str | None, exc: BaseException) -> dict[str, Any]:
    """Build a best-effort graph from a Python workflow file.

    This is the ``wfpy plan --best-effort`` fallback used when a workflow fails
    to elaborate (e.g. a ``connect()`` to a non-existent port raised).

    The strategy is to reproduce the *normal* graph build as faithfully as
    possible: import the (importable) module, instantiate the real ``@task``
    classes so nodes carry their REAL ports/kind/meta, and replay the
    ``connect()`` calls one-by-one. A connect (or statement) that raises is
    recorded as a located :class:`PartialError` and skipped, so the rest of the
    graph still renders — instead of collapsing into synthetic boundary-port
    nodes.

    If the module itself cannot be imported, we fall back to a conservative
    AST-only stub so that at least node names still appear.
    """
    top_error = PartialError(message=_format_exc(exc), file=file_path)
    errors: list[PartialError] = [top_error]

    try:
        source = open(file_path, "r", encoding="utf-8").read()
    except Exception as read_exc:  # pragma: no cover - I/O failure
        errors.append(PartialError(message=_format_exc(read_exc), file=file_path))
        graph = WorkflowGraph(name=workflow_name or "workflow")
        return _finalize_graph(graph, errors, partial=True)

    try:
        module_ast = ast.parse(source, filename=file_path)
    except SyntaxError as syn:
        errors.append(PartialError(
            message=f"SyntaxError: {syn.msg}",
            file=file_path,
            line=syn.lineno,
            column=syn.offset,
        ))
        graph = WorkflowGraph(name=workflow_name or "workflow")
        return _finalize_graph(graph, errors, partial=True)

    target_fn = _find_workflow_fn(module_ast, workflow_name)
    if target_fn is None:
        errors.append(PartialError(message="Workflow function not found", file=file_path))
        graph = WorkflowGraph(name=workflow_name or "workflow")
        return _finalize_graph(graph, errors, partial=True)

    # Preferred path: import the module so we can build nodes from the REAL
    # task classes (real ports / kind / meta), exactly like the success path.
    module = _try_import_module(file_path)
    if module is not None:
        graph, failed_connects = _build_from_real_module(module, target_fn, workflow_name, file_path, errors)
    else:
        graph = _build_from_ast_stub(target_fn, workflow_name, file_path, errors)
        failed_connects = []

    _dedup_errors(errors, top_error)
    return _finalize_graph(
        graph, errors, partial=True, failed_connects=failed_connects, file_path=file_path
    )


# ───────────────────────────────────────────────────────────────────────────
# Real-module path: reuse the normal build so nodes carry real ports.
# ───────────────────────────────────────────────────────────────────────────


def _try_import_module(file_path: str) -> Any | None:
    """Import the workflow module using the normal CLI loader.

    Returns the imported module, or ``None`` if importing fails (in which case
    the AST stub fallback is used). Only the workflow *elaboration* is expected
    to have raised, so this import normally succeeds.
    """
    try:
        from wfpy.cli import _load_module
    except Exception:
        return None
    try:
        return _load_module(file_path)
    except SystemExit:
        return None
    except Exception:
        return None


def _build_from_real_module(
    module: Any,
    target_fn: ast.FunctionDef,
    workflow_name: str | None,
    file_path: str,
    errors: list[PartialError],
) -> tuple[WorkflowGraph, list[dict[str, Any]]]:
    """Replay the workflow body using the real imported classes.

    Each top-level statement of the workflow function is executed individually
    inside the active graph context, using the module's globals. Actor
    assignments register real nodes (with real ports) via the task ``__init__``;
    ``connect()`` calls attach to the real ports. A statement that raises is
    recorded as a located error and skipped — a failed connect therefore does
    NOT fall back to synthesizing boundary-port nodes.

    Returns the graph plus a list of the ``connect()`` calls that failed (with
    their endpoint expressions, line, and message) so the offending node can be
    flagged after export.
    """
    graph = WorkflowGraph(name=workflow_name or target_fn.name or "workflow")
    failed_connects: list[dict[str, Any]] = []

    # Shared namespace seeded from the module globals, used as both globals and
    # locals so statements can reference names bound by earlier statements.
    ns: dict[str, Any] = dict(getattr(module, "__dict__", {}))

    # Mark that we're inside a workflow builder body so that sub-workflow calls
    # (e.g. ``turnus_trace_project()``) compose as actor proxies (with real
    # ports) instead of returning ``None`` — same as the normal build path.
    depth_token = None
    try:
        from wfpy.core import _active_wf_builder_depth

        depth_token = _active_wf_builder_depth.set(_active_wf_builder_depth.get() + 1)
    except Exception:
        depth_token = None

    try:
        with graph:
            for stmt in target_fn.body:
                if _is_docstring(stmt) or isinstance(stmt, (ast.Return, ast.Pass)):
                    continue
                endpoints = _connect_endpoints(stmt)
                before = len(errors)
                _exec_stmt(stmt, ns, file_path, errors)
                if endpoints is not None and len(errors) > before:
                    failed_connects.append({
                        "from": endpoints[0],
                        "to": endpoints[1],
                        "line": getattr(stmt, "lineno", None),
                        "message": errors[-1].message,
                    })
    finally:
        if depth_token is not None:
            try:
                from wfpy.core import _active_wf_builder_depth

                _active_wf_builder_depth.reset(depth_token)
            except Exception:
                pass

    # Rename auto-registered actors to their bound variable names, mirroring the
    # normal builder's locals-snapshot rename pass.
    for name, val in list(ns.items()):
        if not name or name.startswith("_"):
            continue
        try:
            if hasattr(val, "_wfpy_meta") or hasattr(val, "_wfpy_workflow"):
                graph.rename_actor_instance(val, name)
        except Exception:
            continue

    return graph, failed_connects


def _connect_endpoints(stmt: ast.stmt) -> tuple[str, str] | None:
    """Return ``(from_expr, to_expr)`` if ``stmt`` is a ``connect(a, b)`` call."""
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        call = stmt.value
        if _call_name(call.func) == "connect" and len(call.args) >= 2:
            return _expr_text(call.args[0]), _expr_text(call.args[1])
    return None


def _exec_stmt(
    stmt: ast.stmt,
    ns: dict[str, Any],
    file_path: str,
    errors: list[PartialError],
) -> None:
    """Execute a single workflow statement, recording any failure as located."""
    try:
        code = compile(
            ast.Module(body=[stmt], type_ignores=[]),
            filename=file_path,
            mode="exec",
        )
    except Exception as ex:  # pragma: no cover - compile failure on a stmt
        errors.append(PartialError(
            message=_format_exc(ex), file=file_path, line=getattr(stmt, "lineno", None)
        ))
        return

    try:
        exec(code, ns, ns)
    except Exception as ex:
        errors.append(PartialError(
            message=_format_exc(ex), file=file_path, line=getattr(stmt, "lineno", None)
        ))


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


# ───────────────────────────────────────────────────────────────────────────
# AST stub fallback: used only when the module cannot be imported.
# ───────────────────────────────────────────────────────────────────────────


def _build_from_ast_stub(
    target_fn: ast.FunctionDef,
    workflow_name: str | None,
    file_path: str,
    errors: list[PartialError],
) -> WorkflowGraph:
    graph = WorkflowGraph(name=workflow_name or "workflow")
    with graph:
        for stmt in target_fn.body:
            _collect_partial(stmt, graph, errors, file_path)
    return graph


def _find_workflow_fn(module: ast.Module, workflow_name: str | None) -> ast.FunctionDef | None:
    funcs = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    # Also consider functions nested under decorators are top-level FunctionDef.
    if workflow_name:
        for fn in funcs:
            if fn.name == workflow_name:
                return fn
    if funcs:
        return funcs[0]
    return None


def _collect_partial(
    stmt: ast.stmt,
    graph: WorkflowGraph,
    errors: list[PartialError],
    file_path: str,
) -> None:
    if isinstance(stmt, ast.Assign) and stmt.targets:
        target = stmt.targets[0]
        if isinstance(target, ast.Name) and isinstance(stmt.value, ast.Call):
            class_name = _call_name(stmt.value.func) or "Unknown"
            try:
                meta = _stub_task_meta(class_name)
                instance = _StubInstance(class_name, meta)
                instance._wfpy_instance_name = target.id
                graph.actors[target.id] = ActorRecord(
                    instance_name=target.id,
                    instance=instance,
                    meta=meta,
                    scope_id=graph.current_scope_id,
                )
                graph._creation_order.append(("actor", target.id))
            except Exception as ex:
                errors.append(PartialError(message=_format_exc(ex), file=file_path, line=stmt.lineno))
        return

    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        call = stmt.value
        func_name = _call_name(call.func)
        if func_name == "connect" and len(call.args) >= 2:
            from_expr = _expr_text(call.args[0])
            to_expr = _expr_text(call.args[1])
            try:
                from wfpy.graph import connect
                connect(from_expr, to_expr)
            except Exception as ex:
                errors.append(PartialError(message=_format_exc(ex), file=file_path, line=stmt.lineno))
        return

    for child in ast.iter_child_nodes(stmt):
        if isinstance(child, ast.stmt):
            _collect_partial(child, graph, errors, file_path)


def _call_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _expr_text(expr: ast.AST) -> str:
    try:
        return ast.unparse(expr)
    except Exception:
        return ""


class _StubInstance:
    def __init__(self, class_name: str, meta: Any) -> None:
        self._wfpy_class_name = class_name
        self._wfpy_meta = meta
        self._wfpy_workflow = None
        self._wfpy_instance_name = ""


def _stub_task_meta(class_name: str) -> Any:
    return type(
        "_StubTaskMeta",
        (),
        {
            "cls": object,
            "name": class_name,
            "kind": "internal",
            "ports": {},
            "input_ports": {},
            "output_ports": {},
            "actions": [],
            "parameters": {},
            "state_fields": {},
            "schedule": None,
            "priority": None,
            "tool_spec": None,
            "agent_spec": None,
            "annotations": {},
        },
    )()


# ───────────────────────────────────────────────────────────────────────────


def _dedup_errors(errors: list[PartialError], top_error: PartialError) -> None:
    """Drop the generic top-level error if a located error reports the same thing.

    The top-level ``exc`` has no line; the per-connect replay re-raises the same
    exception at the known line. Prefer the located one.
    """
    if top_error.line is not None:
        return
    for e in errors:
        if e is top_error:
            continue
        if e.line is not None and e.message == top_error.message:
            try:
                errors.remove(top_error)
            except ValueError:
                pass
            return


def _parse_endpoint(expr: str) -> tuple[str | None, str | None]:
    """Split a ``node.port`` connect endpoint into ``(node, port)``.

    ``"cp_report.X"`` -> ``("cp_report", "X")``. The node is the first segment;
    everything after the first dot is the port (covers the ``node.port`` form).
    """
    expr = (expr or "").strip()
    if "." not in expr:
        return (expr or None, None)
    node, _, port = expr.partition(".")
    return (node or None, port or None)


def _port_names(node: dict[str, Any]) -> set[str]:
    return {
        str(p.get("name"))
        for p in node.get("ports", [])
        if isinstance(p, dict) and p.get("name")
    }


def _port_id(node: dict[str, Any], port_name: str) -> str:
    """Build a port id matching ``export_graph_json`` (``port:<node_id>:<name>``)."""
    return f"port:{node.get('id')}:{port_name}"


def _find_port(node: dict[str, Any], port_name: str) -> dict[str, Any] | None:
    for p in node.get("ports", []):
        if isinstance(p, dict) and str(p.get("name")) == port_name:
            return p
    return None


def _add_phantom_port(
    node: dict[str, Any], port_name: str, direction: str, message: str
) -> dict[str, Any]:
    """Add (or return existing) a phantom port to ``node`` matching the export
    port shape. Carries its own errored metadata when other ports carry metadata.
    """
    existing = _find_port(node, port_name)
    if existing is not None:
        return existing
    ports = node.setdefault("ports", [])
    if not isinstance(ports, list):
        ports = []
        node["ports"] = ports
    phantom: dict[str, Any] = {
        "id": _port_id(node, port_name),
        "name": port_name,
        "direction": direction,
        "type": "any",
        "role": "data",
        "isErrored": True,
        "errorMessage": message,
        "diagnostics": [
            {"severity": "error", "code": "unknown_port", "message": message}
        ],
    }
    ports.append(phantom)
    return phantom


def _draw_broken_edge(
    container: dict[str, Any],
    from_node: dict[str, Any],
    from_port_name: str,
    to_node: dict[str, Any],
    to_port_name: str,
    scope_id: str | None,
    message: str,
) -> None:
    """Add a broken edge between two (real or phantom) ports, matching the export
    edge shape exactly, with errored ``meta``. Updates the owning scope's edge
    list if subgraphs are exported. No-op if such an edge already exists.
    """
    from_port = _port_id(from_node, from_port_name)
    to_port = _port_id(to_node, to_port_name)
    edge_id = f"edge:{from_port}->{to_port}"

    edges = container.get("edges")
    if not isinstance(edges, list):
        return
    for e in edges:
        if isinstance(e, dict) and e.get("id") == edge_id:
            return  # don't duplicate an edge that already exists

    edges.append(
        {
            "id": edge_id,
            "from": from_port,
            "to": to_port,
            "scope": scope_id,
            "fromNode": from_node.get("id"),
            "toNode": to_node.get("id"),
            "source": None,
            "meta": {
                "isErrored": True,
                "errorMessage": message,
                "diagnostics": [
                    {"severity": "error", "code": "unknown_port", "message": message}
                ],
            },
        }
    )

    # Keep subgraph edge lists in sync (the node is already listed there).
    subgraphs = container.get("subgraphs")
    if isinstance(subgraphs, list) and scope_id is not None:
        for sg in subgraphs:
            if isinstance(sg, dict) and sg.get("id") == scope_id:
                sg_edges = sg.setdefault("edges", [])
                if isinstance(sg_edges, list) and edge_id not in sg_edges:
                    sg_edges.append(edge_id)
                break


def _apply_node_flags(
    data: dict[str, Any],
    failed_connects: list[dict[str, Any]],
    errors: list[PartialError],
    file_path: str | None,
) -> None:
    """Paint the node behind each failed connect red, render the dropped edge as
    a broken edge, and demote the file-level error to an element diagnostic.

    For a failed ``connect(a.x, b.y)`` we identify the endpoint whose port is not
    a real port of its node (using the already-exported real ports) and attach a
    ``meta.diagnostics`` entry to that node (so the diagram marks it errored and
    shows the message on hover). We then re-create the dropped connection as a
    *broken* edge (``meta.isErrored``) anchored on the real source/target ports,
    synthesizing a phantom port for whichever endpoint's port name is invalid so
    the edge has something to land on. The matching file-level error is removed
    so the same failure is not reported twice — the element now carries it,
    located at the connect line for the Problems panel.
    """
    # export_graph_json nests the graph under a "graph" key ({version, graph:{nodes,…}}).
    _graph_val = data.get("graph")
    container = _graph_val if isinstance(_graph_val, dict) else data
    nodes = container.get("nodes")
    if not isinstance(nodes, list) or not failed_connects:
        return
    node_by_label: dict[str, dict[str, Any]] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        label = n.get("label")
        if isinstance(label, str) and label:
            node_by_label[label] = n

    for fc in failed_connects:
        message = str(fc.get("message") or "Invalid connection")
        line = fc.get("line")

        from_name, from_port = _parse_endpoint(str(fc.get("from") or ""))
        to_name, to_port = _parse_endpoint(str(fc.get("to") or ""))
        from_node = node_by_label.get(from_name) if from_name else None
        to_node = node_by_label.get(to_name) if to_name else None

        from_bad = (
            from_node is not None
            and bool(from_port)
            and from_port not in _port_names(from_node)
        )
        to_bad = (
            to_node is not None
            and bool(to_port)
            and to_port not in _port_names(to_node)
        )

        # The "culprit" node (carries the node-level error flag) is whichever
        # endpoint's port name is not among its real ports — preferring the
        # target endpoint when both look bad.
        culprit_node: dict[str, Any] | None = None
        if to_bad:
            culprit_node = to_node
        elif from_bad:
            culprit_node = from_node

        if culprit_node is not None:
            meta = culprit_node.setdefault("meta", {})
            if not isinstance(meta, dict):
                meta = {}
                culprit_node["meta"] = meta
            meta.setdefault("diagnostics", []).append(
                {"severity": "error", "code": "unknown_port", "message": message}
            )
            meta["isErrored"] = True
            meta["errorMessage"] = message
            if file_path is not None and isinstance(line, int):
                # Top-level location for the Problems panel (jump to the connect line).
                culprit_node["location"] = {"file": file_path, "line": line, "column": 1}

        # Render the dropped connection as a broken edge, if both endpoints'
        # NODES exist (we can't anchor an edge on a missing node).
        if from_node is not None and to_node is not None and from_port and to_port:
            scope_id = from_node.get("scope") or to_node.get("scope")
            if to_bad:
                _add_phantom_port(to_node, to_port, "in", message)
            if from_bad:
                _add_phantom_port(from_node, from_port, "out", message)
            _draw_broken_edge(
                container,
                from_node,
                from_port,
                to_node,
                to_port,
                scope_id,
                message,
            )

        # Demote the matching file-level error — the element/edge carries it now.
        if culprit_node is not None or (from_node is not None and to_node is not None):
            for e in list(errors):
                if e.line == line and e.message == message:
                    errors.remove(e)
                    break


def _finalize_graph(
    graph: WorkflowGraph,
    errors: list[PartialError],
    partial: bool,
    failed_connects: list[dict[str, Any]] | None = None,
    file_path: str | None = None,
) -> dict[str, Any]:
    data = export_graph_json(graph)
    if failed_connects:
        _apply_node_flags(data, failed_connects, errors, file_path)
    data["partial"] = partial
    data["errors"] = [
        {"message": e.message, "file": e.file, "line": e.line, "column": e.column}
        for e in errors
    ]
    return data
