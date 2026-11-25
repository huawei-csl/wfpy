"""The ACP stuck detector: its window is configurable and honoured, a silent
window is counted and sends the agent nothing, and the turn is given up when
the budget is spent: so many silent windows in a row.

The detector exists because an agent can wedge. It misfires because an agent
can also be busy and silent: OpenCode running a long `bash` emits no session
event for the whole command, so a 6-minute compile looks identical to a hang.
These tests pin the timing against a fake client, in milliseconds rather than
minutes, so the behaviour is checked without a model or a network.

A silent window used to be answered with a "You appear stuck. Please continue
with the task." prompt. The three fakes below are what OpenCode 2, Codex and
Claude Code were each measured to do with a prompt that arrives during a
turn. None takes it as a nudge of that turn, and each one's turn was lost to
it in its own way.
"""

import asyncio
import gc
import logging
import time

import pytest

pytest.importorskip("acp", reason="the optional `agent` extra is not installed")

import wfpy.acp_client as acp_client  # noqa: E402
from wfpy.acp_client import run_with_stuck_detection  # noqa: E402


class FakeInner:
    def __init__(self):
        self.events = asyncio.Queue()
        self.permission_pending = 0


class FakeClient:
    """A client whose turn stays silent for `busy_for` seconds, then answers.

    `send_prompt` is the agent's turn. A second `send_prompt` while it runs
    is a prompt sent during the turn: `during_the_turn` is what the agent
    does with it, and each agent below does something else.
    """

    def __init__(self, busy_for: float | None):
        self.client = FakeInner()
        self.busy_for = busy_for            # None: a turn that never ends
        self.prompts: list[str] = []
        self.cancelled = False
        self.turn_over = asyncio.Event()

    async def send_prompt(self, session_id: str, prompt: str):
        self.prompts.append(prompt)
        if len(self.prompts) > 1:
            return await self.during_the_turn(session_id)
        return await self.the_turn(session_id)

    async def the_turn(self, session_id: str):
        if self.busy_for is None:
            await asyncio.Event().wait()
        await asyncio.sleep(self.busy_for)
        self.turn_over.set()
        return {"text": "done", "session_id": session_id}

    async def during_the_turn(self, session_id: str):
        return {"text": "", "session_id": session_id}   # answered at once, with nothing

    async def cancel(self, session_id: str):
        self.cancelled = True


class OpenCode2(FakeClient):
    """Refuses the prompt. The refusal used to fail the turn, and the whole
    firing was run again through `opencode run`."""

    async def during_the_turn(self, session_id: str):
        raise RuntimeError("Internal error: Session already has an active ACP prompt: ses_1")


class Codex(FakeClient):
    """Keeps the prompt, and runs it as a turn of its own once the running
    turn has ended. Waiting for it meant waiting for a second answer, and
    for ever when the running turn was hung."""

    async def during_the_turn(self, session_id: str):
        await self.turn_over.wait()
        await asyncio.sleep(0.4)
        for _ in range(5):
            await self.client.events.put({"session_id": session_id, "update": None})
        return {"text": "done again", "session_id": session_id}


class ClaudeCode(FakeClient):
    """Ends the running turn at its next tool call and goes on under the new
    prompt: the turn's own prompt comes back with no answer, and the answer
    is given to the "continue" message."""

    def __init__(self, busy_for: float | None):
        super().__init__(busy_for)
        self.handed_over = asyncio.Event()

    async def the_turn(self, session_id: str):
        work = asyncio.ensure_future(
            asyncio.Event().wait() if self.busy_for is None else asyncio.sleep(self.busy_for))
        handed = asyncio.ensure_future(self.handed_over.wait())
        await asyncio.wait({work, handed}, return_when=asyncio.FIRST_COMPLETED)
        if not work.done():
            work.cancel()
            return {"text": "", "session_id": session_id}
        handed.cancel()
        return {"text": "done", "session_id": session_id}

    async def during_the_turn(self, session_id: str):
        self.handed_over.set()
        await asyncio.sleep(0.3)
        return {"text": "done", "session_id": session_id}


def run(client, **kw):
    """The detector's result, how long it took, and what asyncio had to
    report about a task whose exception nobody read."""
    unread: list[str] = []

    async def drive():
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: unread.append(str(context.get("message"))))
        started = time.monotonic()
        try:
            result = await run_with_stuck_detection(client, "s1", "go", **kw)
            return result, time.monotonic() - started
        finally:
            gc.collect()

    result, elapsed = asyncio.run(drive())
    gc.collect()
    return result, elapsed, unread


