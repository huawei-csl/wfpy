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
