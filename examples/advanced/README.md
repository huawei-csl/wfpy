# Advanced examples: workflows that write workflows

The tutorial in [`examples/`](../README.md) builds graphs that are written
before the run. These five build one during it: an agent emits a workflow file
as a token, and a node of the same graph runs that file. Nothing in wfpy was
added for this. A workflow file is text, an `@agent` can emit text on a `File`
port, and `wfpy run` is a command a `@tool` node can call.

Read the tutorial first (09 at least); these use its vocabulary.

| | Example | Shows |
|---|---|---|
| 01 | [`01_workflow_as_token.py`](01_workflow_as_token.py) | The pattern in three nodes: an author, a tool node that runs what it wrote, a reader |
| 02 | [`02_recursive_workflow.py`](02_recursive_workflow.py) | Recursion: the generated workflow writes and runs the next, down to a base case |
| 03 | [`03_data_shaped_workflow.py`](03_data_shaped_workflow.py) | An author that is told about the data: one node per file, so the graph follows the folder |
| 04 | [`04_recursive_delegation.py`](04_recursive_delegation.py) | The pattern as a sub-workflow the author may use: the depth of the runs follows the folder tree |
| 05 | [`05_repair_generated_workflow.py`](05_repair_generated_workflow.py) | A generated workflow that fails: run it from a task, and send it back to be corrected |

Beside them: [`authoring.py`](authoring.py) holds the nodes 03 and 04 share
(and that a generated workflow imports), [`scripted_author.py`](scripted_author.py)
is the offline stand-in for their author, [`primer.md`](primer.md) is the page
of wfpy a real author is given, and `data/` is what they count.

## Running them

```bash
wfpy run examples/advanced/01_workflow_as_token.py --input X=21
python examples/advanced/04_recursive_delegation.py
```

Every example runs offline by default, with no model, no network and no
optional dependency, and `tests/test_examples_advanced.py` runs each one on
every test run. 01, 02 and 05 use `transport="mock"`; in 03 and 04 a script
writes the workflow (the mock transport replies one fixed text, and there each
folder needs its own).

03, 04 and 05 take a real author:

```bash
pip install "wfpy[agent]"
wfpy connectors                      # which coding agents are available
WFPY_EXAMPLE_TRANSPORT=acp wfpy run examples/advanced/04_recursive_delegation.py
```

`WFPY_EXAMPLE_CONNECTOR` (default `claude`) and `WFPY_EXAMPLE_MODEL` (default
`sonnet`) choose the agent and the model. A real run spends tokens: one model
turn in 03, four in 04, one or more in 05.

## What to watch for

The pattern works with what wfpy has, and that also means wfpy does not look
after it for you.

- **The generated file is code, and it runs with your rights.** `wfpy run`
  imports the file. There is no sandbox, and a coding agent may also use its
  own tools during its turn: in one run of 04 an author left a scratch file in
  the folder it was told about. The prompt now says not to, which is a
  request and not a guarantee. Run a real author only where you would run
  code that agent wrote.
- **The result does not come back by itself.** The tool node's token is the
  folder of the child run. A node has to open `child/run.wf-run.json` in it and
  emit what the child gave (`Read` in 01).
- **A failed child ends the parent, when a `@tool` runs it.** Run it from a
  `@task` to get the failure as a token (05).
- **Nothing bounds the recursion.** 02 ends because its base case is a graph
  with no author. 04 counts the levels through the tool node's environment,
  and `delegate` refuses in a run that is at the limit.
- **Nothing bounds a child's run time.** A `@tool` node has no time limit. A
  generated workflow that never ends holds its parent, and one whose node
  fires without end (a source with no latch) is stopped by wfpy only after
  100000 steps, with a large trace on disk by then. 05 runs the child from a
  task, with a time limit.
- **The parent does not see inside the child.** Its trace and its overlay show
  one tool firing. The child has a complete run folder of its own, under the
  parent's work folder, and its run record lists its nodes (`Shape` in 03).
- **A generated file is not beside your modules.** It is written in the run's
  work folder, so what it imports from your project reaches it through
  `PYTHONPATH` on the tool node (`Execute` in `authoring.py`). A tool node's
  working folder is the folder of its workflow file: give the paths in full,
  since a relative `PYTHONPATH` does not mean in the child what it means in
  your shell.
- **Each level is a process.** About a quarter of a second for `wfpy run` to
  start, before any model turn. Its run folder is inside the one above, so the
  paths grow with every level.

What is not here: a generated workflow that is merged into the running graph.
The child is always a run of its own.
