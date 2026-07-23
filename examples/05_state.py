"""05 — Tasks with state.

A firing can leave information behind for later firings by writing to a **state
field**. In wfpy — which is plain Python, not a compiled language — a state
field is simply an annotated field **with a default** (its initial value):

    total: int = 0      # has a default  -> state, persists across firings

Contrast with a parameter (annotated, *no* default; see 02). Fields whose name
starts with ``_`` are also state. State is where a running total lives — or, for
an agent task, its conversation history.

``Sum`` accumulates every token it consumes. Because the action body is ordinary
Python, *where* you read the field decides which value you get: reading
``self.total`` after the update emits the running sum including the current
token; reading it before would emit the sum of everything prior.

``loop`` feeds one token per item, so ``Sum`` fires four times, carrying
``total`` across firings.

Run it::

    wfpy run examples/05_state.py
    python examples/05_state.py

Expected output — the running total after each token::

    {'Out': [1, 3, 6, 10]}

Next: 06_schedules.py — sequencing a task's actions with a state machine.
"""

from wfpy import Port, action, connect, loop, run, task, workflow


@task
class Sum:
    """Accumulate the sum of all tokens consumed so far."""

    total: int = 0  # default -> state

    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def step(self, x: int) -> int:
        self.total = self.total + x
        return self.total


@workflow(inputs={}, outputs={"Out": int})
def running_total() -> None:
    accumulator = Sum()
    stream = loop([1, 2, 3, 4])
    with stream:
        connect(stream.item, accumulator.In)
    connect(accumulator.Out, "Out")


if __name__ == "__main__":
    print(run(running_total))
