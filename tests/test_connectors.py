"""ACP connectors: discovered on the PATH, declared by the user, overridden
by the workspace; listed by `wfpy connectors`; resolved by name."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from wfpy import connectors as C


@pytest.fixture
def path_with(monkeypatch):
    """A PATH on which only the named binaries resolve."""

    def make(*names):
        monkeypatch.setattr(C.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd in names else None)

    return make


class TestDiscovery:
    def test_known_agents_on_the_path_are_available(self, path_with, tmp_path):
        path_with("opencode", "claude-agent-acp")
        found = C.load_connectors(user=tmp_path / "none.toml", workspace=tmp_path / "none2.toml")
        assert found["opencode"].available and found["opencode"].argv == ["opencode", "acp"]
        assert found["claude"].available and found["claude"].argv == ["claude-agent-acp"]
        assert not found["codex"].available
        assert found["opencode"].http_api and not found["claude"].http_api
        assert all(c.source == "discovered" for c in found.values())

    def test_the_default_connector_is_opencode(self):
        assert C.DEFAULT_CONNECTOR == "opencode"


class TestTheFiles:
    def test_the_user_declares_and_overrides(self, path_with, tmp_path):
        path_with("claude-agent-acp", "my-agent")
        user = tmp_path / "connectors.toml"
        user.write_text('[connectors.claude]\nmodel = "sonnet"\nmode = "acceptEdits"\n'
                        '[connectors.mine]\ncommand = "my-agent --acp"\nenv = { KEY = "v" }\n')
        found = C.load_connectors(user=user, workspace=tmp_path / "none.toml")
        claude = found["claude"]
        assert claude.source == "user" and claude.argv == ["claude-agent-acp"]   # the command kept
        assert claude.model == "sonnet" and claude.mode == "acceptEdits" and claude.available
        mine = found["mine"]
        assert mine.argv == ["my-agent", "--acp"] and mine.env == {"KEY": "v"} and mine.available

    def test_the_workspace_overrides_the_user(self, path_with, tmp_path):
        path_with("opencode")
        user = tmp_path / "u.toml"; user.write_text('[connectors.claude]\nmodel = "sonnet"\n')
        ws = tmp_path / "w.toml"; ws.write_text('[connectors.claude]\nmodel = "haiku"\n')
        found = C.load_connectors(user=user, workspace=ws)
        assert found["claude"].model == "haiku" and found["claude"].source == "workspace"
        assert not found["claude"].available   # claude-agent-acp is not on this PATH

    def test_a_declared_connector_needs_a_command(self, tmp_path):
        user = tmp_path / "u.toml"; user.write_text('[connectors.ghost]\nmodel = "x"\n')
        with pytest.raises(ValueError, match="names no command"):
            C.load_connectors(user=user, workspace=tmp_path / "none.toml")

    def test_the_workspace_file_sits_at_the_project_root(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        deep = tmp_path / "a" / "b"; deep.mkdir(parents=True)
        assert C.workspace_root(deep / "flow.py") == tmp_path
        assert C.workspace_file(C.workspace_root(deep)) == tmp_path / ".wfpy" / "connectors.toml"
        assert C.workspace_root(None) is None

    def test_the_user_file_follows_xdg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert C.user_file() == tmp_path / "wfpy" / "connectors.toml"


class TestResolving:
    def test_by_name_or_a_message_naming_the_rest(self, path_with, tmp_path):
        path_with()
        found = C.load_connectors(user=tmp_path / "n.toml", workspace=tmp_path / "n2.toml")
        assert C.resolve_connector("claude", found).name == "claude"
        with pytest.raises(ValueError, match="Unknown ACP connector 'zed'.*claude.*opencode"):
            C.resolve_connector("zed", found)

    def test_describe_is_what_the_cli_prints(self, path_with, tmp_path):
        path_with("opencode")
        rows = C.describe_all(C.load_connectors(user=tmp_path / "n.toml", workspace=tmp_path / "n2.toml"))
        assert [r["name"] for r in rows] == ["claude", "codex", "gemini", "opencode"]
        assert rows[3] == {"name": "opencode", "command": "opencode acp", "source": "discovered",
                           "available": True, "http_api": True, "model": None, "mode": None,
                           "stuck_timeout_ms": None, "stuck_retries": None}


class TestTheStuckWindow:
    """How long a connector's agent may stream nothing before it is nudged.

    An agent that streams nothing during a long tool call — OpenCode during a
    long `bash` is the measured case — looks stuck to the detector, so a
    workflow whose agents compile or run on a device has to widen the window.
    """

    def test_a_file_sets_the_window_and_the_budget(self, path_with, tmp_path):
        path_with("opencode")
        user = tmp_path / "u.toml"
        user.write_text(
            "[connectors.opencode]\n"
            "stuck_timeout_ms = 1800000\n"
            "stuck_retries = 2\n"
        )
        found = C.load_connectors(user=user, workspace=tmp_path / "n2.toml")
        assert found["opencode"].stuck_timeout_ms == 1_800_000
        assert found["opencode"].stuck_retries == 2
        # Set without a command, the discovered command is kept.
        assert found["opencode"].argv == ["opencode", "acp"]

    def test_unset_stays_none_so_the_default_applies(self, path_with, tmp_path):
        path_with("opencode")
        found = C.load_connectors(user=tmp_path / "n.toml", workspace=tmp_path / "n2.toml")
        assert found["opencode"].stuck_timeout_ms is None
        assert found["opencode"].stuck_retries is None

    def test_the_workspace_overrides_the_user_file(self, path_with, tmp_path):
        path_with("opencode")
        user = tmp_path / "u.toml"
        user.write_text("[connectors.opencode]\nstuck_timeout_ms = 600000\n")
        ws = tmp_path / "w.toml"
        ws.write_text("[connectors.opencode]\nstuck_timeout_ms = 900000\n")
        found = C.load_connectors(user=user, workspace=ws)
        assert found["opencode"].stuck_timeout_ms == 900_000

    def test_a_workspace_file_inherits_the_users_value(self, path_with, tmp_path):
        """A workspace table that names only a command keeps the user's window."""
        path_with("opencode")
        user = tmp_path / "u.toml"
        user.write_text("[connectors.opencode]\nstuck_retries = 7\n")
        ws = tmp_path / "w.toml"
        ws.write_text('[connectors.opencode]\ncommand = "/opt/oc acp"\n')
        found = C.load_connectors(user=user, workspace=ws)
        assert found["opencode"].stuck_retries == 7
        assert found["opencode"].argv == ["/opt/oc", "acp"]

    @pytest.mark.parametrize("value", ["0", "-1", '"300000"', "true", "1.5"])
    def test_a_value_that_is_not_a_positive_integer_is_named(self, path_with, tmp_path, value):
        """Silently falling back to the default would defeat the point of setting it."""
        path_with("opencode")
        user = tmp_path / "u.toml"
        user.write_text(f"[connectors.opencode]\nstuck_timeout_ms = {value}\n")
        with pytest.raises(ValueError, match="stuck_timeout_ms"):
            C.load_connectors(user=user, workspace=tmp_path / "n2.toml")


