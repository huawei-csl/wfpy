from __future__ import annotations

from wfpy.core import task, workflow
from wfpy.runner import _build_workflow_graph


def test_instance_name_preserved_without_connect() -> None:
    @task
    class FileSource:
        class Ports:
            pass

    @workflow
    def demo():
        traceX = FileSource()
        _ = traceX

    wf_def = demo._wfpy_workflow
    graph = _build_workflow_graph(wf_def)
    assert "traceX" in graph.actors
    assert "filesource" not in graph.actors
