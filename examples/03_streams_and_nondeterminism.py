"""03 — Streams, and tasks with more than one action.

So far each task had one action. A task may have **several** — and more than one
can be ready to fire at the same moment. When that happens the model does not
say which fires first: the choice is **nondeterministic**. (This runtime breaks
ties by action-declaration order, but you should not rely on that; a
deterministic order is what schedules and priorities are for — see 06 and 07.)

``Merge`` copies whichever input has a token to a shared output. With tokens
waiting on *both* inputs, either action could fire, so the interleaving of the
output stream is not specified — only that every token comes through.

This example also needs a **stream** of tokens rather than a single value.
``run(inputs=...)`` seeds one token per port; ``loop([...])`` injects a sequence,
firing the downstream once per item. Two loops feed the two inputs here.

Run it::

    wfpy run examples/03_streams_and_nondeterminism.py
    python examples/03_streams_and_nondeterminism.py

Expected output — all six tokens, in *some* order (one possibility)::

    {'Out': [1, 10, 20, 30, 2, 3]}

Next: 04_guarded_actions.py — firing conditions on token values.
"""

from wfpy import Port, action, connect, loop, run, task, workflow


@task
class Merge:
    """Two actions, one output — a nondeterministic merge of two streams."""

    class Ports:
        A = Port[int](direction="in")
        B = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"A": 1}, produces={"Out": 1})
    def from_a(self, x: int) -> int:
        return x

    @action(consumes={"B": 1}, produces={"Out": 1})
    def from_b(self, x: int) -> int:
        return x


@workflow(inputs={}, outputs={"Out": int})
def merging() -> None:
    merge = Merge()
    stream_a = loop([1, 2, 3])
    with stream_a:
        connect(stream_a.item, merge.A)
    stream_b = loop([10, 20, 30])
    with stream_b:
        connect(stream_b.item, merge.B)
    connect(merge.Out, "Out")


if __name__ == "__main__":
    result = run(merging)
    print(result)
    # The order is nondeterministic; the multiset of tokens is not:
    print("all tokens:", sorted(result["Out"]))
