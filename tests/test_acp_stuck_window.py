"""The ACP stuck detector's window is configurable, and it is honoured.

The detector exists because an agent can wedge. It misfires because an agent
can also be busy and silent: OpenCode running a long `bash` emits no session
event for the whole command, so a 6-minute compile looks identical to a hang.
These tests pin the timing against a fake client, in milliseconds rather than
minutes, so the behaviour is checked without a model or a network.
"""

import asyncio
import time

import pytest

pytest.importorskip("acp", reason="the optional `agent` extra is not installed")

from wfpy.acp_client import run_with_stuck_detection  # noqa: E402


class FakeInner:
    def __init__(self):
        self.events = asyncio.Queue()
        self.permission_pending = 0


class FakeClient:
    """A client whose turn stays silent for `busy_for` seconds.

    `send_prompt` is the agent's turn; the nudges the detector sends arrive as
    further `send_prompt` calls, which is exactly how the real one behaves.
    """

    def __init__(self, busy_for: float):
        self.client = FakeInner()
        self.busy_for = busy_for
        self.prompts: list[tuple[float, str]] = []
        self.cancelled = False
        self._t0 = time.monotonic()

    async def send_prompt(self, session_id: str, prompt: str):
        self.prompts.append((time.monotonic() - self._t0, prompt))
        if len(self.prompts) == 1:          # the turn itself
            await asyncio.sleep(self.busy_for)
            return {"text": "done", "session_id": session_id}
        return {"text": "", "session_id": session_id}   # a nudge returns at once

    async def cancel(self, session_id: str):
        self.cancelled = True


def test_a_turn_shorter_than_the_window_is_never_nudged():
    client = FakeClient(busy_for=0.20)
    result = asyncio.run(run_with_stuck_detection(
        client, "s1", "go", stuck_timeout=1.0, max_retries=5))
    assert result["text"] == "done"
    assert [p for _, p in client.prompts] == ["go"]      # no nudge
    assert not client.cancelled


def test_silence_past_the_window_is_nudged_and_the_turn_still_finishes():
    """The measured R1 shape: busy and silent across one window, then done."""
    client = FakeClient(busy_for=0.60)
    result = asyncio.run(run_with_stuck_detection(
        client, "s1", "go", stuck_timeout=0.25, max_retries=5))
    assert result["text"] == "done"
    nudges = [t for t, p in client.prompts[1:]]
    assert nudges, "a silence longer than the window should have been nudged"
    assert nudges[0] == pytest.approx(0.25, abs=0.2)
    assert not client.cancelled


def test_a_wider_window_turns_the_same_silence_into_no_nudge_at_all():
    """This is the fix: the same run, the only change being the window."""
    silence = 0.60
    narrow = FakeClient(busy_for=silence)
    asyncio.run(run_with_stuck_detection(narrow, "s1", "go",
                                         stuck_timeout=0.25, max_retries=5))
    wide = FakeClient(busy_for=silence)
    asyncio.run(run_with_stuck_detection(wide, "s1", "go",
                                         stuck_timeout=5.0, max_retries=5))
    assert len(narrow.prompts) > 1          # nudged
    assert len(wide.prompts) == 1           # left alone


def test_the_budget_is_spent_then_the_turn_is_cancelled():
    client = FakeClient(busy_for=30.0)      # never finishes within the budget
    with pytest.raises(TimeoutError, match="stuck after 3 retries"):
        asyncio.run(run_with_stuck_detection(
            client, "s1", "go", stuck_timeout=0.1, max_retries=3))
    assert client.cancelled
    assert len(client.prompts[1:]) == 2     # nudged until the last retry cancels


def test_an_event_resets_the_window():
    """A streaming agent is never nudged, however long the turn runs."""
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
    assert [p for _, p in client.prompts] == ["go"]
