# TypeScript ↔ Python Runtime Parity Analysis

> Generated 2026-03-04.  Compares the wf-lang TypeScript runtime
> (`packages/cli/src/runtime.ts`) with the Python `wfpy` runtime
> (`packages/wfpy/src/wfpy/`).

---

## Legend

| Symbol | Meaning |
|--------|---------|
| ✅ | Feature exists and matches TS behaviour |
| ⚠️ | Feature exists but diverges from TS behaviour |
| ❌ | Feature missing entirely in wfpy |

---

## 1. Output Directory Structure

| Aspect | TypeScript | Python (wfpy) | Status |
|--------|-----------|---------------|--------|
| Base output dir | `<cwd>/wf-out` (default, configurable `--out-dir`) | `./wf-out` (default, configurable `--out-dir`) | ✅ |
| Run-specific subdir | `<baseOutDir>/<runId>/` | `<baseOutDir>/<runId>/` | ✅ |
| Run ID format | `YYYY-MM-DDTHH-MM-SS-mmmZ_<8-hex>` | Same format | ✅ |
| CWD for external tools | **Workflow source file directory** (`path.dirname(sourcePath)`) | `source_path` directory (resolved via `inspect` or passed explicitly) | ✅ |

### `wf-out/wf-out` nesting bug

The Python runner defaults external tool CWD to `out_dir` (the wf-out directory).
When a tool writes output files using relative paths, they land inside `out_dir`.
But the *output file paths* constructed by the runner also point into `out_dir`.
When a downstream tool receives those paths and also runs with CWD = out_dir, any
relative path resolution can double-nest.  Additionally, the TS runtime uses a
**temporary work directory** for intermediates (cleaned up after the run), while wfpy
dumps everything into `out_dir` directly.

### TODO

- [x] **P0** — Create a temporary `workDir` (`tempfile.mkdtemp`) for intermediate tool
  outputs, matching TS pattern `wf-lang-{workflowName}-XXXXXX`.  Add `--keep-work-dir`
  CLI flag.
- [x] **P0** — Generate a `runId` (ISO timestamp + 8-char UUID suffix).  Create
  `<baseOutDir>/<runId>/` for final outputs.
- [x] **P0** — Default external tool CWD to the **workflow source file directory**,
  not `out_dir`.  Resolve relative `@tool(cwd=...)` against the source dir.
- [x] **P1** — Materialize final File outputs by **copying** from `workDir` to
  `runOutDir` after workflow completion (see §5).
- [x] **P1** — Write `run-log.jsonl` to `baseOutDir` (append-only across runs).
- [x] **P1** — Clean up `workDir` on success unless `--keep-work-dir`.

---

## 2. Output File Naming

| Aspect | TypeScript | Python (wfpy) | Status |
|--------|-----------|---------------|--------|
| Intermediate file pattern | `{instanceName}__{portName}__{fireCount}{ext}` (in `workDir`) | `{instanceName}__{portName}__{fireCount}{ext}` (in `workDir`) | ✅ |
| Double-underscore separator | `__` | `__` | ✅ |
| Fire count in name | Yes — prevents collision on multiple firings | Yes — fire count tracked per actor | ✅ |
| Final output naming | Single: `{portName}{ext}`, Multiple: `{portName}__{i}{ext}` | Same — via `_materialize_outputs()` | ✅ |
| Non-File outputs | Serialized as `{portName}.json` | Serialized as `{portName}.json` in `run()` | ✅ |

### TODO

- [x] **P0** — Use `{instanceName}__{portName}__{fireCount}{ext}` pattern for intermediate
  files in `workDir`.  Use `__` as separator.
- [x] **P1** — Implement `_materialize_outputs()` to copy final File outputs from
  `workDir` to `runOutDir`:
  - Single token: `{portName}{ext}`
  - Multiple tokens: `{portName}__{i}{ext}`
  - Non-File tokens: `{portName}.json` (serialized in `run()`)

---

## 3. Temporary Working Directory

