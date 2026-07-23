"""02 — Queues are the edges.

Three actors in a chain. The important idea is what sits *between* them: an edge
is a **FIFO queue**, and the values travelling on it are **tokens**.

``Trim`` producing a value does not call ``Shout``. It puts a token on the queue
between them. ``Shout`` fires later, when the scheduler notices it can. That
indirection is the whole difference between a dataflow graph and a chain of
function calls, and it is what lets independent actors run at the same time
(see 03).

Run it::

    wfpy run examples/02_pipeline.py --input "In=  hello, dataflow  "
    python examples/02_pipeline.py

Expected output::

    {'Out': ['HELLO, DATAFLOW!']}

Next: 03_fan_out_and_join.py — one producer, two consumers, and concurrency.
"""

from wfpy import Port, connect, run, task, workflow


@task
class Trim:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")

    def action(self, text: str) -> str:
        return text.strip()


@task
class Shout:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")

    def action(self, text: str) -> str:
        return text.upper()


@task
class Punctuate:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")

    def action(self, text: str) -> str:
        return f"{text}!"


@workflow(inputs={"In": str}, outputs={"Out": str})
def shouting() -> None:
    """In -> trim -> shout -> punctuate -> Out.

    Each ``connect()`` creates one queue. Three actors, four queues.
    """
    trim = Trim()
    shout = Shout()
    punctuate = Punctuate()

    connect("In", trim.In)
    connect(trim.Out, shout.In)
    connect(shout.Out, punctuate.In)
    connect(punctuate.Out, "Out")


if __name__ == "__main__":
    print(run(shouting, inputs={"In": "  hello, dataflow  "}))
