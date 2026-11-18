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
import shutil
import subprocess
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import wfpy._agent_prompt_runtime as runtime
from wfpy._agent_prompt_runtime import (
    _claude_agent_search_roots,
    _read_skill_md,
    _read_skill_md_file,
    _resolve_skill_dir,
    _runtime_root_for_skills,
    _skill_lookup_roots,
    _skill_search_roots,
)


@pytest.fixture(autouse=True)
def no_tool_homes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Other agent tools' user folders follow `CODEX_HOME` and
    `XDG_CONFIG_HOME`, and their project folders stop where git says the
    repository ends; the machine's own values, repositories and git
    configuration must not reach these tests."""

    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git_init(directory: Path, separate: Path | None = None) -> None:
    """Make `directory` a git repository, its `.git` a file pointing at
    `separate` when given (as in a linked worktree or a submodule); skip
    without git."""

    if shutil.which("git") is None:
        pytest.skip("no git on PATH")
    directory.mkdir(parents=True, exist_ok=True)
    extra = ["--separate-git-dir", str(separate)] if separate is not None else []
    subprocess.run(["git", "init", "-q", *extra, str(directory)], check=True)


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


def link_or_skip(link: Path, target: Path) -> None:
    """Make `link` a symlink to `target`, or skip where the platform refuses
    one (Windows without the symlink privilege)."""

    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"cannot create a symlink here: {error}")


TOOLS = [
    (".agents", "skills"),
    (".codex", "skills"),
    (".opencode", "skills"),
    (".opencode", "skill"),
]


class TestOtherToolsSkillFolders:
    """Skills installed for other agent tools are found, after wfpy's own.

    `.agents/skills` is the folder the Agent Skills convention shares between
    tools and the one Codex documents for a repository; `.codex/skills` is a
    Codex project configuration's; OpenCode reads `.opencode/skills` and the
    older `.opencode/skill`. At user level: `~/.agents/skills`,
    `$CODEX_HOME/skills`, `$XDG_CONFIG_HOME/opencode/skills` (and `skill`).

    A project's folders are searched from the workflow's directory up to the
    top of the git repository, as Codex and OpenCode do; with no repository,
    in the workflow's directory alone.
    """

    @pytest.fixture(autouse=True)
    def isolated_home(self, tmp_path, monkeypatch):
        """A home and a working directory of the test's own, so the real ones'
        skills cannot answer for the fixture's."""
        home = tmp_path / "home"
        home.mkdir()
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(cwd)
        return home

    @staticmethod
    def workflow(project: Path, repository: bool = True) -> types.SimpleNamespace:
        """A workflow in `<project>/flows`, the project a repository unless
        told otherwise."""
        nested = project / "flows"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "wf.py").write_text("", encoding="utf-8")
        if repository:
            git_init(project)
        return make_plan(source_path=str(nested / "wf.py"))

    @pytest.mark.parametrize("folder", TOOLS)
    def test_a_project_skill_of_another_tool_is_found(self, folder, tmp_path):
        project = tmp_path / "project"
        skill_dir = make_skill(project.joinpath(*folder), "wfroot-tool")
        plan = self.workflow(project)

        assert _resolve_skill_dir("wfroot-tool", plan) == skill_dir

    def test_a_user_skill_of_another_tool_is_found(self, tmp_path, isolated_home, monkeypatch):
        codex_home = tmp_path / "codex-home"
        config_home = tmp_path / "config"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        agents = make_skill(isolated_home / ".agents" / "skills", "wfroot-user-agents")
        codex = make_skill(codex_home / "skills", "wfroot-user-codex")
        opencode = make_skill(config_home / "opencode" / "skills", "wfroot-user-opencode")
        legacy = make_skill(config_home / "opencode" / "skill", "wfroot-user-opencode-legacy")
        plan = self.workflow(tmp_path / "project")

        assert _resolve_skill_dir("wfroot-user-agents", plan) == agents
        assert _resolve_skill_dir("wfroot-user-codex", plan) == codex
        assert _resolve_skill_dir("wfroot-user-opencode", plan) == opencode
        assert _resolve_skill_dir("wfroot-user-opencode-legacy", plan) == legacy

    def test_codex_home_and_xdg_config_home_default_under_home(self, tmp_path, isolated_home):
        codex = make_skill(isolated_home / ".codex" / "skills", "wfroot-default-codex")
        opencode = make_skill(
            isolated_home / ".config" / "opencode" / "skills", "wfroot-default-opencode"
        )
        plan = self.workflow(tmp_path / "project")

        assert _resolve_skill_dir("wfroot-default-codex", plan) == codex
        assert _resolve_skill_dir("wfroot-default-opencode", plan) == opencode

    def test_a_relative_xdg_config_home_counts_as_unset(self, tmp_path, isolated_home, monkeypatch):
        """As the XDG Base Directory specification asks."""
        make_skill(Path.cwd() / "tool-home" / "opencode" / "skills", "wfroot-relative-home")
        default = make_skill(
            isolated_home / ".config" / "opencode" / "skills", "wfroot-default-home"
        )
        monkeypatch.setenv("XDG_CONFIG_HOME", "tool-home")
        plan = self.workflow(tmp_path / "project")

        assert _resolve_skill_dir("wfroot-relative-home", plan) is None
        assert _resolve_skill_dir("wfroot-default-home", plan) == default

    def test_a_relative_codex_home_is_read_from_the_working_directory(
        self, tmp_path, isolated_home, monkeypatch
    ):
        """As Codex reads it."""
        relative = make_skill(Path.cwd() / "tool-home" / "skills", "wfroot-relative-codex")
        make_skill(isolated_home / ".codex" / "skills", "wfroot-default-codex-unread")
        monkeypatch.setenv("CODEX_HOME", "tool-home")
        plan = self.workflow(tmp_path / "project")
        [root] = [root for root in _skill_lookup_roots(plan) if root.folder == "$CODEX_HOME/skills"]

        assert _resolve_skill_dir("wfroot-relative-codex", plan) == relative
        assert _resolve_skill_dir("wfroot-default-codex-unread", plan) is None
        assert (root.path, root.scope) == (relative.parent.resolve(), "user")

    def test_codex_bundled_skills_are_searched_after_codex_home(
        self, tmp_path, isolated_home, monkeypatch
    ):
        """Codex installs the skills it ships in `$CODEX_HOME/skills/.system`
        and reads that folder as a root of its own, ranked after the user's
        skills; `.system` is no skill in `$CODEX_HOME/skills`."""
        codex_home = tmp_path / "codex-home"
        bundled = make_skill(codex_home / "skills" / ".system", "wfroot-bundled")
        users = make_skill(codex_home / "skills", "wfroot-bundled-shadowed")
        make_skill(codex_home / "skills" / ".system", "wfroot-bundled-shadowed")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        plan = self.workflow(tmp_path / "project")
        roots = _skill_lookup_roots(plan)
        folders = [root.folder for root in roots]
        system = roots[folders.index("$CODEX_HOME/skills/.system")]

        assert _resolve_skill_dir("wfroot-bundled", plan) == bundled
        assert _resolve_skill_dir("wfroot-bundled-shadowed", plan) == users
        assert (
            folders.index("$CODEX_HOME/skills/.system") == folders.index("$CODEX_HOME/skills") + 1
        )
        assert (system.path, system.scope) == (bundled.parent.resolve(), "user")

    def test_a_codex_home_that_is_the_projects_codex_folder_is_the_users(
        self, tmp_path, isolated_home, monkeypatch
    ):
        """Codex does not read a project's `.codex` that is `CODEX_HOME` as the
        project's, only as `CODEX_HOME`, and ranks a user's skill after a
        repository's. `CODEX_HOME=.codex`, started in the project, names that
        folder: its skills come after the project's other folders."""
        project = tmp_path / "project"
        plan = self.workflow(project)
        in_codex_home = make_skill(project / ".codex" / "skills", "wfroot-project-codex-home")
        preferred = make_skill(project / ".opencode" / "skills", "wfroot-project-codex-home")
        monkeypatch.chdir(project)
        monkeypatch.setenv("CODEX_HOME", ".codex")
        roots = _skill_lookup_roots(plan)
        [root] = [root for root in roots if root.path == in_codex_home.parent.resolve()]

        assert (root.folder, root.scope) == ("$CODEX_HOME/skills", "user")
        assert _resolve_skill_dir("wfroot-project-codex-home", plan) == preferred

    def test_the_roots_searched_before_keep_their_order_and_come_first(
        self, tmp_path, isolated_home
    ):
        """What was searched before the other tools' folders is searched in
        the same order, ahead of all of them. `<cwd>` is outside the
        repository, so its folders of other tools are not searched."""
        project = tmp_path / "project"
        plan = self.workflow(project)
        runtime_root = (project / "flows").resolve()
        cwd = Path.cwd().resolve()
        home = isolated_home.resolve()

        before: list[Path] = []
        for level in [runtime_root, *runtime_root.parents]:
            for candidate in (level / ".wf" / "skills", level / ".claude" / "skills"):
                if candidate not in before:
                    before.append(candidate)
        for candidate in (
            cwd / ".wf" / "skills",
            cwd / ".claude" / "skills",
            home / ".claude" / "skills",
        ):
            if candidate not in before:
                before.append(candidate)

        after = [
            level.joinpath(*parts) for level in (runtime_root, project.resolve()) for parts in TOOLS
        ] + [
            home / ".agents" / "skills",
            home / ".codex" / "skills",
            home / ".codex" / "skills" / ".system",
            home / ".config" / "opencode" / "skills",
            home / ".config" / "opencode" / "skill",
        ]

        assert _skill_search_roots(plan) == before + after

    def test_a_folder_made_while_a_run_goes_on_is_searched(self, tmp_path):
        """The roots are worked out once per plan, every folder listed made
        or not, so a skill installed after the first lookup is found."""
        project = tmp_path / "project"
        plan = self.workflow(project)

        assert _resolve_skill_dir("wfroot-late", plan) is None
        late = make_skill(project / ".agents" / "skills", "wfroot-late")
        assert _resolve_skill_dir("wfroot-late", plan) == late

    def test_a_name_in_claude_skills_and_codex_skills_keeps_resolving_to_claude(self, tmp_path):
        """The regression guard: a name that resolved before the other tools'
        folders were searched resolves to the same directory now."""
        project = tmp_path / "project"
        preferred = make_skill(project / ".claude" / "skills", "wfroot-both-tools", body="claude")
        make_skill(project / ".codex" / "skills", "wfroot-both-tools", body="codex")
        plan = self.workflow(project)

        assert _resolve_skill_dir("wfroot-both-tools", plan) == preferred

    def test_a_user_claude_skill_outranks_a_project_skill_of_another_tool(
        self, tmp_path, isolated_home
    ):
        """Every root searched before comes first, `~/.claude/skills` too: a
        name it holds resolves there as it did, though a project folder of
        another tool holds it as well."""
        project = tmp_path / "project"
        preferred = make_skill(isolated_home / ".claude" / "skills", "wfroot-home-first")
        make_skill(project / "flows" / ".agents" / "skills", "wfroot-home-first")
        plan = self.workflow(project)

        assert _resolve_skill_dir("wfroot-home-first", plan) == preferred

    def test_nearer_levels_and_the_shared_folder_win_among_other_tools(self, tmp_path):
        project = tmp_path / "project"
        nearer = make_skill(project / "flows" / ".opencode" / "skills", "wfroot-level")
        make_skill(project / ".agents" / "skills", "wfroot-level")
        shared = make_skill(project / ".agents" / "skills", "wfroot-same-level")
        make_skill(project / ".codex" / "skills", "wfroot-same-level")
        plan = self.workflow(project)

        assert _resolve_skill_dir("wfroot-level", plan) == nearer
        assert _resolve_skill_dir("wfroot-same-level", plan) == shared

    def test_wf_skills_root_still_comes_first(self, tmp_path, monkeypatch):
        explicit = tmp_path / "explicit"
        preferred = make_skill(explicit, "wfroot-explicit-first")
        project = tmp_path / "project"
        make_skill(project / ".agents" / "skills", "wfroot-explicit-first")
        monkeypatch.setenv("WF_SKILLS_ROOT", str(explicit))

        assert _resolve_skill_dir("wfroot-explicit-first", self.workflow(project)) == preferred

    def test_a_symlinked_skill_of_another_tool_is_found(self, tmp_path):
        installed = make_skill(tmp_path / "checkout", "wfroot-linked-tool")
        project = tmp_path / "project"
        (project / ".agents" / "skills").mkdir(parents=True)
        link_or_skip(project / ".agents" / "skills" / "wfroot-linked-tool", installed)

        assert _resolve_skill_dir("wfroot-linked-tool", self.workflow(project)) == installed

    def test_with_wf_skills_root_the_walk_starts_at_the_workflow(self, tmp_path, monkeypatch):
        """`WF_SKILLS_ROOT` makes its first entry the runtime root, a folder of
        skills; the other tools' folders are still the workflow's."""
        explicit = tmp_path / "explicit"
        explicit.mkdir()
        beside_explicit = make_skill(explicit / ".agents" / "skills", "wfroot-beside-explicit")
        project = tmp_path / "project"
        in_project = make_skill(project / ".agents" / "skills", "wfroot-in-project")
        monkeypatch.setenv("WF_SKILLS_ROOT", str(explicit))
        plan = self.workflow(project)

        assert _runtime_root_for_skills(plan) == explicit.resolve()
        assert _resolve_skill_dir("wfroot-in-project", plan) == in_project
        assert _resolve_skill_dir("wfroot-beside-explicit", plan) is None
        assert beside_explicit.parent.resolve() not in _skill_search_roots(plan)


