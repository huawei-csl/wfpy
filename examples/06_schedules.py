"""06 — Schedules: sequencing actions with a state machine.

Often you want a task's actions to fire in a controlled order rather than
nondeterministically. You could track a state field and gate each action with a
guard on it — but that is exactly the pattern a **schedule** captures directly.

A schedule is a finite state machine over action names, declared as a nested
``Schedule`` class with an ``initial`` state and ``transitions`` — triples of
``(state, action, next_state)``. An action is eligible only if the current state
has a transition on it; firing it moves the machine to the next state.

``FairMerge`` alternates strictly between its two inputs: fire ``a`` (from state
``s1``, going to ``s2``), then only ``b`` is eligible (going back to ``s1``), and
so on. The nondeterminism of 03's ``Merge`` is gone — the order is fixed by the
machine.

Run it::

    wfpy run examples/06_schedules.py
    python examples/06_schedules.py

Expected output — strict alternation of the two streams::

    {'Out': [1, 2, 3, 4, 5, 6]}

Next: 07_priorities.py — ordering actions that could otherwise both fire.
"""

from wfpy import Port, action, connect, loop, run, task, workflow


@task
class FairMerge:
    """Alternate between reading Input A and Input B."""

    class Ports:
        A = Port[int](direction="in")
        B = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"A": 1}, produces={"Out": 1})
    def a(self, x: int) -> int:
        return x

    @action(consumes={"B": 1}, produces={"Out": 1})
    def b(self, x: int) -> int:
        return x

    class Schedule:
        initial = "s1"
        transitions = [
            ("s1", "a", "s2"),  # in s1, fire a, go to s2
            ("s2", "b", "s1"),  # in s2, fire b, back to s1
        ]


@workflow(inputs={}, outputs={"Out": int})
def alternating() -> None:
    merge = FairMerge()
    stream_a = loop([1, 3, 5])
    with stream_a:
        connect(stream_a.item, merge.A)
    stream_b = loop([2, 4, 6])
    with stream_b:
        connect(stream_b.item, merge.B)
    connect(merge.Out, "Out")


if __name__ == "__main__":
    # max_workers=1 for a reproducible interleaving in the printed output.
    print(run(alternating, max_workers=1))
