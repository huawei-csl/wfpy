"""Workflow boundary ports in the diagram graph: declared type and source."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from wfpy import File, Port, Resource, action, connect, task, workflow
from wfpy.graph import export_graph_json
from wfpy.runner import _build_workflow_graph


@task
class Relay:
    class Ports:
        In = Port[File](direction="in")
        Out = Port[File](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def go(self, path: str) -> str:
        return path


def _boundary_ports(wf: Any) -> dict[str, dict[str, Any]]:
    nodes = export_graph_json(_build_workflow_graph(wf._wfpy_workflow))["graph"]["nodes"]
    return {
        node["label"]: node["ports"][0]
        for node in nodes
        if node["kind"] in ("wf-input", "wf-output")
    }


def test_boundary_ports_carry_declared_type_and_source() -> None:
    @workflow(
        inputs={"In": File(ext=".mlir")},
        outputs={"Out": File(ext=".mlir"), "Rtl": Resource(kind="folder")},
    )
    def declared():
        relay = Relay()
        connect("In", relay.In)
        connect(relay.Out, "Out")

    ports = _boundary_ports(declared)

    # every declared port -- the unconnected `Rtl` too -- typed as task ports are
    assert {name: port["type"] for name, port in ports.items()} == {
        "In": "File(ext='.mlir')",
        "Out": "File(ext='.mlir')",
        "Rtl": "Resource(ext='', kind='folder')",
    }
    # and located at its key in the decorator
    lines, start = inspect.getsourcelines(declared._wfpy_workflow.builder_fn)
    for name, port in ports.items():
        assert Path(port["source"]["file"]).resolve() == Path(__file__).resolve()
        assert f'"{name}":' in lines[port["source"]["line"] - start]


def test_undeclared_boundary_port_stays_untyped() -> None:
    @workflow
    def undeclared():
        relay = Relay()
        connect("In", relay.In)
        connect(relay.Out, "Out")

    ports = _boundary_ports(undeclared)

    assert {name: port["type"] for name, port in ports.items()} == {"In": "any", "Out": "any"}
    assert all("source" not in port for port in ports.values())
