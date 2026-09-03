# wfpy — Pythonic agentic workflows on actor-dataflow semantics

[![CI](https://github.com/huawei-csl/wfpy/actions/workflows/ci.yml/badge.svg)](https://github.com/huawei-csl/wfpy/actions/workflows/ci.yml)

A Pythonic framework for agentic workflows, built on actor-dataflow semantics:
agents and tasks are actors in a dataflow graph, connected by FIFO queues and
fired when their inputs are ready. Independent work runs concurrently by
default, feedback loops are ordinary edges, and the graph is exportable as data.

**New to dataflow?** Read [Dataflow concepts](docs/dataflow-concepts.md) for the
model — actors, ports, tokens, firings, guards, schedules, priorities — and work
through the tutorial in [`examples/`](examples/), which builds it up one runnable
step at a time.

wfpy is fully typed and ships `py.typed` for IDE support.

## Install

Each [release](https://github.com/huawei-csl/wfpy/releases) attaches a built
wheel. Install a specific version straight from GitHub:

```bash
# pinned release wheel (no clone needed)
pip install "wfpy @ https://github.com/huawei-csl/wfpy/releases/download/v1.0.0/wfpy-1.0.0-py3-none-any.whl"

# or download the .whl from the Releases page and install the file
pip install ./wfpy-1.0.0-py3-none-any.whl

# or build from a tagged source
pip install "git+https://github.com/huawei-csl/wfpy@v1.0.0"
```

Add the `agent` extra for the `@agent` transports (HTTP API, MCP tools,
opencode-acp): append `[agent]` to the requirement, e.g.
`pip install "wfpy[agent] @ <wheel-url>"`.

For local development, clone and install editable: `pip install -e ".[dev]"`.

```bash
wfpy run my_workflow.py          # run a workflow
```

## Minimal example

```python
from wfpy import task, workflow, connect, Port

@task
class Doubler:
    factor: int                       # parameter

    class Ports:
        In  = Port[int]()
        Out = Port[int]()

    def action(self, x: int) -> int:
        return x * self.factor

@workflow
def double_pipeline():
    d1 = Doubler(factor=2)
    d2 = Doubler(factor=3)

    connect("Input", d1.In)
    connect(d1.Out, d2.In)
    connect(d2.Out, "Output")
```

## Features

| Feature | Status |
|---|---|
| `@task` with typed ports | ✅ |
| Multi-action tasks with `@action` / `@guard` | ✅ |
| `@workflow` with `connect()` wiring | ✅ |
| FIFO round-robin scheduler | ✅ |
| `@tool(cmd=...)` external process tasks | ✅ |
| `@agent(prompt=...)` LLM tasks | ✅ |
| `@agent(transport="mock")` offline agents (no model, no credentials) | ✅ |
| `@context(read=[...], write=[...])` task context policy | ✅ |
| `@viewer(action_name=..., inputs=[...], viewType=...)` sink/view tasks | ✅ |
| `@source(viewType=...)` one resource — file, folder or URL — emitted once | ✅ |
| Nested workflows | ✅ |
| CLI (`wfpy run`) | ✅ |
| JSON plan IR export | ✅ |

## Running agents offline

`transport="mock"` fires an `@agent` actor without a model, a network call or
any credentials. The actor still obeys the normal firing rules and still emits a
value on every declared output port, so the *shape* of a graph — fan-out and
join, repair feedback loops, guard routing, firing order — can be tested in CI
without spending tokens or depending on how a model happens to word things.

```python
@agent(prompt="Summarize the input.", transport="mock",
       mock_outputs={"Summary": "a concise summary"})
class Summarizer:
    class Ports:
        In = Port[str](direction="in")
        Summary = Port[str](direction="out")
        Score = Port[int](direction="out")   # unlisted → synthesized as 0
```

Ports absent from `mock_outputs` are filled in from their declared type, so an
`int` port receives an `int` and a `File` port is materialized as a real file.
Swapping back to a real transport changes nothing else about the graph.

## Shared context decorator

Use `@context(...)` to attach shared-context read/write policy metadata to task
classes. It can be applied directly to a class or stacked with `@task` in
either order.

`@guard(...)` predicates are supported on both explicit `@action(...)` methods
and implicit default `action(...)` methods.

## Architecture

```
wfpy/
├── types.py      # Port, Resource, File, Map type descriptors
├── core.py       # @task, @action, @guard, @workflow, @agent, @tool decorators
├── graph.py      # connect(), WorkflowGraph builder
├── runner.py     # FIFO round-robin scheduler
├── cli.py        # CLI entry-point (wfpy run ...)
└── __init__.py   # public API re-exports
```

Documentation lives under [docs/](docs/): the [dataflow model](docs/dataflow-concepts.md)
and the [sidecar](docs/sidecar.md).

## Resource draft

wfpy includes a draft `Resource` value type for non-file locators (for example
URLs, folders, and endpoint-like references):

```python
from wfpy import Port, Resource

class Ports:
    In = Port[Resource](direction="in")
    Out = Port[Resource](direction="out", ext=".json")
```

`File` remains supported and is treated as a file-focused specialization of
`Resource` for backward compatibility.

### Kind helpers

wfpy also exposes basic helpers for classifying resource locators:

```python
from wfpy import (
    infer_resource_kind,
    is_url_resource,
    is_http_resource,
    is_folder_resource,
    is_file_resource,
)

infer_resource_kind("https://example.com/data")  # "http"
is_url_resource("s3://bucket/key")              # True
is_folder_resource("/tmp/my-dir/")              # True
```

## Agent skills (`SKILL.md`) draft

wfpy supports name-based skill lookup for agents:

```python
from wfpy import agent, Port

@agent(
    skill="writer",
    use_skill=True,
    use_prompt=True,
    use_skill_hooks=True,
    prompt="Focus on concise release notes."
)
class Writer:
    class Ports:
        In = Port[dict](direction="in")
        Out = Port[dict](direction="out")
```

Skill names resolve by convention under your runtime root:

- `.wf/skills/<skill-name>/SKILL.md`
- `.wf/skills/<skill-name>/scripts/<hook>.py|.sh`

wfpy also searches Claude-compatible skill roots in order:

- `<runtime-root>/.claude/skills/<skill-name>/SKILL.md`
- `<cwd>/.wf/skills/<skill-name>/SKILL.md`
- `<cwd>/.claude/skills/<skill-name>/SKILL.md`
- `~/.claude/skills/<skill-name>/SKILL.md`

Prompt composition order:

1. `SKILL.md` body (if `use_skill=True`)
2. `prompt` (if `use_prompt=True`)
3. wfpy runtime instruction (always appended by runtime)

Missing enabled skill files are runtime errors.

## Claude agent format compatibility (draft)

wfpy agents can also reference Claude-style markdown agent profiles by name:

```python
from wfpy import agent, Port

@agent(
    claudeAgent="code-reviewer",
    useClaudeAgent=True,
    usePrompt=True,
    prompt="Adapt output to this workflow's output ports."
)
class Reviewer:
    class Ports:
        In = Port[dict](direction="in")
        Out = Port[dict](direction="out")
```

Discovery roots (ordered):

- `<runtime-root>/.claude/agents`
- `<cwd>/.claude/agents`
- `~/.claude/agents`

Supported profile fields in compatibility mode:

- `name`, `description`, markdown body prompt, `skills`

Ignored with non-fatal warnings:

- `model` (wfpy keeps the current model/provider selection)
- `tools`, `disallowedTools`, `permissionMode`, `maxTurns`, `mcpServers`,
  `hooks`, `memory`, `background`, `effort`, `isolation`
