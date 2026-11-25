"""
ACP (Agent Client Protocol) client for opencode integration.

This module provides an async client that communicates with opencode via ACP
over JSON-RPC on stdio. It includes stuck detection that monitors all events
and sends "continue" messages if the agent appears stuck.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncGenerator, Callable, Optional

import acp
from acp.client.connection import ClientSideConnection
from acp.schema import AllowedOutcome, DeniedOutcome, TextContentBlock

from wfpy.connectors import DEFAULT_STUCK_RETRIES, DEFAULT_STUCK_TIMEOUT_MS

logger = logging.getLogger(__name__)

# Stuck detection configuration. The defaults live in `connectors`, which has no
# optional dependency, so the CLI and the option resolution can name them without
# importing this module. A run overrides them per agent: `--acp-stuck-timeout-ms`
# / `--acp-stuck-retries`, or a connector's `stuck_timeout_ms` / `stuck_retries`.
STUCK_TIMEOUT_SECONDS = DEFAULT_STUCK_TIMEOUT_MS // 1000
MAX_RETRIES = DEFAULT_STUCK_RETRIES
CONTINUE_MESSAGE = "You appear stuck. Please continue with the task."


PermissionHandler = Callable[[str, list[dict[str, str]]], Optional[str]]
"""Answers an agent's ``session/request_permission``: given the tool call's
title and its options (``{"id", "kind", "name"}``, the kinds ``allow_once``,
``allow_always``, ``reject_once``, ``reject_always``), returns the chosen
option's id, or ``None`` to cancel the prompt turn. It may block: it runs on
a worker thread, and the stuck detector waits with it."""


def option_of_kind(options: list[dict[str, str]], kind: str) -> Optional[str]:
    """The id of the first option whose kind starts with ``kind``, else None."""
    for option in options:
        if str(option.get("kind", "")).startswith(kind):
            return option.get("id")
    return None


def allow_once(title: str, options: list[dict[str, str]]) -> Optional[str]:
    """The default without a user: allow this once, else allow, else the
    first option the agent offered."""
    chosen = option_of_kind(options, "allow_once") or option_of_kind(options, "allow")
    if chosen is None and options:
        chosen = options[0].get("id")
    return chosen


def _tool_call_fields(update: Any, known_name: Optional[str] = None,
                      known_title: Optional[str] = None,
                      known_kind: Optional[str] = None) -> dict[str, Any]:
    """What a run's observer sees of an ACP tool call: a name, the title,
    the kind, the status, the id. The name is the tool's own where the agent
    sends one (`name`, since schema 1.23), else the title, else the kind.
    The `known_*` are what earlier events of the same call said, for an
    update that does not repeat it: the tool's own name, the last title, the
    last kind. What the update itself says comes first at each step."""
    title = getattr(update, "title", None)
    kind = getattr(update, "kind", None)
    name = (getattr(update, "name", None) or known_name
            or title or known_title or kind or known_kind or "tool")
    call_id = getattr(update, "tool_call_id", None) or getattr(update, "toolCallId", None)
    return {"name": str(name), "title": title, "kind": kind,
            "status": getattr(update, "status", None), "id": call_id}


class SimpleClient:
    """
    Simple client implementation that handles callbacks from the agent.
    
    This implements the Client protocol required by the ACP library.
    """
    
    def __init__(self, on_event: Optional[Callable[[dict[str, Any]], None]] = None,
                 on_permission: Optional[PermissionHandler] = None):
        self.events = asyncio.Queue()
        self.agent = None
        self.response_text = ""  # Accumulated response text
        # Who answers the agent's permission requests: a handler (the user,
        # through the elicitation seam, or a policy), else `allow_once`.
        self.on_permission = on_permission
        # Permission requests waiting on the handler: the stuck detector
        # does not count that time.
        self.permission_pending = 0
        # Optional sink for structured, observer-facing events (live streaming to the
        # IDE). Called synchronously and best-effort; must never raise into the run.
        self.on_event = on_event
        # What each call has said of itself so far, by call id: the tool's own
        # name, its last title, its last kind. A later update of the call
        # need repeat none of them, and is still named as its call.
        self._tool_names: dict[str, str] = {}
        self._tool_titles: dict[str, str] = {}
        self._tool_kinds: dict[str, str] = {}

    def _tool_call(self, update: Any) -> dict[str, Any]:
        """The observer's fields of a tool call or of an update of one.

        An event that brings the tool's own name or a title is named by
        it, as it always was. One that brings neither, an update that says
        only how the call ended (Codex's do), is named by what the call said
        last and no longer as "tool"; the kind is the last resort, after
        the call's last title."""
        call_id = getattr(update, "tool_call_id", None) or getattr(update, "toolCallId", None)
        if not call_id:
            return _tool_call_fields(update)
        key = str(call_id)
        said = (("name", self._tool_names), ("title", self._tool_titles),
                ("kind", self._tool_kinds))
        for field, known in said:
            value = getattr(update, field, None)
            if value:
                known[key] = str(value)
        return _tool_call_fields(update, self._tool_names.get(key),
                                 self._tool_titles.get(key), self._tool_kinds.get(key))

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception as exc:  # observation must never break the run
            logger.debug(f"on_event sink failed: {exc}")

    def on_connect(self, conn):
        """Called when connection is established."""
        self.agent = conn
        logger.info("Connected to agent")
        
    async def session_update(self, session_id: str, update, **kwargs):
        """Called when agent sends a session update."""
        event = {
            "session_id": session_id,
            "update": update,
            "kwargs": kwargs,
        }
        
        update_type = getattr(update, 'sessionUpdate', 'unknown')
        logger.info(f"Session update type: {update_type}")
        
        # Extract text from AgentMessageChunk
        if hasattr(update, 'sessionUpdate') and update.sessionUpdate == 'agent_message_chunk':
            if hasattr(update, 'content') and hasattr(update.content, 'type'):
                content_type = update.content.type
                logger.info(f"Message chunk content type: {content_type}")
                if content_type == 'text' and hasattr(update.content, 'text'):
                    text = update.content.text
                    self.response_text += text
                    logger.info(f"Accumulated text chunk ({len(text)} chars): {text[:200]}...")
                    self._emit({"type": "agent.message.delta", "field": "text", "delta": text})
        elif hasattr(update, 'sessionUpdate') and update.sessionUpdate == 'agent_thought_chunk':
            # Reasoning / chain-of-thought stream (separate from the answer text).
            content = getattr(update, 'content', None)
            if content is not None and getattr(content, 'type', None) == 'text' and hasattr(content, 'text'):
                self._emit({"type": "agent.message.delta", "field": "reasoning", "delta": content.text})
        elif hasattr(update, 'sessionUpdate') and update.sessionUpdate == 'tool_call':
            # An ACP tool call carries a title ("Write choice-a.txt") and a
            # kind ("edit", "execute"): the title is what a viewer shows, the
            # kind what it groups by. Since schema 1.23 (SDK 1.0) it may also
            # carry the tool's own name ("Write"), which is then the name
            # reported, for the call and for its updates alike.
            tool = self._tool_call(update)
            logger.info(f"Tool call: {tool['name']}")
            self._emit({"type": "agent.tool_call", **tool})
        elif hasattr(update, 'sessionUpdate') and update.sessionUpdate == 'tool_call_update':
            logger.info("Tool call update")
            self._emit({"type": "agent.tool_call_update", **self._tool_call(update)})
        
        await self.events.put(event)
        
    async def request_permission(self, options, session_id: str, tool_call, **kwargs):
        """Called when the agent requests permission for a tool call: the
        handler's choice, or `allow_once` without one, in the reply the
        protocol takes (a selected option id, or a cancelled turn)."""
        from acp import RequestPermissionResponse
        title = str(getattr(tool_call, "title", None) or getattr(tool_call, "kind", None) or "tool call")
        offered = [{"id": str(getattr(o, "option_id", None) or getattr(o, "optionId", "") or ""),
                    "kind": str(getattr(o, "kind", "") or ""),
                    "name": str(getattr(o, "name", "") or "")} for o in options or []]
        logger.info(f"Permission requested: {title} options={[o['id'] for o in offered]}")
        self._emit({"type": "agent.permission.requested", "title": title, "options": offered})
        await self.events.put({"session_id": session_id, "update": None,
                               "kwargs": {"permission": title}})
        if self.on_permission is not None:
            self.permission_pending += 1
            try:
                chosen = await asyncio.to_thread(self.on_permission, title, offered)
            finally:
                self.permission_pending -= 1
        else:
            chosen = allow_once(title, offered)
        self._emit({"type": "agent.permission.answered", "title": title, "option": chosen})
        await self.events.put({"session_id": session_id, "update": None,
                               "kwargs": {"permission": title, "option": chosen}})
        if chosen is None:
            logger.info(f"Permission cancelled: {title}")
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        logger.info(f"Permission answered: {title} -> {chosen}")
        return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=str(chosen)))
        
    async def create_terminal(self, command, session_id, args=None, cwd=None, env=None, output_byte_limit=None, **kwargs):
        """Called when agent wants to create a terminal."""
        logger.info(f"Terminal creation requested: command={command}, args={args}, cwd={cwd}")
        from acp import CreateTerminalResponse
        return CreateTerminalResponse(terminal_id="not-implemented")
        
    async def read_text_file(self, path, session_id, limit=None, line=None, **kwargs):
        """Called when agent wants to read a file."""
        logger.info(f"Reading file: {path}")
        try:
            with open(path, 'r') as f:
                content = f.read()
            from acp import ReadTextFileResponse
            return ReadTextFileResponse(content=content)
        except Exception as e:
            logger.error(f"Failed to read file: {e}")
            raise
            
    async def write_text_file(self, content, path, session_id, **kwargs):
        """Called when agent wants to write a file."""
        logger.info(f"Writing file: {path} ({len(content)} chars)")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write(content)
            logger.info(f"Successfully wrote {len(content)} chars to {path}")
            from acp import WriteTextFileResponse
            return WriteTextFileResponse()
        except Exception as e:
            logger.error(f"Failed to write file: {e}")
            raise
            
    async def kill_terminal(self, session_id, terminal_id, **kwargs):
        """Called when agent wants to kill a terminal."""
        logger.warning("Terminal kill not implemented")
        
    async def release_terminal(self, session_id, terminal_id, **kwargs):
        """Called when agent wants to release a terminal."""
        logger.warning("Terminal release not implemented")
        
    async def terminal_output(self, session_id, terminal_id, **kwargs):
        """Called when agent wants terminal output."""
        logger.warning("Terminal output not implemented")
        from acp import TerminalOutputResponse
        return TerminalOutputResponse(output="")
        
    async def wait_for_terminal_exit(self, session_id, terminal_id, **kwargs):
        """Called when agent wants to wait for terminal exit."""
        logger.warning("Terminal exit wait not implemented")
        from acp import WaitForTerminalExitResponse
        return WaitForTerminalExitResponse(exit_code=0)
        
    async def ext_method(self, method, params):
        """Called for extension methods."""
        logger.warning(f"Extension method not implemented: {method}")
        return {}
        
    async def ext_notification(self, method, params):
        """Called for extension notifications. None is acted on. One wfpy
        knows to be routine is passed over quietly; another is still named,
        since it may be the agent saying something that matters."""
        if method in _ROUTINE_NOTIFICATIONS:
            logger.debug(f"Extension notification ignored: {method}")
        else:
            logger.warning(f"Extension notification not implemented: {method}")


