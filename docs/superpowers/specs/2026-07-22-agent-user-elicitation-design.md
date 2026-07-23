# Agent → user elicitation: "stop and ask the user" during a firing

Date: 2026-07-22
Status: implemented (v1) — decisions resolved 2026-07-22

Implementation: all six phases landed on the working tree. New module
`src/wfpy/_elicitation_runtime.py`; `AgentSpec.ask_user` + `@agent(ask_user=…/askUser=…)`;
builtin `ask_user` tool (declaration + auto-auth + dispatch in the HTTP tool loop);
`run(elicitation_handler=…, interactive=…, elicit_timeout_ms=…, elicit_default=…,
elicit_require=…)`; `wfpy run` flags `--interactive/--no-interactive`,
`--elicit-timeout-ms`, `--elicit-default`, `--elicit-require`; `askUser` in the graph
export + run artifacts. Tests in `tests/test_elicitation.py` (23) + `tests/test_core.py`;
demo at `examples/ask_user_probe.py`. Zero mypy/test regressions vs. baseline.

## Problem

Today an `@agent` task is fully autonomous. Once it fires, the LLM runs to
completion with the user's privileges and **no way to pause and ask a human**.
The trust model (`SECURITY.md`) says so explicitly: ACP permission requests are
auto-approved (`acp_client.SimpleClient.request_permission`,
`src/wfpy/acp_client.py:92-97`), the native opencode CLI is launched with
`--dangerously-skip-permissions` (`src/wfpy/runner.py:1586`), and there is no
`input()`, TTY prompt, elicitation, or pause/resume anywhere in the runtime (a
repo-wide search for `input(`, `elicit`, `ask_user`, `hitl`, `prompt_user`
returns nothing in `src/`).

We want the opposite of full autonomy, **opt-in per agent**: an agent should be
able to *demand a clarifying question mid-firing*, block, receive the user's
reply, and continue with that reply folded into its context. The capability must
be switched on from the `@agent` decorator (e.g. `@agent(ask_user=True)`), off by
default so existing workflows are unchanged.

Constraint: reuse the machinery we already have — the HTTP-transport tool-calling
loop, the `plan.options` closure-injection pattern, and the live run event bus —
rather than inventing a new scheduler or a durable checkpoint/resume subsystem.

## Decision

Expose a **built-in `ask_user` tool** to the LLM, gated by a new
`AgentSpec.ask_user` boolean. When the model calls it, the runtime dispatches to
a pluggable **elicitation handler** that surfaces the question to the user,
**blocks** for an answer, and feeds the answer back as the tool result. The
LLM's own tool-calling loop is the pause/resume mechanism — no new control flow.

This works because the HTTP-transport agent loop in `_invoke_agent`
(`src/wfpy/_invoke_runtime.py:503-597`) already: emits the request with a `tools`
declaration, extracts tool calls, executes each, appends the result as a
`tool_result` message, and loops. We add `ask_user` as a second built-in
alongside `python` and route it to the handler instead of a subprocess.

The handler is resolved (in priority order) to one of:

1. an explicit `elicitation_handler` callable passed to `run(...)` — for
   embedding, the IDE, or an ACP/SSE frontend;
2. a **console handler** (default when stdin/stdout is a TTY, or `--interactive`
   is passed) that prints the question — clearly labelled as coming from the
   *agent*, not from wfpy — and reads one reply from stdin, serialized under a
   process-wide lock;
3. a **non-interactive fallback** (no handler, not a TTY): by default the tool
   returns an "no user available" result so long pipelines degrade gracefully
   instead of hanging; opt-in strict mode (`--elicit-require`) fails the firing.

The handler is injected as an **actor-scoped closure on `agent_options`**,
mirroring the existing `_wf_event_publish` bridge
(`src/wfpy/runner.py:1596-1599`). Because `_invoke_agent` already receives both
`spec` and `plan_options`, **its signature does not change** — it reads
`spec.ask_user` and `plan_options["_wf_elicit"]`. This preserves the
`_invoke_agent` 4-tuple contract and the `tests/conftest.py::mock_invoke_agent`
patch shape (the mock already absorbs unknown kwargs via `**kwargs`,
`tests/conftest.py:303-311`).

### Why decouple `ask_user` from the general tool gate

