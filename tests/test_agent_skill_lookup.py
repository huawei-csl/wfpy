"""`wfpy skills`: what a run of a workflow finds, for an IDE to offer.

The answer is the runtime's own lookup, so each test holds it against what
the runtime resolves (`_skill_search_roots`, `_resolve_skill_dir`,
`_read_skill_md`, `_resolve_claude_agent_path`) rather than restating the
rules.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

import wfpy._agent_prompt_runtime as runtime
import wfpy._agent_skill_lookup as lookup
from wfpy._agent_prompt_runtime import (
    _claude_agent_search_roots,
    _load_claude_agent_profile,
    _read_skill_md,
    _resolve_claude_agent_path,
    _resolve_skill_dir,
    _skill_search_roots,
)
from wfpy._agent_skill_lookup import agent_skill_lookup
from wfpy.cli import main


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A home and a working directory of the test's own, and no variable of
    the machine's."""

    for name in ("WF_SKILLS_ROOT", "WF_CLAUDE_AGENTS_ROOT", "CODEX_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    # git finds no repository above the test's directory, and reads no
    # configuration of the machine's.
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


def git_init(directory: Path) -> None:
    """Make `directory` a git repository, or skip without git."""

    if shutil.which("git") is None:
        pytest.skip("no git on PATH")
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(directory)], check=True)


def workflow(project: Path) -> Path:
    git_init(project)
    (project / "flows").mkdir(exist_ok=True)
    source = project / "flows" / "flow.py"
    source.write_text("this is not python yet (\n", encoding="utf-8")
    return source


def by_name(entries: list[dict]) -> dict[str, dict]:
    return {entry["name"]: entry for entry in entries}


