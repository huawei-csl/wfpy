---
name: wfpy-graph-composition
description: Wire wfpy nodes into a graph - nest sub-workflows, declare boundary ports, build a feedback loop that terminates, and work around the fact that input ports do not fan in. Use when composing a workflow from actors, nesting workflows, adding a loop or a retry edge, or deciding what becomes a sub-workflow. Triggers: connect, sub-workflow, nested workflow, boundary port, feedback edge, loop, retry, fan in, merge node, quiescence, workflow outputs.
---

# Composing a wfpy graph

## Nesting

A sub-workflow is instantiated like any other node and exposes its boundary ports as
attributes:

```python
@workflow(inputs={"Kernel": str},
          outputs={"Report": File(ext=".md"), "Verdict": str})
def repair_flow() -> None:
    runner, decide, writer = RunKernel(run_mode="sim"), Decide(budget=2), WriteReport()
    connect("Kernel", runner.Initial)       # a string names a boundary port
    connect(runner.Result, decide.Result)
    connect(decide.Retry, runner.Retry)     # the feedback edge
    connect(decide.Done, writer.Done)
    connect(writer.Report, "Report")
    connect(writer.Verdict, "Verdict")

# and in the caller:
flow = repair_flow()
connect(kernel.Out, flow.Kernel)
connect(flow.Report, view.Report)
```

**Declare `ext=` on boundary outputs too, not only on the producing port.** At a
sub-workflow boundary the declared ext is the only thing that names the materialized copy;
the source path's own suffix is not consulted. Measured: a writer returned `report.md` and
its own copy came out `writer__Report__0.md`, while the boundary re-export of the same file
came out `flow__Report__0`, with no extension, because the boundary output was declared as
a bare `File`. Declared `File(ext=".md")`, it came out `flow__Report__0.md`.

A phase earns a sub-workflow when it has its own inputs and outputs and could be swapped
out. A step that only transforms one token is a task. Nesting one task is noise; nesting a
four-actor measurement phase that two top-level workflows share is the point.

## Input ports do not fan in

An output port broadcasts to every consumer. An input port reads **one** producer. Two
`connect()` calls into the same input port raise nothing: the node reads the producer
connected first, and the other producer's tokens are never read. They show under
`leftovers` in `run.wf-queues.json`, as `{"queueId": "b.Out-->t.In", "count": 1}`. This is
the single most common wiring mistake.

So a loop head needs **two input ports on one actor**, and a schedule that says in which
order they are read:

```python
@task
class RunKernel:
    class Ports:
        Initial = Port[str](direction="in")     # from the boundary
        Retry   = Port[str](direction="in")     # from the feedback edge
        Result  = Port[Outcome](direction="out")

    class Schedule:
        initial = "fresh"
        transitions = [
            ("fresh",   "from_initial", "looping"),
            ("looping", "from_retry",   "looping"),
        ]

    @action(consumes={"Initial": 1}, produces={"Result": 1})
    def from_initial(self, kernel): return self._run(kernel)

    @action(consumes={"Retry": 1}, produces={"Result": 1})
    def from_retry(self, kernel): return self._run(kernel)
```

Without the schedule both actions are eligible at any time, and which port is read first
when both hold a token is left to the order of the methods in the file.

When the thing in the loop is a **sub-workflow**, whose boundary has one `Kernel` input,
put a small merge node in front of it instead. That keeps the sub-workflow reusable and
makes the join visible in the graph rather than hidden inside a task:

```python
merge = MergeKernel()                  # Initial + Retry -> Out
connect("Kernel", merge.Initial)
connect(merge.Out, simulate.Kernel)
connect(candidate.Kernel, merge.Retry)   # the feedback edge
```

Note the asymmetry: a `@task` can have two input ports consumed by two different actions
(OR), **and** two consumed together by one action (`consumes={"A": 1, "B": 1}`, AND). An
`@agent` only has the AND form; see `wfpy-agent-nodes`.

## A loop has no backpressure, so it must stop itself

A feedback edge is just an edge. Nothing throttles it, and nothing ends it. **Every cycle
needs a counter that only goes up**, checked by the guard that re-enters the loop:

```python
budget: int                        # a parameter
_rounds: int = 0                   # state

class Priority:                    # the exits of the loop, as a case statement
    rules = [["accept", "ask_again", "give_up"]]

@action(consumes={"Result": 1}, produces={"Retry": 1})
@guard(lambda self, r: self._rounds < self.budget)
def ask_again(self, r):
    self._rounds += 1              # increment on the way round, not in the guard
    ...
```

The node that decides has several actions on one port, so it gets a `Priority`: accept
first, ask again while the budget lasts, and give up last, with no guard, so that the loop
always has an exit that can fire. `wfpy-actor-authoring` has the whole node, with the
`Schedule` that ends it after its exit.

Independent cycles need independent counters. If a loop can re-enter for two different
reasons (a slower candidate, and one that failed to build) give each its own budget, and
bound the total deliberately (`rounds × repairs`).

A node with **no input port at all** is a cycle too: nothing has to arrive for it to fire,
so nothing stops it. A `@task` action with `consumes={}` and no guard fires until the
process is killed. Give it a latch, as `@source` does for you: a `Schedule` with one
transition out of its initial state fires the action once. On an `@agent`,
`fireable_without_input=N` is such a budget only when the agent declares an input port: it
then fires N times with nothing on its inputs, and once more per token. An agent with no
input port ignores the count and fires without end (measured, with
`fireable_without_input=2`: over a thousand firings in twelve seconds).

Make every exit route to a node that **writes something**. A loop whose exit leads nowhere
reaches quiescence with no report, which looks exactly like a hang.

## Count the nodes you meant to build

`wfpy plan <file> --workflow <name> --format graph` prints the graph as JSON. Read it back:

```bash
wfpy plan src/optimize.py --workflow optimize_kernel --format graph \
  | python3 -c "import json,sys; g=json.load(sys.stdin)['graph']; \
      [print(n['kind'], n['label'], n.get('type','')) for n in g['nodes']]; \
      print('edges:', len(g['edges']))"
```

`kind` distinguishes `source` / `internal` (a `@task`) / `agent` / `viewer` / `workflow`,
so a sub-workflow you expected to nest shows as `workflow` and a node you forgot to wire
shows with no edges. The workflow's own boundary ports are listed too, as `wf-input` and
`wf-output`; leave them out of the count. Counting `"kind"` occurrences in the raw text
does **not** give a node count; parse `graph.nodes`.

## When a run does nothing

Quiescence with no error means some port never received a token. `run.wf-queues.json` in
the run directory has one step per firing, each with every queue's size, so a producer
that emitted nothing is immediately visible, and `leftovers` names the queues that still
held tokens at the end: a node waiting on its other inputs, or a queue nothing reads. If
the file is not there at all, no node fired. Work backwards from the first empty queue to
the action that should have filled it, and check it against the action-return rules in
`wfpy-actor-authoring`.
