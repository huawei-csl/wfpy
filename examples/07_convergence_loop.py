"""07 — Convergence loops: feeding output back until it settles.

05 handled "do this for each item" with ``loop()``, when you know the items up
front. This is the other kind of repetition: keep going until a condition is
met — the shape of an agent repair loop (generate, inspect, and on failure send
the work back).

The catch is that **input ports do not fan in** (see docs/dataflow-concepts.md).
You cannot route the feedback edge into the same port that carries the initial
input. So a loop needs **two input ports and two actions**:

* ``Start`` + ``begin`` — the first arrival
* ``Back``  + ``step``  — each fed-back value

The ``Again`` output is wired back to ``Back``, and each action decides whether
to emit again or to finish. Two separate actions are required because one action
consuming both ports would need a token on *each* to fire (a join) — the wrong
rule for a loop. Each action returns a dict naming a single output port, so it
emits on ``Again`` *or* ``Done``, never both.

Run it::

    wfpy run examples/07_convergence_loop.py --input In=4
    python examples/07_convergence_loop.py

Expected output — each result is the sum 1..n, reached by looping n times::

    In=4 -> {'Out': [10]}
    In=1 -> {'Out': [1]}
    In=5 -> {'Out': [15]}

Tier A ends here. You now have actors, ports, tokens, queues, firing, guards,
state, feedback loops, and a way to inspect a run.

Next (Tier B): 08_nested_workflows.py — composing graphs.
"""

from wfpy import Port, action, connect, run, task, workflow


@task
class Countdown:
    """Sums n, n-1, ..., 1 by looping, to show a feedback edge that settles."""

    total: int = 0  # state, carried across every firing of the loop

    class Ports:
        Start = Port[int](direction="in")  # initial input — one source
        Back = Port[int](direction="in")  # feedback — one source
        Again = Port[int](direction="out")  # wired back into Back
        Done = Port[int](direction="out")

    @action(consumes={"Start": 1}, produces={"Again": 1, "Done": 1})
    def begin(self, n: int):
        self.total += n
        return {"Again": n - 1} if n > 1 else {"Done": self.total}

    @action(consumes={"Back": 1}, produces={"Again": 1, "Done": 1})
    def step(self, n: int):
        self.total += n
        return {"Again": n - 1} if n > 1 else {"Done": self.total}


@workflow(inputs={"In": int}, outputs={"Out": int})
def countdown() -> None:
    counter = Countdown()
    connect("In", counter.Start)
    connect(counter.Again, counter.Back)  # feedback lands on a separate port
    connect(counter.Done, "Out")


if __name__ == "__main__":
    for n in (4, 1, 5):
        print(f"In={n} -> {run(countdown, inputs={'In': n})}")
