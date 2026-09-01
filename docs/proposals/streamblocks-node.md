# Proposal: the StreamBlocks node

**Status:** proposal — nothing here is implemented.
**Affects:** wfpy (most of it), wfpy-ide (vocabulary and one setting), and
dialogram (three small changes).

A node that stands for a StreamBlocks/CalPy dataflow design inside a wfpy
workflow. Double-clicking it opens that design in the CalPy GLSP viewer. It comes
in two **facades** — two variants of one node kind, one per node, chosen at
declaration:

| facade | what it is | double-click target |
| --- | --- | --- |
| `design` | an ordinary task that produces or works on a design | the produced artifact — **only after a run** |
| `instance` | a declared `@network` Python file, compiled and executed | the declared file — always; **`network=` is required**, else the node is an error |

## What already exists

Worth stating first, because it is most of the feature.

**The double-click is a decorator argument, not a mechanism.** `@viewer` already
supports `action="openWith"` with a `viewType`, and streamblocks-ide registers
`calpy.networkDiagram` as a custom editor for `*.py`. `viewer-mouse-listener.ts`
turns the annotation into a `vscode.openWith` navigation.

**"Only available after a run" is already the behaviour.** `@viewer` resolves its
target from the *last token* on an input, through a run overlay. With no run it
fails with a written, actionable message telling the user to run the workflow
first. The `design` facade inherits exactly the lifecycle it needs.

**The loop is an existing idiom.** `examples/12_repair_loop.py` is
generate-then-optimise-until-good: an agent seeds, a task with `Start`/`Back`
inputs and `Again`/`Final` outputs iterates, and `connect(x.Again, x.Back)` closes
the feedback edge. Its docstring already notes that making the refiner an
`@agent` changes nothing about the wiring. Nothing needs building for this.

**Folder ports need no new type.** `Resource.kind` already documents `"folder"`
beside `"file"`, so a project-folder port is `Port[Resource(kind="folder")]`.

**Executable resolution has a pattern.** `getSidecarCommand` reads a VS Code
setting under the product's namespace with a default; that is how both sidecars
are already located.

## Why not extend `@viewer`

`@viewer` is sink-only by **execution**, not by convention:

```python
def _step_viewer(actor, plan, verbose):
    """Consume tokens from viewer inputs (sink — no output)."""
    for port_name, queues in actor.in_queues.items():
        for q in queues:
            while q.size() > 0:
                token = q.dequeue()   # drained; nothing produced
```

A viewer never fires an action and never emits. Both facades have outputs, so as
a viewer the runner would swallow their inputs and produce nothing.

The *behaviour* should still be reused. `viewer-mouse-listener.ts` keys on a
`viewer` **annotation**, not on the node's kind — the only thing binding the two
is one line in the exporter:

```python
if rec.meta.annotations.get("viewer") and rec.meta.kind == "viewer":
```

So: new kind, reused annotation, and drop that kind check. Today "openable in an
editor" is welded to "is a runtime sink" for no reason; separating them is what
makes the reuse legitimate rather than a trick.

## wfpy

### The decorator

Mirrors `@viewer`'s shape — `task(cls)`, then set `meta.kind` and add to the open
`meta.annotations` bag.

```python
@streamblocks(facade="design", network="designs/fir.py")
class FirDesign:
    class Ports:
        project = Port[Resource(kind="folder")](direction="in")
        out     = Port[Resource(kind="folder")](direction="out")

    @action(consumes={"project": 1}, produces={"out": 1})
    def refine(self, project): ...

@streamblocks(facade="instance", network="designs/fir.py")
class FirRun:
    class Ports:
        samples = Port[list[int]](direction="in")
        build   = Port[Resource(kind="folder")](direction="out")   # versioned per firing
        results = Port[list[int]](direction="out")
```

It emits two annotations: its own `{facade, network}`, and a `viewer` annotation
carrying `action="openWith"`, `viewType="calpy.networkDiagram"` and the target
source (below).

### Execution

- **`design` dispatches to `_step_internal`.** It is an ordinary task: the user
  declares the ports and writes the actions. wfpy imposes no port shape, so the
  four-port feedback form and the plain one-in-one-out form are equally available
  and it is the user's choice which to use.
- **`instance` runs the network file as a subprocess**, using an interpreter from
  a setting (below). A CalPy network is just a Python file, so wfpy never imports
  CalPy and takes no dependency on its toolchain.

### The exporter

Drop the `kind == "viewer"` condition so any node carrying a `viewer` annotation
exports it, and emit `kind: "streamblocks"` plus the annotations.

## wfpy-ide

