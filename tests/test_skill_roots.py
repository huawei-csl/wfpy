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
    _read_skill_md,
    _resolve_skill_dir,
    _runtime_root_for_skills,
    _skill_search_roots,
)
from wfpy.sidecar import FEATURES


@pytest.fixture(autouse=True)
def no_tool_homes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Other agent tools' user folders follow `CODEX_HOME` and
    `XDG_CONFIG_HOME`; the machine's own values must not reach these tests."""

    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


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

    A project's folders are searched from the runtime root up to the
    repository root, the directory holding `.git`, as Codex and OpenCode do;
    with no repository, at the runtime root alone.
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
            (project / ".git").mkdir(exist_ok=True)
        return make_plan(source_path=str(nested / "wf.py"))

    @staticmethod
    def skip_inside_a_repository(path: Path) -> None:
        """The cases without a repository need none above the test's own
        directory."""
        if any((level / ".git").exists() for level in [path, *path.parents]):
            pytest.skip(f"{path} is inside a repository")

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

    @pytest.mark.parametrize("variable", ["CODEX_HOME", "XDG_CONFIG_HOME"])
    def test_a_relative_tool_home_counts_as_unset(
        self, variable, tmp_path, isolated_home, monkeypatch
    ):
        """The XDG Base Directory specification asks that a relative value be
        ignored; resolving it would depend on where the run starts."""
        below = (
            (".codex", "skills") if variable == "CODEX_HOME" else (".config", "opencode", "skills")
        )
        relative = (
            ("tool-home", "skills")
            if variable == "CODEX_HOME"
            else (
                "tool-home",
                "opencode",
                "skills",
            )
        )
        make_skill(Path.cwd().joinpath(*relative), "wfroot-relative-home")
        default = make_skill(isolated_home.joinpath(*below), "wfroot-default-home")
        monkeypatch.setenv(variable, "tool-home")
        plan = self.workflow(tmp_path / "project")

        assert _resolve_skill_dir("wfroot-relative-home", plan) is None
        assert _resolve_skill_dir("wfroot-default-home", plan) == default

    def test_the_roots_searched_before_keep_their_order_and_come_first(
        self, tmp_path, isolated_home
    ):
        """What was searched before the other tools' folders is searched in
        the same order, ahead of all of them."""
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
            level.joinpath(*parts)
            for level in (runtime_root, project.resolve(), cwd)
            for parts in TOOLS
        ] + [
            home / ".agents" / "skills",
            home / ".codex" / "skills",
            home / ".config" / "opencode" / "skills",
            home / ".config" / "opencode" / "skill",
        ]

        assert _skill_search_roots(plan) == before + after

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

    def test_the_sidecar_says_so(self):
        """An IDE offers what a run finds by asking which lookup it has."""
        assert FEATURES["skillRootsList"] is True
        assert FEATURES["agentSkillFolders"] is True


