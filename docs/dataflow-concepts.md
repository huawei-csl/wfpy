# Dataflow concepts

wfpy orchestrates agents and tools as a **dataflow graph** rather than as a
script. This page explains that model. It assumes no dataflow background, and
every construct it describes is exercised by a runnable example under
[`examples/`](../examples/).

In wfpy you describe a system of **actors** that pass **tokens** to each other
along FIFO channels, and the runtime decides when each actor runs. It is an
actor-dataflow model, expressed entirely in Python decorators and run directly.

## Why a graph instead of a script

A script runs your steps in the order you wrote them. A dataflow graph
*describes* how components are connected and lets the runtime decide when each
runs. For agent orchestration that buys four things:

- **Independent work runs concurrently for free.** Two agents that don't depend
  on each other run at the same time, with no threading code.
- **Branching and repetition are ordinary structure.** Routing is a guard;
  looping is a feedback edge; "wait for all of these" is a join. No control-flow
  scaffolding around your calls.
- **Actors keep state between firings.** A node is a long-lived object, so an
  agent that needs conversation memory just keeps it in a field.
- **The graph is data.** `wfpy plan` exports it as JSON — the IDE draws and edits
  it. A script can't be drawn.

The cost is thinking about *when* a node runs instead of writing the order down.
The rest of this page is that model.

## The model in one minute

- Nodes are **actors** — instances of a `@task`, `@agent`, or `@tool` class.
- Actors have typed **ports** — named inputs and outputs.
- Edges are **queues**: FIFO, holding **tokens** (values in transit).
- An actor performs its work in **firings**. A firing runs one **action**, which
  consumes tokens from input ports, may update state, and produces tokens on
  output ports.
- An action fires only when its declared inputs are present (and its guard, if
  any, is true). The scheduler keeps firing whatever is ready.

Nobody calls anybody. The availability of tokens is what causes work to happen.

## Actors, ports, and actions

```python
from wfpy import task, action, Port

@task
class Doubler:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def double(self, x: int) -> int:
        return x * 2
```

An **action** declares its token rates explicitly:

- `consumes={"In": 1}` reads one token from `In` per firing and binds its value
  to a parameter. It is also a *firing condition*: no token, no firing. Consumed
  tokens bind to the method's parameters **by position, in port order** — the
  parameter names are documentation, not a lookup key.
- `produces={"Out": 1}` emits one token on `Out`, valued by the return.

Ports default to `direction="inout"`; wfpy also treats a port named `out`,
`output`, `result`, `report`, or `summary` as an output. Declaring `direction=`
is clearer, and the examples always do.

**Always decorate actions with `@action`.** A bare method named `action` is
inferred for convenience, but it only produces output when it carries a return
annotation — a silent trap. `@action` is explicit and never surprising.

### Producing on several ports, or none

An action produces exactly the ports in its `produces=`. For several ports,
return a **tuple in port-declaration order** (a dict keyed by port name, or a
list by position, also work):

```python
@action(consumes={"In": 1}, produces={"P": 1, "N": 1})
def split(self, x: int):
    return (x, -x)          # P <- x, N <- -x
```

An action that produces **nothing** — a sink, or a state-only step — simply has
no `produces=` and returns `None`:

```python
@action(consumes={"In": 1})            # no produces -> emits nothing
def record(self, x: int) -> None:
    self._log.append(x)
    return None
```

## Guards

A **guard** is a firing condition beyond token availability — a predicate on the
token values, the actor's state, or both. It receives the same arguments the
action would, and *peeks* without consuming: if it's false, the token stays for
another action.

```python
from wfpy import action, guard

@action(consumes={"In": 1}, produces={"P": 1})
@guard(lambda self, x: x >= 0)
def positive(self, x: int) -> int:
    return x

@action(consumes={"In": 1}, produces={"N": 1})
@guard(lambda self, x: x < 0)
def negative(self, x: int) -> int:
    return x
```

`@action` on the outside, `@guard` directly above the method. This is how you
route on data without a control node — several actions, each guarded (see
[`04_guarded_actions.py`](../examples/04_guarded_actions.py)).

## State versus parameters

Because wfpy is plain Python, state is just a field — no special type wrapper.
Annotated fields are classified by whether they have a default:

```python
@task
class Accumulator:
    step: int          # no default        -> parameter, fixed at construction
    total: int = 0     # has a default     -> state, persists across firings
    _seen: int = 0     # leading underscore -> state
```

**Parameters** have no default and are passed when you construct the actor
(`Accumulator(step=2)`). **State** fields have a default (their initial value)
and persist for the actor's life, across every firing — where a running tally,
or an agent's history, lives.

A consequence worth remembering: giving a field a default makes it *state*, so a
"parameter with a default" is not expressible — parameters are required.

