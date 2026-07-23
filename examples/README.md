# Examples

Runnable, self-contained workflows, ordered so each one introduces a single
idea. Read [`docs/dataflow-concepts.md`](../docs/dataflow-concepts.md) first if
the words *actor*, *token* or *firing* are new — the examples use that
vocabulary without re-explaining it.

Every example runs two ways:

```bash
python examples/01_actor_and_ports.py     # runs it, prints the result
wfpy run examples/01_actor_and_ports.py   # same graph, via the CLI
wfpy plan examples/01_actor_and_ports.py --format graph   # the graph as JSON
```

Nothing here needs credentials, a network, or any optional dependency. Every
file is executed by `tests/test_examples.py` on each run of the suite, so an
example that stops working fails CI rather than rotting quietly.

## Tier A — dataflow fundamentals

Plain `@task` actors, so the semantics stay in the foreground. Everything
learned here applies unchanged to agents, because an `@agent` *is* an actor.

| | Example | Introduces |
|---|---|---|
| 01 | [`01_actor_and_ports.py`](01_actor_and_ports.py) | Actors, typed ports, firing |
| 02 | [`02_pipeline.py`](02_pipeline.py) | Edges are FIFO queues; tokens |
| 03 | [`03_fan_out_and_join.py`](03_fan_out_and_join.py) | Fan-out, join, and concurrency by default |
| 04 | [`04_guards.py`](04_guards.py) | Several actions per actor, selected by `@guard` |
| 05 | [`05_state.py`](05_state.py) | State across firings; `loop()` as a token source |
| 06 | [`06_inspecting_a_run.py`](06_inspecting_a_run.py) | Queue traces: what fired, and what was left over |

## Reading order

01 → 02 → 03 build the model. 04 and 05 add the two things that make an actor
more than a function: choosing between behaviours, and remembering. 06 is the
debugging tool you will want the first time a graph does nothing.
