"""Codex outside a git repository: the firing's error says what to do.

`codex exec` runs only in a git repository or a directory Codex trusts.
Elsewhere it ends at once, before any model turn, with

    Not inside a trusted directory and --skip-git-repo-check was not specified.

That names an option of Codex's own command line, which a person who runs
Codex through wfpy does not write. wfpy keeps Codex's guard as it is (it
does not add the option by itself) and says how to pass it.
"""

from __future__ import annotations

import pytest

from wfpy._agent_cli_runtime import _codex_untrusted_directory_hint, _invoke_agent_codex_cli
from wfpy.core import AgentSpec

# What Codex 0.172.0 writes to stderr, started in a folder that is no repository.
REFUSAL = (
    "Reading additional input from stdin...\n"
    "Not inside a trusted directory and --skip-git-repo-check was not specified.\n"
)


def fire(monkeypatch, *, returncode: int, stdout: str = "", stderr: str = "", **options):
    seen: list[list[str]] = []

    class Proc:
        pass

    Proc.returncode = returncode
    Proc.stdout = stdout
    Proc.stderr = stderr

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return Proc()

    monkeypatch.setattr("wfpy._agent_cli_runtime.subprocess.run", fake_run)
    result = _invoke_agent_codex_cli(
        AgentSpec(prompt="x", transport="codex-cli"),
        payload_text='{"inputs": {}}',
        verbose=False,
        effective_prompt="do task",
        plan_options=options,
    )
    return result, seen


def test_the_refusal_comes_with_the_way_to_pass_codex_s_option(monkeypatch):
    (_, _, error, _), _ = fire(monkeypatch, returncode=1, stderr=REFUSAL)
    said = str(error)
    assert "Not inside a trusted directory" in said             # Codex's own words, kept
    assert "--agent-cli-codex-args=--skip-git-repo-check" in said
    assert 'agent_cli_codex_args="--skip-git-repo-check"' in said
    assert "inside the project's repository" in said            # the other way out


def test_wfpy_does_not_add_the_option_by_itself(monkeypatch):
    """Codex's guard is Codex's: lifting it is said, not done."""
    _, seen = fire(monkeypatch, returncode=1, stderr=REFUSAL)
    assert "--skip-git-repo-check" not in seen[0]


def test_the_option_given_reaches_codex(monkeypatch):
    (_, _, error, _), seen = fire(
        monkeypatch, returncode=0, stdout='{"outputs": {"out": "ok"}}',
        agent_cli_codex_subcommand="exec", agent_cli_codex_args="--skip-git-repo-check")
    assert error is None
    assert seen[0][:3] == ["codex", "exec", "--skip-git-repo-check"]


def test_the_command_line_the_hint_gives_is_one_the_cli_takes():
    """`--agent-cli-codex-args "--skip-git-repo-check"`, with a space, is
    refused by the argument parser ("expected one argument"): the value
    starts with `--`. The hint was first written so, and a run showed it."""
    import subprocess
    import sys

    def parse(*args: str) -> subprocess.CompletedProcess[str]:
        # A file that does not exist: the arguments are parsed before it is read.
        return subprocess.run(
            [sys.executable, "-m", "wfpy.cli", "run", "no-such-workflow.py", *args],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)

    with_equals = parse("--agent-cli-codex-args=--skip-git-repo-check")
    assert "expected one argument" not in with_equals.stderr
    with_space = parse("--agent-cli-codex-args", "--skip-git-repo-check")
    assert "expected one argument" in with_space.stderr


@pytest.mark.parametrize("stderr", [
    "error: unexpected argument '--nope' found",
    "stream disconnected before completion",
    "",
])
def test_another_failure_is_reported_as_it_was(monkeypatch, stderr):
    (_, _, error, _), _ = fire(monkeypatch, returncode=2, stderr=stderr)
    said = str(error)
    assert said == f"Codex CLI invocation failed: {stderr or 'exit code 2'}"
    assert _codex_untrusted_directory_hint(stderr) == ""
