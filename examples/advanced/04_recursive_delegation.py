"""Advanced 04 — Recursive delegation: a generated workflow delegates in turn.

03 wired an author, a tool node and a reader by hand. ``authoring.delegate`` is
those three as one sub-workflow, a task in and an answer out, and the author is
told it may use it. For a folder with sub-folders it writes a workflow that
holds one ``delegate()`` node per sub-folder, each with an author of its own:

    Listing(tree) --Task--> delegate --Answer-->
                               |
                               '-- runs:  count_readme ----------------.
                                          Listing(model) --> delegate --+--> join
                                          Listing(tools) --> delegate --'
                                                                |
                                                                '-- runs: ...

So the depth of the chain of runs follows the depth of the folder tree. With
the data here that is three generated workflows deep, four authors in all, and
this file names none of them.

What a recursion like this needs, and wfpy does not give by itself:

* **A bound.** ``authoring.py`` counts the levels through the tool node's
  environment and refuses to import past ``WFPY_EXAMPLE_MAX_LEVEL`` (5). Run
  this with the limit at 1 and it ends with ``delegation is 2 levels deep; the
  limit is 1``.
* **A path for imports.** A generated file lives in the run's work folder, so
  ``authoring`` reaches it through ``PYTHONPATH``, set on the tool node.

Each level is a ``wfpy run`` of its own, with its own run folder under the work
folder of the level above. The parent's trace shows one tool firing a level.

Offline, the default, a script stands in for the model. With a real author at
every level (four model turns, one to two minutes with ``sonnet``)::

    WFPY_EXAMPLE_TRANSPORT=acp wfpy run examples/advanced/04_recursive_delegation.py

Run it::

    wfpy run examples/advanced/04_recursive_delegation.py
    python examples/advanced/04_recursive_delegation.py

Expected output — every file of the tree, counted three levels down::

    {'Answer': [{'Total': 44, 'Counts': {'readme.txt': 6, 'model/actors.txt': 8,
    'model/queues.txt': 7, 'model/rules/guards.txt': 7,
    'model/rules/priorities.txt': 9, 'tools/cli.txt': 7}}]}

Next: 05_repair_generated_workflow.py — when the generated workflow is wrong.
"""

from pathlib import Path

from authoring import Listing, delegate

from wfpy import connect, run, workflow

TREE = Path(__file__).resolve().parent / "data" / "tree"


@workflow(outputs={"Answer": dict})
def counted_tree() -> None:
    listing, root = Listing(folder=str(TREE)), delegate()
    connect(listing.Task, root.Task)
    connect(root.Answer, "Answer")


if __name__ == "__main__":
    print(run(counted_tree))
