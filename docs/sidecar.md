# wfpy sidecar

The sidecar is a JSONL service that applies semantic diagram edits to Python
workflow source using libcst. It reads one JSON object per line on stdin and
emits one JSON response per line on stdout.

## Run

```bash
wfpy-sidecar
```

## One-shot helper

```bash
wfpy-sidecar-op --file workflow.py --op wfpy.createNode --args '{"workflow": "MyWorkflow", "type": "TaskA", "name": "a1"}'
```

## Request format

```json
{
  "file": "path/to/workflow.py",
  "op": "wfpy.createNode",
  "args": {
    "workflow": "MyWorkflow",
    "type": "Doubler",
    "name": "d1",
    "params": {"factor": 2}
  }
}
```

## Response format

```json
{
  "status": "ok",
  "file": "path/to/workflow.py",
  "revision": "sha256:...",
  "message": "ok"
}
```

## Supported ops

### Workflow graph export (nested)

`wfpy.exportWorkflowGraph` returns the selected workflow graph and, when
possible, recursively includes nested workflow children under `children`.

- Same-file nested workflows are discovered automatically.
- Imported nested workflows are resolved from `from ... import ...` statements
  to local module files and exported recursively.
- Recursive references are guarded to avoid infinite loops.

Response shape includes:

```json
{
  "workflow": "ParentWorkflow",
  "file": "/abs/path/to/workflow.py",
  "nodes": [],
  "edges": [],
  "children": [
    {
      "instance": "child1",
      "workflow": "ChildWorkflow",
      "source": "same-file|import",
      "graph": {
        "workflow": "ChildWorkflow",
        "file": "/abs/path/to/child_module.py",
        "nodes": [],
        "edges": []
      }
    }
  ]
}
```

### Create node

```json
{
  "op": "wfpy.createNode",
  "args": {
    "workflow": "MyWorkflow",
    "type": "TaskA",
    "name": "a1",
    "params": {"x": 1},
    "scope_control": "cond1",
    "scope_branch": "then"
  }
}
```

### Connect ports

Use either `from_expr` / `to_expr` or structured `from_port` / `to_port`:

```json
{
  "op": "wfpy.connect",
  "args": {
    "workflow": "MyWorkflow",
    "from_port": {"type": "actor", "actor": "a1", "port": "Out"},
    "to_port": {"type": "control", "control": "loop1", "port": "iter"}
  }
}
```

### Control nodes

```json
{ "op": "wfpy.createIf", "args": {"workflow": "MyWorkflow", "name": "cond1", "condition_expr": "flag"} }
{ "op": "wfpy.createLoop", "args": {"workflow": "MyWorkflow", "name": "loop1", "iterable_expr": "items"} }

### Port edits

```json
{ "op": "wfpy.renamePort", "args": {"entity": "TaskA", "portDirection": "input", "portName": "In", "newValue": "Input"} }
{ "op": "wfpy.updatePortType", "args": {"entity": "TaskA", "portDirection": "output", "portName": "Out", "newValue": "int"} }
```
```

### Wrap / unwrap

```json
{ "op": "wfpy.wrapInIf", "args": {"workflow": "MyWorkflow", "node_names": ["a1"], "condition_expr": "flag"} }
{ "op": "wfpy.unwrapControl", "args": {"workflow": "MyWorkflow", "name": "cond1"} }
```

### Agent skills

`wfpy.agentSkills` answers which skills and agent presets a run of `file`
finds when started in `cwd` (an absolute directory; the sidecar's own when
absent or relative), with the sidecar's environment (`WF_SKILLS_ROOT`,
`HOME`, `CODEX_HOME`, ...). It runs the runtime's own lookup, so an editor
offers exactly what `use_skill` and `claude_agent` resolve. The file need not
parse. `features.agentSkillsQuery` advertises it.

```json
{ "op": "wfpy.agentSkills", "file": "flows/review.py", "args": {"cwd": "/work/project"} }
```

The answer's `diagnostic`:

- `runtimeVersion`: the line `wfpy --version` prints (`wfpy 1.1.0 from
  <package directory> (python 3.12)`). An editor that lists skills with the
  sidecar and runs workflows with the `wfpy` command compares the two before
  trusting the list for a run, with no other request.
- `roots`: the folders searched, in order, each `{path, folder, scope}`:
  `folder` as a reader knows it (`.claude/skills`, `.agents/skills`,
  `~/.agents/skills`, `$CODEX_HOME/skills`, `WF_SKILLS_ROOT`), `scope` one of
  `explicit`, `project`, `cwd`, `user`.
- `ignored`: other agent tools' folders reached but not trusted,
  `{path, folder, reason}`.
- `skills`: one per name a run can be given, `{name, found, dir, root,
  folder, scope, skillMd, problem, description, hooks, scripts, warnings}`.
  `dir` is the folder a run takes. `problem` is the error a run that takes
  it fails with reading its `SKILL.md`; a name whose folders hold no
  `SKILL.md` has `found: false`. `hooks` maps `pre` and `post`, as the
  runtime reads them from the front matter, to `{name, script, interpreter,
  problem}`; `scripts` names the hooks `scripts/` has a script for.
- `presetRoots` and `presets`: the same for agent presets, each preset
  `{name, path, root, folder, scope, problem, description, skills,
  ignoredFields, warnings}`, `skills` being the skills it preloads, each with
  the folder a run takes or the reason it fails.

`getCapabilities` returns the same `runtimeVersion`. A sidecar without the
op refuses it as an unknown op (`diagnostic.code` `unknown_operation`),
which an editor reads as a wfpy that lists no skills this way.