def test_a_turn_shorter_than_the_window_is_left_alone():
    client = FakeClient(busy_for=0.20)
    result, _, _ = run(client, stuck_timeout=1.0, max_retries=5)
    assert result["text"] == "done"
    assert client.prompts == ["go"]
    assert not client.cancelled


def test_a_silent_window_sends_the_agent_nothing_and_the_turn_still_finishes(caplog):
    """The measured R1 shape: busy and silent across one window, then done."""
    client = FakeClient(busy_for=0.60)
    with caplog.at_level(logging.WARNING, logger="wfpy.acp_client"):
        result, _, _ = run(client, stuck_timeout=0.25, max_retries=5)
    assert result["text"] == "done"
    assert client.prompts == ["go"]         # the turn's own prompt, and no other
    assert not client.cancelled
    assert "stuck detected, retry 1/5" in caplog.text    # the window is still said


@pytest.mark.parametrize("agent", [OpenCode2, Codex, ClaudeCode])
def test_the_turn_of_a_busy_and_silent_agent_comes_back_whole(agent):
    """Each of the three lost such a turn to the "continue" message: OpenCode
    by an error, Codex by a firing that failed after its answer (before
    Python 3.12) or waited for a second one, Claude Code by an empty answer."""
    client = agent(busy_for=0.60)
    result, elapsed, unread = run(client, stuck_timeout=0.25, max_retries=5)
    assert result["text"] == "done"
    assert client.prompts == ["go"]
    assert not client.cancelled
    assert elapsed < 0.9                    # the turn's own time, and no more
    assert unread == []


def test_a_wider_window_turns_the_same_silence_into_no_silent_window_at_all(caplog):
    """This is the fix for a busy agent: the same run, the only change being
    the window."""
    silence = 0.60
    with caplog.at_level(logging.WARNING, logger="wfpy.acp_client"):
        run(FakeClient(busy_for=silence), stuck_timeout=0.25, max_retries=5)
        narrow = caplog.text.count("stuck detected")
        caplog.clear()
        run(FakeClient(busy_for=silence), stuck_timeout=5.0, max_retries=5)
        wide = caplog.text.count("stuck detected")
    assert narrow >= 1                      # counted
    assert wide == 0                        # left alone


def test_the_budget_is_spent_then_the_turn_is_cancelled():
    client = FakeClient(busy_for=30.0)      # never finishes within the budget
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="stuck after 3 retries"):
        asyncio.run(run_with_stuck_detection(
            client, "s1", "go", stuck_timeout=0.1, max_retries=3))
    assert client.cancelled
    assert client.prompts == ["go"]
    assert time.monotonic() - started == pytest.approx(0.3, abs=0.25)   # three windows


@pytest.mark.parametrize("agent", [OpenCode2, Codex, ClaudeCode])
def test_a_hung_turn_is_given_up_whatever_the_agent(agent):
    """With Codex the budget was never spent: the "continue" message waited
    behind the hung turn, wfpy waited for the message, and the run had no end."""
    client = agent(busy_for=None)

    async def bounded():
        return await asyncio.wait_for(
            run_with_stuck_detection(client, "s1", "go", stuck_timeout=0.1, max_retries=3), 5)

    with pytest.raises(TimeoutError, match="stuck after 3 retries"):
        asyncio.run(bounded())
    assert client.cancelled
    assert client.prompts == ["go"]


def test_an_event_resets_the_window():
    """A streaming agent is never counted silent, however long the turn runs."""
    client = FakeClient(busy_for=0.9)

    async def drive():
        async def chatter():
            for _ in range(9):
                await asyncio.sleep(0.1)
                await client.client.events.put({"session_id": "s1", "update": None})
        task = asyncio.create_task(chatter())
        out = await run_with_stuck_detection(client, "s1", "go",
                                             stuck_timeout=0.25, max_retries=5)
        task.cancel()
        return out

    assert asyncio.run(drive())["text"] == "done"
    assert client.prompts == ["go"]
    assert not client.cancelled


