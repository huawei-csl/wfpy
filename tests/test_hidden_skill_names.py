"""A name that starts with `.` names no skill, preset or hook.

`wfpy skills` skips hidden entries (the `.git` of a skills folder kept in
git, Codex's `.system` root), as Codex and Claude Code do. A run must refuse
the same names, or `skill=".draft"` loads `.claude/skills/.draft`, which no
list offers, and `skill=".system"` takes Codex's `.system` root for a skill.
"""

from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from wfpy._agent_prompt_runtime import (
    _read_skill_md,
    _resolve_claude_agent_path,
    _resolve_skill_dir,
    _resolve_skill_hook_script,
    _validate_claude_agent_name,
    _validate_skill_name,
)
from wfpy._agent_skill_lookup import agent_skill_lookup


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A home and a working directory of the test's own, and no variable or
    git configuration of the machine's."""

    for name in ("WF_SKILLS_ROOT", "WF_CLAUDE_AGENTS_ROOT", "CODEX_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)


def skill(root: Path, name: str, text: str = "a skill\n") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    return directory


def flow(directory: Path) -> types.SimpleNamespace:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "flow.py").write_text("", encoding="utf-8")
    return types.SimpleNamespace(source_path=str(directory / "flow.py"), work_dir=None)


class TestASkillName:
    @pytest.mark.parametrize("name", [".draft", ".system", ".git", ".", ". draft"])
    def test_one_that_starts_with_a_dot_is_refused(self, name):
        with pytest.raises(ValueError, match=r"Skill name must not start with '\.'"):
            _validate_skill_name(name)

    def test_a_hidden_skill_folder_is_not_loaded(self, tmp_path):
        """`.claude/skills/.draft` holds a `SKILL.md`; the run refuses the
        name rather than load what `wfpy skills` does not offer."""
        project = tmp_path / "project"
        skill(project / ".claude" / "skills", ".draft")
        plan = flow(project)

        with pytest.raises(ValueError, match="must not start with"):
            _resolve_skill_dir(".draft", plan)
        with pytest.raises(ValueError, match="must not start with"):
            _read_skill_md(".draft", plan)

    def test_codexs_system_root_is_no_skill(self, tmp_path, monkeypatch):
        """`$CODEX_HOME/skills/.system` is a root of its own; with a
        `SKILL.md` in it, `skill=".system"` took it for a skill."""
        codex_home = tmp_path / "codex-home"
        system = codex_home / "skills" / ".system"
        skill(system, "bundled")
        (system / "SKILL.md").write_text("not a skill\n", encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        plan = flow(tmp_path / "project")

        with pytest.raises(ValueError, match="must not start with"):
            _resolve_skill_dir(".system", plan)
        assert _resolve_skill_dir("bundled", plan) == (system / "bundled").resolve()

    @pytest.mark.parametrize("name", ["v1.2", "my.skill", "draft.v2"])
    def test_a_dot_elsewhere_is_allowed(self, name, tmp_path):
        project = tmp_path / "project"
        found = skill(project / ".claude" / "skills", name)

        assert _resolve_skill_dir(name, flow(project)) == found.resolve()


class TestAPresetName:
    def test_one_that_starts_with_a_dot_is_refused(self):
        with pytest.raises(ValueError, match=r"Agent preset name must not start with '\.'"):
            _validate_claude_agent_name(".hidden")

    def test_a_hidden_preset_file_is_not_loaded(self, tmp_path):
        project = tmp_path / "project"
        agents = project / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / ".hidden.md").write_text("Hidden.\n", encoding="utf-8")

        with pytest.raises(ValueError, match="must not start with"):
            _resolve_claude_agent_path(".hidden", flow(project))

    def test_a_run_whose_preset_preloads_a_hidden_skill_is_warned(self, tmp_path):
        """A preloaded skill that cannot be read is a warning of the
        preset's, as a missing one is: the agent runs with the rest of its
        instructions, and the warning is in the firing's debug data."""
        from wfpy._agent_prompt_runtime import _build_effective_agent_prompt
        from wfpy.core import AgentSpec

        project = tmp_path / "project"
        skill(project / ".claude" / "skills", ".draft")
        agents = project / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "writer.md").write_text("---\nskills: .draft\n---\nbody\n", encoding="utf-8")
        spec = AgentSpec(
            prompt="do it", claude_agent="writer", use_claude_agent=True, use_skill=False
        )

        _prompt, debug = _build_effective_agent_prompt(spec, flow(project))

        [warning] = debug["claudeAgentProfile"]["warnings"]
        assert "failed to preload claude-agent skill '.draft'" in warning
        assert "must not start with" in warning

    def test_a_preset_that_preloads_a_hidden_skill_is_told_why(self, tmp_path):
        project = tmp_path / "project"
        skill(project / ".claude" / "skills", ".draft")
        agents = project / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "writer.md").write_text("---\nskills: .draft\n---\nbody\n", encoding="utf-8")
        plan = flow(project)

        [writer] = agent_skill_lookup(plan.source_path)["presets"]

        assert writer["skills"][0]["dir"] is None
        assert "must not start with" in writer["skills"][0]["problem"]


