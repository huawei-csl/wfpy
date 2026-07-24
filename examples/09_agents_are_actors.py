"""09 — An LLM agent is just another task.

Everything so far used plain ``@task`` actors so the dataflow model stayed in
the foreground. The payoff is this: an ``@agent`` is an actor too. It has ports,
it fires when its inputs are ready, it can keep state, and it is wired with the
same ``connect()``. Only the *body* of the firing differs — instead of running
your Python, it calls a language model with the prompt and the consumed tokens.

So every concept from 01–08 applies to agents unchanged: guards route agent
output, schedules sequence multi-step agents, priorities order competing
actions, and a feedback loop (08) is how you build a generate → check → revise
repair cycle.

``transport="mock"`` runs an agent with **no model, no network, no
credentials** — it emits the values in ``mock_outputs`` (and synthesises any
undeclared output port from its type). That makes an agent graph runnable and
testable in CI. Swap the transport for ``"http"`` (with an API key) or a CLI
backend and nothing else about the graph changes.

Here a ``Summarizer`` agent feeds a plain ``Frame`` task — an agent and a task,
side by side on the same edge.

Run it::

    wfpy run examples/09_agents_are_actors.py --input "In=a long paragraph"
    python examples/09_agents_are_actors.py

Expected output::

    {'Out': ['[summary] a one-line summary']}

Next (agent orchestration): 10_parallel_agents.py — fan out to several agents.
"""

from wfpy import Port, action, agent, connect, run, task, workflow


@agent(
    prompt="Summarize the input text in one line.",
    transport="mock",
    mock_outputs={"Summary": "a one-line summary"},
)
class Summarizer:
    """An agent actor: a prompt, an input port, an output port."""

    class Ports:
        In = Port[str](direction="in")
        Summary = Port[str](direction="out")


@task
class Frame:
    """An ordinary task consuming the agent's output."""

    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")

    @action(consumes={"In": 1}, produces={"Out": 1})
    def frame(self, summary: str) -> str:
        return f"[summary] {summary}"


@workflow(inputs={"In": str}, outputs={"Out": str})
def summarize() -> None:
    summarizer = Summarizer()
    frame = Frame()
    connect("In", summarizer.In)
    connect(summarizer.Summary, frame.In)
    connect(frame.Out, "Out")


if __name__ == "__main__":
    print(run(summarize, inputs={"In": "a long paragraph of text"}))
