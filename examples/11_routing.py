"""11 — Route each request to the right specialist.

Where 10 sent every input to *all* agents, this sends each input to *one* — the
dispatcher pattern. A plain `Triage` task inspects the request and, using guards
(04), routes it to a code specialist or a writing specialist. Their replies
converge through a `Merge` (03: two actions, one output).

The routing decision is made by a task on the request value, not by an agent, so
it is deterministic. The agents are `transport="mock"` and run offline.

Run it::

    wfpy run examples/11_routing.py --input "In=please review my code"
    python examples/11_routing.py

Expected output — a "code" request goes to the code agent, anything else to the
writer::

    review my code   -> [code expert] here's the fix
    write a blog post -> [writer] here's the copy

Next: 12_repair_loop.py — generate, check, and revise until good.
"""

from wfpy import Port, action, agent, connect, guard, run, task, workflow


@task
class Triage:
    """Route a request to the code path or the writing path."""

    class Ports:
        In = Port[str](direction="in")
        Code = Port[str](direction="out")
        Text = Port[str](direction="out")

    @action(consumes={"In": 1}, produces={"Code": 1})
    @guard(lambda self, request: "code" in request)
    def to_code(self, request: str) -> str:
        return request

    @action(consumes={"In": 1}, produces={"Text": 1})
    def to_text(self, request: str) -> str:  # no guard — the fallback
        return request


@agent(prompt="Answer as a code reviewer.", transport="mock",
       mock_outputs={"Out": "[code expert] here's the fix"})
class CodeAgent:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")


@agent(prompt="Answer as a copywriter.", transport="mock",
       mock_outputs={"Out": "[writer] here's the copy"})
class TextAgent:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")


@task
class Merge:
    """Whichever specialist replied, forward it (03)."""

    class Ports:
        Code = Port[str](direction="in")
        Text = Port[str](direction="in")
        Out = Port[str](direction="out")

    @action(consumes={"Code": 1}, produces={"Out": 1})
    def from_code(self, reply: str) -> str:
        return reply

    @action(consumes={"Text": 1}, produces={"Out": 1})
    def from_text(self, reply: str) -> str:
        return reply


@workflow(inputs={"In": str}, outputs={"Out": str})
def triage() -> None:
    triage_task = Triage()
    code = CodeAgent()
    text = TextAgent()
    merge = Merge()

    connect("In", triage_task.In)
    connect(triage_task.Code, code.In)
    connect(triage_task.Text, text.In)
    connect(code.Out, merge.Code)
    connect(text.Out, merge.Text)
    connect(merge.Out, "Out")


if __name__ == "__main__":
    for request in ("please review my code", "write a blog post"):
        result = run(triage, inputs={"In": request})
        print(f"{request:20} -> {result['Out'][0]}")
