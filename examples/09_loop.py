"""09 — Bounded iteration with loop().

05 used ``loop()`` in passing to feed a sequence. This is the fuller picture:
``loop(items)`` is a control node that emits **one token per item**, so the body
of the loop runs once per item. It is the tool for "do this for each of a known
collection" — as opposed to 07's convergence loop, which repeats until a
condition is met.

The body is an ordinary subgraph. Here each item flows through a ``Square``
actor (the per-item work) and then a stateful ``Sum`` actor (the running
aggregate), so the output is the running total after each item.

``loop()`` needs an active workflow graph, so it is only valid inside a
``@workflow`` body, used as ``with loop(...) as ...:`` — here ``with items:``.

Run it::

    wfpy run examples/09_loop.py
    python examples/09_loop.py

Expected output — squares 1,4,9,16 accumulated: 1, 5, 14, 30::

    {'Out': [1, 5, 14, 30]}

Tier B ends here. You now have branching-by-guard (04), both loop kinds (07, 09),
and workflow nesting (08) — the shapes a plain chain of edges cannot express.
"""

from wfpy import Port, connect, loop, run, task, workflow

NUMBERS = [1, 2, 3, 4]


@task
class Square:
    """The per-item work — pure, no state."""

    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    def action(self, n: int) -> int:
        return n * n


@task
class Sum:
    """Accumulates across firings; see 05 for state."""

    total: int = 0

    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    def action(self, n: int) -> int:
        self.total += n
        return self.total


@workflow(inputs={}, outputs={"Out": int})
def running_squares() -> None:
    """One token per item flows through Square, then accumulates in Sum."""
    items = loop(NUMBERS)
    with items:
        square = Square()
        total = Sum()
        connect(items.item, square.In)
        connect(square.Out, total.In)
        connect(total.Out, "Out")


if __name__ == "__main__":
    print(run(running_squares))
