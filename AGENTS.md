# AGENTS.md

## Scope

- `wfpy` is a standalone package in this directory; use this directory as the project root.
- `README.md` still shows the older monorepo-root install path (`pip install -e packages/wfpy`). From here, use `.venv/bin/python -m pip install -e ".[dev]"`; add `.[agent]` only when touching agent/MCP features.

## Fast Checks

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest tests/test_runner.py -k nested_function_workflow_composition -q
.venv/bin/python -m pytest tests/test_cli.py -q
.venv/bin/python -m pytest tests/test_rewrite.py -q
.venv/bin/python -m mypy src/wfpy --strict
```

- `mypy` is currently not clean: `src/wfpy/runner.py:2796` assigns `str | None` back into `response_text` under `--strict`.
- `ruff` is configured in `pyproject.toml`, but this repo has no local CI or pre-commit enforcing it.

## Read First

- `src/wfpy/core.py`: decorator semantics, field/port classification, agent alias normalization.
- `src/wfpy/graph.py`: active graph context, actor registration, instance renaming, `connect()/if_()/loop()`.
- `src/wfpy/runner.py`: `_build_workflow_graph()`, FIFO scheduler, run artifacts, agent transport plumbing.
- `src/wfpy/cli.py`: module loading and `sys.path` bootstrapping.
- `src/wfpy/rewrite.py` and `src/wfpy/sidecar.py`: sidecar rewrite ops and nested workflow export.
- `tests/conftest.py`: env cleanup fixtures and `mock_invoke_agent`.

## Gotchas

- Nested function-style workflow composition depends on both `core._active_wf_builder_depth` and `runner._build_workflow_graph()`. The wrapper/proxy path and the locals-snapshot rename path must stay in sync.
- Actor instance names come from workflow locals at builder return, even without `connect()` (`tests/test_instance_names.py`). If names change, inspect locals capture/rename before touching graph export.
- `connect()`, `if_()`, and `loop()` only work with an active `graph._current_graph`.
- `Port[...]()` defaults to `direction="inout"`. Names `out|output|result|report|summary` are treated as outputs; everything else defaults to input. The same heuristic drives auto-inferred actions and class-based workflow I/O.
- Annotated fields with no default are workflow parameters. Annotated fields with defaults, or names starting with `_`, are persistent task state.
- `@agent` accepts both snake_case and GUI/editor camelCase kwargs (`claudeAgent`, `usePrompt`, `lspServers`, `fireableWithoutInput`, etc.); camelCase wins when both are present.
- `_invoke_agent()` must keep returning `(response_text, firing_messages, error, debug_meta)`; `tests/conftest.py::mock_invoke_agent` and runner tests patch that exact 4-tuple.
- `cli._load_module()` prepends the workflow module dir, its package parents, and the nearest project root to `sys.path`. Prefer fixing that logic over adding per-example `sys.path` hacks.
- `wfpy plan --format graph --best-effort` falls back to the AST partial-graph builder in `src/wfpy/partial.py`; plain `plan` format does not.
- `RewriteEngine.export_workflow_graph()` resolves nested child workflows recursively, including `from ... import ...` imports, with recursion guards.
- `run()` defaults `queue_trace=True`; normal runs write `run.wf-queues.json`, `run.wf-run.json`, and append `run-log.jsonl`. `keep_intermediates=True` copies `work/` into the run output.
- CLI `--agent-tools` auto-discovers `agent-tools.json` beside the workflow file. Without the flag, the registry is not auto-loaded.
- Class-based `@workflow` execution currently reads a `connections` attribute only; the `build()` method mentioned in a runner comment is not implemented.
- Do not use system `/tmp` for repo work, generated test artifacts, or scratch debugging. Use the repo-local `.tmp/` directory instead, and keep temporary investigation artifacts there unless the user explicitly asks otherwise.
