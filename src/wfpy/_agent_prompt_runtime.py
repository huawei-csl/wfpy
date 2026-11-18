"""Internal helpers for agent prompt composition and skill loading."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

from wfpy.core import AgentSpec

SKILL_MD_MAX_BYTES = 256_000
"""Maximum SKILL.md size accepted by the runtime loader."""

AGENT_MAX_TOOL_ROUNDS = 6
"""Maximum tool-call continuation rounds per single agent firing."""


def _explicit_skills_roots() -> list[Path]:
    """Parse `WF_SKILLS_ROOT` into the roots it names, in priority order.

    The variable holds one directory or several separated by `os.pathsep`
    (`:` on POSIX, `;` on Windows), like `PATH`. A single directory — the
    only form the variable used to take — parses to a one-element list, so
    an existing setting keeps behaving exactly as before.
    """

    raw = os.environ.get("WF_SKILLS_ROOT", "")
    roots: list[Path] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if entry:
            roots.append(Path(entry).expanduser())
    return roots


OTHER_TOOL_PROJECT_SKILL_DIRS: tuple[tuple[str, ...], ...] = (
    (".agents", "skills"),
    (".codex", "skills"),
    (".opencode", "skills"),
    (".opencode", "skill"),
)
"""Where other agent tools keep a project's skills, relative to a directory.

