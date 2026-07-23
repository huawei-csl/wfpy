# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`wfpy` is a Pythonic framework for agentic workflows built on actor-dataflow semantics: FIFO-queued dataflow scheduling with round-robin actor execution, multi-action tasks with guards, and external tool / LLM agent integration. Python 3.10+.

## Commands

```bash
# Install (use the package dir as project root, not the monorepo root)
.venv/bin/python -m pip install -e ".[dev]"          # core dev
.venv/bin/python -m pip install -e ".[dev,agent]"    # + agent/MCP features

# Tests
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest tests/test_runner.py -k nested_function_workflow_composition -q
.venv/bin/python -m pytest tests/test_cli.py -q
.venv/bin/python -m pytest tests/test_rewrite.py -q

# Type check — clean; keep it that way. strict=true lives in pyproject, so the
# bare command is already strict. `wfpy.acp_client` is exempted there (unannotated).
.venv/bin/python -m mypy src/wfpy

# Lint / format (configured in pyproject.toml, no CI/pre-commit enforcing it)
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format src tests

# Run a workflow
.venv/bin/wfpy run <workflow>.py
```

## Architecture

```
src/wfpy/
├── types.py       Port, Resource, File, Map type descriptors + kind helpers
├── core.py        @task, @action, @guard, @workflow, @agent, @tool, @viewer,
│                  @context, @streaming, @pipeline, @keep, @config decorators;
│                  field/port classification; agent alias normalization
├── graph.py       connect(), if_(), loop(); WorkflowGraph builder; active
│                  graph context (_current_graph); actor registration + rename
├── runner.py      Queue, RuntimeActor, FifoPlan; build_plan(), execute_plan(),
│                  run() orchestration; _step_actor dispatch; _step_agent,
│                  _step_internal, _step_workflow, _step_control_*; _invoke_agent()
├── cli.py         `wfpy run|plan` entry-point; _load_module() sys.path bootstrap
├── rewrite.py     RewriteEngine — sidecar rewrite ops + nested workflow export
├── sidecar.py     Sidecar runtime (nested workflow export + invocation)
├── sidecar_cli.py Sidecar CLI entry-point (wfpy-sidecar-op)
├── partial.py     AST partial-graph builder (used by `wfpy plan --format
│                  graph --best-effort` fallback)
├── workflow_identity.py  Workflow identity helpers
├── _agent_*_runtime.py   Agent subsystem modules (cli, io, prompt, request,
│                         tools, validation) — split out of runner.py
├── _agent_staging_runtime.py  CLI agent file input staging + payload building
├── _step_external_runtime.py  Subprocess tool actor execution + placeholder sub
├── _action_runtime.py         Internal action execution (_try_fire_action)
├── _invoke_runtime.py         Agent LLM invocation, retry, chat history
├── _graph_export.py           Graph JSON export (diagram IR serialization)
├── _graph_context.py          Shared _current_graph variable (breaks core↔graph cycle)
├── _agent_decorator.py        @agent decorator config parsing helpers
├── _run_finalization.py       Run finalization (overlays, artifacts, run record)
├── _context_runtime.py   Shared-context (@context) runtime
├── _lsp_client.py        LSP client for agent LSP integration
├── _mcp_client.py        MCP (Model Context Protocol) client
├── _plan_outputs_runtime.py  Plan output serialization
├── _run_artifacts.py     Run artifact writers (run.wf-queues.json, etc.)
└── _validation_runtime.py    Input/port validation + run_port_validators
```

Public API is re-exported from `src/wfpy/__init__.py`.

## Key Concepts

- **Ports**: `Port[T]()` defaults to `direction="inout"`. Names `out|output|result|report|summary` are treated as outputs; everything else defaults to input. Same heuristic drives auto-inferred actions and class-based workflow I/O.
- **Fields**: Annotated fields with no default = workflow parameters. Annotated fields with defaults, or names starting with `_` = persistent task state.
- **Active graph**: `connect()`, `if_()`, and `loop()` only work when `graph._current_graph` is set (inside a `@workflow` body).
- **Agent kwargs**: `@agent` accepts both snake_case and GUI/editor camelCase (`claudeAgent`, `usePrompt`, `lspServers`, `fireableWithoutInput`, etc.); camelCase wins when both are present.
- **Agent invoke contract**: `_invoke_agent()` returns a 4-tuple `(response_text, firing_messages, error, debug_meta)`. `tests/conftest.py::mock_invoke_agent` and runner tests patch that exact shape — do not change it.
- **Class-based `@workflow`**: reads a `connections` attribute only; the `build()` method mentioned in a runner comment is not implemented.

## Gotchas

- Nested function-style workflow composition depends on both `core._active_wf_builder_depth` and `runner._build_workflow_graph()`. The wrapper/proxy path and the locals-snapshot rename path must stay in sync.
- Actor instance names come from workflow locals at builder return, even without `connect()` (see `tests/test_instance_names.py`). If names change, inspect locals capture/rename before touching graph export.
- `cli._load_module()` prepends the workflow module dir, its package parents, and the nearest project root to `sys.path`. Prefer fixing that logic over adding per-example `sys.path` hacks.
- `RewriteEngine.export_workflow_graph()` resolves nested child workflows recursively, including `from ... import ...` imports, with recursion guards.
- `run()` defaults `queue_trace=True`; writes `run.wf-queues.json`, `run.wf-run.json`, and appends `run-log.jsonl`. `keep_intermediates=True` copies `work/` into the run output.
- CLI `--agent-tools` auto-discovers `agent-tools.json` beside the workflow file. Without the flag, the registry is not auto-loaded.
- `wfpy plan --format graph --best-effort` falls back to the AST partial-graph builder in `partial.py`; plain `plan` does not.
- Do not use system `/tmp` for repo work or scratch — use the repo-local `.tmp/` directory.

## Tests

- `tests/conftest.py` provides env cleanup fixtures and `mock_invoke_agent`.
- `test_acp_simple.py` is skipped unless the optional `agent` extra is installed.
- There are no `examples/` in this repo yet; nothing in `tests/` may depend on
  example files, or a missing example will abort collection for the whole suite.
