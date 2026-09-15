"""The ``ask_user`` tool for an agent on the ACP transport, served over MCP.

An ACP agent (Claude Code through Zed's adapter, opencode, ...) has no way to
put a question to the user: what it can ask is permission for a tool call,
and that is what the human port carried until now. This module serves the
builtin ``ask_user`` tool to the agent's session as an MCP server on the
loopback interface -- the ACP ``session/new`` and ``session/load`` requests
take a list of MCP servers, and an HTTP one is a URL -- so the agent can ask
"which lever first?" with choices, and the answer comes back as the tool's
result through the same elicitation seam the HTTP transport's ``ask_user``
uses (`_elicitation_runtime`): the terminal, the run's ``--elicit-socket``, an
IDE's chat.

The server speaks the streamable HTTP transport's request side only: one
JSON-RPC message per POST, answered as JSON (``initialize``, ``tools/list``,
``tools/call``; a notification is acknowledged with 202). No SSE stream, no
session id: the agent has nothing to be told between its own calls.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from wfpy._elicitation_runtime import ElicitationResponse

#: The version of the protocol this server answers `initialize` with when the
#: client names none it understands.
PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "wfpy"
TOOL_NAME = "ask_user"

TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Ask the user a question and wait for the answer. Use it when a decision is "
        "the user's to make: which direction to take, whether a risky change is worth "
        "trying, whether to stop. Offer `choices` when the answer is one of a few; the "
        "user may also decline, in which case proceed with your best judgment and say so."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question, one sentence."},
            "context": {"type": "string", "description": "What the user needs to decide it."},
            "choices": {"type": "array", "items": {"type": "string"},
                        "description": "The answers offered, if the question has a few."},
        },
        "required": ["question"],
    },
}

#: The elicitation closure `runner._step_agent` injects as `_wf_elicit`.
AskClosure = Callable[..., ElicitationResponse]


def answer_text(resp: ElicitationResponse) -> str:
    """What the agent reads as the tool's result."""
    if resp.declined or resp.answer is None:
        reason = resp.reason or "no answer"
        return (f"The user declined to answer ({reason}). Proceed with your best "
                "judgment and state the assumption you made.")
    return str(resp.answer)


class AskUserServer:
    """The MCP server, one per agent firing; `url` is what the session gets."""

    def __init__(self, ask: AskClosure, name: str = SERVER_NAME):
        self.ask = ask
        self.name = name
        self.calls: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("AskUserServer is not started")
        return f"http://127.0.0.1:{self._server.server_port}/mcp"

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
                pass

            def do_GET(self) -> None:
                self.send_response(405)
                self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    message = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self.send_response(400)
                    self.end_headers()
                    return
                if not isinstance(message, dict):
                    self.send_response(400)
                    self.end_headers()
                    return
                reply = outer.handle(message)
                if reply is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                body = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="wfpy-ask-user-mcp",
                                        daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._thread = None

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """One JSON-RPC message to its reply; None for a notification."""
        method = message.get("method")
        msg_id = message.get("id")
        raw_params = message.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        if isinstance(method, str) and method.startswith("notifications/"):
            return None
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return _result(msg_id, {
                "protocolVersion": wanted if isinstance(wanted, str) and wanted else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": "1"},
            })
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": [TOOL]})
        if method == "tools/call":
            name = params.get("name")
            raw_arguments = params.get("arguments")
            arguments: dict[str, Any] = raw_arguments if isinstance(raw_arguments, dict) else {}
            if name != TOOL_NAME:
                return _result(msg_id, {"content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                                        "isError": True})
            return _result(msg_id, {"content": [{"type": "text", "text": self.call(arguments)}]})
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}

    def call(self, arguments: dict[str, Any]) -> str:
        """The tool: the question to the user, the answer back."""
        question = str(arguments.get("question", "")).strip()
        context = arguments.get("context")
        choices = arguments.get("choices")
        if not isinstance(choices, list):
            choices = None
        else:
            choices = [str(c) for c in choices]
        self.calls.append({"question": question, "context": context, "choices": choices})
        if not question:
            return "The question is empty; ask one sentence."
        resp = self.ask(question, context=str(context) if context else None, choices=choices)
        return answer_text(resp)


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}
