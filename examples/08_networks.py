"""08 — Building networks: sources, sinks, fan-out, and feedback.

The earlier examples fed tasks from ``loop()`` and read results from workflow
boundary ports. A network can also be **closed** — its sources and sinks are
tasks *inside* it. This chapter shows the pieces that make that work.

**Source** — a task with no input ports whose action fires on its own, gated by
a state guard so it eventually stops. A zero-input action must declare
``consumes={}`` explicitly (an omitted ``consumes`` is treated as "infer from
inputs" and never fires without one). ``Ones`` emits ``n`` tokens, then its guard
goes false and it goes quiet.

**Delay** (an "initial token") — a task that places one token at startup and
then copies its input forever, sequenced by a schedule so ``init`` fires exactly
once. This is how you seed a feedback loop.

**Fan-out** — connecting one output port to several destinations. Each consumer
gets its own copy of every token. Here ``add.Out`` feeds both the delay (the
loop-back) and the workflow output.

**Feedback** — an edge from a later task back to an earlier one. The running-sum
ring below feeds each sum back through ``Delay`` into ``Add.B``, so every output
is the sum of the ``Ones`` stream so far.

Two limitations worth knowing (both by design):

* **Inputs do not fan in.** An input port reads from a single source; pointing
  two producers at one port silently ignores the second. To merge streams, use a
  task with one input port per source (03, 06).
* **No backpressure.** Queues are unbounded, so a feedback loop must
  *self-terminate* — here, because ``Ones`` stops. An endless generator would run
  forever.

Run it::

    wfpy run examples/08_networks.py
    python examples/08_networks.py

Expected output — the running sum of six 1s::

    {'Out': [1, 2, 3, 4, 5, 6]}

Next: 09_agents_are_actors.py — an LLM agent is just another task.
"""

from wfpy import Port, action, connect, guard, run, task, workflow


@task
class Ones:
    """Source: emit ``n`` ones, then stop (self-terminating)."""

    n: int  # parameter
    sent: int = 0  # state

    class Ports:
        Out = Port[int](direction="out")

    @action(consumes={}, produces={"Out": 1})  # consumes={} = fires without input
    @guard(lambda self: self.sent < self.n)
    def emit(self) -> int:
        self.sent = self.sent + 1
        return 1


@task
class Delay:
    """Place one initial token, then copy input forever (an initial-token cell)."""

    value: int  # parameter

    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={}, produces={"Out": 1})
    def init(self) -> int:
        return self.value

    @action(consumes={"In": 1}, produces={"Out": 1})
    def copy(self, x: int) -> int:
        return x

    class Schedule:
        initial = "start"
        transitions = [("start", "init", "run"), ("run", "copy", "run")]


@task
class Add:
    class Ports:
        A = Port[int](direction="in")
        B = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"A": 1, "B": 1}, produces={"Out": 1})
    def add(self, a: int, b: int) -> int:
        return a + b


@workflow(inputs={}, outputs={"Out": int})
def running_sum() -> None:
    ones = Ones(n=6)
    delay = Delay(value=0)
    add = Add()
    connect(ones.Out, add.A)
    connect(delay.Out, add.B)
    connect(add.Out, delay.In)  # feedback ring: running sum
    connect(add.Out, "Out")  # fan-out: same output also leaves the network


if __name__ == "__main__":
    print(run(running_sum, max_workers=1))
