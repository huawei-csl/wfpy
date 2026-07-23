"""04 — Guards: choosing what an actor does.

A task can declare several ``@action`` methods. A ``@guard`` is a predicate that
decides whether one is eligible; actions are tried in declaration order and the
first eligible one fires. An unguarded action at the end is the fallback.

A guard runs **before** any token is consumed, so a guard that says no costs
nothing — the token stays on the queue for another action to take.

This is how you route without a branching node, and it is what turns an actor
from a function into a small state machine. For orchestration it is the natural
way to say "if the reviewer approved, ship it; otherwise send it back".

Note the decorator order: ``@action`` on the outside, ``@guard`` directly above
the method.

Run it::

    wfpy run examples/04_guards.py --input In=12
    python examples/04_guards.py

Expected output::

    12 -> {'Out': ['big:12']}
    5  -> {'Out': ['small:5']}
    -3 -> {'Out': ['negative:3']}

Next: 05_state.py — actors that remember across firings.
"""

from wfpy import Port, action, connect, guard, run, task, workflow


@task
class Classify:
    """Three actions on one actor, selected by guard."""

    threshold: int  # a parameter, fixed when the actor is constructed

    class Ports:
        In = Port[int](direction="in")
        Out = Port[str](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    @guard(lambda self, n: n < 0)
    def negative(self, n: int) -> str:
        return f"negative:{abs(n)}"

    @action(consumes={"In": 1}, produces={"Out": 1})
    @guard(lambda self, n: n >= self.threshold)
    def big(self, n: int) -> str:
        return f"big:{n}"

    @action(consumes={"In": 1}, produces={"Out": 1})
    def small(self, n: int) -> str:
        """No guard — the fallback, reached only if the others declined."""
        return f"small:{n}"


@workflow(inputs={"In": int}, outputs={"Out": str})
def classifying() -> None:
    classify = Classify(threshold=10)
    connect("In", classify.In)
    connect(classify.Out, "Out")


if __name__ == "__main__":
    for value in (12, 5, -3):
        print(f"{value:<3}-> {run(classifying, inputs={'In': value})}")
