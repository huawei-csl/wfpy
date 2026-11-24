---
name: wfpy-actor-authoring
description: Write one wfpy node correctly - a @task, @tool, @source or @viewer, its fields, ports and actions, and the Schedule and Priority that say which action fires. Covers the rules that fail silently: a returned dict is a port map, a field with a default is state not a parameter, File ports need ext=, a @source emits a File object, an action left out of a schedule never fires. Use when adding or fixing a single actor, or when a task has several actions. Triggers: write a task, add an actor, @task, @tool, @source, @viewer, Port, action, guard, schedule, priority, action selection, state machine, produces, consumes, actor state, constructor parameter.
---

# Writing one wfpy actor

Most rules here are silent failures, not exceptions. The evidence is given so you can
check it rather than trust it.

## Fields: a default decides what the field *is*

```python
@task
class Prepare:
    run_mode: str          # no default -> CONSTRUCTOR PARAMETER: Prepare(run_mode="sim")
    timeout_s: int = 900   # default     -> PER-ACTOR STATE, not a parameter
    _n: int = 0            # leading _   -> per-actor state
```

`Prepare(run_mode="sim", timeout_s=5)` raises
`TypeError: Prepare() got unexpected parameter(s): 'timeout_s'`, and `Prepare()` raises
`TypeError: Prepare() missing required parameter 'run_mode'`. **A parameter with a default
is not expressible.** Decide which you want and write it that way.

State is per instance: each instance starts from its own deep copy of the default, so
`_history: list = []` is not shared between two nodes of the class, as a plain class
attribute would be. What the default cannot do is differ between two nodes; that takes a
parameter.

## Actions: a returned dict is a port map

With a **single** declared output port, wfpy does `result.get(port_name)` on a returned
dict. So this emits **nothing at all**, with no error, and the run reaches quiescence
early:

```python
@action(consumes={"Kernel": 1}, produces={"Job": 1})
def prepare(self, kernel: str) -> dict:
    return {"kernel": kernel, "name": "x"}      # WRONG: no "Job" key -> emits nothing
```

Three correct shapes:

```python
return JobSpec(kernel=kernel, name="x")         # a frozen dataclass token
return {"Job": JobSpec(...)}                    # explicit port map
return str(path)                                # a plain value, for a scalar port
```

With **several** output ports a dict *is* the port map, and every declared port must get a
key on every firing. A firing that fills only some of its ports leaves the consumers of the
others waiting forever. If a port's value is sometimes unavailable, emit a fallback (a stub
file, or a note) rather than skipping it. Two more shapes are accepted with several ports,
and neither says which port gets what: a tuple or a list is handed out by position, in the
order `produces` names the ports, and any other value is sent to **every** port. Return
the dict.

Use **frozen dataclasses as tokens**, not dicts. Non-resource ports pass values through
untouched, so a dataclass arrives as itself; a dict invites the rule above.

## Ports

```python
class Ports:
    Kernel = Port[str](direction="in")
    Report = Port[File](direction="out", ext=".md")
```

- `Port[...]()` defaults to `direction="inout"`, and the name then decides: `out`,
  `output`, `result`, `report` and `summary` are outputs, every other name is an input.
  A port named `Verdict` with no direction is an input. **Always state the direction.**
  A port without `direction="in"` or `direction="out"` is deprecated: wfpy warns of it
  at the line that declares it (a `FutureWarning`), and wfpy 2.0 will refuse it.
- **Declare `ext=` on every `File` port, and return a path that ends in it.** The copy
  wfpy makes of a file a task returns keeps that path's own suffix, and `ext` names it
  only when the path has none. Measured on ports of one task: `ext=".md"` handed `a` came
  out `writer__Bare__0.md`, `ext=".md"` handed `b.txt` came out `writer__Suffixed__0.txt`,
  and no `ext` handed `c` came out `writer__NoExt__0`. At a workflow boundary `ext` is the
  only thing that names the copy (see `wfpy-graph-composition`). What the IDE hands to an
  editor or a viewer command is that filename, so a correct run with no suffix opens as
  plain text. On an `@agent`, `ext` names the file wfpy writes and reaches the prompt as
  `Port (format: json)`.
- An output port **broadcasts**: wiring it to three consumers gives each its own copy of
  the token. An input port does **not** fan in; see `wfpy-graph-composition`.
- One firing emits exactly **one** token per output port. The count in `produces` does not
  change that: with `produces={"Out": 2}`, a returned list is one token.

## Which action fires: a schedule, then guards, then a priority

A task with several actions may have more than one ready. The model resolves it in three
stages:

1. **Eligible**: allowed by the task's `Schedule` in the state it is in (every action,
   when there is no schedule).
2. **Activated**: eligible, its tokens are there, and its guard passes.
3. **Firable**: activated, with no activated action above it in the task's `Priority`.

When several actions are firable, the model does not say which fires. This runtime takes
the one declared first, and that is not something to build on: moving a method up in the
file would change what the task does. Say the order with the two declarations below, and
keep guards for what depends on the token.

### Prefer a `Schedule` for what happens in sequence

A schedule is a finite state machine on the task: a nested class with an `initial` state
and `(state, action, next_state)` transitions. In a state, only the actions with a
transition out of it can fire, and firing one moves the task to its next state.

