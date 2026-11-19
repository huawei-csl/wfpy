---
name: wfpy-actor-authoring
description: Write one wfpy node correctly - a @task, @tool, @source or @viewer, its fields, ports and actions. Covers the rules that fail silently: a returned dict is a port map, a field with a default is state not a parameter, File ports need ext=, a @source emits a File object. Use when adding or fixing a single actor. Triggers: write a task, add an actor, @task, @tool, @source, @viewer, Port, action, produces, consumes, actor state, constructor parameter.
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

## Guards must be mutually exclusive

wfpy tries a task's actions in the order they are declared and fires the first one whose
tokens are there and whose guard passes (a `Priority` inner class reorders them). An
action declared above another therefore shadows it whenever both could fire, and an
unguarded action shadows every action below it that reads the same port. Do not lean on
the order: write guards that cannot both pass.

Derive the exits from **one pure routing function** rather than four conditions that have
to agree:

```python
def _route(self, outcome) -> str:
    """Pure: a guard may be evaluated more than once. Mutates nothing."""
    if not self._started:            return "first"
    if outcome.ok:                   return "accept"
    if self._tries < self.budget:    return "retry"
    return "give_up"

@action(consumes={"In": 1}, produces={"Done": 1})
@guard(lambda self, outcome: self._route(outcome) == "accept")
def accept(self, outcome): ...
```

Exclusivity is then structural. A guard that mutates state will corrupt the run, because it
is called to *test* the action, not to fire it, and it is called again on every pass of
the scheduler while the token waits (measured: three calls for one token).

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