_METHOD_NOT_FOUND = -32601

# Notifications a connector sends as a matter of course and wfpy does not act
# on. `claude-agent-acp` pushes its login state as `auth/status_update` on
# every turn, under that name and as the extension `_auth/status_update`.
_ROUTINE_NOTIFICATIONS = frozenset({"auth/status_update"})


def _drop_unrouted_notifications(connection: Any) -> None:
    """Drop a notification the SDK has no route for, without its traceback.

    A notification has no reply, so a client that does not know one ignores
    it, and connectors count on that. SDK 1.0 logs each one as an unhandled
    error with its traceback instead: three of them on a run that succeeds,
    for the `auth/status_update` of `claude-agent-acp`.

    A routine one (`_ROUTINE_NOTIFICATIONS`) is dropped at debug; any other
    is dropped with one warning line that names it. Only the SDK's own "no
    route for this method" is taken for that: a request without a route still
    fails (it has a reply to carry the error), and so does any other failure
    inside a notification, a "method not found" for another method included.

    The SDK offers no hook for this, so the handler of its connection is
    wrapped. Should its layout change, nothing is wrapped and the tracebacks
    come back; a run is unaffected either way.
    """
    conn = getattr(connection, "_conn", None)
    handler = getattr(conn, "_handler", None)
    if handler is None:
        logger.debug("The ACP SDK's connection has no handler to wrap; "
                     "notifications it has no route for keep their traceback")
        return

    async def handle(method, params, is_notification):
        try:
            return await handler(method, params, is_notification)
        except acp.RequestError as exc:
            data = getattr(exc, "data", None)
            unrouted = (is_notification
                        and getattr(exc, "code", None) == _METHOD_NOT_FOUND
                        and isinstance(data, dict) and data.get("method") == method)
            if not unrouted:
                raise
            if method in _ROUTINE_NOTIFICATIONS:
                logger.debug(f"Notification ignored: {method}")
            else:
                logger.warning(
                    f"The agent sent a notification wfpy does not know: {method} (ignored)"
                )
            return None

    conn._handler = handle