| Aspect | TypeScript | Python (wfpy) | Status |
|--------|-----------|---------------|--------|
| Created for external workflows | `fs.mkdtemp(os.tmpdir()/wf-lang-{name}-)` | `tempfile.mkdtemp(prefix=f"wf-lang-{name}-")` | ✅ |
| Configurable via CLI | `--work-dir` | `--work-dir` | ✅ |
| Automatic cleanup | Yes (unless `--keep-work-dir` or explicit `--work-dir`) | Yes (unless `--keep-work-dir` or explicit `--work-dir`) | ✅ |
| Sub-workflow workDir | `workDir/{instName}__wf/` (nested) | `workDir/{instName}__wf/` | ✅ |

### TODO

- [x] **P0** — Create `workDir = tempfile.mkdtemp(prefix=f"wf-lang-{name}-")`.
- [x] **P0** — Write all intermediate tool outputs to `workDir` instead of `out_dir`.
- [x] **P1** — Add `--work-dir` and `--keep-work-dir` CLI flags.
- [x] **P1** — Sub-workflow workDir: `workDir/{instName}__wf/`.

---

## 4. External Tool Execution

| Aspect | TypeScript | Python (wfpy) | Status |
|--------|-----------|---------------|--------|
| Default CWD | Workflow source file directory | Workflow source file directory (via `source_path`) | ✅ |
| `inheritStdio` default | `true` | `True` | ✅ |
| Search paths prepended to PATH | Yes — `searchDirs.join(':')` prepended to `PATH` env var | Yes — prepended to `PATH` | ✅ |
| `{in}` / `{out}` (no port name) | Resolves to sole port; errors if >1 | Resolves to sole port; errors if >1 | ✅ |
| Placeholder: `{param.name}` | Supported | Supported | ✅ |
| Shell mode | `@tool(shell=true)` → `spawn(..., {shell: true})` | `shell=tool_spec.shell` passed to `subprocess.run` | ✅ |
| Non-zero exit error | Throws with stderr | Throws with stderr | ✅ |
| ENOENT handling | Special message listing `@path` dirs | Good error message listing searched paths | ✅ |
| Missing input file check | `fs.stat` before spawn | `os.path.isfile` check before spawn | ✅ |

### TODO

- [x] **P0** — Default CWD to workflow source file directory.  Add `source_path` to
  `FifoPlan` or pass it through the call chain.
- [x] **P0** — Default `inherit_stdio` to `True` to match TS.
- [x] **P1** — Prepend `search_paths` to `PATH` in the spawned process env.
- [x] **P1** — Support bare `{in}` / `{out}` placeholders (resolve to sole port,
  error if multiple).
- [x] **P1** — Pass `shell=tool_spec.shell` to `subprocess.run()`.
- [x] **P1** — Check input file existence before spawning (`os.path.isfile`).

---

## 5. Run Logging & Records

| Aspect | TypeScript | Python (wfpy) | Status |
|--------|-----------|---------------|--------|
| `run-log.jsonl` | Appended to `baseOutDir/run-log.jsonl` after each run | Appended to `baseOutDir/run-log.jsonl` | ✅ |
| `run.wf-run.json` | Full run record in `runOutDir/` | Written to `runOutDir/` | ✅ |
| Run record fields | `runId`, `workflowName`, `sourcePath`, `cwd`, `startedAt/finishedAt`, `inputs`, `outputs`, `actors[]`, etc. | Matching fields in run record | ✅ |
| Queue trace (`run.wf-queues.json`) | Records queue sizes after every actor fire | `QueueTraceCollector` records queue sizes + `lastToken` after each fire | ✅ |
| Custom JSON serialization | `bigint → $wfType`, `Set → $wfType`, `Map → $wfType` | `_RuntimeJsonEncoder`: set/frozenset, `File`, Path, bytes | ✅ |
| Viewer edge tokens | Overlay edges carry `lastToken` for viewer open/diff | AST-path edges + queue-id fallback for Python graphs without AST mapping | ✅ |

### TODO

