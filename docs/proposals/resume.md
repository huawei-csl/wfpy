# Proposal: resuming a run

**Status:** proposal — nothing here is implemented.
**Affects:** wfpy (the runtime and the queue trace); dialogram (the queue-trace
stepper gets a resume button); wfpy-ide (one command id).

A run that fails on its fortieth firing is run again from its first. Every
firing before the failure is repeated: the agents are asked again, the tools
run again, and the time and tokens are spent again, to arrive back at the same
state and try the one firing that failed. This proposes resuming instead:

1. **from a failure** — fix what failed, then continue the run from where it
   stopped, with every completed firing kept;
2. **from a completed step** — continue a run from any firing it finished,
   because what came after it should be done differently.

The first is the common case and the first phase. The second builds on it.

## What already exists

Less than it looks like. Each piece keeps part of the state, and none keeps
enough to continue from.

| artifact | written | what it has | why it is not enough |
| --- | --- | --- | --- |
| `run.wf-run.json` | success and failure | the error message, the failing node, each actor's `fireCount`, agent chat histories and CLI session ids, the `@context` store | on failure `outputs` is `{}`; no token values |
| `run.wf-viewer.json` | success and failure | the **last** token on each edge | the last token, not the queue — a queue holding three tokens shows one |
| `run.wf-queues.json` | **success only** | after each firing: which actor fired, its fire count, and every queue's **size** and **last token**, nested workflows included | no queue *contents* and no actor state — and nothing at all when the run fails |
| `work/` | always (`<run>/work/`, never deleted) | the files tools and agents wrote | nothing says which queue holds which file |