`.agents/skills` is the folder the Agent Skills convention shares between
tools, and the one Codex documents for a repository. `.codex/skills` is the
skills folder of a Codex project configuration (`.codex/`). OpenCode reads
`skills` and the older `skill` under `.opencode/`. Each holds
`<skill-name>/SKILL.md`, the layout wfpy already reads.
"""


class LookupRoot(NamedTuple):
    """A directory a lookup searches, and how a reader knows it."""

    path: Path
    """The directory, resolved."""

    folder: str
    """The directory as a reader knows it: `.claude/skills`, `.agents/skills`,
    `~/.agents/skills`, `$CODEX_HOME/skills`, `WF_SKILLS_ROOT`, ..."""

    scope: str
    """How the lookup reached it: `explicit` (named by `WF_SKILLS_ROOT` or
    `WF_CLAUDE_AGENTS_ROOT`), `project` (the workflow's directory or one
    above it), `cwd` (the working directory) or `user`."""


class IgnoredRoot(NamedTuple):
    """A folder of another agent tool that a lookup reaches but does not read."""

    path: Path
    folder: str
    reason: str
    """Why it is not trusted (see `_distrust`)."""


def _absolute_dir_from_env(name: str) -> Path | None:
    """The directory an environment variable names, if it names an absolute one.

    A relative value is ignored, as the XDG Base Directory specification asks
    of `XDG_CONFIG_HOME`: resolving it would make the folder depend on the
    directory a run happens to start in.
    """

    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else None


def _other_tool_user_skill_dirs() -> list[tuple[Path, str]]:
    """Where other agent tools keep a user's skills, in the order searched,
    each with the name a reader knows it by.

    `~/.agents/skills` (the shared folder), Codex's `$CODEX_HOME/skills`
    (`~/.codex/skills` when `CODEX_HOME` is unset), then OpenCode's
    `$XDG_CONFIG_HOME/opencode/skills` and `.../skill` (`~/.config` when
    `XDG_CONFIG_HOME` is unset). A relative `CODEX_HOME` or
    `XDG_CONFIG_HOME` counts as unset.
    """

    home = Path.home()
    codex_home = _absolute_dir_from_env("CODEX_HOME")
    config_home = _absolute_dir_from_env("XDG_CONFIG_HOME")
    codex, codex_name = (codex_home, "$CODEX_HOME") if codex_home else (home / ".codex", "~/.codex")
    config, config_name = (
        (config_home, "$XDG_CONFIG_HOME") if config_home else (home / ".config", "~/.config")
    )
    return [
        (home / ".agents" / "skills", "~/.agents/skills"),
        (codex / "skills", f"{codex_name}/skills"),
        (config / "opencode" / "skills", f"{config_name}/opencode/skills"),
        (config / "opencode" / "skill", f"{config_name}/opencode/skill"),
    ]


def _is_private_group(gid: int) -> bool:
    """Whether `gid` is the current user's private group: the user's primary
    group, named after the user, with no other member.

    Systems that give every user such a group set a umask of 002, so each
    folder the user makes is writable by it. Writable by that group is
    writable by the user alone.
    """

    try:
        import grp
        import pwd

        user = pwd.getpwuid(os.getuid())
        group = grp.getgrgid(gid)
    except (ImportError, KeyError):
        return False
    return (
        gid == user.pw_gid and group.gr_name == user.pw_name and set(group.gr_mem) <= {user.pw_name}
    )


def _distrust(status: os.stat_result) -> str | None:
    """Why a folder of another tool, or a `.git`, with this status is not
    trusted; None when it is.

    Trusted: owned by the current user or by root, and writable by no other
    user: neither by every user nor by a group, unless that group is the
    user's private one. Anyone can create `/tmp/.agents/skills` or
    `/tmp/.git`; what they create is theirs, and is not read.

    POSIX only. Windows has no owner and mode bits to ask, and the callers
    do not check there.
    """

    if status.st_uid not in (os.getuid(), 0):
        return f"owned by another user (uid {status.st_uid})"
    if status.st_mode & stat.S_IWOTH:
        return "writable by every user"
    if status.st_mode & stat.S_IWGRP and not _is_private_group(status.st_gid):
        return f"writable by group {status.st_gid}"
    return None


def _nearest_repository(start: Path) -> Path | None:
    """The repository `start` is in: the nearest directory from `start` up
    that holds a trusted `.git`.

    `.git` is a directory, or a file in a linked worktree or a submodule. The
    first one met decides. One that is not trusted, or cannot be examined, is
    no repository, and the search does not go on past it: a `/tmp/.git`
    anyone can plant neither makes `/tmp` a project nor reaches a repository
    around it.

    `cli._discover_project_root` answers another question: it also stops at
    a `pyproject.toml`, and does not ask who owns the marker. The folders
    bounded here are those of Codex (its default `project_root_markers`) and
    OpenCode (the git worktree), which know only `.git`.
    """

    for level in (start, *start.parents):
        try:
            status = os.stat(level / ".git")
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return None
        if os.name == "posix" and _distrust(status) is not None:
            return None
        return level
    return None


def _project_levels(start: Path) -> list[Path]:
    """The directories whose project folders of other tools are searched:
    from `start` up to its repository root, included; `start` alone outside
    a repository."""

    repository = _nearest_repository(start)
    levels = [start, *start.parents]
    return levels[: levels.index(repository) + 1] if repository is not None else [start]


def _workflow_dir(plan: Any, cwd: Path | None = None) -> Path:
    """The workflow's directory: its source file's, else the plan's work
    directory, else the working directory."""

    if plan.source_path:
        try:
            return Path(plan.source_path).resolve().parent
        except Exception:
            pass
    if plan.work_dir:
        try:
            return Path(plan.work_dir).resolve()
        except Exception:
            pass
    return (cwd or Path.cwd()).resolve()


def _runtime_root_for_skills(plan: Any, cwd: Path | None = None) -> Path:
    """Resolve the runtime root used for `.wf/skills` discovery."""

    explicit = _explicit_skills_roots()
    if explicit:
        # The runtime root is a single directory — it is the base of the
        # ancestor walk in `_skill_search_roots()` and the working directory
        # of a skill hook — so when `WF_SKILLS_ROOT` names several, the first
        # (highest-priority) one is it. Every root it names is still searched
        # directly by `_skill_search_roots()`.
        return explicit[0].resolve()
    return _workflow_dir(plan, cwd)


def _runtime_root_for_claude_agents(plan: Any, cwd: Path | None = None) -> Path:
    """Resolve runtime root used for `.claude/agents` discovery."""

    explicit = os.environ.get("WF_CLAUDE_AGENTS_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return _runtime_root_for_skills(plan, cwd)


def _skill_lookup_roots(
    plan: Any, cwd: Path | None = None
) -> tuple[list[LookupRoot], list[IgnoredRoot]]:
    """The directories a skill name is looked up in, in order, and the
    folders of other tools reached but not trusted.

    Supported roots:
    - `<runtime-root>/.wf/skills`
    - `<runtime-root>/.claude/skills`
    - `<cwd>/.wf/skills`
    - `<cwd>/.claude/skills`
    - `~/.claude/skills`

    then the skill folders of other agent tools, after every root above:
    - `<workflow-dir>/<folder>` for each of `OTHER_TOOL_PROJECT_SKILL_DIRS`
    - `<cwd>/<folder>` for each of them
    - the user-level ones of `_other_tool_user_skill_dirs()`

    The `<runtime-root>` entries of wfpy's own folders walk up to the
    filesystem root. Those of the other tools start at the workflow's own
    directory, `WF_SKILLS_ROOT` or not, and walk up only as far as the tools
    themselves do: to the repository root (`_nearest_repository`), included,
    and with no repository, the workflow's directory alone. A folder above
    the repository, say `/tmp/.agents/skills` for a workflow in `/tmp/job`,
    is not read. A user-level folder reached by the walk or at `<cwd>`, as in
    a home directory kept in git, is searched once, in the user-level place;
    the home directory's other project folders (`~/.opencode/skills`) are a
    project's, as they are for OpenCode.

    A folder of another tool is read only if it is trusted (`_distrust`, on
    POSIX): one another user owns or can write is listed among the ignored.
    wfpy's own folders are not checked, as they never were.

    Searching the other tools' folders last means a name found in one of
    wfpy's own folders resolves exactly as it did before they were searched;
    they only add names.

    If `WF_SKILLS_ROOT` is set, each directory it names is treated as a
    highest-priority directory that directly contains skill folders. It may
    name several, separated by `os.pathsep`, searched in the order written.

    `cwd` is the working directory of the run; this process's when omitted.
    """

    cwd_root = (cwd or Path.cwd()).resolve()
    home_root = Path.home().resolve()
    user_tool_dirs = [(path.resolve(), name) for path, name in _other_tool_user_skill_dirs()]
    user_names = {path: name for path, name in user_tool_dirs}
    user_names[(home_root / ".claude" / "skills").resolve()] = "~/.claude/skills"

    roots: list[LookupRoot] = []
    ignored: list[IgnoredRoot] = []
    seen: set[Path] = set()

    def add_root(path: Path, folder: str, scope: str) -> None:
        """Add a resolved directory, named as the user's wherever it is reached."""
        if path in seen:
            return
        seen.add(path)
        if scope != "explicit" and path in user_names:
            folder, scope = user_names[path], "user"
        roots.append(LookupRoot(path, folder, scope))

    def add_tool_root(path: Path, folder: str, scope: str) -> None:
        """Add another tool's resolved folder if it exists and is trusted."""
        if path in seen:
            return
        try:
            status = os.stat(path)
        except OSError:
            return
        reason = _distrust(status) if os.name == "posix" else None
        if reason is not None:
            seen.add(path)
            ignored.append(IgnoredRoot(path, user_names.get(path, folder), reason))
            return
        add_root(path, folder, scope)

    for explicit in _explicit_skills_roots():
        add_root(explicit.resolve(), "WF_SKILLS_ROOT", "explicit")

    runtime_root = _runtime_root_for_skills(plan, cwd_root)
    for level in (runtime_root, *runtime_root.parents):
        add_root((level / ".wf" / "skills").resolve(), ".wf/skills", "project")
        add_root((level / ".claude" / "skills").resolve(), ".claude/skills", "project")

    add_root((cwd_root / ".wf" / "skills").resolve(), ".wf/skills", "cwd")
    add_root((cwd_root / ".claude" / "skills").resolve(), ".claude/skills", "cwd")
    add_root((home_root / ".claude" / "skills").resolve(), "~/.claude/skills", "user")

    # Other agent tools' folders come after every root above, so that a
    # skill already found keeps resolving to the same directory.
    levels = [(level, "project") for level in _project_levels(_workflow_dir(plan, cwd_root))]
    for level, scope in [*levels, (cwd_root, "cwd")]:
        for parts in OTHER_TOOL_PROJECT_SKILL_DIRS:
            candidate = level.joinpath(*parts).resolve()
            if candidate in user_names:
                continue  # the user's, searched in the user-level place below
            add_tool_root(candidate, "/".join(parts), scope)
    for path, name in user_tool_dirs:
        add_tool_root(path, name, "user")

    return roots, ignored


