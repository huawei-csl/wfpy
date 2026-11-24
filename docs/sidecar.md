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

The graph is read from the source and nothing is run, so it is there for a
file that does not import. It is one a reader can draw:

- `nodes` lists the workflow's own inputs and outputs first (inputs by name,
  then outputs by name), then the instances in the order written. An instance
  is a call assigned to a name: `a = A()`, or each of `a, b = A(), B()`.
- Every node has `ports`. An instance's are those its class declares in
  `class Ports:` (a nested workflow's `inputs` and `outputs`, a `loop` or
  `if_`'s own), followed through the project's imports, then any port a
  connection names that the declaration does not have.
- The `id` of a port is the `portId` an edge ends on, so each end of each edge
  is a port of a node of the graph.

```json
{
  "nodes": [
    {
      "id": "wf:input:A", "nodeId": "wf:input:A", "kind": "wf-input",
      "label": "A", "scope": "scope:root",
      "ports": [
        {"id": "wf:A", "name": "A", "direction": "out", "type": "int", "role": "data"}
      ]
    },
    {
      "id": "scale_a", "nodeId": "n:scale_a", "kind": "task", "ref": "Scale",
      "args": [], "kwargs": {"scale": "2"}, "scope": "scope:root",
      "ports": [
        {"id": "n:scale_a:p:In", "name": "In", "direction": "in", "type": "int",
         "role": "data", "location": {"file": "/abs/path/to/workflow.py", "line": 39}},
        {"id": "n:scale_a:p:Out", "name": "Out", "direction": "out", "type": "int",
         "role": "data", "location": {"file": "/abs/path/to/workflow.py", "line": 40}}
      ]
    }
  ],
  "edges": [
    {
      "id": "e1", "edgeId": "e:wf:A->n:scale_a:p:In", "scope": "scope:root",
      "from": {"kind": "workflow", "port": "A", "portId": "wf:A"},
      "to": {"kind": "node", "nodeId": "scale_a", "port": "In", "ref": "Scale",
             "portId": "n:scale_a:p:In"}
    }
  ]
}
```

A workflow input has direction `out` and an output `in`: inside the workflow
an input is where tokens come out. A port whose direction the source leaves
open (`Port[int]()`) is an output by the names `out`, `output`, `result`,
`report` and `summary`, and an input by any other, as at run time. A `loop` or
`if_` node carries the `label` the runtime names it by (`loop`, `loop_1`,
`if`); its `id` is the name the source binds it to, which the ops go by.

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
```

### Port edits

```json
{ "op": "wfpy.renamePort", "args": {"entity": "TaskA", "portDirection": "input", "portName": "In", "newValue": "Input"} }
{ "op": "wfpy.updatePortType", "args": {"entity": "TaskA", "portDirection": "output", "portName": "Out", "newValue": "int"} }
{ "op": "wfpy.reorderEntityPorts", "args": {"entity": "TaskA", "order": ["Model", "Image", "Program", "Network"]} }
```

`reorderEntityPorts` puts a definition's ports in the order a diagram draws
them: the lines of a class's `class Ports`, each with its comments, or the
entries of a `@workflow` decorator's `inputs=` / `outputs=` dicts, each dict
in the order its names have in `order`. `order` names every port once,
by the name the diagram shows (`Port[int]("Output")` is `Output`). Nothing
else of the file changes. Refused, with the file left as it was: an order
that leaves out a port or names one twice, ports sharing a line or declared
twice, and a dict comment that would have no line to go to.

### Wrap / unwrap

```json
{ "op": "wfpy.wrapInIf", "args": {"workflow": "MyWorkflow", "node_names": ["a1"], "condition_expr": "flag"} }
{ "op": "wfpy.unwrapControl", "args": {"workflow": "MyWorkflow", "name": "cond1"} }
```
