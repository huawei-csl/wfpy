"""The skills wfpy ships are shaped the way wfpy's own resolver requires.

These run against the real `_agent_prompt_runtime` functions rather than a second
copy of their rules, so a change to how wfpy finds or reads a skill fails here.
"""

from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from wfpy import _agent_prompt_runtime as runtime
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