Tool declarations are only sent when `enable_tools=True`
(`src/wfpy/_agent_request_runtime.py:76-80`), and
`enable_tools = auth_mode != "deny-all"` with `deny-all` the default
(`src/wfpy/_invoke_runtime.py:320`, `_agent_tools_runtime.py:387-398`). If we
registered `ask_user` as an ordinary registry tool it would (a) be hidden unless
the user broadly enables the risky `python`/MCP tools, and (b) be subject to
deny-all authorization. That is backwards: **asking a human for input is
inherently safe and consent-based** — no code, no filesystem, no network.

So `ask_user` is treated specially:

- **Declared** to the model whenever `spec.ask_user` is true, independent of the
  registry and of `agent_tool_auth`.
- **Enables the loop**: the loop processes tool calls when
  `enable_tools OR ask_user_enabled`, but under `deny-all` *only* the `ask_user`
  declaration is sent — `python`/MCP stay hidden and unauthorized.
- **Auto-allowed** in `_authorize_tool_call` regardless of mode; every other tool
  keeps its existing authorization.

### Rejected / deferred alternatives

- **Durable suspend-to-disk + resume-later.** Serialize the run, exit, and let a
  later `wfpy run --resume` supply the answer. This is a much larger feature
  (needs a full run-state checkpoint; today only `stateful` chat history and CLI
  session ids persist — `src/wfpy/_run_artifacts.py`, `runner.py:1609`). The
  user's framing ("stop the current execution … and ask … for a reply") is
  in-process blocking, so we build that first. Deferred, not precluded — the
  handler seam is compatible with a future resume implementation.

- **Answer over the live SSE stream.** `RunEventStream` is deliberately
  **read-only** (GET-only, no mutating endpoints — `_run_event_stream.py:15`,
  `SECURITY.md`). We *publish* `agent.question.requested`/`answered` events for
  observers, but routing the *answer* back would require a new token-authed
  loopback control endpoint plus response routing. Deferred to the follow-up that
  makes the IDE interactive.

- **Route through ACP `request_permission`.** For the `opencode-acp`/CLI
  transports the *agent's own* loop runs the tools — wfpy never sees the tool
  call, only an ACP permission request (approve/deny, not free-form Q&A), and
  currently auto-approves it (`acp_client.py:92-97`). Real ACP/MCP elicitation is
  a separate integration (see Non-goals). v1 targets the **HTTP transport**,
  where wfpy owns the loop.

## Scope

**In scope (v1):** HTTP-transport agents; `@agent(ask_user=…)` decorator flag;
the `ask_user` built-in tool + dispatch; the elicitation-handler protocol;
console + injectable handlers; non-interactive fallback; CLI flags; event-stream
observability; graph-export surfacing; docs + tests.

**Non-goals (explicit follow-ups):**

1. **CLI/ACP transports** (`opencode-cli`, `claude-cli`, `codex-cli`,
   `opencode-acp`). Future work: teach `acp_client.SimpleClient.request_permission`
   (`acp_client.py:92`) to consult the same handler instead of auto-approving,
   accepting that ACP's shape is approve/deny rather than free text.
2. **MCP-server-initiated elicitation** (`elicitation/create`). The MCP
   `ClientSession` is built with no callbacks (`_mcp_client.py:81,93,112`); wiring
   an `elicitation_callback` that reuses the same handler is a clean follow-up.
3. **Interactive IDE / remote answer channel** over SSE (needs the read-only
   stream to gain a mutating control endpoint).
4. **Durable suspend/resume across process exit.**

## Components

New module and touch-points (all paths under `src/wfpy/`):

1. **`_elicitation_runtime.py` (new)** — the seam. Defines:
   - `ElicitationRequest` (`question`, `context`, `choices`, `agent_name`,
     `run_id`, `timeout_ms`) and `ElicitationResponse` (`answer: str | None`,
     `declined: bool`, `reason: str`).
   - `ElicitationHandler = Callable[[ElicitationRequest], ElicitationResponse]`.
   - `console_elicitation_handler` — prints a clearly-attributed prompt
     (`Agent '<name>' (<model>) asks:`) and reads one stdin line, guarded by a
     module-level `threading.Lock` so concurrent agents (the scheduler is a thread
     pool — see Data flow) never interleave on the console. Honors `timeout_ms`.
   - `build_elicit_closure(plan_options, *, agent_name, model, run_id, event_publish)`
     — the factory called from `_step_agent`. Resolves handler #1/#2/#3, and
     returns a closure `ask(question, *, context=None, choices=None) ->
     ElicitationResponse` that emits `agent.question.requested`, invokes the
     handler (or fallback), emits `agent.question.answered`, and records the
     exchange. This mirrors `_publish_agent_event` (`runner.py:1596-1599`).

