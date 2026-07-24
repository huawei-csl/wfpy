"""10 — Fan out to parallel agents, then reduce.

The first of three agent-orchestration examples. They build on the dataflow
model from 01–09 — an `@agent` is just an actor — and show the patterns you
actually reach for when orchestrating LLMs.

Here one input fans out to three **specialist agents** that run concurrently
(01–03: independent actors fire in the same round), and a plain `Report` task
**joins** their outputs into one result. This is the map-reduce shape:
map across specialists in parallel, reduce with a task.

All three agents use `transport="mock"`, so this runs offline with no model or
credentials — each returns its canned `mock_outputs`. The mock ignores the input
(it is a stand-in for a real call); what the example demonstrates is the
*wiring*. Swap any agent to a real transport and the graph is unchanged.

Run it::

    wfpy run examples/10_parallel_agents.py --input "In=quarterly results are strong"
    python examples/10_parallel_agents.py

Expected output::

    {'Out': ['sentiment=positive | summary=a short summary | keywords=alpha, beta']}

Next: 11_routing.py — send each input to the right specialist.
"""

from wfpy import Port, action, agent, connect, run, task, workflow


@agent(prompt="Classify the sentiment.", transport="mock",
       mock_outputs={"Out": "positive"})
class Sentiment:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")


@agent(prompt="Summarize in one line.", transport="mock",
       mock_outputs={"Out": "a short summary"})
class Summary:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")


@agent(prompt="Extract keywords.", transport="mock",
       mock_outputs={"Out": "alpha, beta"})
class Keywords:
    class Ports:
        In = Port[str](direction="in")
        Out = Port[str](direction="out")


@task
class Report:
    """Fires only when all three analyses have arrived (a join)."""

    class Ports:
        Sentiment = Port[str](direction="in")
        Summary = Port[str](direction="in")
        Keywords = Port[str](direction="in")
        Out = Port[str](direction="out")

    @action(consumes={"Sentiment": 1, "Summary": 1, "Keywords": 1}, produces={"Out": 1})
    def combine(self, sentiment: str, summary: str, keywords: str) -> str:
        return f"sentiment={sentiment} | summary={summary} | keywords={keywords}"


@workflow(inputs={"In": str}, outputs={"Out": str})
def analyze() -> None:
    sentiment = Sentiment()
    summary = Summary()
    keywords = Keywords()
    report = Report()

    # fan-out: the same input drives all three agents, concurrently
    connect("In", sentiment.In)
    connect("In", summary.In)
    connect("In", keywords.In)

    # join: Report waits for all three before it fires
    connect(sentiment.Out, report.Sentiment)
    connect(summary.Out, report.Summary)
    connect(keywords.Out, report.Keywords)
    connect(report.Out, "Out")


if __name__ == "__main__":
    print(run(analyze, inputs={"In": "quarterly results are strong"}))
