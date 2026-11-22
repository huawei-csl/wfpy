"""Advanced 03 — A workflow written for its data.

In 01 the author wrote the same workflow whatever it was given. Here it is told
about a folder, and asked for a graph with one counting node per file and one
node that joins them. So the number of nodes follows the number of files, and
nobody wrote that graph down before the run.

    Listing --Task--> Author --Workflow (.py)--> Execute --Run--> Read  --Answer-->
                                                             '--> Shape --Nodes-->

``Listing``, ``Author``, ``Execute`` and ``Read`` are in ``authoring.py``, beside
this file. ``Shape`` reads the same run folder a second time (an output port
feeds every consumer its own copy) and gives the nodes of the generated graph,
which is how the parent sees what was built: the child's run record lists them.
The parent's own trace shows the tool node firing once, and nothing inside.

Offline, the default, a script stands in for the model. With a real author::

    WFPY_EXAMPLE_TRANSPORT=acp wfpy run examples/advanced/03_data_shaped_workflow.py

which needs the ``agent`` extra and a connector (``wfpy connectors``). The names
of the nodes are then the model's own; the counts are the same.

Run it::

    wfpy run examples/advanced/03_data_shaped_workflow.py
    python examples/advanced/03_data_shaped_workflow.py

Expected output — four files, so four counting nodes::

    {'Answer': [{'Total': 41, 'Counts': {'actors.txt': 9, 'guards.txt': 12,
    'queues.txt': 10, 'tokens.txt': 10}}], 'Nodes': [['count_actors',
    'count_guards', 'count_queues', 'count_tokens', 'join']]}

Add a ``.txt`` file to ``data/notes`` and run it again: one more node.

Next: 04_recursive_delegation.py — the generated workflow delegates in turn.
"""

import json
from pathlib import Path

from authoring import Author, Execute, Listing, Read

from wfpy import File, Port, action, connect, run, task, workflow

NOTES = Path(__file__).resolve().parent / "data" / "notes"


@task
class Shape:
    """Reads back which nodes the generated workflow had."""

    class Ports:
        Run = Port[File](direction="in")
        Nodes = Port[list](direction="out")

    @action(consumes={"Run": 1}, produces={"Nodes": 1})
    def shape(self, run_dir) -> dict:
        record = json.loads((Path(str(run_dir)) / "child" / "run.wf-run.json").read_text())
        return {"Nodes": sorted(actor["instanceName"] for actor in record["actors"])}


@workflow(outputs={"Answer": dict, "Nodes": list})
def counted() -> None:
    listing, author, execute = Listing(folder=str(NOTES)), Author(), Execute()
    read, shape = Read(), Shape()
    connect(listing.Task, author.Task)
    connect(author.Workflow, execute.Workflow)
    connect(execute.Run, read.Run)
    connect(execute.Run, shape.Run)
    connect(read.Answer, "Answer")
    connect(shape.Nodes, "Nodes")


if __name__ == "__main__":
    print(run(counted))