2. **`core.py`** — `AgentSpec.ask_user: bool = False` next to `stateful`
   (`core.py:404`); `ask_user` kwarg on `agent()` (`core.py:~994`) + `askUser`
   camelCase alias in the alias block (`core.py:1010-1031`); wire
   `ask_user=normalized["ask_user"]` into the `AgentSpec(...)` construction
   (`core.py:1142-1171`), following the `use_prompt` template.

3. **`_agent_decorator.py`** — add `ask_user`/`askUser` to `_normalize_agent_kwargs`
   exactly like `use_prompt` (`_agent_decorator.py:42`): camelCase wins when not
   `None`.

4. **`_agent_tools_runtime.py`**:
   - `_BUILTIN_ASK_USER_SPEC = AgentToolSpec(kind="builtin", name="ask_user", …)`
     next to `_BUILTIN_PYTHON_SPEC` (`:100-111`), with parameters
     `{question: string (required), context: string, choices: array[string]}`.
   - `_provider_tool_declarations(provider, registry, mcp_filter, *,
     include_ask_user=False)` (`:491`) — append the `ask_user` declaration when
     `include_ask_user`, in both Anthropic (`input_schema`) and OpenAI
     (`function.parameters`) shapes.
   - `_authorize_tool_call` (`:411`) — return `(True, "ask_user always allowed")`
     for the `ask_user` call before the mode checks.

5. **`_agent_request_runtime.py`** — `_build_agent_request(…, include_ask_user=False)`
   (`:33`): build `tools` when `enable_tools OR include_ask_user`
   (`:76-80`) and thread `include_ask_user` into `_provider_tool_declarations`.

6. **`_invoke_runtime.py`** — inside `_invoke_agent` (HTTP path):
   - `ask_user_enabled = bool(getattr(spec, "ask_user", False))`; compute
     `process_tools = enable_tools or ask_user_enabled` and use it where the loop
     currently keys off `enable_tools` (`:323`, `:504`).
   - Pass `include_ask_user=ask_user_enabled` into every `_build_agent_request`
     call (`:351`, `:423`, `:580`, `:629`).
   - Extract the per-call dispatch (`:530-552`) into a small pure helper
     `_dispatch_agent_tool_call(...)` and add a branch: `elif call.name ==
     "ask_user": result = _run_ask_user_tool(call, plan_options)`, ahead of the
     python `else`. `_run_ask_user_tool` reads `plan_options["_wf_elicit"]`,
     calls it with the question/context/choices, and wraps the answer as an
     `AgentToolResult` (stdout = the reply; a declined/unavailable response
     yields a clear, model-readable message).
   - Record `response_debug["elicitations"] = [{question, answer, choices,
     declined, ts}]` for the agent-debug artifact.

7. **`runner.py`**:
   - `_step_agent` (`:1299`): after `agent_options = dict(plan.options)`
     (`:1581`) and alongside the `_wf_event_publish` wiring (`:1596-1599`), attach
     `agent_options["_wf_elicit"] = build_elicit_closure(plan.options,
     agent_name=actor.name, model=agent_spec.model, run_id=…, event_publish=…)`.
   - `run(...)` (`:2517`): new kwargs `elicitation_handler=None, interactive=None,
     elicit_timeout_ms=600_000, elicit_default=None, elicit_require=False`; add
     the scalar ones to the `plan.options` literal (`:2673-2701`) and set the
     callable directly (`plan.options["_elicitation_handler"] = …`) the way
     `_agent_tool_registry` is set directly at `:1550` (the literal filters out
     `None`).

8. **`cli.py`** — on the `run` subparser (`:399-551`): `--interactive` /
   `--no-interactive` (`BooleanOptionalAction`, default `None` → autodetect
   `sys.stdin.isatty() and sys.stdout.isatty()`), `--elicit-timeout-ms`,
   `--elicit-default TEXT`, `--elicit-require`; thread them into the `run(...)`
   call (`cli.py:296-334`).

