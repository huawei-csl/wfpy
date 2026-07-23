"""Base CST transformers and utilities for rewrite operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import libcst as cst

if TYPE_CHECKING:
    from wfpy.rewrite import RewriteError


class SingleMatchTransformer(cst.CSTTransformer):
    """Base transformer that expects to match exactly one target.

    Subclasses should set `self.found = True` when they successfully transform
    a target node. After visiting, the caller should check `transformer.found`
    and raise RewriteError if False.

    Example usage:
        class _MyTransformer(SingleMatchTransformer):
            def leave_SimpleStatementLine(self, original_node, updated_node):
                if matches_criteria(original_node):
                    self.found = True
                    return modified_node
                return updated_node

        transformer = _MyTransformer()
        updated = module.visit(transformer)
        if not transformer.found:
            raise RewriteError(message="Target not found")
    """

    def __init__(self) -> None:
        super().__init__()
        self.found = False


class ScopedFunctionTransformer(cst.CSTTransformer):
    """Base transformer that only operates within a specific function.

    Subclasses should override `leave_*` methods to perform transformations.
    Those methods should check `self._in_target_fn` before transforming.

    Example usage:
        class _MyTransformer(ScopedFunctionTransformer):
            def leave_SimpleStatementLine(self, original_node, updated_node):
                if not self._in_target_fn:
                    return updated_node
                # ... transformation logic ...
                self.found = True
                return modified_node
    """

    def __init__(self, fn_name: str) -> None:
        super().__init__()
        self._fn_name = fn_name
        self._in_target_fn = False
        self.found = False

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        """Enter the target function, skip others."""
        if self._in_target_fn:
            return False
        if node.name.value == self._fn_name:
            self._in_target_fn = True
            return True
        return False

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        """Exit the target function."""
        if original_node.name.value == self._fn_name:
            self._in_target_fn = False
        return updated_node


def extract_decorator_name(decorator: cst.Decorator) -> str | None:
    """Extract the name from a decorator node.

    Handles both simple decorators (@name) and call decorators (@name(...)).
    Returns None if the decorator structure is unrecognized.
    """
    if isinstance(decorator.decorator, cst.Name):
        return decorator.decorator.value
    if isinstance(decorator.decorator, cst.Call) and isinstance(
        decorator.decorator.func, cst.Name
    ):
        return decorator.decorator.func.value
    return None


def normalize_python_literal_name(name: str) -> str:
    """Normalize a name to be a valid Python literal (string/number/bool/None).

    Converts Python keywords to their literal equivalents:
    - "true" -> True
    - "false" -> False
    - "none" -> None
    - numeric strings -> int/float
    - otherwise -> quoted string
    """
    if name.lower() == "true":
        return "True"
    if name.lower() == "false":
        return "False"
    if name.lower() == "none":
        return "None"

    # Try to parse as number
    try:
        if "." in name:
            float(name)
            return name
        else:
            int(name)
            return name
    except ValueError:
        pass

    # Default to quoted string
    return f'"{name}"'