**The IDE already steps through a run.** The Debug cluster in the diagram
(◀ `12/40 · review` ▶) reads `run.wf-queues.json` from the run chosen in the
Runs picker and redraws the queue sizes after firing *n*
(`tryLoadQueueTraceAtStep` in dialogram's `source-model-storage.ts`). The
model of "a run is a sequence of firings, and step *n* is the state after the
*n*-th" is already in both the file and the UI, one step per firing of the
top-level plan. A resume is that same step, made runnable. This proposal
builds on it rather than adding a second record beside it.

`--resume-chat-from` and `--resume-context-from` restore an agent's
conversation and the shared context. The workflow still starts from its first
firing; the agents only remember having done it before.

The workflow instance (`child(instance="wf-out/<run>")`) skips a child workflow
that already ran. It is a checkpoint at a nested-workflow boundary, for a child
with no inputs. It does not help with a failure inside a workflow.

## The state between two firings

Dataflow makes the question well posed. Between two firings, a run is fully
described by:

- **the queues** — every token waiting on every edge, in order, including the
  workflow's output queues and the inputs not yet consumed;
- **each actor's state**:
  - its persistent task fields (`TaskMeta.state_fields`: annotated fields with
    defaults, and `_`-prefixed names);
  - the runtime attributes wfpy keeps on the instance: `_wfpy_schedule_state`
    (the FSM state), `_wfpy_source_emitted`;
  - the `RuntimeActor`'s own: `fire_count`, `agent_fire_budget`,
    `chat_history`, `agent_cli_session_ids`;
- **each control node's state**: `_control_initialized`, `_control_running`,
  `_control_condition_used` / `_control_iter_used`, and a loop's position in
  its iterable — today a live Python iterator (`_loop_iter`), which cannot be
  saved and has to become the materialized iterable plus an index;
- **the scheduler's**: `active_scopes`;
- **the shared context**: `context_store`, `context_journal`,
  `context_version`, `context_commit_seq`;
- **every nested workflow's sub-plan**, recursively — a failure inside a child
  is a failure in the middle of the parent's firing of it.

Nothing else carries over from one firing to the next. A snapshot of exactly
this is a checkpoint, and restoring it is a resume.

## Resuming from a failure

### A failed firing must not lose its inputs

Today a firing removes its tokens before it runs:

- an internal action peeks, checks its guard, **dequeues**, then calls the
  action (`_action_runtime.py`);
- a tool (`_step_external_runtime.py`), an agent
  (`_agent_staging_runtime.py`), a StreamBlocks instance and a nested workflow
  dequeue on entry.

When the action raises, its inputs are gone, and no checkpoint taken after the
failure can bring them back. So the first change is **atomic firings**: peek
the inputs, run, and dequeue only once the firing succeeded.

That is safe as it stands. One actor never fires twice at once (the scheduler
keeps `running_actors`), and every queue has exactly one consumer, so nothing
else can take a peeked token in between. An internal action's guard already
works this way; the commit moves from after the guard to after the action.

A nested workflow is the exception: its tokens are handed to the sub-plan,
which may consume them before failing. Its checkpoint is the sub-plan's own,
taken recursively, and the parent's tokens are already in it.

### Write a checkpoint when a run fails

On failure, after the executor has finished the firings still in flight,
`run()` writes `run.wf-checkpoint.json` beside the run record:

```json
{
  "version": 1,
  "workflowName": "pipeline",
  "sourcePath": "/abs/flows/pipeline.py",
  "graph": "sha256:…",
  "failed": { "actor": "review", "fireCount": 3, "error": "…" },
  "activeScopes": ["scope:root"],
  "queues": {
    "q:draft.Out->review.In": [{ "$wfType": "file", "path": "work/draft__Out__2.md" }],
    "q:review.Out->wf:output:Report": []
  },
  "actors": {
    "draft": {
      "fireCount": 3,
      "state": { "_round": 3 },
      "runtime": { "scheduleState": "refine", "chatHistory": [] }
    }
  },
  "controls": { "each": { "initialized": true, "iterable": ["a", "b", "c"], "index": 2 } },
  "context": { "store": {}, "journal": [], "version": 7, "commitSeq": 7 },
  "subPlans": { "child": { "…": "the same shape, recursively" } }
}
```

The executor already waits for in-flight firings when it shuts down, so the
cut is consistent: every firing either completed (its outputs are in the
queues) or did not (its inputs are still there).

A checkpoint is also written on `KeyboardInterrupt` and on a stop from the
IDE: a stopped run is a run to resume, too.

### Tokens and state are written as JSON

Through the runtime JSON encoder, with type tags for what JSON lacks:

- `File` and `Resource` become `{"$wfType": "file" | "resource", …}`, with the
  path relative to the run directory when it is inside it;
- a set becomes `{"$wfType": "set"}`, as the encoder already writes it;
- a dataclass becomes `{"$wfType": "dataclass", "type": "module:Qualname"}`.

A token or a state value that cannot be written makes the checkpoint **not
resumable**. The file is still written, naming each value that could not be,
so the reason is visible. Pickle is not used: a checkpoint is read back by a
newer version of the source, which is the point of resuming, and pickle
breaks first exactly there.

### Resume

```bash
wfpy run flows/pipeline.py --resume-from wf-out/20261001-142201
```

```python
run(pipeline, resume_from="wf-out/20261001-142201")
```

A resumed run:

1. builds the plan from the **current** source, as any run does;
2. checks it against the checkpoint: same workflow, and the same **graph** —
   actor names and kinds, and queue ids. A changed action body is expected;
   that is what was fixed. A changed graph is refused, naming what changed,
   because there would be no meaningful place to put the saved tokens;
3. restores the queues, the actor and control state, the scopes, the context
   and the sub-plans;
4. restores chat histories and context as `--resume-chat-from` and
   `--resume-context-from` do today, without being asked to;
5. runs the scheduler from there.

The resumed run is a **new run** with its own id and directory. Its record
says `"resumedFrom": "<run id>"`, so a chain of resumes can be followed back.
It takes no `--input`: the inputs were consumed into the checkpoint's queues.

File tokens keep pointing into the earlier run's `work/` directory. That run
has to stay where it is, which it does: nothing deletes it. `--copy-work`
copies it into the new run when it should not have to.

## Resuming from a completed step, through the queue trace

Restoring the state **after** firing N needs that state, and the failure
checkpoint has only the last one. Writing a full checkpoint after every firing
is too expensive for a run with large tokens. The queue trace is already
written once per firing; it lacks only what changed **in** the firing.

### The trace becomes a journal (version 2)

Each step keeps what it has now and gains what the firing did:

```json
{"step": 12, "actorInstanceName": "review", "actorKind": "agent", "actorFireCount": 3,
 "queueSizes": [ "…as today…" ],
 "consumed": {"q:draft.Out->review.In": 1},
 "produced": {"q:review.Out->wf:output:Report": [{"$wfType": "file", "path": "work/review__Out__3.md"}]},
 "state": {"_round": 4},
 "runtime": {"scheduleState": "done", "chatHistory": "…appended turns only…"}}
```

- `consumed` is a count per queue. The values are already in an earlier
  step's `produced`, or in the workflow inputs, so they are not written twice.
- `produced` is the tokens the firing enqueued, encoded as checkpoint tokens
  are.
- `state` and `runtime` are the actor's state after the firing, written only
  when it changed.

A step is recorded under the scheduler's lock, in completion order. The trace
is therefore a serialization of the run, even with parallel workers.

The state after step N is computed without running anything. Start from the
inputs, then for each step up to N: dequeue `consumed`, enqueue `produced`,
and replace the actor's state. `queueSizes` is kept as it is. The stepper
still reads it, and on resume it is a check: replayed sizes that differ from
the recorded ones mean a corrupt or edited trace, and the resume is refused.

Two changes to when the trace is written:

- **On failure too.** Today it is built only in `_finalize_run`. A failed run
  is the run most worth stepping through. Its last step is the last firing
  that completed, and the error overlay names the one that did not.
- **Appended as the run goes**, to `run.wf-queues.jsonl`, one step per line,
  then assembled into `run.wf-queues.json` at the end as now. A run killed
  outright still leaves every completed step on disk, and the IDE can step
  through a run that is still going.

Values make the trace larger. Version 2 is on by default only when it stays
under a size (see Open questions). `--queue-trace=sizes` keeps version 1.
Version 1 is still read: a run with a version 1 trace can be stepped through,
but resumed only from its failure checkpoint.

### Resume

```bash
wfpy run flows/pipeline.py --resume-from wf-out/<run> --at-step 12
```

`--at-step` uses the stepper's numbering, so the number a person reads in the
IDE is the number they pass.

Step N is a firing of the top-level plan. A nested workflow's firing is one
step, as the stepper shows it today: resuming lands before or after a child,
not inside it. Resuming inside a child needs the child's trace too. That is
left for later, since the failure checkpoint already covers a failure inside
a child.

## The stepper resumes

The stepper already has the run and the step. One more button in the Debug
cluster:

```
◀  12/40 · review  ▶  ⟲
```

**⟲ Resume from here** runs `--resume-from <that run> --at-step <shown
step>`, through the profile's run driver as ▶ Run does. It is enabled when the
run's trace is version 2. On a failed run's last step it resumes from the
failure checkpoint, so "fix, then ⟲" is the whole loop.

On a node, **Rerun from here** finds the step of that node's last firing and
resumes from the step before it, so the node fires again.

The button belongs to the platform: a trace, a step and a run are not wfpy
words, and calpy runs could use the same button. The product supplies the
command that does it. The resume arguments are product syntax (`cliResumeArgs`
in the profile, beside `cliGraphArgs`), so dialogram does not learn wfpy's
flags.

## What resuming cannot promise

- **Side effects are not undone.** A tool that wrote to a database, or an
  agent that sent a message, did so before the checkpoint. Resuming from step
  N does not take back what a firing after N did.
- **A resumed run is not a replay.** A run with parallel workers is not
  deterministic. Resuming from step N continues from the state at N. It does
  not reproduce the order the original run would have taken from there.
- **The graph cannot change.** Changing what the actions do is the use case.
  Changing the wiring is a new run.

## Phasing

1. **Atomic firings.** Peek, run, then commit, for every actor kind. This is
   useful alone: a failure no longer loses a token. Tests: each kind fails
   once, and its inputs are still queued afterwards.
2. **Checkpoint on failure, and `--resume-from`.** Serialization, graph
   fingerprint, restore. Tests: a workflow that fails at a known firing is
   fixed and resumed. Firings before the failure do not run again (counted),
   and the outputs equal those of a clean run. Also a failure inside a nested
   workflow, inside a loop, and in an agent with chat history.
3. **Queue trace version 2.** Written on failure, appended as it goes,
   `consumed`, `produced` and state per step, and `--at-step`. Tests: for
   every step of a run, the replayed state equals the state captured live at
   that step. The stepper keeps working on version 1 and version 2 traces.
4. **The stepper's ⟲ button**, and "Rerun from here" on a node (dialogram +
   wfpy-ide).

## Open questions

- **Firings in flight on a failure.** Let them finish, as above, or stop at
  the first failure and put their inputs back as well? Letting them finish
  keeps more work. Stopping makes the failure point sharper.
- **Agent outputs on resume.** A stateful agent's chat history is restored. A
  stateless agent asked again after a resume may answer differently than it
  would have the first time. Is that acceptable, or should a resume replay the
  recorded answer for a firing that is in the journal?
- **Trace size.** Version 2 writes every produced token, so a large non-file
  token (a big string, a list) appears inline in the step that produced it.
  What threshold sends it to a side file instead, as File outputs already
  are? And which default: version 2 always, or only when asked for?
