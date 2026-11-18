"""The live overlay holds steady for a reader polling it: written whole, gone only after the record.

The IDE polls ``run.wf-viewer.live.json`` every 400ms while a run writes it at
every firing. ``Path.write_text`` truncates first, so a read in between found the
file empty or cut short, and the run's state blinked off the diagram. And the
live file went at the end of a run before the run was in ``run-log.jsonl``, so
for a moment a reader found only the previous run.
"""

from __future__ import annotations

import json

import pytest

import wfpy._run_finalization as finalization
from wfpy import Port, action, connect, run, task, workflow
from wfpy._run_artifacts import _ViewerOverlayBase, _ViewerOverlayWriter, _write_text_atomic
from wfpy.runner import FifoPlan


def test_atomic_write_leaves_the_file_whole_and_no_sibling(tmp_path):
    target = tmp_path / "run.wf-viewer.live.json"
    _write_text_atomic(target, json.dumps({"n": 1}))
    _write_text_atomic(target, json.dumps({"n": 2}))
    assert json.loads(target.read_text()) == {"n": 2}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["run.wf-viewer.live.json"]


def test_live_overlay_writer_writes_atomically(tmp_path, make_actor):
    base = _ViewerOverlayBase(
        run_id="r", workflow_name="wf", source_path="/wf.py", started_at="t", out_dir="/out"
    )
    writer = _ViewerOverlayWriter(FifoPlan(name="wf"), base, tmp_path / "live.json")
    writer.mark_running()
    writer.set_active(make_actor("a"))
    written = json.loads((tmp_path / "live.json").read_text())
    assert written["active"][0]["entityInstanceName"] == "a"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["live.json"]


@task
class _Doubler:
    class Ports:
        In = Port[int]()
        Out = Port[int]()

    def action(self, x: int) -> int:
        return x * 2


@pytest.mark.parametrize("fails", [False, True])
def test_live_file_goes_only_after_the_run_is_in_the_log(tmp_path, monkeypatch, fails):
    @task
    class Boom:
        class Ports:
            In = Port[int]()
            Out = Port[int]()

        @action(consumes={"In": 1}, produces={"Out": 1})
        def explode(self, x: int) -> int:
            raise RuntimeError("boom")

    @workflow(inputs={"In": int}, outputs={"Out": int})
    def wf():
        node = Boom() if fails else _Doubler()
        connect("In", node.In)
        connect(node.Out, "Out")

    live_path = tmp_path / "run.wf-viewer.live.json"
    seen: list[bool] = []
    original = finalization._persist_run_record

    def record_then_look(*args, **kwargs):
        original(*args, **kwargs)
        # The record is written: the live file must still be there.
        seen.append(live_path.exists())

    monkeypatch.setattr(finalization, "_persist_run_record", record_then_look)

    if fails:
        with pytest.raises(RuntimeError):
            run(wf, inputs={"In": 1}, out_dir=str(tmp_path))
    else:
        assert run(wf, inputs={"In": 1}, out_dir=str(tmp_path))["Out"] == [2]

    assert seen == [True]
    assert not live_path.exists()
    assert (tmp_path / "run-log.jsonl").exists()
