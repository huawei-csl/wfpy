"""Internal helpers for agent prompt composition and skill loading."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NamedTuple

from wfpy.core import AgentSpec

logger = logging.getLogger("wfpy")

SKILL_MD_MAX_BYTES = 256_000
"""Maximum SKILL.md size accepted by the runtime loader."""

GIT_TIMEOUT_SECONDS = 10
"""How long the lookup waits for git to say which repository a workflow is in."""

GIT_REPOSITORY_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_INDEX_FILE",
    "GIT_GRAFT_FILE",
    "GIT_SHALLOW_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
)
"""The variables naming one repository's own parts, not passed on when the
lookup asks git which repository a workflow is in: those `git rev-parse
--local-env-vars` lists, which git clears itself before it runs a
command in a submodule. Its configuration ones (`GIT_CONFIG_PARAMETERS`,
`GIT_CONFIG_COUNT`) are passed on: they carry the user's choices, such
as the `safe.directory` a container sets that way."""

AGENT_MAX_TOOL_ROUNDS = 6
"""Maximum tool-call continuation rounds per single agent firing."""


def _explicit_skills_roots(cwd: Path | None = None) -> list[Path]:
    """Parse `WF_SKILLS_ROOT` into the roots it names, in priority order.

    The variable holds one directory or several separated by `os.pathsep`
    (`:` on POSIX, `;` on Windows), like `PATH`. A single directory — the
    only form the variable used to take — parses to a one-element list, so
    an existing setting keeps behaving exactly as before. That includes a
    directory whose own path holds the separator (`skills:v2`): a value that
    is itself a directory is that one directory, and is not split. A
    relative entry is read from `cwd`, the run's working directory (this
    process's when omitted).
    """

    raw = os.environ.get("WF_SKILLS_ROOT", "")
    whole = _from_cwd(Path(raw.strip()).expanduser(), cwd)
    if os.pathsep in raw and whole.is_dir():
        return [whole]
    roots: list[Path] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if entry:
            roots.append(_from_cwd(Path(entry).expanduser(), cwd))
    return roots


def _from_cwd(path: Path, cwd: Path | None) -> Path:
    """`path`, a relative one read from `cwd` when given."""

    return cwd / path if cwd is not None and not path.is_absolute() else path


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


class _Roots:
    """Lookup roots in the order added, each directory once.

    One rule places a user-level directory (`user_names`) that the walk or
    the working directory reaches as a project's: it is the user's. Among
    wfpy's own folders, which keep the order they always had, it is named
    the user's where it is reached. Among the other tools' folders (added
    after `others_from_here()`), it waits for its user-level place, after
    every project folder, as Codex ranks a user's skill after a
    repository's. A directory a variable names is taken as named.
    """

    def __init__(self, user_names: dict[Path, str]) -> None:
        self.roots: list[LookupRoot] = []
        self._seen: set[Path] = set()
        self._user_names = user_names
        self._others = False

    def others_from_here(self) -> None:
        """The directories added from now on are other tools' folders."""
        self._others = True

    def add(self, path: Path, folder: str, scope: str) -> None:
        """Add a resolved directory."""
        if path in self._seen:
            return
        if scope in ("project", "cwd") and path in self._user_names:
            if self._others:
                return  # the user's: added in its user-level place
            folder, scope = self._user_names[path], "user"
        self._seen.add(path)
        self.roots.append(LookupRoot(path, folder, scope))


def _xdg_config_home() -> Path | None:
    """`XDG_CONFIG_HOME`, if it names an absolute directory.

    A relative value is ignored, as the XDG Base Directory specification asks.
    """

    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else None


