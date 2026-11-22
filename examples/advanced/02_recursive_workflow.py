"""Advanced 02 — Recursion: a generated workflow that generates the next.

Nothing in 01 says what the generated workflow may contain. It may contain an
author and a tool node of its own, and then the workflow it runs is one that
writes and runs another: a recursion, one ``wfpy run`` deep per level.

    level n > 0:   Author --Workflow (.py)--> Execute --Run--> Times(n) --Answer-->
    level 0:       One --Answer-->

Here the recursion is in its smallest form. The author's reply is *this file*
with ``LEVEL`` lowered by one, so the run at level 4 writes and runs level 3,
down to level 0, which holds no author and ends the chain. Each level
multiplies the answer of the level below by its own number: a factorial, with
one run of wfpy as each call.

The base case is a different graph, not a guard: the builder below wires
``One`` alone when ``LEVEL`` is 0. A recursion needs such an end, and wfpy does
not bound it for you (04 passes a level counter and refuses past a limit).

The author is on ``transport="mock"`` on purpose: the point is the recursion.
04 has a real author at every level.

Run it::

    wfpy run examples/advanced/02_recursive_workflow.py
    python examples/advanced/02_recursive_workflow.py

Expected output — 4 * 3 * 2 * 1, through five nested runs::

    {'Answer': [24]}

Next: 03_data_shaped_workflow.py — an author that is told about the data.
"""

import json
import sys
from pathlib import Path

from wfpy import File, Port, action, agent, connect, run, task, tool, workflow

LEVEL = 4

# What the author replies: this file, one level down.
NEXT_LEVEL = Path(__file__).read_text().replace(f"LEVEL = {LEVEL}", f"LEVEL = {LEVEL - 1}", 1)


@agent(prompt="Write the next level.", transport="mock", mock_outputs={"Workflow": NEXT_LEVEL})
class Author:
    class Ports:
        Workflow = Port[File](direction="out", ext=".py")


@tool(
    cmd=sys.executable,
    args=["-m", "wfpy.cli", "run", "{in.Workflow}", "--out-dir", "{out.Run}", "--run-id", "child"],
)
class Execute:
    class Ports:
        Workflow = Port[File](direction="in")
        Run = Port[File](direction="out")


@task
class Times:
    """Multiplies the answer of the level below by ``n``."""

    n: int  # parameter

    class Ports:
        Run = Port[File](direction="in")
        Answer = Port[int](direction="out")

    @action(consumes={"Run": 1}, produces={"Answer": 1})
    def times(self, run_dir) -> int:
        record = json.loads((Path(str(run_dir)) / "child" / "run.wf-run.json").read_text())
        return self.n * record["outputs"]["Answer"][0]


@task
class One:
    """The base case: emits 1, once."""

    class Ports:
        Answer = Port[int](direction="out")

    class Schedule:  # the latch of a node with no input: one firing, then spent
        initial = "armed"
        transitions = [("armed", "one", "spent")]

    @action(consumes={}, produces={"Answer": 1})
    def one(self) -> int:
        return 1


@workflow(outputs={"Answer": int})
def factorial() -> None:
    if LEVEL == 0:
        one = One()
        connect(one.Answer, "Answer")
        return
    author, execute, times = Author(), Execute(), Times(n=LEVEL)
    connect(author.Workflow, execute.Workflow)
    connect(execute.Run, times.Run)
    connect(times.Answer, "Answer")


if __name__ == "__main__":
    print(run(factorial))