def _skill_search_roots(plan: Any, cwd: Path | None = None) -> list[Path]:
    """Return ordered directories that can contain skill folders.

    See `_skill_lookup_roots` for which, in which order.
    """

    return [root.path for root in _skill_lookup_roots(plan, cwd)[0]]


def _claude_agent_lookup_roots(plan: Any, cwd: Path | None = None) -> list[LookupRoot]:
    """The directories an agent preset is looked up in, in order: the one
    `WF_CLAUDE_AGENTS_ROOT` names, `.claude/agents` from its runtime root up
    to the filesystem root, then the working directory's and the user's."""

    cwd_root = (cwd or Path.cwd()).resolve()
    home_agents = (Path.home().resolve() / ".claude" / "agents").resolve()
    roots: list[LookupRoot] = []
    seen: set[Path] = set()

    def add_root(path: Path, folder: str, scope: str) -> None:
        if path in seen:
            return
        seen.add(path)
        if scope != "explicit" and path == home_agents:
            folder, scope = "~/.claude/agents", "user"
        roots.append(LookupRoot(path, folder, scope))

    explicit = os.environ.get("WF_CLAUDE_AGENTS_ROOT", "").strip()
    if explicit:
        add_root(Path(explicit).expanduser().resolve(), "WF_CLAUDE_AGENTS_ROOT", "explicit")

    runtime_root = _runtime_root_for_claude_agents(plan, cwd_root)
    for level in (runtime_root, *runtime_root.parents):
        add_root((level / ".claude" / "agents").resolve(), ".claude/agents", "project")

    add_root((cwd_root / ".claude" / "agents").resolve(), ".claude/agents", "cwd")
    add_root(home_agents, "~/.claude/agents", "user")
    return roots


