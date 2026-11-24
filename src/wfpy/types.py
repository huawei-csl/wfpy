"""wfpy.types — Port descriptor and value types for workflow dataflow."""

from __future__ import annotations

import dataclasses
import os
import sys
import warnings
from urllib.parse import urlparse
from typing import Any, Mapping, TypeVar

__all__ = [
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
]

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Resource, File & Map value types
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Resource:
    """Represents a locatable data token with optional metadata.

    ``path`` may point to a file, folder, URL, or opaque locator.

    Used as a port type: ``Port[Resource]`` or ``Port[Resource(ext=".md")]``.
    At runtime a Resource value is the locator string in ``path``.
    """

    path: str = ""
    ext: str = ""
    validate: list[str] = dataclasses.field(default_factory=list)
    kind: str = ""  # e.g. "file", "folder", "url", "http", "s3", ...

    # Allow Resource(".md") shorthand → Resource(ext=".md")
    def __post_init__(self) -> None:
        if self.ext == "" and self.path.startswith(".") and "/" not in self.path:
            object.__setattr__(self, "ext", self.path)
            object.__setattr__(self, "path", "")

    def __class_getitem__(cls, params: Any) -> type[Resource]:
        """Placeholder for generic subscript — returns the base class."""
        return cls

    def __str__(self) -> str:
        return self.path or f"Resource(ext={self.ext!r}, kind={self.kind!r})"


@dataclasses.dataclass(frozen=True)
class File(Resource):
    """Backwards-compatible file-focused specialization of ``Resource``.

    Used as a port type: ``Port[File]`` or ``Port[File(ext=".md")]``.
    At runtime a File value is simply the resolved path string.
    """

    kind: str = "file"

    def __str__(self) -> str:
        return self.path or f"File(ext={self.ext!r})"


@dataclasses.dataclass(frozen=True)
class Map:
    """A JSON-style dictionary value type for ports.

    At runtime a Map value is a plain ``dict``.
    """

    data: dict[str, Any] = dataclasses.field(default_factory=dict)

    def __class_getitem__(cls, params: Any) -> type[Map]:
        return cls


def infer_resource_kind(value: Resource | str) -> str:
    """Infer a normalized resource kind from a Resource or locator string.

    Returns one of the common kinds (``file``, ``folder``, ``url``, ``http``)
    when possible, or an empty string when unknown.
    """

    if isinstance(value, Resource):
        kind = value.kind.strip().lower()
        if kind:
            if kind in {"dir", "directory"}:
                return "folder"
            if kind in {"https", "http"}:
                return "http"
            return kind
        locator = value.path
    else:
        locator = str(value)

    locator = locator.strip()
    if not locator:
        return ""

    parsed = urlparse(locator)
    if parsed.scheme in {"http", "https"}:
        return "http"
    if parsed.scheme and parsed.netloc:
        return "url"
    if locator.endswith("/"):
        return "folder"

    if os.path.isdir(locator):
        return "folder"
    if os.path.isfile(locator):
        return "file"

    return ""


def is_url_resource(value: Resource | str) -> bool:
    """Return True when the value represents any URL-like locator."""

    kind = infer_resource_kind(value)
    return kind in {"url", "http"}


def is_http_resource(value: Resource | str) -> bool:
    """Return True when the value represents an HTTP(S) URL."""

    return infer_resource_kind(value) == "http"


def is_folder_resource(value: Resource | str) -> bool:
    """Return True when the value represents a folder/directory locator."""

    return infer_resource_kind(value) == "folder"


def is_file_resource(value: Resource | str) -> bool:
    """Return True when the value is explicitly a file resource."""

    kind = infer_resource_kind(value)
    if kind == "file":
        return True
    if isinstance(value, Resource):
        return value.kind.strip().lower() in {"file", "path"}
    return False


# ---------------------------------------------------------------------------
# Port descriptor
# ---------------------------------------------------------------------------

class _PortMeta(type):
    """Metaclass that makes ``Port[int]`` work as a generic subscript."""

    def __getitem__(cls, item: Any) -> PortFactory:
        return PortFactory(item)


class PortFactory:
    """Intermediate created by ``Port[T]`` — call it to produce a PortDescriptor."""

    def __init__(self, type_arg: Any) -> None:
        self._type = type_arg

    def __call__(
        self,
        name: str | None = None,
        *,
        direction: str = "inout",
        ext: str = "",
        validate: list[str] | None = None,
    ) -> PortDescriptor:
        return PortDescriptor(
            name=name,
            port_type=self._type,
            direction=direction,
            ext=ext,
            validate=validate or [],
        )

    # Allow ``Port[int]()`` with no args — __set_name__ fills in the name.
    # Also allow bare ``Port[int]`` without calling (no parens).
    def _as_descriptor(self, attr_name: str) -> PortDescriptor:
        return PortDescriptor(
            name=attr_name,
            port_type=self._type,
            direction="inout",
            ext="",
            validate=[],
        )


