# wfpy — Pythonic agentic workflows on actor-dataflow semantics

[![CI](https://github.com/huawei-csl/wfpy/actions/workflows/ci.yml/badge.svg)](https://github.com/huawei-csl/wfpy/actions/workflows/ci.yml)

A Pythonic framework for agentic workflows, built on actor-dataflow semantics:
agents and tasks are actors in a dataflow graph, connected by FIFO queues and
fired when their inputs are ready. Independent work runs concurrently by
default, feedback loops are ordinary edges, and the graph is exportable as data.

**New to dataflow?** Read [Dataflow concepts](https://github.com/huawei-csl/wfpy/blob/main/docs/dataflow-concepts.md) for the
model — actors, ports, tokens, firings, guards, schedules, priorities — and work
through the tutorial in [`examples/`](https://github.com/huawei-csl/wfpy/tree/main/examples), which builds it up one runnable
step at a time.

wfpy is fully typed and ships `py.typed` for IDE support.

## Install

```bash
pip install wfpy             # the core: workflows, tasks, the CLI
pip install "wfpy[agent]"    # + the @agent transports: HTTP API, MCP tools, ACP
```

Each [release](https://github.com/huawei-csl/wfpy/releases) is published to
[PyPI](https://pypi.org/project/wfpy/) and also attaches the built wheel. Use
these to install a version that is not on PyPI, or without PyPI access:

```bash
# a release wheel straight from GitHub
pip install "wfpy @ https://github.com/huawei-csl/wfpy/releases/download/v1.1.0/wfpy-1.1.0-py3-none-any.whl"

# or a tagged source
pip install "git+https://github.com/huawei-csl/wfpy@v1.1.0"
```

For local development, clone and install editable: `pip install -e ".[dev]"`.

```bash
wfpy run my_workflow.py          # run a workflow
```

## Dependencies

wfpy needs Python 3.10 or newer. What `pip` installs beside it depends on the
extra:

| Install | Package | Version | Used for |
|---|---|---|---|
| `wfpy` | `libcst` | `>=1.4` | the rewrite engine and the sidecar's source edits |
| `wfpy` | `tomli` | `>=2.0`, on Python 3.10 only | reading `connectors.toml` (Python 3.11 has `tomllib`) |
| `wfpy[agent]` | `httpx` | `>=0.27` | the HTTP API transport of an `@agent` |
| `wfpy[agent]` | `mcp` | `>=1.9` | MCP tools for an agent |
| `wfpy[agent]` | `agent-client-protocol` | `>=1.0,<2` | the ACP transport (the package is imported as `acp`) |
| `wfpy[dev]` | `pytest`, `pytest-asyncio`, `mypy`, `ruff` | | the tests, the type check, the lint |

A workflow of tasks and tools needs the core only. The `agent` extra adds what
an `@agent` needs for the HTTP API transport, for MCP tools and for the ACP
transport.

An agent on the `acp` transport needs one thing more, which `pip` does not
install: a connector, the command wfpy spawns. Most are Node.js packages; they
are listed, with the Node.js version each needs, under
[Agent connectors (ACP)](#agent-connectors-acp).

**The ACP SDK is held to its 1.x releases.** `agent-client-protocol` 1.0.0
removed an API that wfpy 1.1.0 and earlier call, and those releases accept any
SDK from 0.10 up. A fresh `wfpy[agent]` install of one of them therefore fails
at the first prompt of an ACP agent:

```text
ImportError: cannot import name 'text_block' from 'acp'
```

With those releases, keep the SDK below 1.0:

```bash
pip install "agent-client-protocol<1"
```

## Agent connectors (ACP)

An `@agent` on the `acp` transport, and the chat in the wfpy IDE, talk to a
coding agent over the [Agent Client Protocol](https://agentclientprotocol.com).
Each such agent is a **connector**: a command wfpy spawns. wfpy knows four, and
a connector is available when its command is on the `PATH`:

| Connector | Command wfpy spawns | Install | Sign in |
|---|---|---|---|
| `opencode` (default) | `opencode acp` | `npm install -g opencode-ai` (or `curl -fsSL https://opencode.ai/install \| bash`) | `opencode auth login` |
| `claude` | `claude-agent-acp` | `npm install -g @anthropic-ai/claude-code @agentclientprotocol/claude-agent-acp` | run `claude` once and `/login`, or set `ANTHROPIC_API_KEY` |
| `codex` | `codex-acp` | `npm install -g @agentclientprotocol/codex-acp` | `codex login`, or set `OPENAI_API_KEY` |
| `gemini` | `gemini --experimental-acp` | `npm install -g @google/gemini-cli` | run `gemini` once and sign in, or set `GEMINI_API_KEY` |

`claude-agent-acp` is the ACP adapter for Claude Code: it drives Claude Code,
so it uses Claude Code's login and settings. Install the connectors you want.
Only `opencode` is needed for the defaults. The npm packages need Node.js 18 or
newer; the Gemini one needs 20, and the Claude ones 22.

The Claude and Codex adapters moved from `@zed-industries` to
`@agentclientprotocol`, under the same commands. The old packages are
deprecated and no longer updated. `@zed-industries/claude-agent-acp` does not
know the newer Claude models: where Claude Code is set to one of them, an agent
on it fails at its first turn ("There's an issue with the selected model").
Uninstall an old package before installing the new one, since both provide the
same command:

```bash
npm uninstall -g @zed-industries/claude-agent-acp
npm install -g @agentclientprotocol/claude-agent-acp

npm uninstall -g @zed-industries/codex-acp          # Codex, the same way
npm install -g @agentclientprotocol/codex-acp
```

### Set up a machine

```bash
# 1. wfpy with the agent transports (HTTP API, MCP tools, ACP)
pip install -e ".[agent]"                  # or "wfpy[agent] @ <wheel-url>"

# 2. the connectors (pick the ones you use)
npm install -g opencode-ai
npm install -g @anthropic-ai/claude-code @agentclientprotocol/claude-agent-acp

# 3. sign in once per agent
opencode auth login
claude                                     # then /login inside it

# 4. check what wfpy sees
wfpy connectors
```

`wfpy connectors` lists every connector, whether it is available and where it
came from:

```text
name       available  source      model      mode         command
claude     yes        discovered  -          -            claude-agent-acp
codex      no         discovered  -          -            codex-acp
gemini     no         discovered  -          -            gemini --experimental-acp
opencode   yes        discovered  -          -            opencode acp
user file: ~/.config/wfpy/connectors.toml  workspace file: <project>/.wfpy/connectors.toml
```

A connector reported `no` while it is installed is not on the `PATH` of the
process that ran wfpy. This happens with nvm, which puts global npm binaries
under `~/.nvm/versions/node/<version>/bin`. When VS Code is started from a
desktop launcher instead of a shell, it may not see that directory. Either
start VS Code from a shell, or give the full path in a connectors file (below).

### Choose a connector

- **One agent:** `@agent(transport="acp", connector="claude")`.
- **A run's default:** `wfpy run flow.py --acp-connector claude`. Agents that
  name no connector use it. Without the flag the default is `opencode`.
- **An explicit command line:** `--agent-cli-acp-command "claude-agent-acp"`
  wins over both.
- **The wfpy IDE:** the `wfpy.acp.connector` setting chooses the connector for
  the chat and for runs started from the editor (default `opencode`).

### Open an agent's session in its own CLI

The `claude` and `opencode` adapters use the agent's own session id as the ACP
session id. So a session can be continued in the agent's CLI, for example
`claude --resume <id>` (started from the session's directory) or
`opencode -s <id>`. Each firing records the session it used, as
`{"id", "connector", "cwd"}`:

- in the `session` field of its `agent.message.end` run event;
- as `agentSession` on the agent's reply in its chat history.

The session's directory is where `wfpy` ran. In the wfpy IDE, the chat header
and each finished turn of a run's agent have a button that opens the session in
a terminal, and another that copies its id. A connector can give its own
command as `resume = "... {sessionId}"` in a connectors file. `resume = ""`
removes it, and then the id is only copied.

### Configure connectors

Two optional TOML files add connectors or override the discovered ones. Both
are read by `wfpy` and by the wfpy IDE:

- `~/.config/wfpy/connectors.toml` (or `$XDG_CONFIG_HOME/wfpy/connectors.toml`)
  holds this machine's settings, such as paths and your model choices.
- `.wfpy/connectors.toml`, at the project root (the directory with
  `pyproject.toml` or `.git`), is checked in with the project and overrides
  the user file.

```toml
[connectors.claude]
command = "claude-agent-acp"          # argv, split as a shell would
model = "sonnet"                      # a model the agent offers
mode = "acceptEdits"                  # a session mode the agent offers

[connectors.opencode]
command = "/opt/opencode/bin/opencode acp"
http_api = true                       # OpenCode's HTTP API beside ACP

[connectors.claude-work]              # a second Claude connector, with its own env
command = "claude-agent-acp"
env = { ANTHROPIC_API_KEY = "..." }
```

### When the agent goes quiet: the stuck window

An ACP turn is watched by a stuck detector. If no session event arrives for
`stuck_timeout_ms` (default 300000, five minutes), wfpy sends the agent a
"You appear stuck. Please continue with the task." message; after
`stuck_retries` of those (default 5) the turn is cancelled with a
`TimeoutError`.

The defaults suit an agent that streams as it works. They do **not** suit an
agent that makes a long tool call which streams nothing. A measured case:
OpenCode running `sleep 360` in `bash` emits no session event at all for the
whole command, so the nudge fires at exactly 300s and lands in the session as
a second, concurrent turn while the first is still working. A compile or a
device run that is silent for over 25 minutes (5 × 300s) is cancelled outright.

Note that `@agent(timeout_ms=...)` does not apply to ACP — this is the knob.

Widen the window for the agents that need it:

```toml
[connectors.claude]
command = "claude-agent-acp"
stuck_timeout_ms = 1800000            # 30 min of silence is fine
stuck_retries = 3
```

or per run, which wins over the connector:

```bash
wfpy run flow.py --acp-stuck-timeout-ms 1800000 --acp-stuck-retries 3
```

`wfpy connectors` prints the effective window in its `stuck` column. Set it
longer than the longest silent tool call the agent makes; the cost of a
generous window is only that a genuinely hung agent takes longer to notice.

An alternative worth preferring where it fits: keep the long-running work out
of the agent entirely. A compile or a test run in a plain `@task` with
`subprocess.run(timeout=...)` never involves ACP, so no stuck detector watches
it, and the timeout is yours.

### Replicate on another machine

1. Install wfpy with `[agent]` and the same connectors (step 1 and step 2
   above). Check versions on the working machine with
   `npm ls -g --depth=0` and pin them with `npm install -g <pkg>@<version>`.
2. Copy `~/.config/wfpy/connectors.toml`, if you have one. Fix the paths in it
   for the new machine. Do not copy API keys into a file that is checked in.
3. Sign in to each agent on the new machine (step 3). Logins are not copied
   with the files.
4. Run `wfpy connectors` from inside the project. Every connector you use
   should show `available yes`.

The project's `.wfpy/connectors.toml` comes with the repository, so nothing
else is needed for it.

## Minimal example

```python
from wfpy import task, workflow, connect, Port

@task
class Doubler:
    factor: int                       # parameter

    class Ports:
        In  = Port[int]()
        Out = Port[int]()

    def action(self, x: int) -> int:
        return x * self.factor

@workflow
def double_pipeline():
    d1 = Doubler(factor=2)
    d2 = Doubler(factor=3)

    connect("Input", d1.In)
    connect(d1.Out, d2.In)
    connect(d2.Out, "Output")
```

## Features

| Feature | Status |
|---|---|
| `@task` with typed ports | ✅ |
| Multi-action tasks with `@action` / `@guard` | ✅ |
| `@workflow` with `connect()` wiring | ✅ |
| FIFO round-robin scheduler | ✅ |
| `@tool(cmd=...)` external process tasks | ✅ |
| `@agent(prompt=...)` LLM tasks | ✅ |
| `@agent(transport="mock")` offline agents (no model, no credentials) | ✅ |
| `@context(read=[...], write=[...])` task context policy | ✅ |
| `@viewer(action_name=..., inputs=[...], viewType=...)` sink/view tasks | ✅ |
| `@source(viewType=...)` one resource — file, folder or URL — emitted once | ✅ |
| Nested workflows | ✅ |
| CLI (`wfpy run`) | ✅ |
| JSON plan IR export | ✅ |

## Running agents offline

`transport="mock"` fires an `@agent` actor without a model, a network call or
any credentials. The actor still obeys the normal firing rules and still emits a
value on every declared output port, so the *shape* of a graph — fan-out and
join, repair feedback loops, guard routing, firing order — can be tested in CI
without spending tokens or depending on how a model happens to word things.

```python
@agent(prompt="Summarize the input.", transport="mock",
       mock_outputs={"Summary": "a concise summary"})
class Summarizer:
    class Ports:
        In = Port[str](direction="in")
        Summary = Port[str](direction="out")
        Score = Port[int](direction="out")   # unlisted → synthesized as 0
```

Ports absent from `mock_outputs` are filled in from their declared type, so an
`int` port receives an `int` and a `File` port is materialized as a real file.
Swapping back to a real transport changes nothing else about the graph.

## Shared context decorator

Use `@context(...)` to attach shared-context read/write policy metadata to task
classes. It can be applied directly to a class or stacked with `@task` in
either order.

`@guard(...)` predicates are supported on both explicit `@action(...)` methods
and implicit default `action(...)` methods.

## Architecture

```
wfpy/
├── types.py      # Port, Resource, File, Map type descriptors
├── core.py       # @task, @action, @guard, @workflow, @agent, @tool decorators
├── graph.py      # connect(), WorkflowGraph builder
├── runner.py     # FIFO round-robin scheduler
├── cli.py        # CLI entry-point (wfpy run ...)
└── __init__.py   # public API re-exports
```

Documentation lives under [docs/](https://github.com/huawei-csl/wfpy/tree/main/docs): the [dataflow model](https://github.com/huawei-csl/wfpy/blob/main/docs/dataflow-concepts.md)
and the [sidecar](https://github.com/huawei-csl/wfpy/blob/main/docs/sidecar.md).

## Resource draft

wfpy includes a draft `Resource` value type for non-file locators (for example
URLs, folders, and endpoint-like references):

```python
from wfpy import Port, Resource

class Ports:
    In = Port[Resource](direction="in")
    Out = Port[Resource](direction="out", ext=".json")
```

`File` remains supported and is treated as a file-focused specialization of
`Resource` for backward compatibility.

### Kind helpers

wfpy also exposes basic helpers for classifying resource locators:

```python
from wfpy import (
    infer_resource_kind,
    is_url_resource,
    is_http_resource,
    is_folder_resource,
    is_file_resource,
)

infer_resource_kind("https://example.com/data")  # "http"
is_url_resource("s3://bucket/key")              # True
is_folder_resource("/tmp/my-dir/")              # True
```

## Agent skills (`SKILL.md`) draft

wfpy supports name-based skill lookup for agents:

```python
from wfpy import agent, Port

@agent(
    skill="writer",
    use_skill=True,
    use_prompt=True,
    use_skill_hooks=True,
    prompt="Focus on concise release notes."
)
class Writer:
    class Ports:
        In = Port[dict](direction="in")
        Out = Port[dict](direction="out")
```

Skill names resolve by convention under your runtime root:

- `.wf/skills/<skill-name>/SKILL.md`
- `.wf/skills/<skill-name>/scripts/<hook>.py|.sh`

wfpy also searches Claude-compatible skill roots in order:

- `<runtime-root>/.claude/skills/<skill-name>/SKILL.md`
- `<cwd>/.wf/skills/<skill-name>/SKILL.md`
- `<cwd>/.claude/skills/<skill-name>/SKILL.md`
- `~/.claude/skills/<skill-name>/SKILL.md`

Then the folders other agent tools install skills into, so a skill installed
for Codex or OpenCode can be named too. They come after every folder above,
so a name found above never resolves here:

- `.agents/skills`, `.codex/skills`, `.opencode/skills` and
  `.opencode/skill`, in the workflow's directory and each directory above it
  up to the top of its git repository, then in `<cwd>` when `<cwd>` is in
  that repository
- `~/.agents/skills`
- `$CODEX_HOME/skills` (`~/.codex/skills` when `CODEX_HOME` is unset; a
  relative `CODEX_HOME` is read from `<cwd>`, as Codex reads it), then
  `$CODEX_HOME/skills/.system`, where Codex installs the skills it ships
- `$XDG_CONFIG_HOME/opencode/skills` and `.../skill` (`~/.config` when
  `XDG_CONFIG_HOME` is unset)

The walk starts at the workflow's directory even when `WF_SKILLS_ROOT` makes
another directory the runtime root, and stops at the top of the git
repository, as Codex and OpenCode stop there. git says where that is
(`git rev-parse --show-toplevel` from the workflow's directory), so what
git refuses bounds nothing: a `.git` that is no repository, and a
repository owned by a user git does not trust (add it to `safe.directory`,
as for `git status`). Without a repository git accepts, or without git,
only the workflow's directory is searched. So a folder above a project,
such as `/tmp/.agents/skills`, is not read for a workflow in `/tmp/job`,
nor for a run started in `/tmp`. A user-level folder the walk or `<cwd>`
reaches, as in a home directory kept in git, is searched once, in its
user-level place; the home directory's other folders (`~/.opencode/skills`)
are a project's, as they are for OpenCode. The same holds for a
`CODEX_HOME` that names a project's `.codex` (`CODEX_HOME=.codex`, started
in the project): Codex reads that folder as `CODEX_HOME`, not as the
project's, and ranks a user's skill after a repository's. A relative
`XDG_CONFIG_HOME` counts as unset.
wfpy's own folders above (`.wf/skills`, `.claude/skills`) are read as
before, up to the filesystem root.

A skill is found by its folder's name, `<folder>/<skill-name>/SKILL.md`, as
in wfpy's own folders; a skill nested deeper in a collection is reached by
naming the collection in `WF_SKILLS_ROOT`.

`wfpy skills <workflow.py>` lists the skills and agent presets a run of the
workflow started in the current directory finds, from the folder it takes,
with what reading each fails with; `--json` prints the same as one JSON
object. An editor that offers skill names runs it with the command it runs
workflows with, from the directory it runs them in, rather than repeating
these rules. The workflow is not loaded, and a relative path, in the
argument or in `WF_SKILLS_ROOT`, is read from that directory. A hidden entry
(`.git`, `.system`) is no skill and is not listed. A run works the folders
out once and keeps them for its plan; `wfpy skills` works them out on each
call.

Prompt composition order:

1. `SKILL.md` body (if `use_skill=True`)
2. `prompt` (if `use_prompt=True`)
3. wfpy runtime instruction (always appended by runtime)

Missing enabled skill files are runtime errors.

A skill's hooks are named in its front matter (`hooks:` with `pre:` and
`post:`) and run from `scripts/<hook>.py` (with `python3`) or
`scripts/<hook>.sh` (with `bash`) in the skill's folder, before and after
the agent's turn, with `use_skill_hooks=True`; a hook runs in the runtime
root and reads the firing as JSON on stdin. Whether one runs is
`--skill-hook-auth` (`WF_SKILL_HOOK_AUTH`): `deny-all`, `allow-all`, or
`policy`, the default, which runs a hook the policy file
(`--skill-hook-policy`, `WF_SKILL_HOOK_POLICY`) approves and no other:

```json
{
  "allow": [
    "lint:pre",
    {"skill": "review", "hook": "pre", "interpreter": "sh",
     "dir": "~/.claude/skills/review"}
  ]
}
```

An entry `"<skill>:<hook>"` or `"<skill>:<hook>:<interpreter>"` approves the
hook by name, in whichever folder the name resolves to. Which folder that is
depends on the working directory, the variables and what a cloned repository
holds, so such an entry also runs the `scripts/pre.sh` of a repository's own
`.claude/skills/review`. An object entry may name the folder it approves,
`dir`: read from the folder of the policy file (a link to it followed), with
`~` expanded, and compared, links resolved, with the skill folder the run
resolved. It approves that folder's hook and no other. `interpreter`
(`python` or `sh`) is optional in both forms. `"requireDir": true`, beside
`allow`, makes the entries that name no folder count for nothing, as
`lint:pre` would above. An object entry with a key wfpy does not read, or a
value that is not text (`null` included), approves nothing rather than
approving by name, and a refusal lists it. Both hooks of a firing run from
the folder its skill was read from, though the agent's turn makes another
where the name resolves first. Why a hook ran or did not names the skill
folder, and the hook's record in the firing's debug data (`WF_AGENT_DEBUG`)
gives it as `skillDir`. `wfpy skills --json` gives each skill's folder as
`dir`: the folder an approval names.

## Claude agent format compatibility (draft)

wfpy agents can also reference Claude-style markdown agent profiles by name:

```python
from wfpy import agent, Port

@agent(
    claudeAgent="code-reviewer",
    useClaudeAgent=True,
    usePrompt=True,
    prompt="Adapt output to this workflow's output ports."
)
class Reviewer:
    class Ports:
        In = Port[dict](direction="in")
        Out = Port[dict](direction="out")
```

Discovery roots (ordered):

- `<runtime-root>/.claude/agents`
- `<cwd>/.claude/agents`
- `~/.claude/agents`

A profile is `<name>.md` or `<name>/AGENT.md` in one of them, a symlink
included, as for a skill.

Supported profile fields in compatibility mode:

- `name`, `description`, markdown body prompt, `skills`

Ignored with non-fatal warnings:

- `model` (wfpy keeps the current model/provider selection)
- `tools`, `disallowedTools`, `permissionMode`, `maxTurns`, `mcpServers`,
  `hooks`, `memory`, `background`, `effort`, `isolation`
