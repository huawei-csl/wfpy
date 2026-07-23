"""wfpy.types — Port descriptor and value types for workflow dataflow."""

from __future__ import annotations

import dataclasses
import os
from urllib.parse import urlparse
from typing import Any, TypeVar

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


def extract_ports(cls: type) -> dict[str, PortDescriptor]:
    """Extract port descriptors from a class's inner ``Ports`` class.

    Also handles bare ``PortFactory`` instances (``Port[int]`` without call parens)
    by converting them to ``PortDescriptor`` via ``_as_descriptor()``.
    """
    ports_cls = getattr(cls, "Ports", None)
    if ports_cls is None:
        return {}

    result: dict[str, PortDescriptor] = {}
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
    return result