- [x] **P1** — Write `run-log.jsonl` entry to `baseOutDir` on completion.
- [x] **P1** — Write `run.wf-run.json` with full run record to `runOutDir`.
- [x] **P2** — Implement queue trace collector; write `run.wf-queues.json`.
- [x] **P3** — Custom JSON serializer for Set, Map, etc.

---

## 6. Viewer Integration

| Aspect | TypeScript | Python (wfpy) | Status |
|--------|-----------|---------------|--------|
| Live overlay file | `run.wf-viewer.live.json` (written during run, deleted after) | `_ViewerOverlayWriter` writes + cleans up | ✅ |
| Final overlay file | `run.wf-viewer.json` (in `runOutDir`) | Written by `run()` on success/failure | ✅ |
| Active actor tracking | `setActive(actor)` before each fire, `setActive(null)` after | `overlay_writer.set_active()` in `execute_plan` loop | ✅ |
| Edge last-token tracking | Updated on every enqueue | `_record_edge_token()` on every enqueue | ✅ |
| Error overlay on failure | Shows red entity for last-active actor | Error overlay with `entityInstanceName` + `message` | ✅ |
| Agent context overlay | `run.wf-agent-context.live.json` / `run.wf-agent-context.json` | `_AgentContextWriter` + `_build_agent_context_overlay` | ✅ |

### TODO

- [x] **P2** — Write viewer overlay files (`live.json` → `final.json`).
- [x] **P2** — Track active actor + edge last-token in overlay.
- [x] **P3** — Error overlay on failure (include entity name + message).
- [x] **P3** — Agent context overlay for chat history.

---

## 7. Agent (LLM) Support

| Aspect | TypeScript | Python (wfpy) | Status |
|--------|-----------|---------------|--------|
| Providers | `github`, `openai`, `anthropic`, `ollama`, `openrouter` | `github`, `openai`, `anthropic`, `ollama`, `openrouter` | ✅ |
| Config cascade | annotation → env vars → `~/.config/wf-lang/config.json` → defaults | annotation → env vars → config file → defaults | ✅ |
| File input truncation | 200,000 chars | 200,000 chars | ✅ |
| System prompt + JSON output instruction | Yes, detailed output schema prompt | Yes, `_build_agent_runtime_instruction()` | ✅ |
| Structured JSON output parsing | Parses `{"outputs": {"port": value}}` | `_parse_agent_outputs()` — 5-shape resolution (matching TS) | ✅ |
| Tool calling (Python) | Up to 6 rounds, auth model (deny/allow/policy) | Up to 6 rounds, auth model (deny-all/allow-all/policy) | ✅ |
| Streaming (SSE / NDJSON) | Full streaming with progress callback | SSE (OpenAI/GitHub/OpenRouter/Anthropic) + NDJSON (Ollama) | ✅ |
| Stateful agent chat history | Persistent `chatHistory`, sliding/summarize truncation | `trim_chat_history` + `summarize_chat_history`, `restore_chat_histories` | ✅ |
| Retry logic | 4 attempts, exponential backoff, `Retry-After` header | 4 attempts, `Retry-After` parsing, fail-fast >60s, cap 15s | ✅ |
| `fireable_without_input` | Quota decremented each fire | Implemented correctly | ✅ |
| Think-mode fallback | Retries without `think: true` if model rejects | HTTP 400 + "does not support thinking" → retry without think | ✅ |

### TODO

- [x] **P1** — Add `github` and `openrouter` providers.
- [x] **P1** — Add system prompt with structured JSON output instruction
  (`{"outputs": {"portName": value}}`), parse response accordingly.
- [x] **P1** — Increase file input truncation to 200,000 chars (match TS).
- [x] **P2** — Implement stateful agent chat history (sliding + summarize).
- [x] **P2** — Implement streaming support (SSE for OpenAI/GitHub, NDJSON for Ollama).
- [x] **P2** — Implement tool calling (Python tool, authorization model).
- [x] **P3** — Support `~/.config/wf-lang/config.json` for API key / provider defaults.
- [x] **P3** — Think-mode fallback.
- [x] **P3** — `Retry-After` header support.

---

## 8. Pipeline / Sub-Workflow Support

