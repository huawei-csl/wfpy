"""The graph document of a workflow file that cannot be imported.

`wfpy plan --format graph` imports the file and asks the runtime for its
graph. A file that does not import (a misspelt name, a failing import, a
syntax fault) leaves nothing to ask, so `--best-effort` had no graph to
give: the command ended on the traceback. The sidecar's static export reads
the same file without running it, and gives its nodes, their ports and the
edges between them.

`static_graph_document` puts that reading through the runtime's own exporter
(`export_graph_json`), so the document has the ids and the shape of the one
the runtime gives for the file, and then says where the error is: at its
line, and on the nodes it belongs to. An error in the body of a task class
is on every node of that class; one on a statement of the workflow is on the
node or the edge that statement makes.

Nothing of the file is run here either.
"""

from __future__ import annotations

import ast
import traceback
from pathlib import Path
from typing import Any, cast

from wfpy._drawable_source import fault_owner, owner_of
from wfpy._graph_export import export_graph_json
from wfpy._project_index import ProjectIndex, declared_as
from wfpy._source_repair import SourceFault
from wfpy.graph import ActorRecord, Connection, ControlPortInstance, WorkflowGraph

__all__ = ["static_graph_document"]

# The decorator a class is written under → the kind the runtime gives it.
_RUNTIME_KIND = {"task": "internal", "tool": "external", "agent": "agent", "viewer": "viewer"}
_SCOPE_KINDS = {"then": "if-then", "else": "if-else", "body": "loop-body"}


class _Shown:
    """A type as the source writes it, for the exporter to show as it is."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text


class _Instance:
    """What the exporter asks of a node's instance, for one that was never made."""

    def __init__(self, class_name: str, instance_name: str) -> None:
        self._wfpy_class_name = class_name
        self._wfpy_instance_name = instance_name
        self._wfpy_workflow = None


class _Port:
    """The end of a connection on such an instance."""

    def __init__(self, instance: _Instance, port_name: str) -> None:
        self.actor_instance = instance
        self.port_name = port_name


class _Meta:
    """A node that is no `TaskMeta`: the exporter writes it without ports,
    and they are added from the source after."""

    def __init__(self, kind: str) -> None:
        self.kind = kind


def static_graph_document(
    file_path: str, workflow_name: str | None, exc: BaseException
) -> dict[str, Any] | None:
    """The document of `workflow_name` in `file_path`, read and not run.

    `exc` is what importing the file raised. The document is `partial`, and
    its `errors` are the syntax faults of the file when it has some, else
    `exc` at the line of the file it was raised from. None when the static
    export cannot draw the file: the caller then has only names to show.
    """
    import libcst as cst

    from wfpy.rewrite import RewriteEngine, RewriteError, load_drawable_module

    try:
        module, source_text, faults = load_drawable_module(file_path)
        engine = RewriteEngine(file_path, module, source_text)
        payload = engine.export_workflow_graph(workflow=workflow_name, include_nested=False)
    except (cst.ParserSyntaxError, RewriteError, OSError, UnicodeDecodeError):
        return None

    index = ProjectIndex.for_file(file_path)
    index.repair_unparsed = True
    index.use_text(file_path, source_text)
    document, doc_ids = _document(payload, index, file_path)

    errors = _errors(file_path, exc, faults)
    document["partial"] = True
    document["errors"] = errors
    try:
        tree: ast.Module | None = ast.parse(source_text)
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None and faults:
        for fault in faults:
            message = f"SyntaxError: {fault.message}"
            _flag(document, payload, doc_ids, fault_owner(tree, fault), message)
    elif tree is not None:
        raised = errors[0]
        here = str(Path(file_path).resolve())
        if raised["line"] is not None and str(Path(str(raised["file"])).resolve()) == here:
            owner = owner_of(tree, int(raised["line"]))
            _flag(document, payload, doc_ids, owner, str(raised["message"]))
    return document


# ── From the static reading to the runtime's document ──────────────────


