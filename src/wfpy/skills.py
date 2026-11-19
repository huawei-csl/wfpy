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

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

SKILLS_DIR = Path(__file__).resolve().parent / "skills"


@dataclass(frozen=True)
class Skill:
    """One shipped skill."""

    name: str
    path: Path
    description: str


def _description(skill_md: Path) -> str:
    """The `description` of a skill's front matter, read as a run reads it."""
    from wfpy._agent_prompt_runtime import _parse_frontmatter_markdown

    meta, _body = _parse_frontmatter_markdown(skill_md.read_text(encoding="utf-8"))
    description = meta.get("description", "")
    return description if isinstance(description, str) else ""


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

    `user` is `~/.claude/skills`: the user-level folder a run searches after a
    project's own folders, and the global one for Claude Code and the tools
    that read its folder, so one link serves both. `project` is
    `<workspace>/.wf/skills`, which wfpy checks ahead of `.claude/skills` at
    the same level. A run finds it when the workspace is the directory the run
    starts in, or lies between the workflow's directory and the top of its
    project (`_agent_prompt_runtime._skill_lookup_roots`).
    """
    if scope == "project":
        return Path(workspace).resolve() / ".wf" / "skills"
    return Path.home() / ".claude" / "skills"


def install(root: Path, skills: list[Skill], force: bool = False) -> list[tuple[str, str]]:
    """Link each skill into `root`. Returns (name, what-happened) per skill.

    A symlink rather than a copy, so `pip install -U wfpy` updates what the
    links point at. A link made from an environment that has gone since (a
    venv rebuilt on another Python) points at nothing. It is reported as such
    and, like any link that is not this wfpy's, kept unless `force`.
    """
    root.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, str]] = []
    for skill in skills:
        link = root / skill.name
        if link.is_symlink():
            if link.resolve() == skill.path.resolve():
                results.append((skill.name, "already linked"))
                continue
            if not force:
                where = "elsewhere" if link.exists() else "to a folder that is gone"
                results.append((skill.name, f"links {where}; --force to replace"))
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


def print_install(root: Path, results: list[tuple[str, str]], out: TextIO | None = None) -> None:
    """Say what `install` did, and how to check that a run finds the skills."""
    out = sys.stdout if out is None else out
    for name, what in results:
        mark = "+" if what == "linked" else "=" if what == "already linked" else "!"
        print(f"  {mark} {name:<26} {what}", file=out)
    print(f"\ninstalled into {root}", file=out)
    print(
        "`wfpy skills <workflow.py>` lists the skills a run of that workflow finds, "
        "and the folder each comes from.",
        file=out,
    )
