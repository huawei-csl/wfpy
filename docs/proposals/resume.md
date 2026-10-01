# Proposal: resuming a run

**Status:** proposal — phases 1 to 3 (atomic firings, checkpoint on failure, the firing journal and `--at-step`) are implemented; the stepper's resume button is not.
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
failure can bring them back. So the first change is **atomic firings**: a
firing that fails gives back what it took.

**Implemented** (phase 1). Rather than reordering each kind's step, every
`Queue.dequeue()` made during a firing is logged per thread
(`_atomic_firing` in `runner.py`, around `_step_actor`). When the firing
raises, each token goes back to the head of its queue, in reverse order, so
the queue is as it was. The same log is what phase 3 records as a step's
`consumed`.

An `if` or a `loop` is not wrapped, any more than a nested workflow is. Its
firing runs its branch to quiescence, and each firing in the branch is atomic
on its own. Giving back the condition or the iterable after part of the
branch had run would run that part twice. Where a control node stopped is
its state, recorded by the checkpoint.

That is safe without holding the queues for the whole firing. One actor never
fires twice at once (the scheduler keeps `running_actors`), and every queue
has exactly one consumer, so nothing else takes from the head meanwhile. A
producer appending to the tail is unaffected.

What is not given back is the actor's own state. An action that set
`self._done = True` and then raised has still set it. Phase 2's checkpoint
records the state as the failure left it.

A nested workflow is the exception: its tokens are handed to the sub-plan,
which may consume them before failing. Its checkpoint is the sub-plan's own,
taken recursively, and the parent's tokens are already in it.

### Write a checkpoint when a run fails

**Implemented** (phase 2), in `_checkpoint_runtime.py`. It differs from the
sketch below in three places:

- **An interrupted composite firing is marked.** A nested workflow running its
  sub-plan, an if running its branch, and a loop running its body set
  `RuntimeActor.pending` before they run the other firings and clear it once
  done. A failure inside leaves it set; the checkpoint records it (for an if,
  with the token it passes on). A resumed run finishes that firing rather than
  starting it again: the child carries on its sub-plan without new input, the
  if or loop runs its branch or body to quiescence, then completes.
- **A loop's position** is the iterable and the number of items taken. The
  iterator is rebuilt on restore and advanced that far. A loop over something
  that cannot be saved (a generator) makes the checkpoint not resumable.
- **Paths are written as they are**, absolute. File tokens keep pointing into
  the failed run's `work/`, which stays where it is. `--copy-work` is not
  implemented.
- **A stop from the IDE does not write one yet.** Ctrl-C does: the
  `KeyboardInterrupt` unwinds through the executor, which waits for the
  firings in flight. The IDE sends SIGTERM, which Python does not turn into an
  exception, then SIGKILL after a grace period that a firing in flight (an
  agent call) can outlast. Handling SIGTERM means choosing between waiting for
  those firings and abandoning them with their inputs. That is left open.

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

**Implemented.** With type tags for what JSON lacks, tried in this order:

1. **Builtins, by exact type.** JSON's own types, plus tags for a tuple, a
   set, a frozenset, bytes, a dict with non-string keys, a `Path`, `File` and
   `Resource`, an `Enum` member (by name), `datetime` / `date` / `time` /
   `timedelta`, `Decimal`, `UUID` and `complex`. The match is on the exact
   type, so a `Counter`, a named tuple or an `IntEnum`'s value is not flattened
   into its base.
2. **An object, by name:** `{"$wfType": "object", "type": "module:Qualname",
   "state": {…its attributes…}}`. It is read back by making a bare object of
   the class and setting the attributes, frozen dataclasses included. This is
   the common case: a task's own classes, dataclasses or not. It survives the
   class's code changing between the failure and the resume, which is what a
   fix does. Used for a class that can be found by its name, keeps its
   attributes in a `__dict__` or Python `__slots__`, is not built on a builtin
   container, and defines none of pickle's hooks.
3. **Pickle, as the fallback**, for what the attributes do not describe: a
   class that defines its own `__getstate__` / `__reduce__` (it knows how it is
   saved), or a subclass of a builtin container. Such a value is tied to its
   class as pickle is. If the class no longer reads it, the resume says so,
   naming the checkpoint.
4. **Otherwise not resumable:** a lock, an open file, a generator, a lambda,
   an object of a class defined inside a function. The file is still written,
   naming each value that could not be, with the reason.

The readable files (the overlay, the run record) use their own encoder. It
now writes any object it cannot otherwise show as its `repr` (a dataclass as
its fields), instead of failing the run.

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

## Resuming from a completed step: replaying a firing journal

**Implemented** (phase 3), in `_journal_runtime.py`, differently from the
first version of this section. That version extended the queue trace with
each step's consumed and produced tokens, and rebuilt the state after step N
by applying steps 1 to N. That is unsound with parallel workers. A step is
recorded when a firing completes, while other firings are in flight, so the
state "after step N" can hold half of a firing that completes later. A
consumer can even complete, and be recorded, before the firing that produced
its input. Applying steps 1 to N then takes a token that was never put.

