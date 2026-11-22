"""The nodes of a workflow that writes workflows. Used by 03 and 04, and
imported by the workflows their author writes.

    Listing --Task--> Author --Workflow (.py)--> Execute --Run--> Read --Answer-->

``delegate`` is the last three as one sub-workflow: a task in, an answer out.
A generated workflow may use it as a node, which is what makes 04 recursive.

Offline (the default) the author is a script; set ``WFPY_EXAMPLE_TRANSPORT=acp``
for a model. ``WFPY_EXAMPLE_CONNECTOR`` (``claude``) and ``WFPY_EXAMPLE_MODEL``
(``sonnet``) choose which.
"""

import json
import os
import sys
from pathlib import Path

from wfpy import File, Port, action, agent, connect, task, tool, workflow

HERE = Path(__file__).resolve().parent

TRANSPORT = os.environ.get("WFPY_EXAMPLE_TRANSPORT", "mock")
CONNECTOR = os.environ.get("WFPY_EXAMPLE_CONNECTOR", "claude")
MODEL = os.environ.get("WFPY_EXAMPLE_MODEL", "sonnet")

# How deep this process is in the chain of runs: 0 for the run you start, and
# each Execute starts its child one level down. wfpy does not bound a
# recursion, so `delegate` does: a run at the limit may not delegate again, and
# an author that delegates without end stops there, with an error that says so.
LEVEL = int(os.environ.get("WFPY_EXAMPLE_LEVEL", "0"))
MAX_LEVEL = int(os.environ.get("WFPY_EXAMPLE_MAX_LEVEL", "5"))

PROMPT = f"""You write wfpy workflows. This is all of wfpy you need:

{(HERE / "primer.md").read_text(encoding="utf-8")}

`inputs.Task` describes one folder: `folder` is its absolute path, `files` the names of
the .txt files directly in it, `folders` the names of its sub-folders.

Write ONE Python file holding a wfpy workflow that counts the words of the folder's .txt
files, sub-folders included. How to split the work is a rule, not a suggestion:

- one counting node per name in `files` (one class, the file's absolute path as its
  parameter);
- one `delegate()` node per name in `folders`, fed by a `Listing` node for that
  sub-folder. Do not open or count the files of a sub-folder yourself;
- one node that joins all the counts and all the delegates' answers.

The workflow has two outputs: `Total` (an int) and `Counts` (a dict from each file's
path relative to `folder` to its count; put a sub-folder's name and "/" in front of the
paths of its delegate's `Counts`).

Besides `wfpy` and the standard library, the file may import two ready pieces from the
module `authoring`:

- `Listing(folder="<absolute path>")`: a node with one output port `Task`. It emits,
  once, the description of that folder: the same kind of token as your own task.
- `delegate()`: a sub-workflow, used like a node. Input port `Task`, output port
  `Answer`, a dict `{{"Total": int, "Counts": {{relative file path: count}}}}`. It hands
  the task to another author, who writes a workflow for it, and runs that workflow.
  Each call makes a new node.

```python
from authoring import Listing, delegate
...
listing_x, sub_x = Listing(folder="/abs/path/x"), delegate()
connect(listing_x.Task, sub_x.Task)
connect(sub_x.Answer, join.X)
```

Give the file's full content as the value of the `Workflow` output port. Do not run it,
and do not create or change any file: your reply is the only thing that is used."""


@task
class Listing:
    """Emits, once, what an author is told about a folder."""

    folder: str  # parameter

    class Ports:
        Task = Port[dict](direction="out")

    class Schedule:  # the latch of a node with no input: one firing, then spent
        initial = "armed"
        transitions = [("armed", "describe", "spent")]

    @action(consumes={}, produces={"Task": 1})
    def describe(self) -> dict:
        folder = Path(self.folder)
        listing = {
            "folder": str(folder),
            "files": sorted(p.name for p in folder.glob("*.txt") if p.is_file()),
            "folders": sorted(
                p.name for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")
            ),
        }
        return {"Task": listing}  # a returned dict is a port map: name the port


if TRANSPORT == "mock":
    # The mock transport replies one fixed text, and here every folder needs its
    # own workflow. So offline a script writes it: the same two ports, on a tool
    # node in place of an agent. The rest of the graph does not change.
    @tool(
        cmd=sys.executable,
        args=[str(HERE / "scripted_author.py"), "{in.Task}", "{out.Workflow}"],
    )
    class Author:
        class Ports:
            Task = Port[dict](direction="in")
            Workflow = Port[File](direction="out", ext=".py")

else:

    @agent(
        prompt=PROMPT,
        transport=TRANSPORT,
        connector=CONNECTOR,
        model=MODEL,
        timeout_ms=600_000,  # a file-sized reply exceeds the 120 s default
    )
    class Author:
        class Ports:
            Task = Port[dict](direction="in")
            Workflow = Port[File](direction="out", ext=".py")


@tool(
    cmd=sys.executable,
    args=["-m", "wfpy.cli", "run", "{in.Workflow}", "--out-dir", "{out.Run}", "--run-id", "child"],
    env={
        # The generated file is in the run's work folder, so this module
        # reaches it through PYTHONPATH and not as a sibling.
        "PYTHONPATH": os.pathsep.join(filter(None, [str(HERE), os.environ.get("PYTHONPATH")])),
        "WFPY_EXAMPLE_LEVEL": str(LEVEL + 1),
    },
)
class Execute:
    """Runs the workflow it is given, one level down."""

    class Ports:
        Workflow = Port[File](direction="in")
        Run = Port[File](direction="out")  # the folder of the child run


@task
class Read:
    """Turns the child's two outputs into one token of this graph."""

    class Ports:
        Run = Port[File](direction="in")
        Answer = Port[dict](direction="out")

    @action(consumes={"Run": 1}, produces={"Answer": 1})
    def read(self, run_dir) -> dict:
        record = json.loads((Path(str(run_dir)) / "child" / "run.wf-run.json").read_text())
        outputs = record["outputs"]  # every output is a list of tokens
        return {"Answer": {"Total": outputs["Total"][0], "Counts": outputs["Counts"][0]}}


@workflow(inputs={"Task": dict}, outputs={"Answer": dict})
def delegate() -> None:
    """Hand a task to an author and run what it writes, one level down."""
    if LEVEL >= MAX_LEVEL:
        raise RuntimeError(
            f"delegation stops at level {MAX_LEVEL}, and this run is at level {LEVEL}"
        )
    author, execute, read = Author(), Execute(), Read()
    connect("Task", author.Task)
    connect(author.Workflow, execute.Workflow)
    connect(execute.Run, read.Run)
    connect(read.Answer, "Answer")
