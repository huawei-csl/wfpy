"""06 — Inspecting a run: which actor fired, and when.

Because nothing in a dataflow graph says what order things happen in, the
question "why didn't my actor fire?" comes up constantly. wfpy answers it with a
**queue trace**: every run records, for each step, which actor fired and how
many tokens were sitting on every queue at that moment.

``run()`` writes it to ``<out_dir>/<timestamp>_<id>/run.wf-queues.json``. The
``leftovers`` entry is the one to check first — tokens still on a queue when the
run ended usually mean a join never received all of its inputs, or a consumer
was never connected.

This example runs the fan-out graph from 03 and prints the trace back.

Run it::

    wfpy run examples/06_inspecting_a_run.py --input In=1
    python examples/06_inspecting_a_run.py

Each firing gets its own step number, so the trace is a flat record of firings
rather than of scheduler rounds — ``left`` and ``right`` actually ran
concurrently (see 03), but appear as separate steps here. What the trace does
show reliably is *that* an actor fired, how many times, and what was queued.

Expected output::

    {'Out': ['L|R']}

    step  actor   fired
    1     left    1
    2     right   1
    3     join    1
    leftover tokens: none

Tier A ends here. You now have actors, ports, tokens, queues, firing, guards,
state, and a way to see it all happen.
"""

import json
import tempfile
from pathlib import Path

from wfpy import Port, connect, run, task, workflow


@task
class Tag:
    """Emits a fixed label, ignoring its input value."""

    label: str

    class Ports:
        In = Port[int](direction="in")
        Out = Port[str](direction="out")

    def action(self, _seed: int) -> str:
        return self.label


@task
class Join:
    class Ports:
        A = Port[str](direction="in")
        B = Port[str](direction="in")
        Out = Port[str](direction="out")

    def action(self, first: str, second: str) -> str:
        return f"{first}|{second}"


@workflow(inputs={"In": int}, outputs={"Out": str})
def traced() -> None:
    left = Tag(label="L")
    right = Tag(label="R")
    join = Join()

    connect("In", left.In)
    connect("In", right.In)
    connect(left.Out, join.A)
    connect(right.Out, join.B)
    connect(join.Out, "Out")


def load_queue_trace(out_dir: Path) -> dict:
    """Read the queue trace from the most recent run directory under *out_dir*."""
    run_dirs = sorted(p for p in out_dir.iterdir() if p.is_dir())
    return json.loads((run_dirs[-1] / "run.wf-queues.json").read_text())


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        print(run(traced, inputs={"In": 1}, out_dir=str(out_dir)))

        trace = load_queue_trace(out_dir)
        print()
        print(f"{'step':<6}{'actor':<8}fired")
        for step in trace["steps"]:
            print(
                f"{step['step']:<6}{step['actorInstanceName']:<8}"
                f"{step['actorFireCount']}"
            )
        leftovers = trace["leftovers"]
        print(f"leftover tokens: {leftovers if leftovers else 'none'}")
