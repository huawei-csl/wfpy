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
| Writing or fixing one node | `wfpy-actor-authoring` |
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

Generic helper modules do not belong at the `src/` root — that level is the list of things
this project can do, and nothing else.

## A top-level workflow is thin

It says what the run *is*, never how a phase works:

```python
@workflow(inputs={}, outputs={"Out": str})
def repair_softmax() -> None:
    kernel = KernelSource(path=File(KERNEL_PATH))   # where the input comes from
    flow = repair_flow()                            # what happens to it
    view = RepairReportViewer()                     # where the answer goes

    connect(kernel.Out, flow.Kernel)
    connect(flow.Report, view.Report)
    connect(flow.Verdict, "Out")
```

Three or four nodes, mostly sub-workflows. If a top-level workflow has ten actors in it, the
phases have not been separated yet.

## Runnable from a terminal and from an IDE

Both, from the first commit — an IDE launches the module directly and shares none of the
shell's environment.

- **No `sys.argv` at import time.** Whoever imports the module owns argv, and the wfpy IDE
  extension passes its own flags, so `sys.argv[1]` can be `--workflow`. Read configuration
  from the environment, and honour argv only under `if __name__ == "__main__"`.
- **Bootstrap the environment in `main()`.** A wrapper script that sources an SDK setup
  script does not run when an IDE launches the file. Do that work in the entry point so both
  paths behave the same.
- **Pin `out_dir`.** wfpy defaults it to `./wf-out` relative to the *invocation* cwd, which
  scatters artifacts wherever the command was typed. Resolve it against the repo.
- **Resolve paths against the repo, not the cwd**, via one `REPO_ROOT` in `src/lib/`. Count
  the `parents[]` levels when a file moves; a short root silently resolves every path to
  somewhere that does not exist, and the graph may still build.

## Check your work mechanically

`wfpy-skills lint src/` checks the rules in these skills that can be checked statically:
mutable field defaults, `File` ports without `ext=`, `argv` at import, an `@agent` with no
`mock_outputs`, a loop with no budget, more than one actor in a file.

`wfpy-skills new actor <kind> <Name>` and `wfpy-skills new project <dir>` scaffold files that
already follow the conventions.

## Before you say it works

A wfpy run that prints no error is not evidence. Check that the nodes you expected actually
fired, and that the tokens you expected actually moved:

```bash
cat wf-out/<run>/run.wf-queues.json     # a producer that emitted nothing is visible here
ls wf-out/<run>/work/                   # every materialized file should carry a suffix
```
