"""Advanced 05: when the generated workflow is wrong, check it and send it back.

In 01 to 04 a tool node ran the generated workflow. A ``@tool`` raises when its
command exits non-zero, and that ends the whole run: a syntax error, a file
with no ``@workflow`` in it, or a node that raises in the generated workflow
all stop the workflow that asked for it. That is right for a command that
must not fail. It is wrong for the work of an author, where a first attempt
that fails is the normal case.

So here a ``@task`` runs the candidate as a subprocess and returns what
happened as a token. The graph, not the runtime, then decides what a failure
means:

    Author --Workflow--> Check --Result--> Decide --Done--> Answer
                           ^                  |
                           |               Failure
                           '--- Workflow -- Reviser

* **Check** runs the file and emits an ``Outcome``. A run that exits 0 without
  a token on the output that was asked for is a failure too: exit 0 is not
  success. So is a run that does not end in time. A ``@tool`` node has no
  time limit, and a generated workflow may never finish.
* **Decide** has three actions on one port, ordered by a ``Priority``: accept a
  good outcome, ask again while the budget lasts, give up when it is spent.
  The budget is what makes the loop end.
* **Reviser** is a second agent, and not a second input of ``Author``: an agent
  fires when *every* input port holds a token, so a retry edge into the author
  would never fire it. The reviser gets the file and the error in one token.

Offline, the default, the author's mock reply has a bug in it (a name that does
not exist) and the reviser's mock reply is the corrected file, so the loop goes
round once. With real agents::

    WFPY_EXAMPLE_TRANSPORT=acp wfpy run examples/advanced/05_repair_generated_workflow.py

the first candidate is usually right and ``revisions`` is 0.

Run it::

    wfpy run examples/advanced/05_repair_generated_workflow.py
    python examples/advanced/05_repair_generated_workflow.py

Expected output (one failed candidate, one revision, then the answer)::

    {'Answer': [{'ok': True, 'answer': 42, 'revisions': 1}]}

This is the last advanced example. See README.md in this folder for what the
five have in common, and what to watch for before using the pattern.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wfpy import File, Port, action, agent, connect, guard, run, task, workflow

HERE = Path(__file__).resolve().parent

TRANSPORT = os.environ.get("WFPY_EXAMPLE_TRANSPORT", "mock")
CONNECTOR = os.environ.get("WFPY_EXAMPLE_CONNECTOR", "claude")
MODEL = os.environ.get("WFPY_EXAMPLE_MODEL", "sonnet")

PRIMER = f"""You write wfpy workflows. This is all of wfpy you need:

{(HERE / "primer.md").read_text(encoding="utf-8")}
"""

REPLY_ONLY = (
    "Do not run it, and do not create or change any file: your reply is the only thing "
    "that is used."
)

FIXED = """\
from wfpy import Port, action, connect, task, workflow


@task
class Product:
    class Ports:
        Out = Port[int](direction="out")

    class Schedule:
        initial = "armed"
        transitions = [("armed", "emit", "spent")]

    @action(consumes={}, produces={"Out": 1})
    def emit(self) -> int:
        return 6 * 7


@workflow(outputs={"Answer": int})
def product() -> None:
    node = Product()
    connect(node.Out, "Answer")
