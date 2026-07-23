"""04 — Guarded actions: firing conditions on values and state.

A **guard** adds a firing condition beyond "enough tokens are present" — a
predicate on the token values, the task's state, or both. An action fires only
when its guard is true.

``@guard(...)`` takes a lambda whose parameters mirror the action: ``self`` plus
one per consumed port. It **peeks** at tokens without consuming them, so if the
guard is false the token stays on the queue for another action to take.

``Split`` routes each token to a different output port by sign — the dataflow
equivalent of an ``if``. Note that the two guards are *exhaustive* (every value
satisfies one) and *disjoint* (never both), so ``Split`` is deterministic even
though it has two actions.

Decorator order: ``@action`` on the outside, ``@guard`` directly above the
method.

Run it::

    wfpy run examples/04_guarded_actions.py
    python examples/04_guarded_actions.py

Expected output — non-negatives to P, negatives to N::

    {'P': [1, 0, 4], 'N': [-2]}

Next: 05_state.py — remembering across firings.
"""

from wfpy import Port, action, connect, guard, loop, run, task, workflow


@task
class Split:
    """Route each token to P (>= 0) or N (< 0)."""

    class Ports:
        In = Port[int](direction="in")
        P = Port[int](direction="out")
        N = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"P": 1})
    @guard(lambda self, x: x >= 0)
    def positive(self, x: int) -> int:
        return x

    @action(consumes={"In": 1}, produces={"N": 1})
    @guard(lambda self, x: x < 0)
    def negative(self, x: int) -> int:
        return x


@workflow(inputs={}, outputs={"P": int, "N": int})
def splitting() -> None:
    split = Split()
    stream = loop([1, -2, 0, 4])
    with stream:
        connect(stream.item, split.In)
    connect(split.P, "P")
    connect(split.N, "N")


if __name__ == "__main__":
    print(run(splitting))