## Action selection

An actor with several actions may have more than one ready at once. Which fires
is resolved in three stages:

1. **Eligible** — allowed by the schedule (if any).
2. **Activated** — eligible, *and* enough tokens are present, *and* all guards
   are true.
3. **Firable** — activated, and no higher-priority activated action exists.

Any firable action may fire. If several are firable, the choice is
**nondeterministic** — the model does not specify it. (This runtime happens to
break ties by declaration order, but relying on that is what schedules and
priorities are for.)

### Schedules

A **schedule** sequences actions with a finite state machine — a nested
`Schedule` class with an initial state and `(state, action, next_state)`
transitions. It makes an otherwise nondeterministic actor deterministic:

```python
class Schedule:
    initial = "s1"
    transitions = [("s1", "a", "s2"), ("s2", "b", "s1")]  # strict alternation
```

### Priorities

A **priority** ranks actions so a definite one wins when several are activated —
a nested `Priority` class whose `rules` list orders action names high-to-low.
The idiomatic use is a deterministic case-statement with overlapping guards (see
[`07_priorities.py`](../examples/07_priorities.py)).

## The scheduler

**wfpy runs ready actors in parallel by default** — each round, every actor that
can fire is dispatched to a thread pool. Pass `max_workers=1` for sequential,
reproducible firing order (useful in tests and when debugging).

```python
run(my_workflow, inputs={"In": 1})                 # parallel (default)
run(my_workflow, inputs={"In": 1}, max_workers=1)  # sequential
```

For agent orchestration this is the main event: fanning out to five agents and
joining their results needs no concurrency code, because five actors became
ready in the same round.

## Building networks

A **workflow** (`@workflow`) instantiates actors and `connect(source, target)`s
their ports. It may declare boundary ports with `@workflow(inputs=..., outputs=...)`
— seeded and read via `run(inputs=...)` — or be *closed*, with its sources and
sinks as actors inside.

- **Feeding a stream.** `run(inputs=...)` seeds **one token** per port. To inject
  a *sequence*, use `loop([...])`, which emits one token per item, or a source
  actor (below).
- **Sources.** A task with no input ports whose action fires on its own. A
  zero-input action must declare `consumes={}` **explicitly** (an omitted
  `consumes` means "infer from inputs" and never fires). A state guard makes it
  terminate:

  ```python
  @action(consumes={}, produces={"Out": 1})
  @guard(lambda self: self.sent < self.n)
  def emit(self) -> int: ...
  ```

- **Sinks.** A task with no output ports, whose action has no `produces=`.
- **Fan-out.** Connect one output to several consumers; each gets its own copy
  of every token. Fan-out is a property of *outputs*.
- **Feedback.** An edge from a later actor back to an earlier one, seeded by a
  delay ("initial token") actor — this is how a running-sum ring, or an agent
  generate→check→revise loop, is built. See
  [`08_networks.py`](../examples/08_networks.py).

## Agents are actors

An `@agent` is a task: it has ports, fires when its inputs are ready, keeps state
the same way, and is wired with the same `connect()`. Only the firing body
differs — it calls a model instead of running your Python. So everything above
applies to agents: guards route agent output, schedules sequence multi-step
agents, feedback builds repair loops.

`transport="mock"` runs an agent with no model, network, or credentials — it
emits `mock_outputs` (synthesising any undeclared port from its type), so an
agent graph is runnable and testable in CI. Swapping to a real transport changes
nothing else about the graph. See
[`09_agents_are_actors.py`](../examples/09_agents_are_actors.py).

## Beyond the core model

The tasks, guards, schedules, and priorities above are the dataflow core. wfpy
layers orchestration constructs on top of it: `@agent` (above), `@tool` for
external processes, `@viewer` for inspecting a run, `@context` and `@config`,
and the `if_()` / `loop()` control nodes. See the README for that surface.

## Sharp edges

Behaviours that are easy to trip over, all verified against the current version.

- **Always use `@action`.** A bare `def action` with no return annotation
  produces nothing, silently. `@action` (with explicit `consumes`/`produces`)
  never surprises.
- **A zero-input action needs `consumes={}` explicitly.** Omitting `consumes`
  means "infer from input ports"; such an action never fires on its own when the
  actor has any input port.
- **Inputs do not fan in.** An input port reads from a single source; a second
  edge into the same port is silently ignored. To merge streams, give the actor
  one input port per source (a join) — see 03 and 06.
- **No backpressure.** Queues are unbounded, so a feedback loop must
  *self-terminate* (a guard eventually stops production). An endless generator
  runs forever.
- **Parameters can't have defaults.** A default makes the field state. Parameters
  are required at construction.
- **Action parameters bind by position, not name.** Keep the parameter order
  aligned with the port order in `consumes`.
