"""A failing tool says why: the error carries what the tool printed."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from wfpy import File, Port, connect, run, tool, workflow


def _one_tool_workflow(program: str, inherit_stdio: bool):
    @tool(cmd=sys.executable, args=["-c", program, "{in.In}"], inherit_stdio=inherit_stdio)
    class Fails:
        class Ports:
            In = Port[str](direction="in")
            Out = Port[File](direction="out", ext=".txt")

    @workflow(inputs={"In": str}, outputs={"Out": File(ext=".txt")})
    def one_tool():
        fails = Fails()
        connect("In", fails.In)
        connect(fails.Out, "Out")

    return one_tool


def test_a_streaming_tool_failure_reports_its_stderr(tmp_path: Path, capfd) -> None:
    program = (
        "import sys; print('working on it', flush=True); "
        "sys.stderr.write('error: the round did not tap this port\\n'); sys.exit(3)"
    )
    wf = _one_tool_workflow(program, inherit_stdio=True)

    with pytest.raises(RuntimeError, match="the round did not tap this port"):
        run(wf, inputs={"In": "x"}, out_dir=str(tmp_path / "wf-out"))

    # still streamed as it ran
    assert "working on it" in capfd.readouterr().out


def test_a_tool_that_reports_on_stdout_is_heard(tmp_path: Path) -> None:
    # yosys and export-rtl print their errors on stdout
    program = "import sys; print('ERROR: no such module Core_wrapper'); sys.exit(1)"
    wf = _one_tool_workflow(program, inherit_stdio=False)

    with pytest.raises(RuntimeError, match="no such module Core_wrapper"):
        run(wf, inputs={"In": "x"}, out_dir=str(tmp_path / "wf-out"))


def test_a_long_report_keeps_its_end(tmp_path: Path) -> None:
    program = (
        "import sys\n"
        "for i in range(500): print(f'line {i}', file=sys.stderr)\n"
        "sys.exit(2)"
    )
    wf = _one_tool_workflow(program, inherit_stdio=True)

    with pytest.raises(RuntimeError) as failure:
        run(wf, inputs={"In": "x"}, out_dir=str(tmp_path / "wf-out"))

    message = str(failure.value)
    assert "line 499" in message
    assert "line 0\n" not in message