"""

# The mock author's reply: the file above with a bug a first draft can have.
BROKEN = FIXED.replace("return 6 * 7", "return six * 7")


@dataclass(frozen=True)
class Outcome:
    """What running one candidate gave. A token of this graph."""

    ok: bool
    source: str  # the candidate's path
    answer: Any = None
    error: str = ""


@agent(
    prompt=PRIMER
    + "Write ONE Python file holding a wfpy workflow with one output, `Answer` (an int): "
    "the product of 6 and 7. Give the file's full content as the value of the `Workflow` "
    "output port. " + REPLY_ONLY,
    transport=TRANSPORT,
    connector=CONNECTOR,
    model=MODEL,
    timeout_ms=600_000,
    mock_outputs={"Workflow": BROKEN},
)
class Author:
    """No input port: one first candidate in a run."""

    class Ports:
        Workflow = Port[File](direction="out", ext=".py")


@agent(
    prompt=PRIMER
    + "`inputs.Failure` holds a wfpy workflow file and what running it gave. Correct the "
    "file. Give the corrected file's full content as the value of the `Workflow` output "
    "port. " + REPLY_ONLY,
    transport=TRANSPORT,
    connector=CONNECTOR,
    model=MODEL,
    timeout_ms=600_000,
    mock_outputs={"Workflow": FIXED},
)
class Reviser:
    class Ports:
        Failure = Port[str](direction="in")
        Workflow = Port[File](direction="out", ext=".py")


@task
class Check:
    """Runs a candidate and says what happened. It does not raise on a bad one."""

    timeout_s: float  # parameter: how long a candidate may run

    class Ports:
        Initial = Port[File](direction="in")  # from the author
        Retry = Port[File](direction="in")  # from the reviser, the feedback edge
        Result = Port[Outcome](direction="out")

    # Two input ports, since a port takes one edge, and so two actions. The
    # loop works without this Schedule; it says, as data, in which order they fire.
    class Schedule:
        initial = "fresh"
        transitions = [
            ("fresh", "first", "looping"),  # the author's candidate, once
            ("looping", "again", "looping"),  # then only what comes back
        ]

    @action(consumes={"Initial": 1}, produces={"Result": 1})
    def first(self, workflow_file) -> Outcome:
        return self._run(workflow_file)

    @action(consumes={"Retry": 1}, produces={"Result": 1})
    def again(self, workflow_file) -> Outcome:
        return self._run(workflow_file)

    def _run(self, workflow_file) -> Outcome:
        source = Path(str(workflow_file))
        run_dir = source.with_suffix("")  # beside the candidate, in the run's folder
        try:
            child = subprocess.run(
                [sys.executable, "-m", "wfpy.cli", "run", str(source)]
                + ["--out-dir", str(run_dir), "--run-id", "child"],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            error = f"the run did not end within {self.timeout_s:g} seconds"
            return Outcome(False, str(source), error=error)
        if child.returncode != 0:
            return Outcome(False, str(source), error=child.stderr.strip()[-2000:])
        record = json.loads((run_dir / "child" / "run.wf-run.json").read_text())
        tokens = record["outputs"].get("Answer") or []
        if not tokens:
            return Outcome(False, str(source), error="the run gave no token on `Answer`")
        return Outcome(True, str(source), answer=tokens[0])


@task
class Decide:
    """Accept, ask again, or give up: a case statement over one port."""

    budget: int  # parameter: how many revisions a run may spend
    _revisions: int = 0  # state

    class Ports:
        Result = Port[Outcome](direction="in")
        Failure = Port[str](direction="out")
        Done = Port[dict](direction="out")

    class Schedule:
        initial = "deciding"
        transitions = [
            ("deciding", "accept", "done"),
            ("deciding", "ask_again", "deciding"),
            ("deciding", "give_up", "done"),
        ]

    class Priority:
        rules = [["accept", "ask_again", "give_up"]]

    @action(consumes={"Result": 1}, produces={"Done": 1})
    @guard(lambda self, outcome: outcome.ok)
    def accept(self, outcome: Outcome) -> dict:
        return {"Done": {"ok": True, "answer": outcome.answer, "revisions": self._revisions}}

    @action(consumes={"Result": 1}, produces={"Failure": 1})
    @guard(lambda self, outcome: self._revisions < self.budget)
    def ask_again(self, outcome: Outcome) -> str:
        self._revisions += 1
        return (
            f"The workflow file:\n\n{Path(outcome.source).read_text()}\n"
            f"What running it gave:\n\n{outcome.error}\n"
        )

    @action(consumes={"Result": 1}, produces={"Done": 1})
    def give_up(self, outcome: Outcome) -> dict:  # no guard: what is left
        return {"Done": {"ok": False, "error": outcome.error, "revisions": self._revisions}}


@workflow(outputs={"Answer": dict})
def repaired() -> None:
    author, check, reviser = Author(), Check(timeout_s=60), Reviser()
    decide = Decide(budget=2)
    connect(author.Workflow, check.Initial)
    connect(check.Result, decide.Result)
    connect(decide.Failure, reviser.Failure)
    connect(reviser.Workflow, check.Retry)  # the feedback edge
    connect(decide.Done, "Answer")


if __name__ == "__main__":
    print(run(repaired))