def _claude_agent_search_roots(plan: Any, cwd: Path | None = None) -> list[Path]:
    """Return ordered directories that can contain Claude agent markdown files."""

    return [root.path for root in _claude_agent_lookup_roots(plan, cwd)]


def _validate_skill_name(skill_name: str) -> None:
    """Validate user-provided skill names used for filesystem lookup."""

    if not skill_name or not skill_name.strip():
        raise ValueError("Skill name is empty.")
    if "/" in skill_name or "\\" in skill_name:
        raise ValueError("Skill name must not include path separators.")
    if ".." in skill_name:
        raise ValueError("Skill name must not include '..'.")


def _validate_claude_agent_name(agent_name: str) -> None:
    """Validate Claude agent names used for filesystem lookup."""

    _validate_skill_name(agent_name)


def _find_skill(skill_name: str, roots: Iterable[Path]) -> tuple[Path, Path] | None:
    """The first of `roots` with a `<skill_name>/SKILL.md`, and the skill's
    directory there, resolved. `skill_name` is validated already."""

    for root in roots:
        candidate = root / skill_name
        # Containment is tested on the joined path, before resolving it: a
        # skill directory is very often a symlink — that is how tools install
        # shared skills into `.claude/skills` — and such a directory resolves
        # outside its root, so testing after `.resolve()` silently skipped
        # every symlinked install.
        #
        # Nothing is lost by testing it unresolved. `_validate_skill_name()`
        # already rejects path separators and `..`, so `root / skill_name` is
        # a direct child of `root` (or, for the name `.`, `root` itself). What
        # remains is a Windows drive-relative name like `d:bar`, which passes
        # that validation and does join outside `root` — this check is what
        # catches it. Do not reinstate the `.resolve()` before it.
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        skill_dir = candidate.resolve()
        if (skill_dir / "SKILL.md").exists():
            return root, skill_dir
    return None


def _resolve_skill_dir(skill_name: str, plan: Any) -> Path | None:
    """Resolve the skill directory for a skill name from supported roots."""

    _validate_skill_name(skill_name)
    found = _find_skill(skill_name, _skill_search_roots(plan))
    return found[1] if found is not None else None


def _resolve_skill_md_path(skill_name: str, plan: Any) -> Path:
    """Resolve `SKILL.md` for a skill name from supported roots."""

    _validate_skill_name(skill_name)
    roots, ignored = _skill_lookup_roots(plan)
    found = _find_skill(skill_name, [root.path for root in roots])
    if found is None:
        message = f"Skill '{skill_name}' was not found. Searched roots: " + ", ".join(
            str(root.path) for root in roots
        )
        if ignored:
            message += ". Not read, as not trusted: " + ", ".join(
                f"{root.path} ({root.reason})" for root in ignored
            )
        raise FileNotFoundError(message)
    return (found[1] / "SKILL.md").resolve()


