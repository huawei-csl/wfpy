---
name: wfpy-actor-authoring
description: Write one wfpy node correctly - a @task, @tool, @source or @viewer, its fields, ports and actions. Covers the rules that fail silently: a returned dict is a port map, a field with a default is state not a parameter, File ports need ext=, a @source emits a File object. Use when adding or fixing a single actor. Triggers: write a task, add an actor, @task, @tool, @source, @viewer, Port, action, produces, consumes, actor state, constructor parameter.
---

# Writing one wfpy actor

Each rule here is a silent failure, not an exception. The evidence is given so you can
check it rather than trust it.

## Fields: a default decides what the field *is*

```python
@task
class Prepare:
    run_mode: str          # no default -> CONSTRUCTOR PARAMETER: Prepare(run_mode="sim")
    timeout_s: int = 900   # default     -> PER-ACTOR STATE, not a parameter
    _n: int = 0            # leading _   -> per-actor state
```

`Prepare(run_mode="sim")` against `run_mode: str = "sim"` raises
`TypeError: got unexpected parameter(s)`. **A parameter with a default is not expressible.**
Decide which you want and write it that way.

**Never use a mutable default.** `_history: list = []` is one object shared by every
instance of the class. Use a string accumulator, or build the container in the first action
that needs it.

## Actions: a returned dict is a port map

With a **single** declared output port, wfpy does `result.get(port_name)` on a returned
dict. So this emits **nothing at all**, with no error — the run just reaches quiescence
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
others waiting forever. If a port's value is sometimes unavailable, emit a fallback — write
a stub file, or a note — rather than skipping it.

Use **frozen dataclasses as tokens**, not dicts. Non-resource ports pass values through
untouched, so a dataclass arrives as itself; a dict invites the rule above.

## Ports

```python
class Ports:
    Kernel = Port[str](direction="in")
    Report = Port[File](direction="out", ext=".md")
```

- `Port[...]()` defaults to `direction="inout"`; name-based heuristics then guess, so
  **always state the direction**.
- **Declare `ext=` on every `File` port.** It names the materialized copy. Without it the
  copy can land with no extension, and what the IDE hands to an editor or a viewer command
  is that filename — so a correct run opens as plain text. `ext` also reaches an agent's
  prompt as `Port (format: json)`.
- An output port **broadcasts**: wiring it to three consumers gives each its own copy of
  the token. An input port does **not** fan in — see `wfpy-graph-composition`.
- One firing emits exactly **one** token per output port.

## Guards must be mutually exclusive

wfpy chooses nondeterministically among actions whose guards all pass. Declaration order
happens to break the tie in the current runtime, but the model does not promise it, so an
unguarded action beside a guarded one is a latent bug.

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
is called to *test* the action, not to fire it.

## Choosing the decorator

- **`@task`** for anything that can fail. A `@tool` raises on non-zero exit and aborts the
  whole graph, which prevents a gate from routing the failure into a repair loop. Run the
  fallible thing as a subprocess inside a `@task` and return its outcome as a token.
- **`@source`** for the head of a run: no inputs, so nothing can trigger it and nothing can
  stop it — wfpy writes the emit-once latch. Its `path` is a **parameter**, so one class
  serves many files.
- **`@viewer`** is a sink: no outputs, no action of its own, and it opens a file **on
  disk**. An upstream task must have written that file, so the port is `Port[File]`, never
  a string that only exists in memory. A viewer class holding no state can be instantiated
  several times, once per artifact to show.

## A @source emits a File object, not a path

`wfpy/types.py` says a `File` value is "simply the resolved path string", but that is only
true of tokens the runtime *materialises*. A `@source` emits its configured `path` as the
`File` **dataclass**:

```
TypeError: argument should be a str or an os.PathLike object where
__fspath__ returns a str, not 'File'
```

This is deliberate: the runtime recognises a re-emitted configured path and leaves it
alone, so a source file's sibling imports keep resolving instead of being copied into a
work dir. **Coerce with `Path(str(token))` in every node that can receive a file token** —
`File.__str__` returns its path, so that handles `File`, `str` and `Path` alike. A
`Port[File]` output can legitimately feed a `Port[str]` input once the consumer coerces.