class Port(metaclass=_PortMeta):
    """Declarative port specification.

    Usage inside a task/workflow ``Ports`` inner class::

        class Ports:
            In  = Port[int]()           # name defaults to "In"
            Out = Port[int]("Output")   # explicit name "Output"
            report = Port[File](direction="out", ext=".md")
    """

    pass


# ---------------------------------------------------------------------------
# PortDescriptor — the actual descriptor stored on a Ports class
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class PortDescriptor:
    """Fully-resolved port metadata.  Created by ``Port[T](...)``."""

    name: str | None
    port_type: Any
    direction: str  # "in" | "out" | "inout"
    ext: str
    validate: list[str]

    # Set automatically by __set_name__ on the Ports inner class
    attr_name: str = ""
    source_file: str | None = None
    source_line: int | None = None
    # A port with no stated direction is warned of once, however many classes
    # share its declaration and however often their ports are read.
    _direction_warned: bool = dataclasses.field(
        default=False, init=False, repr=False, compare=False
    )

    def __set_name__(self, owner: type, name: str) -> None:
        self.attr_name = name
        if self.name is None:
            self.name = name

    def __repr__(self) -> str:
        return f"PortDescriptor({self.name!r}, type={self.port_type}, dir={self.direction})"


# ---------------------------------------------------------------------------
# PortInstance — runtime reference to an actor's port (for connect())
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class PortInstance:
    """A reference to a specific port on a specific actor instance.

    Created at runtime when you access ``actor_instance.PortName`` inside a
    ``@workflow`` function.
    """

    actor_instance: Any  # ActorInstance (forward ref to avoid circular)
    port_descriptor: PortDescriptor

    @property
    def port_name(self) -> str:
        return self.port_descriptor.name or self.port_descriptor.attr_name

    def __repr__(self) -> str:
        actor_name = getattr(self.actor_instance, "_wfpy_instance_name", "?")
        return f"PortInstance({actor_name}.{self.port_name})"

    # Support >> operator for pipeline syntax
    def __rshift__(self, other: PortInstance | str) -> PortInstance:
        from wfpy import _graph_context

        # Read through the module: `_current_graph` is rebound on scope entry/exit,
        # so a `from ... import` would capture a stale snapshot.
        current_graph = _graph_context._current_graph
        if current_graph is None:
            raise RuntimeError(">> operator can only be used inside a @workflow function")
        frame = None
        try:
            import inspect

            frame = inspect.currentframe()
        except Exception:
            frame = None
        caller = frame.f_back if frame is not None else None
        current_graph.connect(self, other, caller_frame=caller)
        if frame is not None:
            del frame
        return other if isinstance(other, PortInstance) else self


