"""Human-in-the-loop elicitation: let an ``@agent`` pause and ask the user.

This module is the seam behind the builtin ``ask_user`` tool. When an HTTP-transport
agent (with ``@agent(ask_user=True)``) calls ``ask_user`` mid-firing, the runtime
invokes an :data:`ElicitationHandler` that surfaces the question, blocks for a
reply, and returns it as the tool result.

Handler resolution (in :func:`resolve_base_handler`):

1. an explicit ``elicitation_handler`` supplied to ``run(...)`` — for embedding,
   the IDE, or an ACP/SSE frontend;
2. the socket handler when ``--elicit-socket`` names one (below);
3. a console handler (default when stdin/stderr is a TTY, or ``--interactive``);
4. otherwise no handler — the closure applies the non-interactive fallback
   (graceful "no user available" by default; strict failure under
   ``--elicit-require``; a fixed reply under ``--elicit-default``).

The console handler serializes on a process-wide lock because the scheduler runs
agents on a thread pool (see ``runner.execute_plan``), so two agents may try to
prompt at once.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
from typing import Any, Callable, cast

# Serializes console prompts so concurrent agents never interleave on the tty.
_console_lock = threading.Lock()


class ElicitationUnavailableError(RuntimeError):
    """Raised when an agent asks the user but strict mode has no way to answer."""


@dataclasses.dataclass
class ElicitationRequest:
    """A question an agent wants to put to the user."""

    question: str
    context: str | None = None
    choices: list[str] | None = None
    agent_name: str = ""
    model: str = ""
    run_id: str = ""
    timeout_ms: int = 600_000


@dataclasses.dataclass
class ElicitationResponse:
    """The outcome of an elicitation."""

    answer: str | None
    declined: bool = False
    reason: str = ""


ElicitationHandler = Callable[[ElicitationRequest], ElicitationResponse]
"""A callable that surfaces a question to the user and returns their reply."""


def console_elicitation_handler(req: ElicitationRequest) -> ElicitationResponse:
    """Default TTY handler: print an attributed prompt and read one reply.

    The prompt is written to stderr and clearly attributed to the *agent* — the
    question text is authored by the (untrusted) model, never by wfpy itself.
    Serialized under :data:`_console_lock`. Honors ``req.timeout_ms`` on POSIX.
    """

    with _console_lock:
        who = f"Agent '{req.agent_name}'" if req.agent_name else "Agent"
        if req.model:
            who = f"{who} ({req.model})"
        lines = [
            "",
            f"┌─ {who} asks ─────────────────────────────",
            *[f"│ {line}" for line in req.question.splitlines() or [""]],
        ]
        if req.context:
            lines += [f"│ context: {line}" for line in req.context.splitlines()]
        if req.choices:
            lines.append(f"│ choices: {', '.join(str(c) for c in req.choices)}")
        lines.append("└─ your answer (empty to decline):")
        sys.stderr.write("\n".join(lines) + " ")
        sys.stderr.flush()

        raw = _read_line_with_timeout(req.timeout_ms)
        if raw is None:
            return ElicitationResponse(answer=None, declined=True, reason="timed out")
        answer = raw.strip()
        if not answer:
            return ElicitationResponse(answer=None, declined=True, reason="empty reply")
        return ElicitationResponse(answer=answer)


def _read_line_with_timeout(timeout_ms: int) -> str | None:
    """Read one line from stdin, returning ``None`` on EOF or timeout.

    Uses ``select`` on POSIX so a slow human doesn't hang forever; falls back to
    a blocking read where ``select`` on stdin is unavailable (e.g. Windows).
    """

    if timeout_ms and timeout_ms > 0:
        try:
            import select

            ready, _, _ = select.select([sys.stdin], [], [], timeout_ms / 1000)
            if not ready:
                return None
        except (ImportError, OSError, ValueError):
            pass  # fall through to a plain blocking read
    line = sys.stdin.readline()
    if line == "":  # EOF
        return None
    return line


class SocketElicitationHandler:
    """A question put to whoever listens on a Unix socket: an IDE, a harness.

    THE WIRE FORMAT, one JSON object a line, the contract with the client:

    wfpy -> client   {"type": "question", "id": 1, "agent": "planner",
                      "model": "opus", "run_id": "...", "question": "...",
                      "context": "..." | null, "choices": ["Allow", "Reject"] | null,
                      "timeout_ms": 600000}
    client -> wfpy   {"id": 1, "answer": "Allow"}
                     {"id": 1, "declined": true, "reason": "..."}   (or no answer at all)

    An agent's permission request over ACP arrives the same way, with the tool
    call's title as the question and the agent's options as the choices. One
    connection serves the run; questions are serialized on it; a connection
    that cannot be made, closes, or answers with another id is a declined
    question, never a failed run.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._sock: Any = None
        self._reader: Any = None
        self._next_id = 0

    def _connect(self) -> bool:
        if self._sock is not None:
            return True
        import socket

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self.path)
        except OSError:
            return False
        self._sock = sock
        self._reader = sock.makefile("r", encoding="utf-8")
        return True

    def _close(self) -> None:
        try:
            if self._reader is not None:
                self._reader.close()
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        self._sock = None
        self._reader = None

    def __call__(self, req: ElicitationRequest) -> ElicitationResponse:
        import json

        with _console_lock:
            if not self._connect():
                return ElicitationResponse(answer=None, declined=True,
                                           reason=f"no listener on {self.path}")
            self._next_id += 1
            qid = self._next_id
            message = {
                "type": "question", "id": qid, "agent": req.agent_name, "model": req.model,
                "run_id": req.run_id, "question": req.question, "context": req.context,
                "choices": req.choices, "timeout_ms": req.timeout_ms,
            }
            try:
                self._sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
                if req.timeout_ms and req.timeout_ms > 0:
                    self._sock.settimeout(req.timeout_ms / 1000)
                line = self._reader.readline()
            except OSError as exc:
                self._close()
                return ElicitationResponse(answer=None, declined=True, reason=f"listener lost: {exc}")
            if not line:
                self._close()
                return ElicitationResponse(answer=None, declined=True, reason="listener closed")
            try:
                reply = json.loads(line)
            except ValueError:
                return ElicitationResponse(answer=None, declined=True, reason="reply was not JSON")
            if not isinstance(reply, dict) or reply.get("id") != qid:
                return ElicitationResponse(answer=None, declined=True, reason="reply to another question")
            if reply.get("declined") or reply.get("answer") is None:
                return ElicitationResponse(answer=None, declined=True,
                                           reason=str(reply.get("reason") or "declined"))
            return ElicitationResponse(answer=str(reply["answer"]))


