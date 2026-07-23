"""05 — State: actors that remember across firings.

An actor is a long-lived object, not a function invocation. Annotated fields are
classified by whether they have a default:

* ``rate: float``    — no default, so a **parameter**, fixed at construction
* ``total: int = 0`` — has a default, so **state**, surviving every firing
* ``_seen: int = 0`` — leading underscore, also state

State is where an agent's conversation history or a running tally lives.

To *see* state persist you need the actor to fire more than once, which means
more than one token has to arrive. Note that ``run(inputs={"In": [1, 2, 3]})``
would seed a single token holding the list — not three tokens. ``loop()`` over a
collection is the way to feed a sequence: it emits one token per item, so the
actor fires once per item and carries its state between them.

Run it::

    wfpy run examples/05_state.py
    python examples/05_state.py

Expected output — a running total, not three independent results::

    {'Out': [100, 250, 400]}
    fired 3 times

Next: 06_inspecting_a_run.py — seeing which actor fired, and when.
"""

from wfpy import Port, connect, loop, run, task, workflow

SALES = [100, 150, 150]


@task
class RunningTotal:
    """Adds each amount to a total that survives between firings."""

    rate: float = 1.0  # has a default -> state, but used here as a fixed factor
    total: int = 0  # state: the whole point of this example
    _firings: int = 0  # state: leading underscore also marks state

    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    def action(self, amount: int) -> int:
        self._firings += 1
        self.total += int(amount * self.rate)
        return self.total

    def report(self) -> str:
        return f"fired {self._firings} times"


@workflow(inputs={}, outputs={"Out": int})
def running_total() -> None:
    """``loop()`` emits one token per item, so the actor fires once per item."""
    sales = loop(SALES)
    with sales:
        accumulator = RunningTotal()
        connect(sales.item, accumulator.In)
        connect(accumulator.Out, "Out")


if __name__ == "__main__":
    print(run(running_total))
    print(f"fired {len(SALES)} times")