def test_silences_with_work_between_them_are_not_added_up():
    """Five compiles of six minutes in one turn are five silences, and a
    working agent. The count was kept for the whole turn, so the fifth
    cancelled it; an event now starts the count again."""
    client = FakeClient(busy_for=1.5)

    async def drive():
        async def a_sign_of_life_between_silences():
            for _ in range(4):
                await asyncio.sleep(0.3)        # one silent window of 0.2 s, each time
                await client.client.events.put({"session_id": "s1", "update": None})
        task = asyncio.create_task(a_sign_of_life_between_silences())
        out = await run_with_stuck_detection(client, "s1", "go",
                                             stuck_timeout=0.2, max_retries=2)
        task.cancel()
        return out

    assert asyncio.run(drive())["text"] == "done"
    assert not client.cancelled


def test_a_fault_of_the_monitor_is_not_taken_for_a_stuck_turn(monkeypatch):
    async def broken(client, session_id, stuck_timeout, max_windows):
        raise KeyError("a bug in the monitor")

    monkeypatch.setattr(acp_client, "_monitor_events", broken)
    client = FakeClient(busy_for=5.0)
    with pytest.raises(KeyError):
        asyncio.run(run_with_stuck_detection(client, "s1", "go", stuck_timeout=0.1, max_retries=3))
    assert not client.cancelled


def test_a_pending_permission_request_is_not_silence():
    """The agent waits for an answer, and so does wfpy: no window is counted
    while a request is with the user."""
    client = FakeClient(busy_for=0.5)
    client.client.permission_pending = 1
    result, _, _ = run(client, stuck_timeout=0.1, max_retries=2)
    assert result["text"] == "done"
    assert not client.cancelled


def test_an_event_that_arrives_with_the_end_of_the_turn_does_not_fail_it():
    """The turn's last event and its answer handled in one pass of the loop.

    Before Python 3.12 `asyncio.wait_for` drops a cancellation that reaches
    it while its result is ready. The monitor, cancelled at the end of the
    turn with that last event just in, was not stopped: it took the event,
    waited a whole window more, and its TimeoutError became the firing's.
    With the default window that is five minutes of nothing after the
    answer, and then a failed firing with an empty message."""

    class LastEventWithTheAnswer(FakeClient):
        async def the_turn(self, session_id: str):
            await asyncio.sleep(0.05)
            self.client.events.put_nowait({"session_id": session_id, "update": None})
            return {"text": "done", "session_id": session_id}

    client = LastEventWithTheAnswer(busy_for=0)
    result, elapsed, unread = run(client, stuck_timeout=0.5, max_retries=5)
    assert result["text"] == "done"
    assert elapsed < 0.4                    # not the window after the answer
    assert not client.cancelled
    assert unread == []


def test_a_window_that_runs_out_as_the_turn_ends_is_not_a_stuck_turn(monkeypatch):
    """The prompt and the monitor end in one pass of the loop: the turn has
    its result, and the window that ran out with it is not a failure."""
    over = asyncio.Event()

    async def window_ends_with_the_turn(client, session_id, stuck_timeout, max_windows):
        await over.wait()
        raise asyncio.TimeoutError()

    class Ending(FakeClient):
        async def the_turn(self, session_id: str):
            await asyncio.sleep(0.05)
            over.set()                      # the monitor wakes, and ends first
            return {"text": "done", "session_id": session_id}

    monkeypatch.setattr(acp_client, "_monitor_events", window_ends_with_the_turn)
    client = Ending(busy_for=0)
    result, _, unread = run(client, stuck_timeout=1.0, max_retries=5)
    assert result["text"] == "done"
    assert not client.cancelled
    assert unread == []


def test_no_task_is_left_with_an_exception_nobody_read():
    """The monitor whose window ran out ended with a TimeoutError. It was
    replaced and dropped, and asyncio printed its traceback ("Task exception
    was never retrieved") into the log of a run that succeeded."""
    _, _, unread = run(FakeClient(busy_for=0.6), stuck_timeout=0.25, max_retries=5)
    assert unread == []


def test_a_turn_that_fails_is_the_firing_s_error():
    class Failing(FakeClient):
        async def the_turn(self, session_id: str):
            await asyncio.sleep(0.05)
            raise RuntimeError("the agent's process ended")

    with pytest.raises(RuntimeError, match="the agent's process ended"):
        asyncio.run(run_with_stuck_detection(Failing(busy_for=0), "s1", "go",
                                             stuck_timeout=1.0, max_retries=5))