- `streamblocks` added to `CreateNodeTypeKind` and the create-node strings.
- A setting for the interpreter that has the StreamBlocks compiler, following
  the `getSidecarCommand` pattern:

  ```
  workflow.streamblocks.pythonPath      default: "python"
  ```

  Pointing it at a venv is how the compiler is found. No new mechanism.
- The StreamBlocks logo passed through `DiagramProfile.clientAssets`.

## dialogram

Three changes, one of them real.

1. **`isExternalNodeKind` gains `'streamblocks'`.** One line, and it does two
   jobs: the node renders as an external actor, *and* it satisfies the
   `node.type !== NODE_EXTERNAL_ACTOR` guard that gates annotation handling in
   `viewer-mouse-listener`. Without it the double-click silently does nothing.
2. **A declared-source branch in the viewer listener.** Today `openWith` always
   resolves through a last token. `instance` has no token — its target is the
   declared `network=` path — so the annotation needs `source="declared"` beside
   the implicit `source="token"`. This is the only new logic in the platform.
3. **A colour, and a consumer-supplied icon.** The colour keys on the neutral
   `streamblocks` kind, which is a kind name and not a brand, so it is legal in
   core.

   The **logo is not**. Dialogram enforces product neutrality: gate 1 forbids
   product tokens in core `src` and gate 3 forbids branded filenames, so
   `sb-wave.svg` cannot ship in the platform. The icon must come from the shell
   through `clientAssets`, with the platform rendering whatever icon a profile
   gives it. That is a real constraint, not a preference — an icon added to core
   fails CI.

## `network=` is required for `instance`

An `instance` with no network has nothing to compile, nothing to run and nothing
to open. It is not a node waiting for input; it is a node that cannot work. So it
is a **static error on the node**, red in the diagram, not a failure discovered
at run time.

The mechanism exists and is the right one. `node.meta['diagnostics']` carries
`{severity: 'error' | 'warning'}` entries, and the model source is explicit that
these are durable where run markers are not:

> a graph-export diagnostic is a property of the source, not run state, so it
> must survive that

which is exactly the distinction here — the missing `network=` is wrong in the
file, whether or not anything has run. The exporter emits the diagnostic; the
node goes red; the double-click reports the same reason rather than silently
doing nothing.

`design` stays optional-by-default: a design node that has not produced anything
yet legitimately has nothing to open, and the existing "no run overlay found"
message already says so.

## Versioned artifact folders

`instance` gains a build-artifact folder on an output port, and that is also
where a `design` facade generates. Which raises the real question: **a loop that
modifies the network produces several versions of it in one run**, and they must
not overwrite each other.

### The token is the version

Nothing needs inventing. wfpy already names per-firing artifacts by the actor's
fire count:

```python
out_dir / f"{actor.name}__{port_name}__{actor.fire_count}{ext}"
```

Extend that from files to folders and versioning falls out of the dataflow model
itself, because in a dataflow graph **each firing already produces its own
token** — a folder-typed port simply makes that token a directory:

```
wf-out/
  Design__out__0/      generated
  Design__out__1/      after the first optimisation pass
  Design__out__2/      after the second
```

`Resource` already normalises `dir` and `directory` to `folder`, so the type side
needs nothing either.

### What this buys, without extra machinery

- **The viewer opens the current design for free.** Double-click resolves the
  *last* token, which is the newest folder. No "which version am I looking at"
  question, and no bookkeeping to keep it right.
- **Every iteration survives** for comparison — and `@viewer(action="diff")`
  already takes two inputs, so diffing pass N against pass N-1 is wiring, not a
  feature.
- **Nothing is mutated in place**, so a failed optimisation cannot corrupt the
  last good design.
- **Provenance is legible on disk.** Folder N came from firing N; the loop's
  history is the directory listing.

### The cost, stated plainly

A whole project folder per iteration. For a loop of any length that is real disk,
and the honest mitigations are, in order of preference:

1. **Copy-on-write** (`cp --reflink=auto`) where the filesystem supports it —
   near-free, and degrades to a plain copy where it does not.
2. **Hard-link the unchanged files**, copying only what the pass rewrote.
3. **Materialise a new folder only when the design actually changed**, so an
   idempotent pass costs nothing.

Retention has a home already: the `@keep` annotation marks task outputs as kept
rather than cleaned up after a run, so "keep every iteration" versus "keep the
last" is an existing switch rather than a new setting.

## Open question

Where does loop termination live? In `examples/12` it is the task's own
`score >= target`. With the design node an ordinary task, the test can sit in it,
in a separate checker task, or in the optimising agent emitting on one of two
ports. All three work; it should be a deliberate choice rather than a default.