def link_or_skip(link: Path, target: Path) -> None:
    """Make `link` a symlink to `target`, or skip where links cannot be made."""

    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


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

    def test_the_working_directory_is_the_one_given_else_this_processs(self, tmp_path):
        source = workflow(tmp_path / "project")
        started_in = tmp_path / "started-in"
        skill(started_in / ".wf" / "skills", "in-run-cwd")

        assert "in-run-cwd" in by_name(agent_skill_lookup(str(source), str(started_in))["skills"])
        assert "in-run-cwd" not in by_name(agent_skill_lookup(str(source))["skills"])

    def test_the_repository_is_the_one_git_finds(self, tmp_path):
        source = workflow(tmp_path / "project")
        loose = tmp_path / "loose"
        loose.mkdir()
        (loose / "flow.py").write_text("", encoding="utf-8")

        assert agent_skill_lookup(str(source))["repository"] == str(
            (tmp_path / "project").resolve()
        )
        assert agent_skill_lookup(str(loose / "flow.py"))["repository"] is None

    def test_relative_paths_are_read_from_the_working_directory(self, tmp_path, monkeypatch):
        """A relative file, `WF_SKILLS_ROOT` and `WF_CLAUDE_AGENTS_ROOT` are
        read from the directory the run starts in, as a run started there
        reads them, not from this process's."""
        project = tmp_path / "project"
        source = workflow(project)
        skill(project / "own-skills", "named-relatively")
        (project / "own-agents").mkdir()
        (project / "own-agents" / "relative.md").write_text("Relative.\n", encoding="utf-8")
        monkeypatch.setenv("WF_SKILLS_ROOT", "own-skills")
        monkeypatch.setenv("WF_CLAUDE_AGENTS_ROOT", "own-agents")

        answer = agent_skill_lookup(os.path.join("flows", "flow.py"), str(project))

        assert answer["workflow"] == str(source.resolve())
        assert answer["runtimeRoot"] == str((project / "own-skills").resolve())
        assert answer["repository"] == str(project.resolve())
        assert answer["roots"][0]["path"] == str((project / "own-skills").resolve())
        assert answer["presetRoots"][0]["path"] == str((project / "own-agents").resolve())
        assert "named-relatively" in by_name(answer["skills"])
        assert "relative" in by_name(answer["presets"])
        monkeypatch.chdir(project)
        run = types.SimpleNamespace(source_path=os.path.join("flows", "flow.py"), work_dir=None)
        assert [Path(root["path"]) for root in answer["roots"]] == _skill_search_roots(run)

    @pytest.mark.skipif(os.pathsep != ":", reason="a POSIX path may hold `:`")
    def test_a_relative_directory_holding_the_separator_is_read_from_the_working_directory(
        self, tmp_path, monkeypatch
    ):
        """A value that is itself a directory is that one directory (#61),
        and a relative one is that directory under where the run starts,
        not under this process's directory."""
        project = tmp_path / "project"
        workflow(project)
        skill(project / "skills:v2", "versioned")
        monkeypatch.setenv("WF_SKILLS_ROOT", "skills:v2")

        answer = agent_skill_lookup(os.path.join("flows", "flow.py"), str(project))

        assert answer["roots"][0]["path"] == str((project / "skills:v2").resolve())
        assert answer["runtimeRoot"] == str((project / "skills:v2").resolve())
        assert "versioned" in by_name(answer["skills"])

    def test_the_runtime_root_is_the_first_listed_directory_there_from_the_working_directory(
        self, tmp_path, monkeypatch
    ):
        """The runtime root is the first `WF_SKILLS_ROOT` directory that
        exists (#61), each relative one looked for where the run starts."""
        project = tmp_path / "project"
        workflow(project)
        skill(project / "present", "listed-second")
        monkeypatch.setenv("WF_SKILLS_ROOT", os.pathsep.join(["absent", "present"]))

        answer = agent_skill_lookup(os.path.join("flows", "flow.py"), str(project))

        assert answer["runtimeRoot"] == str((project / "present").resolve())
        assert "listed-second" in by_name(answer["skills"])

    def test_each_call_looks_again(self, tmp_path):
        """A run keeps its lookup for its plan; `wfpy skills` answers what is
        there now. A repository made and a folder added since the last call
        are in the next answer."""
        project = tmp_path / "project"
        (project / "flows").mkdir(parents=True)
        source = project / "flows" / "flow.py"
        source.write_text("", encoding="utf-8")
        skill(project / ".agents" / "skills", "above-the-workflow")

        first = agent_skill_lookup(str(source))
        git_init(project)
        skill(project / "flows" / ".agents" / "skills", "beside-the-workflow")
        second = agent_skill_lookup(str(source))

        assert first["repository"] is None
        assert "above-the-workflow" not in by_name(first["skills"])
        assert second["repository"] == str(project.resolve())
        assert {"above-the-workflow", "beside-the-workflow"} <= set(by_name(second["skills"]))

    def test_git_is_asked_once(self, tmp_path, monkeypatch):
        """The repository in the answer is the one the roots were built with."""
        calls: list[Path] = []
        real = runtime._repository_root

        def counted(start: Path) -> Path | None:
            calls.append(start)
            return real(start)

        monkeypatch.setattr(runtime, "_repository_root", counted)
        monkeypatch.setattr(lookup, "_repository_root", counted, raising=False)
        source = workflow(tmp_path / "project")

        answer = agent_skill_lookup(str(source))

        assert len(calls) == 1
        assert answer["repository"] == str((tmp_path / "project").resolve())


class TestTheProjectBoundsTheWalk:
    def test_a_folder_above_the_project_is_neither_searched_nor_listed(self, tmp_path):
        """`/tmp/.claude/skills`, `/tmp/.wf/skills` and `/tmp/.claude/agents`,
        planted by someone else, are not read for a workflow in `/tmp/job`
        started elsewhere: the answer gives the folders a run searches, and
        they stop at the project's top, here the workflow's directory."""
        shared_tmp = tmp_path / "tmp"
        skill(shared_tmp / ".claude" / "skills", "planted")
        skill(shared_tmp / ".wf" / "skills", "planted-wf")
        (shared_tmp / ".claude" / "agents").mkdir(parents=True)
        (shared_tmp / ".claude" / "agents" / "planted.md").write_text("Planted.\n")
        (shared_tmp / "job").mkdir()
        source = shared_tmp / "job" / "flow.py"
        source.write_text("", encoding="utf-8")
        plan = types.SimpleNamespace(source_path=str(source), work_dir=None)

        answer = agent_skill_lookup(str(source))
        roots = [Path(root["path"]) for root in answer["roots"]]

        assert roots == _skill_search_roots(plan)
        assert [Path(root["path"]) for root in answer["presetRoots"]] == (
            _claude_agent_search_roots(plan)
        )
        assert answer["repository"] is None
        assert (shared_tmp / ".claude" / "skills").resolve() not in roots
        assert not {"planted", "planted-wf"} & set(by_name(answer["skills"]))
        assert answer["presets"] == []

    def test_the_answer_keeps_its_fields(self, tmp_path):
        """wfpy-ide checks every field it reads: the bound changes which
        folders are listed, not how an answer is made."""
        project = tmp_path / "project"
        source = workflow(project)
        skill(project / ".claude" / "skills", "listed")
        (project / ".claude" / "agents").mkdir(parents=True)
        (project / ".claude" / "agents" / "listed.md").write_text("Listed.\n")

        answer = agent_skill_lookup(str(source))

        assert set(answer) == {
            "workflow",
            "cwd",
            "runtimeRoot",
            "repository",
            "roots",
            "skills",
            "presetRoots",
            "presets",
        }
        for root in answer["roots"] + answer["presetRoots"]:
            assert set(root) == {"path", "folder", "scope"}
        [listed] = answer["skills"]
        assert set(listed) == {
            "name",
            "found",
            "dir",
            "root",
            "folder",
            "scope",
            "skillMd",
            "problem",
            "description",
            "hooks",
            "scripts",
            "warnings",
        }
        [preset] = answer["presets"]
        assert set(preset) == {
            "name",
            "path",
            "root",
            "folder",
            "scope",
            "problem",
            "description",
            "skills",
            "ignoredFields",
            "warnings",
        }