```python
@task
class RunKernel:
    class Ports:
        Initial = Port[str](direction="in")      # from the boundary
        Retry   = Port[str](direction="in")      # from the feedback edge
        Result  = Port[Outcome](direction="out")

    class Schedule:
        initial = "fresh"
        transitions = [
            ("fresh",   "from_initial", "looping"),   # the first kernel, once
            ("looping", "from_retry",   "looping"),   # then only what comes back
        ]

    @action(consumes={"Initial": 1}, produces={"Result": 1})
    def from_initial(self, kernel): return self._run(kernel)

    @action(consumes={"Retry": 1}, produces={"Result": 1})
    def from_retry(self, kernel): return self._run(kernel)
```

Reach for it whenever a guard would test a field that only records where the task is
(`_started`, `_phase`, `_done`). Such a field is a schedule written by hand: every action
that moves the task has to set it, and the one that forgets fails without a word. A
schedule is also data: `wfpy plan --format graph` prints it under the node's
`meta.schedule`, so the phases of a task can be read without reading its actions.

A schedule is the latch of a node with no input too. One transition out of the initial
state, `("armed", "emit", "spent")`, and an action that consumes nothing fires once
instead of without end.

### Give a state with several actions a `Priority`

`rules` is a list of groups, each a list of action names from high to low. An earlier
group outranks a later one, and an action in no group ranks below every listed one.

```python
@task
class Decide:
    budget: int                 # a parameter
    _rounds: int = 0            # state: a counter, not a phase

    class Ports:
        Result = Port[Outcome](direction="in")
        Retry  = Port[str](direction="out")
        Done   = Port[Outcome](direction="out")

    class Schedule:
        initial = "deciding"
        transitions = [
            ("deciding", "accept",    "done"),
            ("deciding", "ask_again", "deciding"),
            ("deciding", "give_up",   "done"),
        ]

    class Priority:
        rules = [["accept", "ask_again", "give_up"]]

    @action(consumes={"Result": 1}, produces={"Done": 1})
    @guard(lambda self, r: r.ok)
    def accept(self, r): return r

    @action(consumes={"Result": 1}, produces={"Retry": 1})
    @guard(lambda self, r: self._rounds < self.budget)
    def ask_again(self, r):
        self._rounds += 1
        return r.kernel

    @action(consumes={"Result": 1}, produces={"Done": 1})
    def give_up(self, r): return r          # no guard: what is left
```

Three actions read the same port in the state `deciding`, and the priority makes them a
case statement: the guards may overlap, and the last action needs none. Without it each
guard has to exclude the others by hand (`not r.ok and self._rounds < self.budget`, then
`not r.ok and self._rounds >= self.budget`), and the first one that drifts leaves two
firable actions, or none. A state with one action needs no priority.

`done` has no transition out of it, so the task ends there: one decision a run, and a
result that arrives later is left in its queue. A task that serves a stream sends its
exits back to `deciding` and resets its counter there.

### What a schedule and a priority do not check

- **An action in no transition never fires.** A schedule lists what may fire. Adding an
  action to the class and not to `transitions` adds nothing, with no error.
- **State names are not checked.** A next state that is spelled wrong is a state with no
  way out. Measured with `("ready", "go", "raedy")`: three tokens in, one firing, and the
  task never fired again. An `initial` that no transition starts from is accepted the same
  way, and that task never fires at all.
- What does raise, when the class is made: an action name the class does not have
  (`TypeError: X.Schedule references unknown action 'nope'`, and the same for `Priority`),
  two transitions for one state and action, and `rules` written flat (`["a", "b"]` where
  `[["a", "b"]]` is meant).
- Both are for the actions of a `@task`. An `@agent` has no actions to order.

### Guards

A guard takes the tokens its action would consume, and says whether this action is for
them: `@guard(lambda self, outcome: outcome.ok)`.

**Keep it pure.** A guard is called to *test* the action, not to fire it, and it is called
again on every pass of the scheduler while the token waits (measured: three calls for one
token). One that mutates state corrupts the run. Count in the action, on the way round.

**A guard that raises counts as false.** wfpy logs `Guard <node>.<action> raised; treating
as not fireable` with the traceback and carries on, so a typo in a guard gives a run that
ends without an error and without that action ever firing.

## Choosing the decorator

- **`@task`** for anything that can fail. A `@tool` raises on non-zero exit
  (`RuntimeError: External tool 'sh' (actor 'f') exited with code 3`) and aborts the whole
  graph, which prevents a gate from routing the failure into a repair loop. Run the
  fallible thing as a subprocess inside a `@task` and return its outcome as a token.
- **`@source`** for the head of a run: no inputs, so nothing can trigger it and nothing can
  stop it, and wfpy writes the emit-once latch. Its `path` is a **parameter**, so one class
  serves many files.
- **`@viewer`** is a sink: no outputs, no action of its own, and it opens a file **on
  disk**. An upstream task must have written that file, so the port is `Port[File]`, never
  a string that only exists in memory. A viewer class holding no state can be instantiated
  several times, once per artifact to show.

## A @source emits a File object, not a path

`wfpy/types.py` says a `File` value is "simply the resolved path string", but that is only
true of tokens the runtime *materialises*. A `@source` emits its configured `path` as the
`File` **dataclass**, and `Path(token)` on it raises (the first wording from Python 3.12,
the second before it):

```
TypeError: argument should be a str or an os.PathLike object where
__fspath__ returns a str, not 'File'
TypeError: expected str, bytes or os.PathLike object, not File
```

This is deliberate: the runtime recognises a re-emitted configured path and leaves it
alone, so a source file's sibling imports keep resolving instead of being copied into a
work dir. **Coerce with `Path(str(token))` in every node that can receive a file token.**
`File.__str__` returns its path, so that handles `File`, `str` and `Path` alike. A
`Port[File]` output can legitimately feed a `Port[str]` input once the consumer coerces.
