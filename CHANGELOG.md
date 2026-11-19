# Changelog

All notable changes to wfpy are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and wfpy uses
[PEP 440](https://peps.python.org/pep-0440/) versioning.

## [Unreleased]

### Added
- `WF_SKILLS_ROOT` names several skill directories, separated by
  `os.pathsep`, searched in the order written; a single directory still
  means what it always did, one whose own path holds the separator
  included (a value that is itself a directory is not split). A skill name
  may not hold a path separator, so
  a skill nested inside another collection
  (`pypto-op-perf-tune/tune-swimlane`) is only reachable when its own
  directory is a root, which one value could not say while also naming the
  collection around it. It also lets an operator point wfpy at the skill
  directories of several tools, wherever they keep them. The first
  directory listed that exists is the runtime root: the base of the walk up
  the tree for `.wf/skills`, `.claude/skills` and `.claude/agents`, and the
  directory a skill hook runs in. The order therefore decides more than
  priority: a project's own `.wf/skills` and `.claude/agents` are found
  only when the walk starts in that project, so its directory goes first.
- A skill installed for another agent tool is found. A skill is the same
  `SKILL.md` folder whichever tool installed it, but wfpy looked only in its
  own and Claude's folders, so a skill installed for Codex or OpenCode was
  reported as not found unless `WF_SKILLS_ROOT` named its folder. wfpy now
  also searches `.agents/skills` (the folder the Agent Skills convention
  shares between tools), `.codex/skills`, `.opencode/skills` and
  `.opencode/skill`, in the workflow's directory and above it up to the top
  of its git repository, and in the working directory when it is in that
  repository, then `~/.agents/skills`, `$CODEX_HOME/skills` (`~/.codex` by
  default; a relative value read from the working directory, as Codex reads
  it), `$CODEX_HOME/skills/.system` (the skills Codex ships) and
  `$XDG_CONFIG_HOME/opencode/skills` and `skill` (`~/.config` by default).
  They are searched after every folder searched before, `~/.claude/skills`
  included, so a name that resolved before resolves to the same folder:
  these folders only add names. The walk up starts at the workflow's
  directory, `WF_SKILLS_ROOT` or not, and stops where git says the
  repository ends (`git rev-parse --show-toplevel`), as Codex and OpenCode
  stop there. A `.git` git does not take for a repository, or a repository
  of dubious ownership git refuses, bounds nothing: with no repository, or
  no git, only the workflow's directory is searched. So a folder above the
  project (`/tmp/.agents/skills`) is not read, whoever planted a `/tmp/.git`.
  git is not handed the variables that name another repository's parts,
  which a git hook exports (`GIT_DIR`, `GIT_COMMON_DIR`, …); a git that does
  not answer within 10 s is said in the log.
  A user-level folder is searched in its user-level place however the walk
  reaches it; a home directory kept in git has its other folders
  (`~/.opencode/skills`) searched as a project's. A `CODEX_HOME` that names
  a project's `.codex` is the user's too, as Codex reads it.
- `wfpy skills <workflow.py> [--json]` lists the skills and agent presets a
  run of the workflow started in the current directory finds: the folders
  searched, in order, the folder each name resolves to, what reading its
  `SKILL.md` fails with, its hooks as the runtime reads them, and the
  presets with the skills they preload. It runs the runtime's own lookup
  and does not load the workflow, so an IDE that runs it with the command
  it runs workflows with offers the skills a run finds, without copying the
  rules or asking another process. A relative path is read from the
  current directory, a hidden entry (`.git`, `.system`) is not listed, and
  each call looks again. A preset over 256 kB is listed without its
  description; a run reads it whole, as before.
- The lookup roots are worked out once per plan, not on every skill read,
  hook and preset lookup of every firing. Parallel workers wait for the
  first to work them out rather than each run git.
- An agent preset's `description` (and `name`) is read as text: one with a
  comma was a list.
- The sidecar answers `pythonEnvironment` for a workflow file: the
  interpreter running it, the import roots `wfpy plan` loads the file with,
  and the folders its installed packages come from. An IDE configures its
  Python language server from the answer, so a workflow that runs is not
  drawn with its imports underlined because the editor looks elsewhere.
- The graph export says what the source wrote beside what it evaluated to.
  A node's parameter carries `expression`, the text of its keyword in the
  call that made the node (`File(path=TESTBENCH)`, not the `File(...)` it
  built); a node of a workflow a factory builds (`x = factory(...)()`) has
  the factory's parameters, with the call's text, and the factory's
  signature as `definitionParams` (`defaultValue` only where it declares
  one). A nested workflow's `graph.meta.parameters` add `defaultValue` the
  same way: `value` is what this instance was given, not a default.
- An annotation argument's `value` is a Python expression, the decorator's
  own text where it writes the argument (`network=NETWORK`), and an
  annotation or argument wfpy works out (the `viewer` a source or a
  StreamBlocks class gets, a source's `port`, a viewer's default `action`)
  is marked `"derived": true`, so writing it back does not add what the
  class never said.
- The sidecar answers `portUses {entity}`: the project's workflows that
  instantiate a class (or the workflow a factory builds), found by its
  definition, read without running anything; `renamePort`,
  `deleteEntityPort`, `deletePort` and a workflow port's rename take that
  `usedIn` and follow or refuse every file (features `portUsesAcrossFiles`,
  `portUsesQuery`). `typeParameters {type}` answers the parameters a type
  takes. `createNode` takes `expressions`, written verbatim.

### Fixed
- An ACP agent runs on `agent-client-protocol` 1.0. That release removed the
  `text_block` helper the transport built its prompt with, and the `agent`
  extra accepted any SDK from 0.10 up, so a fresh `wfpy[agent]` install failed
  at the first prompt of every ACP agent (`ImportError: cannot import name
  'text_block' from 'acp'`). The extra now requires `>=1.0,<2`. With wfpy
  1.1.0 and earlier, `pip install "agent-client-protocol<1"` restores it.
- A run on the Claude connector under its new name
  (`@agentclientprotocol/claude-agent-acp`) no longer prints a traceback for
  each `auth/status_update` the connector sends: a notification the ACP SDK
  has no route for is dropped, as a notification a client does not know is.
- An agent inside a nested workflow receives the run's options. `run()` set
  them on the top plan only, so such an agent was invoked without the ACP
  connector and its stuck window, the tool and skill-hook policies, the
  elicitation handler and the `source_path` its workspace connectors are found
  from. One connection to the elicit socket serves every plan of the run.
- The validators a port declares run inside a nested workflow; they were
  skipped there in silence. A nested workflow is checked with the run's
  validators and, over them, those registered beside its own file. **This
  changes what a run does:** a nested output that fails its validators now
  fails the run under the default `enforce` mode, as it did at the top level.
- An output that fails validation is not delivered. The validators ran after
  the output was put in its queue, so a failed run's checkpoint held the
  rejected output beside the input given back, and a resume delivered both the
  rejected output and the accepted one.
- A skill whose directory is a symlink is found. The lookup resolved
  `<root>/<name>` before checking that it was still under `<root>`, and a
  symlink resolves elsewhere, so every skill installed into `.claude/skills`
  as a link into a checkout was passed over in silence and reported as not
  found. Containment is now checked on the path as written, which is where
  it was doing its work: `_validate_skill_name()` already refuses a name
  holding a path separator or `..`.
- An agent preset installed as a symlink (`<name>.md`, or a `<name>` folder
  holding `AGENT.md`) is found, as a symlinked skill is; the lookup
  resolved it before checking containment and passed it over.
- A run whose workflow holds a sub-workflow taking no input ends. The
  sub-workflow node had its inputs whenever it had none, so after its one
  firing the scheduler fired it again forever, each empty re-run counted as
  progress; it now fires once, as a workflow instance does.
- `wfpy run` takes the workflow a file has of its own when it also imports
  one to use as a node (`from pipeline import pipeline`); it said "Multiple
  workflows found". A workflow is imported when the file imports its name, so
  one a factory from elsewhere builds (`site = make_site(3)`) is the file's
  own, and one bound under two names is one. `wfpy plan` takes it first too;
  with several of the file's own, it still takes the first name, as before.
- The sidecar's `listTaskTypes` without a `kind` lists the file's classes of
  every kind, as `types` and grouped in `byKind`, and ignores the `workflow`
  scope a caller adds to every query: a chat tool asking that way was told
  "missing 1 required keyword-only argument: 'kind'".
- `connect()` (and `>>`) given a node rather than one of its ports
  (`connect(a, b)`) says so where it is written, naming the node and the
  ports it has on that side, instead of "Invalid source" from the plan
  builder. The diagram's partial graph flags that node with the message
  (`node_not_port`); it took the error off the file and put it nowhere.
- Diagram edits leave the project importable. `createPort` adds the port
  to `@workflow(inputs=, outputs=)` (it replaced the module with the
  workflow's body); `deletePort` and renaming a workflow's own port edit
  that dict and its connects, and refuse, naming the line, a port used by
  name elsewhere: any string of the port's name but a docstring, a
  keyword's value and an instance's `getattr(x, "name")`. `renamePort` /
  `updatePortType` take the IDE's arguments. `deleteEntityPort` refuses a
  port an action or a workflow uses. A port a workflow may name some way an
  edit cannot follow (`for p in ("Out",): getattr(a, p)`, `(a, b)[i].Out`,
  `key = "Out"` in the class) is refused as `port_used_by_name`, naming the
  file and line. Ports are written with their direction. An op on a
  workflow a factory builds edits the inner `@workflow`. `createNode`
  imports a type the module does not bind as it runs (an `if
  TYPE_CHECKING:` import is not), from the package that re-exports it; a
  project's `@workflow def filter` wins over the builtin. `createNode` and
  `renameNode` refuse a name the workflow already has (`name_exists`: an
  instance, a loop variable, a parameter), one that would hide what the
  workflow reads (`name_shadows`: `Text = Text(...)`, a builtin, a star
  import's name, a factory's local), and a Python keyword
  (`invalid_identifier`). A class task's new parameter is an annotated
  field, never an `__init__`; an annotation edit imports its decorator. A
  value that is not Python is refused as `invalid_expression`, and a node's
  argument that reads a name not bound where it is written (nothing binds
  it, or the workflow binds it only below the node) as `name_not_bound`.
  An edit that writes several files writes them all or none.
- `renameNode` renames the local wherever Python reads it: `getattr(a, ...)`,
  a tuple a loop goes over, a closure, an assignment that is not one call
  (`a = X() if dry else Y()`). It renamed only `a = Call()`, `a.port` and a
  connect's end, and the workflow then failed with a `NameError`. A local
  bound by a definition or an import, or declared `nonlocal`, is refused as
  `name_used_otherwise`. A node and a port of one workflow are not named
  alike (`name_exists`): renamed by its name alone, the node was renamed for
  the port.
- `updateNodeParameter` writes an argument where the call that makes the
  node gives it: in place when it is given by position (`make("a")`); it
  appended a keyword, and the call got two values. A node a helper function
  makes (`s = _source("a")`), or whose call may give the argument through a
  `*args` / `**kwargs`, is refused as `arguments_elsewhere`; the export no
  longer shows the helper's call text as the node's.
- An argument written over several lines lands at the columns its text
  gives its later lines, which are the file's as the export gives them
  (`expression`) and the panel shows them. Written back, libcst indented
  those inside brackets by the block once more, so each Update of the
  field, even unchanged, moved them right. A value that is the argument as
  it is, but for whitespace or its own parentheses, is not written at all.
  An argument the file writes in parentheses over lines is exported with
  them, which without them is no expression.
- The run's live files are written whole: `run.wf-viewer.live.json`, the
  agent-context and context live files, and the final overlay, queue trace
  and run record go to a sibling file renamed over the target, so a reader
  polling them (the IDE, every 400ms) never finds one half written. Before,
  `Path.write_text` truncated the file first, and a read in between found it
  empty or cut short: the IDE took that for no run and showed the previous
  one, so the running ring and the queue sizes blinked all through a run.
- The live files go only after the run's record is in `run-log.jsonl`, on
  success as on failure. They went before it, and in between a reader found
  only the previous run.
- A run refuses a skill, agent preset or hook name that starts with `.`,
  saying why. `wfpy skills` does not list a hidden entry, as Codex and
  Claude Code skip one, but `skill=".draft"` loaded
  `.claude/skills/.draft`, which no list offers, and `skill=".system"` took
  Codex's `.system` root for a skill. The list now leaves such a name out
  by the same check, so the two cannot differ.

## [1.1.0] — 2026-10-14

First release published to [PyPI](https://pypi.org/project/wfpy/):
`pip install wfpy`, or `pip install "wfpy[agent]"` for the agent transports.

### Added
- ACP connectors: an `@agent(transport="acp")` spawns any agent speaking the
  Agent Client Protocol, not only OpenCode. wfpy discovers `opencode`,
  `claude` (Zed's `claude-agent-acp`), `codex` and `gemini` on the PATH; a
  user's `~/.config/wfpy/connectors.toml` and a project's
  `.wfpy/connectors.toml` add or override them (command, model, mode, env).
  An agent names one with `connector=`, a run's default is
  `--acp-connector`, and `wfpy connectors` lists what a workspace sees. The
  session's model and mode are set where the agent offers them, a stateful
  agent's session is resumed through `session/load`, the `ask_user` tool
  reaches the user over MCP, and permission requests go to the user only when
  `ask_permissions` asks for it.
- A firing records the agent session it talked to -- its id, the connector
  and the directory -- on the `agent.message.end` run event and as
  `agentSession` on the agent's reply, so an IDE can open it again in the
  agent's own CLI (`claude --resume`, `opencode -s`).
- The StreamBlocks node: a CalPy network as a node of a workflow, declared
  external, run through `calpy run` with the same environment a tool gets,
  its build collected from where calpy leaves it. The sidecar creates the type
  from the diagram.
- `@source` — a node holding one resource (file, folder or web resource) that
  emits it once and opens it on double-click. The mirror of `@viewer`: a viewer
  is a sink you can open, a source is a producer you can open. The emit-once
  action is generated, because a source has no inputs and so nothing in the
  wiring can start or stop it; a class that declares its own actions keeps them.
  `path` is a task parameter, so one class serves many nodes and each carries
  its own resource.
- Workflow instances: `child(instance="wf-out/<run>")` nests a workflow that
  already ran and hands on the outputs its `run.wf-run.json` recorded, once,
  without running it again — a source made of a past run. Only a workflow with
  no input ports can be one. A relative path is resolved against the file that
  writes it. The sidecar's `listWorkflowInstances` lists the runs in a project
  that qualify, `createNode` takes `importFrom` for a workflow defined in
  another module, and the graph export marks an instance with an `instance`
  annotation.
- A firing that fails gives back the tokens it took: each goes back to the
  head of its queue, in order, for an action, a tool, an agent and a
  StreamBlocks instance. A nested workflow and an if / loop are made of other
  actors' firings, each atomic on its own. A failed run's queues are
  left as they were before the failing firing, the first step towards resuming
  a run (`docs/proposals/resume.md`).
- Resuming a run that failed: a failed or stopped run writes
  `run.wf-checkpoint.json` (every queue's tokens, each actor's state, each
  if / loop's progress, the shared context, recursively for nested workflows),
  and `wfpy run flow.py --resume-from wf-out/<run>` (or
  `run(..., resume_from=...)`) builds the plan from the current source,
  restores that state and carries on. The firings before the failure do not
  run again. A firing interrupted inside a nested workflow, an if branch or a
  loop body is finished, not restarted. A changed action body is expected (it is
  the fix); a changed graph is refused. Values are written as tagged JSON: an
  object of the user's own classes by its class name and attributes (so it
  survives the class's code changing), pickle only for what attributes do not
  describe. A value neither can write (a lock, an open file, a local class)
  makes the checkpoint not resumable, naming it. The resumed run records
  `resumedFrom`.
- Resuming a run at any step of its queue trace:
  `wfpy run flow.py --resume-from wf-out/<run> --at-step N`. Every firing is
  journalled in `run.wf-journal.jsonl` (what it took, by digest, what it put,
  and the actor's state after). A resume at step N starts again from the run's
  inputs and replays each actor's firings up to the step instead of running
  them, then carries on live. Replaying per actor is sound with parallel
  workers, where restoring a snapshot taken at a step is not. A replayed firing
  whose inputs differ from the original's stops the replay (`replayStopped` in
  the run record). Trace steps carry `journalSeq` and `replayed`, and the queue
  trace is now written when a run fails too.
  In wfpy-ide, the queue-trace stepper's ⟲ resumes a run at the step it
  shows, and a node's "Rerun from Here" resumes it before that node's last
  firing.

- `wfpy plan <file> --format graph --hierarchy`: the root workflow's graph plus
  `hierarchy`, a tree of every nested workflow instance at its path from the
  root, each with its own graph, the file defining it, its node in the parent's
  graph and its node count. Each nested graph is elaborated from the instance
  its parent built, factory parameters included, as a run elaborates it. An IDE
  navigating the hierarchy in one editor reads every view, and its outline,
  from one export.

### Fixed
- An ACP session gets an absolute working directory (`session/new` refuses a
  relative one), and the failure of an ACP agent other than OpenCode is the
  firing's error rather than a fall back to OpenCode's CLI.
- The native MCP client works with `mcp` 2 as well as 1.
- A failing tool says why: the tail of what it printed is kept in its error.
- A run's error names the actor that failed by its instance path under every
  nested workflow (`error.entityInstancePath`, e.g. `["m", "i", "x"]`, in the
  run record and the overlay). It used to name the last actor the overlay saw
  become active: no path, and with parallel workers not necessarily the actor
  that failed.
- Each token that goes to `work/edge-tokens/` gets its own file
  (`<queue>__<n>.json`), instead of one file per queue rewritten by every
  token. The queue trace's last token at an earlier step, which the IDE's
  stepper opens, read as the run's final token.
- A run whose tokens are arbitrary objects no longer fails writing its overlay
  or run record: an object those files cannot otherwise show is written as its
  `repr` (a dataclass as its fields).

## [1.0.0] — 2026-07-24

First stable release.

### Added
- A tutorial-ordered example suite (`examples/01`–`12`) that builds the dataflow
  model from simple tasks through schedules, priorities, networks, and
  multi-agent orchestration (fan-out/reduce, routing, repair loops). Every
  example runs offline and is exercised by the test suite, alongside a
  `docs/dataflow-concepts.md` reference.
- GitHub Actions CI (pytest across Python 3.10–3.13 + strict mypy) and a
  tag-triggered release workflow that builds and publishes the wheel to GitHub
  Releases.
- `@agent(transport="mock")` (alias `"offline"`): runs an agent actor with no
  model, no network call and no credentials. The actor fires under the normal
  dataflow rules and emits a value on every declared output port, so a graph's
  wiring — fan-out and join, repair feedback loops, guard routing, firing order
  — is exercisable in CI without spending tokens or pinning behaviour to a
  model's wording. Values come from `mock_outputs={port: value}` (camelCase
  `mockOutputs`) when given, and are otherwise synthesized from each port's
  declared type, so an `int` port receives an `int` and a `File` port is
  materialized as a real file.
- `@agent(ask_user=True)` (camelCase `askUser`): HTTP-transport agents can pause
  mid-firing and ask the user a question via the builtin `ask_user` tool, then
  continue with the reply folded into their context. The answer source resolves
  to an explicit `run(elicitation_handler=...)` callback, a console prompt
  (default when a TTY is detected or `--interactive`), or a graceful
  non-interactive fallback. New `wfpy run` flags: `--interactive` /
  `--no-interactive`, `--elicit-timeout-ms`, `--elicit-default`,
  `--elicit-require`. The `ask_user` tool is exempt from the `agent_tool_auth`
  gate and never exposes the `python`/MCP tools. Surfaced to the IDE via the
  graph export (`askUser`) and run artifacts.

### Changed
- Project description and the built-in `@agent` runtime prompt no longer
  reference "WorkflowLang"; wfpy is described as "Pythonic agentic workflows on
  actor-dataflow semantics". Agents are now told "You are executing a wfpy
  @agent task."

### Fixed
- `@agent(prompt=...)` with no `skill=` raised `ValueError: Agent has
  use_skill=true but no skill name configured` at run time, which made the
  simplest form of the decorator unusable — `use_skill` defaults to `True`, and
  every prior workflow happened to configure a skill. It now means "include the
  configured skill", so an agent with no skill named simply has none to include.
  A skill that *is* named but cannot be read still raises.

## [1.0.0b1] — 2026-07-06

First public beta.

### Added
- `@task` / `@action` / `@guard` / `@workflow` decorators with typed `Port`s and
  FIFO round-robin dataflow scheduling.
- `@agent` LLM tasks: HTTP API transport plus CLI transports (`opencode-cli`,
  `claude-cli`, `codex-cli`) and the ACP session transport (`opencode-acp`).
  Skills, MCP/LSP integration, output validators, context budgets and
  truncation strategies, stateful sessions.
- `@tool` external command tasks and `@config` environment injection.
- `wfpy` CLI: `run`, `plan` (graph export), viewers and run artifacts
  (`run.wf-run.json`, `run.wf-viewer.live.json`, `run.wf-agent-context.live.json`).
- `wfpy-sidecar`: libcst-based source-rewrite service for diagram editing
  (sidecar contract v2: capabilities discovery, canonical op aliases,
  structured errors, `checkConnection`).
- Live run observation: `RunEventStream` — a per-run localhost SSE endpoint
  streaming agent message deltas, reasoning, and tool calls, advertised via
  `run.wf-stream.live.json` (consumed read-only by the Workflow IDE).
- Graph export with stable element ids and source locations.

### Notes
- `pip install "wfpy[agent]"` is required for `@agent` workflows
  (`httpx`, `mcp`, `agent-client-protocol`).
