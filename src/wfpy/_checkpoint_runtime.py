"""Checkpoints: the state of a run between two firings, saved and restored.

Between two firings a run is fully described by its queues, each actor's
state, each control node's progress, the shared context and, recursively, the
same for every nested workflow. A run that fails writes that state to
``run.wf-checkpoint.json``; ``run(..., resume_from=...)`` builds the plan from
the current source, restores it, and carries on. See
``docs/proposals/resume.md``.

Values are written as JSON with type tags for what JSON lacks, not pickled: a
checkpoint is read back by a newer version of the source -- the version with
the fix -- and pickle breaks first exactly there. A value that cannot be
written makes the checkpoint not resumable, and the file says which.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import importlib
import json
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
    """*value* as JSON, tagged where JSON alone would lose what it is."""
    if value is None or isinstance(value, (bool, int, float, str)):
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
    if isinstance(value, list):
        return [encode(item, f"{where}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, tuple):
        return {
            _TAG: "tuple",
            "items": [encode(item, f"{where}[{i}]") for i, item in enumerate(value)],
        }
    if isinstance(value, (set, frozenset)):
        return {
            _TAG: "frozenset" if isinstance(value, frozenset) else "set",
            "items": [encode(item, f"{where}{{}}") for item in value],
        }
    if isinstance(value, bytes):
        return {_TAG: "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        if all(isinstance(key, str) for key in value) and _TAG not in value:
            return {key: encode(item, f"{where}.{key}") for key, item in value.items()}
        return {
            _TAG: "dict",
            "items": [
                [encode(k, f"{where}<key>"), encode(v, f"{where}[{k!r}]")] for k, v in value.items()
            ],
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        cls = type(value)
        if "<locals>" in cls.__qualname__:
            raise NotResumable(f"{where}: {cls.__qualname__} is defined inside a function")
        return {
            _TAG: "dataclass",
            "type": f"{cls.__module__}:{cls.__qualname__}",
            "fields": {
                f.name: encode(getattr(value, f.name), f"{where}.{f.name}")
                for f in dataclasses.fields(value)
            },
        }
    raise NotResumable(f"{where}: a {type(value).__name__} cannot be saved")


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
    if tag == "dataclass":
        module_name, _, qualname = value["type"].partition(":")
        target: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            target = getattr(target, part)
        return target(**{name: decode(item) for name, item in value["fields"].items()})
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
            entry["state"] = {
                name: enc(value, f"{at}.{name}") for name, value in _instance_state(actor).items()
            }
            if actor.agent_fire_budget is not None:
                entry["agentFireBudget"] = actor.agent_fire_budget
            if actor.chat_history:
                entry["chatHistory"] = enc(actor.chat_history, f"{at}.chatHistory")
            if actor.agent_cli_session_ids:
                entry["agentCliSessionIds"] = dict(actor.agent_cli_session_ids)
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
        for field, value in (entry.get("state") or {}).items():
            setattr(actor.instance, field, decode(value))
        if "agentFireBudget" in entry:
            actor.agent_fire_budget = entry["agentFireBudget"]
        if "chatHistory" in entry:
            actor.chat_history = decode(entry["chatHistory"])
        if "agentCliSessionIds" in entry:
            actor.agent_cli_session_ids = dict(entry["agentCliSessionIds"])
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
    error: dict[str, str],
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
    restore(plan, checkpoint["plan"])
    context = checkpoint.get("context") or {}
    plan.context_store = decode(context.get("store")) or {}
    plan.context_journal = decode(context.get("journal")) or []
    plan.context_version = context.get("version", plan.context_version)
    plan.context_commit_seq = context.get("commitSeq", plan.context_commit_seq)
