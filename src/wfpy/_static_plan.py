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

from wfpy._drawable_source import drawable_source, fault_owner, owner_of
from wfpy._graph_export import export_graph_json
from wfpy._project_index import ProjectIndex, _decorator_name, declared_as, declared_ports
from wfpy._source_repair import SourceFault
from wfpy.graph import ActorRecord, Connection, ControlPortInstance, WorkflowGraph

__all__ = ["may_hold_workflow", "static_graph_document"]

# The decorator a class is written under → the kind the runtime gives it.
_RUNTIME_KIND = {
    "task": "internal",
    "tool": "external",
    "agent": "agent",
    "viewer": "viewer",
    "source": "source",
    "streamblocks": "streamblocks",
}
# What the runtime says of a node of that kind beside the kind.
_KIND_FLAGS = {"viewer": "viewer", "source": "external", "streamblocks": "external"}
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
    except (cst.ParserSyntaxError, OSError, UnicodeDecodeError):
        return None
    try:
        tree: ast.Module | None = ast.parse(source_text)
    except (SyntaxError, ValueError):
        tree = None
    index = ProjectIndex.for_file(file_path)
    index.repair_unparsed = True
    index.use_text(file_path, source_text)
    if workflow_name is None and tree is not None:
        workflow_name = _default_workflow(tree, index, file_path)
    try:
        engine = RewriteEngine(file_path, module, source_text)
        payload = engine.export_workflow_graph(workflow=workflow_name, include_nested=False)
    except RewriteError:
        return None

    document, doc_ids = _document(payload, index, file_path)

    errors = _errors(file_path, exc, faults)
    document["partial"] = True
    document["errors"] = errors
    _add_errors_elsewhere(errors, payload)
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


def _default_workflow(tree: ast.Module, index: ProjectIndex, file_path: str) -> str | None:
    """The workflow `wfpy plan` draws when none is named (`cli._default_workflow`):
    the only one the module holds, else the only one the file writes itself,
    else the first by name. One the file imports is drawn from its own file,
    so of several the first by name that this file writes is taken."""
    own: list[str] = []
    imported: list[str] = []
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.ClassDef)):
            if any(_decorator_name(d) == "workflow" for d in statement.decorator_list):
                own.append(statement.name)
        elif isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                name = alias.asname or alias.name
                found = declared_as(index, file_path, name) if name != "*" else None
                if found is not None and found[0] == "workflow":
                    imported.append(name)
    if len(own) == 1 or (own and not imported):
        return sorted(own)[0]
    if len(set(own) | set(imported)) == 1:
        return (own or imported)[0]
    return sorted(own)[0] if own else None


def may_hold_workflow(file_path: str, name: str) -> bool:
    """Whether `file_path` can have a workflow called `name`: a function or a
    class of that name anywhere in it, or the name bound at its top (an
    assignment, an import). True when the text cannot be read to tell."""
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return True
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        drawable = drawable_source(text)
        if drawable is None:
            return True
        try:
            tree = ast.parse(drawable.text)
        except (SyntaxError, ValueError):
            return True
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound in (name, "*"):
                    return True
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(target)):
                    return True
    return False


