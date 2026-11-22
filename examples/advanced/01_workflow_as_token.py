"""Advanced 01: a workflow as a token. An agent writes one, a tool node runs it.

A workflow file is text, and text is a token. So an ``@agent`` can emit a
workflow on a ``File`` port, and a ``@tool`` node can run it with ``wfpy run``:
the graph that runs is not written before the run starts.

    X ------------------------------.
    Author --Workflow (.py)--> Execute --Run--> Read --Answer-->

Three nodes carry the whole pattern:

* **Author** is an agent with a ``Port[File](direction="out", ext=".py")``.
  wfpy writes its reply to a file in the run's work folder, and the token is
  that file's path.
* **Execute** is a tool node whose command is ``wfpy run`` on that path. Its
  input ``X`` goes to the generated workflow through ``--input``, so a token of
  this graph becomes a token of the generated one. It travels as text
  (``{in.X}`` is ``str(token)``, which ``--input`` reads again): right for a
  number or a word, not for a dict. Its output ``Run`` is a path the runtime
  chooses, given to ``--out-dir``: the folder of the child run.
* **Read** opens the child's run record and emits what the child gave. The
  result does not come back by itself: the tool's token is a folder, and a
  node has to read it.

The command is ``sys.executable -m wfpy.cli``, so the child runs on the Python
that runs the parent, with nothing needed on ``PATH``. A tool node's working
folder is the folder of its workflow file, so the child finds wfpy when it is
installed, or on a ``PYTHONPATH`` given in full, and not through a relative one.

``transport="mock"`` keeps it offline: the author replies with one fixed file,
a doubling workflow like the tutorial's first example. A real author writes
one for its task (03).

Run it::

    wfpy run examples/advanced/01_workflow_as_token.py --input X=21
    python examples/advanced/01_workflow_as_token.py

Expected output::

    {'Answer': [42]}

Next: 02_recursive_workflow.py, a generated workflow that generates the next.
"""

import json
import sys
from pathlib import Path

from wfpy import File, Port, action, agent, connect, run, task, tool, workflow

# What the mock author replies: a complete workflow file.
GENERATED = """\
from wfpy import Port, action, connect, task, workflow


@task
class Double:
    class Ports:
        In = Port[int](direction="in")
        Out = Port[int](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def double(self, x: int) -> int:
        return x * 2


@workflow(inputs={"In": int}, outputs={"Answer": int})
def doubling() -> None:
    double = Double()
    connect("In", double.In)
    connect(double.Out, "Answer")
"""


@agent(
    prompt="Write a wfpy workflow that doubles its input `In` and gives it as `Answer`.",
    transport="mock",
    mock_outputs={"Workflow": GENERATED},
)
class Author:
    """No input port, so it fires once in a run. Its token is a workflow file."""

    class Ports:
        Workflow = Port[File](direction="out", ext=".py")


@tool(
    cmd=sys.executable,
    args=[
        "-m", "wfpy.cli", "run", "{in.Workflow}",
        "--input", "In={in.X}",
        "--out-dir", "{out.Run}",
        "--run-id", "child",
    ],
)  # fmt: skip
class Execute:
    """Runs the workflow it is given. Fires when both of its inputs hold a token."""

    class Ports:
        Workflow = Port[File](direction="in")
        X = Port[int](direction="in")
        Run = Port[File](direction="out")  # the folder of the child run


@task
class Read:
    """Turns the child's output into a token of this graph."""

    class Ports:
        Run = Port[File](direction="in")
        Answer = Port[int](direction="out")

    @action(consumes={"Run": 1}, produces={"Answer": 1})
    def read(self, run_dir) -> dict:
        record = json.loads((Path(str(run_dir)) / "child" / "run.wf-run.json").read_text())
        answer = record["outputs"]["Answer"][0]  # every output is a list of tokens
        # Named by its port: a returned dict is read as {port: value}, so a child
        # whose answer is a dict would otherwise emit nothing here, with no error.
        return {"Answer": answer}


@workflow(inputs={"X": int}, outputs={"Answer": int})
def generated() -> None:
    author, execute, read = Author(), Execute(), Read()
    connect(author.Workflow, execute.Workflow)
    connect("X", execute.X)
    connect(execute.Run, read.Run)
    connect(read.Answer, "Answer")


if __name__ == "__main__":
    print(run(generated, inputs={"X": 21}))
