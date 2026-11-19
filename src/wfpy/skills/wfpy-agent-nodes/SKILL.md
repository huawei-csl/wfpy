---
name: wfpy-agent-nodes
description: Put an LLM in a wfpy graph as an @agent - transport and connector, the model name, loading an Agent Skill into it, mapping that skill's declared inputs and outputs onto ports, and never trusting what it returns. Use when adding an agent node, wiring a skill into one, building an agentic loop, or debugging an agent that returned nothing. Triggers: @agent, acp, connector, mock_outputs, agent skill, WF_SKILLS_ROOT, agentic loop, model, transport, agent returned nothing.
---

# Putting an agent in the graph

An `@agent` is an actor like any other: tokens in, tokens out, and the graph decides what
happens next. The decisions belong to the graph — guards, budgets, gates — and the agent
only analyses, proposes or edits. If an agent is choosing control flow, that control flow
should be a guard.

## The decorator

```python
@agent(
    prompt=PROMPT,
    transport="acp",            # the real thing; see below
    connector="claude",         # which coding agent wfpy spawns
    model="opus",               # the CONNECTOR's vocabulary, not an API id
    timeout_ms=600_000,         # a file-sized reply exceeds the 120s default
    skill=SKILL_NAME,           # or None
    mock_outputs={"Patched": "..."},
)
class RepairAgent:
    class Ports:
        Failure = Port[str](direction="in")
        Patched = Port[str](direction="out")
```

**`acp` is the default to ship.** These graphs exist to drive a real agent; a default that
quietly does not is how a mock run gets mistaken for a real one. Set the transport to
`mock` explicitly for tests, CI and offline work — never rely on it being the default.
Because a default run spends tokens, print transport, connector and model in the entry
point's banner before any work starts.

**`mock_outputs` is still mandatory on every agent.** It is the test double. Make the mock
reply deliberately *unusable* — not a valid patch, not a parseable plan — so the reject
path is what gets exercised offline. An agent proposing something wrong is the normal case;
a mock that returns a perfect answer tests nothing.

**The model is named in the connector's vocabulary.** wfpy asks the agent for its `model`
config option and matches by value, then name, then prefix, case-insensitively. Claude Code
offers `default, sonnet, haiku, opus`, so `"opus"` works and `"claude-opus-5"` matches
nothing — wfpy logs a warning and leaves the session on its default, silently as far as the
run output goes. Turn on INFO logging and check for `Session model set to ...`.

## Loading a skill

`skill="<name>"` injects that skill's `SKILL.md` into the prompt. wfpy resolves the name
against `WF_SKILLS_ROOT` (an `os.pathsep` list, highest priority) and then `.wf/skills` and
`.claude/skills` at every ancestor of the workflow, then cwd, then `~/.claude/skills`.

- **A skill name may not contain `/`.** A sub-skill at `parent/child/SKILL.md` is only
  nameable as `child` when `parent` is itself a skills root. Add it to `WF_SKILLS_ROOT`.
- **Append to `WF_SKILLS_ROOT`, never `setdefault` it.** A wrapper script often exports it
  before the interpreter starts, so a default never applies — and then your on-disk check
  says the skill is available while wfpy cannot resolve it. The two must agree.
- **Probe before relying on a multi-root value.** On an older wfpy a two-path value is one
  bogus directory, which breaks *every* skill, not just the new one. Check the capability,
  and resolve the skill to `None` when it is absent rather than claiming it.
- **Verify the skill resolves before claiming it.** `skill=` raises if it cannot be found,
  so guard it: `name if (root / name / "SKILL.md").is_file() else None`.
- Skill-relative references are rewritten to absolute paths in the injected text, so a
  skill's own `references/` and `scripts/` are readable by an agent with file access.

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
- the **gate** is a `@task` that checks those artifacts exist — the only thing an
  orchestrator in such a flow actually does
- the **applier** transcribes the one decided change, and loads no skill: the analysis is
  done, and re-loading it would reopen what the gate just checked

A skill that reads a directory of measurements wants the **path to that directory** on an
input port, not a prose summary of it.

## An agent is AND-firing

`_dequeue_agent_inputs` requires a token on **every** declared input port. So an agent
cannot have an `Initial`/`Retry` pair the way a `@task` can, and **a retry or re-ask edge
into an agent is not expressible**. Put the branch in a `@task` upstream and have it
annotate what it sends — a gate that cannot send a plan back can still mark it
`INCOMPLETE` and instruct the next node to change nothing.

The corollary: **everything one agent needs must reach it in the tokens it consumes.** When
you split one agent into two, re-check that each still receives every input. A plan is not a
substitute for the artifact it describes — a planner given the source produces a plan, and
that plan does not contain the source, so an applier downstream of only the plan has not
been shown the file it is supposed to edit.

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
the log of a successful call — `Prompt completed with stop reason: end_turn`,
`ACP invocation succeeded` — and the only tell is the length:

```text
ACP invocation succeeded: end_turn (text length: 30, ...)   # {"outputs": {"Candidate": ""}}
```

So **check reply lengths** before believing an agent step worked:

```python
logging.basicConfig(level=logging.INFO)
logging.getLogger("wfpy").setLevel(logging.INFO)
```

That also surfaces `Session model set to <x>`. For large code payloads, prefer returning the
few values to change — module-level constants, say — and applying them in a deterministic
task, rather than a whole file through the envelope.

## Check the connector before blaming the model

`wfpy connectors` lists what is installed and available; a connector is available when its
command is on `PATH`. The ACP SDK (`agent-client-protocol`, imported as `acp`) must match
what wfpy expects — a wfpy that calls a removed API against a newer SDK fails at the first
prompt with `ImportError: cannot import name 'text_block' from 'acp'`.