def _is_interactive() -> bool:
    """Best-effort detection of a human at a terminal."""

    try:
        return bool(sys.stdin.isatty() and sys.stderr.isatty())
    except (AttributeError, ValueError):
        return False


def resolve_base_handler(plan_options: dict[str, Any]) -> ElicitationHandler | None:
    """Resolve the effective handler, or ``None`` for the non-interactive fallback."""

    explicit = plan_options.get("_elicitation_handler")
    if callable(explicit):
        return cast(ElicitationHandler, explicit)

    socket_path = str(plan_options.get("elicit_socket") or "").strip()
    if socket_path:
        handler = plan_options.get("_elicit_socket_handler")
        if handler is None:
            handler = SocketElicitationHandler(socket_path)
            plan_options["_elicit_socket_handler"] = handler
        return cast(ElicitationHandler, handler)

    interactive = plan_options.get("elicit_interactive")
    if interactive is None:
        interactive = _is_interactive()
    if interactive:
        return console_elicitation_handler
    return None


def build_elicit_closure(
    plan_options: dict[str, Any],
    *,
    agent_name: str,
    model: str = "",
    run_id: str = "",
    event_publish: Callable[[dict[str, Any]], None] | None = None,
) -> Callable[..., ElicitationResponse]:
    """Build the actor-scoped ``ask`` closure injected onto ``agent_options``.

    Mirrors the ``_wf_event_publish`` bridge in ``runner._step_agent``: the
    returned closure closes over this agent's identity and the run's event sink,
    emits ``agent.question.requested`` / ``agent.question.answered`` for
    observers, invokes the resolved handler (or the non-interactive fallback),
    and returns an :class:`ElicitationResponse`.
    """

    timeout_ms = int(plan_options.get("elicit_timeout_ms", 600_000) or 0)
    default_answer = plan_options.get("elicit_default")
    require = bool(plan_options.get("elicit_require", False))

    def ask(
        question: str,
        *,
        context: str | None = None,
        choices: list[str] | None = None,
    ) -> ElicitationResponse:
        req = ElicitationRequest(
            question=question,
            context=context,
            choices=choices,
            agent_name=agent_name,
            model=model,
            run_id=run_id,
            timeout_ms=timeout_ms,
        )
        if event_publish is not None:
            _safe_publish(
                event_publish,
                {
                    "type": "agent.question.requested",
                    "question": question,
                    "context": context,
                    "choices": choices,
                },
            )

        handler = resolve_base_handler(plan_options)
        if handler is not None:
            try:
                resp = handler(req)
            except ElicitationUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001 — a failing handler must not crash the run
                resp = ElicitationResponse(
                    answer=None, declined=True, reason=f"handler error: {exc}"
                )
        elif default_answer is not None:
            resp = ElicitationResponse(answer=str(default_answer), reason="elicit_default")
        elif require:
            _publish_answer(event_publish, declined=True, reason="strict: no user available")
            raise ElicitationUnavailableError(
                f"Agent '{agent_name}' asked the user a question but the run is "
                "non-interactive and no elicitation handler is available "
                "(--elicit-require is set)."
            )
        else:
            resp = ElicitationResponse(
                answer=None,
                declined=True,
                reason="no user available (non-interactive run)",
            )

        _publish_answer(event_publish, declined=resp.declined, reason=resp.reason)
        return resp

    return ask


def _publish_answer(
    event_publish: Callable[[dict[str, Any]], None] | None,
    *,
    declined: bool,
    reason: str = "",
) -> None:
    # Note: the user's answer text is intentionally NOT broadcast — the run event
    # stream fans out to observers and replies may be sensitive. Only the
    # decline/answered status and reason are published.
    if event_publish is None:
        return
    _safe_publish(
        event_publish,
        {
            "type": "agent.question.answered",
            "declined": declined,
            "reason": reason,
            "answered": (not declined),
        },
    )


def _safe_publish(
    event_publish: Callable[[dict[str, Any]], None], event: dict[str, Any]
) -> None:
    try:
        event_publish(event)
    except Exception:  # noqa: BLE001 — observability must never break a run
        pass
