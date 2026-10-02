"""Checkpoints: the state of a run between two firings, saved and restored.

Between two firings a run is fully described by its queues, each actor's
state, each control node's progress, the shared context and, recursively, the
same for every nested workflow. A run that fails writes that state to
``run.wf-checkpoint.json``; ``run(..., resume_from=...)`` builds the plan from
the current source, restores it, and carries on. See
``docs/proposals/resume.md``.

Values are written as JSON with type tags for what JSON lacks. An object of a
class of the user's is written by name, as its class and its attributes, and
read back by making the class's object and setting them -- so it survives the
class gaining a method or changing one, which is what a fix does between a
failure and its resume. Pickle is the fallback, for an object that defines its
own way of being saved, or one the attributes do not describe (a subclass of a
builtin container). A value neither can write -- a lock, an open file, an
object of a class defined inside a function -- makes the checkpoint not
resumable, and the file names it.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import enum
import hashlib
import importlib
import json
import pickle
import uuid
from pathlib import Path
from typing import Any

from wfpy.types import File, Resource

CHECKPOINT = "run.wf-checkpoint.json"
VERSION = 1
_TAG = "$wfType"


class NotResumable(ValueError):
    """A value a checkpoint cannot hold."""


# ── Values ────────────────────────────────────────────────────────────────


def encode(value: Any, where: str) -> Any:
    """*value* as JSON, tagged where JSON alone would lose what it is.

    Builtins are matched by their exact type: a subclass (a ``Counter``, a
    named tuple, an ``IntEnum``) is more than its base, and written as the
    object it is.
    """
    kind = type(value)
    if value is None or kind in (bool, int, float, str):
        return value
    if isinstance(value, File):
        return {
            _TAG: "file",
            "path": value.path,
            "ext": value.ext,
            "validate": list(value.validate),
        }
    if isinstance(value, Resource):
        return {
            _TAG: "resource",
            "path": value.path,
            "ext": value.ext,
            "validate": list(value.validate),
            "kind": value.kind,
        }
    if isinstance(value, Path):
        return {_TAG: "path", "path": str(value)}
    if kind is list:
        return [encode(item, f"{where}[{i}]") for i, item in enumerate(value)]
    if kind is tuple:
        return {
            _TAG: "tuple",
            "items": [encode(item, f"{where}[{i}]") for i, item in enumerate(value)],
        }
    if kind in (set, frozenset):
        return {
            _TAG: kind.__name__,
            "items": [encode(item, f"{where}{{}}") for item in value],
        }
    if kind is bytes:
        return {_TAG: "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if kind is dict:
        if all(type(key) is str for key in value) and _TAG not in value:
            return {key: encode(item, f"{where}.{key}") for key, item in value.items()}
        return {
            _TAG: "dict",
            "items": [
                [encode(k, f"{where}<key>"), encode(v, f"{where}[{k!r}]")] for k, v in value.items()
            ],
        }
    if isinstance(value, enum.Enum):
        return {_TAG: "enum", "type": _type_name(kind), "name": value.name}
    if kind is datetime.datetime:
        return {_TAG: "datetime", "iso": value.isoformat()}
    if kind is datetime.date:
        return {_TAG: "date", "iso": value.isoformat()}
    if kind is datetime.time:
        return {_TAG: "time", "iso": value.isoformat()}
    if kind is datetime.timedelta:
        return {_TAG: "timedelta", "seconds": value.total_seconds()}
    if kind is decimal.Decimal:
        return {_TAG: "decimal", "value": str(value)}
    if kind is uuid.UUID:
        return {_TAG: "uuid", "value": str(value)}
    if kind is complex:
        return {_TAG: "complex", "real": value.real, "imag": value.imag}
    return _encode_object(value, where)


def _type_name(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _import_type(name: str) -> Any:
    module_name, _, qualname = name.partition(":")
    target: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def _importable(cls: type) -> bool:
    """Whether the class can be found again by its name."""
    if "<locals>" in cls.__qualname__:
        return False
    try:
        return bool(_import_type(_type_name(cls)) is cls)
    except Exception:
        return False


_SAVING_HOOKS = (
    "__reduce__",
    "__reduce_ex__",
    "__getstate__",
    "__setstate__",
    "__getnewargs__",
    "__getnewargs_ex__",
)


def _described_by_attributes(cls: type) -> bool:
    """Whether setting its attributes on a bare instance rebuilds one.

    True for an ordinary class written in Python. False for a class that says
    how it is saved itself (it knows better), for one built on a builtin
    container (its contents are not attributes), and for one implemented in C
    (a lock has no attributes, and a bare one is not the lock).
    """
    # Somewhere to keep attributes: an instance __dict__, or __slots__ declared
    # in Python. A type implemented in C (a lock) has neither, whatever its
    # module says.
    keeps_attributes = cls.__dictoffset__ != 0 or any(
        "__slots__" in base.__dict__ for base in cls.__mro__ if base is not object
    )
    if not keeps_attributes:
        return False
    if any(base.__module__ == "builtins" and base is not object for base in cls.__mro__):
        return False
    return all(getattr(cls, hook, None) is getattr(object, hook, None) for hook in _SAVING_HOOKS)


def _attributes(value: Any) -> dict[str, Any]:
    state = dict(vars(value)) if hasattr(value, "__dict__") else {}
    for cls in type(value).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        for slot in [slots] if isinstance(slots, str) else slots:
            if slot not in ("__dict__", "__weakref__") and hasattr(value, slot):
                state[slot] = getattr(value, slot)
    return state


def _encode_object(value: Any, where: str) -> Any:
    cls = type(value)
    reason = ""
    if _importable(cls) and _described_by_attributes(cls):
        try:
            return {
                _TAG: "object",
                "type": _type_name(cls),
                "state": {
                    name: encode(item, f"{where}.{name}")
                    for name, item in _attributes(value).items()
                },
            }
        except NotResumable as exc:
            reason = str(exc)
    try:
        data = pickle.dumps(value)
    except Exception as exc:
        raise NotResumable(
            reason or f"{where}: a {cls.__qualname__} cannot be saved ({exc})"
        ) from exc
    return {
        _TAG: "pickle",
        "type": _type_name(cls),
        "base64": base64.b64encode(data).decode("ascii"),
    }


def decode(value: Any) -> Any:
    """The value :func:`encode` wrote."""
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    tag = value.get(_TAG)
    if tag is None:
        return {key: decode(item) for key, item in value.items()}
    if tag == "file":
        return File(
            value["path"], ext=value.get("ext", ""), validate=list(value.get("validate", []))
        )
    if tag == "resource":
        return Resource(
            value["path"],
            ext=value.get("ext", ""),
            validate=list(value.get("validate", [])),
            kind=value.get("kind", ""),
        )
    if tag == "path":
        return Path(value["path"])
    if tag == "tuple":
        return tuple(decode(item) for item in value["items"])
    if tag == "set":
        return {decode(item) for item in value["items"]}
    if tag == "frozenset":
        return frozenset(decode(item) for item in value["items"])
    if tag == "bytes":
        return base64.b64decode(value["base64"])
    if tag == "dict":
        return {decode(k): decode(v) for k, v in value["items"]}
    if tag == "enum":
        return _import_type(value["type"])[value["name"]]
    if tag == "datetime":
        return datetime.datetime.fromisoformat(value["iso"])
    if tag == "date":
        return datetime.date.fromisoformat(value["iso"])
    if tag == "time":
        return datetime.time.fromisoformat(value["iso"])
    if tag == "timedelta":
        return datetime.timedelta(seconds=value["seconds"])
    if tag == "decimal":
        return decimal.Decimal(value["value"])
    if tag == "uuid":
        return uuid.UUID(value["value"])
    if tag == "complex":
        return complex(value["real"], value["imag"])
    if tag == "object":
        cls = _import_type(value["type"])
        obj = cls.__new__(cls)
        for name, item in value["state"].items():
            # Through `object`, so a frozen dataclass or a class guarding its
            # own __setattr__ is rebuilt as it was, not refused.
            object.__setattr__(obj, name, decode(item))
        return obj
    if tag == "pickle":
        return pickle.loads(base64.b64decode(value["base64"]))
    if tag == "dataclass":  # written by the first checkpoints
        return _import_type(value["type"])(
            **{name: decode(item) for name, item in value["fields"].items()}
        )
    raise ValueError(f"unknown checkpoint value tag {tag!r}")


# ── The plan ──────────────────────────────────────────────────────────────


def graph_fingerprint(plan: Any) -> str:
    """What must not change for a checkpoint to fit: actors, kinds, queues."""
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(_graph_shape(plan), sort_keys=True).encode("utf-8")).hexdigest()
    )


def _graph_shape(plan: Any) -> dict[str, Any]:
    return {
        "actors": [
            [a.name, a.kind, type(a.instance).__name__]
            + ([_graph_shape(a.sub_plan)] if a.sub_plan is not None else [])
            for a in plan.actors
        ],
        "queues": [q.id for q in plan.all_queues],
    }


def _instance_state(actor: Any) -> dict[str, Any]:
    names = list(getattr(actor.meta, "state_fields", None) or {})
    names += [n for n in ("_wfpy_schedule_state", "_wfpy_source_emitted") if n not in names]
    return {name: getattr(actor.instance, name) for name in names if hasattr(actor.instance, name)}


def actor_state(actor: Any, enc: Any, at: str) -> dict[str, Any]:
    """An actor's own state -- not a control node's -- written with *enc*.

    Its task fields and the runtime attributes wfpy keeps on the instance, and
    an agent's budget, conversation and CLI sessions. Shared by the checkpoint
    and the firing journal, so both carry the same state.
    """
    entry: dict[str, Any] = {
        "state": {
            name: enc(value, f"{at}.{name}") for name, value in _instance_state(actor).items()
        }
    }
    if actor.agent_fire_budget is not None:
        entry["agentFireBudget"] = actor.agent_fire_budget
    if actor.chat_history:
        entry["chatHistory"] = enc(actor.chat_history, f"{at}.chatHistory")
    if actor.agent_cli_session_ids:
        entry["agentCliSessionIds"] = dict(actor.agent_cli_session_ids)
    return entry


def restore_actor_state(actor: Any, entry: dict[str, Any]) -> None:
    """Put back what :func:`actor_state` wrote."""
    for field, value in (entry.get("state") or {}).items():
        setattr(actor.instance, field, decode(value))
    if "agentFireBudget" in entry:
        actor.agent_fire_budget = entry["agentFireBudget"]
    if "chatHistory" in entry:
        actor.chat_history = decode(entry["chatHistory"])
    if "agentCliSessionIds" in entry:
        actor.agent_cli_session_ids = dict(entry["agentCliSessionIds"])


def capture(plan: Any, problems: list[str], where: str = "") -> dict[str, Any]:
    """The state of *plan* and its sub-plans; what cannot be saved goes to *problems*."""

    def enc(value: Any, at: str) -> Any:
        try:
            return encode(value, at)
        except NotResumable as exc:
            problems.append(str(exc))
            return None

    queues = [
        {
            "id": q.id,
            "tokens": [enc(token, f"{where}{q.id}[{i}]") for i, token in enumerate(list(q.items))],
        }
        for q in plan.all_queues
    ]
    actors: dict[str, Any] = {}
    for actor in plan.actors:
        at = f"{where}{actor.name}"
        entry: dict[str, Any] = {"fireCount": actor.fire_count}
        if actor.pending is not None:
            entry["pending"] = enc(actor.pending, f"{at}.pending")
        if actor.kind.startswith("control-"):
            control = {
                "initialized": actor._control_initialized,
                "conditionUsed": getattr(actor, "_control_condition_used", False),
                "iterUsed": getattr(actor, "_control_iter_used", False),
            }
            if hasattr(actor, "_loop_source"):
                control["loopSource"] = enc(actor._loop_source, f"{at}.iterable")
                control["loopIndex"] = actor._loop_index
            entry["control"] = control
        else:
            entry.update(actor_state(actor, enc, at))
        if actor.sub_plan is not None:
            entry["subPlan"] = capture(actor.sub_plan, problems, f"{at}/")
        actors[actor.name] = entry
    return {"queues": queues, "actors": actors}


def restore(plan: Any, saved: dict[str, Any]) -> None:
    """Put *saved* (from :func:`capture`) back into a freshly built *plan*."""
    by_id: dict[str, list[Any]] = {}
    for q in plan.all_queues:
        by_id.setdefault(q.id, []).append(q)
    seen: dict[str, int] = {}
    for entry in saved["queues"]:
        index = seen.get(entry["id"], 0)
        seen[entry["id"]] = index + 1
        q = by_id[entry["id"]][index]
        q.items.clear()
        q.items.extend(decode(token) for token in entry["tokens"])

    actors = {a.name: a for a in plan.actors}
    for name, entry in saved["actors"].items():
        actor = actors[name]
        actor.fire_count = entry["fireCount"]
        actor.pending = decode(entry["pending"]) if "pending" in entry else None
        control = entry.get("control")
        if control is not None:
            actor._control_initialized = control["initialized"]
            actor._control_condition_used = control["conditionUsed"]
            actor._control_iter_used = control["iterUsed"]
            if "loopSource" in control:
                source = decode(control["loopSource"])
                iterator = iter(source)
                for _ in range(control["loopIndex"]):
                    next(iterator)
                actor._loop_source = source
                actor._loop_index = control["loopIndex"]
                actor._loop_iter = iterator
        restore_actor_state(actor, entry)
        if "subPlan" in entry and actor.sub_plan is not None:
            restore(actor.sub_plan, entry["subPlan"])


# ── The file ──────────────────────────────────────────────────────────────


def write_checkpoint(
    plan: Any,
    run_out_dir: Path,
    *,
    workflow_name: str,
    source_path: str | None,
    run_id: str,
    error: dict[str, Any],
) -> Path:
    """Write ``run.wf-checkpoint.json`` for a run that stopped."""
    problems: list[str] = []
    state = capture(plan, problems)
    context = {
        "store": _safe(plan.context_store, problems),
        "journal": _safe(plan.context_journal, problems),
        "version": plan.context_version,
        "commitSeq": plan.context_commit_seq,
    }
    checkpoint: dict[str, Any] = {
        "version": VERSION,
        "runId": run_id,
        "workflowName": workflow_name,
        "sourcePath": source_path,
        "graph": graph_fingerprint(plan),
        "failed": error,
        "resumable": not problems,
        **({"notResumable": problems} if problems else {}),
        "plan": state,
        "context": context,
    }
    path = run_out_dir / CHECKPOINT
    path.write_text(json.dumps(checkpoint, indent=2))
    return path


def _safe(value: Any, problems: list[str]) -> Any:
    try:
        return encode(value, "context")
    except NotResumable as exc:
        problems.append(str(exc))
        return None


def load_checkpoint(path: str | Path) -> tuple[Path, dict[str, Any]]:
    """The checkpoint at *path* (a run directory, or the file itself)."""
    checkpoint_path = Path(path).expanduser().resolve()
    if checkpoint_path.is_dir():
        checkpoint_path = checkpoint_path / CHECKPOINT
    if not checkpoint_path.is_file():
        raise FileNotFoundError(
            f"no {CHECKPOINT} at {checkpoint_path}: only a run that failed or was "
            "stopped leaves one"
        )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if checkpoint.get("version") != VERSION:
        raise ValueError(
            f"{checkpoint_path}: checkpoint version {checkpoint.get('version')!r} is not {VERSION}"
        )
    return checkpoint_path, checkpoint


def restore_checkpoint(
    plan: Any, workflow_name: str, checkpoint_path: Path, checkpoint: dict[str, Any]
) -> None:
    """Check *checkpoint* fits *plan*, then put it back."""
    if checkpoint.get("workflowName") != workflow_name:
        raise ValueError(
            f"{checkpoint_path} is a run of {checkpoint.get('workflowName')!r}, "
            f"not {workflow_name!r}"
        )
    if not checkpoint.get("resumable", False):
        reasons = "; ".join(checkpoint.get("notResumable") or ["no reason recorded"])
        raise ValueError(f"{checkpoint_path} cannot be resumed: {reasons}")
    if checkpoint.get("graph") != graph_fingerprint(plan):
        raise ValueError(
            f"{checkpoint_path}: the workflow's graph changed since the run (its actors, "
            "their kinds or its connections). An action's code can change before a "
            "resume; its wiring cannot."
        )
    context = checkpoint.get("context") or {}
    try:
        restore(plan, checkpoint["plan"])
        plan.context_store = decode(context.get("store")) or {}
        plan.context_journal = decode(context.get("journal")) or []
    except Exception as exc:
        # A class that moved or was renamed, or a pickled value its class no
        # longer reads: the checkpoint is fine, the code no longer fits it.
        raise ValueError(
            f"{checkpoint_path}: a saved value cannot be read back with the current "
            f"code: {type(exc).__name__}: {exc}"
        ) from exc
    plan.context_version = context.get("version", plan.context_version)
    plan.context_commit_seq = context.get("commitSeq", plan.context_commit_seq)
