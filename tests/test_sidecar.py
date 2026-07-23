from __future__ import annotations

from pathlib import Path

import pytest

from wfpy.rewrite import RewriteEngine, RewriteError, load_module
from wfpy.sidecar import _handle_request


def test_sidecar_update_node_parameter_rewrites_source(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(
        '''
from wfpy import workflow


@workflow
def demo():
    worker = Worker(scale=3)
'''.lstrip(),
        encoding="utf-8",
    )

    resp = _handle_request(
        {
            "file": str(src),
            "op": "wfpy.updateNodeParameter",
            "args": {
                "workflow": "demo",
                "entity": "worker",
                "parameterName": "scale",
                "newValue": "7",
            },
        }
    )

    assert resp.status == "ok"
    assert "worker = Worker(scale=7)" in src.read_text(encoding="utf-8")


def test_sidecar_update_definition_parameter_rewrites_source(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(
        '''
class Worker:
    pass
'''.lstrip(),
        encoding="utf-8",
    )

    resp = _handle_request(
        {
            "file": str(src),
            "op": "wfpy.updateDefinitionParameter",
            "args": {
                "entityType": "Worker",
                "parameterName": "scale",
                "parameterText": "scale: int = 7",
            },
        }
    )

    updated = src.read_text(encoding="utf-8")
    assert resp.status == "ok"
    assert "def __init__(self, scale: int = 7):" in updated


def test_sidecar_update_factory_parameter_rewrites_source(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(
        '''
from wfpy import workflow


def make_demo(scale: int = 3):
    @workflow
    def demo():
        _ = scale
        return None

    return demo
'''.lstrip(),
        encoding="utf-8",
    )

    resp = _handle_request(
        {
            "file": str(src),
            "op": "wfpy.updateDefinitionParameter",
            "args": {
                "entityType": "make_demo",
                "parameterName": "scale",
                "parameterText": "scale: int = 7",
            },
        }
    )

    updated = src.read_text(encoding="utf-8")
    assert resp.status == "ok"
    assert "def make_demo(scale: int = 7):" in updated


def test_sidecar_rejects_oversized_definition_parameter_text(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(
        '''
from wfpy import workflow


def make_demo(scale: int = 3):
    @workflow
    def demo():
        _ = scale
        return None

    return demo
'''.lstrip(),
        encoding="utf-8",
    )

    resp = _handle_request(
        {
            "file": str(src),
            "op": "wfpy.updateDefinitionParameter",
            "args": {
                "entityType": "make_demo",
                "parameterName": "scale",
                "parameterText": f"scale: int = {'x' * 5000}",
            },
        }
    )

    assert resp.status == "error"
    assert (resp.diagnostic or {}).get("code") == "payload_too_large"


def test_rewrite_engine_rejects_stale_save(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(
        '''
class Worker:
    pass
'''.lstrip(),
        encoding="utf-8",
    )

    module, source_text, _revision = load_module(str(src))
    engine = RewriteEngine(str(src), module, source_text)
    engine.update_definition_parameter(
        entityType="Worker",
        parameterName="scale",
        parameterText="scale: int = 7",
    )

    src.write_text(
        '''
class Worker:
    other = 1
'''.lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(RewriteError, match="source changed during rewrite"):
        engine.save()

# ── contract v2: capabilities, stable graph ids, checkConnection, connect-by-id ──


_WF_WITH_EDGE = '''
from wfpy import workflow


@workflow
def demo():
    producer = Producer()
    consumer = Consumer()
    connect(producer.Out, consumer.In)
'''.lstrip()


def test_sidecar_get_capabilities_reports_v2_contract() -> None:
    resp = _handle_request({"op": "wfpy.getCapabilities"})
    assert resp.status == "ok"
    diag = resp.diagnostic or {}
    assert diag.get("protocolVersion") == 2
    # supportedOps is generated + canonical (exportGraph, not exportWorkflowGraph).
    assert "exportGraph" in diag["supportedOps"]
    assert "exportWorkflowGraph" not in diag["supportedOps"]
    assert "getCapabilities" in diag["supportedOps"]
    assert "checkConnection" in diag["supportedOps"]
    assert diag["features"]["stableIds"] is True
    assert diag["features"]["checkConnection"] is True


def test_sidecar_protocol_contracts_alias_matches_capabilities() -> None:
    a = _handle_request({"op": "wfpy.getCapabilities"}).diagnostic
    b = _handle_request({"op": "wfpy.protocolContracts"}).diagnostic
    assert a == b


def test_sidecar_export_graph_has_additive_stable_ids(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(_WF_WITH_EDGE, encoding="utf-8")

    # The canonical `exportGraph` alias resolves to the graph export.
    resp = _handle_request({"file": str(src), "op": "wfpy.exportGraph", "args": {}})
    assert resp.status == "ok"
    graph = (resp.diagnostic or {})["graph"]

    node = next(n for n in graph["nodes"] if n["id"] == "producer")
    # Existing field kept (diagram-safe) + canonical id added.
    assert node["id"] == "producer"
    assert node["nodeId"] == "n:producer"

    edge = graph["edges"][0]
    assert edge["id"] == "e1"  # legacy id preserved
    assert edge["edgeId"] == "e:n:producer:p:Out->n:consumer:p:In"
    assert edge["from"]["portId"] == "n:producer:p:Out"
    assert edge["to"]["portId"] == "n:consumer:p:In"

    # Source locations (contract v2): nodes/edges carry {file, line}.
    assert node["location"]["file"] == str(src)
    assert node["location"]["line"] == 6  # `producer = Producer()`
    assert edge["location"]["line"] == 8  # `connect(producer.Out, consumer.In)`


def test_sidecar_check_connection_validates_existence(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(_WF_WITH_EDGE, encoding="utf-8")

    ok = _handle_request(
        {
            "file": str(src),
            "op": "wfpy.checkConnection",
            "args": {"workflow": "demo", "sourceId": "n:producer:p:Out", "targetId": "n:consumer:p:In"},
        }
    )
    assert ok.status == "ok"
    assert (ok.diagnostic or {}).get("code") == "connection_ok"
    assert (ok.diagnostic or {}).get("preflightToken")

    bad = _handle_request(
        {
            "file": str(src),
            "op": "wfpy.checkConnection",
            "args": {"workflow": "demo", "sourceId": "n:missing:p:Out", "targetId": "n:consumer:p:In"},
        }
    )
    assert bad.status == "error"
    assert (bad.diagnostic or {}).get("code") == "parse_error"


def test_sidecar_connect_accepts_canonical_ids(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(
        '''
from wfpy import workflow


@workflow
def demo():
    producer = Producer()
    consumer = Consumer()
'''.lstrip(),
        encoding="utf-8",
    )

    resp = _handle_request(
        {
            "file": str(src),
            "op": "wfpy.connect",
            "args": {"workflow": "demo", "sourceId": "n:producer:p:Out", "targetId": "n:consumer:p:In"},
        }
    )
    assert resp.status == "ok"
    assert "connect(producer.Out, consumer.In)" in src.read_text(encoding="utf-8")


def test_sidecar_error_has_code_and_revision(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(_WF_WITH_EDGE, encoding="utf-8")

    resp = _handle_request(
        {"file": str(src), "op": "wfpy.deleteNode", "args": {"workflow": "demo", "name": "ghost"}}
    )
    assert resp.status == "error"
    assert (resp.diagnostic or {}).get("code")  # always present now
    assert resp.revision  # on-disk revision reported, not empty


_WF_TYPED = '''
from wfpy import workflow, task


@task
class Producer:
    class Ports:
        Out = Port[int](direction="out")


@task
class Consumer:
    class Ports:
        In = Port[int](direction="in")


@workflow
def demo():
    p = Producer()
    c = Consumer()
'''.lstrip()


def test_sidecar_check_connection_validates_direction_and_type(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(_WF_TYPED, encoding="utf-8")

    def check(source_id: str, target_id: str):
        return _handle_request(
            {
                "file": str(src),
                "op": "wfpy.checkConnection",
                "args": {"workflow": "demo", "sourceId": source_id, "targetId": target_id},
            }
        )

    ok = check("n:p:p:Out", "n:c:p:In")
    assert ok.status == "ok"
    assert (ok.diagnostic or {}).get("semanticValidation") == "type"  # port metadata resolved

    # Wrong direction: In is an input, can't be a source.
    bad_dir = check("n:c:p:In", "n:p:p:Out")
    assert bad_dir.status == "error"
    assert (bad_dir.diagnostic or {}).get("code") == "invalid_source_direction"

    # Type mismatch.
    src.write_text(_WF_TYPED.replace('In = Port[int]', 'In = Port[str]'), encoding="utf-8")
    mismatch = check("n:p:p:Out", "n:c:p:In")
    assert mismatch.status == "error"
    assert (mismatch.diagnostic or {}).get("code") == "type_mismatch"


def test_sidecar_expected_revision_optimistic_concurrency(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(
        '''
from wfpy import workflow


@workflow
def demo():
    worker = Worker(scale=1)
'''.lstrip(),
        encoding="utf-8",
    )

    rev = _handle_request({"file": str(src), "op": "wfpy.exportGraph", "args": {}}).revision
    assert rev

    # Matching revision → the mutation proceeds.
    ok = _handle_request(
        {
            "file": str(src),
            "op": "wfpy.updateNodeParameter",
            "args": {
                "workflow": "demo", "entity": "worker", "parameterName": "scale",
                "newValue": "5", "expectedRevision": rev,
            },
        }
    )
    assert ok.status == "ok"
    assert "worker = Worker(scale=5)" in src.read_text(encoding="utf-8")

    # Stale revision → rejected (the file changed since the client last read it).
    stale = _handle_request(
        {
            "file": str(src),
            "op": "wfpy.updateNodeParameter",
            "args": {
                "workflow": "demo", "entity": "worker", "parameterName": "scale",
                "newValue": "9", "expectedRevision": "sha256:deadbeef",
            },
        }
    )
    assert stale.status == "error"
    assert (stale.diagnostic or {}).get("code") == "concurrent_source_modification"
    assert "worker = Worker(scale=5)" in src.read_text(encoding="utf-8")  # unchanged


_WF_SCOPED = '''
from wfpy import workflow, task


@task
class P:
    class Ports:
        Out = Port[int](direction="out")
        In = Port[int](direction="in")


@workflow
def demo():
    ctrl = wfpy.if_(flag)
    with ctrl.then:
        a = P()
    with ctrl.else_:
        b = P()
    top = P()
'''.lstrip()


def test_sidecar_check_connection_rejects_cross_scope(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(_WF_SCOPED, encoding="utf-8")

    def check(s: str, t: str):
        return _handle_request(
            {"file": str(src), "op": "wfpy.checkConnection",
             "args": {"workflow": "demo", "sourceId": s, "targetId": t}}
        )

    # `then` vs `else` branch → cross-scope, rejected.
    bad = check("n:a:p:Out", "n:b:p:In")
    assert bad.status == "error"
    assert (bad.diagnostic or {}).get("code") == "cross_scope_disallowed"

    # root → nested (ancestor relationship) is allowed.
    assert check("n:top:p:Out", "n:a:p:In").status == "ok"
    # same scope is allowed.
    assert check("n:a:p:Out", "n:a:p:In").status == "ok"


def test_sidecar_graph_emits_control_flow_scopes(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(_WF_SCOPED, encoding="utf-8")
    graph = (_handle_request({"file": str(src), "op": "wfpy.exportGraph", "args": {}}).diagnostic or {})["graph"]
    by_id = {n["id"]: n.get("scope") for n in graph["nodes"]}
    assert by_id["a"] == "scope:ctrl:then"
    assert by_id["b"] == "scope:ctrl:else"
    assert by_id["top"] == "scope:root"


_WF_BAD_EDGE = '''
from wfpy import workflow, task


@task
class Producer:
    class Ports:
        Out = Port[int](direction="out")


@task
class Consumer:
    class Ports:
        In = Port[str](direction="in")


@workflow
def demo():
    producer = Producer()
    consumer = Consumer()
    connect(producer.Out, consumer.In)
'''.lstrip()


def test_sidecar_export_graph_flags_bad_edge(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(_WF_BAD_EDGE, encoding="utf-8")

    graph = (_handle_request({"file": str(src), "op": "wfpy.exportGraph", "args": {}}).diagnostic or {})["graph"]

    # Partial flag set because a diagnostic exists.
    assert graph.get("partial") is True
    # The type-mismatched edge is still emitted (renders, marked) and carries diagnostics.
    edge = graph["edges"][0]
    meta = edge["meta"]
    assert meta["isErrored"] is True
    assert meta["errorMessage"]
    assert meta["diagnostics"][0]["code"] == "type_mismatch"
    assert meta["diagnostics"][0]["severity"] == "error"
    # The edge itself is not dropped.
    assert edge["from"]["nodeId"] == "producer"
    assert edge["to"]["nodeId"] == "consumer"


def test_sidecar_export_graph_does_not_flag_valid_edge(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(_WF_BAD_EDGE.replace('In = Port[str]', 'In = Port[int]'), encoding="utf-8")

    graph = (_handle_request({"file": str(src), "op": "wfpy.exportGraph", "args": {}}).diagnostic or {})["graph"]

    # A clean graph adds nothing: no partial flag, no errors, no edge diagnostics.
    assert "partial" not in graph
    assert "errors" not in graph
    for edge in graph["edges"]:
        assert "meta" not in edge or "diagnostics" not in edge.get("meta", {})


def test_sidecar_export_graph_clean_file_is_additive_only(tmp_path: Path) -> None:
    """Regression guard: a clean file's exported graph must be byte-identical to the bare
    engine export — no partial/errors/meta.diagnostics keys added."""
    src = tmp_path / "wf.py"
    src.write_text(_WF_TYPED + "    connect(p.Out, c.In)\n", encoding="utf-8")

    module, source_text, _rev = load_module(str(src))
    bare = RewriteEngine(str(src), module, source_text).export_workflow_graph(workflow=None)

    graph = (_handle_request({"file": str(src), "op": "wfpy.exportGraph", "args": {}}).diagnostic or {})["graph"]

    import json as _json

    assert _json.dumps(graph, sort_keys=True) == _json.dumps(bare, sort_keys=True)
    assert "partial" not in graph
    assert "errors" not in graph
    for node in graph["nodes"]:
        assert "diagnostics" not in node.get("meta", {})
    for edge in graph["edges"]:
        assert "diagnostics" not in edge.get("meta", {})


def test_sidecar_export_graph_syntax_error_is_partial_not_catastrophic(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text("def f(:\n    x = 1\n", encoding="utf-8")

    resp = _handle_request({"file": str(src), "op": "wfpy.exportGraph", "args": {}})

    # A syntax error degrades to a partial graph, NOT a catastrophic status: error.
    assert resp.status == "ok"
    graph = (resp.diagnostic or {})["graph"]
    assert graph["partial"] is True
    assert graph["nodes"] == []
    assert graph["edges"] == []
    err = graph["errors"][0]
    assert err["code"] == "syntax_error"
    assert err["severity"] == "error"
    assert err["location"]["file"] == str(src)
    assert err["location"]["line"] == 2


def test_sidecar_export_graph_reports_partial_graph_feature() -> None:
    diag = _handle_request({"op": "wfpy.getCapabilities"}).diagnostic or {}
    assert diag["features"]["partialGraph"] is True


def test_sidecar_connect_enforces_preflight(tmp_path: Path) -> None:
    src = tmp_path / "wf.py"
    src.write_text(_WF_SCOPED, encoding="utf-8")

    # The actual connect mutation (not just checkConnection) rejects a cross-scope edge
    # and writes nothing.
    resp = _handle_request(
        {"file": str(src), "op": "wfpy.connect",
         "args": {"workflow": "demo", "sourceId": "n:a:p:Out", "targetId": "n:b:p:In"}}
    )
    assert resp.status == "error"
    assert (resp.diagnostic or {}).get("code") == "cross_scope_disallowed"
    assert "connect(" not in src.read_text(encoding="utf-8")


# wfpy plan --best-effort uses build_partial_graph, which (on a failed connect to
# a non-existent port) keeps the node flagged AND now renders the dropped edge as a
# broken edge to a synthesized phantom port.
_WF_PARTIAL_BAD_PORT = '''
from wfpy import workflow, task, connect, Port


@task
class Producer:
    class Ports:
        Out = Port[int](direction="out")


@task
class Consumer:
    class Ports:
        In = Port[int](direction="in")


@workflow
def demo():
    producer = Producer()
    consumer = Consumer()
    connect(producer.Out, consumer.Nope)
'''.lstrip()


def test_partial_graph_renders_broken_edge_and_phantom_port(tmp_path: Path) -> None:
    from wfpy.partial import build_partial_graph

    src = tmp_path / "wf.py"
    src.write_text(_WF_PARTIAL_BAD_PORT, encoding="utf-8")

    data = build_partial_graph(str(src), "demo", RuntimeError("elaboration failed"))
    g = data["graph"]
    assert data["partial"] is True

    nodes = {n["label"]: n for n in g["nodes"]}
    node_by_id = {n["id"]: n for n in g["nodes"]}
    consumer = nodes["consumer"]

    # (a) The consumer node grew a phantom port named "Nope" (the bad target port).
    phantom = [p for p in consumer["ports"] if p["name"] == "Nope"]
    assert len(phantom) == 1
    assert phantom[0]["direction"] == "in"
    assert phantom[0]["id"] == f"port:{consumer['id']}:Nope"
    assert phantom[0]["isErrored"] is True

    # (b) A broken edge lands on that phantom port, marked errored with the message.
    broken = [e for e in g["edges"] if e.get("meta", {}).get("isErrored")]
    assert len(broken) == 1
    edge = broken[0]
    assert node_by_id[edge["toNode"]]["label"] == "consumer"
    assert edge["to"] == f"port:{consumer['id']}:Nope"
    assert edge["from"] == f"port:{nodes['producer']['id']}:Out"
    assert edge["meta"]["errorMessage"]
    assert edge["meta"]["diagnostics"][0]["code"] == "unknown_port"
    assert edge["meta"]["diagnostics"][0]["severity"] == "error"

    # (c) The consumer node still carries its node-level error flag.
    assert consumer["meta"]["isErrored"] is True