class TestHiddenEntries:
    def test_a_hidden_entry_is_no_skill_and_no_preset(self, tmp_path, monkeypatch):
        """A skills folder kept in git has a `.git`, Codex keeps the skills it
        ships in `$CODEX_HOME/skills/.system`: a hidden entry is not offered,
        as Codex does not take one either. `.system` is a folder of its own,
        searched after `$CODEX_HOME/skills`."""
        project = tmp_path / "project"
        source = workflow(project)
        codex_home = tmp_path / "codex-home"
        skill(codex_home / "skills", "codex-users")
        skill(codex_home / "skills" / ".system", "codex-bundled")
        (codex_home / "skills" / ".system" / ".codex-system-skills.marker").write_text("v1")
        root = project / ".agents" / "skills"
        skill(root, "shown")
        skill(root, ".hidden-with-skill-md")
        (root / ".git").mkdir()
        agents = project / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "shown.md").write_text("Shown.\n", encoding="utf-8")
        (agents / ".hidden.md").write_text("Hidden.\n", encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        plan = types.SimpleNamespace(source_path=str(source), work_dir=None)

        answer = agent_skill_lookup(str(source))
        skills = by_name(answer["skills"])

        assert set(skills) == {"shown", "codex-users", "codex-bundled"}
        assert skills["codex-bundled"]["folder"] == "$CODEX_HOME/skills/.system"
        assert Path(skills["codex-bundled"]["dir"]) == _resolve_skill_dir("codex-bundled", plan)
        assert [entry["name"] for entry in answer["presets"]] == ["shown"]


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

    def test_a_preset_description_that_is_no_text_shows_the_body_instead(self, tmp_path):
        """The preset reader makes indented lines under `description:` a list
        or a mapping; that is no text to show."""
        source = workflow(tmp_path / "project")
        agents = tmp_path / "project" / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "listed.md").write_text(
            "---\ndescription:\n  - Reviews code\n---\nFirst line.\n", encoding="utf-8"
        )
        (agents / "mapped.md").write_text(
            "---\ndescription:\n  when: reviewing\n---\nSecond line.\n", encoding="utf-8"
        )

        presets = by_name(agent_skill_lookup(str(source), os.getcwd())["presets"])

        assert presets["listed"]["description"] == "First line."
        assert presets["mapped"]["description"] == "Second line."


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

    def test_a_description_with_commas_is_one_text(self, tmp_path):
        """The front-matter reader makes a comma value a list (`tools: Read,
        Grep`), but a description is text."""
        project = tmp_path / "project"
        source = workflow(project)
        agents = project / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "careful.md").write_text(
            "---\nname: careful, thorough\ndescription: Reviews code, finds bugs, says why\n"
            "tools: Read, Grep\n---\nbody\n",
            encoding="utf-8",
        )
        plan = types.SimpleNamespace(source_path=str(source), work_dir=None)

        [careful] = agent_skill_lookup(str(source))["presets"]

        assert careful["description"] == "Reviews code, finds bugs, says why"
        profile = _load_claude_agent_profile("careful", plan)
        assert profile["description"] == "Reviews code, finds bugs, says why"
        assert profile["meta"]["name"] == "careful, thorough"
        assert profile["meta"]["tools"] == ["Read", "Grep"]

    def test_a_large_preset_loads_as_it_always_has(self, tmp_path):
        """A run reads a preset whole, whatever its size. The list reads one
        within `PRESET_LIST_MAX_BYTES` for its description; a larger one is
        listed, found, with a warning instead."""
        project = tmp_path / "project"
        source = workflow(project)
        agents = project / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "huge.md").write_text(
            "---\ndescription: large\n---\n" + "x" * lookup.PRESET_LIST_MAX_BYTES,
            encoding="utf-8",
        )
        plan = types.SimpleNamespace(source_path=str(source), work_dir=None)

        [huge] = agent_skill_lookup(str(source))["presets"]

        assert _load_claude_agent_profile("huge", plan)["description"] == "large"
        assert huge["path"] == str((agents / "huge.md").resolve())
        assert huge["problem"] is None
        assert huge["description"] is None
        assert "a run reads it whole" in huge["warnings"][0]

    def test_a_symlinked_preset_is_found(self, tmp_path):
        """As a symlinked skill is: a preset installed as a link, a file or a
        folder holding `AGENT.md`, resolves where the link points."""
        project = tmp_path / "project"
        source = workflow(project)
        shared = tmp_path / "shared-agents"
        (shared / "nested").mkdir(parents=True)
        (shared / "linked.md").write_text("Linked.\n", encoding="utf-8")
        (shared / "nested" / "AGENT.md").write_text("Nested and linked.\n", encoding="utf-8")
        agents = project / ".claude" / "agents"
        agents.mkdir(parents=True)
        link_or_skip(agents / "linked.md", shared / "linked.md")
        link_or_skip(agents / "nested-linked", shared / "nested")
        plan = types.SimpleNamespace(source_path=str(source), work_dir=None)

        presets = by_name(agent_skill_lookup(str(source))["presets"])

        assert _resolve_claude_agent_path("linked", plan) == (shared / "linked.md").resolve()
        assert (
            _resolve_claude_agent_path("nested-linked", plan)
            == (shared / "nested" / "AGENT.md").resolve()
        )
        assert _load_claude_agent_profile("linked", plan)["body"] == "Linked."
        assert presets["linked"]["description"] == "Linked."
        assert presets["nested-linked"]["description"] == "Nested and linked."