| Aspect | TypeScript | Python (wfpy) | Status |
|--------|-----------|---------------|--------|
| Sub-workflow execution | Recursive `executeFifoPlan` call | Recursive `execute_plan` call | ✅ |
| Sub-workflow workDir | `workDir/{instName}__wf/` | `workDir/{instName}__wf/` | ✅ |
| Search path inheritance | Parent search paths inherited by sub-workflows | Inherited (search_paths + env + source_path) | ✅ |
| Pipeline decorator | Layout hint only | Layout hint only | ✅ |

### TODO

- [x] **P1** — Inherit parent `search_paths` and `env` into sub-plans.
- [x] **P1** — Use `workDir/{instName}__wf/` for sub-workflow intermediates.

---

## 9. Validators

| Aspect | TypeScript | Python (wfpy) | Status |
|--------|-----------|---------------|--------|
| Builtin validators | `json.parse`, `xml.wellFormed` | `_execute_builtin_validator()` — json.loads + tag-stack | ✅ |
| Module validators | Dynamic JS module import | `_execute_module_validator()` — dynamic Python import with cache | ✅ |
| MCP validators | External bridge process | `_execute_mcp_validator()` — subprocess JSON bridge | ✅ |
| Validator registry | `wf-validators.json` walked up from source | `load_validator_registry()` — walk-up + env/option overrides | ✅ |
| Port `validate=[...]` | Triggers pre/post validation on external tasks | `run_port_validators()` wired into `_step_external` and `_step_agent` | ✅ |
| Enforcement modes | `off` / `warn` / `enforce` | `normalize_validation_mode()` — options → env → default `enforce` | ✅ |

### TODO

- [x] **P2** — Implement builtin `json.parse` and `xml.wellFormed` validators.
- [x] **P2** — Port-level `validate=[...]` triggers pre/post validation.
- [x] **P3** — Module validator support (dynamic Python module import).
- [x] **P3** — `wf-validators.json` registry discovery.
- [x] **P3** — MCP validator bridge.

---

## 10. Port Types & Type Checking

| Aspect | TypeScript | Python (wfpy) | Status |
|--------|-----------|---------------|--------|
| Supported types | `File`, `int`, `bool`, `string`, `bigint`, `Set`, `Map`, `List` | `File`, `Map`, `int`, `str`, `float`, `bool` (via Python typing) | ⚠️ |
| `File(ext=".md")` | Extension used for output naming | `ext` field exists on `PortDescriptor` and used | ✅ |
| `File(validate=[...])` | Triggers validators | Wired into `_step_external` and `_step_agent` | ✅ |
| `bigint` / `BigInt` | Supported | Not needed (Python has native big ints) | ✅ |
| `Set` | Supported | Not explicitly modeled | ⚠️ |
| `List` | Supported | Not explicitly modeled (use Python `list`) | ⚠️ |
| Static type checking on connections | Not implemented in TS either | Not implemented | ✅ (parity) |

### TODO

- [ ] **P3** — Consider adding `Set` and `List` type wrappers if needed.

---

## 11. CLI Features

| Aspect | TypeScript | Python (wfpy) | Status |
|--------|-----------|---------------|--------|
| `run` command | Yes | Yes | ✅ |
| `plan` command (export JSON) | Yes (via `generate`) | Yes | ✅ |
| `--out-dir` | Yes | Yes | ✅ |
| `--work-dir` | Yes | Yes | ✅ |
| `--keep-work-dir` | Yes | Yes | ✅ |
| `--keep-intermediates` | Yes (copies intermediates to `runOutDir/work/`) | Yes (`shutil.copytree` to `runOutDir/work/`) | ✅ |
| `--run-id` | Yes (override auto-generated run ID) | Yes | ✅ |
| `--verbose` / `-v` | Yes | Yes | ✅ |
| `--input` / `-i` | Yes (key=value) | Yes (key=value) | ✅ |
| `--agent-tool-auth` | Yes | Yes (`deny-all`/`allow-all`/`policy`) | ✅ |
| `--agent-tool-policy` | Yes | Yes | ✅ |
| `--agent-debug` | Yes | Yes (`agent-debug/` directory with request/response/chat-history) | ✅ |
| `--resume-chat-from` | Yes (restore agent chat history from prior run) | Yes | ✅ |
| `--queue-trace` / `--no-queue-trace` | Yes | Yes (`run.wf-queues.json` with per-step snapshots) | ✅ |
| `--validate` | Yes (`off` / `warn` / `enforce`) | Yes (`--validate off/warn/enforce`) | ✅ |
| Input type parsing | `int`, `float`, `bool`, `JSON`, file path resolution | Same | ✅ |

