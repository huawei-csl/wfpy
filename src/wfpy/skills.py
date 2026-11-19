#!/usr/bin/env python3
# coding: utf-8
"""The skills wfpy ships, and where to put them so a tool can find them.

wfpy ships Agent Skills that describe how to build a wfpy workflow: how one actor
is written, how a graph is composed, how an agent node behaves, and the project
layout a new project starts from. They are here rather than in a separate package
because they document *this* code, including behaviour that changed in a given
release -- a skill that describes the framework drifts the moment it versions
separately from it.

`wfpy skills install` links them where a tool looks for skills. That is a link,
not a copy, so an upgrade of wfpy updates the skills in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

SKILLS_DIR = Path(__file__).resolve().parent / "skills"


@dataclass(frozen=True)
class Skill:
    """One shipped skill."""

    name: str
    path: Path
    description: str


def _description(skill_md: Path) -> str:
    """The `description:` line from the frontmatter, unwrapped."""
    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    collected: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if collected and (line.startswith((" ", "\t")) or not line.strip()):
            collected.append(line.strip())      # a folded continuation line
            continue
        if line.startswith("description:"):
            collected = [line[len("description:"):].strip()]
        elif collected:
            break
    return " ".join(p for p in collected if p)


def shipped() -> list[Skill]:
    """Every skill in this wfpy, by name."""
    if not SKILLS_DIR.is_dir():
        return []
    out = []
    for folder in sorted(SKILLS_DIR.iterdir()):
        skill_md = folder / "SKILL.md"
        if skill_md.is_file():
            out.append(Skill(folder.name, folder, _description(skill_md)))
    return out


def install_root(scope: str, workspace: str | Path = ".") -> Path:
    """Where a tool looks for skills, for the given scope.

    `user` is `~/.claude/skills`: wfpy's last search root, and the global one for
    Claude Code and the tools that read its folder, so one link serves both.
    `project` is `<workspace>/.wf/skills`, which wfpy checks ahead of
    `.claude/skills` at the same level.
    """
    if scope == "project":
        return Path(workspace).resolve() / ".wf" / "skills"
    return Path.home() / ".claude" / "skills"


def install(root: Path, skills: list[Skill], force: bool = False) -> list[tuple[str, str]]:
    """Link each skill into `root`. Returns (name, what-happened) per skill.

    A symlink rather than a copy, so `pip install -U wfpy` updates what the links
    point at. Note that wfpy resolves a symlinked skill only from the release that
    moved its containment check ahead of `.resolve()`; an earlier wfpy follows the
    link, finds the target outside the root, and skips it in silence. For those,
    `wfpy skills path` prints a directory to use as WF_SKILLS_ROOT instead.
    """
    root.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, str]] = []
    for skill in skills:
        link = root / skill.name
        if link.is_symlink():
            if link.readlink() == skill.path:
                results.append((skill.name, "already linked"))
                continue
            if not force:
                results.append((skill.name, "links elsewhere; --force to replace"))
                continue
            link.unlink()
        elif link.exists():
            # Never delete a real directory: it is someone's own copy of a skill
            # with that name, and replacing it would lose their edits.
            results.append((skill.name, "a real directory is in the way; left alone"))
            continue
        link.symlink_to(skill.path, target_is_directory=True)
        results.append((skill.name, "linked"))
    return results


def print_install(root: Path, results: list[tuple[str, str]], out=sys.stdout) -> None:
    for name, what in results:
        mark = "+" if what == "linked" else "=" if what == "already linked" else "!"
        print(f"  {mark} {name:<26} {what}", file=out)
    print(f"\ninstalled into {root}", file=out)
    print("If a skill does not resolve, this wfpy may predate symlink support; "
          "use the directory from `wfpy skills path` as WF_SKILLS_ROOT instead.", file=out)
