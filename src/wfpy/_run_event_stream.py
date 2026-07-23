"""Live run event stream — a tiny localhost SSE server for the IDE to observe a run.

A workflow run can stream agent message deltas, tool calls, and lifecycle events to
any read-only observer (the IDE) over Server-Sent Events, instead of the observer
polling ``run.*.live.json`` files. The run owns the stream; the observer just reads
it. The bound port is published in ``run.wf-stream.live.json`` next to the other live
files so the IDE can discover and connect to it.

Design notes:
- One server per run, bound to ``127.0.0.1:0`` (an ephemeral free port).
- ``GET /events`` is the SSE endpoint; each connection drains its own queue.
- ``publish()`` fans an event out to all current subscribers; it never blocks the run
  (bounded per-subscriber queues drop oldest on overflow so a slow/stale observer
  can't back-pressure execution).
- Read-only: there are no mutating endpoints. Observing cannot affect the run.
"""

from __future__ import annotations

import json
import logging
import queue
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# Per-subscriber buffered events. Bounded so a stalled observer can't grow memory or
# back-pressure the run; on overflow we drop the oldest event for that subscriber.
_SUBSCRIBER_QUEUE_MAX = 2048
# Seconds the SSE loop waits for an event before emitting a keepalive comment.
_KEEPALIVE_INTERVAL = 15.0


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RunEventStream:
    """A localhost SSE server that broadcasts a run's live events to observers."""

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id
        self.port: Optional[int] = None
        # Per-run bearer token. Even though the server is bound to loopback, a token
        # (required on every request) prevents other local processes and DNS-rebinding
        # web pages from reading the agent stream. Published only in the local
        # run.wf-stream.live.json discovery file.
        self.token = secrets.token_urlsafe(24)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()
        self._seq = 0
        self._closed = threading.Event()
        # Replay buffer: late-connecting observers (the IDE attaches after the run has
        # already started) get recent history so the view isn't empty.
        self._history: list[dict[str, Any]] = []

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> int:
        """Bind a free port and start serving in a background thread. Returns the port."""
        if self._server is not None:
            return self.port  # type: ignore[return-value]
        port = _find_free_port()
        stream = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # silence default stderr logging
                pass

            def _host_ok(self) -> bool:
                # Only accept loopback Host headers. Blocks DNS-rebinding: a web page on
                # some domain resolving to 127.0.0.1 would send Host: <domain>:<port>.
                host = (self.headers.get("Host") or "").strip()
                hostname = host.rsplit(":", 1)[0].strip("[]").lower() if host else ""
                return hostname in ("127.0.0.1", "localhost", "::1")

            def _authed(self) -> bool:
                parsed = urlparse(self.path)
                token = (parse_qs(parsed.query).get("token") or [""])[0]
                if not token:
                    auth = self.headers.get("Authorization") or ""
                    if auth.startswith("Bearer "):
                        token = auth[len("Bearer "):].strip()
                # Constant-time compare to avoid token-guessing via timing.
                return bool(token) and secrets.compare_digest(token, stream.token)

            def _reject(self, code: int) -> None:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                if not self._host_ok():
                    self._reject(403)
                    return
                if not self._authed():
                    self._reject(401)
                    return
                path = urlparse(self.path).path
                if path in ("/health", "/"):
                    body = json.dumps({"ok": True, "runId": stream.run_id}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path != "/events":
                    self._reject(404)
                    return
                self._serve_events()

            def _serve_events(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q = stream._subscribe()
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while not stream._closed.is_set():
                        try:
                            event = q.get(timeout=_KEEPALIVE_INTERVAL)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        payload = json.dumps(event, default=str)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # observer disconnected
                finally:
                    stream._unsubscribe(q)

        self._server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="wf-run-event-stream", daemon=True
        )
        self._thread.start()
        self.port = port
        logger.info("Run event stream listening on http://127.0.0.1:%d/events", port)
        return port

    def stop(self) -> None:
        self._closed.set()
        # Wake any blocked SSE loops so they notice _closed and exit.
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait({"type": "run.stream.closing"})
            except queue.Full:
                pass
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # best-effort teardown
                pass
            self._server = None

    # ── publish / subscribe ──────────────────────────────────────────────────
    def publish(self, event: dict[str, Any]) -> None:
        """Broadcast one event to all current subscribers (non-blocking)."""
        if self._closed.is_set():
            return
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, **event}
            if len(self._history) < 4096:
                self._history.append(event)
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()  # drop oldest, then enqueue newest
                    q.put_nowait(event)
                except queue.Empty:
                    pass

    def _subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        with self._lock:
            for past in self._history[-512:]:  # replay recent history to new observers
                try:
                    q.put_nowait(past)
                except queue.Full:
                    break
            self._subscribers.add(q)
        return q

    def _unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(q)