def _read_skill_md_file(skill_name: str, skill_md: Path) -> str:
    """Read a skill's `SKILL.md`, checked with one `stat`: a file within
    `SKILL_MD_MAX_BYTES`, in UTF-8."""

    try:
        status = skill_md.stat()
    except FileNotFoundError:
        raise FileNotFoundError(f"Skill '{skill_name}': {skill_md} does not exist.") from None
    if not stat.S_ISREG(status.st_mode):
        # The lookup took the first folder holding a `SKILL.md`, so it exists
        # and is no file: a directory, for instance.
        raise FileNotFoundError(f"Skill '{skill_name}': {skill_md} is not a file.")
    if status.st_size > SKILL_MD_MAX_BYTES:
        raise ValueError(
            f"Skill '{skill_name}' is too large ({status.st_size} bytes). "
            f"Limit is {SKILL_MD_MAX_BYTES} bytes."
        )
    return skill_md.read_text(encoding="utf-8")


def _read_skill_md(skill_name: str, plan: Any) -> tuple[str, Path]:
    """Read and return SKILL.md body text for a skill name."""

    skill_md = _resolve_skill_md_path(skill_name, plan)
    return _read_skill_md_file(skill_name, skill_md), skill_md


def _parse_skill_frontmatter(skill_text: str) -> tuple[dict[str, Any], str]:
    """Parse a minimal YAML-like frontmatter block from SKILL.md."""

    if not skill_text.startswith("---\n"):
        return {}, skill_text
    end = skill_text.find("\n---\n", 4)
    if end < 0:
        return {}, skill_text
    header = skill_text[4:end]
    body = skill_text[end + 5 :]
    meta: dict[str, Any] = {}
    lines = header.splitlines()
    in_hooks = False
    hooks: dict[str, str] = {}
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "hooks:" or stripped == "hooks":
            in_hooks = True
            continue
        if in_hooks:
            if not line.startswith(" ") and not line.startswith("\t"):
                in_hooks = False
            else:
                if ":" in stripped:
                    k, v = stripped.split(":", 1)
                    hooks[k.strip()] = v.strip().strip('"').strip("'")
                continue
        if ":" in stripped:
            k, v = stripped.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    if hooks:
        meta["hooks"] = hooks
    return meta, body


def _split_reference_suffix(ref: str) -> tuple[str, str]:
    """Split a file reference from any trailing query or fragment."""

    for idx, ch in enumerate(ref):
        if ch in "?#":
            return ref[:idx], ref[idx:]
    return ref, ""


def _resolve_skill_reference(ref: str, skill_dir: Path) -> str | None:
    """Resolve an existing relative file reference against a skill directory."""

    stripped = ref.strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        return None
    if "://" in stripped or stripped.startswith(("/", "~")):
        return None

    path_part, suffix = _split_reference_suffix(stripped)
    if not path_part:
        return None

    path_ref = Path(path_part)
    if path_ref.is_absolute():
        return None
    if (
        "/" not in path_part
        and "\\" not in path_part
        and not path_part.startswith(".")
        and path_ref.suffix == ""
    ):
        return None

    resolved = (skill_dir / path_part).resolve()
    if not resolved.exists():
        return None
    return str(resolved) + suffix


def _rewrite_skill_body_paths(skill_body: str, skill_dir: Path) -> str:
    """Rewrite skill-body file references so agents can read them from any cwd."""

    scripts_dir = (skill_dir / "scripts").resolve()
    if scripts_dir.is_dir():
        skill_body = skill_body.replace("./scripts/", str(scripts_dir) + "/")

    def _replace_code_span(match: re.Match[str]) -> str:
        ref = match.group(1)
        resolved = _resolve_skill_reference(ref, skill_dir)
        if resolved is None:
            return match.group(0)
        return f"`{resolved}`"

    skill_body = re.sub(r"`([^`\n]+)`", _replace_code_span, skill_body)

    def _replace_markdown_link(match: re.Match[str]) -> str:
        label = match.group(1)
        target = match.group(2)
        resolved = _resolve_skill_reference(target, skill_dir)
        if resolved is None:
            return match.group(0)
        return f"[{label}]({resolved})"

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _replace_markdown_link, skill_body)


