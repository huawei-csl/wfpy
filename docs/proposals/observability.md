# Proposal: what a run cost, and what its agents did

**Status:** proposed — nothing here is implemented. Phase 1 is the one the
other three are waiting on.
**Affects:** wfpy (the journal, the queue trace, the run record, the event
stream); dialogram and wfpy-ide read the new fields but need no new contract.

A wfpy run already knows how long each firing took, how many tokens each agent
spent, what it cost, which tools it called and which permissions it asked for.
Almost none of that survives the run. Durations are computed and `print()`ed;
token and cost totals are assembled and then written to disk only if someone
remembered `--agent-debug`; the twelve live agent events are published to an
SSE stream and dropped when the run ends. The journal records *what* fired, in
what order, with input digests, and can replay it exactly — but not what any of
it cost or how long it took.

So a run that takes forty minutes and twelve dollars leaves behind a perfect
account of its causality and no account of its expense. "Which agent is eating
the budget", "did the repair loop cost more than the kernel it fixed", "when did
the context fill up" are all unanswerable after the fact, from a run whose every
firing is otherwise reconstructable.

This proposal is one half of a comparison with
[cannbot-sentry](https://gitcode.com/cann/cannbot-sentry), which does
observability for coding agents in three packages: `insight` (reads
`~/.claude/projects/*.jsonl`, opencode's SQLite and Codex rollouts into a web
workbench), `cpx` (a capture proxy on the model wire) and `sift` (audits a
transcript against a declared `SKILL.md`, and gates CI on it). Most of Sentry
cannot and should not move here, because Sentry stands on the far side of a
boundary: **it observes sessions it did not run.** wfpy only ever observes runs
it orchestrates itself.

That boundary cuts both ways, and the useful direction is the one that gets
overlooked. Sentry *infers* a run's shape from transcripts — insight's Graph tab
parses a DAG manifest some other tool produced, and its retry-waste simulation
reconstructs what a re-run cost by summing node durations. wfpy does not need to
infer any of it: it owns the graph, the firings, the guards and the queues. Every
number insight reconstructs approximately, wfpy can record exactly. What follows
takes the four ideas worth having and drops the rest, with the reasons for both.

## What already exists

Nearly all the data. Almost none of the recording.

| today | where | why it is not enough |
| --- | --- | --- |
| a task's duration | `runner.py:1343` → `print` | never reaches a file |
| a tool's duration | `_step_external_runtime.py:299` → `print` | never reaches a file |
| an agent step's duration | `runner.py:2538` → `print` | never reaches a file |
| an agent's reply time | `runner.py:1891`, accumulated over repairs at `2041`/`2161`/`2289`/`2365` | the one duration that lands: `replyTimeMs` is attached to the last assistant message (`runner.py:245`), so it reaches `chatHistory` → `run.wf-run.json`. Per-message, never summed per run |
| tokens, cache reads, requests, tool rounds, cost | assembled in `_invoke_runtime.py:774-786`; OpenRouter cost lookup `_agent_request_runtime.py:320` | printed by `_agent_io_runtime._print_agent_usage_summary` (`:849`) and written to `agent-debug/…response__N.json` **only when `--agent-debug` is on** (`_run_artifacts.py:653`). Not in the run record, the journal or the agent-context overlay. There is no per-run total and no cross-run ledger |
| the firing journal | `_journal_runtime.py:112` — `actor`, `fireCount`, `consumed` (with sha256), `produced`, `state`, `seq` | no time and no cost. It can replay a firing but cannot tell you what repeating it saved |
| the queue trace | `_run_artifacts.py:610` — `step`, actor, every queue's size and last token, `journalSeq` | the same: a complete causal record with no cost axis |
| `wf-out/run-log.jsonl` | `_run_finalization.py:229` — runId, workflow, times, inputs, outputs, error, `resumedFrom` | already the right shape for a cross-run ledger, and carries neither duration nor cost |
| twelve live agent events | published to the run's event stream from six places: `acp_client.SimpleClient._emit` (`agent.message.delta`, `agent.tool_call`, `agent.tool_call_update`, `agent.permission.requested/answered`), `_agent_cli_runtime` (`agent.message.start/end`), `_elicitation_runtime` (`agent.question.requested/answered`), `_run_finalization` (`run.started`), `runner` (`run.finished`) and `_run_event_stream` (`run.stream.closing`) | **never persisted.** The SSE stream (`_run_event_stream.py`) keeps a 512-event replay buffer in memory and dies with the run. The only `*events.jsonl` on disk is `validation-events.jsonl`. An agent inside a nested workflow publishes none of them: the stream is set on the top plan only (`_run_finalization.py:133`) |
| an agent's context budget | `core.py:414` — `context_budget: int = 50`, trimmed by `trim_chat_history` (`_agent_io_runtime.py:203`) | counted in **messages**. The window that actually overflows is counted in tokens |

## Phase 1 — a firing records its time and its cost

Everything else here waits on this, and it is the cheapest change in the
proposal: the numbers already exist, one call below the point where the journal
entry and the trace step are written. The entry is recorded in `_step_actor`
from what the firing took and put; the duration and the usage are locals of
`_step_internal` and `_step_agent`, and have to be carried up to it.

1. **The journal entry** (`_journal_runtime.py:112`) gains `durationMs`, and for
   an agent firing `usage` — `{promptTokens, completionTokens, cacheReadTokens,
   cacheCreationTokens, numRequests, toolRounds, costUsd}`, the dict
   `_invoke_runtime.py:779` already builds. Replay must ignore both: a replayed
   firing spent no time and no tokens, and `entry["replayed"]` already says so.
2. **The queue-trace step** (`_run_artifacts.py:610`) gains the same
   `durationMs`, so the IDE's stepper can show a run's time as well as its
   shape, and a slow firing is visible where it happened rather than in
   scrollback.
3. **The run record** (`_run_finalization.py:181`) gains `totals`:
   wall-clock, summed firing time, and a per-actor breakdown
   (`{instanceName, entityName, fireCount, durationMs, usage}`) rolled up from
   the journal. Writing it on the failure path too, as `run.wf-queues.json`
   already is, matters more than on the success path — a run that died at
   minute thirty is exactly the one whose expense you want to see. Two limits
   of today's record carry over unless they are lifted with it. The journal is
   written only with `queue_trace` on (the default), so a run without it has
   nothing to roll up. And the record's `actors` are the top plan's
   (`_run_finalization.py:194`): a nested workflow is one entry, so the
   breakdown has to descend into the sub-plans to say which nested actor
   spent what.
4. **`run-log.jsonl`** (`_run_finalization.py:229`) gains `durationMs` and
   `costUsd`. One line per run, already append-only: that is the cross-run
   ledger, and it makes "this workflow got 40% more expensive last week" a
   `jq` one-liner rather than a research project.

Sentry's schema is worth copying here rather than inventing: insight's `Turn`
model records prompt, completion, **and cache read/write** tokens separately,
and rolls them up onto `Session` — because cache reads are most of the token
volume in an agentic loop and averaging them into one number hides the thing
you are trying to see. `_invoke_runtime.py:553` already tracks
`cache_read_input_tokens` separately. Keep it separate all the way to disk.

Cost needs a stated honesty boundary. wfpy knows the true cost only on the
`http` transport, where it is the LLM client. On `acp`, `claude-cli`,
`codex-cli` and `opencode-cli` it has what it can scrape — opencode's
`step_finish` events — and for Claude Code over `claude-agent-acp` it has
nothing at all. So `usage` must carry its provenance (`"measured"` |
`"scraped"` | `"unavailable"`) instead of letting a missing number read as
zero. A cost ledger that silently reports $0 for the most expensive agent in
the graph is worse than no ledger.

## Phase 2 — the retry-waste ledger

With Phase 1 in place, wfpy can answer a question insight can only estimate.

insight's `run-simulate.ts` computes a "retry-waste ledger and parallel-universe
simulation": the reality — the sum of node durations — against a hypothetical
run where everything passed first try. It is a good idea executed at a
disadvantage, because insight is reasoning over a manifest another tool emitted,
with no access to why a node ran twice.

wfpy has that why. A feedback loop is an ordinary edge; a verdict is a `@task`
with `@guard`-routed `Ok`/`Bad` ports (the pattern `pypto-wfpy`'s
`src/tasks/gate.py` uses, and `wfpy-skillyard`'s Phase-0 gate); a repair loop's
second pass is a firing whose `fireCount` is 2, recorded in the journal with its
inputs digested. So the ledger is a derivation over the journal, not a
simulation:

- **first-pass cost** — the cost of each actor's `fireCount == 1` firings;
- **repair cost** — everything after, grouped by the actor whose `Bad` verdict
  sent the token back, which the graph's edges name;
- **replay credit** — what `--at-step` replay already saved, since
  `entry["replayed"]` marks it;
- **stranded cost** — work whose tokens are in `leftovers` in the queue trace:
  paid for, consumed by nothing.

That last one has no equivalent in Sentry and is the kind of thing only the
dataflow model can see. A `@guard` that never fires presents today as a silent
deadlock — `wfpy-skillyard` documents this as a trap — and a ledger that says
"nine firings produced tokens nobody consumed" turns that into a diagnosis.

Output as a `ledger` section of the run record, or `run.wf-cost.json` beside it
if it grows. Derived, never authoritative: the journal stays the source of truth.

## Phase 3 — the agent event log

Persist the event stream. `_run_event_stream.py` already publishes twelve event
types through one `publish()` chokepoint and already keeps them in a bounded
ring; the change is a `run.wf-events.jsonl` writer on the same call, with the
atomic-write discipline the live overlays use.

This is the largest fidelity gain for the least work, because of what is
currently lost. On `transport="http"` wfpy keeps everything — the full message
list, every tool call and result, usage. On `acp` and the CLI transports it
keeps only the boundary: the prompt in, the final assistant text out, and
`debug_meta` (argv, exit code, stderr, `acpStopReason`, session id). The
streaming deltas, the agent's reasoning, **every ACP tool call** and every
permission prompt exist — they are emitted live, watched by the IDE if it
happens to be attached, and then discarded. For the connector wfpy-skillyard
runs nine phases of Claude Code on, that is most of what happened.

Persisting it gives a postmortem of an agent firing with no IDE attached, which
is the CI and the headless case, and makes the `agent-debug` artifacts the
opt-in *deep* record they were meant to be rather than the only record.

One gap has to be closed with it. The stream is set on the top plan only, so an
agent inside a nested workflow has no publisher: its events are not emitted at
all, live or otherwise, and a log written at `publish()` would miss them. The
log is complete once `_run_sub_plan` hands the stream to the sub-plan, as it
does the journal and the overlay writer. An event of a nested agent then needs
the path of its workflow instance beside the agent's name, since two instances
of one workflow hold agents of the same name.

What this still does not get is the external agent's **own** context: its
system prompt, its file reads, its internal transcript. wfpy sees an ACP
boundary, not Claude Code's session. Sentry solves exactly that with `cpx`, a
capture proxy that injects an upstream override **into the child process only**
— and wfpy spawns the connector command (`connectors.py`), so wrapping that
spawn in `cpx` would close the gap without wfpy growing a proxy of its own.
Worth a documented recipe; not worth a feature. The `agentSession = {id,
connector, cwd}` handle wfpy already records is the cheap version of the same
idea, and should stay.

## Phase 4 — context pressure, in tokens

`context_budget` defaults to 50 and is a count of **messages**
(`core.py:414` → `trim_chat_history`). Nothing overflows at fifty messages;
things overflow at a token limit, and fifty messages of tool results against
fifty messages of one-line replies differ by two orders of magnitude. The
budget is therefore either far too loose or far too tight, and which one it is
cannot be known from any artifact a run leaves behind.

insight records `contextWindowPct` per turn and draws the curve across a
session, which makes "the agent degraded because it was at 95% from turn
twelve onward" visible at a glance. wfpy should record the same per agent
firing — occupancy in tokens and as a fraction of the model's window — in the
event log and in `run.wf-agent-context.json`, which already carries
`contextBudget` and `truncationStrategy` but nothing about what actually
happened to the context.

Then make the budget token-aware, keeping the message count as a secondary
bound, and record each truncation as an event. A `"sliding"` strategy that
silently drops the message holding the task description is a failure mode worth
seeing in a log rather than inferring from a confused agent.

## Non-goals

Stated, because the tempting half of Sentry is the half that should not come.

- **No LLM judge, no scorecard.** sift's audit (a regex instruction extractor,
  deterministic tool matching, then batched LLM judges) and insight's
  six-dimension scorecard are 35k LOC of opinions about agent *quality*. wfpy
  should stay a runtime that records what happened and holds no opinion about
  whether it was good. A judge belongs in a `@task` in someone's workflow, or
  in sift downstream of the artifacts.
- **No eval harness, no A/B.** Note in passing that the inverse is interesting:
  `sift run`/`eval` fans an eval × side × run matrix over a
  `ThreadPoolExecutor` (`execution/runner.py`, ~1000 LOC of pool, cache keys
  and rerun classification). That is a dataflow graph with the scheduling
  hand-rolled, and `pypto-wfpy`'s `workflows/pool.py` already expresses the
  same `Dispatch → N×Worker → Join` shape in a fraction of it. If anything
  moves between these projects it is sift's harness onto wfpy, not wfpy
  growing an eval mode.
- **No web UI, no TUI.** dialogram and wfpy-ide own the surface; wfpy's side of
  that contract is files and the SSE stream, and every phase above stays inside
  it. A new field in the trace reaches the stepper for free.
- **No session-log readers, no proxy.** `~/.claude/projects`, opencode's
  SQLite, Codex rollouts, the model wire: this is the far side of the boundary,
  it is what insight and cpx exist for, and they already interoperate with
  anything wfpy spawns.

## Interop, if it is wanted later

insight's run workbench opens a run bundle — `manifest.json` plus sessions —
that another orchestrator produced, and gives it eleven tabs of analysis. wfpy
is a natural producer of that manifest: it has the DAG, the per-node timing
(after Phase 1), and the agent sessions. An exporter would be maybe a few
hundred lines, and would hand wfpy runs a workbench, a comparison view and a
scorecard without wfpy implementing any of it.

Explicitly later. The phases above are worth doing whether or not this is ever
written, and this is worth nothing until Phase 1 exists.

## Open questions

- **Journal compatibility.** New keys on a journal entry are additive, but
  replay compares a digest and refuses a changed graph. Does a journal written
  before these fields replay against a runtime expecting them, or does the
  header's `version` need a bump and a read path for both?
- **Where the per-actor roll-up is computed.** Summing the journal at
  finalization keeps one source of truth and costs a file read; accumulating
  on `RuntimeActor` as firings complete is free but adds state that
  `_checkpoint_runtime.actor_state` must then carry, or a resume loses the
  totals. The second is probably right and the cost of forgetting is silent.
- **Event-log volume.** `agent.message.delta` is per-token. A long run writes a
  large file, most of it deltas. Drop deltas and keep message start/end, tool
  calls and permissions? Keep them behind a flag? Or write them and let the
  reader skip a `field`-tagged line?
- **Cost on the CLI transports.** Is a scraped number with
  `provenance: "scraped"` good enough to aggregate into a run total, or should
  a run containing any `unavailable` agent refuse to report one total and
  report the breakdown only?
- **The model's window size.** Phase 4 needs it to compute a percentage, and
  wfpy does not have a model registry. Per-agent declaration, a small table, or
  only report absolute tokens and leave the fraction to whoever knows?
