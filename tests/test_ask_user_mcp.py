"""The ``ask_user`` tool an ACP agent gets over MCP: the server answers the
protocol's request side, and a call is the elicitation seam's question and
answer, declined included."""

from __future__ import annotations

import json
import urllib.request

from wfpy._ask_user_mcp import TOOL_NAME, AskUserServer, answer_text
from wfpy._elicitation_runtime import ElicitationResponse


def _post(url: str, message: dict) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, data=json.dumps(message).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req) as r:
        body = r.read()
        return r.status, (json.loads(body) if body else None)


def test_the_server_answers_initialize_list_and_call_and_acknowledges_notifications():
    asked = []

    def ask(question, context=None, choices=None):
        asked.append((question, context, choices))
        return ElicitationResponse(answer="the model")

    server = AskUserServer(ask)
    url = server.start()
    try:
        status, init = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        assert status == 200 and init["result"]["protocolVersion"] == "2025-06-18"
        assert init["result"]["capabilities"] == {"tools": {}} and init["result"]["serverInfo"]["name"] == "wfpy"
        status, _ = _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert status == 202
        _, listed = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        (tool,) = listed["result"]["tools"]
        assert tool["name"] == TOOL_NAME and tool["inputSchema"]["required"] == ["question"]
        _, called = _post(url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": TOOL_NAME, "arguments": {
                                    "question": "Which lever first?", "context": "two are left",
                                    "choices": ["the compiler's knobs", "the model"]}}})
        assert called["result"] == {"content": [{"type": "text", "text": "the model"}]}
        assert asked == [("Which lever first?", "two are left", ["the compiler's knobs", "the model"])]
        assert server.calls[0]["question"] == "Which lever first?"
        _, unknown = _post(url, {"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        assert unknown["error"]["code"] == -32601
        _, other = _post(url, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                               "params": {"name": "nope", "arguments": {}}})
        assert other["result"]["isError"] is True
    finally:
        server.stop()


def test_a_declined_or_empty_question_tells_the_agent_what_to_do():
    assert answer_text(ElicitationResponse(answer=None, declined=True, reason="no user")).startswith(
        "The user declined to answer (no user)")
    assert answer_text(ElicitationResponse(answer="42")) == "42"
    server = AskUserServer(lambda *a, **k: ElicitationResponse(answer="x"))
    assert server.call({}) == "The question is empty; ask one sentence."
    assert server.call({"question": "q", "choices": "not a list"}) == "x"
    assert server.calls[-1]["choices"] is None


def test_a_get_is_refused_and_the_server_stops():
    server = AskUserServer(lambda *a, **k: ElicitationResponse(answer="x"))
    url = server.start()
    req = urllib.request.Request(url, method="GET")
    try:
        urllib.request.urlopen(req)
        raise AssertionError("a GET was answered")
    except urllib.error.HTTPError as e:
        assert e.code == 405
    server.stop()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=b"{}", method="POST"), timeout=1)
        raise AssertionError("the server still answers")
    except (urllib.error.URLError, ConnectionError, OSError):
        pass
