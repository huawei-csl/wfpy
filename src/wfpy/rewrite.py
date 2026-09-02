"""wfpy.rewrite — libcst rewrite primitives for diagram edits."""

from __future__ import annotations

import dataclasses
import hashlib
import keyword
import tempfile
from typing import Any, Sequence
from pathlib import Path
from typing import Any, Iterable

import libcst as cst
import libcst.matchers as m

from wfpy.workflow_identity import (
    detect_dynamic_workflow_identity_issues,
    format_dynamic_workflow_identity_error,
)

__all__ = [
    "RewriteError",
    "RewriteEngine",
    "load_module",
]


def _expr_to_code(node: cst.CSTNode | None) -> str:
    if node is None:
        return ""
    return cst.Module([]).code_for_node(node).strip()


def _source_revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_port_value(value: cst.BaseExpression) -> dict[str, str] | None:
    """Extract `{direction, type}` from a `Port[<type>](direction="…")` expression."""
    call = value if isinstance(value, cst.Call) else None
    subscript = call.func if call and isinstance(call.func, cst.Subscript) else (
        value if isinstance(value, cst.Subscript) else None
    )
    if subscript is None:
        return None
    port_type = ""
    if subscript.slice:
        sl = subscript.slice[0].slice
        if isinstance(sl, cst.Index):
            port_type = cst.Module([]).code_for_node(sl.value).strip()
    direction = "in"
    if call is not None:
        for arg in call.args:
            if arg.keyword is not None and arg.keyword.value == "direction":
                raw = cst.Module([]).code_for_node(arg.value).strip().strip("\"'")
                direction = raw or "in"
    return {"direction": direction, "type": port_type}


def _graph_port_id(endpoint: dict[str, Any]) -> str:
    """Canonical port id for a graph edge endpoint (contract v2)."""
    if endpoint.get("kind") == "node":
        return f"n:{endpoint.get('nodeId', '')}:p:{endpoint.get('port', '')}"
    return f"wf:{endpoint.get('port', '')}"


def _with_scope_id(stmt: cst.With, parent: str) -> str:
    """Scope id for a wfpy control `with` block, nested under `parent`:
    `with ctrl.then:` → `<parent>/ctrl:then` (`else_` → `:else`); `with loop:` →
    `<parent>/loop:body`. Returns `parent` if the item shape isn't recognized."""
    for item in stmt.items:
        target = item.item
        if isinstance(target, cst.Attribute) and isinstance(target.value, cst.Name):
            attr = target.attr.value
            branch = "then" if attr == "then" else ("else" if attr == "else_" else "body")
            seg = f"{target.value.value}:{branch}"
        elif isinstance(target, cst.Name):
            seg = f"{target.value}:body"
        else:
            continue
        return f"scope:{seg}" if parent == "scope:root" else f"{parent}/{seg}"
    return parent


