# Agent CLI Backends Plan

Status: draft
Date: 2026-04-14

## Goal

Investigate and design support for running `@agent` tasks through local AI CLIs
instead of only OpenAI-compatible HTTP chat completions.

Target CLIs:

- OpenCode CLI (`opencode`)
- Claude Code CLI (`claude`)
- OpenAI Codex CLI (`codex`) when available

The design must preserve wfpy behavior that users already depend on:

- existing `@agent` defaults and backward compatibility
- workflow payload structure (`inputs`, `fileInputs`, `resourceInputs`, context)
- output parsing, validation, and repair loops
- stateful chat semantics for `stateful=True`

## Current Runtime Snapshot

Today, agent execution is centered in `src/wfpy/runner.py`:

- `_step_agent()` builds the runtime payload, including file/resource handling.
- `_invoke_agent()` sends HTTP requests to provider endpoints.
- `_parse_agent_outputs()` resolves response shapes into declared output ports.
- repair loops and validators run after the first response.

This gives us a good separation already: if we abstract invocation, we can keep
most of `_step_agent()` and output validation intact.

## Feasibility Findings

Environment check in this repo:

- `opencode` is installed and supports non-interactive runs (`opencode run`).
- `claude` is installed and supports non-interactive runs (`claude --print`).
- `codex` is not installed in this environment, so behavior is unverified.

Initial conclusion:

- OpenCode and Claude Code integration are feasible.
- Codex support should be marked experimental until command contract is verified.

## Proposed Public API (Decorator / AgentSpec)

Add a backend selector while keeping defaults unchanged.

Recommended new `AgentSpec` field:

- `transport: str = "http"`

Allowed values (phase target):

- `http` (current behavior, default)
- `opencode-cli`
- `claude-cli`
- `codex-cli` (experimental)

Optional follow-up fields (not required for phase 1):

- `cli_session_mode: str = "wfpy"` (`wfpy` or `native`)
- `cli_tools_mode: str = "none"` (`none`, `read-only`, `default`)
- `cli_command: str | None` (override executable path)
- `cli_args: list[str] | None` (extra backend-specific args)

Backward compatibility requirements:

- Existing workflows without `transport` keep `http` behavior.
- Existing `provider`, `model`, `endpoint`, skills, validators keep semantics.

## Internal Architecture Proposal

Introduce a backend abstraction and move invocation logic behind it.

New internal module (proposed): `src/wfpy/_agent_backend_runtime.py`

Core types:

- `AgentInvokeRequest` (spec, prompt, payload, output ports, history, options)
- `AgentInvokeResult` (response text, firing messages, error, debug meta)

Backend interface:

- `invoke_agent_backend(request: AgentInvokeRequest) -> AgentInvokeResult`

Backend implementations:

- `HttpAgentBackend` (existing `_invoke_agent` behavior)
- `OpenCodeCliAgentBackend`
- `ClaudeCliAgentBackend`
- `CodexCliAgentBackend` (optional/experimental)

Runner wiring:

- `_step_agent()` keeps payload assembly, hooks, validators, repair loops.
- `_step_agent()` dispatches invocation via backend selector.
- runner keeps private symbol aliases for test compatibility.

## File Handling Strategy (Critical)

wfpy already builds rich file/resource input context. Keep this as the primary
cross-backend contract.

For all transports (including CLI):

- pass the same JSON payload with:
  - `inputs`
  - `fileInputs` (`path`, `content`, `sizeBytes`, `truncated`)
  - `resourceInputs` (`path`, `kind`, `listing`, `exists`)
  - `outputPorts`
  - context metadata

This ensures CLI and HTTP backends receive equivalent workflow context without
requiring backend-specific file attachment features.

Optional optimization (later):

- add backend-native file attachment if a CLI provides a stable contract and it
  improves quality without breaking determinism.

## Stateful Behavior Strategy

Phase 1 recommendation: keep wfpy-managed history as source of truth.

- reuse existing `actor.chat_history`
- prepend history into backend input (same as current behavior)
- do not rely on backend-native session state initially

Reason:

- keeps parity across HTTP and CLI backends
- avoids lock-in to backend-specific session semantics

Phase 2 option:

- allow opt-in native CLI sessions for performance/quality, while still keeping
  wfpy history in run records.

## Tool Calling and MCP Scope

Do not attempt full parity in phase 1.

Current HTTP path supports explicit tool-call loops with authorization and MCP.
CLI ecosystems have their own tool frameworks and permission models.

Phase 1 scope:

- keep existing wfpy tool-call loop only for `transport=http`
- CLI transports run in single-response mode
- reuse existing output validators and repair loops after response

Phase 2 scope:

- evaluate backend-native tool usage and permissions as separate workstream

## Error and Security Model

CLI invocation must be strict and deterministic:

- use `subprocess.run([...], shell=False)`
- enforce timeout from `agent_spec.timeout_ms`
- capture stdout/stderr and include in debug artifacts
- treat non-zero exit as invocation failure
- avoid implicit environment mutation beyond what wfpy already supports

## Phased Implementation Plan

### Phase 0 - API + backend scaffold

- add `transport` to `AgentSpec` and `@agent` decorator
- add backend selection function
- keep all behavior mapped to current HTTP backend

Acceptance:

- existing tests pass unchanged
- no behavior change when `transport` is omitted

### Phase 1 - OpenCode CLI backend

- implement `opencode` subprocess invocation
- map `model` to `opencode` model flag
- feed combined prompt + payload text
- parse response text from CLI JSON/default output

Acceptance:

- unit tests with mocked subprocess output
- one integration smoke test (gated by env) returns valid outputs JSON

### Phase 2 - Claude CLI backend

- implement `claude --print` subprocess invocation
- pass model and deterministic output settings
- parse response and route through existing output parser/repair flow

Acceptance:

- unit tests with mocked subprocess output
- one integration smoke test (env-gated)

### Phase 3 - Codex CLI backend (experimental)

- add backend only if executable and non-interactive contract are confirmed
- mark feature experimental in docs

Acceptance:

- capability check and graceful error when unavailable

### Phase 4 - optional native session and tool experiments

- evaluate backend-native sessions
- evaluate backend-native tool controls and permission integration

## Test Plan

Unit tests:

- backend selector by `AgentSpec.transport`
- command builder tests per backend
- parser tests for backend stdout/stderr variants
- timeout/non-zero exit handling

Behavioral tests:

- same payload through `http` and CLI backends produces parseable outputs
- file/resource payload parity checks in `_step_agent()`
- stateful history append/truncate unchanged

Integration tests (optional, env-gated):

- run minimal workflow with each installed backend
- assert declared output port values are produced

## Open Questions

- Should `provider` remain meaningful for CLI backends, or be ignored when
  `transport != "http"`?
- Should CLI backends be allowed to use their own tools by default, or default
  to disabled tools for deterministic workflow behavior?
- Do we want a strict JSON schema path for all CLI backends, or rely on current
  wfpy repair loop as universal fallback?

## Recommendation

Proceed with Phase 0 and Phase 1 first. This gives a low-risk path to prove the
architecture and validate workflow file-handling parity with minimal runtime
churn.
