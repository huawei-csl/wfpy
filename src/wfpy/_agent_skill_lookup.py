"""Which skills and agent presets a run of a workflow finds: what
`wfpy skills` prints.

An editor that offers skill names must offer the ones a run resolves, from
the folder the run takes, and none it cannot find. Rather than each editor
copying the lookup's rules, it runs `wfpy skills --json` with the command it
runs workflows with, from the directory it runs them in, and this answers
with the runtime's own lookup:
`_skill_lookup_roots` for the folders and their order, `_find_skill` for the
folder a name resolves to, `_read_skill_md_file` and
`_parse_skill_frontmatter` for what the run reads there, and the same for
agent presets.
"""

from __future__ import annotations

import os
import re
import types
from pathlib import Path
from typing import Any

from wfpy._agent_prompt_runtime import (
    CLAUDE_AGENT_IGNORED_FIELDS,
    LookupRoot,
    _build_claude_agent_lookup_roots,
    _build_skill_lookup_roots,
    _claude_agent_profile_from,
    _find_claude_agent,
    _find_skill,
    _parse_skill_frontmatter,
    _read_skill_md_file,
    _runtime_root_for_skills,
    _skill_hook_name,
    _skill_hook_script_in,
    _validate_claude_agent_name,
    _validate_skill_name,
)

__all__ = ["agent_skill_lookup"]

DESCRIPTION_MAX_CHARS = 180
"""A skill's description is cut to this many characters for a picker."""

PRESET_LIST_MAX_BYTES = 256_000
"""The largest preset this list reads for its description and the skills it
preloads. A run reads a preset whole, whatever its size; a larger one is
listed without them."""

BLOCK_SCALAR = re.compile(r"[|>][+-]?[0-9]?|[|>][0-9][+-]")
"""A YAML block scalar's indicator: `|`, `>-`, `|2+`, ..."""


def agent_skill_lookup(file_path: str, cwd: str | None = None) -> dict[str, Any]:
    """The skills and agent presets a run of the workflow at `file_path`
    finds, started in `cwd` (this process's working directory when omitted),
    with the environment of this process. A relative `file_path`, like a
    relative `WF_SKILLS_ROOT`, is read from `cwd`. The file is not loaded:
    one that does not parse yet names its skills from the same folders.

    A run keeps its lookup roots for its plan (`_once_per_plan`); this
    builds them on each call, so it answers what is there now.

    `roots` are the folders searched, in order, whether they exist or not.
    `repository` is the git repository that bounds other tools' folders, or
    None. Each of `skills` is a name a run resolves, with the directory it
    takes (`dir`), the root that holds it and, when the run takes it but
    cannot read its `SKILL.md`, the `problem` it fails with. A name whose
    folders hold no `SKILL.md` is listed with `found: false`: a run that
    names it fails. `presets` are the same for agent presets, over
    `presetRoots`.
    """

    run_cwd = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    workflow = (run_cwd / file_path).resolve()
    plan = types.SimpleNamespace(source_path=str(workflow), work_dir=None)
    roots, repository = _build_skill_lookup_roots(plan, run_cwd)
    # A root that is no directory holds no skill: leaving it out changes no
    # answer of `_find_skill` and spares it most of the walk to `/`.
    root_paths = [root.path for root in roots if root.path.is_dir()]
    by_path = {root.path: root for root in roots}

    skills: list[dict[str, Any]] = []
    for name in _names_in(root_paths):
        try:
            _validate_skill_name(name)
        except ValueError:
            continue
        found = _find_skill(name, root_paths)
        if found is None:
            folder = next((path for path in root_paths if (path / name).is_dir()), None)
            if folder is not None:
                skills.append(_without_skill_md(name, by_path[folder], folder / name))
            continue
        skills.append(_skill(name, by_path[found[0]], found[1]))

    preset_roots = _build_claude_agent_lookup_roots(plan, run_cwd)
    preset_paths = [root.path for root in preset_roots if root.path.is_dir()]
    preset_by_path = {root.path: root for root in preset_roots}
    presets: list[dict[str, Any]] = []
    for name in _names_in(preset_paths, ".md"):
        try:
            _validate_claude_agent_name(name)
        except ValueError:
            continue
        found_preset = _find_claude_agent(name, preset_paths)
        if found_preset is not None:
            presets.append(
                _preset(name, preset_by_path[found_preset[0]], found_preset[1], root_paths)
            )

    return {
        "workflow": str(workflow),
        "cwd": str(run_cwd),
        "runtimeRoot": str(_runtime_root_for_skills(plan, run_cwd)),
        "repository": str(repository) if repository is not None else None,
        "roots": [_root(root) for root in roots],
        "skills": skills,
        "presetRoots": [_root(root) for root in preset_roots],
        "presets": presets,
    }


