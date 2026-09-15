"""ACP connectors: the agents wfpy can spawn over the Agent Client Protocol.

A connector is a named ACP agent: its command line, what it can do, and its
defaults. The set a run sees is, in order of precedence:

1. what wfpy DISCOVERS: the known agents whose binary is on the PATH
   (``opencode acp``, Zed's ``claude-agent-acp`` for Claude Code,
   ``codex-acp``, ``gemini --experimental-acp``);
2. the USER's file, ``$XDG_CONFIG_HOME/wfpy/connectors.toml``
   (``~/.config/wfpy/connectors.toml``), adding connectors or overriding the
   discovered ones;
3. the WORKSPACE's file, ``.wfpy/connectors.toml`` at the project root (the
   directory with a ``pyproject.toml`` or ``.git`` above the workflow file),
   overriding both, so a workflow checked into a repository names a
   connector without carrying a path.

The file, one table per connector::

    [connectors.claude]
    command = "claude-agent-acp"      # argv, split as a shell would
    model = "sonnet"                  # a session config option the agent offers
    mode = "acceptEdits"              # a session mode the agent offers
    http_api = false                  # OpenCode's HTTP API beside ACP

    [connectors.opencode]
    command = "/opt/opencode/bin/opencode acp"

A connector is AVAILABLE when its command's first word resolves on the PATH.
``wfpy connectors`` lists them; an agent names one with
``@agent(transport="acp", connector="claude")``; a run's default is
``--acp-connector NAME``; an explicit ``--agent-cli-acp-command`` argv wins
over both, as before.
"""

from __future__ import annotations

import dataclasses
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10: the standard library has no tomllib
    import tomli as tomllib

#: The agents wfpy knows how to spawn without being told.
KNOWN: dict[str, dict[str, Any]] = {
    "opencode": {"command": "opencode acp", "http_api": True},
    "claude": {"command": "claude-agent-acp", "http_api": False},
    "codex": {"command": "codex-acp", "http_api": False},
    "gemini": {"command": "gemini --experimental-acp", "http_api": False},
}

DEFAULT_CONNECTOR = "opencode"


@dataclasses.dataclass
class Connector:
    """One ACP agent as wfpy would spawn it."""

    name: str
    argv: list[str]
    source: str = "discovered"        # discovered | user | workspace
    available: bool = False           # argv[0] resolves on the PATH
    http_api: bool = False            # OpenCode's HTTP API beside ACP
    model: str | None = None          # a session config option, if the agent offers one
    mode: str | None = None           # a session mode, if the agent offers one
    env: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def command(self) -> str:
        return shlex.join(self.argv)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "source": self.source,
            "available": self.available,
            "http_api": self.http_api,
            "model": self.model,
            "mode": self.mode,
        }


def user_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "wfpy" / "connectors.toml"


def workspace_root(start: str | Path | None) -> Path | None:
    """The project root above ``start``: the first directory up with a
    ``pyproject.toml`` or a ``.git``, else None."""
    if not start:
        return None
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() or (candidate / ".git").exists():
            return candidate
    return None


def workspace_file(root: Path | None) -> Path | None:
    return (root / ".wfpy" / "connectors.toml") if root else None


def _available(argv: list[str]) -> bool:
    return bool(argv) and shutil.which(argv[0]) is not None


def _from_table(name: str, table: dict[str, Any], source: str, base: Connector | None) -> Connector:
    command = table.get("command")
    argv = shlex.split(str(command)) if command else (list(base.argv) if base else [])
    if not argv:
        raise ValueError(f"connector '{name}' in the {source} file names no command")
    env = dict(base.env) if base else {}
    env.update({str(k): str(v) for k, v in (table.get("env") or {}).items()})
    return Connector(
        name=name,
        argv=argv,
        source=source,
        available=_available(argv),
        http_api=bool(table.get("http_api", base.http_api if base else False)),
        model=table.get("model", base.model if base else None),
        mode=table.get("mode", base.mode if base else None),
        env=env,
    )


def _read(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    with open(path, "rb") as fp:
        data = tomllib.load(fp)
    tables = data.get("connectors", {})
    if not isinstance(tables, dict):
        raise ValueError(f"{path}: `connectors` must be a table of tables")
    return {str(k): dict(v) for k, v in tables.items()}


def load_connectors(start: str | Path | None = None, *,
                    user: Path | None = None, workspace: Path | None = None) -> dict[str, Connector]:
    """Every connector a run starting at ``start`` sees: discovered, then the
    user's file, then the workspace's, later ones overriding earlier ones by
    name. ``user`` and ``workspace`` name the files explicitly (tests)."""
    found: dict[str, Connector] = {}
    for name, spec in KNOWN.items():
        argv = shlex.split(spec["command"])
        found[name] = Connector(name=name, argv=argv, source="discovered",
                                available=_available(argv), http_api=bool(spec.get("http_api")))
    user_path = user if user is not None else user_file()
    for name, table in _read(user_path).items():
        found[name] = _from_table(name, table, "user", found.get(name))
    ws_path = workspace if workspace is not None else workspace_file(workspace_root(start))
    for name, table in _read(ws_path).items():
        found[name] = _from_table(name, table, "workspace", found.get(name))
    return found


def resolve_connector(name: str, connectors: dict[str, Connector]) -> Connector:
    """The connector by name, available or not: a named agent that is not on
    the PATH fails when spawned, with its command in the message."""
    try:
        return connectors[name]
    except KeyError:
        known = ", ".join(sorted(connectors)) or "none"
        raise ValueError(f"Unknown ACP connector '{name}'. Connectors: {known}. "
                         f"Declare one in {user_file()} or the workspace's .wfpy/connectors.toml.") from None


def describe_all(connectors: dict[str, Connector]) -> list[dict[str, Any]]:
    return [c.describe() for c in sorted(connectors.values(), key=lambda c: c.name)]
