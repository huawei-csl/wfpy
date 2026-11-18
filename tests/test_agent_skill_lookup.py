"""The sidecar's `agentSkills`: what a run of a workflow finds, for an IDE to offer.

The answer is the runtime's own lookup, so each test holds it against what
the runtime resolves (`_skill_search_roots`, `_resolve_skill_dir`,
`_read_skill_md`, `_resolve_claude_agent_path`) rather than restating the
rules.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from wfpy._agent_prompt_runtime import (
    _claude_agent_search_roots,
    _read_skill_md,
    _resolve_claude_agent_path,
    _resolve_skill_dir,
    _skill_search_roots,
)
from wfpy._agent_skill_lookup import agent_skill_lookup
from wfpy._version import runtime_version
from wfpy.sidecar import FEATURES, SUPPORTED_OPS, _handle_request


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A home and a working directory of the test's own, and no variable of
    the machine's."""

    for name in ("WF_SKILLS_ROOT", "WF_CLAUDE_AGENTS_ROOT", "CODEX_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
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


def workflow(project: Path) -> Path:
    (project / ".git").mkdir(parents=True, exist_ok=True)
    (project / "flows").mkdir(exist_ok=True)
    source = project / "flows" / "flow.py"
    source.write_text("this is not python yet (\n", encoding="utf-8")
    return source


def by_name(entries: list[dict]) -> dict[str, dict]:
    return {entry["name"]: entry for entry in entries}


class TestTheAnswerIsTheRuntimes:
    def test_roots_and_winners_are_what_a_run_resolves(self, tmp_path):
        project = tmp_path / "project"
        source = workflow(project)
        skill(project / "flows" / ".claude" / "skills", "shared")
        skill(project / ".agents" / "skills", "shared")
        skill(project / ".agents" / "skills", "only-agents")
        skill(tmp_path / "home" / ".agents" / "skills", "only-user")
        (project / ".opencode" / "skills" / "no-skill-md").mkdir(parents=True)
        plan = types.SimpleNamespace(source_path=str(source), work_dir=None)

        answer = agent_skill_lookup(str(source), os.getcwd())
        skills = by_name(answer["skills"])

        assert [Path(root["path"]) for root in answer["roots"]] == _skill_search_roots(plan)
        for name in ("shared", "only-agents", "only-user"):
            assert skills[name]["found"] is True
            assert Path(skills[name]["dir"]) == _resolve_skill_dir(name, plan)
            assert Path(skills[name]["skillMd"]) == _read_skill_md(name, plan)[1]
        assert (skills["shared"]["folder"], skills["shared"]["scope"]) == (
            ".claude/skills",
            "project",
        )
        assert (skills["only-agents"]["folder"], skills["only-agents"]["scope"]) == (
            ".agents/skills",
            "project",
        )
        assert (skills["only-user"]["folder"], skills["only-user"]["scope"]) == (
            "~/.agents/skills",
            "user",
        )
        assert skills["no-skill-md"]["found"] is False
        assert _resolve_skill_dir("no-skill-md", plan) is None

    def test_with_wf_skills_root_the_answer_follows_the_runtime_too(self, tmp_path, monkeypatch):
        """`WF_SKILLS_ROOT` moves wfpy's own walk to its first folder, as it
        always has, so the workflow's `.claude/skills` is not searched; the
        other tools' walk stays at the workflow."""
        project = tmp_path / "project"
        source = workflow(project)
        explicit = tmp_path / "explicit"
        skill(explicit, "in-explicit")
        skill(project / "flows" / ".claude" / "skills", "own-beside-workflow")
        skill(project / ".agents" / "skills", "tool-in-project")
        monkeypatch.setenv("WF_SKILLS_ROOT", str(explicit))
        plan = types.SimpleNamespace(source_path=str(source), work_dir=None)

        answer = agent_skill_lookup(str(source), os.getcwd())
        skills = by_name(answer["skills"])

        assert [Path(root["path"]) for root in answer["roots"]] == _skill_search_roots(plan)
        assert (skills["in-explicit"]["folder"], skills["in-explicit"]["scope"]) == (
            "WF_SKILLS_ROOT",
            "explicit",
        )
        assert "own-beside-workflow" not in skills
        assert _resolve_skill_dir("own-beside-workflow", plan) is None
        assert Path(skills["tool-in-project"]["dir"]) == _resolve_skill_dir("tool-in-project", plan)

    def test_the_working_directory_is_the_runs_not_the_sidecars(self, tmp_path):
        source = workflow(tmp_path / "project")
        started_in = tmp_path / "started-in"
        skill(started_in / ".wf" / "skills", "in-run-cwd")

        assert "in-run-cwd" in by_name(agent_skill_lookup(str(source), str(started_in))["skills"])
        assert "in-run-cwd" not in by_name(agent_skill_lookup(str(source))["skills"])

    @pytest.mark.skipif(os.name != "posix", reason="no owner check on Windows")
    def test_an_untrusted_folder_is_named_with_why(self, tmp_path):
        source = workflow(tmp_path / "project")
        loose = skill(tmp_path / "project" / ".agents" / "skills", "loose")
        loose.parent.chmod(0o777)

        answer = agent_skill_lookup(str(source), os.getcwd())

        assert "loose" not in by_name(answer["skills"])
        assert answer["ignored"] == [
            {
                "path": str(loose.parent.resolve()),
                "folder": ".agents/skills",
                "reason": "writable by every user",
            }
        ]


class TestWhatTheRunReads:
    def test_a_skill_md_the_run_cannot_read_is_its_problem(self, tmp_path):
        source = workflow(tmp_path / "project")
        skills_root = tmp_path / "project" / ".claude" / "skills"
        (skills_root / "md-dir" / "SKILL.md").mkdir(parents=True)
        skill(skills_root, "too-large", "x" * 256_001)
        bad = skills_root / "not-utf8"
        bad.mkdir()
        (bad / "SKILL.md").write_bytes(b"\xff\xfe not utf-8")
        plan = types.SimpleNamespace(source_path=str(source), work_dir=None)

        skills = by_name(agent_skill_lookup(str(source), os.getcwd())["skills"])

        for name in ("md-dir", "too-large", "not-utf8"):
            with pytest.raises((OSError, ValueError)) as raised:
                _read_skill_md(name, plan)
            assert skills[name]["found"] is True
            assert skills[name]["problem"] == str(raised.value)

    def test_hooks_are_read_as_the_runtime_reads_them(self, tmp_path):
        source = workflow(tmp_path / "project")
        root = tmp_path / "project" / ".claude" / "skills"
        directory = skill(
            root,
            "hooked",
            "---\ndescription: checks things\nhooks:\n  pre: prepare\n  post: report\n---\nbody\n",
        )
        (directory / "scripts").mkdir()
        (directory / "scripts" / "prepare.sh").write_text("true\n", encoding="utf-8")
        (directory / "scripts" / "other.py").write_text("", encoding="utf-8")
        skill(root, "upper-case-key", "---\nhooks:\n  Pre: prepare\n---\n")
        skill(root, "crlf", "---\r\ndescription: x\r\nhooks:\r\n  pre: prepare\r\n---\r\nbody\r\n")
        skill(root, "unclosed", "---\ndescription: x\nbody\n")

        skills = by_name(agent_skill_lookup(str(source), os.getcwd())["skills"])
        hooked = skills["hooked"]

        assert hooked["description"] == "checks things"
        assert hooked["hooks"]["pre"] == {
            "name": "prepare",
            "script": str((directory / "scripts" / "prepare.sh").resolve()),
            "interpreter": "sh",
            "problem": None,
        }
        assert hooked["hooks"]["post"]["script"] is None
        assert hooked["hooks"]["post"]["problem"] == "no scripts/report.py or .sh"
        assert hooked["scripts"] == ["other", "prepare"]
        # Keys as written. CRLF line ends read as LF: the file is read in
        # text mode, whose universal newlines the front-matter reader sees.
        assert skills["upper-case-key"]["hooks"] == {}
        assert skills["crlf"]["hooks"]["pre"]["name"] == "prepare"
        assert skills["crlf"]["description"] == "x"
        assert skills["crlf"]["warnings"] == []
        assert "front matter is not read" in skills["unclosed"]["warnings"][0]
        assert hooked["warnings"] == []

    def test_a_block_scalar_description_shows_the_body_instead(self, tmp_path):
        source = workflow(tmp_path / "project")
        skill(
            tmp_path / "project" / ".claude" / "skills",
            "folded",
            "---\ndescription: >-\n  folded text\n---\n# Title\n\nFirst line.\n",
        )

        skills = by_name(agent_skill_lookup(str(source), os.getcwd())["skills"])

        assert skills["folded"]["description"] == "First line."


class TestPresets:
    def test_presets_are_what_a_run_resolves(self, tmp_path):
        project = tmp_path / "project"
        source = workflow(project)
        agents = project / "flows" / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.md").write_text(
            "---\ndescription: reviews\nmodel: opus\nskills: present, absent\n---\nbody\n",
            encoding="utf-8",
        )
        (agents / "nested").mkdir()
        (agents / "nested" / "AGENT.md").write_text("Nested body.\n", encoding="utf-8")
        skill(project / ".agents" / "skills", "present")
        home_agents = tmp_path / "home" / ".claude" / "agents"
        home_agents.mkdir(parents=True)
        (home_agents / "reviewer.md").write_text("shadowed\n", encoding="utf-8")
        plan = types.SimpleNamespace(source_path=str(source), work_dir=None)

        answer = agent_skill_lookup(str(source), os.getcwd())
        presets = by_name(answer["presets"])

        assert [Path(root["path"]) for root in answer["presetRoots"]] == (
            _claude_agent_search_roots(plan)
        )
        for name in ("reviewer", "nested"):
            assert Path(presets[name]["path"]) == _resolve_claude_agent_path(name, plan)
        reviewer = presets["reviewer"]
        assert reviewer["description"] == "reviews"
        assert reviewer["ignoredFields"] == ["model"]
        assert [(entry["name"], entry["problem"]) for entry in reviewer["skills"]] == [
            ("present", None),
            ("absent", "Skill 'absent' was not found."),
        ]
        assert presets["nested"]["description"] == "Nested body."


