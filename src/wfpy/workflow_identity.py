from __future__ import annotations

import ast
import dataclasses


@dataclasses.dataclass(frozen=True)
class DynamicWorkflowIdentityIssue:
    workflow_name: str
    line: int


def _is_workflow_decorator(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id == "workflow"
    return isinstance(target, ast.Attribute) and target.attr == "workflow"


def detect_dynamic_workflow_identity_issues(source_text: str) -> list[DynamicWorkflowIdentityIssue]:
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return []

    workflow_names: set[str] = set()

    class _WorkflowCollector(ast.NodeVisitor):
        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if any(_is_workflow_decorator(decorator) for decorator in node.decorator_list):
                workflow_names.add(node.name)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if any(_is_workflow_decorator(decorator) for decorator in node.decorator_list):
                workflow_names.add(node.name)
            self.generic_visit(node)

    _WorkflowCollector().visit(tree)

    issues: list[DynamicWorkflowIdentityIssue] = []

    class _DynamicIdentityCollector(ast.NodeVisitor):
        def _record_if_dynamic_name_mutation(self, target: ast.expr, line: int) -> None:
            if not isinstance(target, ast.Attribute):
                return
            if target.attr != "__name__":
                return
            if not isinstance(target.value, ast.Name):
                return
            if target.value.id not in workflow_names:
                return
            issues.append(DynamicWorkflowIdentityIssue(workflow_name=target.value.id, line=line))

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            self._record_if_dynamic_name_mutation(node.target, node.lineno)
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                self._record_if_dynamic_name_mutation(target, node.lineno)
            self.generic_visit(node)

    _DynamicIdentityCollector().visit(tree)
    issues.sort(key=lambda issue: (issue.line, issue.workflow_name))
    return issues


def format_dynamic_workflow_identity_error(
    file_path: str,
    issues: list[DynamicWorkflowIdentityIssue],
) -> str:
    first = issues[0]
    suffix = ""
    if len(issues) > 1:
        suffix = f" Found {len(issues) - 1} additional dynamic workflow rename(s)."

    return (
        f"Unsupported dynamic workflow naming in {file_path}: @workflow function "
        f"{first.workflow_name!r} assigns to __name__ at line {first.line}. "
        "wfpy requires statically discoverable workflow names for CLI and sidecar graph loading. "
        "Define a stable exported @workflow function and move parameterization into helper code or "
        "a factory without mutating __name__."
        f"{suffix}"
    )