def _parse_frontmatter_markdown(text: str) -> tuple[dict[str, Any], str]:
    """Parse generic markdown frontmatter and return `(meta, body)`.

    This parser is intentionally minimal and supports flat keys, comma lists,
    and one-level nested maps for compatibility with Claude agent docs.
    """

    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    header = text[4:end]
    body = text[end + 5 :]

    meta: dict[str, Any] = {}
    lines = header.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip("\n")
        stripped = line.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()

        if value == "":
            nested: dict[str, Any] = {}
            list_items: list[str] = []
            while i < len(lines):
                child = lines[i]
                child_stripped = child.strip()
                if not child_stripped:
                    i += 1
                    continue
                if not child.startswith(" ") and not child.startswith("\t"):
                    break
                i += 1
                child_clean = child_stripped
                if child_clean.startswith("-"):
                    list_items.append(child_clean[1:].strip().strip('"').strip("'"))
                    continue
                if ":" in child_clean:
                    ck, cv = child_clean.split(":", 1)
                    nested[ck.strip()] = cv.strip().strip('"').strip("'")
            if nested:
                meta[key] = nested
            elif list_items:
                meta[key] = [item for item in list_items if item]
            else:
                meta[key] = ""
            continue

        normalized = value.strip().strip('"').strip("'")
        if "," in normalized:
            items = [part.strip() for part in normalized.split(",") if part.strip()]
            meta[key] = items
        else:
            meta[key] = normalized

    return meta, body


CLAUDE_AGENT_IGNORED_FIELDS: tuple[str, ...] = (
    "tools",
    "disallowedTools",
    "permissionMode",
    "maxTurns",
    "mcpServers",
    "hooks",
    "memory",
    "background",
    "effort",
    "isolation",
)
"""Claude agent front-matter fields wfpy's compatibility mode ignores (and `model`)."""


def _claude_agent_frontmatter_warnings(meta: dict[str, Any]) -> list[str]:
    """Return compatibility warnings for unsupported Claude agent fields."""

    warnings: list[str] = []
    for field in CLAUDE_AGENT_IGNORED_FIELDS:
        if field in meta:
            warnings.append(
                f"claude-agent field '{field}' is currently ignored in wfpy compatibility mode"
            )
    if "model" in meta:
        warnings.append(
            "claude-agent field 'model' is ignored; wfpy keeps current model/provider selection"
        )
    return warnings


def _find_claude_agent(agent_name: str, roots: Iterable[Path]) -> tuple[Path, Path] | None:
    """The first of `roots` with `<agent_name>.md` or `<agent_name>/AGENT.md`,
    and that file, resolved. `agent_name` is validated already."""

    for root in roots:
        direct = (root / f"{agent_name}.md").resolve()
        nested = (root / agent_name / "AGENT.md").resolve()
        for candidate in (direct, nested):
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                return root, candidate
    return None


def _resolve_claude_agent_path(agent_name: str, plan: Any) -> Path:
    """Resolve Claude agent markdown by name from supported roots."""

    _validate_claude_agent_name(agent_name)
    roots = _claude_agent_search_roots(plan)
    found = _find_claude_agent(agent_name, roots)
    if found is None:
        raise FileNotFoundError(
            f"Claude agent '{agent_name}' was not found. Searched roots: "
            + ", ".join(str(root) for root in roots)
        )
    return found[1]


def _normalize_frontmatter_string_list(value: Any) -> list[str]:
    """Normalize frontmatter list values that may appear as list or CSV."""

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        if not value.strip():
            return []
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _load_claude_agent_profile(agent_name: str, plan: Any) -> dict[str, Any]:
    """Load Claude agent markdown profile and compatibility metadata."""

    return _claude_agent_profile_at(agent_name, _resolve_claude_agent_path(agent_name, plan))


def _claude_agent_profile_at(agent_name: str, profile_path: Path) -> dict[str, Any]:
    """Read the Claude agent markdown profile at `profile_path`."""

    raw = profile_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter_markdown(raw)
    skills = _normalize_frontmatter_string_list(meta.get("skills"))
    warnings = _claude_agent_frontmatter_warnings(meta)
    return {
        "name": agent_name,
        "path": str(profile_path),
        "meta": meta,
        "body": body.strip(),
        "skills": skills,
        "warnings": warnings,
        "description": str(meta.get("description", "")).strip(),
    }


def _claude_agent_max_turns(profile: dict[str, Any]) -> int | None:
    """Extract optional maxTurns from Claude profile frontmatter."""

    meta = profile.get("meta")
    if not isinstance(meta, dict):
        return None
    raw = meta.get("maxTurns")
    if raw is None:
        return None
    try:
        val = int(str(raw).strip())
    except Exception:
        return None
    if val <= 0:
        return None
    return val


