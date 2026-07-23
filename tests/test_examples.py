"""Every example is executed, and doubles as a fixture for the tooling.

Two jobs:

1. **Anti-rot.** The previous example set decayed unnoticed because nothing ran
   it. Each example here is executed with known inputs and its result asserted,
   so a change that breaks one fails the suite.

2. **Tooling fixtures.** The examples are also the corpus for the paths the IDE
   depends on — the CLI module loader, workflow discovery, plan IR export and
   graph IR export. Those are exercised against every example, mirroring what
   ``wfpy plan`` does in ``cli.cmd_plan``.

Examples must stay dependency-free: no credentials, no network, no optional
extras.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wfpy.cli import _find_workflows, _load_module
from wfpy.graph import export_graph_json
from wfpy.runner import _build_workflow_graph, build_plan, export_plan_json, run

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

# path stem -> (workflow name, run kwargs, expected outputs)
EXPECTATIONS: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {
    "01_actor_and_ports": ("doubling", {"inputs": {"In": 21}}, {"Out": [42]}),
    "02_pipeline": (
        "shouting",
        {"inputs": {"In": "  hello, dataflow  "}},
        {"Out": ["HELLO, DATAFLOW!"]},
    ),
    "03_fan_out_and_join": (
        "fan_out_join",
        {"inputs": {"In": 1}},
        {"Out": ["slow-a + slow-b"]},
    ),
    "04_guards": ("classifying", {"inputs": {"In": 12}}, {"Out": ["big:12"]}),
    "05_state": ("running_total", {}, {"Out": [100, 250, 400]}),
    "06_inspecting_a_run": ("traced", {"inputs": {"In": 1}}, {"Out": ["L|R"]}),
    "07_convergence_loop": ("countdown", {"inputs": {"In": 4}}, {"Out": [10]}),
    "08_nested_workflows": (
        "greeting",
        {"inputs": {"In": "  Hello, WORLD  "}},
        {"Out": ["greeting: hello, world!"]},
    ),
    "09_loop": ("running_squares", {}, {"Out": [1, 5, 14, 30]}),
}

EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("[0-9][0-9]_*.py"))


def _stem(path: Path) -> str:
    return path.stem


def test_every_example_file_is_covered():
    """A new example without an entry here would otherwise be silently untested."""
    assert EXAMPLE_FILES, f"no examples found in {EXAMPLES_DIR}"
    assert {p.stem for p in EXAMPLE_FILES} == set(EXPECTATIONS)


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=_stem)
def test_example_runs_and_produces_documented_result(path: Path, tmp_path: Path):
    workflow_name, run_kwargs, expected = EXPECTATIONS[path.stem]

    module = _load_module(str(path))
    workflows = _find_workflows(module)
    assert workflow_name in workflows, f"{path.name} should define {workflow_name!r}"

    kwargs = dict(run_kwargs)
    kwargs.setdefault("out_dir", str(tmp_path))
    outputs = run(workflows[workflow_name], verbose=False, **kwargs)

    assert outputs == expected


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=_stem)
def test_example_exports_plan_and_graph_ir(path: Path):
    """Mirrors cli.cmd_plan: the loader, discovery, and both export formats."""
    module = _load_module(str(path))
    workflows = _find_workflows(module)
    assert workflows, f"{path.name} exposes no @workflow to the CLI"

    for wf in workflows.values():
        wf_def = wf._wfpy_workflow
        graph = _build_workflow_graph(wf_def)

        graph_json = export_graph_json(graph)
        assert graph_json, f"{path.name}: empty graph IR"

        plan_json = export_plan_json(build_plan(graph, wf_def))
        assert plan_json, f"{path.name}: empty plan IR"


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=_stem)
def test_example_documents_itself(path: Path):
    """Each example teaches by its docstring, so require one with a run recipe."""
    docstring = _load_module(str(path)).__doc__
    assert docstring, f"{path.name} has no module docstring"
    assert "Run it::" in docstring, f"{path.name} docstring lacks a 'Run it::' block"
