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
    runner, decide = RunKernel(run_mode="sim"), Decide(budget=2)
    connect("Kernel", runner.Initial)       # a string names a boundary port
    connect(decide.Done, writer.Done)
    connect(writer.Report, "Report")

# and in the caller:
flow = repair_flow()
connect(kernel.Out, flow.Kernel)
connect(flow.Report, view.Report)
```

**Declare `ext=` on boundary outputs too, not only on the producing port.** At a
sub-workflow boundary the declared ext is the only thing that names the materialized copy;
the source path's own suffix is not consulted. Measured: a writer's own copy came out
`writer__Report__0.md` (its port declared `ext=".md"`), while the boundary re-export of the
same file came out `flow__Report__0` — no extension — because the boundary output was
declared as a bare `File`. Ports declared `File(ext=".json")` came out `.json` at both levels.

A phase earns a sub-workflow when it has its own inputs and outputs and could be swapped
out. A step that only transforms one token is a task. Nesting one task is noise; nesting a
four-actor measurement phase that two top-level workflows share is the point.

## Input ports do not fan in

An output port broadcasts to every consumer. An input port accepts **one** producer. Two
`connect()` calls into the same input port is the single most common wiring mistake.

So a loop head needs **two input ports on one actor**:

```python
@task
class RunKernel:
    class Ports:
        Initial = Port[str](direction="in")     # from the boundary
        Retry   = Port[str](direction="in")     # from the feedback edge
        Result  = Port[Outcome](direction="out")

    @action(consumes={"Initial": 1}, produces={"Result": 1})
    def from_initial(self, kernel): return self._run(kernel)

    @action(consumes={"Retry": 1}, produces={"Result": 1})
    def from_retry(self, kernel): return self._run(kernel)
```

When the thing in the loop is a **sub-workflow** — whose boundary has one `Kernel` input —
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
`@agent` only has the AND form — see `wfpy-agent-nodes`.

## A loop has no backpressure, so it must stop itself

A feedback edge is just an edge. Nothing throttles it, and nothing ends it. **Every cycle
needs a counter that only goes up**, checked by the guard that re-enters the loop:

```python
budget: int                        # a parameter
_rounds: int = 0                   # state

@action(consumes={"Result": 1}, produces={"Retry": 1})
@guard(lambda self, r: self._route(r) == "retry")
def ask_again(self, r):
    self._rounds += 1              # increment on the way round, not in the guard
    ...
```

Independent cycles need independent counters. If a loop can re-enter for two different
reasons — a slower candidate versus one that failed to build — give each its own budget,
and bound the total deliberately (`rounds × repairs`). A node with **no input at all**, or
`fireable_without_input`, is a cycle too: it has nothing to stop it and needs a latch or a
budget just the same.

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

`kind` distinguishes `source` / `internal` / `agent` / `viewer` / `workflow`, so a
sub-workflow you expected to nest shows as `workflow` and a node you forgot to wire shows
with no edges. Counting `"kind"` occurrences in the raw text does **not** give a node count
— parse `graph.nodes`.

## When a run does nothing

Quiescence with no error means some port never received a token. `run.wf-queues.json` in
the run directory shows each queue's size, so a producer that emitted nothing is
immediately visible. Work backwards from the first empty queue to the action that should
have filled it, and check it against the action-return rules in `wfpy-actor-authoring`.