def _effective_max_tool_rounds(spec: AgentSpec, profile: dict[str, Any] | None) -> int:
    """Resolve max tool rounds, honoring Claude profile maxTurns when present.

    The value is treated as a compatibility hint and clamped for safety.
    """

    del spec
    rounds = AGENT_MAX_TOOL_ROUNDS
    if profile is not None:
        hint = _claude_agent_max_turns(profile)
        if hint is not None:
            rounds = min(max(hint, 1), 64)
    return rounds


def _normalize_skill_hook_auth_mode(options: dict[str, Any]) -> str:
    """Resolve skill-hook authorization mode from options / env."""

    raw = (
        str(options.get("skill_hook_auth") or os.environ.get("WF_SKILL_HOOK_AUTH", "policy"))
        .strip()
        .lower()
    )
    if raw in ("deny-all", "allow-all", "policy"):
        return raw
    raise ValueError(
        f"Invalid skill hook auth mode '{raw}'. Expected: deny-all, allow-all, policy."
    )


def _load_skill_hook_policy(path_str: str) -> dict[str, Any]:
    """Load the JSON skill-hook policy file."""

    try:
        result: dict[str, Any] = json.loads(Path(path_str).read_text())
        return result
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Cannot load skill hook policy '{path_str}': {exc}") from exc


def _authorize_skill_hook(
    options: dict[str, Any],
    skill_name: str,
    hook_name: str,
    interpreter: str,
) -> tuple[bool, str]:
    """Authorize running a skill hook script under configured policy."""

    mode = _normalize_skill_hook_auth_mode(options)
    if mode == "deny-all":
        return False, "mode=deny-all"
    if mode == "allow-all":
        return True, "mode=allow-all"
    policy_path = options.get("skill_hook_policy") or os.environ.get("WF_SKILL_HOOK_POLICY")
    if not policy_path:
        return False, "mode=policy but no policy path provided"
    policy = _load_skill_hook_policy(policy_path)
    allow = set(str(v) for v in policy.get("allow", []))
    keys = {
        f"{skill_name}:{hook_name}",
        f"{skill_name}:{hook_name}:{interpreter}",
    }
    if any(k in allow for k in keys):
        return True, f"allowed by policy '{policy_path}'"
    return False, f"hook '{skill_name}:{hook_name}' not listed in policy '{policy_path}'"


def _resolve_skill_hook_script(
    skill_name: str, hook_name: str, plan: Any
) -> tuple[Path, str] | None:
    """Resolve a skill hook script by convention under `<skill>/scripts`."""

    _validate_skill_name(skill_name)
    _validate_skill_name(hook_name)
    skill_dir = _resolve_skill_dir(skill_name, plan)
    if skill_dir is None:
        return None
    return _skill_hook_script_in(skill_dir, hook_name)


def _skill_hook_script_in(skill_dir: Path, hook_name: str) -> tuple[Path, str] | None:
    """A hook's script in a skill's directory: `scripts/<hook>.py`, else
    `scripts/<hook>.sh`. `hook_name` is validated already."""

    scripts_root = (skill_dir / "scripts").resolve()
    py = (scripts_root / f"{hook_name}.py").resolve()
    sh = (scripts_root / f"{hook_name}.sh").resolve()
    if py.exists() and py.is_file():
        return py, "python"
    if sh.exists() and sh.is_file():
        return sh, "sh"
    return None