def _document(
    payload: dict[str, Any], index: ProjectIndex, file_path: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """`payload` as `export_graph_json` writes a graph, and the id each node
    of the payload has in it."""
    graph = WorkflowGraph(name=str(payload.get("workflow") or "workflow"))
    scopes = {"scope:root": "scope:root"}
    doc_ids: dict[str, str] = {}
    controls: dict[str, Any] = {}
    instances: dict[str, _Instance] = {}
    declared: dict[str, tuple[str, dict[str, Any] | None]] = {}

    for node in payload.get("nodes", []):
        name = str(node.get("id", ""))
        kind = node.get("kind")
        ports = node.get("ports") or []
        if kind in ("wf-input", "wf-output"):
            label = str(node.get("label", ""))
            port = ports[0] if ports else {}
            names = graph.input_names if kind == "wf-input" else graph.output_names
            names[label] = _Shown(str(port.get("type", "any")))
            place = _place(port.get("location"), file_path)
            if place:
                graph.port_sources.setdefault(label, place)
            doc_ids[name] = name
            continue
        written_in = str(node.get("scope", "scope:root"))
        scope = scopes.get(written_in, "scope:root")
        control = _control_kind(ports)
        if control:
            graph.push_scope(scope)
            try:
                record = graph.register_control_node(control)
            finally:
                graph.pop_scope()
            for branch in ("then", "else") if control == "if" else ("body",):
                record.scopes[branch] = graph.register_scope(
                    _SCOPE_KINDS[branch],
                    parent_id=record.parent_scope_id,
                    control_node_id=record.node_id,
                )
                # The static export's name for the scope, as `_with_scope_id` writes it.
                inside = f"{name}:{branch}"
                if written_in == "scope:root":
                    scopes[f"scope:{inside}"] = record.scopes[branch]
                else:
                    scopes[f"{written_in}/{inside}"] = record.scopes[branch]
            controls[name] = record
            doc_ids[name] = record.node_id
            continue
        ref = str(node.get("ref", ""))
        found = declared_as(index, file_path, ref)
        runtime_kind = "internal"
        defined_at: dict[str, Any] | None = None
        if found is not None:
            decorator, definition = found
            runtime_kind = "workflow" if decorator == "workflow" else _RUNTIME_KIND.get(
                decorator, "internal"
            )
            statement = index.definition_node(definition)
            if statement is not None:
                defined_at = {"file": str(definition.file), "line": statement.lineno}
        instance = _Instance(ref.split("(")[0].split(".")[-1] or ref, name)
        instances[name] = instance
        graph.actors[name] = ActorRecord(
            instance_name=name, instance=instance, meta=_Meta(runtime_kind), scope_id=scope
        )
        graph._creation_order.append(("actor", name))
        declared[name] = (runtime_kind, defined_at)
        doc_ids[name] = f"node:{scope}:{name}"

    for edge in payload.get("edges", []):
        ends: list[Any] = []
        for side in ("from", "to"):
            end = edge.get(side) or {}
            port = str(end.get("port", ""))
            owner = str(end.get("nodeId", ""))
            if end.get("kind") != "node":
                ends.append(port)
            elif owner in controls:
                control_node = controls[owner]
                ends.append(ControlPortInstance(control_node.node_id, control_node.name, port))
            elif owner in instances:
                ends.append(_Port(instances[owner], port))
        if len(ends) != 2:
            continue  # an end on a name the workflow does not bind
        graph.connections.append(
            Connection(
                from_port=cast(Any, ends[0]),
                to_port=cast(Any, ends[1]),
                scope_id=scopes.get(str(edge.get("scope", "scope:root")), "scope:root"),
                source=_place(edge.get("location"), file_path),
            )
        )

    document = export_graph_json(graph)
    written = {str(node.get("id")): node for node in document["graph"]["nodes"]}
    for node in payload.get("nodes", []):
        name = str(node.get("id", ""))
        if name not in declared:
            continue
        drawn = written[doc_ids[name]]
        runtime_kind, defined_at = declared[name]
        drawn["ports"] = [_port(drawn["id"], port, file_path) for port in node.get("ports") or []]
        meta: dict[str, Any] = {"taskKind": runtime_kind}
        place = _place(node.get("location"), file_path)
        if place:
            meta["source"] = place
        if defined_at:
            meta["referencedSource"] = defined_at
        drawn["meta"] = meta
    return document, doc_ids


def _control_kind(ports: list[dict[str, Any]]) -> str:
    """`if` or `loop` for the ports of a control construct, else nothing."""
    if not ports or any(port.get("role") != "control" for port in ports):
        return ""
    names = {port.get("name") for port in ports}
    return "loop" if "iter" in names else "if" if "cond" in names else ""


def _port(node_id: str, port: dict[str, Any], file_path: str) -> dict[str, Any]:
    # The id goes by what a connection writes after the dot, which is the
    # end the exporter gave the edges of this node.
    attr = str(port.get("id", "")).rsplit(":p:", 1)[-1] or str(port.get("name", ""))
    entry: dict[str, Any] = {
        "id": f"port:{node_id}:{attr}",
        "name": port.get("name", attr),
        "direction": port.get("direction", "in"),
        "type": port.get("type", "any"),
        "role": port.get("role", "data"),
    }
    place = _place(port.get("location"), file_path)
    if place:
        entry["source"] = place
    return entry


def _place(location: Any, file_path: str) -> dict[str, Any] | None:
    """A place of the static reading as the runtime writes one: the resolved
    file and the line."""
    if not isinstance(location, dict) or not isinstance(location.get("line"), int):
        return None
    file = str(location.get("file") or file_path)
    return {"file": str(Path(file).resolve()), "line": location["line"]}


# ── The error, and what it belongs to ──────────────────────────────────


def _errors(
    file_path: str, exc: BaseException, faults: tuple[SourceFault, ...]
) -> list[dict[str, Any]]:
    """The errors of the document: each is a message and where it is."""
    if faults:
        return [
            {
                "message": f"SyntaxError: {fault.message}",
                "file": file_path,
                "line": fault.line,
                "column": fault.column,
            }
            for fault in faults
        ]
    file, line, column = _raised_at(exc, file_path)
    return [
        {
            "message": f"{type(exc).__name__}: {exc}",
            "file": file,
            "line": line,
            "column": column,
        }
    ]


def _raised_at(exc: BaseException, file_path: str) -> tuple[str, int | None, int | None]:
    """Where `exc` belongs: a syntax error names its own file and line, any
    other is at the last line of `file_path` it went through."""
    if isinstance(exc, SyntaxError) and exc.lineno:
        return exc.filename or file_path, exc.lineno, exc.offset or None
    here = str(Path(file_path).resolve())
    line: int | None = None
    for frame in traceback.extract_tb(exc.__traceback__):
        try:
            same = str(Path(frame.filename).resolve()) == here
        except OSError:
            same = False
        if same and frame.lineno:
            line = frame.lineno
    return file_path, line, None


def _flag(
    document: dict[str, Any],
    payload: dict[str, Any],
    doc_ids: dict[str, str],
    owner: tuple[str, str, int] | None,
    message: str,
) -> None:
    """Flag what an error belongs to (`owner_of`): the nodes of the class it
    is in, or the node or the edge of the workflow's statement it is on. The
    error stays the file's: it is reported once, where it is."""
    if owner is None:
        return
    kind, name, statement_line = owner
    nodes = {str(node.get("id")): node for node in document["graph"]["nodes"]}
    if kind == "class":
        for node in payload.get("nodes", []):
            if str(node.get("ref", "")) == name and str(node.get("id")) in doc_ids:
                _mark(nodes.get(doc_ids[str(node["id"])]), message)
        return
    if name != payload.get("workflow"):
        return
    for node in payload.get("nodes", []):
        if (node.get("location") or {}).get("line") == statement_line and node.get("ref"):
            _mark(nodes.get(doc_ids.get(str(node.get("id")), "")), message)
    drawn = [edge for edge in document["graph"]["edges"] if edge.get("source")]
    for edge in drawn:
        if edge["source"].get("line") == statement_line:
            _mark(edge, message)


def _mark(element: dict[str, Any] | None, message: str) -> None:
    if element is None:
        return
    meta = element.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        element["meta"] = meta
    meta["isErrored"] = True
    meta.setdefault("errorMessage", message)
