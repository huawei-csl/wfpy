---
name: wfpy-workflow-builder
description: Entry point for building a wfpy workflow or project. Routes to the authoring, composition and agent skills, and holds the project conventions. Use when creating or restructuring a wfpy workflow, adding a task/agent/tool/source/viewer, composing sub-workflows, or wiring an agent into a dataflow graph. Triggers: wfpy workflow, wfpy project, add a task, add an agent, sub-workflow, actor, dataflow graph, @task, @agent, @workflow, Port, connect.
---

# Building a wfpy workflow

wfpy is an actor-dataflow framework: `@task`, `@agent`, `@tool`, `@source`, `@viewer` are
nodes, `Port` declares their edges, `connect()` wires them, `@workflow` composes them, and
`run()` executes until quiescence.

Most wfpy mistakes **do not raise**. A graph with a wiring error reaches quiescence and
reports success, having done nothing. The rules in these skills are the ones that cost
real debugging time, each with the evidence for it.

## Route by what you are doing

| Doing | Read |
|---|---|
| Writing or fixing one node, or saying which of its actions fires | `wfpy-actor-authoring` |
| Wiring nodes, nesting workflows, building a loop | `wfpy-graph-composition` |
| Putting an LLM in the graph, or loading a skill into one | `wfpy-agent-nodes` |
| Starting a project, or deciding where a file goes | this file, below |

Read one leaf, not all of them.

## Project layout

One actor per file. A file holds exactly one `@task` / `@agent` / `@tool` / `@source` /
`@viewer` / `@workflow`. Data types (frozen dataclasses used as tokens) may share a module;
the rule is about actors.

```
src/<workflow>.py        top-level workflows ONLY, one per file
src/workflows/           sub-workflows
src/tasks/ agents/ tools/ sources/ viewers/
src/lib/                 shared code, no actors: paths, parsers, config, records
run.sh                   one subcommand per top-level workflow
wf-out/                  run artifacts, pinned to the repo
```

Generic helper modules do not belong at the `src/` root: that level is the list of things
this project can do, and nothing else.

**Put an empty `__init__.py` in every folder under `src/`** (`src/` itself needs none), and
write imports from `src/`:

```python
from tasks.decide import Decide
from lib.paths import REPO_ROOT
```

`wfpy run` and `wfpy plan` put the folder of the file they load on the import path, and
the parent of that folder when it is a package. A top-level workflow in `src/` loads
either way. A file one level down does not: without the `__init__.py`,
`wfpy plan src/workflows/repair_flow.py` (and the IDE drawing that file) fails with
`ModuleNotFoundError: No module named 'tasks'`.

## A top-level workflow is thin

It says what the run *is*, never how a phase works:

```python
@workflow(inputs={}, outputs={"Out": str})
def repair_softmax() -> None:
    kernel = KernelSource(path=File(str(KERNEL_PATH)))   # where the input comes from
    flow = repair_flow()                                 # what happens to it
    view = RepairReportViewer()                          # where the answer goes

    connect(kernel.Out, flow.Kernel)
    connect(flow.Report, view.Report)
    connect(flow.Verdict, "Out")
```

Three or four nodes, mostly sub-workflows. If a top-level workflow has ten actors in it, the
phases have not been separated yet.

`File(...)` takes a string. `File(KERNEL_PATH)` with a `pathlib.Path` raises
`AttributeError: 'PosixPath' object has no attribute 'startswith'`.

## Runnable from a terminal and from an IDE

A workflow file is started in two ways, and they do not run the same code:

- `wfpy run src/<workflow>.py` **imports** the file and runs its `@workflow`. The wfpy IDE
  does the same (`wfpy run <file> --out-dir <dir> --keep-intermediates`). The module is not
  `__main__` (its `__name__` is `__wfpy_user_module__`), so `main()` and the
  `if __name__ == "__main__":` block never run.
- `python src/<workflow>.py` runs the file as `__main__`, with the `run()` call it makes.

Measured with a bootstrap written in `main()`: `python flow.py` saw `SDK_HOME='/opt/sdk'`,
and `wfpy run flow.py` saw `SDK_HOME=None`. So:

- **Nothing a run needs may live only in `main()`.** Set the environment a node depends on
  where both ways pass: at import, in a `src/lib/` module the nodes import, or in the node
  that needs it. A wrapper script that sources an SDK setup script is a third way, and
  neither of the two above runs it.
- **No `sys.argv` at import time.** Whoever imports the module owns argv: under `wfpy run`,
  `sys.argv[1]` is `run` and the rest are wfpy's flags. Read configuration from the
  environment, and honour argv only under `if __name__ == "__main__"`.
- **Pin `out_dir`.** `run()` defaults it to `./wf-out`, and `wfpy run --out-dir` likewise,
  relative to the *invocation* cwd, which scatters artifacts wherever the command was
  typed. Resolve it against the repo in your own `run()` call and in `run.sh`. The IDE
  passes its own `--out-dir`.
- **Resolve paths against the repo, not the cwd**, via one `REPO_ROOT` in `src/lib/`. Count
  the `parents[]` levels when a file moves; a short root silently resolves every path to
  somewhere that does not exist, and the graph may still build.

## Before you say it works

A wfpy run that prints no error is not evidence. Check that the nodes you expected actually
fired, and that the tokens you expected actually moved:

```bash
wfpy plan src/<workflow>.py --format graph   # the graph wfpy built, as JSON
cat wf-out/<run>/run.wf-queues.json          # one step per firing, with every queue's size
ls wf-out/<run>/work/                        # every materialized file should carry a suffix
```

In `run.wf-queues.json`, `leftovers` lists the queues that still held tokens when the run
ended: tokens that a node was waiting on, or that nothing read. A run in which no node
fired writes no `run.wf-queues.json` at all. `wfpy-graph-composition` says how to read both.
