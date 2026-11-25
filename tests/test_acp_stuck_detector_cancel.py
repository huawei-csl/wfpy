"""Two ends of a turn that the stuck detector left loose.

1. The turn ends with an answer in the instant its budget is spent, while
   `session/cancel` is on its way. The answer was dropped and the firing
   failed as "Agent stuck": an agent that answered is not stuck.

2. The caller of the detector is cancelled. The prompt and the monitor were
   left running, for whoever closes the loop to cancel, and the agent was
   not told: its turn went on with nobody waiting for it.

A wfpy run does not reach the second: a firing has an event loop of its own
on a worker thread, and nothing cancels it. It is the contract of
`run_with_stuck_detection` for a caller that has a timeout of its own.
"""

import asyncio

import pytest

pytest.importorskip("acp", reason="the optional `agent` extra is not installed")

from wfpy.acp_client import run_with_stuck_detection  # noqa: E402


class Inner:
    def __init__(self):
        self.events = asyncio.Queue()
        self.permission_pending = 0


class SilentAgent:
    """A turn that says nothing until it is told to end.

    `at_cancel` is what the agent does with `session/cancel`: the reply it
    ends the turn with, or None to go on as if nothing was sent."""

    def __init__(self, at_cancel=None):
        self.client = Inner()
        self.at_cancel = at_cancel
        self.cancels = 0
        self.turn_was_cancelled = False
        self._turn = None

    async def send_prompt(self, session_id, prompt):
        self._turn = asyncio.get_running_loop().create_future()
        try:
            return await self._turn
        except asyncio.CancelledError:
            self.turn_was_cancelled = True
            raise

    async def cancel(self, session_id):
        self.cancels += 1
        if self.at_cancel is not None and not self._turn.done():
            self._turn.set_result(dict(self.at_cancel))
        await asyncio.sleep(0)          # the notification leaves, the loop turns


def detect(agent, **settings):
    settings = {"stuck_timeout": 0.05, "max_retries": 2, **settings}
    return run_with_stuck_detection(agent, "s1", "go", **settings)


class TestAnAnswerThatLandsWhileTheTurnIsCancelled:
    def test_the_answer_is_the_firing_s(self):
        answer = {"stop_reason": "end_turn", "text": "the answer", "refused": []}
        agent = SilentAgent(at_cancel=answer)
        assert asyncio.run(detect(agent)) == answer
        assert agent.cancels == 1

    def test_a_turn_the_agent_ended_at_the_cancel_is_still_stuck(self):
        """What `session/cancel` asks for: the turn ends with `cancelled`."""
        agent = SilentAgent(at_cancel={"stop_reason": "cancelled", "text": "half an ans"})
        with pytest.raises(TimeoutError, match="stuck after 2 retries"):
            asyncio.run(detect(agent))

    def test_a_turn_that_does_not_end_is_stuck_and_stopped(self):
        agent = SilentAgent()
        with pytest.raises(TimeoutError, match="stuck after 2 retries"):
            asyncio.run(detect(agent))
        assert agent.cancels == 1 and agent.turn_was_cancelled


class TestACallerThatIsCancelled:
    @staticmethod
    async def cancel_the_caller(agent, **settings):
        """The detector as a task, cancelled while the turn runs. Returns the
        tasks still alive when the cancellation has come back."""
        caller = asyncio.create_task(detect(agent, **settings))
        await asyncio.sleep(0.02)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    def test_the_turn_and_the_monitor_have_ended_with_it(self):
        agent = SilentAgent()
        left = asyncio.run(self.cancel_the_caller(agent, stuck_timeout=5.0))
        assert left == []
        assert agent.turn_was_cancelled

    def test_the_agent_is_told(self):
        agent = SilentAgent()
        asyncio.run(self.cancel_the_caller(agent, stuck_timeout=5.0))
        assert agent.cancels == 1

    def test_an_agent_that_cannot_be_told_does_not_hide_the_cancellation(self):
        """The connection is gone: the cancellation still goes up, and the
        tasks are still stopped."""
        class Unreachable(SilentAgent):
            async def cancel(self, session_id):
                self.cancels += 1
                raise RuntimeError("Connection closed")

        agent = Unreachable()
        left = asyncio.run(self.cancel_the_caller(agent, stuck_timeout=5.0))
        assert left == [] and agent.cancels == 1 and agent.turn_was_cancelled