def _other_tool_user_skill_dirs(cwd: Path) -> list[tuple[Path, str]]:
    """Where other agent tools keep a user's skills, in the order searched,
    each with the name a reader knows it by.

    `~/.agents/skills` (the shared folder), Codex's `$CODEX_HOME/skills`
    (`~/.codex/skills` when `CODEX_HOME` is unset; a relative value is read
    from the working directory `cwd`, as Codex reads it) and
    `$CODEX_HOME/skills/.system`, where Codex installs the skills it ships
    and which it ranks after the user's, then OpenCode's
    `$XDG_CONFIG_HOME/opencode/skills` and `.../skill` (`~/.config` when
    `XDG_CONFIG_HOME` is unset or relative).

    `$CODEX_HOME` may name a project's `.codex`, as `CODEX_HOME=.codex` does
    started in the project. Codex then reads that folder as `CODEX_HOME`
    only, not as the project's, so it is searched here in the user-level
    place too (`_Roots`).
    """

    home = Path.home()
    raw_codex = os.environ.get("CODEX_HOME", "").strip()
    codex, codex_name = (
        (cwd / Path(raw_codex).expanduser(), "$CODEX_HOME")
        if raw_codex
        else (home / ".codex", "~/.codex")
    )
    config_home = _xdg_config_home()
    config, config_name = (
        (config_home, "$XDG_CONFIG_HOME") if config_home else (home / ".config", "~/.config")
    )
    return [
        (home / ".agents" / "skills", "~/.agents/skills"),
        (codex / "skills", f"{codex_name}/skills"),
        (codex / "skills" / ".system", f"{codex_name}/skills/.system"),
        (config / "opencode" / "skills", f"{config_name}/opencode/skills"),
        (config / "opencode" / "skill", f"{config_name}/opencode/skill"),
    ]


def _repository_root(start: Path) -> Path | None:
    """The top of the git work tree `start` is in, as git itself finds it
    (`git -C <start> rev-parse --show-toplevel`); None when git finds none,
    refuses the one it finds, or cannot be run.

    git decides what a repository is: a `.git` directory, file (a linked
    worktree, a submodule) or link that it accepts, owned by someone it
    trusts. It refuses a repository of dubious ownership, such as a
    `/tmp/.git` another user planted, unless the user lists it in
    `safe.directory`, as containers and CI already do for their checkouts.
    `GIT_DIR`, `GIT_WORK_TREE` and the other `GIT_REPOSITORY_VARIABLES` are
    not passed on: a git hook, or a push git holds in quarantine, exports
    its own repository's, and the question is which repository the
    workflow's directory is in, not which one the environment names. A git
    that does not answer within `GIT_TIMEOUT_SECONDS` is said in the log.

    `cli._discover_project_root` answers another question: it also stops at
    a `pyproject.toml`. The folders bounded here are those of Codex and
    OpenCode, which stop at the git repository.
    """

    env = {key: value for key, value in os.environ.items() if key not in GIT_REPOSITORY_VARIABLES}
    try:
        found = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "git did not say within %s s which repository %s is in: the other agent"
            " tools' project folders are searched there alone",
            GIT_TIMEOUT_SECONDS,
            start,
        )
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    top = os.fsdecode(found.stdout).rstrip("\r\n")
    if found.returncode != 0 or not top:
        return None
    try:
        root = Path(top).resolve()
    except (OSError, RuntimeError):
        return None
    return root if root == start or root in start.parents else None


def _project_levels(start: Path) -> tuple[list[Path], Path | None]:
    """The directories whose project folders of other tools are searched,
    and the repository: from `start` up to the repository root
    (`_repository_root`), included; `start` alone outside a repository."""

    repository = _repository_root(start)
    if repository is None:
        return [start], None
    levels = [start, *start.parents]
    return levels[: levels.index(repository) + 1], repository


def _workflow_dir(plan: Any, cwd: Path | None = None) -> Path:
    """The workflow's directory: its source file's, else the plan's work
    directory, else the working directory. A relative path is read from
    `cwd`, the run's working directory (this process's when omitted)."""

    if plan.source_path:
        try:
            return _from_cwd(Path(plan.source_path), cwd).resolve().parent
        except Exception:
            pass
    if plan.work_dir:
        try:
            return _from_cwd(Path(plan.work_dir), cwd).resolve()
        except Exception:
            pass
    return (cwd or Path.cwd()).resolve()


