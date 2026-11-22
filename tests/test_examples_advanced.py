"""The advanced examples are executed too: workflows that write workflows.

``examples/advanced`` holds graphs in which a node emits a workflow file and
another node runs it with ``wfpy run``, as a subprocess. Each example is run
here offline, as ``tests/test_examples.py`` runs the tutorial, and what it is
documented to give is asserted.

Beyond the results, three claims of that folder's README are held here: the
generated graph follows the data it was written for (03), the recursion stops
at its limit with an error that names it (04), and a failed candidate is a
token and not the end of the run (05).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from wfpy.cli import _find_workflows, _load_module
from wfpy.graph import export_graph_json
from wfpy.runner import _build_workflow_graph, build_plan, export_plan_json, run

ADVANCED_DIR = Path(__file__).resolve().parent.parent / "examples" / "advanced"

# path stem -> (workflow name, run kwargs, expected outputs)
EXPECTATIONS: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {
    "01_workflow_as_token": ("generated", {"inputs": {"X": 21}}, {"Answer": [42]}),
    "02_recursive_workflow": ("factorial", {}, {"Answer": [24]}),
    "03_data_shaped_workflow": (
        "counted",
        {},
        {
            "Answer": [
                {
                    "Total": 41,
                    "Counts": {
                        "actors.txt": 9,
                        "guards.txt": 12,
                        "queues.txt": 10,
                        "tokens.txt": 10,
                    },
                }
            ],
            "Nodes": [["count_actors", "count_guards", "count_queues", "count_tokens", "join"]],
        },
    ),
    "04_recursive_delegation": (
        "counted_tree",
        {},
        {
            "Answer": [
                {
                    "Total": 44,
                    "Counts": {
                        "readme.txt": 6,
                        "model/actors.txt": 8,
                        "model/queues.txt": 7,
                        "model/rules/guards.txt": 7,
                        "model/rules/priorities.txt": 9,
                        "tools/cli.txt": 7,
                    },
                }
            ]
        },
    ),
    "05_repair_generated_workflow": (
        "repaired",
        {},
        {"Answer": [{"ok": True, "answer": 42, "revisions": 1}]},
    ),
}

EXAMPLE_FILES = sorted(ADVANCED_DIR.glob("[0-9][0-9]_*.py"))


def _stem(path: Path) -> str:
    return path.stem


def _workflow(path: Path) -> Any:
    workflow_name = EXPECTATIONS[path.stem][0]
    workflows = _find_workflows(_load_module(str(path)))
    assert workflow_name in workflows, f"{path.name} should define {workflow_name!r}"
    return workflows[workflow_name]


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """These run with no model: a shell that asks for a real one must not leak in."""
    for name in ("TRANSPORT", "CONNECTOR", "MODEL", "LEVEL", "MAX_LEVEL"):
        monkeypatch.delenv(f"WFPY_EXAMPLE_{name}", raising=False)


def test_every_advanced_example_is_covered():
    """A new example without an entry here would otherwise be silently untested."""
    assert EXAMPLE_FILES, f"no examples found in {ADVANCED_DIR}"
    assert {p.stem for p in EXAMPLE_FILES} == set(EXPECTATIONS)


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=_stem)
def test_advanced_example_runs_and_produces_documented_result(path: Path, tmp_path: Path):
    _, run_kwargs, expected = EXPECTATIONS[path.stem]
    outputs = run(_workflow(path), verbose=False, out_dir=str(tmp_path), **run_kwargs)
    assert outputs == expected


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=_stem)
def test_advanced_example_exports_plan_and_graph_ir(path: Path):
    """Mirrors cli.cmd_plan: the loader, discovery, and both export formats."""
    wf_def = _workflow(path)._wfpy_workflow
    graph = _build_workflow_graph(wf_def)
    assert export_graph_json(graph), f"{path.name}: empty graph IR"
    assert export_plan_json(build_plan(graph, wf_def)), f"{path.name}: empty plan IR"


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=_stem)
def test_advanced_example_documents_itself(path: Path):
    docstring = _load_module(str(path)).__doc__
    assert docstring, f"{path.name} has no module docstring"
    assert "Run it::" in docstring, f"{path.name} docstring lacks a 'Run it::' block"


def test_recursion_is_one_run_a_level(tmp_path: Path):
    """02: level 4 down to level 0, each a run of its own under the one above."""
    run(_workflow(ADVANCED_DIR / "02_recursive_workflow.py"), verbose=False, out_dir=str(tmp_path))
    records = sorted(tmp_path.rglob("child/run.wf-run.json"), key=lambda p: len(p.parts))
    answers = [json.loads(p.read_text())["outputs"]["Answer"] for p in records]
    assert answers == [[6], [2], [1], [1]]


def test_generated_graph_follows_the_folder(tmp_path: Path):
    """03 and 04: what the scripted author writes has a node per file and per sub-folder."""
    scripted_author = _load_module(str(ADVANCED_DIR / "scripted_author.py"))

    def nodes(listing: dict[str, Any]) -> list[str]:
        source = tmp_path / f"generated_{len(listing['files'])}_{len(listing['folders'])}.py"
        source.write_text(scripted_author.workflow_source(listing), encoding="utf-8")
        workflow = _find_workflows(_load_module(str(source)))["counting"]
        graph = export_graph_json(_build_workflow_graph(workflow._wfpy_workflow))["graph"]
        return sorted(n["label"] for n in graph["nodes"] if not n["kind"].startswith("wf-"))

    folder = str(ADVANCED_DIR / "data" / "tree")
    two = nodes({"folder": folder, "files": ["a.txt", "b.txt"], "folders": []})
    three = nodes({"folder": folder, "files": ["a.txt", "b.txt", "c.txt"], "folders": []})
    nested = nodes({"folder": folder, "files": ["readme.txt"], "folders": ["model", "tools"]})

    assert two == ["count_a", "count_b", "join"]
    assert three == ["count_a", "count_b", "count_c", "join"]
    assert nested == [
        "count_readme",
        "join",
        "listing_model",
        "listing_tools",
        "sub_model",
        "sub_tools",
    ]


def test_generated_graph_changes_with_the_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """04 on a copy of its tree with one more sub-folder: one more level is counted."""
    tree = tmp_path / "tree"
    shutil.copytree(ADVANCED_DIR / "data" / "tree", tree)
    extra = tree / "tools" / "extra"
    extra.mkdir()
    (extra / "more.txt").write_text("three more words\n", encoding="utf-8")

    module = _load_module(str(ADVANCED_DIR / "04_recursive_delegation.py"))
    monkeypatch.setattr(module, "TREE", tree)
    outputs = run(module.counted_tree, verbose=False, out_dir=str(tmp_path / "out"))

    answer = outputs["Answer"][0]
    assert answer["Total"] == 47
    assert answer["Counts"]["tools/extra/more.txt"] == 3


def test_recursion_stops_at_its_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """04: the tree is three levels deep; with the limit at 1 the run ends, and says why."""
    monkeypatch.setenv("WFPY_EXAMPLE_MAX_LEVEL", "1")
    workflow = _workflow(ADVANCED_DIR / "04_recursive_delegation.py")
    with pytest.raises(RuntimeError, match="delegation is 2 levels deep; the limit is 1"):
        run(workflow, verbose=False, out_dir=str(tmp_path))


def test_failed_candidate_is_a_token_not_the_end_of_the_run(tmp_path: Path):
    """05: the first candidate fails, and what the reviser was sent says how."""
    module = _load_module(str(ADVANCED_DIR / "05_repair_generated_workflow.py"))
    check = module.Check()
    candidate = tmp_path / "candidate.py"

    candidate.write_text(module.BROKEN, encoding="utf-8")
    broken = check._run(str(candidate))
    assert not broken.ok
    assert "name 'six' is not defined" in broken.error

    candidate.write_text("this is not python\n", encoding="utf-8")
    assert not check._run(str(candidate)).ok

    # Exit 0 is not success: a workflow that runs and gives no `Answer` fails too.
    candidate.write_text(module.FIXED.replace('"Answer"', '"Other"'), encoding="utf-8")
    silent = check._run(str(candidate))
    assert not silent.ok
    assert "no token on `Answer`" in silent.error

    candidate.write_text(module.FIXED, encoding="utf-8")
    assert check._run(str(candidate)) == module.Outcome(True, str(candidate), answer=42)


def test_budget_ends_the_repair_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """05 with a reviser that never corrects the file: two revisions, then it gives up."""
    module = _load_module(str(ADVANCED_DIR / "05_repair_generated_workflow.py"))
    reviser = module.Reviser._wfpy_meta.agent_spec
    monkeypatch.setattr(reviser, "mock_outputs", {"Workflow": module.BROKEN})

    outputs = run(module.repaired, verbose=False, out_dir=str(tmp_path))

    answer = outputs["Answer"][0]
    assert answer["ok"] is False
    assert answer["revisions"] == 2
    assert "name 'six' is not defined" in answer["error"]