class TestOtherToolsFoldersStopAtTheRepository:
    """How far up a project's folders of other tools are searched.

    Before they were searched, a folder above the project was never read for
    them, so it must not be now: `/tmp/.agents/skills`, which any local user
    can write, would hand a workflow in `/tmp/job` its prompt text and hook
    scripts. Codex and OpenCode stop at the git repository, and so does
    this walk, at the top git gives (`git rev-parse --show-toplevel`). wfpy's
    own `.wf` and `.claude` folders keep walking to the filesystem root, as
    they always have.
    """

    @pytest.fixture(autouse=True)
    def isolated_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(cwd)
        return home

    @staticmethod
    def flow(directory: Path) -> types.SimpleNamespace:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "flow.py").write_text("", encoding="utf-8")
        return make_plan(source_path=str(directory / "flow.py"))

    @pytest.mark.parametrize("folder", TOOLS)
    def test_a_shared_folder_above_a_workflow_outside_a_repository_is_not_read(
        self, folder, tmp_path
    ):
        """The `/tmp` case: `/tmp/.agents/skills/review` and `/tmp/job/flow.py`."""
        shared_tmp = tmp_path / "tmp"
        make_skill(shared_tmp.joinpath(*folder), "wfroot-review")
        plan = self.flow(shared_tmp / "job")

        assert _resolve_skill_dir("wfroot-review", plan) is None
        assert shared_tmp.joinpath(*folder).resolve() not in _skill_search_roots(plan)

    def test_outside_a_repository_the_workflows_directory_is_searched(self, tmp_path):
        shared_tmp = tmp_path / "tmp"
        beside = make_skill(shared_tmp / "job" / ".agents" / "skills", "wfroot-beside")

        assert _resolve_skill_dir("wfroot-beside", self.flow(shared_tmp / "job")) == beside

    def test_wfpy_own_folders_above_still_walk_to_the_filesystem_root(self, tmp_path):
        shared_tmp = tmp_path / "tmp"
        above = make_skill(shared_tmp / ".claude" / "skills", "wfroot-own-above")

        assert _resolve_skill_dir("wfroot-own-above", self.flow(shared_tmp / "job")) == above

    @pytest.mark.parametrize("marker", ["directory", "file"])
    def test_the_walk_stops_at_the_repository_root_included(self, marker, tmp_path):
        """`.git` is a directory, or a file in a linked worktree or a submodule."""
        outer = tmp_path / "outer"
        repository = outer / "repository"
        git_init(repository, tmp_path / "elsewhere.git" if marker == "file" else None)
        assert (repository / ".git").is_dir() is (marker == "directory")
        make_skill(outer / ".agents" / "skills", "wfroot-above-repository")
        at_root = make_skill(repository / ".codex" / "skills", "wfroot-at-repository-root")
        plan = self.flow(repository / "flows" / "nested")

        assert _resolve_skill_dir("wfroot-at-repository-root", plan) == at_root
        assert _resolve_skill_dir("wfroot-above-repository", plan) is None

    @pytest.mark.parametrize(
        ("started_in", "read"), [("repository/tools", True), ("elsewhere", False)]
    )
    def test_the_working_directory_is_searched_only_inside_the_repository(
        self, started_in, read, tmp_path, monkeypatch
    ):
        """`<cwd>` is where the run was started from: its folders of other
        tools are the project's when it is in the workflow's repository, and
        none of the project's otherwise (`/tmp/.agents/skills` for a run
        started in `/tmp`)."""
        repository = tmp_path / "repository"
        git_init(repository)
        cwd = tmp_path / started_in
        in_cwd = make_skill(cwd / ".opencode" / "skills", "wfroot-in-cwd")
        monkeypatch.chdir(cwd)

        assert (
            _resolve_skill_dir("wfroot-in-cwd", self.flow(repository / "flows")) == in_cwd
        ) is read

    @pytest.mark.parametrize(("started_in", "read"), [("tmp/job", True), ("tmp", False)])
    def test_outside_a_repository_the_working_directory_is_searched_only_if_the_workflows(
        self, started_in, read, tmp_path, monkeypatch
    ):
        shared_tmp = tmp_path / "tmp"
        plan = self.flow(shared_tmp / "job")
        cwd = tmp_path / started_in
        in_cwd = make_skill(cwd / ".agents" / "skills", "wfroot-cwd-no-repository")
        monkeypatch.chdir(cwd)

        assert (_resolve_skill_dir("wfroot-cwd-no-repository", plan) == in_cwd) is read


