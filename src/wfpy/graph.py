"""wfpy.graph — WorkflowGraph builder and connect() wiring API."""

from __future__ import annotations

import dataclasses
from typing import Any

import inspect

from wfpy import _graph_context
from wfpy.core import TaskMeta, WorkflowDef
from wfpy.types import PortInstance

__all__ = [
    "connect",
    "if_",
    "loop",
    "WorkflowGraph",
    "Connection",
    "ControlPortInstance",
    "export_graph_json",
]


def _instance_type_name(instance: Any) -> str:
    return str(getattr(instance, "_wfpy_class_name", type(instance).__name__))


def _caller_outside_wfpy() -> dict[str, Any] | None:
    """File and line of the nearest caller that is not wfpy itself: the
    workflow line that created an instance, wherever wfpy registers it."""
    frame = inspect.currentframe()
    try:
        while frame is not None:
            module = frame.f_globals.get("__name__", "")
            if module != "wfpy" and not module.startswith("wfpy."):
                return {"file": frame.f_code.co_filename, "line": frame.f_lineno}
            frame = frame.f_back
        return None
    finally:
        del frame


@dataclasses.dataclass
class Connection:
    """A single connection (edge) between two ports."""

    from_port: PortInstance | ControlPortInstance | str  # str = workflow-level input port name
    to_port: PortInstance | ControlPortInstance | str  # str = workflow-level output port name
    scope_id: str = "scope:root"
    source: dict[str, Any] | None = None


@dataclasses.dataclass
class ControlPortInstance:
    """A reference to a control node port (if/loop)."""

    control_node_id: str
    control_name: str
    port_name: str

    def __repr__(self) -> str:
        return f"ControlPortInstance({self.control_name}.{self.port_name})"


@dataclasses.dataclass
class ActorRecord:
    """An actor instance registered in a workflow graph."""

    instance_name: str
    instance: Any  # the @task/@tool/@agent decorated object
    meta: Any  # TaskMeta from the instance
    scope_id: str = "scope:root"


@dataclasses.dataclass
class ScopeRecord:
    """A scope in the workflow graph (root, if-then, if-else, loop-body)."""

    id: str
    kind: str
    parent_id: str | None = None
    control_node_id: str | None = None


@dataclasses.dataclass
class ControlNodeRecord:
    """A control-flow node (if/loop) registered in the graph."""

    node_id: str
    kind: str  # "if" | "loop"
    name: str
    parent_scope_id: str
    condition: Any | None = None
    iterable: Any | None = None
    scopes: dict[str, str] = dataclasses.field(default_factory=dict)


