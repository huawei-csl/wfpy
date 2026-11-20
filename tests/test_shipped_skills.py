"""The skills wfpy ships are shaped the way wfpy's own resolver requires.

These run against the real `_agent_prompt_runtime` functions rather than a second
copy of their rules, so a change to how wfpy finds or reads a skill fails here.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from wfpy import _agent_prompt_runtime as runtime
from wfpy.cli import main
from wfpy.skills import SKILLS_DIR, install, install_root, shipped

EXPECTED = {
    "wfpy-workflow-builder",
    "wfpy-actor-authoring",
    "wfpy-graph-composition",
    "wfpy-agent-nodes",
}


@pytest.fixture()
def plan(tmp_path):
    return SimpleNamespace(source_path=None, work_dir=str(tmp_path))


@pytest.fixture()
def names():
    return sorted(s.name for s in shipped())


def test_ships_the_expected_skills(names):
    assert set(names) == EXPECTED


def test_every_skill_has_a_description():
    for skill in shipped():
        assert len(skill.description) > 80, f"{skill.name}: description too thin to route on"
        assert "trigger" in skill.description.lower(), f"{skill.name}: no trigger words"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_resolver_finds_each_skill(name, plan, monkeypatch):
    monkeypatch.setenv("WF_SKILLS_ROOT", str(SKILLS_DIR))
    assert runtime._resolve_skill_dir(name, plan) == SKILLS_DIR / name


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_resolver_reads_each_body(name, plan, monkeypatch):
    """Exercises the size cap and the frontmatter parser, not only the path."""
    monkeypatch.setenv("WF_SKILLS_ROOT", str(SKILLS_DIR))
    body, path = runtime._read_skill_md(name, plan)
    assert path == SKILLS_DIR / name / "SKILL.md"
    assert body.strip()
    assert len(body.encode("utf-8")) < runtime.SKILL_MD_MAX_BYTES


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_frontmatter_name_matches_directory(name):
    text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    meta, _ = runtime._parse_frontmatter_markdown(text)
    assert meta.get("name") == name


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_no_dangling_relative_references(name):
    """A reference wfpy cannot resolve is left as written and reaches the agent
    as a path that is not there.

    Fenced and inline code is stripped first: `Port[File](direction="in")` reads
    as a markdown link to a regex, and flagging it was this check's own first bug.
    """
    skill_dir = SKILLS_DIR / name
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    prose = re.sub(r"`[^`\n]+`", "", prose)

    dangling = [
        target
        for target in re.findall(r"\[[^\]]+\]\(([^)\s]+)\)", prose)
        if "://" not in target
        and not target.startswith(("#", "/", "~"))
        and runtime._resolve_skill_reference(target, skill_dir) is None
    ]
    assert not dangling, f"{name} references missing files: {dangling}"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_injection_leaves_each_body_as_written(name):
    """An agent that loads a skill gets its text with the references to the
    skill's own files made absolute. These skills have no such files, so
    nothing in them may be taken for one: a code span broken over two lines
    was, and the folder's path landed in the middle of a sentence."""
    skill_dir = SKILLS_DIR / name
    _, body = runtime._parse_frontmatter_markdown(
        (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    )
    assert runtime._rewrite_skill_body_paths(body, skill_dir) == body


def test_no_skill_names_a_command_wfpy_does_not_have():
    """`wfpy-skills lint` and `wfpy-skills new` belong to a package wfpy does
    not ship; a skill that sends its reader to them sends it nowhere."""
    for skill in shipped():
        assert "wfpy-skills " not in (skill.path / "SKILL.md").read_text(encoding="utf-8")


# ── install ────────────────────────────────────────────────────────────────


def test_install_links_every_skill(tmp_path):
    results = install(tmp_path / "skills", shipped())
    assert {what for _, what in results} == {"linked"}
    for skill in shipped():
        link = tmp_path / "skills" / skill.name
        assert link.is_symlink() and link.readlink() == skill.path


def test_install_is_idempotent(tmp_path):
    root = tmp_path / "skills"
    install(root, shipped())
    results = install(root, shipped())
    assert {what for _, what in results} == {"already linked"}


def test_install_keeps_a_foreign_link_without_force(tmp_path):
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    other = tmp_path / "somewhere-else"
    other.mkdir()
    (root / "wfpy-actor-authoring").symlink_to(other, target_is_directory=True)

    results = dict(install(root, shipped()))
    assert "--force" in results["wfpy-actor-authoring"]
    assert (root / "wfpy-actor-authoring").readlink() == other

    results = dict(install(root, shipped(), force=True))
    assert results["wfpy-actor-authoring"] == "linked"
    assert (root / "wfpy-actor-authoring").readlink() == SKILLS_DIR / "wfpy-actor-authoring"


def test_install_never_deletes_a_real_directory(tmp_path):
    """Someone's own copy of a skill by that name must survive, even with --force:
    replacing it would lose edits that are not ours to discard."""
    root = tmp_path / "skills"
    (root / "wfpy-agent-nodes").mkdir(parents=True)
    (root / "wfpy-agent-nodes" / "SKILL.md").write_text("mine", encoding="utf-8")

    results = dict(install(root, shipped(), force=True))
    assert "in the way" in results["wfpy-agent-nodes"]
    assert (root / "wfpy-agent-nodes" / "SKILL.md").read_text(encoding="utf-8") == "mine"


def test_install_says_when_a_link_points_at_nothing(tmp_path):
    """A link made from an environment that is gone since: kept, as any link
    that is not ours, and said to be broken rather than to be elsewhere."""
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    (root / "wfpy-agent-nodes").symlink_to(tmp_path / "old-venv" / "wfpy-agent-nodes")

    results = dict(install(root, shipped()))
    assert results["wfpy-agent-nodes"] == "links to a folder that is gone; --force to replace"
    assert not (root / "wfpy-agent-nodes").exists()

    results = dict(install(root, shipped(), force=True))
    assert results["wfpy-agent-nodes"] == "linked"
    assert (root / "wfpy-agent-nodes" / "SKILL.md").is_file()


def test_install_knows_its_own_link_by_where_it_leads(tmp_path):
    """Written another way (through a link to the package), it is still ours."""
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(SKILLS_DIR, target_is_directory=True)
    (root / "wfpy-agent-nodes").symlink_to(alias / "wfpy-agent-nodes", target_is_directory=True)

    assert dict(install(root, shipped()))["wfpy-agent-nodes"] == "already linked"


def test_install_root_scopes(tmp_path):
    assert install_root("user") == Path.home() / ".claude" / "skills"
    assert install_root("project", tmp_path) == tmp_path.resolve() / ".wf" / "skills"


def test_installed_skills_resolve_from_the_user_root(tmp_path, plan, monkeypatch):
    """The whole point of the symlink install: a skill resolves with no
    WF_SKILLS_ROOT set. Needs the containment check that precedes `.resolve()`."""
    root = tmp_path / ".claude" / "skills"
    install(root, shipped())
    monkeypatch.delenv("WF_SKILLS_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert runtime._resolve_skill_dir("wfpy-agent-nodes", plan) == SKILLS_DIR / "wfpy-agent-nodes"


# ── the command ────────────────────────────────────────────────────────────


class TestTheCommand:
    """`wfpy skills install|path|list` beside `wfpy skills <workflow.py>`, which
    the IDE runs: both are one command, told apart by its first argument."""

    def run(self, monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
        monkeypatch.setattr(sys, "argv", ["wfpy", "skills", *argv])
        code = 0
        try:
            main()
        except SystemExit as exited:
            code = int(exited.code or 0)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def test_list_names_every_skill(self, monkeypatch, capsys):
        code, out, _ = self.run(monkeypatch, capsys, "list")
        assert code == 0
        assert [line.split()[0] for line in out.splitlines()] == sorted(EXPECTED)

    def test_path_is_the_directory_a_run_can_be_given(self, plan, monkeypatch, capsys):
        code, out, _ = self.run(monkeypatch, capsys, "path")
        assert code == 0
        monkeypatch.setenv("WF_SKILLS_ROOT", out.strip())
        found = runtime._resolve_skill_dir("wfpy-agent-nodes", plan)
        assert found == SKILLS_DIR / "wfpy-agent-nodes"

    def test_install_for_the_user(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        code, out, _ = self.run(monkeypatch, capsys, "install")
        assert code == 0
        assert f"installed into {tmp_path / '.claude' / 'skills'}" in out
        for name in EXPECTED:
            assert (tmp_path / ".claude" / "skills" / name).is_symlink()

    def test_install_for_a_project_and_the_file_form_finds_it(self, tmp_path, monkeypatch, capsys):
        """The two forms of the command, one after the other: what `install`
        links, `wfpy skills <workflow.py>` lists, from `.wf/skills`."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        project = tmp_path / "project"
        (project / "src").mkdir(parents=True)
        (project / "src" / "flow.py").write_text("", encoding="utf-8")
        monkeypatch.chdir(project)

        code, _, _ = self.run(
            monkeypatch, capsys, "install", "--scope", "project", "--workspace", str(project)
        )
        assert code == 0

        code, out, _ = self.run(monkeypatch, capsys, "--json", "src/flow.py")
        assert code == 0
        found = {skill["name"]: skill for skill in json.loads(out)["skills"]}
        assert set(found) == EXPECTED
        assert {skill["folder"] for skill in found.values()} == {".wf/skills"}
        assert not any(skill["problem"] for skill in found.values())

    def test_install_fails_when_a_name_is_taken(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path / ".claude" / "skills" / "wfpy-agent-nodes").mkdir(parents=True)
        code, out, _ = self.run(monkeypatch, capsys, "install", "--force")
        assert code == 1
        assert "a real directory is in the way" in out

    def test_a_workflow_file_named_as_a_command_is_written_with_its_folder(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        (tmp_path / "list").write_text("", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        code, out, _ = self.run(monkeypatch, capsys, "--json", "./list")
        assert code == 0
        assert json.loads(out)["workflow"] == str((tmp_path / "list").resolve())

    @pytest.mark.parametrize(
        "argv",
        [
            ("list", "--json"),
            ("path", "--force"),
            ("list", "--scope", "user"),
            ("install", "--json"),
        ],
    )
    def test_an_option_of_the_other_form_is_refused(self, argv, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        code, out, err = self.run(monkeypatch, capsys, *argv)
        assert code == 2
        assert out == ""
        assert "go" in err
        assert not (tmp_path / ".claude").exists()

    def test_an_install_option_with_a_workflow_file_is_refused(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "flow.py").write_text("", encoding="utf-8")
        code, out, err = self.run(monkeypatch, capsys, str(tmp_path / "flow.py"), "--force")
        assert code == 2
        assert out == ""
        assert "not with a workflow file" in err