9. **`_graph_export.py`** — append `askUser` to `_build_agent_meta`'s
   `agent_args` (`:233`, mirroring the `stateful` emit at `:319-324`) so the IDE
   knows the node may prompt.

10. **`_run_artifacts.py`** — add `ask_user` to `_build_agent_context_entry`
    (`:305`, mirroring `stateful` at `:314`).

11. **Docs** — a "Human-in-the-loop / elicitation" subsection in `SECURITY.md`
    (interactive mode is the *first* real human gate — safety-positive — but the
    prompt text is authored by the untrusted model; label it as such and never
    auto-act on the answer beyond feeding it back). `CHANGELOG.md` under
    Unreleased. An `examples/ask_user_probe.py` demo.

## Data flow

```
@agent(ask_user=True) ─► AgentSpec.ask_user=True (core.py)
                                    │
run(..., interactive/elicitation_handler) ─► plan.options{ elicit_* , _elicitation_handler }
                                    │
_step_agent (runner.py, on a ThreadPoolExecutor worker):
    agent_options = dict(plan.options)
    agent_options["_wf_elicit"] = build_elicit_closure(...)   # actor-scoped
    _invoke_agent(spec, ..., plan_options=agent_options)      # signature unchanged
                                    │
_invoke_agent (HTTP loop, _invoke_runtime.py):
    include_ask_user = spec.ask_user
    request tools = registry(if enable_tools) + ask_user(if include_ask_user)
    ├─ model emits tool_use "ask_user"{question,...}
    ├─ _authorize_tool_call → allowed (special-cased)
    ├─ _run_ask_user_tool → plan_options["_wf_elicit"](question,...)
    │       ├─ publish agent.question.requested   (read-only SSE, observers see it)
    │       ├─ handler:  explicit ▸ console(TTY, locked) ▸ fallback
    │       └─ publish agent.question.answered
    ├─ answer wrapped as tool_result, appended to messages
    └─ loop continues → model finishes with the answer in context
                                    │
firing_messages (tool_use + tool_result) ─► actor.chat_history (persists if stateful)
response_debug["elicitations"] ─► agent-debug artifact
```

No wire/protocol change to providers — `ask_user` is a normal tool declaration.
The exchange rides the existing `tool_use`/`tool_result` message flow, so it
lands in chat history and (for `stateful` agents) survives into later firings for
free. Bounded by `AGENT_MAX_TOOL_ROUNDS = 6` (`_invoke_runtime.py:56`); an
optional per-firing `ask_user` counter can cap repeated questions tighter.

### Concurrency (the load-bearing subtlety)

`execute_plan` is **not** single-threaded — ready actors run on a
`ThreadPoolExecutor` (`runner.py:844`, default `min(32, cpu+4)`); `max_workers=1`
restores sequential round-robin. Consequences:

- **Default parallelism:** a blocking prompt freezes only *that* worker thread;
  independent branches keep running. Two agents could ask at once → the console
  handler **must** serialize on a process-wide lock (in `_elicitation_runtime.py`).
- **`max_workers=1` + interactive:** the whole run pauses until the human answers
  — exactly the requested "stop execution and ask" behavior.