class TestAHookName:
    def test_one_that_starts_with_a_dot_is_refused(self, tmp_path):
        project = tmp_path / "project"
        directory = skill(project / ".claude" / "skills", "review")
        (directory / "scripts").mkdir()
        (directory / "scripts" / ".pre.sh").write_text("echo ran\n", encoding="utf-8")

        with pytest.raises(ValueError, match=r"Hook name must not start with '\.'"):
            _resolve_skill_hook_script("review", ".pre", flow(project))

    def test_the_list_says_why_a_hidden_hook_does_not_run(self, tmp_path):
        project = tmp_path / "project"
        directory = skill(
            project / ".claude" / "skills", "review", "---\nhooks:\n  pre: .pre\n---\nbody\n"
        )
        (directory / "scripts").mkdir()
        (directory / "scripts" / ".pre.sh").write_text("echo ran\n", encoding="utf-8")
        plan = flow(project)

        [review] = agent_skill_lookup(plan.source_path)["skills"]

        assert review["hooks"]["pre"]["script"] is None
        assert "must not start with" in review["hooks"]["pre"]["problem"]
        assert review["scripts"] == []


class TestAHooksScriptIsInsideTheSkill:
    def test_a_name_that_joins_outside_scripts_takes_no_script(self, tmp_path):
        """What validation leaves is a Windows drive-relative name,
        `d:evil`, which joins outside `scripts/` as it does outside a skills
        root (`_find_skill`)."""
        from wfpy._agent_prompt_runtime import _skill_hook_script_in

        directory = skill(tmp_path / "root", "review")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "pre.sh").write_text("echo ran\n", encoding="utf-8")
        planted = directory / "scripts" / "d:evil.sh"
        planted.write_text("echo planted\n", encoding="utf-8")

        assert _skill_hook_script_in(directory, "pre") == (
            (directory / "scripts" / "pre.sh").resolve(),
            "sh",
        )
        joined = Path("d:evil.sh")
        if joined.is_absolute() or joined.drive:
            assert _skill_hook_script_in(directory, "d:evil") is None
        else:
            # On this platform the name is a plain file name: it is inside
            # `scripts/`, and the script there is the hook's.
            assert _skill_hook_script_in(directory, "d:evil") == (planted.resolve(), "sh")


class TestTheListAndTheRunAgree:
    def test_a_name_with_spaces_around_it_is_refused_and_not_listed(self, tmp_path):
        """A run reads `skill=" draft "` as `draft`, so the folder
        `" draft "` is no name it can resolve; the list offered it."""
        project = tmp_path / "project"
        root = project / ".claude" / "skills"
        skill(root, " .draft ")
        skill(root, "shown")
        plan = flow(project)

        with pytest.raises(ValueError, match="must not begin or end with whitespace"):
            _resolve_skill_dir(" .draft ", plan)
        assert [entry["name"] for entry in agent_skill_lookup(plan.source_path)["skills"]] == [
            "shown"
        ]

    def test_a_name_the_run_refuses_is_not_listed(self, tmp_path):
        """The list offers the names a run takes, and no other."""
        project = tmp_path / "project"
        root = project / ".claude" / "skills"
        skill(root, "shown")
        skill(root, ".draft")
        (root / ".git").mkdir()
        agents = project / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "shown.md").write_text("Shown.\n", encoding="utf-8")
        (agents / ".hidden.md").write_text("Hidden.\n", encoding="utf-8")
        (agents / ".hidden-folder").mkdir()
        (agents / ".hidden-folder" / "AGENT.md").write_text("Hidden.\n", encoding="utf-8")
        plan = flow(project)

        answer = agent_skill_lookup(plan.source_path)

        assert [entry["name"] for entry in answer["skills"]] == ["shown"]
        assert [entry["name"] for entry in answer["presets"]] == ["shown"]