class TestTheSidecarOp:
    def test_it_answers_for_a_file_that_does_not_parse(self, tmp_path):
        source = workflow(tmp_path / "project")
        skill(tmp_path / "project" / ".agents" / "skills", "from-op")

        response = _handle_request(
            {"op": "wfpy.agentSkills", "file": str(source), "args": {"cwd": os.getcwd()}}
        )

        assert response.status == "ok"
        assert "from-op" in by_name(response.diagnostic["skills"])

    def test_a_relative_cwd_is_no_cwd(self, tmp_path):
        source = workflow(tmp_path / "project")

        response = _handle_request(
            {"op": "agentSkills", "file": str(source), "args": {"cwd": "elsewhere"}}
        )

        assert response.diagnostic["cwd"] == str(Path.cwd().resolve())

    def test_it_is_advertised(self):
        assert "agentSkills" in SUPPORTED_OPS
        assert FEATURES["agentSkillsQuery"] is True
        capabilities = _handle_request({"op": "wfpy.getCapabilities"}).diagnostic
        assert capabilities["runtimeVersion"] == runtime_version()

    def test_the_cli_prints_the_same_version_line(self, capsys, monkeypatch):
        from wfpy.cli import main

        monkeypatch.setattr(sys, "argv", ["wfpy", "--version"])
        with pytest.raises(SystemExit) as exited:
            main()

        assert exited.value.code == 0
        assert capsys.readouterr().out == runtime_version() + "\n"
