---
name: wfpy-agent-nodes
description: Put an LLM in a wfpy graph as an @agent - transport and connector, the model name, loading an Agent Skill into it, mapping that skill's declared inputs and outputs onto ports, and never trusting what it returns. Use when adding an agent node, wiring a skill into one, building an agentic loop, saying how many times an agent with no input fires, or debugging an agent that returned nothing. Triggers: @agent, acp, connector, mock_outputs, agent skill, WF_SKILLS_ROOT, agentic loop, model, transport, fireable_without_input, agent with no input, agent returned nothing.
---

# Putting an agent in the graph

An `@agent` is an actor like any other: tokens in, tokens out, and the graph decides what
happens next. The decisions belong to the graph (guards, budgets, gates) and the agent
only analyses, proposes or edits. If an agent is choosing control flow, that control flow
should be a guard.

## The decorator

```python
@agent(
    prompt=PROMPT,
    transport="acp",            # the real thing; see below
    connector="claude",         # which coding agent wfpy spawns
    model="opus",               # a name the CONNECTOR offers; see below
    timeout_ms=600_000,         # a file-sized reply exceeds the 120s default
    skill=SKILL_NAME,           # or None
    mock_outputs={"Patched": "..."},
)
class RepairAgent:
    class Ports:
        Failure = Port[str](direction="in")
        Patched = Port[str](direction="out")
```

**State the transport, and make `acp` your project's default.** With `transport=` left
out, wfpy calls a model API directly (`http`), which is not what a graph built to drive a
coding agent wants. A project usually reads the transport from its configuration so that
tests can run offline; let that setting default to `acp` and ask for `mock` by name in
tests, CI and offline work. A default that quietly is not the real thing is how a mock run
gets mistaken for a real one. Because a default run then spends tokens, print transport,
connector and model before any work starts.

**Give every agent `mock_outputs`.** It is the test double, and only `transport="mock"`
reads it. wfpy does not ask for it: a port it does not list is filled in from its declared
type (`"mock:<Port>"` for a `str`, `0` for an `int`, a real file for a `File`), and every
one of those looks like a usable answer. Make the mock reply deliberately *unusable* (not
a valid patch, not a parseable plan) so the reject path is what gets exercised offline. An
agent proposing something wrong is the normal case; a mock that returns a perfect answer
tests nothing.

**The model is a name the connector offers.** wfpy asks the agent for its `model` config
option and matches `model=` against its choices by value, then by name, then by the start
of a value, case-insensitively. Which choices there are is the connector's to say, and it
changes with the connector's version: Claude Code's connector offered `default`, `opus`,
`sonnet`, `haiku` and a list of full model ids when this was measured. A name that matches
nothing does not fail the run. wfpy logs a warning, printed with no logging set up,

```text
The agent's model option has no 'no-such-model'; it offers ['default', 'opus', ...]
```

and the turn runs on the session's default model. A name that matches logs
`Session model set to <value>` at INFO; look for one of the two lines.

## Loading a skill

`skill="<name>"` puts that skill's instructions (the body of its `<name>/SKILL.md`) at the
head of the prompt. wfpy takes the first folder that holds the name, in this order:

1. the directories `WF_SKILLS_ROOT` names (an `os.pathsep` list), each holding skill
   folders directly;
2. `.wf/skills`, then `.claude/skills`, in the workflow's directory and in each directory
   above it up to the top of the project (the top of its git repository), and no higher;
3. `.wf/skills` and `.claude/skills` in the directory the run was started in;
4. `~/.claude/skills`;
5. the folders other agent tools install into: `.agents/skills`, `.codex/skills` and
   `.opencode/skills` in the project, then their user-level folders.

`wfpy skills <workflow.py>` prints the skills a run of that file finds, and the folder
each comes from. Run it instead of working the order out by hand.

- **A skill name is one folder name.** A `/` raises `ValueError: Skill name must not
  include path separators.`, and a name that starts with `.` is refused as a hidden
  entry. A sub-skill at `parent/child/SKILL.md` is only nameable as `child` when `parent`
  is itself a skills root. Add it to `WF_SKILLS_ROOT`.
- **Append to `WF_SKILLS_ROOT`, never `setdefault` it.** A wrapper script often exports it
  before the interpreter starts, so a default never applies, and then your on-disk check
  says the skill is available while wfpy cannot resolve it. Append rather than prepend:
  the first directory in the list that exists is also the run's runtime root, where a
  skill's hooks run.
- **Several roots need a wfpy that has `wfpy skills`.** wfpy 1.1.0 and earlier read the
  whole value as one path, which exists nowhere, and then *no* skill named through the
  variable resolves, not only the new one. `wfpy skills path` is the probe: an older wfpy
  answers `invalid choice: 'skills'`.
- **A skill that is not found fails the run when its agent first fires**, not when the
  graph is built: `FileNotFoundError: Skill '<name>' was not found. Searched roots: ...`.
  On a node deep in the graph that is after the nodes before it have spent their turns,
  so check with `wfpy skills <workflow.py>` before the run.