class TestOtherToolsFoldersStopAtTheRepository:
    """How far up a project's folders of other tools are searched.

    Before they were searched, a folder above the project was never read for
    them, so it must not be now: `/tmp/.agents/skills`, which any local user
    can write, would hand a workflow in `/tmp/job` its prompt text and hook
    scripts. Codex stops at the nearest directory holding `.git`, OpenCode
    at the git worktree. wfpy's own `.wf` and `.claude` folders keep walking
    to the filesystem root, as they always have.
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
        TestOtherToolsSkillFolders.skip_inside_a_repository(shared_tmp)
        make_skill(shared_tmp.joinpath(*folder), "wfroot-review")
        plan = self.flow(shared_tmp / "job")

        assert _resolve_skill_dir("wfroot-review", plan) is None
        assert shared_tmp.joinpath(*folder).resolve() not in _skill_search_roots(plan)

    def test_outside_a_repository_the_runtime_root_is_searched(self, tmp_path):
        shared_tmp = tmp_path / "tmp"
        TestOtherToolsSkillFolders.skip_inside_a_repository(shared_tmp)
        beside = make_skill(shared_tmp / "job" / ".agents" / "skills", "wfroot-beside")

        assert _resolve_skill_dir("wfroot-beside", self.flow(shared_tmp / "job")) == beside

    def test_wfpy_own_folders_above_still_walk_to_the_filesystem_root(self, tmp_path):
        shared_tmp = tmp_path / "tmp"
        TestOtherToolsSkillFolders.skip_inside_a_repository(shared_tmp)
        above = make_skill(shared_tmp / ".claude" / "skills", "wfroot-own-above")

        assert _resolve_skill_dir("wfroot-own-above", self.flow(shared_tmp / "job")) == above

    @pytest.mark.parametrize("marker", ["directory", "file"])
    def test_the_walk_stops_at_the_repository_root_included(self, marker, tmp_path):
        """`.git` is a directory, or a file in a linked worktree or a submodule."""
        outer = tmp_path / "outer"
        repository = outer / "repository"
        repository.mkdir(parents=True)
        if marker == "directory":
            (repository / ".git").mkdir()
        else:
            (repository / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        make_skill(outer / ".agents" / "skills", "wfroot-above-repository")
        at_root = make_skill(repository / ".codex" / "skills", "wfroot-at-repository-root")
        plan = self.flow(repository / "flows" / "nested")

        assert _resolve_skill_dir("wfroot-at-repository-root", plan) == at_root
        assert _resolve_skill_dir("wfroot-above-repository", plan) is None

    def test_the_working_directory_is_searched_though_outside_the_repository(
        self, tmp_path, monkeypatch
    ):
        """`<cwd>` is where the run was started from, as for Codex and
        OpenCode; only the walk up is bounded."""
        repository = tmp_path / "repository"
        (repository / ".git").mkdir(parents=True)
        started_in = tmp_path / "started-in"
        in_cwd = make_skill(started_in / ".opencode" / "skills", "wfroot-in-cwd")
        monkeypatch.chdir(started_in)

        assert _resolve_skill_dir("wfroot-in-cwd", self.flow(repository / "flows")) == in_cwd


class TestHomeIsNoProject:
    """A workflow under the home directory: the walk, or `<cwd>`, can reach
    `~`, whose tool folders are the user's. They are searched in the
    user-level place, after every project folder, wherever they are reached.
    """

    @pytest.fixture(autouse=True)
    def home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".git").mkdir(parents=True)  # a home kept in git, so the walk reaches it
        monkeypatch.setenv("HOME", str(home))
        return home

    @staticmethod
    def flow(home: Path) -> types.SimpleNamespace:
        flows = home / "work" / "flows"
        flows.mkdir(parents=True)
        (flows / "flow.py").write_text("", encoding="utf-8")
        return make_plan(source_path=str(flows / "flow.py"))

    def test_the_home_folders_come_after_the_working_directory(self, home, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        codex_home = tmp_path / "codex-home"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        plan = self.flow(home)
        roots = _skill_search_roots(plan)
        flows = (home / "work" / "flows").resolve()
        cwd = cwd.resolve()
        home = home.resolve()

        # The walk reached `~` and took `~/.claude/skills` as it always did;
        # every root after `<cwd>`'s own is another tool's.
        tool_roots = roots[roots.index(cwd / ".claude" / "skills") + 1 :]
        assert tool_roots == [
            *(flows.joinpath(*parts) for parts in TOOLS),
            *((home / "work").joinpath(*parts) for parts in TOOLS),
            *(cwd.joinpath(*parts) for parts in TOOLS),
            home / ".agents" / "skills",
            codex_home.resolve() / "skills",
            home / ".config" / "opencode" / "skills",
            home / ".config" / "opencode" / "skill",
        ]

    def test_a_working_directory_skill_outranks_the_users(self, home, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        preferred = make_skill(cwd / ".agents" / "skills", "wfroot-cwd-first")
        make_skill(home / ".agents" / "skills", "wfroot-cwd-first")
        monkeypatch.chdir(cwd)

        assert _resolve_skill_dir("wfroot-cwd-first", self.flow(home)) == preferred

    def test_codex_home_elsewhere_is_the_codex_folder_searched(self, home, tmp_path, monkeypatch):
        """With `CODEX_HOME` elsewhere, `~/.codex/skills` is not Codex's user
        folder, and the walk does not take it for a project's."""
        codex_home = tmp_path / "codex-home"
        preferred = make_skill(codex_home / "skills", "wfroot-codex-home")
        make_skill(home / ".codex" / "skills", "wfroot-codex-home")
        make_skill(home / ".codex" / "skills", "wfroot-only-in-home-codex")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.chdir(tmp_path)
        plan = self.flow(home)

        assert _resolve_skill_dir("wfroot-codex-home", plan) == preferred
        assert _resolve_skill_dir("wfroot-only-in-home-codex", plan) is None

    def test_started_in_home_the_user_folders_keep_their_place(self, home, tmp_path, monkeypatch):
        """`<cwd>` is the home directory: its folders are still the user's."""
        preferred = make_skill(home / "work" / ".agents" / "skills", "wfroot-project-first")
        make_skill(home / ".agents" / "skills", "wfroot-project-first")
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
