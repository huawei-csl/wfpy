"""12 — Generate, check, and revise until good.

The pattern people most want from agent orchestration: produce a candidate,
check it, and if it falls short, send it back for another pass — a feedback
loop. This is the convergence-loop shape from 07 (two input ports, two actions,
a feedback edge), now driven by an agent.

A `Drafter` agent proposes an initial candidate (here, a quality score). The
`Refine` task loops: while the score is below the bar it raises it and feeds it
back; once the bar is met it emits the result. The loop **self-terminates**
(08: no backpressure, so it must), because the score strictly rises to the
target.

The agent seeds the loop; the refinement step here is a deterministic task so
the example is reproducible. In a real workflow the refiner is itself an
`@agent` re-drafting from the critique — the wiring is identical.

`transport="mock"` keeps it offline: the `Drafter` returns a fixed starting
score.

Run it::

    wfpy run examples/12_repair_loop.py --input "Brief=write a haiku about autumn"
    python examples/12_repair_loop.py

Expected output — seeded at 3, refined to the bar of 8::

    {'Final': [8]}

This is the last tutorial example. From here, swap any `transport="mock"` agent
for a real one (`"http"` with an API key, or a CLI backend) — the graphs do not
change.
"""

from wfpy import Port, action, agent, connect, run, task, workflow


@agent(prompt="Draft an answer to the brief and rate its quality from 1 to 10.",
       transport="mock", mock_outputs={"Score": 3})
class Drafter:
    class Ports:
        Brief = Port[str](direction="in")
        Score = Port[int](direction="out")


@task
class Refine:
    """Raise the score until it meets `target`, then finish (07's loop shape)."""

    target: int  # parameter

    class Ports:
        Start = Port[int](direction="in")  # the agent's initial score
        Back = Port[int](direction="in")  # the fed-back score
        Again = Port[int](direction="out")  # wired back into Back
        Final = Port[int](direction="out")

    @action(consumes={"Start": 1}, produces={"Again": 1, "Final": 1})
    def begin(self, score: int):
        return {"Final": score} if score >= self.target else {"Again": score + 1}

    @action(consumes={"Back": 1}, produces={"Again": 1, "Final": 1})
    def step(self, score: int):
        return {"Final": score} if score >= self.target else {"Again": score + 1}


@workflow(inputs={"Brief": str}, outputs={"Final": int})
def refine_until_good() -> None:
    drafter = Drafter()
    refine = Refine(target=8)

    connect("Brief", drafter.Brief)
    connect(drafter.Score, refine.Start)
    connect(refine.Again, refine.Back)  # feedback edge
    connect(refine.Final, "Final")


if __name__ == "__main__":
    print(run(refine_until_good, inputs={"Brief": "write a haiku about autumn"}))
