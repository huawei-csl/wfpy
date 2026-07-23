"""
Lightweight LSP client for agent output validation.

Spawns an LSP server (e.g. clangd), sends a document for validation,
collects structured diagnostics, and shuts down.  Uses raw JSON-RPC
over stdio — no external LSP library required.

Usage::

    diagnostics = validate_file_with_lsp(
        server_cmd="clangd",
        server_args=["--log=error"],
        file_path="/tmp/agent_output.cpp",
        language_id="cpp",
        timeout_ms=30_000,
    )
    for d in diagnostics:
        print(f"  line {d.line}: [{d.severity}] {d.message}")
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("wfpy.lsp_client")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class LspDiagnostic:
    """A single structured diagnostic from the LSP server."""
    line: int  # 1-based
    col: int  # 1-based
    end_line: int = 0
    end_col: int = 0
    severity: str = "error"  # "error" | "warning" | "info" | "hint"
    message: str = ""
    code: str = ""
    source: str = ""

    def format(self, file_path: str = "") -> str:
        """Human-readable one-liner (gcc-like)."""
        loc = f"{file_path}:" if file_path else ""
        return f"{loc}{self.line}:{self.col}: {self.severity}: {self.message}"


_SEVERITY_MAP = {1: "error", 2: "warning", 3: "info", 4: "hint"}


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------

class _LspConnection:
    """Manages the JSON-RPC conversation with an LSP server over stdin/stdout."""

    def __init__(self, proc: subprocess.Popen[bytes]):
        self._proc = proc
        self._req_id = 0
        self._pending: dict[int, threading.Event] = {}
        self._responses: dict[int, dict[str, Any]] = {}
        self._notifications: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    # ── send ──────────────────────────────────────────────────────────

    def _send(self, msg: dict[str, Any]) -> None:
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        assert self._proc.stdin is not None
        self._proc.stdin.write(header + body)
        self._proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> int:
        self._req_id += 1
        rid = self._req_id
        event = threading.Event()
        with self._lock:
            self._pending[rid] = event
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        return rid

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def wait_response(self, rid: int, timeout: float) -> dict[str, Any] | None:
        event = self._pending.get(rid)
        if event is None:
            return None
        event.wait(timeout=timeout)
        with self._lock:
            self._pending.pop(rid, None)
            return self._responses.pop(rid, None)

    def drain_notifications(self, method: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if method is None:
                result = list(self._notifications)
                self._notifications.clear()
            else:
                result = [n for n in self._notifications if n.get("method") == method]
                self._notifications = [n for n in self._notifications if n.get("method") != method]
            return result

    # ── reader thread ─────────────────────────────────────────────────

    def _reader_loop(self) -> None:
        stdout = self._proc.stdout
        assert stdout is not None
        try:
            while True:
                # Parse Content-Length header
                content_length = self._read_header(stdout)
                if content_length is None:
                    break  # EOF
                body = stdout.read(content_length)
                if not body:
                    break
                try:
                    msg = json.loads(body)
                except json.JSONDecodeError:
                    continue

                rid = msg.get("id")
                if rid is not None and ("result" in msg or "error" in msg):
                    # It's a response
                    with self._lock:
                        self._responses[rid] = msg
                        event = self._pending.get(rid)
                        if event:
                            event.set()
                else:
                    # It's a notification
                    with self._lock:
                        self._notifications.append(msg)
        except (OSError, ValueError):
            pass  # pipe closed

    @staticmethod
    def _read_header(stream: Any) -> int | None:
        """Read HTTP-like headers and return Content-Length, or None on EOF."""
        headers: dict[str, str] = {}
        while True:
            line = stream.readline()
            if not line:
                return None  # EOF
            decoded = line.decode("ascii", errors="replace").strip()
            if decoded == "":
                break  # blank line = end of headers
            if ":" in decoded:
                key, _, val = decoded.partition(":")
                headers[key.strip().lower()] = val.strip()
        cl = headers.get("content-length")
        if cl is None:
            return None
        try:
            return int(cl)
        except ValueError:
            return None

    def shutdown(self) -> None:
        """Send shutdown + exit and terminate the server."""
        try:
            rid = self.request("shutdown")
            self.wait_response(rid, timeout=5.0)
            self.notify("exit")
        except (OSError, BrokenPipeError):
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_file_with_lsp(
    server_cmd: str,
    file_path: str,
    language_id: str = "cpp",
    server_args: list[str] | None = None,
    root_uri: str | None = None,
    initialization_options: dict[str, Any] | None = None,
    timeout_ms: int = 30_000,
    extra_flags: list[str] | None = None,
) -> list[LspDiagnostic]:
    """Validate a file by opening it in an LSP server and collecting diagnostics.

    Args:
        server_cmd: LSP server binary (e.g. "clangd", "pylsp", "rust-analyzer").
        file_path: Absolute path to the file to validate.
        language_id: LSP language identifier ("cpp", "python", "rust", etc.).
        server_args: Extra CLI args for the LSP server.
        root_uri: Workspace root URI.  Defaults to the file's parent directory.
        initialization_options: Extra options passed in the ``initialize`` request.
        timeout_ms: Max time to wait for diagnostics.
        extra_flags: For clangd — extra compiler flags injected via
            ``--compile-commands-dir`` or ``initializationOptions.fallbackFlags``.

    Returns:
        List of :class:`LspDiagnostic` instances.
    """
    file_path = os.path.abspath(file_path)
    if not os.path.isfile(file_path):
        return [LspDiagnostic(line=1, col=1, severity="error",
                              message=f"File not found: {file_path}")]

    file_uri = Path(file_path).as_uri()
    if root_uri is None:
        root_uri = Path(file_path).parent.as_uri()

    file_content = Path(file_path).read_text(errors="replace")

    # Build server command
    cmd = [server_cmd] + (server_args or [])

    # For clangd, inject fallback flags
    init_opts = dict(initialization_options or {})
    if server_cmd.endswith("clangd") or "clangd" in server_cmd:
        if extra_flags and "fallbackFlags" not in init_opts:
            init_opts["fallbackFlags"] = extra_flags

    logger.info("[lsp] starting: %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return [LspDiagnostic(line=1, col=1, severity="error",
                              message=f"LSP server not found: {server_cmd}")]

    conn = _LspConnection(proc)
    timeout_s = timeout_ms / 1000

    try:
        # 1. initialize
        init_params: dict[str, Any] = {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {
                        "relatedInformation": True,
                        "codeDescriptionSupport": True,
                    },
                    "synchronization": {
                        "didOpen": True,
                        "didClose": True,
                    },
                },
            },
        }
        if init_opts:
            init_params["initializationOptions"] = init_opts
        rid = conn.request("initialize", init_params)
        resp = conn.wait_response(rid, timeout=min(timeout_s, 15.0))
        if resp is None:
            return [LspDiagnostic(line=1, col=1, severity="error",
                                  message="LSP server did not respond to initialize")]

        # 2. initialized notification
        conn.notify("initialized", {})

        # 3. textDocument/didOpen
        conn.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": file_uri,
                "languageId": language_id,
                "version": 1,
                "text": file_content,
            },
        })

        # 4. Wait for textDocument/publishDiagnostics
        diagnostics = _wait_for_diagnostics(conn, file_uri, timeout_s)

        # 5. textDocument/didClose
        conn.notify("textDocument/didClose", {
            "textDocument": {"uri": file_uri},
        })

        return diagnostics

    finally:
        conn.shutdown()


def _wait_for_diagnostics(
    conn: _LspConnection,
    file_uri: str,
    timeout_s: float,
) -> list[LspDiagnostic]:
    """Poll for ``textDocument/publishDiagnostics`` notifications."""
    deadline = time.monotonic() + timeout_s
    result: list[LspDiagnostic] = []
    seen_any = False

    while time.monotonic() < deadline:
        notes = conn.drain_notifications("textDocument/publishDiagnostics")
        for note in notes:
            params = note.get("params", {})
            if params.get("uri") != file_uri:
                continue
            seen_any = True
            for d in params.get("diagnostics", []):
                rng = d.get("range", {})
                start = rng.get("start", {})
                end = rng.get("end", {})
                sev_num = d.get("severity", 1)
                code_val = d.get("code", "")
                result.append(LspDiagnostic(
                    line=start.get("line", 0) + 1,  # LSP is 0-based
                    col=start.get("character", 0) + 1,
                    end_line=end.get("line", 0) + 1,
                    end_col=end.get("character", 0) + 1,
                    severity=_SEVERITY_MAP.get(sev_num, "error"),
                    message=d.get("message", ""),
                    code=str(code_val) if code_val else "",
                    source=d.get("source", ""),
                ))

        if seen_any:
            # Give a short grace period for additional diagnostic batches
            time.sleep(0.3)
            extra = conn.drain_notifications("textDocument/publishDiagnostics")
            for note in extra:
                params = note.get("params", {})
                if params.get("uri") != file_uri:
                    continue
                for d in params.get("diagnostics", []):
                    rng = d.get("range", {})
                    start = rng.get("start", {})
                    end = rng.get("end", {})
                    sev_num = d.get("severity", 1)
                    code_val = d.get("code", "")
                    result.append(LspDiagnostic(
                        line=start.get("line", 0) + 1,
                        col=start.get("character", 0) + 1,
                        end_line=end.get("line", 0) + 1,
                        end_col=end.get("character", 0) + 1,
                        severity=_SEVERITY_MAP.get(sev_num, "error"),
                        message=d.get("message", ""),
                        code=str(code_val) if code_val else "",
                        source=d.get("source", ""),
                    ))
            break

        time.sleep(0.1)

    return result
