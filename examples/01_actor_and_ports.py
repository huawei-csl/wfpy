"""01 — Actors and ports.

An **actor** is an instance of a ``@task`` class. **Ports** are its typed inputs
and outputs. An actor **fires** when its input ports have data: firing consumes
the input, runs ``action``, and puts the return value on the output port.

Nothing here calls ``Doubler``. Seeding a value on the workflow's ``In`` port is
what makes it run.

Run it::

    wfpy run examples/01_actor_and_ports.py --input In=21
    python examples/01_actor_and_ports.py

Expected output::

    {'Out': [42]}

Next: 02_pipeline.py — what sits on the edge between two actors.
"""

from wfpy import Port, connect, run, task, workflow


@task
class Doubler:
    """One actor: takes an int, emits twice that int."""

    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    def action(self, x: int) -> int:
        return x * 2


@workflow(inputs={"In": int}, outputs={"Out": int})
def doubling() -> None:
    """A graph with a single actor wired to the workflow's own ports.

    ``connect()`` takes a port instance, or a string naming a workflow-level
    port. Wiring is declarative: it says what is connected, never when it runs.
    """
    doubler = Doubler()
    connect("In", doubler.In)
    connect(doubler.Out, "Out")


if __name__ == "__main__":
    print(run(doubling, inputs={"In": 21}))
