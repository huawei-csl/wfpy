"""Internal context helpers shared by the workflow runtime."""

from __future__ import annotations

import copy
import inspect
import json
from datetime import datetime, timezone
from typing import Any, Protocol

__all__ = [
    "_CTX_ALLOWED_OPS",
    "_split_context_path",
    "_context_get",
    "_context_set",
    "_context_delete",
    "_merge_dict_deep",
    "_context_append",
    "_context_merge",
    "_path_matches_scope",
    "_path_allowed",
    "_collect_leaf_paths",
    "_filter_context_store_by_scopes",
    "_default_context_policy",
    "_actor_context_policy",
    "_build_context_view",
    "_validate_context_patch",
    "_apply_context_patch",
    "_ActionContextFacade",
    "_action_accepts_ctx",
]


_CTX_ALLOWED_OPS = {"set", "append", "delete", "merge"}


class _ContextLiveWriterLike(Protocol):
    def write(self) -> None: ...


class _ActorLike(Protocol):
    kind: str
    name: str
    fire_count: int
    meta: Any


class _PlanLike(Protocol):
    context_config: dict[str, Any]
    context_store: dict[str, Any]
    context_version: int
    context_commit_seq: int
    context_journal: list[dict[str, Any]]
    context_live_writer: Any
    _context_lock: Any


def _split_context_path(path: str) -> list[str]:
    return [part for part in path.strip().split(".") if part]


def _context_get(store: dict[str, Any], path: str, default: Any = None) -> Any:
    parts = _split_context_path(path)
    if not parts:
        return default
    cur: Any = store
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _context_set(store: dict[str, Any], path: str, value: Any) -> None:
    parts = _split_context_path(path)
    if not parts:
        return
    cur = store
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _context_delete(store: dict[str, Any], path: str) -> None:
    parts = _split_context_path(path)
    if not parts:
        return
    cur = store
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            return
        cur = nxt
    cur.pop(parts[-1], None)


