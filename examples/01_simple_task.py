"""01 — Simple tasks: ports, actions, tokens, firings.

A **task** is an actor: it performs its work in a sequence of steps called
**firings**. Each firing may consume tokens from input ports, update state, and
produce tokens on output ports.

An **action** is one such step. You declare its token rates explicitly with
``@action(consumes=..., produces=...)``:

* ``consumes={"In": 1}`` — read one token from port ``In`` per firing; its value
  is bound to the method parameter. This is also a *firing condition*: without a
  token, the action cannot fire.
* ``produces={"Out": 1}`` — emit one token on port ``Out`` per firing; its value
  is the method's return value.

Always decorate an action with ``@action``. (An undecorated method named
``action`` is inferred, but only emits output when it has a return annotation —
``@action`` is explicit and never surprising.)

Run it::

    wfpy run examples/01_simple_task.py --input In=21
    python examples/01_simple_task.py

Expected output::

    {'Out': [42]}

Next: 02_pipeline.py — chaining tasks, and task parameters.
"""

from wfpy import Port, action, connect, run, task, workflow


@task
class Doubler:
    """One action: read an int, emit twice its value."""

    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def double(self, x: int) -> int:
        return x * 2


@workflow(inputs={"In": int}, outputs={"Out": int})
def doubling() -> None:
    """Wire the workflow's ``In``/``Out`` boundary ports to one Doubler."""
    doubler = Doubler()
    connect("In", doubler.In)
    connect(doubler.Out, "Out")


if __name__ == "__main__":
    print(run(doubling, inputs={"In": 21}))
