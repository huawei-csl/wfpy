"""02 — Chaining tasks, and task parameters.

Tasks compose into a **pipeline** by connecting one task's output port to the
next task's input port. The edge between them is a FIFO queue; a token produced
on one end is consumed on the other, at whatever later time the consumer fires.

This example also introduces **parameters**. A parameter is an annotated field
with **no default** — it is fixed when the task is constructed:

    scale: int          # no default  -> parameter, passed as Scale(scale=3)

(An annotated field *with* a default is state, not a parameter — see 05.)

Two ``Scale`` tasks and one ``Add`` show both ideas: each input is scaled by its
own factor, then the two streams are joined.

Run it::

    wfpy run examples/02_pipeline.py --input A=5 --input B=10
    python examples/02_pipeline.py

Expected output — 5·2 + 10·3::

    {'Out': [40]}

Next: 03_streams_and_nondeterminism.py — tasks with more than one action.
"""

from wfpy import Port, action, connect, run, task, workflow


@task
class Scale:
    """Multiply each token by a constant factor fixed at construction."""

    scale: int  # parameter — no default

    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def apply(self, x: int) -> int:
        return self.scale * x


@task
class Add:
    """Read one token from each input, emit their sum."""

    class Ports:
        A = Port[int](direction="in")
        B = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"A": 1, "B": 1}, produces={"Out": 1})
    def add(self, a: int, b: int) -> int:
        return a + b


@workflow(inputs={"A": int, "B": int}, outputs={"Out": int})
def scaled_sum() -> None:
    scale_a = Scale(scale=2)
    scale_b = Scale(scale=3)
    add = Add()
    connect("A", scale_a.In)
    connect("B", scale_b.In)
    connect(scale_a.Out, add.A)
    connect(scale_b.Out, add.B)
    connect(add.Out, "Out")


if __name__ == "__main__":
    print(run(scaled_sum, inputs={"A": 5, "B": 10}))
