# Dataflow concepts

wfpy orchestrates agents as a **dataflow graph** rather than as a script or a
chain of calls. If you have used an agent framework before, most of the ideas
here will be familiar in outline and different in the details that matter. This
page explains the model itself. It assumes no dataflow background.

Everything below is runnable against the current version.

## Why a graph instead of a script

A script runs your agents. A dataflow graph *describes* how they are connected,
and lets the runtime decide when each one runs. Four things fall out of that,
and they are the reason wfpy exists:

**Independent work runs concurrently for free.** Two agents that do not depend
on each other run at the same time because nothing says they must not. You do
not write threading code, and you do not write `asyncio.gather`.

**Repetition is data-driven.** An actor fires once per token that arrives, so
"do this for each item" is `loop()` emitting tokens rather than a `for` loop
wrapped around a call, and the actor keeps its state across every firing. A
convergence loop — feed output back until it settles, the shape of an agent
repair loop — is a feedback edge into a dedicated port; see
[Graph shapes](#graph-shapes).

**Actors keep state between runs.** A node is a long-lived object, not a
function invocation, so an agent that needs conversation memory keeps it in a
field.

**The graph is data.** `wfpy plan` exports it as JSON, which is how the IDE
draws and edits it. A script cannot be drawn.

The cost is that you have to think about *when* a node runs instead of writing
the order down. The rest of this page is that model.

## The model in one minute

- Nodes are **actors**. An actor is an instance of a `@task`, `@agent`, or
  `@tool` class.
- Actors have typed **ports** — named inputs and outputs.
- Edges are **queues**. A queue is FIFO and holds **tokens**, which are just
  values in transit.
- An actor **fires** when its input ports have enough tokens. Firing consumes
  those tokens, runs the actor's **action**, and enqueues the result on its
  output ports.
- The scheduler keeps firing whatever is ready until nothing is.

Nobody calls anybody. Availability of data is what causes work to happen.

## Vocabulary

### Actors and ports

```python
from wfpy import task, Port

@task
class Doubler:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    def action(self, x: int) -> int:
        return x * 2
```

`Doubler` is a task *class*; each instance you create inside a `@workflow` is a
separate actor with its own ports, queues, and state.

Ports default to `direction="inout"`. wfpy also infers direction from the name:
a port called `out`, `output`, `result`, `report`, or `summary` is treated as an
output, and anything else defaults to an input. Declaring `direction=` explicitly
is clearer, and this page always does.

### Tokens and queues

A **token** is one value on one edge. A **queue** is the edge itself: FIFO,
holding tokens that have been produced but not yet consumed.

This is the idea that most distinguishes dataflow from a call graph. Producing a
value does not run the consumer. It puts a token in a queue. The consumer runs
later, when the scheduler notices it can.

### Firing

An actor is **fireable** when every input port has at least the number of tokens
that firing will consume. By default that is **one token per input port**. An
action can ask for more:

```python
from wfpy import action

@action(consumes={"In": 3})
def act(self, a: int, b: int, c: int) -> int:
    return a + b + c
```

Firing consumes those tokens — they are removed from the queues — runs the
action, and enqueues whatever it returns.

### Actions

The method that runs on a firing. A class with a single `action` method needs no
decorator; use `@action` when a task has several, and `@guard` to control which
one is eligible.

**Action parameters are bound by position, not by name.** The runtime collects
the consumed tokens in port order and splats them. Parameter names are
documentation; they are not matched against port names. This is the single most
common way to get a working graph that computes the wrong thing — see
[Sharp edges](#sharp-edges).

### Guards

A guard is a predicate that decides whether an action may fire. It receives the
same arguments the action would, and it runs *before* any tokens are consumed:

```python
from wfpy import action, guard

@action(consumes={"In": 1}, produces={"Out": 1})
@guard(lambda self, x: x > 0)
def positive(self, x: int) -> str:
    return f"+{x}"

@action(consumes={"In": 1}, produces={"Out": 1})
def otherwise(self, x: int) -> str:
    return f"-{abs(x)}"
```

`@action` goes on the outside and `@guard` directly above the method. Actions are
tried in declaration order and the first eligible one fires, so an unguarded
action at the end acts as the fallback.

This is how you route without a conditional node: several actions on one actor,
each guarded, and the runtime picks the first eligible one. It is also the piece
that makes an actor a small state machine rather than a function.

### State versus parameters

Annotated fields are classified by whether they have a default:

```python
@task
class Accumulator:
    step: int          # no default        -> parameter, set at construction
    total: int = 0     # has a default     -> state, persists across firings
    _seen: int = 0     # leading underscore -> state
```

Parameters are supplied when you construct the actor (`Accumulator(step=2)`).
State fields persist for the life of the actor, across every firing. That is
where an agent's conversation history or a running tally lives.

## The scheduler

**wfpy runs ready actors in parallel by default.** Each round, every actor that
can fire is dispatched to a thread pool; control nodes (`if_`, `loop`) run in a
sequential phase first.

```python
run(my_workflow, inputs={"In": 1})                  # parallel (default)
run(my_workflow, inputs={"In": 1}, max_workers=1)   # sequential round-robin
```

The difference is real. Two independent actors that each sleep half a second:

| mode | wall time |
|---|---|
| default | 0.53s |
| `max_workers=1` | 1.03s |

For agent orchestration this is the main event: fanning out to five agents and
joining their results needs no concurrency code, because five actors became
ready in the same round.

Use `max_workers=1` when you want deterministic, reproducible firing order —
debugging, or a test that asserts on sequencing.

## Graph shapes

**Chain.** `a.Out -> b.In -> c.In`. The everyday pipeline.

**Fan-out.** One output port connected to several consumers. Every consumer gets
its own token, and they run concurrently. Fan-out is a property of *outputs*: an
output port drives as many edges as you connect to it.

**Join.** Inputs are the opposite — an input port does not fan *in*. It takes
from one source, so combining several producers means an actor with several
input *ports*, one per source. Such an actor only fires once *all* of its ports
have a token, which makes a join a synchronisation point without any extra
machinery. (Pointing two edges at one input port is not a merge; see
[Sharp edges](#sharp-edges).)

**Iteration over a collection.** `loop(items)` emits one token per item, so a
downstream actor fires once per item and carries its state between firings — the
right tool when you know the items up front.

**Convergence loop (feed output back until it settles).** Because an input port
does not fan in, you cannot route feedback into the same port that carries the
initial input. The pattern is **two input ports and two actions**: one port and
action for the first arrival, a second port and action for each fed-back value,
with the loop-back edge landing on the feedback port. Each action decides
whether to emit again or to finish.

```python
@task
class Countdown:
    total: int = 0

    class Ports:
        Start = Port[int](direction="in")    # initial input — one source
        Back = Port[int](direction="in")     # feedback — one source
        Again = Port[int](direction="out")   # wired back into Back
        Done = Port[int](direction="out")

    @action(consumes={"Start": 1}, produces={"Again": 1, "Done": 1})
    def begin(self, n: int):
        self.total += n
        return {"Again": n - 1} if n > 1 else {"Done": self.total}

    @action(consumes={"Back": 1}, produces={"Again": 1, "Done": 1})
    def step(self, n: int):
        self.total += n
        return {"Again": n - 1} if n > 1 else {"Done": self.total}

@workflow(inputs={"In": int}, outputs={"Out": int})
def countdown():
    c = Countdown()
    connect("In", c.Start)
    connect(c.Again, c.Back)   # feedback lands on a separate port, not fan-in
    connect(c.Done, "Out")
```

Two actions are needed because a single action consuming both ports would need a
token on *each* to fire (a join), which is the wrong rule for a loop. Each action
returns a dict naming just one output port, so it emits on `Again` *or* `Done`,
never both. This is the shape of an agent repair loop: generate on `Start`,
inspect, and on failure send the work back through `Again → Back`.

**Conditional and iteration.** `if_()` and `loop()` for branching and bounded
iteration that the graph's shape cannot express on its own.

**Nesting.** A `@workflow` can be used as an actor inside another workflow, so a
subgraph gets reused as a unit.

## Agents are just actors

This is the point of the whole design. An `@agent` has ports, fires on the same
rule, keeps state in the same way, and is wired with the same `connect()`:

```python
from wfpy import agent, Port

@agent(prompt="Summarize the input.")
class Summarizer:
    class Ports:
        In = Port[str](direction="in")
        Summary = Port[str](direction="out")
```

Swap a `@task` for an `@agent` and the rest of the graph does not change. Every
concept above applies unchanged: guards route agent output, feedback edges retry
it, joins wait for several agents, state gives an agent memory.

### Running without a model

`transport="mock"` fires an agent with no model, no network call, and no
credentials, emitting a value on every declared output port:

```python
@agent(prompt="Summarize.", transport="mock",
       mock_outputs={"Summary": "a concise summary"})
class Summarizer:
    class Ports:
        In = Port[str](direction="in")
        Summary = Port[str](direction="out")
```

Use it to test a graph's *shape* — fan-out and join, guard routing, retry
loops, firing order — without spending tokens or making assertions depend on
how a model happens to word things. Ports you do not list are filled in from
their declared type. See the README for details.

## Sharp edges

Things that are easy to get wrong, all of them verified against the current
version.

**A default `action` needs a return type annotation to emit anything.** A task's
plain `action` method (no `@action` decorator) produces output on its ports only
if it has a `-> T` return annotation. Without one, `def action(self, x): return
x * 2` fires, runs, and emits *nothing* — every downstream port receives `None`,
with no error. `def action(self, x) -> int:` works. So does explicit
`@action(produces={...})`, which does not consult the annotation. This is the
single most confusing way to get a silently empty run.

**Action parameters bind positionally.** Names are not matched to ports. If
`Report` declares ports `Score` and `Summary` and its action is
`def action(self, Summary, Score)`, the values arrive in *port* order, not the
order you wrote the parameters. Keep the parameter order aligned with the port
order, and prefer neutral names (`a`, `b`) over names that imply a binding that
does not exist.

**An extra trailing parameter receives a context facade.** When an action's
signature takes exactly one more argument than there are consumed tokens, the
runtime passes a shared-context object as the last argument. A signature that
accidentally has one parameter too many will therefore be handed an object
instead of failing loudly.

**Multi-output tasks need `produces=`.** An action that returns a dict for
several output ports must declare them:

```python
@action(produces={"Doubled": 1, "Tripled": 1})
def act(self, x: int):
    return {"Doubled": x * 2, "Tripled": x * 3}
```

Without `produces=`, the return value is pushed to *every* output port, and a
dict that does not match a port's type becomes `None` — silently, with no error.
If a multi-output task is emitting `None`, this is why.

**`inputs=` seeds exactly one token per port.** `run(wf, inputs={"In": [1,2,3]})`
puts a single token holding the list `[1,2,3]` on `In` — not three tokens. To
feed a sequence, have a source actor emit the items.

**Input ports do not fan in.** Outputs fan out; inputs do not. An input port
takes from a single source, so pointing two producers at one input port is not a
merge — the second edge is simply not read. To combine several sources, give the
actor one input port per source and let it join them (above). The corollary for
loops: never route a feedback edge into the port that carries the initial input,
because that would be fan-in. A convergence loop instead uses a *separate*
feedback port with its own action — the two-port, two-action pattern above.

**Leftover tokens are reported, not fatal.** A run that ends with tokens still
sitting in a queue warns rather than fails. It usually means a join never got
all its inputs, or a consumer was never connected.

## Where next

- `README.md` — API surface, decorators, and the feature matrix
- `wfpy plan <workflow.py> --format graph` — the graph as JSON, which is what
  the IDE renders
- `run(..., queue_trace=True)` (the default) writes `run.wf-queues.json`, a
  record of every token that crossed every edge — the best tool for working out
  why an actor did or did not fire