class TestTheCommand:
    def test_wfpy_connectors_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "wfpy").mkdir()
        (tmp_path / "wfpy" / "connectors.toml").write_text('[connectors.mine]\ncommand = "my-agent"\n')
        out = subprocess.run([sys.executable, "-m", "wfpy.cli", "connectors", "--workspace", str(tmp_path), "--json"],
                             capture_output=True, text=True, check=True,
                             env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        data = json.loads(out.stdout)
        names = {r["name"]: r for r in data["connectors"]}
        assert names["mine"]["command"] == "my-agent" and names["mine"]["source"] == "user"
        assert "opencode" in names and data["user_file"].endswith("wfpy/connectors.toml")


class TestTheRunResolvesTheWindow:
    """`_acp_stuck_policy`: the run's option, then the connector's, then the
    default — the same precedence as everything else about an ACP agent."""

    @staticmethod
    def _policy(options, connector=None):
        from wfpy._agent_cli_runtime import _acp_stuck_policy
        return _acp_stuck_policy(options, connector)

    def test_nothing_set_is_the_documented_default(self):
        assert self._policy({}) == (300, 5)
        assert (C.DEFAULT_STUCK_TIMEOUT_MS, C.DEFAULT_STUCK_RETRIES) == (300_000, 5)

    def test_the_connector_widens_the_window(self):
        conn = C.Connector(name="claude", argv=["claude-agent-acp"],
                           stuck_timeout_ms=1_800_000, stuck_retries=2)
        assert self._policy({}, conn) == (1800, 2)

    def test_the_runs_option_beats_the_connector(self):
        conn = C.Connector(name="claude", argv=["claude-agent-acp"],
                           stuck_timeout_ms=1_800_000, stuck_retries=2)
        assert self._policy({"acp_stuck_timeout_ms": 60_000,
                             "acp_stuck_retries": 9}, conn) == (60, 9)

    def test_an_unset_option_falls_through_to_the_connector(self):
        """`run()` passes None for a flag the user did not give."""
        conn = C.Connector(name="claude", argv=["claude-agent-acp"], stuck_timeout_ms=900_000)
        assert self._policy({"acp_stuck_timeout_ms": None, "acp_stuck_retries": None},
                            conn) == (900, 5)

    def test_a_sub_second_window_is_floored_at_one_second(self):
        """Rounding to 0 would nudge the agent continuously."""
        assert self._policy({"acp_stuck_timeout_ms": 1})[0] == 1

    def test_a_nonsense_option_is_named_not_ignored(self):
        with pytest.raises(ValueError, match="acp_stuck_timeout_ms"):
            self._policy({"acp_stuck_timeout_ms": "soon"})
        with pytest.raises(ValueError, match="acp_stuck_retries"):
            self._policy({"acp_stuck_retries": 0})
