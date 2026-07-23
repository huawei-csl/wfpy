"""08 — Nested workflows: a graph as an actor.

A ``@workflow`` can be used inside another workflow exactly like a task actor:
construct it, and its declared ``inputs``/``outputs`` become its ports. This is
how a subgraph is reused and named as a unit — the same reason you extract a
function, applied to graphs.

Here a two-stage ``normalize`` workflow (trim, then lowercase) is slotted into a
larger ``greeting`` pipeline without repeating its internals. The nested
workflow's ``In``/``Out`` are just ports on the ``stage`` actor.

Every ``action`` has a return type annotation. That is not decoration: a default
``action`` with no return annotation silently emits nothing (see
docs/dataflow-concepts.md, "Sharp edges").

This file defines two workflows (`normalize` and the top-level `greeting`), so
the CLI needs ``--workflow`` to say which one to run.

Run it::

    wfpy run examples/08_nested_workflows.py --workflow greeting --input "In=  Hello, WORLD  "
    python examples/08_nested_workflows.py

Expected output::

    {'Out': ['greeting: hello, world!']}

Next: 09_loop.py — bounded iteration with loop().
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
class Lower:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")

    def action(self, text: str) -> str:
        return text.lower()


@workflow(inputs={"In": str}, outputs={"Out": str})
def normalize() -> None:
    """A reusable subgraph: trim, then lowercase."""
    trim = Trim()
    lower = Lower()
    connect("In", trim.In)
    connect(trim.Out, lower.In)
    connect(lower.Out, "Out")


@task
class Greet:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")

    def action(self, text: str) -> str:
        return f"greeting: {text}!"


@workflow(inputs={"In": str}, outputs={"Out": str})
def greeting() -> None:
    """Uses the `normalize` workflow as an actor, then formats the result.

    `stage` is a whole workflow wired in like any other actor — its `In` and
    `Out` are the ports declared on `normalize`.
    """
    stage = normalize()  # a nested workflow, used as an actor
    greet = Greet()
    connect("In", stage.In)
    connect(stage.Out, greet.In)
    connect(greet.Out, "Out")


if __name__ == "__main__":
    print(run(greeting, inputs={"In": "  Hello, WORLD  "}))
