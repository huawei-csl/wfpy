"""The firing journal: what each firing did, and replaying it.

Every firing that completes appends one line to ``run.wf-journal.jsonl``:
which actor fired, the tokens it took from each queue (as a digest), the
tokens it put on each queue, and the actor's state afterwards. A run is then
resumable from any step its queue trace shows (``run(resume_from=...,
at_step=N)``): the run starts again from its inputs, and each actor's first
firings -- those the original run had completed by step N -- are replayed from
the journal instead of run.

Replaying per actor rather than restoring a snapshot is what makes this sound
with parallel workers. A snapshot taken at step N can hold half of a firing
still in flight; but in a dataflow network each actor sees the same sequence of
tokens however its firings interleave with others', so its k-th firing can be
replayed whenever its inputs arrive. A replayed firing checks that the tokens
it takes are the ones the original took. When they are not -- an agent upstream
answered differently -- replay stops there, and the run carries on live.

See ``docs/proposals/resume.md``.
"""

from __future__ import annotations

import collections
import hashlib
import json
import threading
from pathlib import Path
from typing import IO, Any

from wfpy._checkpoint_runtime import (
    NotResumable,
    actor_state,
    decode,
    encode,
    restore_actor_state,
)

JOURNAL = "run.wf-journal.jsonl"
VERSION = 1


def actor_path(plan: Any, actor: Any) -> str:
    """The actor's name, under the nested workflows it is in: ``child/leaf``."""
    return "/".join([*plan.overlay_path_prefix, actor.name])


def _digest(encoded: list[Any]) -> str:
    return hashlib.sha256(json.dumps(encoded, sort_keys=True).encode("utf-8")).hexdigest()


def _grouped(log: list[tuple[Any, Any]]) -> list[tuple[Any, list[Any]]]:
    """The values in *log* per queue, queues in the order first touched."""
    by_queue: dict[int, tuple[Any, list[Any]]] = {}
    for queue, value in log:
        by_queue.setdefault(id(queue), (queue, []))[1].append(value)
    return list(by_queue.values())


