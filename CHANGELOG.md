# Changelog

All notable changes to wfpy are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and wfpy uses
[PEP 440](https://peps.python.org/pep-0440/) versioning.

## [Unreleased]

### Fixed
- The `claude-cli` and `opencode-cli` transports give their agent no stdin.
  `claude --print` and `opencode run` read a stdin that is not a terminal to
  its end before they answer, and `claude` adds what it read to the prompt.
  wfpy passed its own stdin on, so a run started by a parent that keeps the
  pipe open (an IDE, a supervisor, `something | wfpy run`) waited for that
  pipe on every firing, up to the agent's timeout, and what was piped to wfpy
  became part of the question. Measured with a line and a pipe held open for
  25 s: `claude-cli` answered after 32.2 s, and with the word the line asked
  for; `opencode-cli` after 35.1 s. Both now answer as with stdin closed
  (2.5 s and 6.7 s). The `run --help` that wfpy reads from OpenCode is given
  no stdin either.

## [1.4.1] — 2026-11-24

One deprecation and the fix that led to it (#94). A port declared without
`direction="in"` or `direction="out"` still works and is now warned of where
it is declared; wfpy 2.0 will refuse it. The sidecar, which took such a port
for an input whatever its name, reads it as the runtime does. Nothing that
ran stops running. Read the Deprecated entry before running a suite with
warnings turned into errors.

### Deprecated
- A port declared without `direction="in"` or `direction="out"`:
  `Port[T]()`, the bare `Port[T]`, `direction="inout"`, and any other value.
  Nothing in the runtime treats such a port as two-way. Its side is read from
  its name (`out`, `output`, `result`, `report` and `summary` are outputs,
  every other name is an input), so `Verdict = Port[str]()` is an input with
  nothing to say so. wfpy now warns where such a port is declared, with the
  class, the port and the direction to write to keep what the workflow does:
  a `FutureWarning`, since Python hides a `DeprecationWarning` of a file that
  `wfpy run` loads. Every workflow that ran still runs and gives what it
  gave; only a run that turns warnings into errors (`python -W error`) now
  stops at such a port. wfpy 2.0 will refuse it. The sidecar's graph
  (`wfpy.exportWorkflowGraph`) says the same on each node of a class that
  declares one: a `metadata.diagnostics` entry of severity `warning` and code
  `port_without_direction`, with the runtime's text and the place of the
  declaration. The node is not `isErrored` and the graph is not `partial`.
  The graph says it for the classes of tasks; a workflow written as a class
  is warned of by the runtime alone, and with `inputs=` or `outputs=` the
  direction it is told to write is the one they give. The README's minimal
  example, which was written with two such ports, now states them.

### Changed
- The sidecar op `createEntityPort` asked for the direction `inout` writes
  the side the runtime would take the port for by its name
  (`direction="out"` for `Summary`, `direction="in"` for `Extra`), and no
  longer `direction="inout"`, which is now deprecated.

### Fixed
- The sidecar took a port declared without a direction for an input
  whatever its name, where the runtime takes one named `out`, `output`,
  `result`, `report` or `summary` for an output. A connection out of one
  (`connect(a.Out, b.In)` with `Out = Port[str]()`), which runs, was refused
  by `checkConnection` and by `connect` with `invalid_source_direction`, and
  its edge in the graph of `wfpy.exportWorkflowGraph` was flagged as an
  error, which also made that graph `partial`. For a connection such a port
  now has the side its name gives: the side the runtime takes it for, the
  side the graph draws it on, and the direction the new warning says to
  write. A port's own name counts (`Port[str]("Result")` is an output). A
  port that is an input by its name is still refused as a source, and an
  output as a target, as a port that states its direction is.
- A direction written by a name (`direction=OUT`) was read by the sidecar
  as no valid direction, and a connection out of such a port was refused.
  The name is not followed, so no connection is refused for that port.

## [1.4.0] — 2026-11-24

A workflow file has a diagram while it is broken. The sidecar's static graph
has ports and the workflow's own inputs and outputs, a file with a syntax
fault is drawn from a repaired copy of its text, and `wfpy plan --format graph
--best-effort` draws a file it cannot import, with the error on the nodes it
belongs to. Two of these change what a reader of the graphs meets, and both
are under Changed: the static graph lists `wf-input` and `wf-output` nodes
before the instances, and the `plan` command answers a file that does not
import with exit code 0 and a `partial` document, where it ended on the
traceback. The release also has the sidecar op `reorderEntityPorts`, the
reasoning level of an agent over ACP, and the fixes made since 1.3.2 to the
edits an IDE writes through the sidecar and to OpenCode 2.

### Added
- The sidecar op `reorderEntityPorts` (`RewriteEngine.reorder_entity_ports`):
  a definition's ports in a given order, written into its source, for an
  IDE that lays a diagram out with its ports reordered against crossings.
  Ports are named as the diagram names them (`Port[int]("Output")` is
  `Output`). A class's port lines in `class Ports` move, each with the
  comment directly above it and the one after it; every other line (a
  docstring, a constant, a section's header comment, blank lines) stays
  where it is. A `@workflow def`'s `inputs=` and `outputs=` dict entries
  move in the same way, the commas and line breaks staying in place. An
  order that leaves out a port, names one the definition does not have, or
  names one twice is refused, as are ports sharing a line or declared twice
  and a dict comment that would have no line to go to.
- The reasoning level over ACP: an agent's `reasoningEffort` (or
  `variant`) is set on the session where the agent offers a config option
  of category `thought_level` (OpenCode 2's `effort`: minimal, low, medium,
  high, xhigh, max), matched by value then name. The level is looked up in
  the options the agent sends back after the model is set, since a model can
  bring its own levels. An agent that offers none logs it and runs at its
  own level, as before. Claude Code's connector offers one too
  (`claude-agent-acp` 0.108.0: default, low, medium, high, xhigh, max), so a
  level on a Claude Code agent, which was ignored, is now applied.

### Changed
- The static graph of a file (`wfpy.exportWorkflowGraph`, which reads the
  file and runs nothing) is one a reader can draw. It gave the nodes of a
  workflow and the edges between them, but no port for an edge to end on:
  an IDE that falls back on it when the file does not import (a misspelt
  name, a failing import) drew the nodes and nothing else. Three things
  are added, and the keys it had are as they were:
  - Each node has `ports` (`id`, `name`, `direction` of `in` or `out`,
    `type`, `role`, and the `location` it is declared at). They are read
    from the `class Ports:` of its class, followed through the project's
    imports and to a base class, from the `inputs` and `outputs` of a
    nested workflow (of the one a factory builds for `flow = make()()`),
    and from wfpy for `loop` and `if_`. A direction the source leaves open
    is settled by name, as the runtime settles it. A port a connection
    names that the class does not declare is there too, so its edge has an
    end. The `id` of a port is the `portId` the edges already carry.
  - The workflow's own inputs and outputs are nodes, as in the graph
    `wfpy plan --format graph` gives: `wf:input:<name>` of kind `wf-input`
    and `wf:output:<name>` of kind `wf-output`, one port each, every
    declared one and any a connection only names. They are listed first,
    inputs by name then outputs by name, as that graph lists them. A reader
    that took every entry of `nodes` for an instance now meets these. A
    name the workflow assigns (`src = a.Out`) is a local and makes no such
    node. An input and an output of one name share the port id `wf:<name>`,
    the `portId` the edges had: the side of the edge end tells them apart.
  - A `loop` or `if_` node has the `label` the runtime names it by (`loop`,
    `loop_1`, `if`); its `id` stays the name the source binds it to.
  On every tutorial and advanced example the static graph is now the graph
  the runtime builds: the same nodes, the same ports with their direction
  and type, the same edges. What is not done: a node a workflow makes in a
  Python loop or a comprehension is still not seen, and a port is not
  judged, so one a class does not declare is drawn and not flagged.
- A node written in a tuple assignment (`author, reader = Author(),
  Reader()`) is a node of the static graph. It was not seen: the graph of
  such a workflow had no nodes, and `connect` and `checkConnection`
  answered `unknown node` for a name the workflow binds.
- A file with a syntax fault has a static graph. Neither `ast` nor libcst
  gives a tree for a file with one bracket left open, so
  `wfpy.exportWorkflowGraph` answered with no nodes and no edges, and the
  diagram of a file under edit went blank at each keystroke that left it
  unparsable. The export now draws from a repaired copy of the text
  (`wfpy._source_repair`): each statement Python refuses is replaced, the
  rest is the file's own, and every line keeps its number. The graph is
  `partial`, and `errors` has a `syntax_error` for each fault with the
  message, the line and the column Python gives. A node or an edge written
  on a faulty line is still drawn where the start of its statement can be
  read (`scale_a = Scale(scale=2` is the node `scale_a` of `Scale`,
  `connect(a.Out b.In)` is the edge) and is flagged with `isErrored` and
  `errorMessage`, a node under `metadata` and an edge under `meta`. The
  copy is for drawing: nothing is run, the file is not written, and every
  other op still refuses a file that does not parse. What is not done: a
  string left open in a file that holds a workflow as text can let lines of
  that text be read as code.
- `wfpy plan --format graph --best-effort` draws a file it cannot import.
  The command imports the file before anything else, and that import was
  outside what `--best-effort` covers: a misspelt name, a failing import or
  a syntax fault ended it on the traceback, with no graph. Such a file is
  now drawn from the source. The static reading of the file goes through
  the runtime's own exporter, so the document has the ids, the ports, the
  edges and the scopes of the one the runtime gives when the file does
  import (held on every tutorial and advanced example), and it is `partial`
  with the error in `errors`: the exception at the last line of the file it
  went through, or each syntax fault at its line and column. The document
  follows the runtime where a file departs from the plain case: a port goes
  by the name it is declared with (`Port[T](name="...")`), a direction left
  open is `inout`, a `@source` or `@streamblocks` node has its kind, a name
  bound in both branches of an `if_` is two nodes, and with no `--workflow`
  the workflow drawn is the one the command takes when the file imports.
  `--workflow` with a name the file has nowhere is `not found`, as it is for
  a file that imports. Without `--best-effort` the command fails as it did.
  What is not the runtime's: a node's parameters are not in its `meta`, and a
  class defined on both sides of an `if` is read as the one written last.
- An error is on the nodes it belongs to, in the static graph of the
  sidecar and in the document of `wfpy plan`. An error in the body of a task
  class (a misspelt name in a port, a syntax fault in an action) flags every
  node of that class; one on a statement of the workflow flags the node or
  the edge that statement makes; one elsewhere flags nothing and stays the
  file's. A flag is `isErrored` and `errorMessage` (under `meta` in the
  document, under `metadata` for a node of the sidecar's graph). The error
  is reported once, in `errors`, at its own line: a flag is no second
  report. A syntax fault in a task class of another file is said the same
  way on the graph of the workflow that makes nodes of it: the graph is
  `partial`, the error has the path and the line of that file, and the nodes
  of the class are flagged. The class is read from the repaired text of its
  file, so without this the drawing looked whole with a port missing.

### Fixed
- The `syntax_error` of a static graph is at the line of the fault. It was
  at the line libcst stops at, which is one or two lines further on (line
  65 for a bracket left open on line 63), and its message was libcst's list
  of the tokens it expected. It is now the line, the column and the message
  of `python file.py`.
- A syntax fault in the file of a nested workflow no longer takes the
  diagram of the workflow that uses it. The parent's graph came back empty,
  with the fault reported at a line of the parent file. The parent is now
  drawn whole, and the nested graph under `children` is the one that is
  `partial` and carries the error, with its own file's path.
- An IDE that writes `@agent`'s `mcpServers` back no longer loses what the
  file configures. The graph export gives the servers configured on an
  agent (`mcpServerConfigs`) as values, and their env by name only
  (`envKeys`: env values are often secrets, and never leave the process;
  args and url are given as they are). On an annotation merge the update
  is the agent's whole list of servers, each once, known by name. One
  written as its name is the file's dict for it, as written (what it
  computes included). One written as a dict (the fields the IDE changed)
  is the file's dict with those values in it, laid out as it was, every
  other entry kept, `env` among them. A dict for a name the file does not
  configure is a new server (a renamed one is one); an `envKeys` is not
  written, and a dict with no name is refused. Servers given as a literal
  `mcpServerConfigs` move into `mcpServers` and the argument goes (one the
  update leaves out is left out); a computed one stays as written, and a
  server written as a dict that the file does not configure is refused
  while it does (it may be one of those, edited). Refused, not overwritten:
  a `mcpServers` the file computes, or a server in it; a file that gives
  an argument under both its spellings, or configures one server twice (the
  run takes one of them); a write of `mcpServerConfigs`.
  An agent written `@wfpy.agent(...)` is treated the same, under either
  name the merge finds it by. An MCP server's args are exported as text:
  one that is not a string (a `Path`) made the whole export fail.
- An `@agent` given no reasoning level had the text `"None"` as its level
  (`str(None)`): `opencode-cli` was started with `--variant None`, and the
  graph export showed `reasoningEffort="None"`, which an IDE wrote back into
  the file. No level is now no level, and so is a level written `"None"`
  (that spelling: `"none"` is a level some models take).
- The graph export carries `connector`, `mode` and `askPermissions` when the
  decorator sets them, so an IDE shows an ACP agent's settings as written;
  `usePrompt` is false for a prompt the agent is told not to use
  (`use_prompt=False`). `@agent` takes `askPermissions`, the camelCase
  spelling the export uses, beside `ask_permissions`.
- Merging arguments into a decorator (`mergeDefinitionAnnotationArgs`, an
  IDE's Save):
  - matches an argument whatever its spelling: `useSkill` replaces
    `use_skill` and keeps the file's spelling and place, a second spelling
    of the argument replaced is removed, and on `@agent` `reasoningEffort`
    is `variant`; an argument neither updated nor removed stays as written,
    and one argument given twice under two spellings is refused;
  - takes `argRemovals`, the arguments to take out: a list of names, and
    anything else is refused (a bare string would be read letter by
    letter). A name given to update and to remove is updated;
  - lays appended arguments out as the call is written: one per line and
    indented as the others, the closing parenthesis where it was, and each
    comment, on an argument's line or on the lines above it, staying with
    that argument (or going with it when it is removed), in a call whose
    commas open the lines as in one whose commas end them. The argument
    after a removed one that opened a line takes its place there. A call
    left with no argument is `@agent()` (its comments kept on lines of
    their own), not a bare `@agent`; a bare `@agent` with nothing to add is
    left alone. The sidecar declares `features.mergeArgRemovals`.
- OpenCode 2 over ACP: an agent's `mode` is applied again. OpenCode 2
  offers the mode only as a session config option (`build`, `plan`), not
  in the session's `modes`, so a `mode="plan"` agent ran in `build` with a
  warning that no mode was offered. The ACP client now sets a mode through
  the config option with id or category `mode` where the agent offers one,
  and through `session/set_mode` otherwise (Claude Code); either way the
  mode is matched case-insensitively.
- OpenCode 2 over the CLI (`opencode-cli`): `opencode run` dropped
  `--variant`, so every agent with a reasoning level failed with
  "Unrecognized flag: --variant". wfpy reads whether `run --help` offers the
  flag, with the environment the run gets (the workflow's `@config` env
  included), once per command and PATH; a help that cannot be read is read
  again at the next firing. Where the flag is gone, the level goes in the
  model, `provider/model#variant`. An agent that names a level but no model,
  or a model that names another level, gets a warning and
  `opencodeVariantDropped` in its debug record.
- A decorator written without parentheses keeps its comments when a merge
  (`mergeDefinitionAnnotationArgs`) gives it its first argument (#82). The
  merge wrote a new decorator in place of `@agent  # note`, and the comment
  on its line and the comment lines above it were lost. The arguments now
  go into the decorator that is there, laid out as in an empty `@agent()`:
  on one line, where two arguments given to a bare `@agent` were written
  one per line.
- The merge finds a decorator written with its module, and a decorator on a
  function (#83). `@wfpy.agent(...)` on a class, and `@workflow(...)` or
  `@config(...)` on a `def`, answered `Definition not found for annotation
  merge`. A decorator is now matched by its name, with `annotationName`
  given as `agent` or as `wfpy.agent`, on the class or the function that
  `entityType` names. What is written keeps the form the file uses, and a
  file that writes `@wfpy.agent` is not given a `from wfpy import agent`.
  `removeDefinitionAnnotation` finds a decorator the same way. A method is
  not found by either: `entityType` names no class, and two classes may
  each have a method of that name. `updateDefinitionAnnotation` is not
  changed: it matches a decorator written as a bare name on a class.
- An empty decorator call written over lines (`@agent(` and `)` on two
  lines) gets its first arguments one per line (#84). The first argument
  was written at the decorator's column, with the closing parenthesis right
  after it. Each argument is now on a line of its own, indented as the
  call's comment lines are (four spaces when it has none), with a comma
  after the last, and the closing parenthesis stays on its line. A comment
  on the opening line stays there, and comment lines inside the call stay
  above the first argument.
- An edit that writes a text an IDE gives as a value wrote any Python
  expression as given, also one that is not one value where it is written
  (#81). `mcpServers="a", "b"` and `path=*y` left a file that no longer
  parsed, with the sidecar answering `ok`; `wfpy.loop("a", "b")` and
  `model="m", *rest` parsed, with an argument nobody asked for. A tuple
  written without its parentheses is now written with them
  (`mcpServers=("a", "b")`, and `("a",)` for `"a",`), and a text that is not
  one value with them either (`*y`) is refused with `invalid_expression`.
  This holds for a node's parameter (`updateNodeParameter`, the
  `expressions` of `createNode`), a decorator's argument
  (`mergeDefinitionAnnotationArgs`) and the decorator itself
  (`updateDefinitionAnnotation`), the two ends of a `connect`, the condition
  of an `if_`, what a `loop` iterates over, and the type of a workflow's
  port (`createPort`). The type of a class's port, inside `Port[...]`, is
  written as before: a tuple needs no parentheses there. The merge also
  refuses an argument whose name is a keyword or not an identifier
  (`class=1`), with `invalid_argument_name`.
- Merging arguments into a decorator refuses an argument that one of wfpy's
  own decorators does not take (#85), with `unknown_argument` and a message
  that names the argument and the decorator. The graph export lists
  `useLsp`, `useMcp` and `lspServerConfigs` among an `@agent`'s arguments,
  for an IDE to show, and the merge wrote any name it was given: sent back,
  each left a file that failed as it was imported (`agent() got an
  unexpected keyword argument`). The names a decorator takes are read from
  its signature. A name that is another spelling of an argument the
  decorator takes is that argument: the export names `@tool`'s
  `inherit_stdio` `inheritStdio`, and a new argument given so was written
  as given, which `tool()` refuses. It is now written `inherit_stdio`; an
  argument in the file keeps its spelling, unless the decorator refuses
  that one. The decorator is asked as the file writes it: `@agent` where
  the file imports it from wfpy or binds it not at all, `@wfpy.agent`
  where `wfpy` is the module (`import wfpy`, `import wfpy as wf`). One
  that the file defines, or takes from elsewhere (`@mylib.agent`), is not
  asked, nor would one that takes `**kwargs` be. A name in `argRemovals`
  is not asked either: taking out an argument the decorator refuses
  repairs the file. The export is unchanged, and still does not say which
  of its entries are for display only.

## [1.3.2] — 2026-11-23

One fix, to what decides that a port carries a file (#75), and a folder of
advanced examples. The fix changes which ports are file ports: read its
second paragraph before upgrading a graph whose token types are files by
their name only.

### Added
- Advanced examples (`examples/advanced/`): workflows that write workflows.
  An `@agent` emits a workflow file as a token on a `File` port, and a `@tool`
  node of the same graph runs it with `wfpy run`. Five examples: the pattern
  in three nodes, a recursion down to a base case, a generated graph that
  follows the folder it was written for, the pattern as a sub-workflow that a
  generated workflow may use in turn (so the depth of the runs follows a
  folder tree), and a repair loop in which a `@task` runs the candidate and a
  failed one goes back to an agent. They run offline by default and are
  executed by `tests/test_examples_advanced.py`; three of them take a real
  agent through `WFPY_EXAMPLE_TRANSPORT=acp`. Nothing in wfpy changed for
  them, and the folder's README says what the pattern leaves to the author of
  the graph: the generated file runs with the user's rights, a recursion has
  no bound of its own, and the parent run does not see inside the child.

### Fixed
- A port is a `File`/`Resource` port by its type, and no longer by the
  spelling of its type (#75). The test that every node kind asks before it
  materializes a token ended with a search for `file` or `resource` in the
  printed type. Thus a dataclass named `ProfileRun`, `FileSpec` or
  `ResourceUsage`, and any class defined in a module named `resources` or
  `profile`, was taken for a file: its token left a nested workflow as the
  path of a file that was never written, a run returned that path in place of
  the object, and the dict an agent replied on such a port was written to a
  `.txt` file. An edge between two tasks was not affected. The test now reads
  the type: `File`, `Resource`, a subclass or an instance (`File(ext=".md")`),
  and what stands for one of them: either name written as a string,
  `Optional[...]`, `... | None`, `Annotated[...]`, a `NewType` and a `type`
  alias.

  Whatever was a file port through its spelling alone is no longer one. This
  includes three forms that do name `File`: a container (`list[File]`), a
  union with another type (`File | str`), and a string that is an expression
  (`"File | None"`, which is not parsed). It also includes a name of one's
  own, such as `NewType("MlirFile", str)`: declare the port as `File` to keep
  it a file port. On a port that is no longer a file port a value is handed
  on as it is: the reply of an agent is not written to a file, and a path
  that a task returns is not copied into the work folder. A workflow output
  that is the path of an existing file is still copied beside the run record,
  as for a port of any type.

## [1.3.1] — 2026-11-20

One fix, to what a run writes about the edges of nested workflows (the run
overlay and the queue trace). It is the whole of this release.

### Fixed
- The run overlay (`run.wf-viewer.json`) has one entry per edge of a
  workflow with nested workflows. It keyed an edge by its name within its
  workflow (`queue:p.Out-->WF.Out`), so two nested workflows that connect
  the same actor names (two instances of one workflow, above all) shared
  one entry: the one merged last replaced the others, and an IDE showed on
  every edge of the first instance the token of the second. A nested edge's
  key now carries the instance path of the workflow it is in
  (`queue:a/i/p.Out-->WF.Out`), and its entry says the path as
  `instancePath` (`["a", "i"]`). The queue trace's entries
  (`run.wf-queues.json`) say it too, beside `scope`, the workflow's name,
  which two instances share. The top level's keys and entries are as
  before, so a flat workflow's overlay does not change. An IDE that matches
  an entry to an edge by its endpoints alone now finds one per instance,
  and chooses by `instancePath` the one of the workflow it shows.

## [1.3.0] — 2026-11-20

wfpy ships four Agent Skills for building a workflow with it, and `wfpy skills
install` links them where an agent tool looks for them. They were merged after
1.2.0 was cut, and are the whole of this release.

### Added
- wfpy ships Agent Skills for building a wfpy workflow, and `wfpy skills
  install` links them where a tool looks for them. Four skills:
  `wfpy-workflow-builder` (the entry point, and the project layout a new
  project starts from), `wfpy-actor-authoring` (one node: its fields, ports
  and actions, and the `Schedule` and `Priority` that say which action
  fires), `wfpy-graph-composition` (wiring, nesting, and a loop that
  terminates) and `wfpy-agent-nodes` (an agent in the graph, and the skill
  loaded into it). They are here rather than in a package of their own
  because they describe this code, down to behaviour that changed in a given
  release; a skill that versions separately from what it documents drifts.
  What each says wfpy does is run by `tests/test_shipped_skill_claims.py`,
  so a change that makes one wrong fails the suite; what needs a real agent
  (a connector's model names, the lines an ACP turn logs) is not run there.
  `install`, `path` and `list` are taken in place of the workflow file of
  `wfpy skills <workflow.py>`, which keeps listing what a run finds; a file
  of one of those names is written `./install`, and an option of one form
  given with the other is refused. `wfpy skills install` links rather than
  copies, so upgrading wfpy updates the skills in place: `--scope user` (the
  default) links into `~/.claude/skills`, the user-level folder a run
  searches after a project's own and the one the Claude tools read, so one
  link serves a workflow and an editor alike, and `--scope project` into
  `<workspace>/.wf/skills`. A link that points somewhere else is kept unless
  `--force` says otherwise, one that points at nothing (its environment is
  gone) is said to, and a real directory of that name is never deleted, with
  or without `--force`. `wfpy skills path` prints the directory to use as
  `WF_SKILLS_ROOT`, and `wfpy skills list` says what is shipped.

## [1.2.0] — 2026-11-20

A skill is found where the other agent tools install it (Codex, OpenCode,
`.agents/skills`), and `wfpy skills <workflow.py>` lists what a run of that
workflow finds. The `agent` extra runs on `agent-client-protocol` 1.0. Two of
the fixes are to runs that went wrong with no error: an agent with no input
port fired without end, and a parallel run could end with a token an actor
could have taken.

`wfpy.__version__` is the installed version. It was a constant that no release
had updated, and said `0.1.0` in the 1.1.0 wheel.

### Added
- A skill-hook policy entry may name the skill folder it approves:
  `{"skill": "review", "hook": "pre", "interpreter": "sh", "dir": "<path>"}`,
  `interpreter` optional, `dir` read from the policy file's folder (a link
  to the file followed) with `~` expanded and compared, links resolved,
  with the folder the run resolved the name to. An entry `review:pre`
  approves the hook of whichever `review` a run finds, and which one that
  is depends on the working directory, the variables and what a cloned
  repository holds: a repository's own `.claude/skills/review` ran its
  `scripts/pre.sh` under an approval given for another folder. Text
  entries keep approving by name, read as before; `"requireDir": true`
  makes them, and object entries without `dir`, count for nothing. An
  object entry wfpy cannot read (a key it does not take, a value that is
  not text) approves nothing, not the hook by name, and a refusal lists
  it, as the log does. Both hooks of a firing run from the folder its skill was read from:
  the post hook resolved the name again after the agent's turn, which could
  have made another folder where the name resolves first. The reason a hook
  ran or was refused names its skill folder, and the hook's debug record
  gives it as `skillDir`. The README now describes hooks and the policy
  file.
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
  no git, the project is the working directory or the workflow's directory
  (see Changed). So `/tmp/.agents/skills` is not read for a workflow in
  `/tmp/job` started elsewhere, whoever planted a `/tmp/.git`.
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

### Changed
- wfpy's own project folders, `.wf/skills`, `.claude/skills` and
  `.claude/agents`, are searched up the tree only as far as the top of the
  project, as the other agent tools' folders are. They were searched up to
  the filesystem root, so a `/tmp/.claude/skills` another local user planted
  handed a workflow in `/tmp/job` its prompt text and hook scripts. The
  project is the workflow's git repository, as git says (a submodule or a
  nested repository is a project of its own); with no repository git
  accepts, the working directory when the workflow's directory is in it,
  else the workflow's directory alone. The other tools' folders follow the
  same rule, so outside a repository a run started in a directory that
  holds the workflow searches them up to that directory, where it searched
  the workflow's directory alone. The working directory's own folders, the
  user's and what `WF_SKILLS_ROOT` and `WF_CLAUDE_AGENTS_ROOT` name are read
  as before, and a name resolves to the folder it did when that folder is
  in the project. When the runtime root (the first `WF_SKILLS_ROOT`
  directory that exists, or `WF_CLAUDE_AGENTS_ROOT` for presets) is outside
  the project, the walk from it is that directory alone: its own
  `.wf/skills`, `.claude/skills` or `.claude/agents` are read, the
  directories above it are not. **This changes what a run finds:** a skill
  or preset kept above the top of the workflow's repository (a
  superproject's, for a submodule), or above the workflow's directory in a
  project with no `.git` (an unpacked archive) that the run is not started
  in, is no longer found; name its folder in `WF_SKILLS_ROOT` (or
  `WF_CLAUDE_AGENTS_ROOT`). For a workflow in a project below the home
  directory, `~/.claude/skills` and `~/.claude/agents` come after the
  working directory's folders, as the documented order has them; the walk
  reached them first. `~/.wf/skills` is reached by no other rule — the
  user's folders are `~/.claude/skills` and `~/.claude/agents` — so a
  skill kept there is now found only by a run whose project is the home
  directory; name it in `WF_SKILLS_ROOT`, or keep it in
  `~/.claude/skills`. Which repository a directory is in is git's answer:
  where git cannot be run (an image without it) or refuses the checkout
  (another user's, not in `safe.directory`), there is no repository to
  bound the search, so the folders of a checkout's top are not searched
  and the log says so. `wfpy skills` lists the folders a run searches, its
  answer made as before.

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
  Codex's `.system` root for a skill. A preset that preloads such a skill
  is warned about it, as one that preloads a missing skill is, and a hook
  declared with such a name does not run. A name is also refused when it
  begins or ends with whitespace, since a run reads one without it and so
  can never resolve that folder. A hook's script is held inside the
  skill's `scripts/`, as a skill is held inside its root: what validation
  leaves is a Windows drive-relative name (`d:evil`), which joined outside
  the folder. The list leaves such names out by the same
  check, so what it offers and what a run takes cannot differ.
- A parallel run does not end with a token an actor could have taken. An
  actor that does not fire is held until another makes progress, and "since
  when" was read when its answer was collected, not when it was asked.
  Progress made in between was lost: a task that looked at an empty queue
  while its producer was firing was never asked again, and the run reached
  quiescence with no error. The race has been there since 1.0.0b1 and shows
  on a loaded machine. A sequential run (`max_workers=1`) was not affected.
- An `@agent` that declares no input port fires once in a run, and
  `fireable_without_input=N` in its decorator makes that N times. Such an
  agent fired without end, whatever the decorator said: "a token on every
  input port" holds at every step of an agent that has none, so its count
  was never read. A run with one did not end, and on a real transport each
  firing was a model turn. The count is for the run: the agent fires once
  inside a child workflow or a loop body that several tokens pass through,
  and a resume does not give the firing again. On such an agent the count
  is 1 or more: `fireable_without_input=0` is refused when the class is
  made (`TypeError`), since the agent would never fire. An agent that has
  input ports is unchanged, and may say 0.

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
