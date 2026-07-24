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

# path stem -> (workflow name, run kwargs, expected outputs, compare mode)
# compare "eq" asserts equality; "sorted" compares each port's tokens as a
# multiset (for the nondeterministic merge in 03, whose order is unspecified).
EXPECTATIONS: dict[str, tuple[str, dict[str, Any], dict[str, Any], str]] = {
    "01_simple_task": ("doubling", {"inputs": {"In": 21}}, {"Out": [42]}, "eq"),
    "02_pipeline": (
        "scaled_sum",
        {"inputs": {"A": 5, "B": 10}},
        {"Out": [40]},
        "eq",
    ),
    "03_streams_and_nondeterminism": (
        "merging",
        {},
        {"Out": [1, 2, 3, 10, 20, 30]},
        "sorted",
    ),
    "04_guarded_actions": (
        "splitting",
        {},
        {"P": [1, 0, 4], "N": [-2]},
        "eq",
    ),
    "05_state": ("running_total", {}, {"Out": [1, 3, 6, 10]}, "eq"),
    "06_schedules": ("alternating", {}, {"Out": [1, 2, 3, 4, 5, 6]}, "eq"),
    "07_priorities": (
        "routing",
        {},
        {"X": [4, 6], "Y": [9], "Z": [5]},
        "eq",
    ),
    "08_networks": ("running_sum", {}, {"Out": [1, 2, 3, 4, 5, 6]}, "eq"),
    "09_agents_are_actors": (
        "summarize",
        {"inputs": {"In": "a long paragraph of text"}},
        {"Out": ["[summary] a one-line summary"]},
        "eq",
    ),
    "10_parallel_agents": (
        "analyze",
        {"inputs": {"In": "quarterly results are strong"}},
        {"Out": ["sentiment=positive | summary=a short summary | keywords=alpha, beta"]},
        "eq",
    ),
    "11_routing": (
        "triage",
        {"inputs": {"In": "please review my code"}},
        {"Out": ["[code expert] here's the fix"]},
        "eq",
    ),
    "12_repair_loop": (
        "refine_until_good",
        {"inputs": {"Brief": "write a haiku about autumn"}},
        {"Final": [8]},
        "eq",
    ),
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
    workflow_name, run_kwargs, expected, compare = EXPECTATIONS[path.stem]

    module = _load_module(str(path))
    workflows = _find_workflows(module)
    assert workflow_name in workflows, f"{path.name} should define {workflow_name!r}"

    kwargs = dict(run_kwargs)
    kwargs.setdefault("out_dir", str(tmp_path))
    outputs = run(workflows[workflow_name], verbose=False, **kwargs)

    if compare == "sorted":
        norm = {k: sorted(v) for k, v in outputs.items()}
        assert norm == {k: sorted(v) for k, v in expected.items()}
    else:
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