class TestHomeKeptInGit:
    """A workflow under a home directory kept in git: the walk, or `<cwd>`,
    reaches `~`. Its user-level folders (`~/.agents/skills`, and
    `~/.codex/skills` when it is Codex's user folder) are searched once, in
    the user-level place, after every project folder. Its other folders
    (`~/.opencode/skills`) are a project's, as they are for OpenCode.
    """

    @pytest.fixture(autouse=True)
    def home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        git_init(home)  # a home kept in git, so the walk reaches it
        monkeypatch.setenv("HOME", str(home))
        return home

    @staticmethod
    def flow(home: Path) -> types.SimpleNamespace:
        flows = home / "work" / "flows"
        flows.mkdir(parents=True, exist_ok=True)
        (flows / "flow.py").write_text("", encoding="utf-8")
        return make_plan(source_path=str(flows / "flow.py"))

    def test_the_user_folders_come_after_the_working_directory(self, home, tmp_path, monkeypatch):
        cwd = home / "started-in"
        codex_home = tmp_path / "codex-home"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        flows = home / "work" / "flows"
        expected_tools = [
            *(flows.joinpath(*parts) for parts in TOOLS),
            *((home / "work").joinpath(*parts) for parts in TOOLS),
            # `~` as a project: `~/.agents/skills` is the user's, below;
            # `~/.codex/skills` is not Codex's user folder, `CODEX_HOME` is.
            *(home.joinpath(*parts) for parts in TOOLS if parts != (".agents", "skills")),
            *(cwd.joinpath(*parts) for parts in TOOLS),
            home / ".agents" / "skills",
            codex_home / "skills",
            codex_home / "skills" / ".system",
            home / ".config" / "opencode" / "skills",
            home / ".config" / "opencode" / "skill",
        ]
        for folder in expected_tools:
            folder.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(cwd)
        roots = _skill_search_roots(self.flow(home))

        # The walk reached `~` and took `~/.claude/skills` as it always did;
        # every root after `<cwd>`'s own is another tool's.
        tool_roots = roots[roots.index(cwd.resolve() / ".claude" / "skills") + 1 :]
        assert tool_roots == [folder.resolve() for folder in expected_tools]

    def test_an_opencode_folder_in_home_is_a_projects(self, home, tmp_path, monkeypatch):
        """OpenCode reads `.opencode/skills` up to the git worktree, `~`
        included when `~` is the worktree: before `<cwd>`'s folders."""
        in_home = make_skill(home / ".opencode" / "skills", "wfroot-home-opencode")
        make_skill(tmp_path / "cwd" / ".opencode" / "skills", "wfroot-home-opencode")
        monkeypatch.chdir(tmp_path / "cwd")
        plan = self.flow(home)
        roots = _skill_lookup_roots(plan)
        [root] = [root for root in roots if root.path == in_home.parent.resolve()]

        assert _resolve_skill_dir("wfroot-home-opencode", plan) == in_home
        assert (root.folder, root.scope) == (".opencode/skills", "project")

    def test_a_working_directory_skill_outranks_the_users(self, home, tmp_path, monkeypatch):
        cwd = home / "started-in"
        preferred = make_skill(cwd / ".agents" / "skills", "wfroot-cwd-first")
        make_skill(home / ".agents" / "skills", "wfroot-cwd-first")
        monkeypatch.chdir(cwd)
        plan = self.flow(home)
        roots = _skill_lookup_roots(plan)
        [user] = [root for root in roots if root.path == (home / ".agents" / "skills").resolve()]

        assert _resolve_skill_dir("wfroot-cwd-first", plan) == preferred
        assert (user.folder, user.scope) == ("~/.agents/skills", "user")

    def test_with_codex_home_elsewhere_the_home_codex_folder_is_a_projects(
        self, home, tmp_path, monkeypatch
    ):
        """With `CODEX_HOME` elsewhere, `~/.codex/skills` is not Codex's user
        folder: it is searched as the home project's, before `$CODEX_HOME`."""
        codex_home = tmp_path / "codex-home"
        make_skill(codex_home / "skills", "wfroot-codex-home")
        in_home = make_skill(home / ".codex" / "skills", "wfroot-codex-home")
        only_user = make_skill(codex_home / "skills", "wfroot-only-in-codex-home")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.chdir(tmp_path)
        plan = self.flow(home)

        assert _resolve_skill_dir("wfroot-codex-home", plan) == in_home
        assert _resolve_skill_dir("wfroot-only-in-codex-home", plan) == only_user

    def test_with_codex_home_unset_the_home_codex_folder_is_the_users(
        self, home, tmp_path, monkeypatch
    ):
        """`~/.codex/skills` is then Codex's user folder: searched last, so a
        working directory's `.codex/skills` comes first."""
        make_skill(home / ".codex" / "skills", "wfroot-codex-user")
        in_cwd = make_skill(home / "started-in" / ".codex" / "skills", "wfroot-codex-user")
        monkeypatch.chdir(home / "started-in")

        assert _resolve_skill_dir("wfroot-codex-user", self.flow(home)) == in_cwd

    def test_started_in_home_the_user_folders_keep_their_place(self, home, tmp_path, monkeypatch):
        """`<cwd>` is the home directory: its user-level folders are still
        the user's."""
        preferred = make_skill(home / "work" / ".agents" / "skills", "wfroot-project-first")
        make_skill(home / ".agents" / "skills", "wfroot-project-first")
        (home / "work" / ".opencode" / "skill").mkdir(parents=True)
        monkeypatch.chdir(home)
        plan = self.flow(home)
        roots = _skill_search_roots(plan)

        assert _resolve_skill_dir("wfroot-project-first", plan) == preferred
        assert roots.index((home / ".agents" / "skills").resolve()) > roots.index(
            (home / "work" / ".opencode" / "skill").resolve()
        )

    def test_a_codex_home_inside_the_project_is_searched_as_the_users(
        self, home, tmp_path, monkeypatch
    ):
        project = home / "work"
        codex_home = project / ".codex"
        (codex_home / "skills").mkdir(parents=True)
        (home / ".agents" / "skills").mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.chdir(tmp_path)
        roots = _skill_search_roots(self.flow(home))

        assert roots.index((codex_home / "skills").resolve()) > roots.index(
            (home / ".agents" / "skills").resolve()
        )