def _session_option(options: list[Any], key: str, *, by_id: bool = True) -> Any:
    """The select config option a session offers for `key`: the one with that
    id, else the first with that category (ACP's hint for what an option
    sets; OpenCode's reasoning level is id `effort`, category
    `thought_level`)."""
    selects = [o for o in options if getattr(o, "options", None) is not None]
    return ((next((o for o in selects if str(getattr(o, "id", "")) == key), None) if by_id else None)
            or next((o for o in selects if str(getattr(o, "category", "") or "") == key), None))


def _option_choices(option: Any) -> list[Any]:
    """A select option's choices, groups flattened."""
    choices: list[Any] = []
    for entry in list(getattr(option, "options", None) or []):
        grouped = getattr(entry, "options", None)
        choices.extend(grouped if isinstance(grouped, list) else [entry])
    return choices


def _choose(choices: list[Any], wanted: str) -> Any:
    """The choice whose value, else whose name, is `wanted`, case-insensitive."""
    key = wanted.strip().lower()
    return (next((c for c in choices if str(getattr(c, "value", "")).lower() == key), None)
            or next((c for c in choices if str(getattr(c, "name", "")).lower() == key), None))

class ACPClient:
    """
    Async client for communicating with opencode via ACP protocol.
    
    Spawns opencode as a subprocess and communicates via JSON-RPC over stdio.
    Provides methods for session management and event streaming.
    """
    
    def __init__(self, opencode_command: str = "opencode", env: dict[str, str] | None = None,
                 on_event: Optional[Callable[[dict[str, Any]], None]] = None,
                 agent_argv: Optional[list[str]] = None,
                 on_permission: Optional[PermissionHandler] = None):
        self.opencode_command = opencode_command
        # The agent's command line: `agent_argv` verbatim (`claude-agent-acp`,
        # any agent speaking ACP over stdio), else opencode's `acp` subcommand.
        self.agent_argv = list(agent_argv) if agent_argv else [opencode_command, "acp"]
        self.env = env
        self.on_event = on_event
        self.on_permission = on_permission
        # Whether the agent advertised `session/load` at initialize: a session
        # from an earlier process can be resumed only through it.
        self.can_load_session = False
        # The last new_session / load_session response: what the agent offers
        # (modes, config options) for `configure_session`.
        self.last_session_response: Any = None
        self.client: Optional[SimpleClient] = None
        self.connection: Optional[ClientSideConnection] = None
        self.process = None
        self._context_manager = None

    async def start(self):
        """Start the opencode subprocess and initialize ACP connection."""
        logger.info(f"Starting ACP agent subprocess: {' '.join(self.agent_argv)}")

        # Create client implementation
        self.client = SimpleClient(on_event=self.on_event, on_permission=self.on_permission)
        
        # Spawn agent process using the ACP library
        # Increase buffer limit to 10MB to handle large JSON-RPC messages
        self._context_manager = acp.spawn_agent_process(
            self.client,
            *self.agent_argv,
            env=self.env,
            transport_kwargs={"limit": 10 * 1024 * 1024}  # 10MB buffer
        )
        
        # Enter the context manager
        self.connection, self.process = await self._context_manager.__aenter__()
        _drop_unrouted_notifications(self.connection)

        logger.info("ACP subprocess started, initializing connection...")
        
        # Initialize the connection
        init = await self.connection.initialize(
            protocol_version=1,
            client_capabilities=None,
            client_info=None,
        )
        capabilities = getattr(init, "agent_capabilities", None) or getattr(init, "agentCapabilities", None)
        self.can_load_session = bool(getattr(capabilities, "load_session", False)
                                     or getattr(capabilities, "loadSession", False))
        logger.info(f"ACP connection initialized successfully (load_session={self.can_load_session})")
        
    async def stop(self):
        """Stop the opencode subprocess."""
        if self._context_manager:
            logger.info("Closing ACP client connection")
            try:
                await self._context_manager.__aexit__(None, None, None)
            except RuntimeError as e:
                # Ignore "message queue already closed" errors - this is expected during shutdown
                if "queue already closed" not in str(e).lower():
                    raise
                logger.debug("Ignored queue closed error during shutdown")
            self._context_manager = None
            self.connection = None
            self.process = None
            self.client = None
            
        logger.info("ACP client stopped")
        
    async def create_session(self, cwd: str = ".", title: Optional[str] = None,
                             mcp_servers: Optional[list[dict[str, Any]]] = None) -> str:
        """
        Create a new ACP session.
        
        Args:
            cwd: Working directory for the session
            title: Optional session title (not used in current API)
            
        Returns:
            Session ID
        """
        if not self.connection:
            raise RuntimeError("Client not started. Call start() first.")
            
        cwd = _session_cwd(cwd)
        logger.info(f"Creating new session (cwd={cwd})")
        
        response = await self.connection.new_session(cwd=cwd, mcp_servers=_mcp_servers(mcp_servers))
        self.last_session_response = response
        session_id = response.session_id
        logger.info(f"Session created: {session_id}")
        
        return session_id
        
    async def load_session(self, session_id: str, cwd: str = ".",
                           mcp_servers: Optional[list[dict[str, Any]]] = None) -> None:
        """Resume a session from an earlier process: `session/load`, which the
        agent answers by replaying the session's history as updates. Every
        firing is a new agent process, so a stateful agent's session lives
        on only through this."""
        if not self.connection:
            raise RuntimeError("Client not started. Call start() first.")
        cwd = _session_cwd(cwd)
        logger.info(f"Loading session {session_id} (cwd={cwd})")
        self.last_session_response = await self.connection.load_session(
            session_id=session_id, cwd=cwd, mcp_servers=_mcp_servers(mcp_servers))
        # The replayed history is not this firing's answer.
        self.client.response_text = ""
        logger.info(f"Session loaded: {session_id}")

    async def configure_session(self, session_id: str, response: Any,
                                model: str | None = None, mode: str | None = None,
                                effort: str | None = None) -> dict[str, Any]:
        """Apply a connector's model, mode and reasoning level to a session
        the agent offers them on. `response` is the new_session or
        load_session response, whose `config_options` and `modes` say what
        the agent offers.

        - The model: a config option with id or category `model` (Claude
          Code: default, sonnet, haiku, opus...), matched by value, then by
          name, case-insensitive, then by prefix.
        - The mode: from a config option with id or category `mode` (OpenCode
          2 offers it only so: build, plan), by value then name, else by id
          from the session's `modes` (Claude Code); case-insensitive.
        - The reasoning level (`effort`): a config option with category
          `thought_level` (OpenCode 2: minimal ... max), by value then name.

        Each option is looked up in the options the agent sent back when the
        one before it was set: a model can bring its own reasoning levels.

        What the agent does not offer is logged and left."""
        applied: dict[str, Any] = {}
        options = getattr(response, "config_options", None) or getattr(response, "configOptions", None) or []
        modes = getattr(response, "modes", None)
        if model:
            option = _session_option(options, "model")
            if option is None:
                logger.warning(f"The agent offers no model option; model {model!r} not applied")
            else:
                choices = _option_choices(option)
                chosen = (_choose(choices, model)
                          or next((c for c in choices if str(getattr(c, "value", "")).lower()
                                   .startswith(model.strip().lower())), None))
                if chosen is None:
                    logger.warning(f"The agent's model option has no {model!r}; it offers "
                                   f"{[getattr(c, 'value', None) for c in choices]}")
                else:
                    applied["model"], options = await self._set_option(session_id, option, chosen, options)
                    logger.info(f"Session model set to {applied['model']}")
        if mode:
            available = [str(getattr(m, "id", "")) for m in (getattr(modes, "available_modes", None) or [])]
            option = _session_option(options, "mode")
            chosen = _choose(_option_choices(option), mode) if option is not None else None
            legacy = next((m for m in available if m.lower() == mode.strip().lower()), None)
            if chosen is not None:
                applied["mode"], options = await self._set_option(session_id, option, chosen, options)
                logger.info(f"Session mode set to {applied['mode']}")
            elif legacy is not None:
                await self.connection.set_session_mode(session_id=session_id, mode_id=legacy)
                applied["mode"] = legacy
                logger.info(f"Session mode set to {legacy}")
            else:
                offered = available or [getattr(c, "value", None) for c in _option_choices(option)]
                logger.warning(f"The agent offers no mode {mode!r}; it offers {offered}")
        if effort:
            option = _session_option(options, "thought_level", by_id=False)
            if option is None:
                logger.warning(f"The agent offers no reasoning level option; {effort!r} not applied")
            else:
                choices = _option_choices(option)
                chosen = _choose(choices, effort)
                if chosen is None:
                    logger.warning(f"The agent's reasoning level option has no {effort!r}; it offers "
                                   f"{[getattr(c, 'value', None) for c in choices]}")
                else:
                    applied["effort"], options = await self._set_option(session_id, option, chosen, options)
                    logger.info(f"Session reasoning level set to {applied['effort']}")
        return applied

    async def _set_option(self, session_id: str, option: Any, chosen: Any,
                          options: list[Any]) -> tuple[str, list[Any]]:
        """Set a config option; return the value and the session's options
        as the agent now offers them. Choices can follow another option
        (OpenCode 2's reasoning levels are the model's), so the next option
        is looked up in the agent's reply where it sends one."""
        value = str(getattr(chosen, "value"))
        reply = await self.connection.set_config_option(config_id=str(getattr(option, "id")),
                                                        session_id=session_id, value=value)
        updated = getattr(reply, "config_options", None) or getattr(reply, "configOptions", None)
        return value, (list(updated) if updated else options)

    async def send_prompt(self, session_id: str, prompt: str) -> dict[str, Any]:
        """
        Send a prompt to the agent.
        
        Args:
            session_id: Session ID
            prompt: User prompt text
            
        Returns:
            Response from the agent
        """
        if not self.connection:
            raise RuntimeError("Client not started. Call start() first.")
        
        # Reset accumulated text before sending new prompt
        self.client.response_text = ""
            
        logger.info(f"Sending prompt to session {session_id}: {prompt[:100]}...")
        
        # One text content block, built from the schema's own model: the
        # `text_block` helper that used to build it was removed in SDK 1.0.
        prompt_blocks = [TextContentBlock(type="text", text=prompt)]
        
        response = await self.connection.prompt(
            session_id=session_id,
            prompt=prompt_blocks,
        )
        
        logger.info(f"Prompt completed with stop reason: {response.stopReason}")
        
        # Return both the stop reason and the accumulated text
        return {
            "stop_reason": response.stopReason,
            "text": self.client.response_text
        }
        
    async def cancel(self, session_id: str):
        """
        Cancel an ongoing operation in the session.
        
        Args:
            session_id: Session ID
        """
        if not self.connection:
            raise RuntimeError("Client not started. Call start() first.")
            
        logger.info(f"Cancelling session {session_id}")
        
        await self.connection.cancel(session_id=session_id)
        
        logger.info("Cancel notification sent")
        
    async def stream_events(self, session_id: str) -> AsyncGenerator[dict[str, Any], None]:
        """
        Stream session/update events from the agent.
        
        This is an async generator that yields events as they arrive.
        Events include: agent_message_chunk, tool_call, tool_call_update, plan, usage_update
        
        Args:
            session_id: Session ID
            
        Yields:
            Event dictionaries
        """
        if not self.client:
            raise RuntimeError("Client not started. Call start() first.")
            
        logger.info(f"Starting event stream for session {session_id}")
        
        while True:
            try:
                # Wait for event with timeout
                event = await asyncio.wait_for(
                    self.client.events.get(),
                    timeout=1.0
                )
                
                # Filter events for this session
                if event.get("session_id") == session_id:
                    update = event.get("update")
                    event_type = type(update).__name__
                    logger.debug(f"Event received: {event_type}")
                    
                    yield event
                    
            except asyncio.TimeoutError:
                # No event available, continue waiting
                continue
            except asyncio.CancelledError:
                # Stream was cancelled
                break
                
        logger.info(f"Event stream ended for session {session_id}")