def _runtime_root_for_skills(plan: Any, cwd: Path | None = None) -> Path:
    """Resolve the runtime root used for `.wf/skills` discovery."""

    explicit = _explicit_skills_roots(cwd)
    if explicit:
        # The runtime root is a single directory — it is the base of the
        # ancestor walk in `_skill_search_roots()` and the working directory
        # of a skill hook — so when `WF_SKILLS_ROOT` names several, the first
        # (highest-priority) one is it. Every root it names is still searched
        # directly by `_skill_search_roots()`.
        #
        # The first one that exists, that is. A list names the folders of
        # several tools and a project seldom has them all; a hook cannot run
        # in a directory that is not there (`subprocess` raises), and the walk
        # from one finds nothing. With none present it is the first, as a
        # single missing directory always was.
        present = next((root for root in explicit if root.is_dir()), explicit[0])
        return present.resolve()
    return _workflow_dir(plan, cwd)


def _runtime_root_for_claude_agents(plan: Any, cwd: Path | None = None) -> Path:
    """Resolve runtime root used for `.claude/agents` discovery."""

    explicit = os.environ.get("WF_CLAUDE_AGENTS_ROOT", "").strip()
    if explicit:
        return _from_cwd(Path(explicit).expanduser(), cwd).resolve()
    return _runtime_root_for_skills(plan, cwd)


LOOKUP_VARIABLES = ("WF_SKILLS_ROOT", "WF_CLAUDE_AGENTS_ROOT", "CODEX_HOME", "XDG_CONFIG_HOME")
"""The environment variables the lookup roots depend on."""

_ROOTS_ON_PLAN = "_wfpy_lookup_roots"

_ROOTS_LOCK = threading.Lock()