### Replay per actor, not per state

Every firing that completes appends one line to `run.wf-journal.jsonl`:

```json
{"seq": 12, "actor": "child/review", "fireCount": 3,
 "consumed": [["draft.Out-->review.In", 1, "sha256 of the tokens"]],
 "produced": [["review.Out-->WF.Report", [{"$wfType": "file", "path": "…"}]]],
 "state": {"_round": 4}, "chatHistory": ["…"]}
```

A header line holds the run's inputs and graph fingerprint. Tokens and state
are encoded as checkpoints encode them.

`wfpy run flow.py --resume-from wf-out/<run> --at-step N` starts the run
again from that run's inputs. Each actor's first firings, those the run had
completed by step N, are **replayed** instead of run: the recorded tokens are
taken and put, and the recorded state is set. Then the run carries on live.

This is sound under any scheduling, because in a dataflow network each actor
sees the same sequence of tokens however its firings interleave with others'.
An actor's k-th firing can therefore be replayed whenever its inputs arrive.
It does not matter which other firings happen to be in flight, or which
completed first.

### What "by step N" means

Which firings a step covers is chosen per actor, not by one journal cut-off
(that is the same race again: another actor's firing can be journalled
between a firing's entry and its step):

- an actor the trace shows replays its entries up to its own last step at or
  before N;
- an actor inside a nested workflow (path `child/leaf`) follows that
  workflow's steps;
- an actor the trace never shows fires only inside an if or a loop, which run
  with nothing else in flight, so the cut-off of step N is exact for it.

Each trace step carries `journalSeq`, the journal's length when it was
recorded, and `replayed: true` when the firing was replayed.

### When the run goes another way

A replayed firing checks that the tokens it takes are the ones the original
took (by digest). The firings feeding a replayed one are replayed too, so in
a straight resume they always match. They do not when something upstream runs
live and behaves differently: a firing of the race above, or a journal that
was cut short. Then replay stops, the reason goes into the run record
(`replayStopped`), and the run carries on live from there. Replay also stops if
the run goes quiet with firings left to replay, and at a firing whose values
could not be saved (`notReplayable`).

What a replayed firing returns is what it returned then. Resuming at step N
keeps everything before N as it happened, including what an agent answered.
A fix to an actor's code applies to its firings after N.

### Also

- The queue trace is now written when a run fails too: the last step is the
  last firing that completed.
- A resumed run keeps its own journal, replayed firings included, so it can be
  resumed at a step in turn. A run resumed from a checkpoint (phase 2) cannot,
  because it did not start from inputs. Resume at a step of the run it came
  from instead.
- `queue_trace=False` keeps no journal either.
- Not done: the trace is not appended as the run goes (`run.wf-queues.jsonl`).
  The journal is, so a run killed outright can still be resumed at a step if
  its trace survives, which today it does not.

## The stepper resumes

The stepper already has the run and the step. One more button in the Debug
cluster:

```
◀  12/40 · review  ▶  ⟲
```

**⟲ Resume from here** runs `--resume-from <that run> --at-step <shown
step>`, through the profile's run driver as ▶ Run does. The number shown is
the trace's `step`, which is the number `--at-step` takes. It is enabled when
the run's trace steps carry `journalSeq`. On a failed run's last step it
replays every firing that completed and runs the failed one live, so "fix,
then ⟲" is the whole loop.

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

1. **Atomic firings.** *Done.*
2. **Checkpoint on failure, and `--resume-from`.** *Done.*
3. **The firing journal, and `--at-step`.** *Done.* Tests resume at every
   step of a run, with one worker and with parallel ones. They check that the
   firings before the step are replayed and not run, that those after it run,
   and that the outputs equal a clean run's. Also covered: a failed run at its
   last step, a loop over a child workflow, a resumed run resumed again,
   replay stopping when inputs differ, and the CLI.
4. **The stepper's ⟲ button**, and "Rerun from here" on a node (dialogram +
   wfpy-ide). The button passes the step it shows to `--at-step`.

## Open questions

- **Firings in flight on a failure.** Let them finish, as above, or stop at
  the first failure and put their inputs back as well? Letting them finish
  keeps more work. Stopping makes the failure point sharper.
- **Agent outputs on resume.** A stateful agent's chat history is restored. A
  stateless agent asked again after a resume may answer differently than it
  would have the first time. Is that acceptable, or should a resume replay the
  recorded answer for a firing that is in the journal?
- **Journal size.** The journal writes every produced token, so a large
  non-file token (a big string, a list) appears inline in the firing that
  produced it. What threshold sends it to a side file instead, as File outputs
  already are? It is on whenever the queue trace is (the default).