async def run_with_stuck_detection(
    client: ACPClient,
    session_id: str,
    prompt: str,
    stuck_timeout: int = STUCK_TIMEOUT_SECONDS,
    max_retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    """
    Run opencode with stuck detection.
    
    Monitors all session/update events. If no event for stuck_timeout seconds,
    sends a "continue" message. Retries up to max_retries times.
    
    Args:
        client: ACPClient instance
        session_id: Session ID
        prompt: Initial prompt
        stuck_timeout: Seconds without events before considering stuck
        max_retries: Maximum number of continue attempts
        
    Returns:
        Final response from the agent
        
    Raises:
        TimeoutError: If stuck after max_retries attempts
    """
    logger.info(f"Starting prompt with stuck detection (timeout={stuck_timeout}s, max_retries={max_retries})")
    
    retries = 0
    
    # Start the prompt task
    prompt_task = asyncio.create_task(
        client.send_prompt(session_id, prompt)
    )
    
    # Monitor events in parallel
    event_task = asyncio.create_task(
        _monitor_events(client, session_id, stuck_timeout)
    )
    
    try:
        while True:
            # Wait for either prompt completion or stuck detection
            done, pending = await asyncio.wait(
                [prompt_task, event_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            
            if prompt_task in done:
                # Prompt completed successfully
                logger.info("Prompt completed successfully")
                event_task.cancel()
                try:
                    await event_task
                except asyncio.CancelledError:
                    pass
                return prompt_task.result()
                
            if event_task in done:
                # Stuck detected
                retries += 1
                logger.warning(f"Stuck detected, retry {retries}/{max_retries}")
                
                if retries >= max_retries:
                    logger.error(f"Stuck after {max_retries} retries, cancelling")
                    await client.cancel(session_id)
                    prompt_task.cancel()
                    try:
                        await prompt_task
                    except asyncio.CancelledError:
                        pass
                    # Name the window as well as the budget: an agent that is
                    # working but silent (OpenCode during a long `bash` streams
                    # nothing) is cancelled by exactly this path, and the fix is
                    # to widen the window, not to retry more.
                    raise TimeoutError(
                        f"Agent stuck after {max_retries} retries "
                        f"({stuck_timeout}s of silence each). If it was working, "
                        f"not hung, raise --acp-stuck-timeout-ms above its longest "
                        f"silent tool call."
                    )
                    
                # Send continue message
                logger.info(f"Sending continue message: {CONTINUE_MESSAGE}")
                await client.send_prompt(session_id, CONTINUE_MESSAGE)
                
                # Restart event monitoring
                event_task = asyncio.create_task(
                    _monitor_events(client, session_id, stuck_timeout)
                )
                
    except Exception as e:
        logger.error(f"Error during stuck detection: {e}")
        # Shut both tasks down, then retrieve whatever they ended with and drop
        # it. Awaiting them bare would be wrong twice over: `event_task` has
        # usually *already* finished with the bare `asyncio.TimeoutError` that
        # signalled the stuck turn, and since 3.11 that is `TimeoutError`, so
        # awaiting it replaced the error above — the caller got an empty
        # TimeoutError instead of the one naming the budget. Not retrieving it
        # at all makes asyncio warn about an exception that was never retrieved.
        prompt_task.cancel()
        event_task.cancel()
        for task in (prompt_task, event_task):
            with suppress(BaseException):
                await task
        raise


async def _monitor_events(
    client: ACPClient,
    session_id: str,
    stuck_timeout: float,
) -> None:
    """
    Monitor events and raise asyncio.TimeoutError if stuck.
    
    This is a helper task that runs in parallel with the prompt.
    It raises an exception when stuck_timeout is exceeded without any events.
    """
    while True:
        try:
            # Wait for next event with timeout
            event = await asyncio.wait_for(
                client.client.events.get(),
                timeout=stuck_timeout
            )
            
            # Filter events for this session
            if event.get("session_id") == session_id:
                update = event.get("update")
                event_type = type(update).__name__
                logger.debug(f"Monitor received event: {event_type}")
                # Event received, timer reset automatically by wait_for
                
        except asyncio.TimeoutError:
            if getattr(client.client, "permission_pending", 0):
                # The agent is waiting for a permission answer, and so are we.
                logger.info("Permission request pending, not stuck")
                continue
            # No event within timeout period - we're stuck!
            logger.warning(f"No events for {stuck_timeout}s - stuck detected")
            raise
        except asyncio.CancelledError:
            # Task was cancelled (prompt completed)
            logger.debug("Event monitor cancelled")
            return


def _session_cwd(cwd: str) -> str:
    """The session's working directory as the protocol wants it: an absolute
    path (`session/new` and `session/load` refuse a relative one)."""
    return os.path.abspath(cwd)


def _mcp_servers(servers: Optional[list[dict[str, Any]]]) -> list[Any]:
    """The session's MCP servers as the protocol's objects: HTTP ones, a
    name and a URL each (`session/new` and `session/load` take the list)."""
    if not servers:
        return []
    from acp.schema import HttpMcpServer
    return [HttpMcpServer(type="http", name=str(s["name"]), url=str(s["url"]), headers=[])
            for s in servers]


async def invoke_opencode_acp(
    prompt: str,
    cwd: str = ".",
    stuck_timeout: int = STUCK_TIMEOUT_SECONDS,
    max_retries: int = MAX_RETRIES,
    opencode_command: str = "opencode",
    env: dict[str, str] | None = None,
    session_id: str | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    agent_argv: list[str] | None = None,
    on_permission: PermissionHandler | None = None,
    model: str | None = None,
    mode: str | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """
    High-level function to invoke opencode via ACP with stuck detection.
    
    This is the main entry point for using ACP mode.
    
    Args:
        prompt: User prompt
        cwd: Working directory for the session
        stuck_timeout: Seconds without events before considering stuck
        max_retries: Maximum number of continue attempts
        opencode_command: Path to opencode executable
        env: Optional environment variables to pass to the subprocess
        session_id: Optional existing session ID to continue (if None, creates new session)
        agent_argv: The ACP agent's command line, verbatim; None for opencode's `acp`
        on_permission: Answers the agent's permission requests; None allows each once
        model: A session model to set, if the agent offers a `model` config option
        mode: A session mode to set, if the agent offers it
        effort: A reasoning level to set, if the agent offers a `thought_level` config option
        mcp_servers: HTTP MCP servers for the session, `{"name", "url"}` each: the
            run's own `ask_user` tool (`_ask_user_mcp`) is one
        
    Returns:
        Response from the agent
        
    Raises:
        TimeoutError: If stuck after max_retries attempts
        RuntimeError: If ACP communication fails
    """
    logger.info("Invoking opencode via ACP")
    
    client = ACPClient(opencode_command=opencode_command, env=env, on_event=on_event,
                       agent_argv=agent_argv, on_permission=on_permission)
    
    try:
        # Start the client
        await client.start()
        
        # Resume the session of an earlier firing, or create one. A session id
        # means nothing to a new agent process until `session/load`; an agent
        # that does not offer it, or fails to load, gets a fresh session, and
        # the caller records the new id.
        if session_id and client.can_load_session:
            try:
                await client.load_session(session_id, cwd=cwd, mcp_servers=mcp_servers)
                logger.info(f"Resumed session: {session_id}")
            except Exception as exc:  # noqa: BLE001 — a lost session is a fresh one, not a failed firing
                logger.warning(f"Could not load session {session_id} ({exc}); creating a new one")
                session_id = await client.create_session(cwd=cwd, mcp_servers=mcp_servers)
        elif session_id:
            logger.warning(f"The agent does not offer session/load; session {session_id} cannot be "
                           "resumed in a new process, creating a new one")
            session_id = await client.create_session(cwd=cwd, mcp_servers=mcp_servers)
        else:
            session_id = await client.create_session(cwd=cwd, mcp_servers=mcp_servers)
        applied = await client.configure_session(session_id, client.last_session_response,
                                                 model=model, mode=mode, effort=effort)
        # Run with stuck detection
        response = await run_with_stuck_detection(
            client=client,
            session_id=session_id,
            prompt=prompt,
            stuck_timeout=stuck_timeout,
            max_retries=max_retries,
        )
        
        logger.info("ACP invocation completed successfully")
        
        # Add session_id to response for session continuation
        response["session_id"] = session_id
        response["applied"] = applied
        return response
        
    finally:
        # Always stop the client
        await client.stop()