class _Once:
    """Lookup roots worked out by the first caller; the others wait for them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._roots: tuple[LookupRoot, ...] | None = None

    def get(self, compute: Callable[[], list[LookupRoot]]) -> list[LookupRoot]:
        with self._lock:
            if self._roots is None:
                self._roots = tuple(compute())
            return list(self._roots)


def _once_per_plan(
    plan: Any, kind: str, cwd_root: Path, compute: Callable[[], list[LookupRoot]]
) -> list[LookupRoot]:
    """`compute()`, worked out once per plan and kept on it.

    A firing reads a skill, its hooks and an agent preset, each through the
    lookup roots, and building them asks git and walks the tree. They are
    built once per plan instead. Parallel workers share the plan: the first
    to ask builds them, and the others wait for that answer rather than run
    git too.

    What is kept is the list of folders, in order, and so the repository git
    found when it was built. It is built again when something it is built
    from differs: the working directory, the home directory, the variables
    in `LOOKUP_VARIABLES`, the workflow's file or work directory. It is not
    built again when the filesystem changes while the plan runs: a
    repository made, removed or moved, or a folder turned into a link, is
    seen by the next plan. What the folders hold is not kept: every folder
    is listed whether it exists or not, and a name is looked for in them at
    each firing, so a skill folder made while a run goes on is found.
    `wfpy skills` keeps nothing: each call builds the list again.
    """

    key = (
        kind,
        cwd_root,
        Path.home(),
        plan.source_path,
        plan.work_dir,
        *(os.environ.get(name) for name in LOOKUP_VARIABLES),
    )
    with _ROOTS_LOCK:
        kept: dict[tuple[Any, ...], _Once] | None = getattr(plan, _ROOTS_ON_PLAN, None)
        if not isinstance(kept, dict):
            kept = {}
            try:
                setattr(plan, _ROOTS_ON_PLAN, kept)
            except (AttributeError, TypeError):
                kept = None
        once = kept.setdefault(key, _Once()) if kept is not None else None
    if once is None:
        return compute()
    return once.get(compute)


def _skill_lookup_roots(plan: Any, cwd: Path | None = None) -> list[LookupRoot]:
    """The directories a skill name is looked up in, in order.

    Supported roots:
    - `<runtime-root>/.wf/skills`
    - `<runtime-root>/.claude/skills`
    - `<cwd>/.wf/skills`
    - `<cwd>/.claude/skills`
    - `~/.claude/skills`

    then the skill folders of other agent tools, after every root above:
    - `<workflow-dir>/<folder>` for each of `OTHER_TOOL_PROJECT_SKILL_DIRS`
    - `<cwd>/<folder>` for each of them, when `<cwd>` is in the same project
    - the user-level ones of `_other_tool_user_skill_dirs()`

    The `<runtime-root>` entries of wfpy's own folders walk up to the
    filesystem root. Those of the other tools start at the workflow's own
    directory, `WF_SKILLS_ROOT` or not, and walk up only as far as the tools
    themselves do: to the top of the git repository (`_repository_root`),
    included. With no repository git accepts, only the workflow's
    directory. So a folder above the project, say `/tmp/.agents/skills` for
    a workflow in `/tmp/job`, is not read. `<cwd>`'s folders of other tools
    are read only when `<cwd>` is in that repository.

    A user-level folder reached by the walk or at `<cwd>`, as in a home
    directory kept in git, is searched once, in the user-level place; the
    home directory's other project folders (`~/.opencode/skills`) are a
    project's, as they are for OpenCode.

    Searching the other tools' folders last means a name found in one of
    wfpy's own folders resolves exactly as it did before they were searched;
    they only add names.

    If `WF_SKILLS_ROOT` is set, each directory it names is treated as a
    highest-priority directory that directly contains skill folders. It may
    name several, separated by `os.pathsep`, searched in the order written.

    `cwd` is the working directory of the run; this process's when omitted.
    The roots are worked out once per plan (`_once_per_plan`).
    """

    cwd_root = (cwd or Path.cwd()).resolve()
    return _once_per_plan(
        plan, "skills", cwd_root, lambda: _build_skill_lookup_roots(plan, cwd_root)[0]
    )


def _build_skill_lookup_roots(plan: Any, cwd_root: Path) -> tuple[list[LookupRoot], Path | None]:
    """`_skill_lookup_roots`, worked out, and the repository that bounds the
    other tools' project folders (`_repository_root`), or None."""

    home_root = Path.home().resolve()
    user_tool_dirs = [
        (path.resolve(), name) for path, name in _other_tool_user_skill_dirs(cwd_root)
    ]
    user_names = {path: name for path, name in user_tool_dirs}
    user_names[(home_root / ".claude" / "skills").resolve()] = "~/.claude/skills"
    roots = _Roots(user_names)

    for explicit in _explicit_skills_roots(cwd_root):
        roots.add(explicit.resolve(), "WF_SKILLS_ROOT", "explicit")

    runtime_root = _runtime_root_for_skills(plan, cwd_root)
    for level in (runtime_root, *runtime_root.parents):
        roots.add((level / ".wf" / "skills").resolve(), ".wf/skills", "project")
        roots.add((level / ".claude" / "skills").resolve(), ".claude/skills", "project")

    roots.add((cwd_root / ".wf" / "skills").resolve(), ".wf/skills", "cwd")
    roots.add((cwd_root / ".claude" / "skills").resolve(), ".claude/skills", "cwd")
    roots.add((home_root / ".claude" / "skills").resolve(), "~/.claude/skills", "user")

    # Other agent tools' folders come after every root above, so that a
    # skill already found keeps resolving to the same directory.
    roots.others_from_here()
    levels, repository = _project_levels(_workflow_dir(plan, cwd_root))
    places = [(level, "project") for level in levels]
    if repository is not None and (cwd_root == repository or repository in cwd_root.parents):
        places.append((cwd_root, "cwd"))
    for level, scope in places:
        for parts in OTHER_TOOL_PROJECT_SKILL_DIRS:
            roots.add(level.joinpath(*parts).resolve(), "/".join(parts), scope)
    for path, name in user_tool_dirs:
        roots.add(path, name, "user")

    return roots.roots, repository