def _augment_graph_ids(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    """Attach canonical stable ids (`n:<name>`, `n:<name>:p:<port>`, `e:…`) to the
    graph in place. Purely additive — existing keys (`id`, `from`, `to`, …) are kept
    so current consumers and tests are unaffected."""
    for node in nodes:
        node_name = str(node.get("id", ""))
        if node_name and "nodeId" not in node:
            node["nodeId"] = f"n:{node_name}"
    for edge in edges:
        src = edge.get("from") if isinstance(edge.get("from"), dict) else None
        dst = edge.get("to") if isinstance(edge.get("to"), dict) else None
        if src is None or dst is None:
            continue
        src_pid = _graph_port_id(src)
        dst_pid = _graph_port_id(dst)
        src["portId"] = src_pid
        dst["portId"] = dst_pid
        edge["edgeId"] = f"e:{src_pid}->{dst_pid}"


def _atomic_write_if_unchanged(file_path: str, *, expected_revision: str, text: str) -> None:
    path = Path(file_path)
    current_text = path.read_text(encoding="utf-8")
    current_revision = _source_revision(current_text)
    if current_revision != expected_revision:
        raise RewriteError(
            message="source changed during rewrite",
            diagnostic={
                "code": "concurrent_source_modification",
                "file": file_path,
                "expectedRevision": expected_revision,
                "actualRevision": current_revision,
            },
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        handle.write(text)
        temp_name = handle.name
    Path(temp_name).replace(path)


def load_module(file_path: str) -> tuple[cst.Module, str, str]:
    raw_source = open(file_path, "r", encoding="utf-8").read()
    issues = detect_dynamic_workflow_identity_issues(raw_source)
    if issues:
        raise RewriteError(
            message=format_dynamic_workflow_identity_error(file_path, issues),
            diagnostic={
                "file": file_path,
                "issues": [dataclasses.asdict(issue) for issue in issues],
            },
        )

    source_text = cst.parse_module(raw_source).code
    revision = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    module = cst.parse_module(source_text)
    return module, source_text, revision


@dataclasses.dataclass
class RewriteError(RuntimeError):
    message: str
    diagnostic: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


class RewriteEngine:
    def __init__(self, file_path: str, module: cst.Module, source_text: str) -> None:
        self.file_path = file_path
        self.module = module
        self.source_text = source_text
        self._updated = False
        self.revision = hashlib.sha256(source_text.encode("utf-8")).hexdigest()

    def save(self) -> None:
        if not self._updated:
            return
        new_source = self.module.code
        _atomic_write_if_unchanged(self.file_path, expected_revision=self.revision, text=new_source)
        self.revision = _source_revision(new_source)

    def _set_module(self, module: cst.Module) -> None:
        self.module = module
        self._updated = True

    def _resolve_module_file_path(self, module_name: str) -> str:
        """Resolve a Python module to a local file path.

        Relative resolution is based on ``self.file_path`` parent package.
        Supports both module files and package ``__init__.py``.
        """

        if not module_name.strip():
            raise RewriteError(message="Module name is empty")

        module_parts = [part for part in module_name.split(".") if part]
        if not module_parts:
            raise RewriteError(message=f"Invalid module name: {module_name}")

        search_root = Path(self.file_path).resolve().parent
        while True:
            candidate = search_root.joinpath(*module_parts)
            py_file = candidate.with_suffix(".py")
            init_file = candidate / "__init__.py"
            if py_file.is_file():
                return str(py_file)
            if init_file.is_file():
                return str(init_file)
            if search_root.parent == search_root:
                break
            search_root = search_root.parent

        raise RewriteError(
            message=f"Cannot resolve module path for '{module_name}'",
            diagnostic={
                "file": self.file_path,
                "module": module_name,
            },
        )

    def _resolve_nested_workflow_candidates(self, workflow_type: str) -> list[str]:
        """Return candidate module files that may define imported workflow_type."""

        candidates: list[str] = []
        seen: set[str] = set()

        for stmt in self.module.body:
            if not isinstance(stmt, cst.SimpleStatementLine):
                continue
            for simple in stmt.body:
                if not isinstance(simple, cst.ImportFrom):
                    continue
                if simple.relative:
                    continue
                module_expr = simple.module
                if module_expr is None:
                    continue
                module_name = _expr_to_code(module_expr)
                if not module_name:
                    continue

                if isinstance(simple.names, cst.ImportStar):
                    try:
                        module_file = self._resolve_module_file_path(module_name)
                    except RewriteError:
                        continue
                    if module_file not in seen:
                        seen.add(module_file)
                        candidates.append(module_file)
                    continue

                imported_items: list[tuple[str, str]] = []
                for alias in simple.names:
                    if not isinstance(alias, cst.ImportAlias):
                        continue
                    imported_name = _expr_to_code(alias.name)
                    as_name = alias.asname.name.value if alias.asname and isinstance(alias.asname.name, cst.Name) else imported_name
                    if imported_name and as_name:
                        imported_items.append((imported_name, as_name))

                matching = [item for item in imported_items if item[1] == workflow_type]
                if not matching:
                    continue

                imported_name = matching[0][0]
                try:
                    module_file = self._resolve_module_file_path(module_name)
                except RewriteError:
                    continue
                if module_file not in seen:
                    seen.add(module_file)
                    candidates.append(module_file)

                if "." not in module_name:
                    parent_name = Path(self.file_path).resolve().parent.name
                    if parent_name:
                        qualified_module = f"{parent_name}.{module_name}"
                        try:
                            qualified_file = self._resolve_module_file_path(qualified_module)
                        except RewriteError:
                            qualified_file = ""
                        if qualified_file and qualified_file not in seen:
                            seen.add(qualified_file)
                            candidates.append(qualified_file)

                del imported_name

        return candidates

    def _resolve_nested_workflow_graph(
        self,
        workflow_type: str,
        visited_workflows: set[str] | None = None,
    ) -> dict[str, Any]:
        """Resolve a workflow declared in another module and export its graph recursively."""

        visited = visited_workflows if visited_workflows is not None else set()

        module_files = self._resolve_nested_workflow_candidates(workflow_type)
        for module_file in module_files:
            nested_module, nested_source, _nested_rev = load_module(module_file)
            nested_engine = RewriteEngine(module_file, nested_module, nested_source)
            if workflow_type in visited:
                return {
                    "workflow": workflow_type,
                    "nodes": [],
                    "edges": [],
                    "children": [],
                    "file": module_file,
                    "recursiveRef": True,
                }
            next_visited = set(visited)
            next_visited.add(workflow_type)
            try:
                return nested_engine.export_workflow_graph(
                    workflow=workflow_type,
                    include_nested=True,
                    _visited_workflows=next_visited,
                )
            except RewriteError:
                continue

        raise RewriteError(
            message=f"Nested workflow '{workflow_type}' is not imported in this module",
            diagnostic={
                "file": self.file_path,
                "workflow": workflow_type,
            },
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _first_non_docstring_index(self, body: list[cst.BaseStatement]) -> int:
        if not body:
            return 0
        first = body[0]
        if isinstance(first, cst.SimpleStatementLine) and len(first.body) == 1:
            expr = first.body[0]
            if m.matches(expr, m.Expr(value=m.SimpleString())):
                return 1
        return 0

    def _find_insert_index(self, body: list[cst.BaseStatement], kind: str) -> int:
        first_idx = self._first_non_docstring_index(body)
        last_assign = -1
        last_connect = -1
        for idx, stmt in enumerate(body):
            if isinstance(stmt, cst.SimpleStatementLine):
                for s in stmt.body:
                    if m.matches(s, m.Assign()):
                        last_assign = idx
                    if m.matches(s, m.Expr(value=m.Call(func=m.Name("connect")))):
                        last_connect = idx
        if kind == "connect":
            if last_connect >= 0:
                return last_connect + 1
            if last_assign >= 0:
                return last_assign + 1
            return first_idx
        if last_assign >= 0:
            return last_assign + 1
        return first_idx

    def _strip_placeholder_pass(self, body: list[cst.BaseStatement]) -> list[cst.BaseStatement]:
        if len(body) <= 1:
            return body
        cleaned: list[cst.BaseStatement] = []
        removed = False
        for stmt in body:
            if (
                not removed
                and isinstance(stmt, cst.SimpleStatementLine)
                and len(stmt.body) == 1
                and isinstance(stmt.body[0], cst.Pass)
            ):
                removed = True
                continue
            cleaned.append(stmt)
        return cleaned if removed else body

    def _insert_statement(
        self,
        body: list[cst.BaseStatement],
        stmt: cst.BaseStatement,
        kind: str,
    ) -> list[cst.BaseStatement]:
        idx = self._find_insert_index(body, kind)
        updated = body[:idx] + [stmt] + body[idx:]
        return self._strip_placeholder_pass(updated)

    def _is_workflow_def(self, fn: cst.FunctionDef) -> bool:
        for dec in fn.decorators:
            target = dec.decorator
            if m.matches(target, m.Name("workflow")):
                return True
            if m.matches(target, m.Attribute(value=m.Name("wfpy"), attr=m.Name("workflow"))):
                return True
            if m.matches(target, m.Call(func=m.Name("workflow"))):
                return True
            if m.matches(target, m.Call(func=m.Attribute(value=m.Name("wfpy"), attr=m.Name("workflow")))):
                return True
        return False

    def _find_workflow_def(
        self,
        workflow_name: str | None,
    ) -> tuple[cst.FunctionDef, cst.IndentedBlock, list[cst.FunctionDef]]:
        if workflow_name is None:
            fn, body = self._get_workflow_body(None)
            return fn, body, []

        def visit_block(
            block: Sequence[cst.BaseStatement],
            parents: list[cst.FunctionDef],
        ) -> tuple[cst.FunctionDef, cst.IndentedBlock, list[cst.FunctionDef]] | None:
            for stmt in block:
                if not isinstance(stmt, cst.FunctionDef):
                    continue
                next_parents = parents + [stmt]
                if stmt.name.value == workflow_name and self._is_workflow_def(stmt):
                    body = stmt.body
                    if isinstance(body, cst.IndentedBlock):
                        return stmt, body, parents
                body = stmt.body
                if isinstance(body, cst.IndentedBlock):
                    nested = visit_block(body.body, next_parents)
                    if nested is not None:
                        return nested
            return None

        resolved = visit_block(self.module.body, [])
        if resolved is not None:
            return resolved

        raise RewriteError(
            message="Workflow function not found",
            diagnostic={"file": self.file_path, "workflow": workflow_name},
        )

    def _helper_default_parameters(self, fn: cst.FunctionDef) -> list[dict[str, Any]]:
        parameters: list[dict[str, Any]] = []
        positional_params = [*fn.params.posonly_params, *fn.params.params]
        for param in [*positional_params, *fn.params.kwonly_params]:
            if param.default is None:
                continue
            parameters.append(
                {
                    "name": param.name.value,
                    "value": self._resolve_expr_code(param.default),
                }
            )
        return parameters

    def _get_workflow_body(self, workflow_name: str | None) -> tuple[cst.FunctionDef, cst.IndentedBlock]:
        candidates: list[cst.FunctionDef] = []
        for stmt in self.module.body:
            if isinstance(stmt, cst.FunctionDef):
                if workflow_name is None or stmt.name.value == workflow_name:
                    candidates.append(stmt)
        if not candidates:
            raise RewriteError(
                message="Workflow function not found",
                diagnostic={"file": self.file_path},
            )
        if workflow_name is None:
            for fn in candidates:
                if self._is_workflow_def(fn):
                    body = fn.body
                    if isinstance(body, cst.IndentedBlock):
                        return fn, body
        fn = candidates[0]
        body = fn.body
        if isinstance(body, cst.IndentedBlock):
            return fn, body
        raise RewriteError(
            message="Workflow function body not found",
            diagnostic={"file": self.file_path},
        )

    def _get_local_function(self, function_name: str) -> cst.FunctionDef | None:
        for stmt in self.module.body:
            if isinstance(stmt, cst.FunctionDef) and stmt.name.value == function_name:
                return stmt
        return None

    def _resolve_expr_code(
        self,
        expr: cst.BaseExpression,
        bindings: dict[str, str] | None = None,
    ) -> str:
        if bindings and isinstance(expr, cst.Name) and expr.value in bindings:
            return bindings[expr.value]
        return _expr_to_code(expr)

    def _build_call_bindings(
        self,
        fn: cst.FunctionDef,
        call: cst.Call,
        outer_bindings: dict[str, str] | None = None,
    ) -> dict[str, str]:
        bindings: dict[str, str] = {}
        positional_params = [*fn.params.posonly_params, *fn.params.params]
        positional_args = [arg for arg in call.args if arg.keyword is None]
        keyword_args = {
            _expr_to_code(arg.keyword): self._resolve_expr_code(arg.value, outer_bindings)
            for arg in call.args
            if arg.keyword is not None
        }

        positional_index = 0
        for param in positional_params:
            name = param.name.value
            if positional_index < len(positional_args):
                bindings[name] = self._resolve_expr_code(positional_args[positional_index].value, outer_bindings)
                positional_index += 1
            elif name in keyword_args:
                bindings[name] = keyword_args[name]
            elif param.default is not None:
                bindings[name] = self._resolve_expr_code(param.default, outer_bindings)

        for param in fn.params.kwonly_params:
            name = param.name.value
            if name in keyword_args:
                bindings[name] = keyword_args[name]
            elif param.default is not None:
                bindings[name] = self._resolve_expr_code(param.default, outer_bindings)

        return bindings

    def _get_same_file_helper_call(self, call: cst.Call) -> tuple[cst.FunctionDef, dict[str, str]] | None:
        if not isinstance(call.func, cst.Name):
            return None

        helper_fn = self._get_local_function(call.func.value)
        if helper_fn is None or self._is_workflow_def(helper_fn):
            return None

        if not isinstance(helper_fn.body, cst.IndentedBlock):
            return None

        return helper_fn, self._build_call_bindings(helper_fn, call)

    def _collect_static_graph_items(
        self,
        body: cst.IndentedBlock,
        *,
        bindings: dict[str, str] | None = None,
        visited_helpers: set[str] | None = None,
        positions: Any | None = None,
        scope: str = "scope:root",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        instance_to_type: dict[str, str] = {}
        active_helpers = set(visited_helpers or set())

        def _loc(node: cst.CSTNode) -> dict[str, Any] | None:
            # Best-effort source location from libcst PositionProvider metadata.
            if not positions:
                return None
            cr = positions.get(node)
            if cr is None:
                return None
            return {"file": self.file_path, "line": cr.start.line, "column": cr.start.column + 1}

        for stmt in body.body:
            # Recurse into wfpy control-flow scopes (`with ctrl.then:` / `.else_:` /
            # `with loop:`), tagging the nodes/edges inside with the nested scope id.
            if isinstance(stmt, cst.With) and isinstance(stmt.body, cst.IndentedBlock):
                child_scope = _with_scope_id(stmt, scope)
                nested_nodes, nested_edges, nested_instances = self._collect_static_graph_items(
                    stmt.body,
                    bindings=bindings,
                    visited_helpers=active_helpers,
                    positions=positions,
                    scope=child_scope,
                )
                nodes.extend(nested_nodes)
                edges.extend(nested_edges)
                instance_to_type.update(nested_instances)
                continue
            if not isinstance(stmt, cst.SimpleStatementLine):
                continue
            for simple in stmt.body:
                if isinstance(simple, cst.Assign) and isinstance(simple.value, cst.Call):
                    helper_call = self._get_same_file_helper_call(simple.value)
                    if helper_call is not None:
                        helper_fn, helper_bindings = helper_call
                        helper_name = helper_fn.name.value
                        if helper_name not in active_helpers and isinstance(helper_fn.body, cst.IndentedBlock):
                            nested_nodes, nested_edges, nested_instances = self._collect_static_graph_items(
                                helper_fn.body,
                                bindings=helper_bindings,
                                visited_helpers={*active_helpers, helper_name},
                                positions=positions,
                                scope=scope,
                            )
                            nodes.extend(nested_nodes)
                            edges.extend(nested_edges)
                            instance_to_type.update(nested_instances)
                        continue

                    if len(simple.targets) != 1:
                        continue

                    target = simple.targets[0].target
                    if isinstance(target, cst.Name):
                        call = simple.value
                        callee = self._resolve_expr_code(call.func, bindings)
                        loc = _loc(simple)
                        node = {
                            "id": target.value,
                            "ref": callee,
                            "kind": "task",
                            "args": [self._resolve_expr_code(arg.value, bindings) for arg in call.args if arg.keyword is None],
                            "kwargs": {
                                _expr_to_code(arg.keyword): self._resolve_expr_code(arg.value, bindings)
                                for arg in call.args if arg.keyword is not None
                            },
                            "scope": scope,
                            **({"location": loc} if loc else {}),
                        }
                        instance_to_type[target.value] = callee
                        nodes.append(node)
                        continue

                if isinstance(simple, cst.Expr) and isinstance(simple.value, cst.Call):
                    helper_call = self._get_same_file_helper_call(simple.value)
                    if helper_call is not None:
                        helper_fn, helper_bindings = helper_call
                        helper_name = helper_fn.name.value
                        if helper_name not in active_helpers and isinstance(helper_fn.body, cst.IndentedBlock):
                            nested_nodes, nested_edges, nested_instances = self._collect_static_graph_items(
                                helper_fn.body,
                                bindings=helper_bindings,
                                visited_helpers={*active_helpers, helper_name},
                                positions=positions,
                                scope=scope,
                            )
                            nodes.extend(nested_nodes)
                            edges.extend(nested_edges)
                            instance_to_type.update(nested_instances)
                        continue

                    call = simple.value
                    if self._resolve_expr_code(call.func, bindings) != "connect" or len(call.args) != 2:
                        continue
                    left = self._resolve_expr_code(call.args[0].value, bindings)
                    right = self._resolve_expr_code(call.args[1].value, bindings)
                    if not left or not right:
                        continue

                    def parse_endpoint(expr: str) -> dict[str, Any]:
                        if "." in expr:
                            node_id, port = expr.split(".", 1)
                            return {
                                "kind": "node",
                                "nodeId": node_id,
                                "port": port,
                                "ref": instance_to_type.get(node_id, "")
                            }
                        raw = expr.strip()
                        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
                            raw = raw[1:-1]
                        return {
                            "kind": "workflow",
                            "port": raw
                        }

                    edge_loc = _loc(simple)
                    edges.append({
                        "id": f"e{len(edges) + 1}",
                        "from": parse_endpoint(left),
                        "to": parse_endpoint(right),
                        "scope": scope,
                        **({"location": edge_loc} if edge_loc else {}),
                    })

        return nodes, edges, instance_to_type

    def _matches_scope_with(self, stmt: cst.With, scope_control: str, scope_branch: str | None) -> bool:
        for item in stmt.items:
            target = item.item
            if isinstance(target, cst.Attribute) and isinstance(target.value, cst.Name):
                if target.value.value != scope_control:
                    continue
                if scope_branch == "then" and target.attr.value == "then":
                    return True
                if scope_branch == "else" and target.attr.value == "else_":
                    return True
            if isinstance(target, cst.Name):
                if target.value == scope_control and scope_branch in (None, "body"):
                    return True
        return False

    def _child_blocks(self, stmt: cst.BaseStatement) -> Iterable[cst.IndentedBlock]:
        if isinstance(stmt, cst.With):
            if isinstance(stmt.body, cst.IndentedBlock):
                yield stmt.body
            return
        if isinstance(stmt, (cst.If, cst.For, cst.While, cst.Try)):
            if isinstance(stmt.body, cst.IndentedBlock):
                yield stmt.body
            if isinstance(stmt, cst.If) and isinstance(stmt.orelse, cst.Else):
                if isinstance(stmt.orelse.body, cst.IndentedBlock):
                    yield stmt.orelse.body
            if isinstance(stmt, (cst.For, cst.While)) and isinstance(stmt.orelse, cst.Else):
                if isinstance(stmt.orelse.body, cst.IndentedBlock):
                    yield stmt.orelse.body
            if isinstance(stmt, cst.Try):
                for handler in stmt.handlers:
                    if isinstance(handler.body, cst.IndentedBlock):
                        yield handler.body
                if isinstance(stmt.orelse, cst.Else) and isinstance(stmt.orelse.body, cst.IndentedBlock):
                    yield stmt.orelse.body

    def _find_scope_block(
        self,
        body: cst.IndentedBlock,
        scope_control: str | None,
        scope_branch: str | None,
    ) -> cst.IndentedBlock:
        if not scope_control:
            return body
        for stmt in body.body:
            if isinstance(stmt, cst.With) and self._matches_scope_with(stmt, scope_control, scope_branch):
                if isinstance(stmt.body, cst.IndentedBlock):
                    return stmt.body
            for child in self._child_blocks(stmt):
                try:
                    return self._find_scope_block(child, scope_control, scope_branch)
                except RewriteError:
                    continue
        raise RewriteError(
            message="Scope not found",
            diagnostic={"file": self.file_path, "scope": scope_control},
        )

    def _matches_control_with_any(self, stmt: cst.With, name: str) -> bool:
        for item in stmt.items:
            target = item.item
            if isinstance(target, cst.Attribute) and isinstance(target.value, cst.Name):
                if target.value.value == name:
                    return True
            if isinstance(target, cst.Name) and target.value == name:
                return True
        return False

    def _update_scope_body(
        self,
        fn_name: str,
        scope_control: str | None,
        scope_branch: str | None,
        new_block: cst.IndentedBlock,
    ) -> None:
        if not scope_control:
            self._update_function_body(fn_name, new_block)
            return

        class _ScopeRewriter(cst.CSTTransformer):
            def __init__(self, matcher: RewriteEngine, control: str, branch: str | None, body: cst.IndentedBlock) -> None:
                self._matcher = matcher
                self._control = control
                self._branch = branch
                self._body = body
                self.updated = False

            def leave_With(self, original_node: cst.With, updated_node: cst.With) -> cst.With:
                if self.updated:
                    return updated_node
                if self._matcher._matches_scope_with(original_node, self._control, self._branch):
                    self.updated = True
                    return updated_node.with_changes(body=self._body)
                return updated_node

        rewriter = _ScopeRewriter(self, scope_control, scope_branch, new_block)
        updated = self.module.visit(rewriter)
        if not rewriter.updated:
            raise RewriteError(
                message="Scope not found",
                diagnostic={"file": self.file_path, "scope": scope_control},
            )
        self._set_module(updated)

    def _collect_assigned_names(self, fn: cst.FunctionDef) -> set[str]:
        names: set[str] = set()

        class _Visitor(cst.CSTVisitor):
            def visit_Assign(self, node: cst.Assign) -> None:
                for target in node.targets:
                    if isinstance(target.target, cst.Name):
                        names.add(target.target.value)

            def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
                if isinstance(node.target, cst.Name):
                    names.add(node.target.value)

        fn.visit(_Visitor())
        return names

    def _unique_name(self, base: str, taken: set[str]) -> str:
        if base not in taken:
            return base
        idx = 2
        while f"{base}_{idx}" in taken:
            idx += 1
        return f"{base}_{idx}"

    def _update_function_body(self, fn_name: str, new_body: cst.IndentedBlock) -> None:
        class _BodyRewriter(cst.CSTTransformer):
            def __init__(self, name: str, body: cst.IndentedBlock) -> None:
                self._name = name
                self._body = body

            def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
                if original_node.name.value == self._name:
                    return updated_node.with_changes(body=self._body)
                return updated_node

        updated = self.module.visit(_BodyRewriter(fn_name, new_body))
        self._set_module(updated)

    def _port_expr_from_spec(self, spec: Any) -> str:
        if isinstance(spec, str):
            return spec
        if isinstance(spec, dict):
            kind = spec.get("type")
            if kind == "workflow":
                name = str(spec.get("name", ""))
                if not name:
                    raise RewriteError(message="workflow port requires name")
                return repr(name)
            if kind == "actor":
                actor = str(spec.get("actor", ""))
                port = str(spec.get("port", ""))
                if not actor or not port:
                    raise RewriteError(message="actor port requires actor and port")
                return f"{actor}.{port}"
            if kind == "control":
                control = str(spec.get("control", ""))
                port = str(spec.get("port", ""))
                if not control or not port:
                    raise RewriteError(message="control port requires control and port")
                return f"{control}.{port}"
        raise RewriteError(message="Invalid port spec")

    def _ensure_identifier(self, name: str) -> None:
        if not name.isidentifier():
            raise RewriteError(message=f"Invalid identifier: {name}")

    # ── Operations ───────────────────────────────────────────────────────

    def create_node(
        self,
        *,
        workflow: str | None = None,
        type: str,
        name: str,
        params: dict[str, Any] | None = None,
        scope_control: str | None = None,
        scope_branch: str | None = None,
    ) -> None:
        self._ensure_identifier(name)
        fn, body = self._get_workflow_body(workflow)
        target_block = self._find_scope_block(body, scope_control, scope_branch)
        target_body = list(target_block.body)
        params = params or {}
        args = []
        for key, value in params.items():
            if not str(key).isidentifier():
                raise RewriteError(message=f"Invalid parameter name: {key}")
            args.append(
                cst.Arg(
                    keyword=cst.Name(str(key)),
                    value=cst.parse_expression(repr(value)),
                )
            )
        assign = cst.Assign(
            targets=[cst.AssignTarget(target=cst.Name(name))],
            value=cst.Call(func=cst.Name(type), args=args),
        )
        stmt = cst.SimpleStatementLine([assign])
        new_stmt_body = self._insert_statement(target_body, stmt, "node")
        new_block = target_block.with_changes(body=new_stmt_body)
        self._update_scope_body(fn.name.value, scope_control, scope_branch, new_block)

    def create_port(
        self,
        *,
        workflow: str | None = None,
        direction: str,
        portName: str,
        portType: str,
    ) -> None:
        if direction not in ("input", "output"):
            raise RewriteError(message=f"Invalid port direction: {direction}")
        self._ensure_identifier(portName)
        fn, body = self._get_workflow_body(workflow)
        target_body = list(body.body)
        ports_attr = "inputs" if direction == "input" else "outputs"
        port_expr = cst.parse_expression(portType)
        dict_entry = cst.DictElement(key=cst.SimpleString(f"\"{portName}\""), value=port_expr)
        assign_stmt = cst.SimpleStatementLine([
            cst.Assign(
                targets=[cst.AssignTarget(target=cst.Name("ports"))],
                value=cst.Call(func=cst.Name("getattr"), args=[
                    cst.Arg(value=cst.Name(fn.name.value)),
                    cst.Arg(value=cst.SimpleString(f"\"{ports_attr}\"")),
                    cst.Arg(value=cst.Dict(elements=[]))
                ])
            )
        ])
        update_stmt = cst.SimpleStatementLine([
            cst.Expr(
                cst.Call(
                    func=cst.Attribute(value=cst.Name("ports"), attr=cst.Name("update")),
                    args=[cst.Arg(value=cst.Dict(elements=[dict_entry]))]
                )
            )
        ])
        assign_back_stmt = cst.SimpleStatementLine([
            cst.Assign(
                targets=[cst.AssignTarget(target=cst.Attribute(value=cst.Name(fn.name.value), attr=cst.Name(ports_attr)))],
                value=cst.Name("ports")
            )
        ])
        new_stmt_body = self._insert_statement(target_body, assign_stmt, "node")
        new_stmt_body = self._insert_statement(new_stmt_body, update_stmt, "node")
        new_stmt_body = self._insert_statement(new_stmt_body, assign_back_stmt, "node")
        new_block = body.with_changes(body=new_stmt_body)
        updated = self.module.with_changes(body=new_block.body)
        if "Any" in portType:
            updated = self._ensure_any_import(updated)
        self._set_module(updated)

    def list_task_types(self, *, kind: str) -> list[str]:
        kinds = {"tool", "agent", "viewer", "task"}
        if kind not in kinds:
            return []
        results: list[str] = []

        class _Visitor(cst.CSTVisitor):
            def visit_ClassDef(self, node: cst.ClassDef) -> None:
                for dec in node.decorators:
                    target = dec.decorator
                    if kind == "tool" and m.matches(target, m.Name("tool")):
                        results.append(node.name.value)
                        return
                    if kind == "tool" and m.matches(target, m.Call(func=m.Name("tool"))):
                        results.append(node.name.value)
                        return
                    if kind == "agent" and m.matches(target, m.Name("agent")):
                        results.append(node.name.value)
                        return
                    if kind == "agent" and m.matches(target, m.Call(func=m.Name("agent"))):
                        results.append(node.name.value)
                        return
                    if kind == "viewer" and m.matches(target, m.Name("viewer")):
                        results.append(node.name.value)
                        return
                    if kind == "viewer" and m.matches(target, m.Call(func=m.Name("viewer"))):
                        results.append(node.name.value)
                        return
                    if kind == "task" and m.matches(target, m.Name("task")):
                        results.append(node.name.value)
                        return
                    if kind == "task" and m.matches(target, m.Call(func=m.Name("task"))):
                        results.append(node.name.value)
                        return

        self.module.visit(_Visitor())
        results.sort()
        return results

    def list_workflow_types(self) -> list[str]:
        results: list[str] = []

        class _Visitor(cst.CSTVisitor):
            def __init__(self, matcher: RewriteEngine) -> None:
                self._matcher = matcher

            def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
                if self._matcher._is_workflow_def(node):
                    results.append(node.name.value)

        self.module.visit(_Visitor(self))
        results.sort()
        return results

    def list_instance_names(self, *, workflow: str | None = None) -> list[str]:
        fn, body = self._get_workflow_body(workflow)
        nodes, _edges, _instance_to_type = self._collect_static_graph_items(body)
        return sorted({str(node.get("id", "")) for node in nodes if str(node.get("id", ""))})

    def instance_types(self, *, workflow: str | None = None) -> dict[str, str]:
        """Map each instance (node) name → its component type name in the workflow."""
        fn, body = self._get_workflow_body(workflow)
        _nodes, _edges, instance_to_type = self._collect_static_graph_items(body)
        return dict(instance_to_type)

    def instance_scopes(self, *, workflow: str | None = None) -> dict[str, str]:
        """Map each instance (node) name → its control-flow scope id in the workflow."""
        fn, body = self._get_workflow_body(workflow)
        nodes, _edges, _instances = self._collect_static_graph_items(body)
        return {str(n["id"]): str(n.get("scope", "scope:root")) for n in nodes if n.get("id")}

    def port_metadata(self, type_name: str, port_name: str) -> dict[str, str] | None:
        """Resolve a component port's `{direction, type}` from its type's `class Ports`.
        Returns None if the type/port cannot be found. Direction defaults to "in" when no
        `direction=` kwarg is present (matches the Port default)."""
        for stmt in self.module.body:
            if not (isinstance(stmt, cst.ClassDef) and stmt.name.value == type_name):
                continue
            ports = next(
                (c for c in stmt.body.body if isinstance(c, cst.ClassDef) and c.name.value == "Ports"),
                None,
            )
            if ports is None:
                return None
            for line in ports.body.body:
                if not isinstance(line, cst.SimpleStatementLine):
                    continue
                for s in line.body:
                    if isinstance(s, cst.Assign) and any(
                        isinstance(t.target, cst.Name) and t.target.value == port_name for t in s.targets
                    ):
                        return _parse_port_value(s.value)
            return None
        return None

    def _source_positions(self) -> Any | None:
        """Resolve libcst PositionProvider metadata for self.module (node → CodeRange),
        so graph elements can carry source `location`. Best-effort; None on failure."""
        try:
            from libcst.metadata import MetadataWrapper, PositionProvider

            return MetadataWrapper(self.module, unsafe_skip_copy=True).resolve(PositionProvider)
        except Exception:
            return None

    def export_workflow_graph(
        self,
        *,
        workflow: str | None = None,
        include_nested: bool = True,
        _visited_workflows: set[str] | None = None,
    ) -> dict[str, Any]:
        fn, body, parents = self._find_workflow_def(workflow)
        workflow_name = fn.name.value
        nodes, edges, instance_to_type = self._collect_static_graph_items(
            body, positions=self._source_positions()
        )
        # Additively attach canonical, stable element ids (contract v2) without
        # touching the existing fields the diagram server / tests rely on.
        _augment_graph_ids(nodes, edges)
        visited = set(_visited_workflows or set())
        visited.add(workflow_name)

        graph: dict[str, Any] = {
            "workflow": workflow_name,
            "nodes": nodes,
            "edges": edges,
            "file": self.file_path,
        }
        if parents:
            factory_fn = parents[-1]
            helper_parameters = self._helper_default_parameters(factory_fn)
            if helper_parameters:
                graph["parameters"] = helper_parameters
            graph["factoryName"] = factory_fn.name.value

        if include_nested:
            children: list[dict[str, Any]] = []
            for node in nodes:
                ref = str(node.get("ref", "")).strip()
                if not ref:
                    continue
                if ref in visited:
                    continue
                try:
                    child_graph = self.export_workflow_graph(
                        workflow=ref,
                        include_nested=True,
                        _visited_workflows=set(visited),
                    )
                    children.append(
                        {
                            "instance": str(node.get("id", "")),
                            "workflow": ref,
                            "graph": child_graph,
                            "source": "same-file",
                        }
                    )
                    continue
                except RewriteError:
                    pass

                try:
                    child_graph = self._resolve_nested_workflow_graph(
                        ref,
                        visited_workflows=set(visited),
                    )
                    children.append(
                        {
                            "instance": str(node.get("id", "")),
                            "workflow": ref,
                            "graph": child_graph,
                            "source": "import",
                        }
                    )
                except RewriteError:
                    continue

            if children:
                graph["children"] = children

        return graph

    def create_task_type(
        self,
        *,
        kind: str,
        name: str,
        facade: str | None = None,
        network: str | None = None,
    ) -> None:
        self._ensure_identifier(name)
        if kind not in ("tool", "agent", "viewer", "task", "streamblocks"):
            raise RewriteError(message=f"Invalid task kind: {kind}")
        if kind == "streamblocks":
            if facade not in ("design", "instance"):
                raise RewriteError(
                    message=(
                        "A streamblocks type needs facade='design' or "
                        f"facade='instance', not {facade!r}"
                    )
                )
            if facade == "instance" and not (network or "").strip():
                raise RewriteError(
                    message=(
                        "A streamblocks instance needs network='path/to/network.py' "
                        "— without one there is nothing to compile, run or open"
                    )
                )
        if kind == "tool":
            decorator: cst.BaseExpression = cst.Call(
                func=cst.Name("tool"),
                args=[cst.Arg(keyword=cst.Name("cmd"), value=cst.SimpleString('"echo"'))]
            )
        elif kind == "agent":
            decorator = cst.Call(
                func=cst.Name("agent"),
                args=[cst.Arg(keyword=cst.Name("prompt"), value=cst.SimpleString('"TODO"'))]
            )
        elif kind == "viewer":
            decorator = cst.Name("viewer")
        elif kind == "task":
            decorator = cst.Name("task")
        elif kind == "streamblocks":
            # The decorator refuses a bad combination too, but writing one into
            # the file and letting import time report it would be a poor way to
            # find out.
            sb_args = [
                cst.Arg(keyword=cst.Name("facade"), value=cst.SimpleString(f'"{facade}"'))
            ]
            if (network or "").strip():
                sb_args.append(
                    cst.Arg(
                        keyword=cst.Name("network"),
                        value=cst.SimpleString(f'"{network}"'),
                    )
                )
            decorator = cst.Call(func=cst.Name("streamblocks"), args=sb_args)
        else:
            decorator = cst.Name(kind)
        ensure_import = False
        if kind == "viewer":
            ports_body = [
                cst.SimpleStatementLine([
                    cst.Assign(
                        targets=[cst.AssignTarget(target=cst.Name("In"))],
                        value=cst.Call(
                            func=cst.Subscript(
                                value=cst.Name("Port"),
                                slice=[cst.SubscriptElement(slice=cst.Index(value=cst.Name("Any")))]
                            ),
                            args=[]
                        )
                    )
                ])
            ]
        else:
            ports_body = [
                cst.SimpleStatementLine([
                    cst.Assign(
                        targets=[cst.AssignTarget(target=cst.Name("In"))],
                        value=cst.Call(
                            func=cst.Subscript(
                                value=cst.Name("Port"),
                                slice=[cst.SubscriptElement(slice=cst.Index(value=cst.Name("Any")))]
                            ),
                            args=[]
                        )
                    )
                ]),
                cst.SimpleStatementLine([
                    cst.Assign(
                        targets=[cst.AssignTarget(target=cst.Name("Out"))],
                        value=cst.Call(
                            func=cst.Subscript(
                                value=cst.Name("Port"),
                                slice=[cst.SubscriptElement(slice=cst.Index(value=cst.Name("Any")))]
                            ),
                            args=[cst.Arg(keyword=cst.Name("direction"), value=cst.SimpleString('"out"'))]
                        )
                    )
                ])
            ]
        ensure_import = True
        ports_class = cst.ClassDef(
            name=cst.Name("Ports"),
            body=cst.IndentedBlock(body=ports_body)
        )
        class_def = cst.ClassDef(
            name=cst.Name(name),
            decorators=[cst.Decorator(decorator=decorator)],
            body=cst.IndentedBlock(
                body=[ports_class]
            )
        )
        body = list(self.module.body)
        insert_at = len(body)
        for idx, stmt in enumerate(body):
            if isinstance(stmt, cst.FunctionDef) and self._is_workflow_def(stmt):
                insert_at = idx
                break
        new_body = body[:insert_at] + [class_def] + body[insert_at:]
        updated = self.module.with_changes(body=new_body)
        if ensure_import:
            # Everything the emitted class needs to RUN, not just `Any`: the
            # decorator that makes it a node and the `Port` its members are
            # built from. Adding one of the three was enough while every file
            # already imported the other two by hand; it stops being enough the
            # moment a node is created in a file that does not.
            updated = self._ensure_any_import(updated)
            # `kind` IS the decorator's name for all four kinds — `tool` and
            # `agent` are called (`@tool(...)`), which changes the decorator
            # expression but not the symbol that has to be in scope.
            updated = self._ensure_import(updated, "wfpy", ["Port", kind])
        self._set_module(updated)

    def create_workflow_type(self, *, name: str) -> None:
        self._ensure_identifier(name)
        body = list(self.module.body)
        insert_at = len(body)
        for idx, stmt in enumerate(body):
            if isinstance(stmt, cst.FunctionDef) and self._is_workflow_def(stmt):
                insert_at = idx
                break
        fn = cst.FunctionDef(
            name=cst.Name(name),
            decorators=[cst.Decorator(decorator=cst.Name("workflow"))],
            params=cst.Parameters([]),
            body=cst.IndentedBlock(body=[cst.SimpleStatementLine([cst.Pass()])])
        )
        new_body = body[:insert_at] + [fn] + body[insert_at:]
        self._set_module(self.module.with_changes(body=new_body))

    def _ensure_any_import(self, module: cst.Module) -> cst.Module:
        return self._ensure_import(module, "typing", ["Any"])

    def _ensure_import(self, module: cst.Module, package: str, names: list[str]) -> cst.Module:
        """Make sure `from <package> import <names>` covers every name.

        Generated code has to be code that RUNS. A created task carries a
        `@task` decorator and `Port[Any]()` members, and dropping that into a
        file that imports neither leaves the author with a NameError to fix by
        hand — the sidecar wrote it, so the sidecar owes the import.

        Names are folded into an existing `from <package> import ...` rather
        than added as a second line, because that is what the examples and the
        hand-written workflows look like, and a rewriter should leave a file
        looking the way its author would have written it. Import order within
        the statement is kept alphabetical for the same reason.

        A star import already covers everything, and an aliased import
        (`from wfpy import task as t`) is left alone: the name is in scope under
        another spelling, and adding the plain one would be redundant at best
        and shadowing at worst.
        """
        wanted = [name for name in names if name]
        if not wanted:
            return module

        body = list(module.body)
        for index, line in enumerate(body):
            # Only a simple statement line can hold an import at module level;
            # a compound statement (a `try:` guarding an optional dependency,
            # say) is left alone rather than rewritten from underneath.
            if not isinstance(line, cst.SimpleStatementLine):
                continue
            statements = list(line.body)
            for position, stmt in enumerate(statements):
                if not isinstance(stmt, cst.ImportFrom):
                    continue
                if not isinstance(stmt.module, cst.Name) or stmt.module.value != package:
                    continue
                if isinstance(stmt.names, cst.ImportStar):
                    return module
                present = {
                    alias.name.value
                    for alias in stmt.names
                    if isinstance(alias, cst.ImportAlias) and isinstance(alias.name, cst.Name)
                }
                # An alias covers the name under a different spelling.
                aliased = {
                    alias.name.value
                    for alias in stmt.names
                    if isinstance(alias, cst.ImportAlias) and alias.asname is not None
                }
                missing = [name for name in wanted if name not in present and name not in aliased]
                if not missing:
                    return module
                merged = sorted(
                    [*stmt.names, *[cst.ImportAlias(name=cst.Name(name)) for name in missing]],
                    key=lambda alias: alias.name.value if isinstance(alias.name, cst.Name) else "",
                )
                # The last alias must not carry a trailing comma.
                merged = [alias.with_changes(comma=cst.MaybeSentinel.DEFAULT) for alias in merged]
                new_statements = list(statements)
                new_statements[position] = stmt.with_changes(names=merged)
                body[index] = line.with_changes(body=new_statements)
                return module.with_changes(body=body)

        new_import = cst.SimpleStatementLine([
            cst.ImportFrom(
                module=cst.Name(package),
                names=[cst.ImportAlias(name=cst.Name(name)) for name in sorted(wanted)],
            )
        ])
        insert_at = 0
        if body:
            first = body[0]
            if isinstance(first, cst.SimpleStatementLine) and len(first.body) == 1:
                expr = first.body[0]
                if isinstance(expr, cst.Expr) and isinstance(expr.value, cst.SimpleString):
                    # Keep a module docstring first.
                    insert_at = 1
        new_body = body[:insert_at] + [new_import] + body[insert_at:]
        return module.with_changes(body=new_body)

    def connect(
        self,
        *,
        workflow: str | None = None,
        from_expr: str | None = None,
        to_expr: str | None = None,
        from_port: Any | None = None,
        to_port: Any | None = None,
        scope_control: str | None = None,
        scope_branch: str | None = None,
    ) -> None:
        fn, body = self._get_workflow_body(workflow)
        target_block = self._find_scope_block(body, scope_control, scope_branch)
        target_body = list(target_block.body)
        if from_expr is None:
            from_expr = self._port_expr_from_spec(from_port)
        if to_expr is None:
            to_expr = self._port_expr_from_spec(to_port)
        call = cst.Call(
            func=cst.Name("connect"),
            args=[
                cst.Arg(value=cst.parse_expression(from_expr)),
                cst.Arg(value=cst.parse_expression(to_expr)),
            ],
        )
        stmt = cst.SimpleStatementLine([cst.Expr(call)])
        new_stmt_body = self._insert_statement(target_body, stmt, "connect")
        new_block = target_block.with_changes(body=new_stmt_body)
        self._update_scope_body(fn.name.value, scope_control, scope_branch, new_block)

    def delete_node(self, *, workflow: str | None = None, name: str) -> None:
        fn, _ = self._get_workflow_body(workflow)

        class _RemoveNode(cst.CSTTransformer):
            def __init__(self, fn_name: str, node_name: str) -> None:
                self._fn_name = fn_name
                self._node_name = node_name
                self._in_target_fn = False
                self.removed = False

            def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
                if self._in_target_fn:
                    return False
                if node.name.value == self._fn_name:
                    self._in_target_fn = True
                    return True
                return False

            def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
                if original_node.name.value == self._fn_name:
                    self._in_target_fn = False
                return updated_node

            def _is_incident_edge_expr(self, expr: cst.BaseExpression) -> bool:
                code = _expr_to_code(expr)
                return code == self._node_name or code.startswith(f"{self._node_name}.")

            def leave_SimpleStatementLine(
                self,
                original_node: cst.SimpleStatementLine,
                updated_node: cst.SimpleStatementLine,
            ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
                if not self._in_target_fn:
                    return updated_node

                kept = []
                changed = False
                for stmt in updated_node.body:
                    if any(
                        isinstance(target.target, cst.Name) and target.target.value == self._node_name
                        for target in getattr(stmt, "targets", [])
                    ):
                        self.removed = True
                        changed = True
                        continue

                    if isinstance(stmt, cst.Expr) and isinstance(stmt.value, cst.Call):
                        expr = stmt.value
                        if (
                            isinstance(expr.func, cst.Name)
                            and expr.func.value == "connect"
                            and len(expr.args) == 2
                            and (
                                self._is_incident_edge_expr(expr.args[0].value)
                                or self._is_incident_edge_expr(expr.args[1].value)
                            )
                        ):
                            changed = True
                            continue

                    kept.append(stmt)

                if not changed:
                    return updated_node
                if not kept:
                    return cst.RemovalSentinel.REMOVE
                return updated_node.with_changes(body=kept)

        transformer = _RemoveNode(fn.name.value, name)
        updated = self.module.visit(transformer)
        if not transformer.removed:
            raise RewriteError(message=f"Node not found: {name}")
        self._set_module(updated)

    def delete_edge(self, *, workflow: str | None = None, from_expr: str, to_expr: str) -> None:
        fn, _ = self._get_workflow_body(workflow)

        class _RemoveEdge(cst.CSTTransformer):
            def __init__(self, fn_name: str) -> None:
                self._fn_name = fn_name
                self._in_target_fn = False
                self.removed = False

            def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
                if self._in_target_fn:
                    return False
                if node.name.value == self._fn_name:
                    self._in_target_fn = True
                    return True
                return False

            def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
                if original_node.name.value == self._fn_name:
                    self._in_target_fn = False
                return updated_node

            def leave_SimpleStatementLine(
                self,
                original_node: cst.SimpleStatementLine,
                updated_node: cst.SimpleStatementLine,
            ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
                if not self._in_target_fn or self.removed:
                    return updated_node
                for s in updated_node.body:
                    if not isinstance(s, cst.Expr):
                        continue
                    if not m.matches(s, m.Expr(value=m.Call(func=m.Name("connect")))):
                        continue
                    expr = s.value
                    if not isinstance(expr, cst.Call):
                        continue
                    if len(expr.args) != 2:
                        continue
                    left = expr.args[0].value
                    right = expr.args[1].value
                    if left is None or right is None:
                        continue
                    if cst.Module([]).code_for_node(left) == from_expr and cst.Module([]).code_for_node(right) == to_expr:
                        self.removed = True
                        return cst.RemovalSentinel.REMOVE
                return updated_node

        transformer = _RemoveEdge(fn.name.value)
        updated = self.module.visit(transformer)
        if not transformer.removed:
            raise RewriteError(message="Edge not found")
        self._set_module(updated)

    def delete_port(self, *, workflow: str | None = None, direction: str, portName: str) -> None:
        if direction not in ("input", "output"):
            raise RewriteError(message=f"Invalid port direction: {direction}")
        fn, body = self._get_workflow_body(workflow)
        ports_attr = "inputs" if direction == "input" else "outputs"

        class _RemovePort(cst.CSTTransformer):
            def __init__(self) -> None:
                self.removed = False

            def leave_SimpleStatementLine(
                self,
                original_node: cst.SimpleStatementLine,
                updated_node: cst.SimpleStatementLine,
            ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
                for stmt in updated_node.body:
                    if not isinstance(stmt, cst.Assign):
                        continue
                    if len(stmt.targets) != 1:
                        continue
                    target = stmt.targets[0].target
                    if not isinstance(target, cst.Attribute):
                        continue
                    if not isinstance(target.value, cst.Name):
                        continue
                    if target.value.value != fn.name.value or target.attr.value != ports_attr:
                        continue
                    if isinstance(stmt.value, cst.Dict):
                        elements = []
                        removed = False
                        for el in stmt.value.elements:
                            if not isinstance(el, cst.DictElement):
                                elements.append(el)
                                continue
                            if isinstance(el.key, cst.SimpleString) and el.key.value.strip('"') == portName:
                                removed = True
                                continue
                            elements.append(el)
                        if removed:
                            self.removed = True
                            if not elements:
                                return cst.RemovalSentinel.REMOVE
                            return updated_node.with_changes(body=[stmt.with_changes(value=stmt.value.with_changes(elements=elements))])
                return updated_node

        transformer = _RemovePort()
        updated = self.module.visit(transformer)
        if not transformer.removed:
            raise RewriteError(message=f"Port not found: {portName}")
        self._set_module(updated)

    def update_node_parameter(
        self,
        *,
        workflow: str | None = None,
        entity: str,
        parameterName: str,
        newValue: str,
    ) -> None:
        fn, body = self._get_workflow_body(workflow)

        class _UpdateParam(cst.CSTTransformer):
            def __init__(self) -> None:
                self.updated = False

            def leave_SimpleStatementLine(
                self,
                original_node: cst.SimpleStatementLine,
                updated_node: cst.SimpleStatementLine,
            ) -> cst.SimpleStatementLine:
                if self.updated:
                    return updated_node
                new_body: list[cst.BaseSmallStatement] = []
                changed = False
                for stmt in updated_node.body:
                    if isinstance(stmt, cst.Assign) and len(stmt.targets) == 1:
                        target = stmt.targets[0].target
                        if isinstance(target, cst.Name) and target.value == entity and isinstance(stmt.value, cst.Call):
                            args = list(stmt.value.args)
                            found = False
                            for idx, arg in enumerate(args):
                                if arg.keyword and arg.keyword.value == parameterName:
                                    args[idx] = arg.with_changes(value=cst.parse_expression(newValue))
                                    found = True
                                    changed = True
                                    self.updated = True
                                    break
                            if not found:
                                args.append(cst.Arg(keyword=cst.Name(parameterName), value=cst.parse_expression(newValue)))
                                changed = True
                                self.updated = True
                            stmt = stmt.with_changes(value=stmt.value.with_changes(args=args))
                    new_body.append(stmt)
                return updated_node.with_changes(body=new_body) if changed else updated_node

        transformer = _UpdateParam()
        updated = self.module.visit(transformer)
        if not transformer.updated:
            raise RewriteError(message=f"Entity parameter not found: {entity}.{parameterName}")
        self._set_module(updated)

    def update_definition_annotation(
        self,
        *,
        entityType: str,
        annotationName: str,
        annotationText: str,
    ) -> None:
        def _normalize_python_literal_name(value: cst.BaseExpression) -> cst.BaseExpression:
            if isinstance(value, cst.Name):
                if value.value == "true":
                    return cst.Name("True")
                if value.value == "false":
                    return cst.Name("False")
                if value.value == "null":
                    return cst.Name("None")
            return value

        parsed_decorator = cst.parse_expression(annotationText.lstrip('@'))
        if isinstance(parsed_decorator, cst.Call):
            normalized_args: list[cst.Arg] = []
            for arg in parsed_decorator.args:
                normalized_args.append(arg.with_changes(value=_normalize_python_literal_name(arg.value)))
            parsed_decorator = parsed_decorator.with_changes(args=normalized_args)

        class _UpdateAnnot(cst.CSTTransformer):
            def __init__(self) -> None:
                self.updated = False

            def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
                if self.updated or original_node.name.value != entityType:
                    return updated_node
                decorators = list(updated_node.decorators)
                new_decorator = cst.Decorator(decorator=parsed_decorator)
                replaced = False
                for idx, dec in enumerate(decorators):
                    name = None
                    if isinstance(dec.decorator, cst.Name):
                        name = dec.decorator.value
                    elif isinstance(dec.decorator, cst.Call) and isinstance(dec.decorator.func, cst.Name):
                        name = dec.decorator.func.value
                    if name == annotationName:
                        decorators[idx] = new_decorator
                        replaced = True
                        break
                if not replaced:
                    decorators.insert(0, new_decorator)
                self.updated = True
                return updated_node.with_changes(decorators=decorators)

        transformer = _UpdateAnnot()
        updated = self.module.visit(transformer)
        if not transformer.updated:
            raise RewriteError(message=f"Definition not found for annotation update: {entityType}")
        self._set_module(updated)

    def merge_definition_annotation_args(
        self,
        *,
        entityType: str,
        annotationName: str,
        argUpdates: dict[str, str],
    ) -> None:
        """Merge *argUpdates* into an existing decorator, preserving unlisted args.

        ``argUpdates`` maps keyword-argument names to Python expression strings.
        Args already present in the decorator are replaced; new args are appended.
        Args **not** in *argUpdates* are left untouched, so complex parameters the
        UI does not manage (e.g. ``outputValidators``) survive editing.
        """

        def _normalize_python_literal_name(value: cst.BaseExpression) -> cst.BaseExpression:
            if isinstance(value, cst.Name):
                if value.value == "true":
                    return cst.Name("True")
                if value.value == "false":
                    return cst.Name("False")
                if value.value == "null":
                    return cst.Name("None")
            return value

        # Parse each update value as a CST expression
        parsed_updates: dict[str, cst.BaseExpression] = {}
        for name, expr_str in argUpdates.items():
            parsed = cst.parse_expression(expr_str.strip())
            parsed = _normalize_python_literal_name(parsed)
            parsed_updates[name] = parsed

        class _MergeArgs(cst.CSTTransformer):
            def __init__(self) -> None:
                self.updated = False

            def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
                if self.updated or original_node.name.value != entityType:
                    return updated_node
                decorators = list(updated_node.decorators)
                for idx, dec in enumerate(decorators):
                    dec_name = None
                    if isinstance(dec.decorator, cst.Name):
                        dec_name = dec.decorator.value
                    elif isinstance(dec.decorator, cst.Call) and isinstance(dec.decorator.func, cst.Name):
                        dec_name = dec.decorator.func.value
                    if dec_name != annotationName:
                        continue

                    call = dec.decorator
                    if not isinstance(call, cst.Call):
                        # bare @agent with no parens → create a Call with the updates
                        new_args = [
                            cst.Arg(
                                keyword=cst.Name(k),
                                value=v,
                                equal=cst.AssignEqual(
                                    whitespace_before=cst.SimpleWhitespace(""),
                                    whitespace_after=cst.SimpleWhitespace(""),
                                ),
                            )
                            for k, v in parsed_updates.items()
                        ]
                        # Add trailing comma + newline formatting
                        formatted = []
                        for i, a in enumerate(new_args):
                            comma = cst.Comma(whitespace_after=cst.ParenthesizedWhitespace(
                                indent=True,
                                last_line=cst.SimpleWhitespace("    "),
                            )) if i < len(new_args) - 1 else cst.MaybeSentinel.DEFAULT
                            formatted.append(a.with_changes(comma=comma))
                        new_call = cst.Call(func=cst.Name(dec_name), args=formatted)
                        decorators[idx] = cst.Decorator(decorator=new_call)
                        self.updated = True
                        break

                    # Merge into existing Call args
                    existing_args = list(call.args)
                    seen_keys: set[str] = set()
                    merged: list[cst.Arg] = []
                    for arg in existing_args:
                        key = arg.keyword.value if arg.keyword else None
                        if key and key in parsed_updates:
                            seen_keys.add(key)
                            merged.append(arg.with_changes(value=parsed_updates[key]))
                        else:
                            merged.append(arg)

                    # Append new args not in original
                    for k, v in parsed_updates.items():
                        if k not in seen_keys:
                            merged.append(cst.Arg(
                                keyword=cst.Name(k),
                                value=v,
                                equal=cst.AssignEqual(
                                    whitespace_before=cst.SimpleWhitespace(""),
                                    whitespace_after=cst.SimpleWhitespace(""),
                                ),
                            ))

                    # Fix trailing commas — ensure all but last have comma
                    final_args: list[cst.Arg] = []
                    for i, a in enumerate(merged):
                        if i < len(merged) - 1:
                            if isinstance(a.comma, cst.MaybeSentinel):
                                a = a.with_changes(comma=cst.Comma(whitespace_after=cst.ParenthesizedWhitespace(
                                    indent=True,
                                    last_line=cst.SimpleWhitespace("    "),
                                )))
                        final_args.append(a)

                    new_call = call.with_changes(args=final_args)
                    decorators[idx] = dec.with_changes(decorator=new_call)
                    self.updated = True
                    break

                return updated_node.with_changes(decorators=decorators)

        transformer = _MergeArgs()
        updated = self.module.visit(transformer)
        if not transformer.updated:
            raise RewriteError(message=f"Definition not found for annotation merge: {entityType}")
        self._set_module(updated)

    def remove_definition_annotation(
        self,
        *,
        entityType: str,
        annotationName: str,
    ) -> None:
        class _RemoveAnnot(cst.CSTTransformer):
            def __init__(self) -> None:
                self.updated = False

            def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
                if self.updated or original_node.name.value != entityType:
                    return updated_node
                decorators = list(updated_node.decorators)
                new_decorators: list[cst.Decorator] = []
                removed = False
                for dec in decorators:
                    name = None
                    if isinstance(dec.decorator, cst.Name):
                        name = dec.decorator.value
                    elif isinstance(dec.decorator, cst.Call) and isinstance(dec.decorator.func, cst.Name):
                        name = dec.decorator.func.value
                    if name == annotationName and not removed:
                        removed = True
                        continue
                    new_decorators.append(dec)
                if removed:
                    self.updated = True
                    return updated_node.with_changes(decorators=new_decorators)
                return updated_node

        transformer = _RemoveAnnot()
        updated = self.module.visit(transformer)
        if not transformer.updated:
            raise RewriteError(message=f"Annotation not found for removal: {entityType}.{annotationName}")
        self._set_module(updated)

    def update_definition_parameter(
        self,
        *,
        entityType: str,
        parameterName: str,
        parameterText: str,
    ) -> None:
        class _UpdateDefParam(cst.CSTTransformer):
            def __init__(self) -> None:
                self.updated = False

            def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
                if self.updated or original_node.name.value != entityType:
                    return updated_node
                parsed_name, parsed_type, parsed_default = self._parse_parameter_text(parameterText)
                params = list(updated_node.params.params)
                replacement = self._build_param(parsed_name, parsed_type, parsed_default)
                replaced = False
                for idx, param in enumerate(params):
                    if param.name.value == parameterName:
                        params[idx] = replacement
                        replaced = True
                        break
                if not replaced:
                    params.append(replacement)
                self.updated = True
                return updated_node.with_changes(params=updated_node.params.with_changes(params=params))

            def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
                if self.updated or original_node.name.value != entityType:
                    return updated_node
                init_fn = None
                init_idx = -1
                for idx, stmt in enumerate(updated_node.body.body):
                    if isinstance(stmt, cst.FunctionDef) and stmt.name.value == '__init__':
                        init_fn = stmt
                        init_idx = idx
                        break
                parsed_name, parsed_type, parsed_default = self._parse_parameter_text(parameterText)
                if init_fn is None:
                    params = [cst.Param(name=cst.Name('self')), self._build_param(parsed_name, parsed_type, parsed_default)]
                    init_fn = cst.FunctionDef(
                        name=cst.Name('__init__'),
                        params=cst.Parameters(params=params),
                        body=cst.IndentedBlock(body=[cst.SimpleStatementLine([cst.Pass()])])
                    )
                    body = list(updated_node.body.body)
                    body.append(init_fn)
                    self.updated = True
                    return updated_node.with_changes(body=updated_node.body.with_changes(body=body))

                params = list(init_fn.params.params)
                replaced = False
                for idx, param in enumerate(params):
                    if param.name.value == parameterName:
                        params[idx] = self._build_param(parsed_name, parsed_type, parsed_default)
                        replaced = True
                        break
                if not replaced:
                    params.append(self._build_param(parsed_name, parsed_type, parsed_default))
                body = list(updated_node.body.body)
                body[init_idx] = init_fn.with_changes(params=init_fn.params.with_changes(params=params))
                self.updated = True
                return updated_node.with_changes(body=updated_node.body.with_changes(body=body))

            def _parse_parameter_text(self, text: str) -> tuple[str, str | None, str | None]:
                name = text.strip()
                type_expr: str | None = None
                default_expr: str | None = None
                if '=' in name:
                    left, right = name.split('=', 1)
                    name = left.strip()
                    default_expr = right.strip()
                if ':' in name:
                    left, right = name.split(':', 1)
                    name = left.strip()
                    type_expr = right.strip()
                if not name.isidentifier() or keyword.iskeyword(name):
                    raise RewriteError(message=f"invalid parameter name: {name}")
                return name, type_expr, default_expr

            def _build_param(self, name: str, type_expr: str | None, default_expr: str | None) -> cst.Param:
                return cst.Param(
                    name=cst.Name(name),
                    annotation=cst.Annotation(cst.parse_expression(type_expr)) if type_expr else None,
                    default=cst.parse_expression(default_expr) if default_expr else None,
                )

        transformer = _UpdateDefParam()
        updated = self.module.visit(transformer)
        if not transformer.updated:
            raise RewriteError(message=f"Definition parameter update failed: {entityType}.{parameterName}")
        self._set_module(updated)

    def create_entity_port(
        self,
        *,
        entityType: str,
        portDirection: str,
        portName: str,
        portType: str,
    ) -> None:
        self._ensure_identifier(portName)
        class _CreatePort(cst.CSTTransformer):
            def __init__(self) -> None:
                self.updated = False

            def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
                if self.updated or original_node.name.value != entityType:
                    return updated_node
                body = list(updated_node.body.body)
                ports_idx = -1
                ports_cls = None
                for idx, stmt in enumerate(body):
                    if isinstance(stmt, cst.ClassDef) and stmt.name.value == 'Ports':
                        ports_idx = idx
                        ports_cls = stmt
                        break
                port_call = cst.Call(
                    func=cst.Subscript(
                        value=cst.Name('Port'),
                        slice=[cst.SubscriptElement(slice=cst.Index(value=cst.parse_expression(portType)))]
                    ),
                    args=[] if portDirection == 'input' else [cst.Arg(keyword=cst.Name('direction'), value=cst.SimpleString('"out"'))]
                )
                port_stmt = cst.SimpleStatementLine([
                    cst.Assign(targets=[cst.AssignTarget(target=cst.Name(portName))], value=port_call)
                ])
                if ports_cls is None:
                    ports_cls = cst.ClassDef(name=cst.Name('Ports'), body=cst.IndentedBlock(body=[port_stmt]))
                    body.append(ports_cls)
                else:
                    ports_body = list(ports_cls.body.body)
                    ports_body.append(port_stmt)
                    body[ports_idx] = ports_cls.with_changes(body=ports_cls.body.with_changes(body=ports_body))
                self.updated = True
                return updated_node.with_changes(body=updated_node.body.with_changes(body=body))

        transformer = _CreatePort()
        updated = self.module.visit(transformer)
        if not transformer.updated:
            raise RewriteError(message=f"Entity port create failed: {entityType}.{portName}")
        if 'Any' in portType:
            updated = self._ensure_any_import(updated)
        self._set_module(updated)

    def delete_entity_port(
        self,
        *,
        entityType: str,
        portDirection: str,
        portName: str,
    ) -> None:
        class _DeletePort(cst.CSTTransformer):
            def __init__(self) -> None:
                self.updated = False

            def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
                if self.updated or original_node.name.value != entityType:
                    return updated_node
                body = list(updated_node.body.body)
                for idx, stmt in enumerate(body):
                    if isinstance(stmt, cst.ClassDef) and stmt.name.value == 'Ports':
                        new_ports_body: list[cst.BaseStatement] = []
                        removed = False
                        for port_stmt in stmt.body.body:
                            if isinstance(port_stmt, cst.SimpleStatementLine):
                                matched = False
                                for s in port_stmt.body:
                                    if isinstance(s, cst.Assign) and len(s.targets) == 1 and isinstance(s.targets[0].target, cst.Name) and s.targets[0].target.value == portName:
                                        matched = True
                                        removed = True
                                        break
                                if matched:
                                    continue
                            if isinstance(port_stmt, cst.BaseStatement):
                                new_ports_body.append(port_stmt)
                        if removed:
                            body[idx] = stmt.with_changes(body=stmt.body.with_changes(body=new_ports_body or [cst.SimpleStatementLine([cst.Pass()])]))
                            self.updated = True
                            return updated_node.with_changes(body=updated_node.body.with_changes(body=body))
                return updated_node

        transformer = _DeletePort()
        updated = self.module.visit(transformer)
        if not transformer.updated:
            raise RewriteError(message=f"Entity port delete failed: {entityType}.{portName}")
        self._set_module(updated)

    def rename_node(self, *, workflow: str | None = None, old: str, new: str) -> None:
        self._ensure_identifier(new)
        fn, _ = self._get_workflow_body(workflow)

        class _RenameNodeTransformer(cst.CSTTransformer):
            def __init__(self, fn_name: str, old_name: str, new_name: str) -> None:
                self._fn_name = fn_name
                self._old = old_name
                self._new = new_name
                self._in_target_fn = False
                self.renamed = False

            def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
                if self._in_target_fn:
                    return False
                if node.name.value == self._fn_name:
                    self._in_target_fn = True
                    return True
                return False

            def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
                if original_node.name.value == self._fn_name:
                    self._in_target_fn = False
                return updated_node

            def leave_Assign(self, original_node: cst.Assign, updated_node: cst.Assign) -> cst.Assign:
                if not self._in_target_fn:
                    return updated_node
                if not isinstance(updated_node.value, cst.Call):
                    return updated_node
                changed = False
                new_targets: list[cst.AssignTarget] = []
                for tgt in updated_node.targets:
                    target = tgt.target
                    if isinstance(target, cst.Name) and target.value == self._old:
                        target = target.with_changes(value=self._new)
                        changed = True
                    new_targets.append(tgt.with_changes(target=target))
                if changed:
                    self.renamed = True
                    return updated_node.with_changes(targets=new_targets)
                return updated_node

            def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.Attribute:
                if not self._in_target_fn:
                    return updated_node
                if isinstance(updated_node.value, cst.Name) and updated_node.value.value == self._old:
                    self.renamed = True
                    return updated_node.with_changes(value=updated_node.value.with_changes(value=self._new))
                return updated_node

            def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
                if not self._in_target_fn:
                    return updated_node
                if not isinstance(updated_node.func, cst.Name) or updated_node.func.value != "connect":
                    return updated_node
                changed = False
                new_args = list(updated_node.args)
                for idx in (0, 1):
                    if idx >= len(new_args):
                        continue
                    expr = new_args[idx].value
                    if isinstance(expr, cst.Name) and expr.value == self._old:
                        new_args[idx] = new_args[idx].with_changes(value=expr.with_changes(value=self._new))
                        changed = True
                    elif isinstance(expr, cst.Attribute) and isinstance(expr.value, cst.Name) and expr.value.value == self._old:
                        new_value = expr.value.with_changes(value=self._new)
                        new_args[idx] = new_args[idx].with_changes(value=expr.with_changes(value=new_value))
                        changed = True
                if changed:
                    self.renamed = True
                    return updated_node.with_changes(args=new_args)
                return updated_node

        transformer = _RenameNodeTransformer(fn.name.value, old, new)
        updated = self.module.visit(transformer)
        if not transformer.renamed:
            raise RewriteError(message=f"Node not found: {old}")
        self._set_module(updated)

    def rename_port(self, *, entity: str, port_direction: str, port_name: str, new_value: str) -> None:
        self._ensure_identifier(new_value)
        transformer = _PortRenameTransformer(entity, port_direction, port_name, new_value)
        self._set_module(self.module.visit(transformer))

    def update_port_type(self, *, entity: str, port_direction: str, port_name: str, new_value: str) -> None:
        transformer = _PortTypeTransformer(entity, port_direction, port_name, new_value)
        self._set_module(self.module.visit(transformer))

    def create_if(
        self,
        *,
        workflow: str | None = None,
        name: str,
        condition_expr: str,
        scope_control: str | None = None,
        scope_branch: str | None = None,
    ) -> None:
        self._ensure_identifier(name)
        fn, body = self._get_workflow_body(workflow)
        target_block = self._find_scope_block(body, scope_control, scope_branch)
        target_body = list(target_block.body)
        assign = cst.Assign(
            targets=[cst.AssignTarget(target=cst.Name(name))],
            value=cst.Call(func=cst.Attribute(value=cst.Name("wfpy"), attr=cst.Name("if_")), args=[cst.Arg(value=cst.parse_expression(condition_expr))]),
        )
        then_block = cst.With(
            items=[cst.WithItem(item=cst.Attribute(value=cst.Name(name), attr=cst.Name("then")))],
            body=cst.IndentedBlock(body=[cst.SimpleStatementLine([cst.Pass()])]),
        )
        else_block = cst.With(
            items=[cst.WithItem(item=cst.Attribute(value=cst.Name(name), attr=cst.Name("else_")))],
            body=cst.IndentedBlock(body=[cst.SimpleStatementLine([cst.Pass()])]),
        )
        stmts = [cst.SimpleStatementLine([assign]), then_block, else_block]
        new_stmt_body = self._insert_statement(target_body, stmts[0], "node")
        new_stmt_body = self._insert_statement(new_stmt_body, stmts[1], "node")
        new_stmt_body = self._insert_statement(new_stmt_body, stmts[2], "node")
        new_block = target_block.with_changes(body=new_stmt_body)
        self._update_scope_body(fn.name.value, scope_control, scope_branch, new_block)

    def create_loop(
        self,
        *,
        workflow: str | None = None,
        name: str,
        iterable_expr: str,
        scope_control: str | None = None,
        scope_branch: str | None = None,
    ) -> None:
        self._ensure_identifier(name)
        fn, body = self._get_workflow_body(workflow)
        target_block = self._find_scope_block(body, scope_control, scope_branch)
        target_body = list(target_block.body)
        assign = cst.Assign(
            targets=[cst.AssignTarget(target=cst.Name(name))],
            value=cst.Call(func=cst.Attribute(value=cst.Name("wfpy"), attr=cst.Name("loop")), args=[cst.Arg(value=cst.parse_expression(iterable_expr))]),
        )
        loop_block = cst.With(
            items=[cst.WithItem(item=cst.Name(name))],
            body=cst.IndentedBlock(body=[cst.SimpleStatementLine([cst.Pass()])]),
        )
        new_stmt_body = self._insert_statement(target_body, cst.SimpleStatementLine([assign]), "node")
        new_stmt_body = self._insert_statement(new_stmt_body, loop_block, "node")
        new_block = target_block.with_changes(body=new_stmt_body)
        self._update_scope_body(fn.name.value, scope_control, scope_branch, new_block)

    def wrap_in_if(self, *, workflow: str | None = None, node_names: list[str], condition_expr: str) -> None:
        self._wrap_in_control("if", workflow, node_names, condition_expr)

    def wrap_in_loop(self, *, workflow: str | None = None, node_names: list[str], iterable_expr: str) -> None:
        self._wrap_in_control("loop", workflow, node_names, iterable_expr)

    def unwrap_control(self, *, workflow: str | None = None, name: str) -> None:
        class _Unwrap(cst.CSTTransformer):
            def __init__(self, matcher: RewriteEngine) -> None:
                self._matcher = matcher
                self.unwrapped = False

            def leave_With(
                self,
                original_node: cst.With,
                updated_node: cst.With,
            ) -> cst.BaseStatement | cst.FlattenSentinel[cst.BaseStatement]:
                if self._matcher._matches_control_with_any(original_node, name):
                    self.unwrapped = True
                    statements = [s for s in updated_node.body.body if isinstance(s, cst.BaseStatement)]
                    return cst.FlattenSentinel(statements)
                return updated_node

        transformer = _Unwrap(self)
        updated = self.module.visit(transformer)
        if not transformer.unwrapped:
            raise RewriteError(message=f"Control scope not found for {name}")
        self._set_module(updated)

    def move_node(self, *, workflow: str | None = None, name: str, target_control: str, target_scope: str) -> None:
        class _ExtractAssign(cst.CSTTransformer):
            def __init__(self) -> None:
                self.extracted: cst.BaseStatement | None = None

            def leave_SimpleStatementLine(
                self,
                original_node: cst.SimpleStatementLine,
                updated_node: cst.SimpleStatementLine,
            ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
                if self.extracted is not None:
                    return updated_node
                if any(
                    m.matches(s, m.Assign(targets=[m.AssignTarget(target=m.Name(name))]))
                    for s in updated_node.body
                ):
                    self.extracted = updated_node
                    return cst.RemovalSentinel.REMOVE
                return updated_node

        extractor = _ExtractAssign()
        updated = self.module.visit(extractor)
        if extractor.extracted is None:
            raise RewriteError(message=f"Node not found: {name}")
        self._set_module(updated)

        fn, body = self._get_workflow_body(workflow)
        target_block = self._find_scope_block(body, target_control, target_scope)
        target_body = list(target_block.body)
        new_stmt_body = self._insert_statement(target_body, extractor.extracted, "node")
        new_block = target_block.with_changes(body=new_stmt_body)
        self._update_scope_body(fn.name.value, target_control, target_scope, new_block)

    def _wrap_in_control(self, kind: str, workflow: str | None, node_names: list[str], expr: str) -> None:
        if not node_names:
            raise RewriteError(message="node_names is required")
        fn, body = self._get_workflow_body(workflow)
        extracted: list[cst.BaseStatement] = []
        new_body: list[cst.BaseStatement] = []
        for stmt in body.body:
            if isinstance(stmt, cst.SimpleStatementLine) and any(
                m.matches(s, m.Assign(targets=[m.AssignTarget(target=m.Name(n))]))
                for n in node_names
                for s in stmt.body
            ):
                extracted.append(stmt)
                continue
            new_body.append(stmt)

        if not extracted:
            raise RewriteError(message="No matching nodes found")

        ctrl_name = self._unique_name(f"{kind}_1", self._collect_assigned_names(fn))
        if kind == "if":
            assign = cst.Assign(
                targets=[cst.AssignTarget(target=cst.Name(ctrl_name))],
                value=cst.Call(
                    func=cst.Attribute(value=cst.Name("wfpy"), attr=cst.Name("if_")),
                    args=[cst.Arg(value=cst.parse_expression(expr))],
                ),
            )
            with_block = cst.With(
                items=[cst.WithItem(item=cst.Attribute(value=cst.Name(ctrl_name), attr=cst.Name("then")))],
                body=cst.IndentedBlock(body=extracted),
            )
            new_body.extend([cst.SimpleStatementLine([assign]), with_block])
        else:
            assign = cst.Assign(
                targets=[cst.AssignTarget(target=cst.Name(ctrl_name))],
                value=cst.Call(
                    func=cst.Attribute(value=cst.Name("wfpy"), attr=cst.Name("loop")),
                    args=[cst.Arg(value=cst.parse_expression(expr))],
                ),
            )
            with_block = cst.With(
                items=[cst.WithItem(item=cst.Name(ctrl_name))],
                body=cst.IndentedBlock(body=extracted),
            )
            new_body.extend([cst.SimpleStatementLine([assign]), with_block])

        self._update_function_body(fn.name.value, body.with_changes(body=new_body))


class _PortBaseTransformer(cst.CSTTransformer):
    def __init__(self, entity: str, direction: str, port_name: str) -> None:
        self.entity = entity
        self.direction = self._normalize_direction(direction)
        self.port_name = port_name
        self._in_entity = False
        self._in_ports_class = False

    def _normalize_direction(self, direction: str) -> str:
        mapping = {
            "input": "in",
            "output": "out",
            "inout": "inout",
            "in": "in",
            "out": "out",
        }
        return mapping.get(direction, direction)

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        if not self._in_entity:
            if node.name.value == self.entity:
                self._in_entity = True
                return True
            return False

        if node.name.value == "Ports":
            self._in_ports_class = True
            return True
        return False

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        if original_node.name.value == "Ports" and self._in_ports_class:
            self._in_ports_class = False
        if original_node.name.value == self.entity:
            self._in_entity = False
        return updated_node

    def _matches_port_assign(self, node: cst.Assign) -> bool:
        if not self._in_ports_class:
            return False
        if not node.targets:
            return False
        target = node.targets[0].target
        if not isinstance(target, cst.Name):
            return False
        if target.value != self.port_name:
            return False
        value = node.value
        if not isinstance(value, cst.Call):
            return False
        if not isinstance(value.func, cst.Subscript):
            return False
        if not isinstance(value.func.value, cst.Name):
            return False
        if value.func.value.value != "Port":
            return False
        # direction filter if explicitly provided
        for arg in value.args:
            if arg.keyword and arg.keyword.value == "direction":
                if isinstance(arg.value, cst.SimpleString):
                    if arg.value.evaluated_value != self.direction:
                        return False
        return True


class _PortRenameTransformer(_PortBaseTransformer):
    def __init__(self, entity: str, direction: str, port_name: str, new_name: str) -> None:
        super().__init__(entity, direction, port_name)
        self.new_name = new_name

    def leave_Assign(self, original_node: cst.Assign, updated_node: cst.Assign) -> cst.Assign:
        if not self._matches_port_assign(updated_node):
            return updated_node
        return updated_node.with_changes(
            targets=[cst.AssignTarget(target=cst.Name(self.new_name))]
        )

    def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.Attribute:
        if isinstance(original_node.value, cst.Name):
            if original_node.value.value == self.entity and original_node.attr.value == self.port_name:
                return updated_node.with_changes(attr=cst.Name(self.new_name))
        return updated_node


class _PortTypeTransformer(_PortBaseTransformer):
    def __init__(self, entity: str, direction: str, port_name: str, new_type: str) -> None:
        super().__init__(entity, direction, port_name)
        self.new_type = new_type

    def leave_Assign(self, original_node: cst.Assign, updated_node: cst.Assign) -> cst.Assign:
        if not self._matches_port_assign(updated_node):
            return updated_node
        value = updated_node.value
        if not isinstance(value, cst.Call):
            return updated_node
        if not isinstance(value.func, cst.Subscript):
            return updated_node
        new_func = value.func.with_changes(
            slice=[cst.SubscriptElement(slice=cst.Index(value=cst.parse_expression(self.new_type)))]
        )
        return updated_node.with_changes(value=value.with_changes(func=new_func))
