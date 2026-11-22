"""The offline stand-in for the author of 03 and 04.

A model is asked for a workflow that counts the words of one folder: a
counting node per file, a ``delegate()`` node per sub-folder, a joining node.
This script writes that workflow by rule, so the examples run with no model:

    python scripted_author.py "<the folder's listing>" <workflow.py>

It is here for CI and for a first run. What it writes is a correct answer to
the author's prompt, and nothing in it is cleverer than that prompt.
"""

import ast
import re
import sys
from pathlib import Path

COUNT = """

@task
class Count:
    path: str  # parameter

    class Ports:
        Out = Port[dict](direction="out")

    class Schedule:
        initial = "armed"
        transitions = [("armed", "count", "spent")]

    @action(consumes={}, produces={"Out": 1})
    def count(self) -> dict:
        with open(self.path, encoding="utf-8") as text:
            words = len(text.read().split())
        return {"Out": {os.path.basename(self.path): words}}
"""


def _identifier(name: str) -> str:
    return re.sub(r"\W", "_", name)


def workflow_source(listing: dict) -> str:
    """The text of a workflow for ``listing``: its shape follows the folder."""
    folder = Path(listing["folder"])
    files, subs = list(listing["files"]), list(listing["folders"])
    file_ports = [f"F{i}" for i in range(len(files))]
    sub_ports = [f"S{i}" for i in range(len(subs))]
    ports = file_ports + sub_ports

    out = ["import os", ""]
    if subs:
        out += ["from authoring import Listing, delegate", ""]
    out += ["from wfpy import Port, action, connect, task, workflow"]
    if files:
        out += [COUNT.rstrip("\n")]

    # The joining node: one input port per count, since a port takes one edge.
    out += ["", "", "@task", "class Join:", "    class Ports:"]
    out += [f'        {port} = Port[dict](direction="in")' for port in ports]
    out += ['        Total = Port[int](direction="out")']
    out += ['        Counts = Port[dict](direction="out")', ""]
    out += ["    class Schedule:", '        initial = "waiting"']
    out += ['        transitions = [("waiting", "join", "done")]', ""]
    consumes = ", ".join(f'"{port}": 1' for port in ports)
    arguments = ", ".join(["self", *(port.lower() for port in ports)])
    out += [f'    @action(consumes={{{consumes}}}, produces={{"Total": 1, "Counts": 1}})']
    out += [f"    def join({arguments}) -> dict:", "        counts = {}"]
    out += [f"        counts.update({port.lower()})" for port in file_ports]
    for port, sub in zip(sub_ports, subs):
        prefixed = f'{{"{sub}/" + path: n for path, n in {port.lower()}["Counts"].items()}}'
        out += [f"        counts.update({prefixed})"]
    out += ['        return {"Total": sum(counts.values()), "Counts": counts}']

    out += ["", "", '@workflow(outputs={"Total": int, "Counts": dict})', "def counting() -> None:"]
    out += ["    join = Join()"]
    for port, name in zip(file_ports, files):
        node = f"count_{_identifier(Path(name).stem)}"
        out += [f"    {node} = Count(path={str(folder / name)!r})"]
        out += [f"    connect({node}.Out, join.{port})"]
    for port, sub in zip(sub_ports, subs):
        node = _identifier(sub)
        out += [f"    listing_{node} = Listing(folder={str(folder / sub)!r})"]
        out += [f"    sub_{node} = delegate()"]
        out += [f"    connect(listing_{node}.Task, sub_{node}.Task)"]
        out += [f"    connect(sub_{node}.Answer, join.{port})"]
    out += ['    connect(join.Total, "Total")', '    connect(join.Counts, "Counts")']
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    # The listing is a dict token; the tool node wrote it on the command line as text.
    Path(sys.argv[2]).write_text(workflow_source(ast.literal_eval(sys.argv[1])), encoding="utf-8")
