# Examples

A tutorial-ordered introduction to wfpy's dataflow programming model. Each file
introduces one idea, is runnable, and is executed by `tests/test_examples.py` on
every test run — so an example that stops working fails CI rather than rotting.

Read [`docs/dataflow-concepts.md`](../docs/dataflow-concepts.md) alongside these;
the examples use its vocabulary (actor, token, firing, guard) without
re-explaining it.

Every example runs two ways:

```bash
python examples/01_simple_task.py            # runs it, prints the result
wfpy run examples/01_simple_task.py --input In=21   # same graph, via the CLI
wfpy plan examples/01_simple_task.py --format graph # the graph as JSON
```

Nothing here needs credentials, a network, or an optional dependency — the one
agent example (09) uses the offline `transport="mock"`.

## The dataflow model

Plain `@task` actors, so the semantics stay in the foreground. Everything here
applies unchanged to agents, because an `@agent` *is* an actor (09).

| | Example | Introduces |
|---|---|---|
| 01 | [`01_simple_task.py`](01_simple_task.py) | Ports, `@action(consumes=, produces=)`, tokens, firings |
| 02 | [`02_pipeline.py`](02_pipeline.py) | Chaining tasks; parameters (annotated field, no default) |
| 03 | [`03_streams_and_nondeterminism.py`](03_streams_and_nondeterminism.py) | Several actions per task; `loop()` streams; nondeterminism |
| 04 | [`04_guarded_actions.py`](04_guarded_actions.py) | `@guard` — firing conditions on values and state |
| 05 | [`05_state.py`](05_state.py) | State fields (annotated field *with* a default) |
| 06 | [`06_schedules.py`](06_schedules.py) | `class Schedule` — an FSM sequencing a task's actions |
| 07 | [`07_priorities.py`](07_priorities.py) | `class Priority` — deterministic action ordering |
| 08 | [`08_networks.py`](08_networks.py) | Sources, sinks, fan-out, and self-terminating feedback |
| 09 | [`09_agents_are_actors.py`](09_agents_are_actors.py) | An `@agent` is a task; the offline `mock` transport |

## Agent orchestration

The patterns you reach for when wiring LLMs together. Each runs offline via
`transport="mock"` and is CI-tested; swap in a real transport unchanged.

| | Example | Pattern |
|---|---|---|
| 10 | [`10_parallel_agents.py`](10_parallel_agents.py) | Fan out to parallel specialists, reduce with a task (map-reduce) |
| 11 | [`11_routing.py`](11_routing.py) | Dispatch each request to the right specialist (guard routing) |
| 12 | [`12_repair_loop.py`](12_repair_loop.py) | Generate → check → revise until good (feedback loop) |

## Reading order

01 → 02 establish tasks, ports, and wiring. 03 introduces multiple actions (and
the nondeterminism that motivates 06 and 07). 04 adds guards, 05 adds state — the
two things that make an actor more than a function. 06 (schedules) and 07
(priorities) are the two ways to make action selection deterministic. 08 is the
network-level view: sources, sinks, fan-out, feedback. 09 is the payoff — an LLM
agent slotted into the same model. 10–12 then apply that model to
multi-agent orchestration: fan out and reduce, route, and loop.