def _skill_search_roots(plan: Any, cwd: Path | None = None) -> list[Path]:
    """Return ordered directories that can contain skill folders.

    See `_skill_lookup_roots` for which, in which order.
    """

    return [root.path for root in _skill_lookup_roots(plan, cwd)]


def _claude_agent_lookup_roots(plan: Any, cwd: Path | None = None) -> list[LookupRoot]:
    """The directories an agent preset is looked up in, in order: the one
    `WF_CLAUDE_AGENTS_ROOT` names, `.claude/agents` from its runtime root up
    to the filesystem root, then the working directory's and the user's.
    Worked out once per plan (`_once_per_plan`)."""

    cwd_root = (cwd or Path.cwd()).resolve()
    return _once_per_plan(
        plan, "presets", cwd_root, lambda: _build_claude_agent_lookup_roots(plan, cwd_root)
    )


def _build_claude_agent_lookup_roots(plan: Any, cwd_root: Path) -> list[LookupRoot]:
    """`_claude_agent_lookup_roots`, worked out."""

    home_agents = (Path.home().resolve() / ".claude" / "agents").resolve()
    roots = _Roots({home_agents: "~/.claude/agents"})

    explicit = os.environ.get("WF_CLAUDE_AGENTS_ROOT", "").strip()
    if explicit:
        roots.add(
            _from_cwd(Path(explicit).expanduser(), cwd_root).resolve(),
            "WF_CLAUDE_AGENTS_ROOT",
            "explicit",
        )

    runtime_root = _runtime_root_for_claude_agents(plan, cwd_root)
    for level in (runtime_root, *runtime_root.parents):
        roots.add((level / ".claude" / "agents").resolve(), ".claude/agents", "project")

    roots.add((cwd_root / ".claude" / "agents").resolve(), ".claude/agents", "cwd")
    roots.add(home_agents, "~/.claude/agents", "user")
    return roots.roots


def _claude_agent_search_roots(plan: Any, cwd: Path | None = None) -> list[Path]:
    """Return ordered directories that can contain Claude agent markdown files."""

    return [root.path for root in _claude_agent_lookup_roots(plan, cwd)]


def _validate_skill_name(skill_name: str, what: str = "Skill name") -> None:
    """Validate user-provided skill names used for filesystem lookup.

    The same rule holds for an agent preset's name and a hook's, which name
    a file or folder the same way; `what` names the kind in the error. A
    name that starts with `.` names a hidden entry, which is no skill: the
    `.git` of a skills folder kept in git, or Codex's `.system`, a root of
    its own; `.` and `..` name a folder of the walk itself. Codex and
    Claude Code skip hidden entries, `wfpy skills` does not list them, and
    so a run does not take one either.

    A name is taken without the spaces around it everywhere a run reads one
    (`spec.skill`, `claude_agent`, a preset's `skills`, a hook in
    `SKILL.md`), so a folder whose name has them is no name a run can
    resolve, and this refuses it: what `wfpy skills` offers is what a run
    takes.
    """

    if not skill_name or not skill_name.strip():
        raise ValueError(f"{what} is empty.")
    if skill_name != skill_name.strip():
        raise ValueError(
            f"{what} must not begin or end with a space: '{skill_name}' is read without"
            " them, so it names no file a run finds."
        )
    if "/" in skill_name or "\\" in skill_name:
        raise ValueError(f"{what} must not include path separators.")
    if ".." in skill_name:
        raise ValueError(f"{what} must not include '..'.")
    if skill_name.startswith("."):
        raise ValueError(
            f"{what} must not start with '.': '{skill_name}' names a hidden entry, or a"
            " folder of the search itself, which is no skill, preset or hook."
        )