class TestSkillMdThatIsNoFile:
    def test_the_run_takes_the_folder_and_says_why_it_cannot_read_it(self, tmp_path, monkeypatch):
        """`SKILL.md` exists, so the folder is the skill's and shadows later
        ones; reading it then fails with the reason."""
        root = tmp_path / "skills"
        (root / "wfroot-md-dir" / "SKILL.md").mkdir(parents=True)
        make_skill(tmp_path / "later", "wfroot-md-dir")
        monkeypatch.setenv("WF_SKILLS_ROOT", os.pathsep.join([str(root), str(tmp_path / "later")]))
        plan = make_plan()

        assert _resolve_skill_dir("wfroot-md-dir", plan) == (root / "wfroot-md-dir").resolve()
        with pytest.raises(FileNotFoundError, match="is not a file"):
            _read_skill_md("wfroot-md-dir", plan)


class TestSkillMdChecks:
    def test_a_skill_md_gone_since_the_lookup_is_missing_not_no_file(self, tmp_path):
        """One `stat` tells a `SKILL.md` that is not there from one that is
        no file."""
        with pytest.raises(FileNotFoundError, match="does not exist"):
            _read_skill_md_file("wfroot-gone", tmp_path / "wfroot-gone" / "SKILL.md")

    def test_a_skill_not_found_computes_the_roots_once(self, tmp_path, monkeypatch):
        """The error lists the roots the lookup searched, not a second
        computation of them."""
        calls = []
        lookup_roots = runtime._skill_lookup_roots

        def counted(plan, cwd=None):
            calls.append(plan)
            return lookup_roots(plan, cwd)

        monkeypatch.setattr(runtime, "_skill_lookup_roots", counted)
        monkeypatch.setenv("WF_SKILLS_ROOT", str(tmp_path / "skills"))

        with pytest.raises(FileNotFoundError, match="was not found. Searched roots: "):
            _read_skill_md("wfroot-nowhere", make_plan())
        assert len(calls) == 1


