# Changelog

All notable changes to wfpy are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and wfpy uses
[PEP 440](https://peps.python.org/pep-0440/) versioning.

## [Unreleased]

### Added
- `@source` — a node holding one resource (file, folder or web resource) that
  emits it once and opens it on double-click. The mirror of `@viewer`: a viewer
  is a sink you can open, a source is a producer you can open. The emit-once
  action is generated, because a source has no inputs and so nothing in the
  wiring can start or stop it; a class that declares its own actions keeps them.
  `path` is a task parameter, so one class serves many nodes and each carries
  its own resource.

## [1.0.0] — 2026-07-24

First stable release.

### Added
- A tutorial-ordered example suite (`examples/01`–`12`) that builds the dataflow
  model from simple tasks through schedules, priorities, networks, and
  multi-agent orchestration (fan-out/reduce, routing, repair loops). Every
  example runs offline and is exercised by the test suite, alongside a
  `docs/dataflow-concepts.md` reference.
- GitHub Actions CI (pytest across Python 3.10–3.13 + strict mypy) and a
  tag-triggered release workflow that builds and publishes the wheel to GitHub
  Releases.
- `@agent(transport="mock")` (alias `"offline"`): runs an agent actor with no
  model, no network call and no credentials. The actor fires under the normal
  dataflow rules and emits a value on every declared output port, so a graph's
  wiring — fan-out and join, repair feedback loops, guard routing, firing order
  — is exercisable in CI without spending tokens or pinning behaviour to a
  model's wording. Values come from `mock_outputs={port: value}` (camelCase
  `mockOutputs`) when given, and are otherwise synthesized from each port's
  declared type, so an `int` port receives an `int` and a `File` port is
  materialized as a real file.
- `@agent(ask_user=True)` (camelCase `askUser`): HTTP-transport agents can pause
  mid-firing and ask the user a question via the builtin `ask_user` tool, then
  continue with the reply folded into their context. The answer source resolves
  to an explicit `run(elicitation_handler=...)` callback, a console prompt
  (default when a TTY is detected or `--interactive`), or a graceful
  non-interactive fallback. New `wfpy run` flags: `--interactive` /
  `--no-interactive`, `--elicit-timeout-ms`, `--elicit-default`,
  `--elicit-require`. The `ask_user` tool is exempt from the `agent_tool_auth`
  gate and never exposes the `python`/MCP tools. Surfaced to the IDE via the
  graph export (`askUser`) and run artifacts.

### Changed
- Project description and the built-in `@agent` runtime prompt no longer
  reference "WorkflowLang"; wfpy is described as "Pythonic agentic workflows on
  actor-dataflow semantics". Agents are now told "You are executing a wfpy
  @agent task."

### Fixed
- `@agent(prompt=...)` with no `skill=` raised `ValueError: Agent has
  use_skill=true but no skill name configured` at run time, which made the
  simplest form of the decorator unusable — `use_skill` defaults to `True`, and
  every prior workflow happened to configure a skill. It now means "include the
  configured skill", so an agent with no skill named simply has none to include.
  A skill that *is* named but cannot be read still raises.

## [1.0.0b1] — 2026-07-06

First public beta.

### Added
- `@task` / `@action` / `@guard` / `@workflow` decorators with typed `Port`s and
  FIFO round-robin dataflow scheduling.
- `@agent` LLM tasks: HTTP API transport plus CLI transports (`opencode-cli`,
  `claude-cli`, `codex-cli`) and the ACP session transport (`opencode-acp`).
  Skills, MCP/LSP integration, output validators, context budgets and
  truncation strategies, stateful sessions.
- `@tool` external command tasks and `@config` environment injection.
- `wfpy` CLI: `run`, `plan` (graph export), viewers and run artifacts
  (`run.wf-run.json`, `run.wf-viewer.live.json`, `run.wf-agent-context.live.json`).
- `wfpy-sidecar`: libcst-based source-rewrite service for diagram editing
  (sidecar contract v2: capabilities discovery, canonical op aliases,
  structured errors, `checkConnection`).
- Live run observation: `RunEventStream` — a per-run localhost SSE endpoint
  streaming agent message deltas, reasoning, and tool calls, advertised via
  `run.wf-stream.live.json` (consumed read-only by the Workflow IDE).
- Graph export with stable element ids and source locations.

### Notes
- `pip install "wfpy[agent]"` is required for `@agent` workflows
  (`httpx`, `mcp`, `agent-client-protocol`).
