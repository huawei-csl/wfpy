"""Where a skill name is looked up, and which names are allowed to be one.

Two things decide whether an agent's `use_skill` finds anything: the ordered
list of directories that may hold skill folders, and the name-to-directory
step inside one of them.

`WF_SKILLS_ROOT` names roots for the first. It takes several, separated by
`os.pathsep`, because a skill name may not contain a path separator, so a
nested sub-collection (`pypto-op-perf-tune/tune-swimlane`) is only reachable
by naming its parent directory as a root of its own — and one root cannot be
both a collection and one of its sub-collections.

The second step must accept a symlinked skill directory, since that is how
shared skills are installed into `.claude/skills`, while still refusing a
name that tries to leave its root.
"""

from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from wfpy._agent_prompt_runtime import (
    _claude_agent_search_roots,
    _resolve_skill_dir,
    _run_skill_hook,
    _runtime_root_for_skills,
    _skill_search_roots,
)


def make_plan(source_path: str = "", work_dir: str = "") -> types.SimpleNamespace:
    """The two plan attributes skill-root resolution reads."""

    return types.SimpleNamespace(source_path=source_path, work_dir=work_dir)


def make_skill(root: Path, name: str, body: str = "a skill") -> Path:
    """Create `<root>/<name>/SKILL.md` and return the skill directory."""

    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


class TestSkillsRootVariable:
    def test_a_single_root_still_resolves_a_skill(self, tmp_path, monkeypatch):
        """The form the variable has always taken keeps working."""
        root = tmp_path / "skills"
        skill_dir = make_skill(root, "wfroot-single")
        monkeypatch.setenv("WF_SKILLS_ROOT", str(root))

        plan = make_plan()

        assert _resolve_skill_dir("wfroot-single", plan) == skill_dir
        assert _skill_search_roots(plan)[0] == root.resolve()
        assert _runtime_root_for_skills(plan) == root.resolve()

    def test_a_skill_in_each_of_several_roots_is_found(self, tmp_path, monkeypatch):
        first = tmp_path / "collection"
        second = tmp_path / "collection" / "sub-collection"
        in_first = make_skill(first, "wfroot-in-first")
        in_second = make_skill(second, "wfroot-in-second")
        monkeypatch.setenv("WF_SKILLS_ROOT", os.pathsep.join([str(first), str(second)]))

        plan = make_plan()

        assert _resolve_skill_dir("wfroot-in-first", plan) == in_first
        assert _resolve_skill_dir("wfroot-in-second", plan) == in_second

    def test_the_earlier_root_wins_the_same_name(self, tmp_path, monkeypatch):
        first = tmp_path / "first"
        second = tmp_path / "second"
        preferred = make_skill(first, "wfroot-shared", body="from first")
        make_skill(second, "wfroot-shared", body="from second")
        monkeypatch.setenv("WF_SKILLS_ROOT", os.pathsep.join([str(first), str(second)]))

        plan = make_plan()

        assert _resolve_skill_dir("wfroot-shared", plan) == preferred

    def test_every_listed_root_is_searched_in_the_order_written(self, tmp_path, monkeypatch):
        roots = [tmp_path / "a", tmp_path / "b", tmp_path / "c"]
        for root in roots:
            root.mkdir()
        monkeypatch.setenv("WF_SKILLS_ROOT", os.pathsep.join(str(r) for r in roots))

        search_roots = _skill_search_roots(make_plan())

        assert search_roots[:3] == [r.resolve() for r in roots]

    def test_the_first_root_is_the_runtime_root(self, tmp_path, monkeypatch):
        """The runtime root is one directory: the ancestor walk starts there,
        and a skill hook runs there."""
        first = tmp_path / "first"
        second = tmp_path / "second"
        make_skill(first / ".wf" / "skills", "wfroot-walked-to")
        monkeypatch.setenv("WF_SKILLS_ROOT", os.pathsep.join([str(first), str(second)]))

        plan = make_plan()

        assert _runtime_root_for_skills(plan) == first.resolve()
        assert _resolve_skill_dir("wfroot-walked-to", plan) is not None

    def test_a_root_that_is_missing_is_not_the_runtime_root(self, tmp_path, monkeypatch):
        """A list names the folders of several tools, and a project seldom
        has them all. The runtime root is the first that is there."""
        missing = tmp_path / ".claude" / "skills"
        present = tmp_path / ".codex" / "skills"
        present.mkdir(parents=True)
        monkeypatch.setenv("WF_SKILLS_ROOT", os.pathsep.join([str(missing), str(present)]))

        assert _runtime_root_for_skills(make_plan()) == present.resolve()

    def test_with_no_root_present_the_first_is_the_runtime_root(self, tmp_path, monkeypatch):
        """As one missing directory always was."""
        first = tmp_path / "first"
        second = tmp_path / "second"
        monkeypatch.setenv("WF_SKILLS_ROOT", os.pathsep.join([str(first), str(second)]))

        assert _runtime_root_for_skills(make_plan()) == first.resolve()

    def test_a_hook_of_a_skill_in_a_later_root_runs(self, tmp_path, monkeypatch):
        """The hook's working directory is the runtime root. With the first
        root listed missing, the skill was found in the second and its hook
        then failed to start: `FileNotFoundError`, for the first."""
        missing = tmp_path / ".claude" / "skills"
        present = tmp_path / ".codex" / "skills"
        scripts = make_skill(present, "wfroot-hooked") / "scripts"
        scripts.mkdir()
        (scripts / "pre.py").write_text("import os\nprint(os.getcwd())\n", encoding="utf-8")
        monkeypatch.setenv("WF_SKILLS_ROOT", os.pathsep.join([str(missing), str(present)]))
        plan = make_plan()
        plan.options = {"skill_hook_auth": "allow-all"}

        result = _run_skill_hook(
            skill_name="wfroot-hooked",
            hook_name="pre",
            phase="pre",
            actor_name="a",
            payload={},
            response_text=None,
            plan=plan,
            timeout_ms=10_000,
        )

        assert result["ran"] and result["exitCode"] == 0, result
        assert Path(result["stdout"].strip()) == present.resolve()

    @pytest.mark.skipif(os.pathsep != ":", reason="a POSIX path may hold `:`")
    def test_one_directory_whose_path_holds_the_separator_is_one_root(self, tmp_path, monkeypatch):
        """`skills:v2` was a valid setting before the variable took a list."""
        root = tmp_path / "skills:v2"
        skill_dir = make_skill(root, "wfroot-versioned")
        monkeypatch.setenv("WF_SKILLS_ROOT", str(root))

        plan = make_plan()

        assert _resolve_skill_dir("wfroot-versioned", plan) == skill_dir
        assert _runtime_root_for_skills(plan) == root.resolve()

    def test_blank_entries_are_ignored(self, tmp_path, monkeypatch):
        root = tmp_path / "skills"
        skill_dir = make_skill(root, "wfroot-padded")
        monkeypatch.setenv("WF_SKILLS_ROOT", os.pathsep + f" {root} " + os.pathsep + os.pathsep)

        assert _resolve_skill_dir("wfroot-padded", make_plan()) == skill_dir

    def test_claude_agent_discovery_is_unchanged_by_a_single_root(self, tmp_path, monkeypatch):
        """`_runtime_root_for_claude_agents()` delegates here when
        `WF_CLAUDE_AGENTS_ROOT` is unset, so the agents walk must still start
        at the root the variable names."""
        root = tmp_path / "project"
        root.mkdir()
        monkeypatch.setenv("WF_SKILLS_ROOT", str(root))

        assert _claude_agent_search_roots(make_plan())[0] == (root / ".claude" / "agents").resolve()