def _run_skill_hook(
    *,
    skill_name: str,
    hook_name: str,
    phase: str,
    actor_name: str,
    payload: dict[str, Any],
    response_text: str | None,
    plan: Any,
    timeout_ms: int,
) -> dict[str, Any]:
    """Execute one skill hook script and return debug metadata."""

    resolved = _resolve_skill_hook_script(skill_name, hook_name, plan)
    if resolved is None:
        return {
            "ran": False,
            "reason": "script-not-found",
            "phase": phase,
            "hook": hook_name,
        }
    script_path, interpreter = resolved
    allowed, reason = _authorize_skill_hook(plan.options, skill_name, hook_name, interpreter)
    if not allowed:
        return {
            "ran": False,
            "reason": f"denied: {reason}",
            "phase": phase,
            "hook": hook_name,
            "script": str(script_path),
            "interpreter": interpreter,
        }

    env = os.environ.copy()
    env.update(
        {
            "WF_SKILL_NAME": skill_name,
            "WF_SKILL_HOOK": hook_name,
            "WF_SKILL_HOOK_PHASE": phase,
            "WF_AGENT_INSTANCE": actor_name,
        }
    )
    cwd = str(_runtime_root_for_skills(plan))
    proc_input = json.dumps(
        {
            "phase": phase,
            "skill": skill_name,
            "hook": hook_name,
            "instance": actor_name,
            "payload": payload,
            "response": response_text,
        },
        default=str,
    )

    cmd = ["python3", str(script_path)] if interpreter == "python" else ["bash", str(script_path)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            input=proc_input,
            timeout=timeout_ms / 1000,
        )
        return {
            "ran": True,
            "phase": phase,
            "hook": hook_name,
            "script": str(script_path),
            "interpreter": interpreter,
            "exitCode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "authorized": reason,
        }
    except subprocess.TimeoutExpired:
        return {
            "ran": True,
            "phase": phase,
            "hook": hook_name,
            "script": str(script_path),
            "interpreter": interpreter,
            "timeoutMs": timeout_ms,
            "error": "timeout",
            "authorized": reason,
        }


def _skill_hook_name(skill_meta: dict[str, Any], phase: str) -> str | None:
    """Extract hook name (`pre`/`post`) from parsed SKILL.md metadata."""

    hooks = skill_meta.get("hooks")
    if not isinstance(hooks, dict):
        return None
    value = hooks.get(phase)
    if value is None:
        return None
    name = str(value).strip()
    return name or None


def _build_effective_agent_prompt(spec: AgentSpec, plan: Any) -> tuple[str, dict[str, Any]]:
    """Build final user/system instruction for an agent from skill + prompt toggles."""

    parts: list[str] = []
    debug_meta: dict[str, Any] = {
        "claudeAgentEnabled": bool(spec.use_claude_agent),
        "claudeAgent": spec.claude_agent,
        "skillEnabled": bool(spec.use_skill),
        "promptEnabled": bool(spec.use_prompt),
        "hooksEnabled": bool(spec.use_skill_hooks),
        "skill": spec.skill,
    }

    if spec.use_claude_agent:
        claude_agent_name = (spec.claude_agent or "").strip()
        if not claude_agent_name:
            raise ValueError("Agent has use_claude_agent=true but no claude_agent name configured.")
        profile = _load_claude_agent_profile(claude_agent_name, plan)
        body = str(profile.get("body", "")).strip()
        if body:
            parts.append(body)
        for preloaded_skill in profile.get("skills", []):
            try:
                skill_text, skill_path = _read_skill_md(str(preloaded_skill), plan)
                _skill_meta, skill_body = _parse_skill_frontmatter(skill_text)
                skill_body = _rewrite_skill_body_paths(skill_body, skill_path.parent)
                if skill_body.strip():
                    parts.append(skill_body.strip())
            except Exception as exc:  # noqa: BLE001
                warnings_val = profile.get("warnings")
                warnings: list[Any] = warnings_val if isinstance(warnings_val, list) else []
                warnings.append(f"failed to preload claude-agent skill '{preloaded_skill}': {exc}")
                profile["warnings"] = warnings
        debug_meta["claudeAgentProfile"] = profile

    # `use_skill` means "include the configured skill", so with no skill named
    # there is simply nothing to include. It defaults to True, so raising here
    # would make the bare `@agent(prompt=...)` form unusable. A skill that IS
    # named but cannot be read still raises, from _read_skill_md.
    skill_name = (spec.skill or "").strip()
    if spec.use_skill and skill_name:
        skill_text, skill_path = _read_skill_md(skill_name, plan)
        skill_meta, skill_body = _parse_skill_frontmatter(skill_text)
        skill_dir = skill_path.parent
        skill_body = _rewrite_skill_body_paths(skill_body, skill_dir)
        parts.append(skill_body)
        debug_meta["skillPath"] = str(skill_path)
        debug_meta["skillDir"] = str(skill_dir)
        debug_meta["skillMeta"] = skill_meta

    if spec.use_prompt:
        prompt_text = (spec.prompt or "").strip()
        if prompt_text:
            parts.append(prompt_text)

    if not parts:
        raise ValueError(
            "Agent has no enabled instruction sources. "
            "Enable at least one of: use_claude_agent, use_skill, use_prompt."
        )

    return "\n\n".join(parts), debug_meta