def _root(root: LookupRoot, key: str = "path") -> dict[str, str]:
    """A root as the answer gives it: its path under `key`, the folder as a
    reader knows it, and how the lookup reached it."""

    return {key: str(root.path), "folder": root.folder, "scope": root.scope}


def _names_in(roots: list[Path], suffix: str = "") -> list[str]:
    """Every name in the roots, each once, in the order first met; with
    `suffix` taken off an entry that has it (a preset is `<name>.md`, or a
    directory `<name>` for `<name>/AGENT.md`).

    A hidden entry (the `.git` of a skills folder kept in git, Codex's
    `.system`, a root of its own) is in the list, and goes with every name a
    run refuses: a caller checks each name as the run does
    (`_validate_skill_name`), which refuses one that starts with `.`.
    """

    names: dict[str, None] = {}
    for root in roots:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            names.setdefault(
                entry[: -len(suffix)] if suffix and entry.endswith(suffix) else entry, None
            )
    return list(names)


def _without_skill_md(name: str, root: LookupRoot, skill_dir: Path) -> dict[str, Any]:
    return {
        "name": name,
        "found": False,
        "dir": str(skill_dir),
        **_root(root, "root"),
        "skillMd": None,
        "problem": f"no SKILL.md in {skill_dir}",
        "description": None,
        "hooks": {},
        "scripts": [],
        "warnings": [],
    }


def _skill(name: str, root: LookupRoot, skill_dir: Path) -> dict[str, Any]:
    """What a run that names `name` takes and reads."""

    skill_md = (skill_dir / "SKILL.md").resolve()
    entry: dict[str, Any] = {
        "name": name,
        "found": True,
        "dir": str(skill_dir),
        **_root(root, "root"),
        "skillMd": str(skill_md),
        "problem": None,
        "description": None,
        "hooks": {},
        "scripts": _hook_scripts(skill_dir),
        "warnings": [],
    }
    try:
        text = _read_skill_md_file(name, skill_md)
    except (OSError, ValueError) as exc:  # UnicodeDecodeError is a ValueError
        entry["problem"] = str(exc)
        return entry
    meta, body = _parse_skill_frontmatter(text)
    if body == text and text.startswith("---"):
        # The reader returns the text whole when it takes no front matter.
        entry["warnings"].append(
            "the front matter is not read (hooks, description): it must open with a"
            " `---` line and close with one, with LF line ends"
        )
    entry["description"] = _description(meta, body)
    for phase in ("pre", "post"):
        hook = _skill_hook_name(meta, phase)
        if hook is not None:
            entry["hooks"][phase] = _hook(skill_dir, hook)
    return entry


def _hook(skill_dir: Path, hook: str) -> dict[str, Any]:
    """A declared hook, with the script a run would execute or why none."""

    try:
        _validate_skill_name(hook, "Hook name")
    except ValueError as exc:
        return {"name": hook, "script": None, "problem": str(exc)}
    script = _skill_hook_script_in(skill_dir, hook)
    if script is None:
        return {"name": hook, "script": None, "problem": f"no scripts/{hook}.py or .sh"}
    return {"name": hook, "script": str(script[0]), "interpreter": script[1], "problem": None}