class WorkflowGraph:
    """Collects actor instances and connections during workflow construction.

    The graph is populated by ``connect()`` calls (and actor instantiation)
    inside a ``@workflow``-decorated function body, while the graph is the
    active ``_graph_context._current_graph``.
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.factory_name: str | None = None
        self.factory_parameters: list[dict[str, Any]] = []
        # The declared `@workflow(inputs=, outputs=)` ports and where each is
        # written; the runner fills them from the WorkflowDef.
        self.input_names: dict[str, Any] = {}
        self.output_names: dict[str, Any] = {}
        self.port_sources: dict[str, dict[str, Any]] = {}
        self.actors: dict[str, ActorRecord] = {}
        self.connections: list[Connection] = []
        self.scopes: dict[str, ScopeRecord] = {}
        self.control_nodes: dict[str, ControlNodeRecord] = {}
        self._creation_order: list[tuple[str, str]] = []
        # Track auto-naming counters
        self._name_counters: dict[str, int] = {}
        self._control_counters: dict[str, int] = {}
        self._id_counter = 0

        root_id = "scope:root"
        self.scopes[root_id] = ScopeRecord(id=root_id, kind="root", parent_id=None)
        self._scope_stack: list[str] = [root_id]

    # ── Scope management ──────────────────────────────────────────────────

    def _new_id(self, prefix: str) -> str:
        self._id_counter += 1
        return f"{prefix}:{self._id_counter}"

    @property
    def current_scope_id(self) -> str:
        return self._scope_stack[-1]

    def push_scope(self, scope_id: str) -> None:
        if scope_id not in self.scopes:
            raise KeyError(f"Unknown scope_id: {scope_id}")
        self._scope_stack.append(scope_id)

    def pop_scope(self) -> None:
        if len(self._scope_stack) <= 1:
            raise RuntimeError("Cannot pop root scope")
        self._scope_stack.pop()

    def register_scope(
        self,
        kind: str,
        parent_id: str | None = None,
        control_node_id: str | None = None,
    ) -> str:
        scope_id = self._new_id("scope")
        self.scopes[scope_id] = ScopeRecord(
            id=scope_id,
            kind=kind,
            parent_id=parent_id,
            control_node_id=control_node_id,
        )
        return scope_id

    def register_control_node(
        self,
        kind: str,
        name: str = "",
        *,
        condition: Any | None = None,
        iterable: Any | None = None,
    ) -> ControlNodeRecord:
        parent_scope_id = self.current_scope_id
        counter = self._control_counters.get(kind, 0)
        self._control_counters[kind] = counter + 1
        node_name = name or (f"{kind}_{counter}" if counter > 0 else kind)
        node_id = self._new_id("control")
        record = ControlNodeRecord(
            node_id=node_id,
            kind=kind,
            name=node_name,
            parent_scope_id=parent_scope_id,
            condition=condition,
            iterable=iterable,
        )
        self.control_nodes[node_id] = record
        self._creation_order.append(("control", node_id))
        return record

    # ── Actor registration ───────────────────────────────────────────────

    def register_actor(self, instance: Any, explicit_name: str = "") -> str:
        """Register an actor instance and return its assigned name."""
        meta = getattr(instance, "_wfpy_meta", None)
        wf_def = getattr(instance, "_wfpy_workflow", None)

        type_name = type(instance).__name__
        if explicit_name:
            name = explicit_name
        else:
            # Auto-name: lowercase type + counter
            counter = self._name_counters.get(type_name, 0)
            self._name_counters[type_name] = counter + 1
            name = f"{type_name.lower()}_{counter}" if counter > 0 else type_name.lower()

        instance._wfpy_instance_name = name

        definition: Any = None
        if meta is not None:
            self.actors[name] = ActorRecord(
                instance_name=name,
                instance=instance,
                meta=meta,
                scope_id=self.current_scope_id,
            )
            definition = type(instance)
        elif wf_def is not None:
            self.actors[name] = ActorRecord(
                instance_name=name,
                instance=instance,
                meta=wf_def,
                scope_id=self.current_scope_id,
            )
            # A nested workflow's instance is a proxy whose class lives in
            # wfpy.core; its definition is the workflow's own (a factory's
            # inner one too).
            definition = wf_def.builder_fn or wf_def.cls
        if meta is not None or wf_def is not None:
            # Where a diagram node navigates. `_wfpy_source` is the line that
            # created the instance: the IDE resolves "go to definition" from
            # it in the file the diagram shows, so it must be a line of that
            # file. `_wfpy_definition` is what the node names.
            if getattr(instance, "_wfpy_source", None) is None:
                instance._wfpy_source = _caller_outside_wfpy()
            if getattr(instance, "_wfpy_definition", None) is None:
                try:
                    instance._wfpy_definition = {
                        "file": inspect.getsourcefile(definition) or "",
                        "line": inspect.getsourcelines(definition)[1],
                    }
                except Exception:
                    instance._wfpy_definition = None
        self._creation_order.append(("actor", name))
        return name

    def rename_actor_instance(self, instance: Any, new_name: str) -> None:
        if not new_name:
            return
        if new_name in self.actors:
            return
        old_name = None
        for name, rec in self.actors.items():
            if rec.instance is instance:
                old_name = name
                break
        if old_name is None or old_name == new_name:
            return
        rec = self.actors.pop(old_name)
        rec.instance_name = new_name
        try:
            rec.instance._wfpy_instance_name = new_name
        except Exception:
            pass
        self.actors[new_name] = rec
        updated_order: list[tuple[str, str]] = []
        for kind, name in self._creation_order:
            if kind == "actor" and name == old_name:
                updated_order.append((kind, new_name))
            else:
                updated_order.append((kind, name))
        self._creation_order = updated_order

    # ── Connection recording ─────────────────────────────────────────────

    def connect(
        self,
        source: PortInstance | ControlPortInstance | str,
        target: PortInstance | ControlPortInstance | str,
        *,
        caller_frame: Any | None = None,
    ) -> None:
        """Record a connection from *source* to *target*.

        Either endpoint can be:
        - A ``PortInstance`` (``actor.PortName``) — port on an actor instance
        - A ``str`` — workflow-level input/output port name
        """

        def _infer_name(inst: Any, frame: Any | None) -> str:
            if frame is None:
                return ""
            try:
                for key, val in frame.f_locals.items():
                    if val is inst and key and not key.startswith("_"):
                        return str(key)
            except Exception:
                return ""
            return ""

        # Auto-register actors that haven't been registered yet
        frame = inspect.currentframe()
        caller = caller_frame
        if isinstance(source, PortInstance):
            inst = source.actor_instance
            inst_name = getattr(inst, "_wfpy_instance_name", "")
            if not inst_name or inst_name not in self.actors:
                explicit = inst_name or _infer_name(inst, caller)
                self.register_actor(inst, explicit_name=explicit)
        if isinstance(target, PortInstance):
            inst = target.actor_instance
            inst_name = getattr(inst, "_wfpy_instance_name", "")
            if not inst_name or inst_name not in self.actors:
                explicit = inst_name or _infer_name(inst, caller)
                self.register_actor(inst, explicit_name=explicit)
        if frame is not None:
            del frame

        source_meta: dict[str, Any] | None = None
        frame = None
        try:
            frame = inspect.currentframe()
            # The public connect() (and `>>`) hand in their caller's frame:
            # the workflow line that wired this edge. The frame above this
            # method is wfpy's own connect().
            where = caller_frame if caller_frame is not None else (
                frame.f_back if frame is not None else None
            )
            if where is not None:
                info = inspect.getframeinfo(where)
                if info.filename and info.lineno:
                    source_meta = {"file": info.filename, "line": info.lineno}
        except Exception:
            source_meta = None
        finally:
            try:
                if frame is not None:
                    del frame
            except Exception:
                pass

        self.connections.append(
            Connection(
                from_port=source,
                to_port=target,
                scope_id=self.current_scope_id,
                source=source_meta,
            )
        )

    # ── Context manager ──────────────────────────────────────────────────

    def __enter__(self) -> WorkflowGraph:
        import wfpy._graph_context as _ctx

        self._prev_graph = _ctx._current_graph
        _ctx._current_graph = self
        return self

    def __exit__(self, *exc: Any) -> None:
        import wfpy._graph_context as _ctx

        _ctx._current_graph = self._prev_graph

    # ── Introspection ────────────────────────────────────────────────────

    def summary(self) -> str:
        """Human-readable summary of the graph."""
        lines = [f"WorkflowGraph({self.name!r}):"]
        lines.append(f"  Actors ({len(self.actors)}):")
        for name, rec in self.actors.items():
            lines.append(f"    {name}: {_instance_type_name(rec.instance)}")
        lines.append(f"  Connections ({len(self.connections)}):")
        for conn in self.connections:
            lines.append(f"    {conn.from_port} → {conn.to_port}")
        return "\n".join(lines)


def connect(
    source: PortInstance | ControlPortInstance | str,
    target: PortInstance | ControlPortInstance | str,
) -> None:
    """Connect a source port to a target port in the current workflow graph.

    Usage inside a ``@workflow`` function::

        connect("Input", d1.In)
        connect(d1.Out, d2.In)
        connect(d2.Out, "Output")

    The first/last forms use string names for workflow-level I/O ports.
    """
    if _graph_context._current_graph is None:
        raise RuntimeError(
            "connect() can only be called inside a @workflow function body "
            "(no active WorkflowGraph context)"
        )
    caller = None
    try:
        frame = inspect.currentframe()
        caller = frame.f_back if frame is not None else None
    except Exception:
        caller = None
    _graph_context._current_graph.connect(source, target, caller_frame=caller)


class _IfScope:
    def __init__(self, graph: WorkflowGraph, scope_id: str) -> None:
        self._graph = graph
        self._scope_id = scope_id

    def __enter__(self) -> _IfScope:
        self._graph.push_scope(self._scope_id)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._graph.pop_scope()


class _IfContext:
    def __init__(
        self,
        graph: WorkflowGraph,
        record: ControlNodeRecord,
        then_scope: str,
        else_scope: str,
    ) -> None:
        self._graph = graph
        self._record = record
        self.then = _IfScope(graph, then_scope)
        self.else_ = _IfScope(graph, else_scope)

    @property
    def cond(self) -> ControlPortInstance:
        return ControlPortInstance(
            control_node_id=self._record.node_id,
            control_name=self._record.name,
            port_name="cond",
        )

    @property
    def out(self) -> ControlPortInstance:
        return ControlPortInstance(
            control_node_id=self._record.node_id,
            control_name=self._record.name,
            port_name="out",
        )


class _LoopContext:
    def __init__(self, graph: WorkflowGraph, record: ControlNodeRecord, body_scope: str) -> None:
        self._graph = graph
        self._record = record
        self._body_scope = body_scope

    @property
    def iter(self) -> ControlPortInstance:
        return ControlPortInstance(
            control_node_id=self._record.node_id,
            control_name=self._record.name,
            port_name="iter",
        )

    @property
    def item(self) -> ControlPortInstance:
        return ControlPortInstance(
            control_node_id=self._record.node_id,
            control_name=self._record.name,
            port_name="item",
        )

    @property
    def out(self) -> ControlPortInstance:
        return ControlPortInstance(
            control_node_id=self._record.node_id,
            control_name=self._record.name,
            port_name="out",
        )

    def __enter__(self) -> _LoopContext:
        self._graph.push_scope(self._body_scope)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._graph.pop_scope()


def if_(condition: Any) -> _IfContext:
    """Create a control-flow if node in the current workflow graph."""
    if _graph_context._current_graph is None:
        raise RuntimeError(
            "if_() can only be called inside a @workflow function body "
            "(no active WorkflowGraph context)"
        )

    record = _graph_context._current_graph.register_control_node("if", condition=condition)
    then_scope = _graph_context._current_graph.register_scope(
        "if-then", parent_id=record.parent_scope_id, control_node_id=record.node_id
    )
    else_scope = _graph_context._current_graph.register_scope(
        "if-else", parent_id=record.parent_scope_id, control_node_id=record.node_id
    )
    record.scopes = {"then": then_scope, "else": else_scope}
    return _IfContext(_graph_context._current_graph, record, then_scope, else_scope)


def loop(iterable: Any) -> _LoopContext:
    """Create a control-flow loop node in the current workflow graph."""
    if _graph_context._current_graph is None:
        raise RuntimeError(
            "loop() can only be called inside a @workflow function body "
            "(no active WorkflowGraph context)"
        )

    record = _graph_context._current_graph.register_control_node("loop", iterable=iterable)
    body_scope = _graph_context._current_graph.register_scope(
        "loop-body", parent_id=record.parent_scope_id, control_node_id=record.node_id
    )
    record.scopes = {"body": body_scope}
    return _LoopContext(_graph_context._current_graph, record, body_scope)


from wfpy._graph_export import export_graph_json