class TestSymlinkedSkill:
    def test_a_symlinked_skill_directory_resolves(self, tmp_path, monkeypatch):
        """How shared skills are installed: a symlink in `.claude/skills`
        pointing at a checkout elsewhere."""
        installed = make_skill(tmp_path / "checkout", "wfroot-linked")
        root = tmp_path / "skills"
        root.mkdir()
        (root / "wfroot-linked").symlink_to(installed)
        monkeypatch.setenv("WF_SKILLS_ROOT", str(root))

        assert _resolve_skill_dir("wfroot-linked", make_plan()) == installed


class TestSkillNameTraversal:
    @pytest.mark.parametrize("name", ["../x", "..", "/etc", "a/../../b", "C:\\x"])
    def test_a_traversal_name_is_refused(self, name, tmp_path, monkeypatch):
        monkeypatch.setenv("WF_SKILLS_ROOT", str(tmp_path))

        with pytest.raises(ValueError):
            _resolve_skill_dir(name, make_plan())

    def test_a_name_that_joins_outside_its_root_finds_nothing(self, tmp_path, monkeypatch):
        """The second line of defence, with the first taken away. The name
        check refuses every such name on POSIX; a Windows drive-relative one
        (`d:bar`) passes it and joins outside the root, as an absolute path
        does here. The containment test in `_resolve_skill_dir` is what stops
        it, and no other test reaches that line."""
        import wfpy._agent_prompt_runtime as prompt_runtime

        root = tmp_path / "skills"
        root.mkdir()
        outside = make_skill(tmp_path / "elsewhere", "wfroot-outside")
        monkeypatch.setenv("WF_SKILLS_ROOT", str(root))
        monkeypatch.setattr(prompt_runtime, "_validate_skill_name", lambda name: None)

        assert _resolve_skill_dir(str(outside), make_plan()) is None


class TestAncestorWalk:
    def test_a_claude_skill_beside_the_workflow_is_found(self, tmp_path):
        project = tmp_path / "project"
        skill_dir = make_skill(project / ".claude" / "skills", "wfroot-walk-claude")
        (project / "wf.py").write_text("", encoding="utf-8")

        plan = make_plan(source_path=str(project / "wf.py"))

        assert _resolve_skill_dir("wfroot-walk-claude", plan) == skill_dir

    def test_a_wf_skill_above_the_workflow_is_found(self, tmp_path):
        project = tmp_path / "project"
        skill_dir = make_skill(project / ".wf" / "skills", "wfroot-walk-wf")
        nested = project / "flows" / "nested"
        nested.mkdir(parents=True)
        (nested / "wf.py").write_text("", encoding="utf-8")

        plan = make_plan(source_path=str(nested / "wf.py"))

        assert _resolve_skill_dir("wfroot-walk-wf", plan) == skill_dir

    def test_wf_skills_outranks_claude_skills_at_the_same_level(self, tmp_path):
        project = tmp_path / "project"
        preferred = make_skill(project / ".wf" / "skills", "wfroot-both", body="wf")
        make_skill(project / ".claude" / "skills", "wfroot-both", body="claude")
        (project / "wf.py").write_text("", encoding="utf-8")

        plan = make_plan(source_path=str(project / "wf.py"))

        assert _resolve_skill_dir("wfroot-both", plan) == preferred