def _hook_scripts(skill_dir: Path) -> list[str]:
    """The hook names the skill's `scripts/` has a script for."""

    try:
        entries = os.listdir(skill_dir / "scripts")
    except OSError:
        return []
    names = set()
    for entry in entries:
        stem, dot, suffix = entry.rpartition(".")
        if not dot or suffix not in ("py", "sh"):
            continue
        try:
            _validate_skill_name(stem, "Hook name")
        except ValueError:
            continue
        if _skill_hook_script_in(skill_dir, stem) is not None:
            names.add(stem)
    return sorted(names)


def _description(meta: dict[str, Any], body: str) -> str | None:
    """The front matter's `description`, else the body's first line that is
    neither blank nor a heading.

    The runtime's front-matter reader takes one line per field, so a YAML
    block scalar (`description: |` or `>`) reads as its indicator alone;
    that is no description to show, nor is the list or mapping the preset
    reader makes of indented lines under `description:`. A description is
    for the picker; the run does not read one.
    """

    raw = meta.get("description")
    text = raw.strip() if isinstance(raw, str) else ""
    if BLOCK_SCALAR.fullmatch(text):
        text = ""
    if not text:
        lines = [line.strip() for line in body.splitlines()]
        if lines and lines[0] == "---" and "---" in lines[1:]:
            # Front matter the reader did not take is still no description.
            lines = lines[lines.index("---", 1) + 1 :]
        text = next((line for line in lines if line and not line.startswith("#")), "")
    if not text:
        return None
    if len(text) > DESCRIPTION_MAX_CHARS:
        return text[: DESCRIPTION_MAX_CHARS - 3] + "..."
    return text


def _preset(name: str, root: LookupRoot, path: Path, skill_roots: list[Path]) -> dict[str, Any]:
    """What a run that names the preset `name` reads, and whether the skills
    it preloads are found."""

    entry: dict[str, Any] = {
        "name": name,
        "path": str(path),
        **_root(root, "root"),
        "problem": None,
        "description": None,
        "skills": [],
        "ignoredFields": [],
        "warnings": [],
    }
    try:
        size = path.stat().st_size
        if size > PRESET_LIST_MAX_BYTES:
            entry["warnings"].append(
                f"{size} bytes, more than this list reads ({PRESET_LIST_MAX_BYTES}): its"
                " description and skills are not shown; a run reads it whole"
            )
            return entry
        profile = _claude_agent_profile_from(name, path, path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # UnicodeDecodeError is a ValueError
        entry["problem"] = str(exc)
        return entry
    meta = profile["meta"]
    entry["description"] = _description(meta, str(profile["body"]))
    entry["ignoredFields"] = [
        field for field in (*CLAUDE_AGENT_IGNORED_FIELDS, "model") if field in meta
    ]
    entry["warnings"] = list(profile["warnings"])
    for skill_name in profile["skills"]:
        entry["skills"].append(_preloaded_skill(skill_name, skill_roots))
    return entry


def _preloaded_skill(name: str, skill_roots: list[Path]) -> dict[str, Any]:
    """A skill a preset preloads: the folder a run takes, or why it fails."""

    try:
        _validate_skill_name(name)
    except ValueError as exc:
        return {"name": name, "dir": None, "problem": str(exc)}
    found = _find_skill(name, skill_roots)
    if found is None:
        return {"name": name, "dir": None, "problem": f"Skill '{name}' was not found."}
    try:
        _read_skill_md_file(name, (found[1] / "SKILL.md").resolve())
    except (OSError, ValueError) as exc:
        return {"name": name, "dir": str(found[1]), "problem": str(exc)}
    return {"name": name, "dir": str(found[1]), "problem": None}