- A Markdown link or an inline code span that names an existing file of the skill,
  relative to its folder, is rewritten to an absolute path in the injected text, so the
  files under a skill's own `references/` and `scripts/` are readable by an agent with
  file access. A path that does not exist is left as written.

## Match the node's contract to the skill's contract

**Read what the skill declares it takes and produces, and make the ports that.** This is
the step that is easiest to skip and most expensive to get wrong.

A skill whose contract is "analyse first, then edit one item at a time" cannot be served by
one node asked for a finished file. Measured: an agent loaded with such a skill produced
two outputs the compiler rejected, because the prompt demanded a complete file while the
skill demanded staged analysis. Split the phase to match:

```
ScorePerf --Request--> Planner(skill=...) --Plan--> ArtifactGate --Directive--> Applier --Candidate-->
```

- the **planner** loads the skill and produces the artifacts it declares, writing no code
- the **gate** is a `@task` that checks those artifacts exist, which is the only thing an
  orchestrator in such a flow actually does
- the **applier** transcribes the one decided change, and loads no skill: the analysis is
  done, and re-loading it would reopen what the gate just checked

A skill that reads a directory of measurements wants the **path to that directory** on an
input port, not a prose summary of it.

## An agent is AND-firing

`_dequeue_agent_inputs` requires a token on **every** declared input port. So an agent
cannot have an `Initial`/`Retry` pair the way a `@task` can, and **a retry or re-ask edge
into an agent is not expressible**: with a token on `Initial` only, the agent never fires
and the run ends with that token still queued. Put the branch in a `@task` upstream and
have it annotate what it sends; a gate that cannot send a plan back can still mark it
`INCOMPLETE` and instruct the next node to change nothing.

The corollary: **everything one agent needs must reach it in the tokens it consumes.** When
you split one agent into two, re-check that each still receives every input. A plan is not a
substitute for the artifact it describes: a planner given the source produces a plan, and
that plan does not contain the source, so an applier downstream of only the plan has not
been shown the file it is supposed to edit.

## An agent with no input port fires once

Nothing arrives at it, so nothing triggers it. wfpy fires it once in a run, and the
decorator says how many times instead:

```python
@agent(prompt="Propose a kernel.", fireable_without_input=3)   # three candidates
class Propose:
    class Ports:
        Candidate = Port[File](direction="out", ext=".cpp")
```

- The count is for the run, not for each token of the workflow around it. Measured: such
  an agent inside a sub-workflow that received three tokens fired once, and the node that
  joined its output with them fired once too. An agent that has to answer each token needs
  that token on an input port.
- A run that is resumed does not give the firing again: the checkpoint carries what is left.
- On an agent that **has** input ports the option means something else: how many times it
  may fire with nothing on them, besides once for each token. With
  `fireable_without_input=2` and one token it fires three times.

## Never trust the output

Treat a reply as a proposal and let the graph judge it:

1. Write it to a **new** file, never over the input.
2. Run it, or compile it, or check it.
3. Adopt it only if it passes **and** improves the objective by a stated margin.
4. Keep the incumbent otherwise, and record why.

Distinguish a **rejected** candidate from a **worse** one. A compiler or validation failure
is a constraint problem and deserves its own agent, its own skill and its own budget; a
slower result is a different lever. Scoring both as "infinitely bad" and discarding the
diagnostic lets the next round repeat the same illegal change.

Strip a code fence the agent added despite the prompt, and re-check the result still looks
like what you asked for.

## An empty reply logs as success

A reply travels as `{"outputs": {"<PortName>": "..."}}`. An empty payload produces exactly
the log of a successful call (`Prompt completed with stop reason: end_turn`,
`ACP invocation succeeded`), the empty string is emitted as a token like any other, and
the only tell is the length:

```text
ACP invocation succeeded: end_turn (text length: 28, ...)   # {"outputs": {"Out": "pong"}}
ACP invocation succeeded: end_turn (text length: 30, ...)   # {"outputs": {"Candidate": ""}}
```

So **check reply lengths** before believing an agent step worked. Those lines are logged
at INFO:

```python
logging.basicConfig(level=logging.INFO)
logging.getLogger("wfpy").setLevel(logging.INFO)
```

That also surfaces `Session model set to <x>`. For large code payloads, prefer returning the
few values to change (module-level constants, say) and applying them in a deterministic
task, rather than a whole file through the envelope.

## Check the connector before blaming the model

`wfpy connectors` lists the connectors and says which are available; one is available when
its command is on `PATH`. The ACP SDK (`agent-client-protocol`, imported as `acp`) has to be
one this wfpy runs on, and `pip` holds it to the range the `agent` extra gives. A mismatch
fails at the first prompt, not at import: wfpy 1.1.0 on SDK 1.0 gave
`ImportError: cannot import name 'text_block' from 'acp'`.
