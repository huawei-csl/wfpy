"""03 — Fan-out, join, and concurrency for free.

Connect one output to several consumers and each gets its own token: that is
**fan-out**. An actor with several input ports only fires once *every* one of
them holds a token: that is a **join**, and it synchronises without any extra
machinery.

The part that matters for orchestration: ``Slow`` fires twice, and the two
firings happen **at the same time**. wfpy dispatches every ready actor to a
thread pool each round, so independent work is concurrent by default. Nothing
in this file mentions threads.

This is the shape of fanning out to several agents and waiting for all of them.

Run it::

    wfpy run examples/03_fan_out_and_join.py --input In=1
    python examples/03_fan_out_and_join.py

Expected output (roughly 0.5s, not 1.0s)::

    {'Out': ['slow-a + slow-b']}
    elapsed: 0.5s  (sequential would be 1.0s)

Next: 04_guards.py — choosing what an actor does.
"""

import time

from wfpy import Port, connect, run, task, workflow


@task
class Slow:
    """Stands in for anything with latency — an API call, or an agent."""

    label: str  # annotated, no default -> a parameter, set at construction

    class Ports:
        In = Port[int](direction="in")
        Out = Port[str](direction="out")

    def action(self, _seed: int) -> str:
        time.sleep(0.5)
        return f"slow-{self.label}"


@task
class Join:
    """Fires only when BOTH inputs hold a token.

    Parameters bind by *position*, in port order — not by name. Keep the two
    orders aligned; see docs/dataflow-concepts.md, "Sharp edges".
    """

    class Ports:
        A = Port[str](direction="in")
        B = Port[str](direction="in")
        Out = Port[str](direction="out")

    def action(self, first: str, second: str) -> str:
        return f"{first} + {second}"


@workflow(inputs={"In": int}, outputs={"Out": str})
def fan_out_join() -> None:
    """One input fans out to two slow actors, whose results are joined."""
    slow_a = Slow(label="a")
    slow_b = Slow(label="b")
    join = Join()

    connect("In", slow_a.In)
    connect("In", slow_b.In)
    connect(slow_a.Out, join.A)
    connect(slow_b.Out, join.B)
    connect(join.Out, "Out")


if __name__ == "__main__":
    started = time.time()
    result = run(fan_out_join, inputs={"In": 1})
    elapsed = time.time() - started
    print(result)
    print(f"elapsed: {elapsed:.1f}s  (sequential would be 1.0s)")
