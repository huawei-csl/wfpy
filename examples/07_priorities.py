"""07 — Priorities: ordering actions that could both fire.

Sometimes several actions are activated at once and you want a definite
precedence among them. **Priorities** declare that ordering: a nested
``Priority`` class whose ``rules`` list ranks action names from highest to
lowest.

The classic use is a deterministic case-statement. ``Route`` sends even numbers
to ``X``, remaining multiples of three to ``Y``, and everything else to ``Z``.
The guards deliberately overlap (6 is both even and a multiple of three), but
the priority ``toX > toY > toZ`` resolves it: 6 goes to ``X``. Without the
priority this task would be nondeterministic on such inputs.

``toZ`` has no guard — it is the fallback, and being last in the priority order
it only fires when the others cannot.

The workflow feeds the stream 4, 9, 5, 6.

Run it::

    wfpy run examples/07_priorities.py
    python examples/07_priorities.py

Expected output — 4 and 6 to X, 9 to Y, 5 to Z::

    {'X': [4, 6], 'Y': [9], 'Z': [5]}

Next: 08_networks.py — sources, sinks, fan-out, and feedback loops.
"""

from wfpy import Port, action, connect, guard, loop, run, task, workflow


@task
class Route:
    """Route by divisibility, made deterministic by a priority order."""

    class Ports:
        In = Port[int](direction="in")
        X = Port[int](direction="out")
        Y = Port[int](direction="out")
        Z = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"X": 1})
    @guard(lambda self, v: v % 2 == 0)
    def toX(self, v: int) -> int:
        return v

    @action(consumes={"In": 1}, produces={"Y": 1})
    @guard(lambda self, v: v % 3 == 0)
    def toY(self, v: int) -> int:
        return v

    @action(consumes={"In": 1}, produces={"Z": 1})
    def toZ(self, v: int) -> int:  # no guard — the fallback
        return v

    class Priority:
        rules = [["toX", "toY", "toZ"]]  # highest to lowest


@workflow(inputs={}, outputs={"X": int, "Y": int, "Z": int})
def routing() -> None:
    route = Route()
    stream = loop([4, 9, 5, 6])
    with stream:
        connect(stream.item, route.In)
    connect(route.X, "X")
    connect(route.Y, "Y")
    connect(route.Z, "Z")


if __name__ == "__main__":
    print(run(routing, max_workers=1))