### TODO

- [x] **P0** — Add `--run-id` flag.
- [x] **P1** — Add `--work-dir` and `--keep-work-dir` flags.
- [x] **P2** — Add `--keep-intermediates` flag.
- [x] **P3** — Add agent-related flags (`--agent-tool-auth`, `--agent-debug`, etc.).
- [x] **P3** — Add `--validate` and `--queue-trace` flags.

---

## 12. Error Handling

| Aspect | TypeScript | Python (wfpy) | Status |
|--------|-----------|---------------|--------|
| Actor error → run fails | Yes (re-thrown) | Yes (re-raised) | ✅ |
| Error overlay for viewer | Written on failure (red border on failing entity) | Written by `run()` in error path | ✅ |
| Cleanup on error | `finally` block: remove live overlays, clean workDir | `finally` block: clean workDir | ✅ |
| Max steps safety valve | 100,000 | 100,000 | ✅ |
| Leftover token warning | Yes | Yes (in verbose mode) | ✅ |

### TODO

- [x] **P1** — Add `finally` block to clean up `workDir` on error.
- [x] **P2** — Write error overlay on failure.

---

## Priority Summary

### P0 — Correctness / blocking parity (do first)

1. ~~Create `workDir` via `tempfile.mkdtemp` for intermediate outputs~~ ✅
2. ~~Generate `runId`; create `<baseOutDir>/<runId>/` for final outputs~~ ✅
3. ~~Default external tool CWD to workflow source file directory~~ ✅
4. ~~Use `{instanceName}__{portName}__{fireCount}{ext}` for intermediate file naming~~ ✅
5. ~~Default `inherit_stdio` to `True`~~ ✅
6. ~~Add `--run-id` CLI flag~~ ✅

### P1 — Important parity

7. ~~Materialize final File outputs from `workDir` → `runOutDir` (copy)~~ ✅
8. ~~Write `run-log.jsonl`~~ ✅
9. ~~Write `run.wf-run.json`~~ ✅
10. ~~Prepend `search_paths` to subprocess `PATH`~~ ✅
11. ~~Support bare `{in}` / `{out}` placeholders~~ ✅
12. ~~Pass `shell=tool_spec.shell` to `subprocess.run()`~~ ✅
13. ~~Add `--work-dir` / `--keep-work-dir` CLI flags~~ ✅
14. ~~Inherit search_paths + env into sub-plans~~ ✅
15. ~~Agent: add `github` / `openrouter` providers~~ ✅
16. ~~Agent: structured JSON output parsing~~ ✅
17. ~~Agent: increase file truncation to 200K chars~~ ✅
18. ~~`finally` cleanup on error~~ ✅

### P2 — Nice-to-have

19. Viewer overlay files (live + final)
20. Queue trace recording + `run.wf-queues.json`
21. Active actor tracking in overlay
22. Stateful agent chat history
23. Agent streaming support
24. Agent tool calling
25. Builtin validators (`json.parse`, `xml.wellFormed`)
26. Sub-workflow workDir nesting
27. `--keep-intermediates` flag
28. Error overlay on failure

### P3 — Low priority

29. Agent config file (`~/.config/wf-lang/config.json`)
30. Module / MCP validators
31. `wf-validators.json` registry
32. Set / List type wrappers
33. Agent think-mode fallback
34. `Retry-After` header support
35. Agent-related CLI flags
36. Custom JSON serialization ($wfType)
37. `--validate` / `--queue-trace` CLI flags