def extract_ports(
    cls: type, taken_for: Mapping[str, str] | None = None
) -> dict[str, PortDescriptor]:
    """Extract port descriptors from a class's inner ``Ports`` class.

    Also handles bare ``PortFactory`` instances (``Port[int]`` without call parens)
    by converting them to ``PortDescriptor`` via ``_as_descriptor()``.

    A port declared without ``direction="in"`` or ``direction="out"`` is
    warned of. ``taken_for`` is given by a caller that does not read the side
    of such a port from its name (a workflow class with ``inputs=`` or
    ``outputs=``): the side, ``in`` or ``out``, of each port it has one for.
    """
    ports_cls = getattr(cls, "Ports", None)
    if ports_cls is None:
        return {}

    result: dict[str, PortDescriptor] = {}
    # The `Ports` class that writes each name, read before a bare `Port[T]` is
    # replaced below: a `class Ports(Base.Ports)` holds the ports of its base.
    # `Ports` need not be a class (a namespace of ports is read too).
    written_in: dict[str, type] = {
        name: klass
        for klass in reversed(getattr(ports_cls, "__mro__", ()))
        for name in vars(klass)
    }
    source_map: dict[str, tuple[str, int]] = {}
    try:
        import inspect

        src_lines, src_start = inspect.getsourcelines(ports_cls)
        for idx, line in enumerate(src_lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                left = stripped.split("=", 1)[0].strip()
                if left.isidentifier():
                    source_map[left] = (
                        inspect.getsourcefile(ports_cls) or "",
                        src_start + idx,
                    )
    except Exception:
        source_map = {}
    ordered: list[str] = []
    if source_map:
        ordered = [
            name for name, _ in sorted(source_map.items(), key=lambda item: item[1][1])
        ]
    for attr_name in dir(ports_cls):
        if attr_name in ordered:
            continue
        ordered.append(attr_name)

    for attr_name in ordered:
        if attr_name.startswith("_"):
            continue
        val = getattr(ports_cls, attr_name)
        if isinstance(val, PortDescriptor):
            if val.name is None:
                val.name = attr_name
            val.attr_name = attr_name
            if attr_name in source_map:
                val.source_file, val.source_line = source_map[attr_name]
            result[attr_name] = val
        elif isinstance(val, PortFactory):
            desc = val._as_descriptor(attr_name)
            if attr_name in source_map:
                desc.source_file, desc.source_line = source_map[attr_name]
            result[attr_name] = desc
            setattr(ports_cls, attr_name, desc)  # replace factory with descriptor
    for attr_name, desc in result.items():
        if desc.direction in ("in", "out") or desc._direction_warned:
            continue
        name = desc.name or attr_name
        _warn_port_without_direction(
            cls,
            written_in.get(attr_name, ports_cls),
            attr_name,
            desc,
            port_side_by_name(name) if taken_for is None else taken_for.get(name),
            "by its name" if taken_for is None else "by the workflow's inputs= and outputs=",
        )
        desc._direction_warned = True
    return result


# A port of no stated direction is an output by these names and an input by
# any other (`core._infer_direction`).
_OUTPUT_PORT_NAMES = ("out", "output", "result", "report", "summary")


def port_side_by_name(name: str) -> str:
    """``out`` or ``in``: the side wfpy takes a port named ``name`` for when
    its declaration states neither."""
    return "out" if name.lower() in _OUTPUT_PORT_NAMES else "in"


def port_without_direction_notice(
    where: str, side: str | None, by: str = "by its name"
) -> str:
    """What wfpy says of a port declared without ``direction="in"`` or
    ``direction="out"``. ``where`` names the declaration (``Up.Ports.Out``),
    ``side`` is what wfpy takes the port for, ``in`` or ``out``, and ``by``
    how it comes to that; with no side, no direction is advised. The
    runtime's warning and the sidecar's are this one text."""
    text = (
        f'{where}: a port declared without direction="in" or direction="out" '
        "is deprecated, and wfpy 2.0 will refuse it."
    )
    if side in ("in", "out"):
        taken = "an output" if side == "out" else "an input"
        text += f' wfpy takes this one for {taken}, {by}: write direction="{side}" to keep that.'
    return text


def _port_line(holder: object, attr_name: str) -> tuple[str, int] | None:
    """The file and line at which the class ``holder`` assigns ``attr_name``
    in its own body, read from its source. ``None`` when that cannot be read."""
    try:
        import ast
        import inspect
        import textwrap

        file = inspect.getsourcefile(holder)  # type: ignore[arg-type]
        lines, start = inspect.getsourcelines(holder)  # type: ignore[arg-type]
        tree = ast.parse(textwrap.dedent("".join(lines)))
    except Exception:
        return None
    if not file or not tree.body or not isinstance(tree.body[0], ast.ClassDef):
        return None
    for stmt in tree.body[0].body:
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == attr_name for t in targets):
            return file, start + stmt.lineno - 1
    return None


_WFPY_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep


def _warn_port_without_direction(
    cls: type,
    holder: object,
    attr_name: str,
    desc: PortDescriptor,
    side: str | None,
    by: str,
) -> None:
    """Warn of a port whose direction is not stated, at the line that
    declares it. ``holder`` is what writes the port: the ``Ports`` of ``cls``,
    the one that extends, or whatever else ``cls.Ports`` is.

    A ``FutureWarning`` and not a ``DeprecationWarning``: Python shows the
    second only for code run as ``__main__``, and a workflow file is loaded by
    ``wfpy run``, so its author would not see it."""
    qualname = str(getattr(holder, "__qualname__", ""))
    if qualname.endswith(".Ports"):
        # `Up.Ports` is `Up`, and `f.<locals>.Up.Ports` too.
        where = f"{qualname.split('.')[-2]}.Ports.{attr_name}"
    elif isinstance(holder, type):
        # A class given as `Ports = Shared`: the port is `Shared.A`.
        where = f"{holder.__name__}.{attr_name}"
    else:
        where = f"{cls.__name__}.Ports.{attr_name}"
    message = port_without_direction_notice(where, side, by)

    at = _port_line(holder, attr_name)
    if at is None and desc.source_file and desc.source_line:
        at = (desc.source_file, desc.source_line)
    module_name = str(getattr(holder, "__module__", None) or cls.__module__)
    if at is None:
        # No source to read (a class written in a string that is executed):
        # the place the class is being defined at, outside wfpy.
        frame = sys._getframe(1)
        while frame is not None and os.path.abspath(frame.f_code.co_filename).startswith(
            _WFPY_DIR
        ):
            frame = frame.f_back  # type: ignore[assignment]
        at = (frame.f_code.co_filename, frame.f_lineno) if frame is not None else ("<unknown>", 1)
        module_name = cls.__module__
    # The registry of the module the line is in, where a warning raised on
    # that line would be counted. A file that is run and not imported has none.
    module_globals = getattr(sys.modules.get(module_name), "__dict__", None)
    warnings.warn_explicit(
        message,
        FutureWarning,
        at[0],
        at[1],
        module=module_name,
        registry=(
            module_globals.setdefault("__warningregistry__", {})
            if isinstance(module_globals, dict)
            else None
        ),
    )