def _merge_dict_deep(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _merge_dict_deep(dst[key], value)
        else:
            dst[key] = copy.deepcopy(value)
    return dst


def _context_append(store: dict[str, Any], path: str, value: Any) -> None:
    current = _context_get(store, path)
    if current is None:
        _context_set(store, path, [value])
        return
    if isinstance(current, list):
        current.append(value)
        return
    _context_set(store, path, [current, value])


def _context_merge(store: dict[str, Any], path: str, value: Any) -> None:
    if not isinstance(value, dict):
        _context_set(store, path, value)
        return
    current = _context_get(store, path)
    if isinstance(current, dict):
        _merge_dict_deep(current, value)
    else:
        _context_set(store, path, copy.deepcopy(value))


def _path_matches_scope(path: str, scope: str) -> bool:
    scope_norm = scope.strip()
    if not scope_norm:
        return False
    if scope_norm.endswith(".*"):
        root = scope_norm[:-2]
        return path == root or path.startswith(f"{root}.")
    return path == scope_norm or path.startswith(f"{scope_norm}.")


def _path_allowed(path: str, scopes: list[str]) -> bool:
    return any(_path_matches_scope(path, scope) for scope in scopes)


def _collect_leaf_paths(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        rows: list[tuple[str, Any]] = []
        for key, child in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else key
            rows.extend(_collect_leaf_paths(child, next_prefix))
        return rows
    return [(prefix, value)]


def _filter_context_store_by_scopes(store: dict[str, Any], scopes: list[str]) -> dict[str, Any]:
    if not scopes:
        return {}
    out: dict[str, Any] = {}
    for path, value in _collect_leaf_paths(store):
        if not path:
            continue
        if _path_allowed(path, scopes):
            _context_set(out, path, copy.deepcopy(value))
    return out


def _default_context_policy(actor: _ActorLike) -> dict[str, list[str]]:
    if actor.kind == "internal":
        return {
            "read": ["global.*", f"agent.{actor.name}.*"],
            "write": [f"artifacts.byNode.{actor.name}", f"agent.{actor.name}.*", "runtime.events"],
        }
    if actor.kind == "external":
        return {
            "read": ["global.*", f"tools.requests.{actor.name}", f"agent.{actor.name}.*"],
            "write": [f"tools.calls.{actor.name}", f"artifacts.byNode.{actor.name}"],
        }
    if actor.kind == "agent":
        return {
            "read": ["global.*", f"agent.{actor.name}.*", "artifacts.latest", "tools.*"],
            "write": [f"agent.{actor.name}.*", f"artifacts.byNode.{actor.name}", "artifacts.latest"],
        }
    return {
        "read": ["global.*"],
        "write": [f"artifacts.byNode.{actor.name}"],
    }


def _actor_context_policy(actor: _ActorLike) -> dict[str, list[str]]:
    base = _default_context_policy(actor)
    meta = getattr(actor, "meta", None)
    annotations = getattr(meta, "annotations", {}) if meta is not None else {}
    ctx_ann = annotations.get("context") if isinstance(annotations, dict) else None
    if isinstance(ctx_ann, dict):
        read_scopes = ctx_ann.get("read")
        write_scopes = ctx_ann.get("write")
        if isinstance(read_scopes, list) and all(isinstance(item, str) for item in read_scopes):
            base["read"] = [item for item in read_scopes if item.strip()]
        if isinstance(write_scopes, list) and all(isinstance(item, str) for item in write_scopes):
            base["write"] = [item for item in write_scopes if item.strip()]
    return base


def _build_context_view(
    plan: _PlanLike,
    actor: _ActorLike,
    policy: dict[str, list[str]],
) -> dict[str, Any]:
    mode = str(plan.context_config.get("mode", "scoped")).strip().lower()
    if mode == "off":
        return {}

    if mode == "full":
        selected = copy.deepcopy(plan.context_store)
    else:
        selected = _filter_context_store_by_scopes(plan.context_store, policy.get("read", []))

    budget = plan.context_config.get("budget")
    summarize = bool(plan.context_config.get("summarize", True))
    if isinstance(budget, int) and budget > 0:
        max_chars = max(256, budget * 32)
        serialized = json.dumps(selected, default=str)
        if len(serialized) > max_chars:
            if summarize:
                selected = {
                    "_contextSummary": {
                        "actor": actor.name,
                        "truncated": True,
                        "approxChars": len(serialized),
                        "preview": serialized[:max_chars],
                    }
                }
            else:
                selected = {
                    "_contextSummary": {
                        "actor": actor.name,
                        "truncated": True,
                        "approxChars": len(serialized),
                    }
                }
    return selected


def _validate_context_patch(patch: dict[str, Any]) -> list[dict[str, Any]]:
    ops_raw = patch.get("ops")
    if not isinstance(ops_raw, list):
        raise ValueError("contextPatch.ops must be a list")
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(ops_raw):
        if not isinstance(item, dict):
            raise ValueError(f"contextPatch.ops[{idx}] must be an object")
        op = str(item.get("op", "")).strip().lower()
        if op not in _CTX_ALLOWED_OPS:
            raise ValueError(f"contextPatch.ops[{idx}] has invalid op '{op}'")
        path = str(item.get("path", "")).strip()
        if not path:
            raise ValueError(f"contextPatch.ops[{idx}] missing non-empty path")
        if op in ("set", "append", "merge") and "value" not in item:
            raise ValueError(f"contextPatch.ops[{idx}] op '{op}' requires value")
        normalized.append({
            "op": op,
            "path": path,
            **({"value": item.get("value")} if "value" in item else {}),
        })
    return normalized


def _apply_context_patch(
    plan: _PlanLike,
    actor: _ActorLike,
    policy: dict[str, list[str]],
    patch: dict[str, Any],
    *,
    source: str,
) -> None:
    mode = str(plan.context_config.get("mode", "scoped")).strip().lower()
    if mode == "off":
        return

    ops = _validate_context_patch(patch)
    write_scopes = policy.get("write", [])
    for op in ops:
        path = str(op["path"])
        if not _path_allowed(path, write_scopes):
            raise RuntimeError(
                f"contextPatch write denied for actor '{actor.name}' on path '{path}'. "
                f"Allowed scopes: {write_scopes}"
            )

    with plan._context_lock:
        base_version = patch.get("baseVersion")
        if base_version is not None and int(base_version) != int(plan.context_version):
            raise RuntimeError(
                f"contextPatch baseVersion mismatch for actor '{actor.name}': "
                f"expected {plan.context_version}, got {base_version}"
            )

        for op in ops:
            op_name = op["op"]
            path = op["path"]
            if op_name == "set":
                _context_set(plan.context_store, path, copy.deepcopy(op.get("value")))
            elif op_name == "append":
                _context_append(plan.context_store, path, copy.deepcopy(op.get("value")))
            elif op_name == "delete":
                _context_delete(plan.context_store, path)
            elif op_name == "merge":
                _context_merge(plan.context_store, path, copy.deepcopy(op.get("value")))

        if not ops:
            return

        version_before = plan.context_version
        plan.context_version += 1
        plan.context_commit_seq += 1
        entry = {
            "commitSequence": plan.context_commit_seq,
            "contextVersion": plan.context_version,
            "baseVersion": version_before,
            "nodeInstance": actor.name,
            "source": source,
            "step": actor.fire_count + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ops": ops,
        }
        plan.context_journal.append(entry)
        if plan.context_live_writer:
            plan.context_live_writer.write()


class _ActionContextFacade:
    """Scoped context facade injected into internal task actions."""

    def __init__(
        self,
        plan: _PlanLike,
        actor: _ActorLike,
        policy: dict[str, list[str]],
        view: dict[str, Any],
    ) -> None:
        self._plan = plan
        self._actor = actor
        self._policy = policy
        self._view = copy.deepcopy(view)
        self._base_version = int(plan.context_version)
        self._ops: list[dict[str, Any]] = []

    def get(self, path: str, default: Any = None) -> Any:
        if not _path_allowed(path, self._policy.get("read", [])):
            return default
        value = _context_get(self._plan.context_store, path, default)
        return copy.deepcopy(value)

    def set(self, path: str, value: Any) -> None:
        self._ops.append({"op": "set", "path": path, "value": copy.deepcopy(value)})

    def append(self, path: str, value: Any) -> None:
        self._ops.append({"op": "append", "path": path, "value": copy.deepcopy(value)})

    def delete(self, path: str) -> None:
        self._ops.append({"op": "delete", "path": path})

    def merge(self, path: str, value: Any) -> None:
        self._ops.append({"op": "merge", "path": path, "value": copy.deepcopy(value)})

    def view(self) -> dict[str, Any]:
        return copy.deepcopy(self._view)

    def emit(self, event_name: str, payload: Any) -> None:
        self._ops.append({
            "op": "append",
            "path": "runtime.events",
            "value": {
                "name": event_name,
                "payload": copy.deepcopy(payload),
                "actor": self._actor.name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        })

    def context_patch(self) -> dict[str, Any] | None:
        if not self._ops:
            return None
        return {
            "baseVersion": self._base_version,
            "ops": copy.deepcopy(self._ops),
        }


def _action_accepts_ctx(fn: Any, consumed_args: int) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False

    params = list(sig.parameters.values())
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
    positional = [
        p
        for p in params
        if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    non_self = max(0, len(positional) - 1)
    if has_varargs:
        return non_self >= consumed_args
    return non_self >= (consumed_args + 1)