def fake_git(directory: Path, script: str) -> Path:
    """A `git` in `directory` that runs `script` (sh), and records that it ran."""

    git = directory / "git"
    directory.mkdir(parents=True, exist_ok=True)
    git.write_text(f"#!/bin/sh\necho ran >> '{directory / 'ran'}'\n{script}\n", encoding="utf-8")
    git.chmod(0o755)
    return git


@pytest.mark.skipif(shutil.which("git") is None, reason="no git on PATH")
class TestGitDecidesTheRepository:
    """Where a repository ends is git's answer, `git rev-parse
    --show-toplevel`, which refuses what `git status` there would refuse: a
    `.git` that is no repository, and one owned by a user git does not trust
    (`safe.directory`). Without that answer, the other tools' project
    folders are the workflow's directory's alone.
    """

    @pytest.fixture(autouse=True)
    def isolated_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(tmp_path)
        return home

    @staticmethod
    def flow(directory: Path) -> types.SimpleNamespace:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "flow.py").write_text("", encoding="utf-8")
        return make_plan(source_path=str(directory / "flow.py"))

    @pytest.mark.parametrize("planted", ["file", "link to a directory", "link to a file"])
    def test_a_planted_git_is_no_repository(self, planted, tmp_path):
        """A `/tmp/.git` that is a file, or a link to anything, does not make
        `/tmp` a project for a workflow in `/tmp/job`."""
        shared_tmp = tmp_path / "tmp"
        shared_tmp.mkdir()
        victim = tmp_path / "victim"
        (victim / "anything").mkdir(parents=True)
        (victim / "notes.txt").write_text("", encoding="utf-8")
        if planted == "file":
            (shared_tmp / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
        else:
            target = victim / ("anything" if planted == "link to a directory" else "notes.txt")
            try:
                (shared_tmp / ".git").symlink_to(target)
            except OSError as error:
                pytest.skip(f"cannot create a symlink here: {error}")
        make_skill(shared_tmp / ".agents" / "skills", "wfroot-planted")
        beside = make_skill(shared_tmp / "job" / ".agents" / "skills", "wfroot-beside-job")
        plan = self.flow(shared_tmp / "job")

        assert runtime._repository_root((shared_tmp / "job").resolve()) is None
        assert _resolve_skill_dir("wfroot-planted", plan) is None
        assert _resolve_skill_dir("wfroot-beside-job", plan) == beside

    def test_a_repository_git_refuses_is_none(self, tmp_path, monkeypatch):
        """git refuses a repository of dubious ownership (another user's, not
        in `safe.directory`), as it does for `git status` there."""
        project = tmp_path / "project"
        git_init(project)
        make_skill(project / ".agents" / "skills", "wfroot-dubious")
        bin_dir = tmp_path / "bin"
        refusal = f"fatal: detected dubious ownership in repository at '{project}'"
        fake_git(bin_dir, f'echo "{refusal}" >&2\nexit 128')
        monkeypatch.setenv("PATH", str(bin_dir))

        assert _resolve_skill_dir("wfroot-dubious", self.flow(project / "flows")) is None
        assert (bin_dir / "ran").exists()

    def test_git_itself_refuses_another_users_repository_unless_it_is_a_safe_directory(
        self, tmp_path, monkeypatch
    ):
        """With git's own check: `GIT_TEST_ASSUME_DIFFERENT_OWNER` makes git
        take every repository for another user's, as it would one planted
        by someone else; `safe.directory` is how a user says it is theirs."""
        project = tmp_path / "project"
        git_init(project)
        in_project = make_skill(project / ".agents" / "skills", "wfroot-not-yours")
        monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
        flows = (project / "flows").resolve()
        flows.mkdir()
        if runtime._repository_root(flows) is not None:
            pytest.skip("this git has no GIT_TEST_ASSUME_DIFFERENT_OWNER")

        assert _resolve_skill_dir("wfroot-not-yours", self.flow(project / "flows")) is None
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(project.resolve()))
        assert _resolve_skill_dir("wfroot-not-yours", self.flow(project / "flows")) == in_project

    def test_without_git_only_the_workflows_directory_is_searched(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        git_init(project)
        make_skill(project / ".agents" / "skills", "wfroot-no-git")
        beside = make_skill(project / "flows" / ".agents" / "skills", "wfroot-no-git-beside")
        empty = tmp_path / "empty-path"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        plan = self.flow(project / "flows")

        assert _resolve_skill_dir("wfroot-no-git", plan) is None
        assert _resolve_skill_dir("wfroot-no-git-beside", plan) == beside

    def test_a_toplevel_that_does_not_hold_the_workflow_is_none(self, tmp_path, monkeypatch):
        """Whatever a git on PATH prints, the bound is above the workflow."""
        bin_dir = tmp_path / "bin"
        fake_git(bin_dir, f"echo '{tmp_path / 'somewhere-else'}'")
        monkeypatch.setenv("PATH", str(bin_dir))

        assert runtime._repository_root((tmp_path / "project" / "flows").resolve()) is None

    def test_git_dir_in_the_environment_does_not_name_the_repository(self, tmp_path, monkeypatch):
        elsewhere = tmp_path / "elsewhere"
        git_init(elsewhere)
        loose = tmp_path / "loose" / "flows"
        loose.mkdir(parents=True)
        monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))

        assert runtime._repository_root(loose.resolve()) is None

    def test_wfpy_own_folders_are_read_as_they_always_were(self, tmp_path):
        """`.wf/skills` and `.claude/skills` walk to the filesystem root, with
        no repository or git asked."""
        theirs = make_skill(tmp_path / "tmp" / ".claude" / "skills", "wfroot-own-unchecked")

        assert (
            _resolve_skill_dir("wfroot-own-unchecked", self.flow(tmp_path / "tmp" / "job"))
            == theirs
        )


