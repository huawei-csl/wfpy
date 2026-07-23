# Security

## Trust model — `@agent` tasks run autonomously with your privileges

An `@agent` task delegates work to an LLM backend (an HTTP API, a local CLI such
as opencode/claude/codex, or an ACP session). **When you run a workflow that
contains `@agent` nodes, that backend acts autonomously with the full privileges
of the user running the workflow.** Specifically:

- The agent's tool-use / permission requests over the ACP transport are
  **auto-approved** (`wfpy.acp_client.SimpleClient.request_permission`), and the
  agent may **read and write arbitrary files** the user can access
  (`read_text_file` / `write_text_file`).
- In native CLI-tools mode the opencode backend is launched with
  `--dangerously-skip-permissions`, disabling its own interactive prompts.

This is by design — it enables non-interactive, automated agent workflows (like a
CI job or any autonomous agent runner). But it means:

> **Running an `@agent` workflow is equivalent to letting the chosen model read
> and write your files and run tools without prompting.** The trust boundary is
> **the workflow author and the model/endpoint you point at.** Only run workflows
> you trust, pointed at endpoints you trust.

Mitigations available today:
- Agents only fire when you explicitly `wfpy run` a workflow.
- The IDE's built-in `python` tool defaults to deny-all
  (`wfLang.agentToolAuth: "deny-all"`); enable tools deliberately.
- Prefer running untrusted workflows in a sandbox / container / throwaway
  workdir.

## Human-in-the-loop — `@agent(ask_user=True)`

An agent may be granted the ability to **pause mid-firing and ask the operator a
question** via the builtin `ask_user` tool (`@agent(ask_user=True)`, HTTP
transport). This is the first real human gate in an otherwise auto-approving
system — **safety-positive** — but note:

- **The question text is authored by the (untrusted) model, not by wfpy.** The
  console prompt is clearly attributed to the agent; never treat it as a wfpy
  system message, and don't reveal secrets in response to an unexpected question.
- The reply is only ever fed back to the model as a tool result — wfpy never
  executes or interprets it.
- `ask_user` is exempt from the `agent_tool_auth` gate (asking a human runs no
  code) and works even under `deny-all`; it never enables the `python`/MCP tools.
- Questions are bounded by the tool-round cap (`AGENT_MAX_TOOL_ROUNDS`).
- In non-interactive runs the tool degrades gracefully (the agent is told no user
  is available and proceeds) unless `--elicit-require` is set. Replies are **not**
  broadcast on the run event stream.

## Live run event stream

A run serves a localhost Server-Sent-Events endpoint (`RunEventStream`) so an
observer (the IDE) can watch agent messages live. It is:

- **Bound to `127.0.0.1` only** (never a routable interface).
- **Read-only** — there are no mutating endpoints; observing cannot affect a run.
- **Token-protected** — every request must carry a per-run bearer token
  (`?token=…` or `Authorization: Bearer …`); the token is published only in the
  run's local `run.wf-stream.live.json` discovery file.
- **DNS-rebinding hardened** — non-loopback `Host` headers are rejected, so a web
  page resolving to `127.0.0.1` cannot read the stream.

## Reporting a vulnerability

Please report security issues privately via the project's issue tracker
(mark the issue confidential) rather than opening a public report.