- **`max_workers=1` + non-interactive + no answer source:** would hang the run
  *and* teardown (the `with executor:` join). The non-interactive fallback (#3)
  and `elicit_timeout_ms` exist precisely to prevent this.

## Verification

- **Unit (isolated helpers, matching the existing style — cf. `_extract_tool_calls`
  tests at `tests/test_runner.py:1490-1527`):**
  - `@agent(ask_user=True)` and `@agent(askUser=True)` → `AgentSpec.ask_user is
    True`; default `False` (`tests/test_core.py`).
  - `_provider_tool_declarations(..., include_ask_user=True)` contains `ask_user`
    for anthropic + openai; absent when `False`.
  - `_authorize_tool_call` allows `ask_user` under `deny-all`; still denies
    `python` under `deny-all`.
  - `_dispatch_agent_tool_call` routes an `ask_user` call to a stub handler and
    returns the reply as the tool result; routes `python`/MCP as before.
  - `_elicitation_runtime`: console handler reads a monkeypatched stdin and
    attributes the prompt; non-interactive fallback returns "unavailable" (and
    raises under `elicit_require`); timeout path declines cleanly.
  - `build_elicit_closure` publishes `agent.question.requested`/`answered` to a
    fake event sink.
- **Integration:** one test monkeypatching `httpx.post` to return an `ask_user`
  `tool_use` then a final text answer; assert the injected handler saw the
  question, the reply reached the model, and the run completed. Assert that under
  `deny-all` + `ask_user=True`, `python` is never declared.
- **Graph export:** `askUser` appears in the agent node's args when set
  (`tests/test_graph.py`).
- **Contract guard:** `mock_invoke_agent` still works unchanged (4-tuple; new
  keys arrive via `plan_options`, not new positionals).
- **Regression:** full `pytest tests/ -q`; `ruff check`; `mypy --strict`
  (noting the pre-existing `runner.py:2796` issue in `CLAUDE.md`).
- **Manual smoke:** `wfpy run examples/ask_user_probe.py --interactive` — agent
  asks, human answers at the terminal, output reflects the reply; then without
  `--interactive` to confirm graceful degradation.

## Trust / security

Interactive elicitation is **safety-positive**: it is the first mechanism that
lets a human gate an otherwise-autonomous agent. But the *question text is
authored by the untrusted model*. Therefore: (a) the console handler attributes
the prompt to the agent/model, never presenting it as a wfpy system message;
(b) the answer is only ever fed back as a tool result — the runtime never
executes or interprets it; (c) questions are bounded (`AGENT_MAX_TOOL_ROUNDS`,
plus an optional per-firing cap). `SECURITY.md` gains a short note to this
effect. Off by default: no existing workflow changes behavior.

## Implementation plan (phased, TDD)

- **Phase 1 — decorator + spec (pure, no runtime).** `AgentSpec.ask_user`;
  `agent()` kwarg + `askUser` alias; `_normalize_agent_kwargs`. Tests first
  (`test_core.py`). Shippable and inert.
- **Phase 2 — tool declaration + authorization.** `_BUILTIN_ASK_USER_SPEC`;
  `_provider_tool_declarations(include_ask_user=…)`; `_authorize_tool_call`
  special-case; `_build_agent_request(include_ask_user=…)`. Unit tests on the
  declaration + auth.
- **Phase 3 — elicitation runtime.** New `_elicitation_runtime.py` (protocol,
  console handler + lock, fallback, closure factory, event emission). Unit tests
  with monkeypatched stdin + fake event sink.
- **Phase 4 — dispatch wiring.** Extract `_dispatch_agent_tool_call`; add the
  `ask_user` branch; `process_tools` gating; `elicitations` debug. Unit test the
  dispatcher; integration test with monkeypatched `httpx`.
- **Phase 5 — runner + CLI plumbing.** `_step_agent` closure injection; `run(...)`
  kwargs + `plan.options`; `cli.py` flags. Tests in `test_cli.py`.
- **Phase 6 — surfacing + docs.** `_graph_export.py`, `_run_artifacts.py`,
  `SECURITY.md`, `CHANGELOG.md`, `examples/ask_user_probe.py`. Export test.

Phases 1–2 are safe to land independently (feature inert until Phase 4). Each
phase is red-green-refactor with its tests written first.

## Resolved decisions (2026-07-22)

1. **Flag name:** `ask_user` (snake) / `askUser` (camel). Confirmed.
2. **Non-interactive default: graceful.** In headless runs — no TTY *and* no
   injected `elicitation_handler` — the `ask_user` tool returns a "no user
   available" result and the agent proceeds with a stated assumption; it does
   **not** hard-error. Strict failure stays opt-in via `--elicit-require`.
3. **v1 reach:** HTTP transport only. CLI/ACP/MCP transports and the SSE
   answer-channel are accepted as follow-ups (see Non-goals).
4. **Tool surface:** `choices` (multiple-choice) is **in** v1 — the `ask_user`
   tool accepts an optional `choices: array[string]` alongside free-text.

## Fallback

If the built-in-tool approach proves awkward for a provider, the same handler
seam supports a **structured post-response protocol**: the agent emits a
sentinel JSON (`{"__ask_user__": "question"}`), the runner detects it after the
firing, elicits, and re-invokes with the answer prepended — reusing the existing
output-repair re-invoke loop (`runner.py:1751-1765`). Lower fidelity (one
question per firing, no mid-turn tool context) but zero provider tool-calling
dependence. Kept as the fallback if tool-calling elicitation misbehaves.