class TestTheRootsAreWorkedOutOncePerPlan:
    def test_git_is_asked_once_for_every_lookup_of_a_plan(self, tmp_path, monkeypatch):
        """A firing reads the skill, resolves its hooks and loads a preset;
        each looks the roots up, which are worked out once."""
        calls: list[Path] = []
        real = runtime._repository_root

        def counted(start: Path) -> Path | None:
            calls.append(start)
            return real(start)

        monkeypatch.setattr(runtime, "_repository_root", counted)
        monkeypatch.chdir(tmp_path)
        skill_dir = make_skill(tmp_path / "flows" / ".agents" / "skills", "wfroot-once")
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "pre.sh").write_text("true\n", encoding="utf-8")
        (tmp_path / "flows" / "flow.py").write_text("", encoding="utf-8")
        plan = make_plan(source_path=str(tmp_path / "flows" / "flow.py"))

        _read_skill_md("wfroot-once", plan)
        _resolve_skill_dir("wfroot-once", plan)
        assert runtime._resolve_skill_hook_script("wfroot-once", "pre", plan) is not None
        assert len(calls) == 1

        other = make_plan(source_path=str(tmp_path / "flows" / "flow.py"))
        _resolve_skill_dir("wfroot-once", other)
        assert len(calls) == 2

    def test_workers_firing_at_once_run_git_once(self, tmp_path, monkeypatch):
        """Parallel workers fire on a thread pool and share the plan: the
        first lookup runs git, and the others wait for its answer."""
        calls: list[Path] = []

        def slow(start: Path) -> Path | None:
            calls.append(start)
            time.sleep(0.2)
            return None

        monkeypatch.setattr(runtime, "_repository_root", slow)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "flows").mkdir()
        (tmp_path / "flows" / "flow.py").write_text("", encoding="utf-8")
        plan = make_plan(source_path=str(tmp_path / "flows" / "flow.py"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            answers = list(pool.map(lambda _: _skill_search_roots(plan), range(8)))

        assert len(calls) == 1
        assert all(answer == answers[0] for answer in answers)

    def test_a_plan_asked_from_elsewhere_or_with_other_variables_gets_its_own(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "flows").mkdir()
        (tmp_path / "flows" / "flow.py").write_text("", encoding="utf-8")
        plan = make_plan(source_path=str(tmp_path / "flows" / "flow.py"))
        monkeypatch.chdir(tmp_path)
        first = _skill_search_roots(plan)
        explicit = tmp_path / "explicit"
        monkeypatch.setenv("WF_SKILLS_ROOT", str(explicit))

        assert _skill_search_roots(plan)[0] == explicit.resolve()
        monkeypatch.delenv("WF_SKILLS_ROOT")
        assert _skill_search_roots(plan) == first
        (tmp_path / "elsewhere").mkdir()
        monkeypatch.chdir(tmp_path / "elsewhere")
        assert (tmp_path / "elsewhere" / ".wf" / "skills").resolve() in _skill_search_roots(plan)