def _validate_claude_agent_name(agent_name: str) -> None:
    """Validate Claude agent names used for filesystem lookup."""

    _validate_skill_name(agent_name, "Agent preset name")


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
        # already rejects path separators, `..` and a leading `.` (so `.`
        # too), so `root / skill_name` is a direct child of `root`. What
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


def _lookup_skill(skill_name: str, plan: Any) -> tuple[list[Path], tuple[Path, Path] | None]:
    """The roots a skill name is looked up in, and what `_find_skill` finds
    there."""

    _validate_skill_name(skill_name)
    roots = _skill_search_roots(plan)
    return roots, _find_skill(skill_name, roots)


def _resolve_skill_dir(skill_name: str, plan: Any) -> Path | None:
    """Resolve the skill directory for a skill name from supported roots."""

    found = _lookup_skill(skill_name, plan)[1]
    return found[1] if found is not None else None


def _resolve_skill_md_path(skill_name: str, plan: Any) -> Path:
    """Resolve `SKILL.md` for a skill name from supported roots."""

    roots, found = _lookup_skill(skill_name, plan)
    if found is None:
        raise FileNotFoundError(
            f"Skill '{skill_name}' was not found. Searched roots: "
            + ", ".join(str(root) for root in roots)
        )
    return (found[1] / "SKILL.md").resolve()


def _read_capped_text(path: Path, what: str, limit: int) -> str:
    """Read `path`, checked with one `stat`: a file within `limit` bytes, in
    UTF-8. `what` names it in an error: `Skill 'review'`, `Agent preset 'x'`."""

    try:
        status = path.stat()
    except FileNotFoundError:
        raise FileNotFoundError(f"{what}: {path} does not exist.") from None
    if not stat.S_ISREG(status.st_mode):
        # The lookup took the first folder holding it, so it exists and is no
        # file: a directory, for instance.
        raise FileNotFoundError(f"{what}: {path} is not a file.")
    if status.st_size > limit:
        raise ValueError(f"{what} is too large ({status.st_size} bytes). Limit is {limit} bytes.")
    return path.read_text(encoding="utf-8")


def _read_skill_md_file(skill_name: str, skill_md: Path) -> str:
    """Read a skill's `SKILL.md`: a file within `SKILL_MD_MAX_BYTES`, in UTF-8."""

    return _read_capped_text(skill_md, f"Skill '{skill_name}'", SKILL_MD_MAX_BYTES)


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


FRONTMATTER_TEXT_KEYS: tuple[str, ...] = ("name", "description")
"""Front-matter keys of an agent preset whose value is text, commas and all."""


def _parse_frontmatter_markdown(text: str) -> tuple[dict[str, Any], str]:
    """Parse generic markdown frontmatter and return `(meta, body)`.

    This parser is intentionally minimal and supports flat keys, comma lists,
    and one-level nested maps for compatibility with Claude agent docs. A
    value with a comma is a list (`tools: Read, Grep`), except for the keys
    in `FRONTMATTER_TEXT_KEYS`, whose value is text.
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
        if "," in normalized and key not in FRONTMATTER_TEXT_KEYS:
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
    and that file, resolved. `agent_name` is validated already.

    As in `_find_skill`, containment is tested on the joined path, before
    resolving it: a preset installed as a link, to a file or to a folder
    holding `AGENT.md`, resolves outside its root and is still found.
    """

    for root in roots:
        for candidate in (root / f"{agent_name}.md", root / agent_name / "AGENT.md"):
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                return root, candidate.resolve()
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
    """Load Claude agent markdown profile and compatibility metadata. The
    file is read whole, whatever its size."""

    profile_path = _resolve_claude_agent_path(agent_name, plan)
    return _claude_agent_profile_from(
        agent_name, profile_path, profile_path.read_text(encoding="utf-8")
    )


def _claude_agent_profile_from(agent_name: str, profile_path: Path, raw: str) -> dict[str, Any]:
    """The Claude agent markdown profile `raw`, read from `profile_path`."""

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
    _validate_skill_name(hook_name, "Hook name")
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
