"""The CLI transports give their agent no stdin.

`claude --print` and `opencode run` read a stdin that is not a terminal to
its end before they answer, and `claude` adds what it read to the prompt.
wfpy passed its own stdin on to them. Started by a parent that keeps the
pipe open (an IDE, a supervisor, `something | wfpy run`), a firing waited
for that pipe, up to its timeout, and what was piped to wfpy became part of
the agent's question.

Measured on the real CLIs, a line then an open pipe of 25 s on wfpy's stdin:
`claude-cli` answered after 32.2 s with "Paris (banana)", the line asking
for the banana; `opencode-cli` answered after 35.1 s, against 4.6 s with
stdin closed.

Each stand-in below reads its stdin to the end, as the CLI does, and says
what it read. The run's stdin is an open pipe with a line in it.
"""

from __future__ import annotations

import contextlib
import os
import sys

from wfpy._agent_cli_runtime import (
    _VARIANT_FLAG_CACHE,
    _invoke_agent_claude_cli,
    _invoke_agent_opencode_cli,
)
from wfpy.core import AgentSpec


@contextlib.contextmanager
def an_open_pipe_on_stdin():
    """File descriptor 0 is the read end of a pipe that holds a line and is
    not closed: what a process started by an IDE has."""
    read_end, write_end = os.pipe()
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        os.write(write_end, b"a line piped to wfpy\n")
        yield
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)


def invoke(function, transport, **options):
    return function(
        AgentSpec(prompt="x", transport=transport, timeout_ms=20_000),
        payload_text='{"inputs": {}}',
        verbose=False,
        effective_prompt="do task",
        plan_options=options,
    )


def test_claude_cli_is_given_no_stdin(tmp_path):
    stand_in = tmp_path / "claude_stand_in.py"
    stand_in.write_text(
        "import json, sys\n"
        "piped = sys.stdin.read()\n"
        "print(json.dumps({'outputs': {'out': 'stdin was: ' + repr(piped)}}))\n"
    )
    with an_open_pipe_on_stdin():
        response, _, err, _ = invoke(
            _invoke_agent_claude_cli, "claude-cli",
            agent_cli_claude_command=f"{sys.executable} {stand_in}",
        )
    assert err is None                              # not "Claude CLI timed out"
    assert "stdin was: ''" in response


def test_opencode_cli_is_given_no_stdin(tmp_path):
    stand_in = tmp_path / "opencode_stand_in.py"
    stand_in.write_text(
        "import json, sys\n"
        "piped = sys.stdin.read()\n"
        "if '--help' in sys.argv:\n"
        "    print('opencode run [flags] --variant')\n"
        "    sys.exit(0)\n"
        "answer = json.dumps({'outputs': {'out': 'stdin was: ' + repr(piped)}})\n"
        "print(json.dumps({'type': 'text', 'part': {'text': answer}}))\n"
    )
    _VARIANT_FLAG_CACHE.clear()
    try:
        with an_open_pipe_on_stdin():
            response, _, err, _ = invoke(
                _invoke_agent_opencode_cli, "opencode-cli",
                agent_cli_opencode_command=f"{sys.executable} {stand_in}",
            )
    finally:
        _VARIANT_FLAG_CACHE.clear()
    assert err is None                              # not "OpenCode CLI timed out"
    assert "stdin was: ''" in response


def test_the_help_that_opencode_is_asked_for_is_given_no_stdin(tmp_path):
    """`run --help` is read once per command, to see how this OpenCode takes
    a reasoning level. It is a child of the run as the agent is, and is
    given the run's stdin no more than the agent. The stand-in answers by
    what it finds on its stdin, without waiting for its end."""
    from wfpy._agent_cli_runtime import _opencode_run_takes_variant

    stand_in = tmp_path / "opencode_help_stand_in.py"
    stand_in.write_text(
        "import os\n"
        "found = os.read(0, 100)\n"
        "print('--variant' if found == b'' else 'model#variant only')\n"
    )
    argv = (sys.executable, str(stand_in))
    _VARIANT_FLAG_CACHE.clear()
    try:
        with an_open_pipe_on_stdin():
            assert _opencode_run_takes_variant(argv, dict(os.environ)) is True
    finally:
        _VARIANT_FLAG_CACHE.clear()
