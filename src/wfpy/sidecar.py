"""wfpy.sidecar — libcst-based rewrite service for diagram edits.

Implements a JSONL protocol over stdin/stdout so editors can issue semantic
graph edits (create node, connect, wrap in if/loop, etc.) and receive file
rewrite results.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Any

import libcst as cst

from wfpy.package_exports import apply_package_export, find_package_export
from wfpy.rewrite import (
    RewriteEngine,
    RewriteError,
    _atomic_write_if_unchanged,
    _source_revision,
    load_module,
)

__all__ = ["run_sidecar"]

MAX_PARAMETER_TEXT_LENGTH = 4096

# Canonical sidecar-contract version (see workflow-ide/docs/sidecar-contract-v2.md).
PROTOCOL_VERSION = 2

# Canonical op aliases → the bare op name this sidecar dispatches on. The IDE speaks
# canonical names; we keep the legacy wfpy name as the dispatch key so nothing breaks.
_OP_ALIASES = {
    "getCapabilities": "protocolContracts",
    "exportGraph": "exportWorkflowGraph",
}

# Query ops (read-only): bare op name → callable(engine, args) -> diagnostic dict.
_QUERY_OPS: dict[str, Callable[[RewriteEngine, dict[str, Any]], dict[str, Any]]] = {
    "listTaskTypes": lambda engine, args: {"types": engine.list_task_types(**args)},
    "listInstanceNames": lambda engine, args: {"names": engine.list_instance_names(**args)},
    "listWorkflowTypes": lambda engine, args: {"types": engine.list_workflow_types()},
    "exportWorkflowGraph": lambda engine, args: {"graph": _export_graph(engine, args)},
}


def _export_graph(engine: RewriteEngine, args: dict[str, Any]) -> dict[str, Any]:
    """Export the workflow graph and additively flag bad edges (partial-graph contract
    v2). A clean graph is returned byte-identical to the bare export — no `partial`,
    `errors`, or `meta.diagnostics` keys are added when nothing is wrong."""
    workflow = args.get("workflow")
    graph = engine.export_workflow_graph(workflow=workflow)
    _annotate_graph_diagnostics(engine, graph, str(workflow) if workflow else None)
    return graph

# Mutating ops: bare op name → RewriteEngine method name. Each calls the method then save().
_MUTATION_OPS = {
    "createNode": "create_node",
    "createPort": "create_port",
    "createTaskType": "create_task_type",
    "createWorkflowType": "create_workflow_type",
    "connect": "connect",
    "deleteNode": "delete_node",
    "deleteEdge": "delete_edge",
    "deletePort": "delete_port",
    "updateNodeParameter": "update_node_parameter",
    "updateDefinitionAnnotation": "update_definition_annotation",
    "mergeDefinitionAnnotationArgs": "merge_definition_annotation_args",
    "removeDefinitionAnnotation": "remove_definition_annotation",
    "updateDefinitionParameter": "update_definition_parameter",
    "createEntityPort": "create_entity_port",
    "deleteEntityPort": "delete_entity_port",
    "renameNode": "rename_node",
    "renamePort": "rename_port",
    "updatePortType": "update_port_type",
    "createIf": "create_if",
    "createLoop": "create_loop",
    "wrapInIf": "wrap_in_if",
    "wrapInLoop": "wrap_in_loop",
    "unwrapControl": "unwrap_control",
    "moveNode": "move_node",
}

# Legacy bare op name → canonical name advertised in getCapabilities.supportedOps.
_BARE_TO_CANONICAL = {"exportWorkflowGraph": "exportGraph"}

# supportedOps is GENERATED from the dispatch tables so it can never drift from what is
# actually handled. Always includes the discovery + checkConnection ops.
SUPPORTED_OPS = sorted(
    {"getCapabilities", "checkConnection"}
    | {_BARE_TO_CANONICAL.get(name, name) for name in _QUERY_OPS}
    | {_BARE_TO_CANONICAL.get(name, name) for name in _MUTATION_OPS}
)

# Declared feature flags (see contract v2). wfpy is the source-truth reference for edits and
# owns control-flow ops. Stable graph ids + a checkConnection preflight are available; per
# element source locations remain a follow-up.
FEATURES = {
    "sourceTruthEdits": True,
    "stableIds": True,
    "sourceLocations": True,
    "checkConnection": True,
    "controlFlow": True,
    "layout": False,
    "expectedRevision": True,
    "partialGraph": True,
}

SIDECAR_CONTRACTS = {
    "sidecarOperation": "wf.sidecar-op/v1",
    "runEvents": "wf.run-events/v1",
}


def _capabilities_response() -> "SidecarResponse":
    return SidecarResponse(
        status="ok",
        file="",
        revision="",
        message="ok",
        diagnostic={
            "protocolVersion": PROTOCOL_VERSION,
            "supportedOps": SUPPORTED_OPS,
            "features": FEATURES,
            "contracts": SIDECAR_CONTRACTS,
        },
    )


def _current_revision(file_path: str) -> str:
    """Best-effort content-hash of the on-disk source (for error envelopes)."""
    try:
        return _normalize_revision(
            hashlib.sha256(Path(file_path).read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        )
    except Exception:
        return ""


def _endpoint_to_expr(value: Any) -> str | None:
    """Map a connection endpoint (canonical id, or a raw source expression) to the
    Python expression written into `connect(...)`.

    - `n:<node>:p:<port>`  → `<node>.<port>`
    - `wf:<port>` / `wf:in:<port>` → `"<port>"` (string-boundary form)
    - anything else is assumed to already be an expression (e.g. `task1.Out`).
    """
    s = str(value).strip()
    if not s:
        return None
    if s.startswith("n:") and ":p:" in s:
        # The delimiter is the LAST ":p:" (node/port are colon-free identifiers).
        node_id, _, port = s.rpartition(":p:")
        node = node_id[2:]
        return f"{node}.{port}"
    if s.startswith("wf:"):
        return json.dumps(s.rpartition(":p:")[2] if ":p:" in s else s.split(":")[-1])
    return s


def _normalize_connect_args(args: dict[str, Any]) -> dict[str, Any]:
    """Accept canonical (`sourceId`/`targetId`) and shorthand (`source`/`target`)
    connection args in addition to the native `from_expr`/`to_expr`/`from_port`/
    `to_port`, normalizing to what `RewriteEngine.connect` expects."""
    out = dict(args)
    if out.get("from_expr") is None and out.get("from_port") is None:
        raw = out.get("sourceId", out.get("source"))
        expr = _endpoint_to_expr(raw) if raw is not None else None
        if expr is not None:
            out["from_expr"] = expr
    if out.get("to_expr") is None and out.get("to_port") is None:
        raw = out.get("targetId", out.get("target"))
        expr = _endpoint_to_expr(raw) if raw is not None else None
        if expr is not None:
            out["to_expr"] = expr
    for key in ("sourceId", "targetId", "source", "target"):
        out.pop(key, None)
    return out


def _scope_is_ancestor(a: str, b: str) -> bool:
    """True if scope `a` contains scope `b` (root contains all; same is contained)."""
    return a == "scope:root" or b == a or b.startswith(a + "/")


def _scopes_compatible(a: str, b: str) -> bool:
    """A connection is allowed when its endpoints share a scope or one nests the other;
    siblings (e.g. an if's `then` vs `else`) are incompatible."""
    return a == b or _scope_is_ancestor(a, b) or _scope_is_ancestor(b, a)


def _validate_connection(
    engine: RewriteEngine, workflow: str | None, src_expr: str, dst_expr: str
) -> tuple[tuple[str, str, dict[str, Any]] | None, str]:
    """Validate a connection over the resolved graph: existence → scope → direction →
    type. Returns `((code, message, details) | None, semanticValidation)` — the first
    element is the rejection (or None if valid); checks degrade to existence-only when a
    port's `class Ports` metadata can't be resolved. Shared by the `checkConnection`
    dry-run and the actual `connect` mutation so both enforce the same rules."""

    def _node_of(expr: str) -> str | None:
        return expr.split(".", 1)[0] if "." in expr else None

    instances = set(engine.list_instance_names(workflow=workflow) if workflow else engine.list_instance_names())
    for expr in (src_expr, dst_expr):
        node = _node_of(expr)
        if node is not None and node not in instances:
            return ("parse_error", f"unknown node: {node}", {"endpoint": expr}), "existence"

    # Scope: reject incompatible control-flow scopes (e.g. an if's `then` vs `else`);
    # same-or-nested is allowed.
    scopes = engine.instance_scopes(workflow=workflow) if workflow else engine.instance_scopes()
    src_scope = scopes.get(_node_of(src_expr) or "")
    dst_scope = scopes.get(_node_of(dst_expr) or "")
    if src_scope and dst_scope and not _scopes_compatible(src_scope, dst_scope):
        return (
            "cross_scope_disallowed",
            "cross-scope connection is not allowed",
            {"sourceScope": src_scope, "targetScope": dst_scope},
        ), "scope"

    types = engine.instance_types(workflow=workflow)

    def _meta(expr: str) -> dict[str, str] | None:
        if "." not in expr:
            return None
        node, port = expr.split(".", 1)
        type_name = types.get(node)
        return engine.port_metadata(type_name, port) if type_name else None

    src_meta, dst_meta = _meta(src_expr), _meta(dst_expr)
    validation = "type" if (src_meta and dst_meta) else "existence"

    if src_meta and src_meta.get("direction") not in ("out", "inout"):
        return ("invalid_source_direction", "source port is not an output", {"endpoint": src_expr}), validation
    if dst_meta and dst_meta.get("direction") not in ("in", "inout"):
        return ("invalid_target_direction", "target port is not an input", {"endpoint": dst_expr}), validation
    if src_meta and dst_meta:
        st, dt = src_meta.get("type", ""), dst_meta.get("type", "")
        if st and dt and st != dt and st != "Any" and dt != "Any":
            return ("type_mismatch", "port type mismatch", {"sourceType": st, "targetType": dt}), validation

    return None, validation


def _edge_endpoint_expr(endpoint: Any) -> str | None:
    """Map a graph edge endpoint dict (`{kind, nodeId, port, ...}`) to the
    `node.port` expression `_validate_connection` consumes. Returns None for
    workflow-boundary endpoints (no node.port to validate against `class Ports`)."""
    if not isinstance(endpoint, dict):
        return None
    if endpoint.get("kind") != "node":
        return None
    node_id = str(endpoint.get("nodeId", "")).strip()
    port = str(endpoint.get("port", "")).strip()
    if not node_id or not port:
        return None
    return f"{node_id}.{port}"


def _annotate_graph_diagnostics(
    engine: RewriteEngine, graph: dict[str, Any], workflow: str | None
) -> None:
    """Additively flag bad edges in an exported graph (partial-graph contract v2).

    For each edge whose endpoints are `node.port` expressions, re-run the shared
    `_validate_connection`; if it rejects the edge, attach `meta.diagnostics` /
    `isErrored` / `errorMessage` to THAT edge (without dropping it or touching any
    existing edge field). Sets `graph["partial"] = True` only when at least one
    element or file diagnostic exists. Purely additive: a clean graph is untouched."""
    wf = workflow or graph.get("workflow")
    wf = str(wf) if wf else None
    has_diagnostic = False

    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        src_expr = _edge_endpoint_expr(edge.get("from"))
        dst_expr = _edge_endpoint_expr(edge.get("to"))
        if not src_expr or not dst_expr:
            continue
        try:
            err, _ = _validate_connection(engine, wf, src_expr, dst_expr)
        except Exception:
            continue
        if err is None:
            continue
        # The "unknown node" existence rejection is a parse_error code that doesn't map
        # to an element-level code; only the connection codes (type/direction/scope/port)
        # are element diagnostics, so skip a bare existence failure here.
        code, message, _details = err
        if code == "parse_error":
            continue
        meta = edge.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            edge["meta"] = meta
        meta.setdefault("diagnostics", []).append(
            {"severity": "error", "code": code, "message": message}
        )
        meta["isErrored"] = True
        meta.setdefault("errorMessage", message)
        has_diagnostic = True

    if has_diagnostic:
        graph["partial"] = True


def _syntax_error_graph(file_path: str, exc: cst.ParserSyntaxError) -> dict[str, Any]:
    """Build a partial-graph payload for an un-parseable file: empty nodes/edges plus a
    file-level `syntax_error` diagnostic (contract v2). The IDE shows a file-level
    overlay instead of collapsing to a bare `status: error`."""
    line = getattr(exc, "editor_line", None) or getattr(exc, "raw_line", None)
    column = getattr(exc, "editor_column", None)
    if column is None:
        raw_col = getattr(exc, "raw_column", None)
        column = (raw_col + 1) if isinstance(raw_col, int) else None
    location: dict[str, Any] = {"file": file_path}
    if isinstance(line, int):
        location["line"] = line
    if isinstance(column, int):
        location["column"] = column
    return {
        "workflow": "",
        "nodes": [],
        "edges": [],
        "file": file_path,
        "partial": True,
        "errors": [
            {
                "severity": "error",
                "code": "syntax_error",
                "message": str(exc),
                "location": location,
            }
        ],
    }


def _check_connection(engine: RewriteEngine, args: dict[str, Any]) -> SidecarResponse:
    """Dry-run connection preflight (contract v2): validates existence → scope →
    direction → type and returns a preflight token. `semanticValidation` reports the
    depth reached."""
    workflow = args.get("workflow") or args.get("network")
    wf = str(workflow) if workflow else None
    src_expr = _endpoint_to_expr(args.get("sourceId", args.get("source")))
    dst_expr = _endpoint_to_expr(args.get("targetId", args.get("target")))
    file_path = engine.file_path
    rev = _normalize_revision(engine.revision)

    if not src_expr or not dst_expr:
        return SidecarResponse(
            status="error", file=file_path, revision=rev,
            message="missing source or target endpoint", diagnostic={"code": "parse_error"},
        )

    err, validation = _validate_connection(engine, wf, src_expr, dst_expr)
    if err:
        code, message, details = err
        return SidecarResponse(
            status="error", file=file_path, revision=rev, message=message,
            diagnostic={"code": code, "details": details},
        )

    token = hashlib.sha1(f"preflight:{src_expr}->{dst_expr}".encode("utf-8")).hexdigest()[:16]
    return SidecarResponse(
        status="ok",
        file=file_path,
        revision=rev,
        message="ok",
        diagnostic={
            "code": "connection_ok",
            "sourceExpr": src_expr,
            "targetExpr": dst_expr,
            "preflightToken": token,
            "semanticValidation": validation,
        },
    )


def _export_created_class(file_path: str, class_name: str) -> list[dict[str, str]] | None:
    """Re-export a freshly created class from its package, if it has one.

    Returns what the host needs to snapshot for undo — `[{file, revision}]` —
    or None when there was nothing to do: no package beside the file, no class
    name, or an `__init__.py` that already exports it.

    Deliberately forgiving. This runs AFTER the edit the caller asked for has
    been written, so a failure here must not turn a successful node creation
    into an error; the node is real either way, and the missing export is a line
    the author can add. The reason is reported through the response instead.
    """
    if not class_name:
        return None
    try:
        export = find_package_export(file_path, class_name)
        if export is None:
            return None
        with open(export.init_path, "r", encoding="utf-8") as handle:
            before = handle.read()
        after = apply_package_export(before, export)
        if after == before:
            return None
        _atomic_write_if_unchanged(
            export.init_path,
            expected_revision=_source_revision(before),
            text=after,
        )
        return [{
            "file": export.init_path,
            "revision": _normalize_revision(_source_revision(after)),
        }]
    except Exception:
        return None


def _normalize_revision(revision: str) -> str:
    if not revision:
        return ""
    return revision if revision.startswith("sha256:") else f"sha256:{revision}"


@dataclasses.dataclass
class SidecarResponse:
    status: str
    file: str
    revision: str
    message: str | None = None
    diagnostic: dict[str, Any] | None = None
    changedFiles: list[dict[str, str]] | None = None
    """Other files this op wrote, as `{file, revision}`.

    An op edits `file`, and the host snapshots exactly that file to make the
    edit undoable. When an op has to touch a second one — a package's
    `__init__.py`, so a created class is exported the way its neighbours are —
    the host has to be told, or undo restores half the change and the reader is
    left with an export pointing at a class that no longer exists.

    Additive and optional: a host that does not read it is no worse off than
    before, and no op sets it unless it was ASKED to touch a second file.
    """


def _emit_response(resp: SidecarResponse) -> None:
    sys.stdout.write(json.dumps(dataclasses.asdict(resp)) + "\n")
    sys.stdout.flush()


def _bare_op(op: str) -> str:
    """Strip the runtime prefix and resolve canonical aliases to the dispatch key."""
    bare = op.split(".", 1)[1] if "." in op else op
    return _OP_ALIASES.get(bare, bare)


def _handle_request(payload: dict[str, Any]) -> SidecarResponse:
    op = _bare_op(str(payload.get("op", "")))
    if op == "protocolContracts":
        return _capabilities_response()

    file_path = str(payload.get("file", ""))
    if not file_path:
        return SidecarResponse(
            status="error",
            file="",
            revision="",
            message="Missing 'file' in request",
            diagnostic={"code": "parse_error"},
        )

    try:
        module, source_text, revision = load_module(file_path)
        engine = RewriteEngine(file_path, module, source_text)
        args = payload.get("args", {}) or {}

        if op == "updateNodeParameter":
            new_value = str(args.get("newValue", "")).strip()
            if len(new_value) > MAX_PARAMETER_TEXT_LENGTH:
                return SidecarResponse(
                    status="error",
                    file=file_path,
                    revision=_normalize_revision(revision),
                    message="parameter expression is too large",
                    diagnostic={"code": "payload_too_large"},
                )

        if op == "updateDefinitionParameter":
            parameter_text = str(args.get("parameterText", "")).strip()
            if len(parameter_text) > MAX_PARAMETER_TEXT_LENGTH:
                return SidecarResponse(
                    status="error",
                    file=file_path,
                    revision=_normalize_revision(revision),
                    message="parameter text is too large",
                    diagnostic={"code": "payload_too_large"},
                )

        if op == "checkConnection":
            return _check_connection(engine, args)

        query = _QUERY_OPS.get(op)
        if query is not None:
            return SidecarResponse(
                status="ok",
                file=file_path,
                revision=_normalize_revision(revision),
                message="ok",
                diagnostic=query(engine, args),
            )

        method_name = _MUTATION_OPS.get(op)
        if method_name is None:
            return SidecarResponse(
                status="error",
                file=file_path,
                revision=_normalize_revision(revision),
                message=f"Unknown op: {op}",
                diagnostic={"code": "unknown_operation"},
            )

        # Optimistic concurrency (opt-in): if the client supplies expectedRevision,
        # reject the mutation when the on-disk source changed since they last read it.
        # Stripped from args so it never reaches the engine method.
        expected = args.pop("expectedRevision", None)
        if expected is not None and _normalize_revision(revision) != _normalize_revision(str(expected)):
            return SidecarResponse(
                status="error",
                file=file_path,
                revision=_normalize_revision(revision),
                message="source changed since expectedRevision",
                diagnostic={
                    "code": "concurrent_source_modification",
                    "expectedRevision": _normalize_revision(str(expected)),
                    "actualRevision": _normalize_revision(revision),
                },
            )

        # Accept canonical / shorthand connection args (sourceId/targetId/source/target).
        if op == "connect":
            args = _normalize_connect_args(args)
            # Enforce the connection preflight on the actual mutation:
            # reject a cross-scope / wrong-direction / type-mismatched edge at write time,
            # not just on the checkConnection dry-run. Only when both endpoints are exprs.
            src_expr, dst_expr = args.get("from_expr"), args.get("to_expr")
            if src_expr and dst_expr:
                workflow = args.get("workflow")
                err, _ = _validate_connection(
                    engine, str(workflow) if workflow else None, str(src_expr), str(dst_expr)
                )
                if err:
                    code, message, details = err
                    return SidecarResponse(
                        status="error",
                        file=file_path,
                        revision=_normalize_revision(revision),
                        message=message,
                        diagnostic={"code": code, "details": details},
                    )

        # OPT-IN. The host asks for this only when it can snapshot a second
        # file for undo; a host that cannot never sets it and never gets a
        # write it does not know about. That keeps the two repos free to ship
        # in either order.
        update_exports = bool(args.pop("updatePackageExports", False))
        created_name = str(args.get("name") or "") if update_exports else ""

        getattr(engine, method_name)(**args)
        engine.save()

        changed = _export_created_class(file_path, created_name) if update_exports else None
        return SidecarResponse(
            status="ok",
            file=file_path,
            revision=_normalize_revision(engine.revision),
            message="ok",
            changedFiles=changed,
        )
    except cst.ParserSyntaxError as exc:
        # Partial-graph contract v2: an un-parseable file must NOT collapse the diagram for
        # the export op — return `status: ok` with an empty, partial graph carrying a
        # file-level `syntax_error` so the IDE shows an overlay. All OTHER ops keep their
        # original error behavior (fall through to the generic internal_error envelope).
        if op == "exportWorkflowGraph":
            return SidecarResponse(
                status="ok",
                file=file_path,
                revision=_current_revision(file_path),
                message="ok",
                diagnostic={"graph": _syntax_error_graph(file_path, exc)},
            )
        return SidecarResponse(
            status="error",
            file=file_path,
            revision=_current_revision(file_path),
            message=f"Unhandled error: {exc}",
            diagnostic={"code": "internal_error"},
        )
    except RewriteError as exc:
        # Structured errors (contract v2): every error carries a machine `code`, and the
        # envelope reports the current on-disk revision so clients can detect drift.
        diag = dict(exc.diagnostic) if isinstance(exc.diagnostic, dict) else {}
        diag.setdefault("code", "rewrite_error")
        return SidecarResponse(
            status="error",
            file=file_path,
            revision=_current_revision(file_path),
            message=str(exc),
            diagnostic=diag,
        )
    except Exception as exc:
        return SidecarResponse(
            status="error",
            file=file_path,
            revision=_current_revision(file_path),
            message=f"Unhandled error: {exc}",
            diagnostic={"code": "internal_error"},
        )


def run_sidecar() -> None:
    """Run a JSONL request loop on stdin/stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit_response(
                SidecarResponse(
                    status="error",
                    file="",
                    revision="",
                    message=f"Invalid JSON: {exc}",
                )
            )
            continue

        resp = _handle_request(payload)
        _emit_response(resp)


if __name__ == "__main__":
    run_sidecar()