class Journal:
    """Appends the run's firings, and replays another run's."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seq = 0
        self._lock = threading.Lock()
        self._file: IO[str] | None = path.open("a", encoding="utf-8")
        # Replay: actor path -> that actor's firings still to replay, in order.
        self._replay: dict[str, collections.deque[dict[str, Any]]] = {}
        self.replaying = False
        self.replay_stopped: str | None = None

    # ── Writing ───────────────────────────────────────────────────────────

    def _write(self, line: dict[str, Any]) -> None:
        if self._file is None:
            return
        self._file.write(json.dumps(line) + "\n")
        self._file.flush()

    def header(
        self,
        *,
        workflow_name: str,
        graph: str,
        inputs: dict[str, Any] | None,
        resumed_from: str | None = None,
    ) -> None:
        head: dict[str, Any] = {"version": VERSION, "workflowName": workflow_name, "graph": graph}
        if resumed_from is not None:
            head["resumedFrom"] = resumed_from
        else:
            try:
                head["inputs"] = encode(inputs or {}, "inputs")
            except NotResumable as exc:
                head["inputsNotSaved"] = str(exc)
        with self._lock:
            self._write({"header": head})

    def record(
        self,
        plan: Any,
        actor: Any,
        taken: list[tuple[Any, Any]],
        put: list[tuple[Any, Any]],
        context_version: int,
        *,
        replayed: bool = False,
    ) -> int:
        """Append one completed firing; returns its sequence number."""
        path = actor_path(plan, actor)
        entry: dict[str, Any] = {"actor": path, "fireCount": actor.fire_count}
        if replayed:
            entry["replayed"] = True
        try:
            entry["consumed"] = [
                [queue.id, len(values), _digest([encode(v, path) for v in values])]
                for queue, values in _grouped(taken)
            ]
            entry["produced"] = [
                [queue.id, [encode(v, f"{path}->{queue.id}") for v in values]]
                for queue, values in _grouped(put)
            ]
            entry.update(actor_state(actor, encode, path))
            if plan.context_version != context_version:
                entry["context"] = {
                    "store": encode(plan.context_store, "context"),
                    "version": plan.context_version,
                    "commitSeq": plan.context_commit_seq,
                }
        except NotResumable as exc:
            # Recorded all the same, so the steps keep their numbers; a resume
            # replays up to this firing and runs it live.
            for key in ("consumed", "produced", "state", "chatHistory", "context"):
                entry.pop(key, None)
            entry["notReplayable"] = str(exc)
        with self._lock:
            self.seq += 1
            entry["seq"] = self.seq
            self._write(entry)
            return self.seq

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    # ── Replaying ─────────────────────────────────────────────────────────

    def load_replay(self, entries: list[dict[str, Any]]) -> None:
        """Replay *entries* (another run's firings, in order) as they come due."""
        for entry in entries:
            self._replay.setdefault(entry["actor"], collections.deque()).append(entry)
        self.replaying = bool(self._replay)

    def pending(self) -> bool:
        return any(self._replay.values())

    def stop_replay(self, reason: str) -> None:
        """Run everything live from here; the first reason is kept."""
        if self.replaying:
            self.replay_stopped = reason
        self.replaying = False
        self._replay.clear()

    def replay_firing(self, plan: Any, actor: Any) -> bool | None:
        """Replay *actor*'s next recorded firing.

        ``None``: nothing to replay for it, so fire it live. ``False``: its
        recorded inputs have not all arrived yet. ``True``: replayed.
        """
        due = self._replay.get(actor_path(plan, actor))
        if not due:
            return None
        entry = due[0]
        if "notReplayable" in entry:
            self.stop_replay(f"{entry['actor']}: {entry['notReplayable']}")
            return None

        in_queues = {q.id: q for queues in actor.in_queues.values() for q in queues}
        taking: list[tuple[Any, int]] = []
        for queue_id, count, digest in entry["consumed"]:
            queue = in_queues.get(queue_id)
            if queue is None:
                self.stop_replay(f"{entry['actor']}: no input queue {queue_id}")
                return None
            if queue.size() < count:
                return False
            try:
                arrived = [encode(v, entry["actor"]) for v in queue.peek(count)]
            except NotResumable:
                arrived = None
            if arrived is None or _digest(arrived) != digest:
                self.stop_replay(
                    f"{entry['actor']} (firing {entry['fireCount']}): its inputs differ "
                    "from the original run's"
                )
                return None
            taking.append((queue, count))

        due.popleft()
        taken: list[tuple[Any, Any]] = []
        for queue, count in taking:
            for _ in range(count):
                taken.append((queue, queue.dequeue()))

        out_queues = {q.id: q for queues in actor.out_queues.values() for q in queues}
        put: list[tuple[Any, Any]] = []
        from wfpy._run_artifacts import _record_edge_token

        for queue_id, tokens in entry["produced"]:
            queue = out_queues[queue_id]
            for token in tokens:
                value = decode(token)
                queue.enqueue(value)
                _record_edge_token(plan, queue, value)
                put.append((queue, value))

        actor.fire_count = entry["fireCount"]
        restore_actor_state(actor, entry)
        context_version = plan.context_version
        context = entry.get("context")
        if context is not None:
            plan.context_store = decode(context["store"])
            plan.context_version = context["version"]
            plan.context_commit_seq = context["commitSeq"]
        self.record(plan, actor, taken, put, context_version, replayed=True)
        return True


def load_journal(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The header and the firings of the journal in *run_dir*."""
    path = run_dir / JOURNAL
    if not path.is_file():
        raise FileNotFoundError(
            f"no {JOURNAL} in {run_dir}: the run was made without a queue trace, or "
            "by a wfpy that did not keep one"
        )
    header: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            break  # a line cut short by a run that was killed: the rest is gone
        if "header" in record:
            header = record["header"]
        else:
            entries.append(record)
    if header is None:
        raise ValueError(f"{path} has no header line")
    return header, entries


def entries_at_step(
    run_dir: Path, step: int, entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The firings of *entries* that the run had completed by its step *step*.

    Chosen per actor, not by one cut-off. With parallel workers another
    actor's firing can be journalled between a firing's own journal entry and
    its step in the trace, so "everything journalled by step N" holds a firing
    the trace puts after N. So an actor the trace shows replays its entries up
    to its own last step at or before N; an actor inside a nested workflow
    follows that workflow's steps (its path starts with the workflow's name).
    An actor the trace never shows fires only inside an if or a loop, whose
    firings run with nothing else in flight, so the cut-off of step N itself is
    exact for it.
    """
    if step == 0:
        return []
    trace_path = run_dir / "run.wf-queues.json"
    if not trace_path.is_file():
        raise FileNotFoundError(f"no run.wf-queues.json in {run_dir}: the run kept no queue trace")
    steps = json.loads(trace_path.read_text(encoding="utf-8")).get("steps") or []
    if not 1 <= step <= len(steps):
        raise ValueError(f"{run_dir} has no step {step}; its steps are 1 to {len(steps)}")
    if any("journalSeq" not in entry for entry in steps[:step]):
        raise ValueError(
            f"{run_dir}'s trace carries no journal positions: the run was made by a wfpy "
            "that did not keep a journal"
        )

    traced = {entry["actorInstanceName"] for entry in steps}
    upto: dict[str, int] = {}
    for entry in steps[:step]:
        upto[entry["actorInstanceName"]] = int(entry["journalSeq"])
    at_step = int(steps[step - 1]["journalSeq"])

    def due(entry: dict[str, Any]) -> bool:
        top = entry["actor"].split("/", 1)[0]
        limit = upto.get(top, 0) if top in traced else at_step
        return int(entry["seq"]) <= limit

    return [entry for entry in entries if due(entry)]