class TestTheCommand:
    """`wfpy skills <file> [--json]`, run as an IDE runs it: from the
    directory a run starts in, with the run's environment."""

    def run(self, monkeypatch, capsys, *argv: str) -> tuple[int, str]:
        monkeypatch.setattr(sys, "argv", ["wfpy", "skills", *argv])
        code = 0
        try:
            main()
        except SystemExit as exited:
            code = int(exited.code or 0)
        return code, capsys.readouterr().out

    def test_json_is_the_lookup_from_the_working_directory(self, tmp_path, monkeypatch, capsys):
        """For a file that does not parse: it is not loaded."""
        project = tmp_path / "project"
        source = workflow(project)
        skill(project / ".agents" / "skills", "from-the-command")
        monkeypatch.chdir(project)

        code, out = self.run(monkeypatch, capsys, "--json", str(source.relative_to(project)))

        assert code == 0
        assert json.loads(out) == agent_skill_lookup(str(source), str(project))
        assert "from-the-command" in by_name(json.loads(out)["skills"])

    def test_a_table_without_json(self, tmp_path, monkeypatch, capsys):
        project = tmp_path / "project"
        source = workflow(project)
        skill(project / ".agents" / "skills", "in-the-table")
        monkeypatch.chdir(project)

        code, out = self.run(monkeypatch, capsys, str(source))

        assert code == 0
        assert "in-the-table" in out
        assert f"repository: {project.resolve()}" in out

    def test_a_table_outside_a_repository_says_where_the_project_ends(
        self, tmp_path, monkeypatch, capsys
    ):
        project = tmp_path / "project"
        (project / "flows").mkdir(parents=True)
        (project / "flows" / "flow.py").write_text("", encoding="utf-8")
        monkeypatch.chdir(project)

        code, out = self.run(monkeypatch, capsys, str(project / "flows" / "flow.py"))

        assert code == 0
        assert f"repository: none (the project's top: {project.resolve()})" in out

    def test_a_file_that_is_not_there_is_an_error(self, tmp_path, monkeypatch, capsys):
        code, out = self.run(monkeypatch, capsys, "--json", str(tmp_path / "nowhere.py"))

        assert code == 1
        assert out == ""