def _document(
    payload: dict[str, Any], index: ProjectIndex, file_path: str
) -> tuple[dict[str, Any], list[str | None]]:
    """`payload` as `export_graph_json` writes a graph, and the id in it of
    each node of the payload, in the payload's order."""
    graph = WorkflowGraph(name=str(payload.get("workflow") or "workflow"))
    scopes = {"scope:root": "scope:root"}
    payload_nodes = list(payload.get("nodes", []))
    doc_ids: list[str | None] = [None] * len(payload_nodes)
    controls: dict[str, Any] = {}
    # The instances a name is bound to, in the order of the source.
    bound: dict[str, list[_Bound]] = {}
    actors: list[tuple[tuple[int, int], int, _Bound, ActorRecord]] = []
    declared: dict[int, tuple[str, dict[str, Any] | None, dict[str, str]]] = {}
    first: dict[str, int] = {}
    last: dict[str, int] = {}
    for position, node in enumerate(payload_nodes):
        first.setdefault(str(node.get("id", "")), position)
        last[str(node.get("id", ""))] = position
    made: dict[str, int] = {}

    for position, node in enumerate(payload_nodes):
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
            doc_ids[position] = name
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
            doc_ids[position] = record.node_id
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
        class_name = ref.split("(")[0].split(".")[-1] or ref
        count = made.get(class_name, 0)
        made[class_name] = count + 1
        key = name
        if last[name] != position:
            # The name is bound again further down, and the runtime names a
            # node by the name it has at the end. This one keeps the name it
            # was made with (`graph.WorkflowGraph.register_actor`).
            key = f"{class_name.lower()}_{count}" if count else class_name.lower()
        instance = _Instance(class_name, key)
        line = (node.get("location") or {}).get("line")
        binding = _Bound(
            line if isinstance(line, int) else 0,
            instance,
            {_attr(port): str(port.get("name", _attr(port))) for port in ports},
        )
        bound.setdefault(name, []).append(binding)
        actor = ActorRecord(
            instance_name=key, instance=instance, meta=_Meta(runtime_kind), scope_id=scope
        )
        # `graph.actors` in the runtime's order: a node that kept the name it
        # was made with stays where it was made, a named one is where its
        # name was first bound.
        order = (0, position) if key != name else (1, first[name])
        actors.append((order, position, binding, actor))
        declared_directions = {
            str(entry["attr"]): str(entry["declared"])
            for entry in (declared_ports(index, file_path, ref) if ref else None) or []
            if entry.get("declared")
        }
        declared[position] = (runtime_kind, defined_at, declared_directions)
        doc_ids[position] = f"node:{scope}:{key}"

    taken: set[str] = set()
    for _order, position, binding, actor in sorted(actors, key=lambda entry: entry[0]):
        key = actor.instance_name
        while key in taken:  # two nodes cannot share a name in one graph
            key += "_"
        if key != actor.instance_name:
            binding.instance._wfpy_instance_name = key
            actor.instance_name = key
            doc_ids[position] = f"node:{actor.scope_id}:{key}"
        taken.add(key)
        graph.actors[key] = actor
        graph._creation_order.append(("actor", key))

    for edge in payload.get("edges", []):
        ends: list[Any] = []
        written_at = (edge.get("location") or {}).get("line")
        for side in ("from", "to"):
            end = edge.get(side) or {}
            port = str(end.get("port", ""))
            owner = str(end.get("nodeId", ""))
            if end.get("kind") != "node":
                ends.append(port)
            elif owner in controls:
                control_node = controls[owner]
                ends.append(ControlPortInstance(control_node.node_id, control_node.name, port))
            elif owner in bound:
                binding = _bound_at(bound[owner], written_at)
                ends.append(_Port(binding.instance, binding.port_names.get(port, port)))
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
    for position, node in enumerate(payload_nodes):
        if position not in declared:
            continue
        drawn = written[str(doc_ids[position])]
        runtime_kind, defined_at, directions = declared[position]
        drawn["ports"] = [
            _port(drawn["id"], port, file_path, directions) for port in node.get("ports") or []
        ]
        meta: dict[str, Any] = {"taskKind": runtime_kind}
        if runtime_kind in _KIND_FLAGS:
            meta[_KIND_FLAGS[runtime_kind]] = True
        place = _place(node.get("location"), file_path)
        if place:
            meta["source"] = place
        if defined_at:
            meta["referencedSource"] = defined_at
        # What the static reading flagged: a class of a file that does not parse.
        flags = node.get("metadata")
        if isinstance(flags, dict) and flags.get("isErrored"):
            meta["isErrored"] = True
            meta["errorMessage"] = f"SyntaxError: {flags.get('errorMessage', '')}"
        drawn["meta"] = meta
    return document, doc_ids


class _Bound:
    """One binding of a name of the workflow to a node."""

    def __init__(self, line: int, instance: _Instance, port_names: dict[str, str]) -> None:
        self.line = line
        self.instance = instance
        # What a connection writes after the dot → the name the port has.
        self.port_names = port_names


def _bound_at(bindings: list[_Bound], line: Any) -> _Bound:
    """The node a name stands for on `line`: the last one bound above it, as
    the builder runs both branches of an `if` in the order they are written."""
    if isinstance(line, int):
        above = [binding for binding in bindings if binding.line <= line]
        if above:
            return above[-1]
    return bindings[-1]


def _attr(port: dict[str, Any]) -> str:
    """What a connection writes after the dot for a port of the payload."""
    return str(port.get("id", "")).rsplit(":p:", 1)[-1] or str(port.get("name", ""))


def _add_errors_elsewhere(errors: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    """The faults of the other files the static reading went through, each
    once: the error importing raised is often one of them."""
    seen = {(_resolved(str(error.get("file", ""))), error.get("line")) for error in errors}
    for error in payload.get("errors") or []:
        location = error.get("location") if isinstance(error, dict) else None
        if not isinstance(location, dict):
            continue
        place = (_resolved(str(location.get("file", ""))), location.get("line"))
        if place in seen:
            continue
        seen.add(place)
        errors.append(
            {
                "message": f"SyntaxError: {error.get('message', '')}",
                "file": str(location.get("file", "")),
                "line": location.get("line"),
                "column": location.get("column"),
            }
        )


def _resolved(file: str) -> str:
    try:
        return str(Path(file).resolve())
    except OSError:
        return file


def _control_kind(ports: list[dict[str, Any]]) -> str:
    """`if` or `loop` for the ports of a control construct, else nothing."""
    if not ports or any(port.get("role") != "control" for port in ports):
        return ""
    names = {port.get("name") for port in ports}
    return "loop" if "iter" in names else "if" if "cond" in names else ""


def _port(
    node_id: str, port: dict[str, Any], file_path: str, directions: dict[str, str]
) -> dict[str, Any]:
    # The runtime names a port by the name it is declared with
    # (`Port[T](name="...")`), else by its attribute, and gives the direction
    # as it is written: `inout` for one that is not.
    attr = _attr(port)
    name = str(port.get("name", attr))
    entry: dict[str, Any] = {
        "id": f"port:{node_id}:{name}",
        "name": name,
        "direction": directions.get(attr) or port.get("direction", "in"),
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
    doc_ids: list[str | None],
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
        for position, node in enumerate(payload.get("nodes", [])):
            if str(node.get("ref", "")) == name and doc_ids[position] is not None:
                _mark(nodes.get(str(doc_ids[position])), message)
        return
    if name != payload.get("workflow"):
        return
    for position, node in enumerate(payload.get("nodes", [])):
        if (node.get("location") or {}).get("line") == statement_line and node.get("ref"):
            _mark(nodes.get(str(doc_ids[position])), message)
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
