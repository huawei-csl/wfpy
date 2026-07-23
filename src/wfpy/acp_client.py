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
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable, Optional

import acp
from acp.client.connection import ClientSideConnection

logger = logging.getLogger(__name__)

# Stuck detection configuration
STUCK_TIMEOUT_SECONDS = 300  # 5 minutes
MAX_RETRIES = 5
CONTINUE_MESSAGE = "You appear stuck. Please continue with the task."


class SimpleClient:
    """
    Simple client implementation that handles callbacks from the agent.
    
    This implements the Client protocol required by the ACP library.
    """
    
    def __init__(self, on_event: Optional[Callable[[dict[str, Any]], None]] = None):
        self.events = asyncio.Queue()
        self.agent = None
        self.response_text = ""  # Accumulated response text
        # Optional sink for structured, observer-facing events (live streaming to the
        # IDE). Called synchronously and best-effort; must never raise into the run.
        self.on_event = on_event

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
            tool_name = getattr(update, 'name', 'unknown')
            logger.info(f"Tool call: {tool_name}")
            self._emit({"type": "agent.tool_call", "name": tool_name})
        elif hasattr(update, 'sessionUpdate') and update.sessionUpdate == 'tool_call_update':
            logger.info(f"Tool call update")
            self._emit({"type": "agent.tool_call_update", "name": getattr(update, 'name', None)})
        
        await self.events.put(event)
        
    async def request_permission(self, options, session_id: str, tool_call, **kwargs):
        """Called when agent requests permission."""
        logger.info(f"Permission requested for tool: {tool_call}")
        # Auto-approve all permissions for now
        from acp import RequestPermissionResponse
        return RequestPermissionResponse(outcome="approved", option_id=options[0].id if options else None)
        
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
        """Called for extension notifications."""
        logger.warning(f"Extension notification not implemented: {method}")


class ACPClient:
    """
    Async client for communicating with opencode via ACP protocol.
    
    Spawns opencode as a subprocess and communicates via JSON-RPC over stdio.
    Provides methods for session management and event streaming.
    """
    
    def __init__(self, opencode_command: str = "opencode", env: dict[str, str] | None = None,
                 on_event: Optional[Callable[[dict[str, Any]], None]] = None):
        self.opencode_command = opencode_command
        self.env = env
        self.on_event = on_event
        self.client: Optional[SimpleClient] = None
        self.connection: Optional[ClientSideConnection] = None
        self.process = None
        self._context_manager = None

    async def start(self):
        """Start the opencode subprocess and initialize ACP connection."""
        logger.info(f"Starting opencode ACP subprocess: {self.opencode_command} acp")

        # Create client implementation
        self.client = SimpleClient(on_event=self.on_event)
        
        # Spawn agent process using the ACP library
        # Increase buffer limit to 10MB to handle large JSON-RPC messages
        self._context_manager = acp.spawn_agent_process(
            self.client,
            self.opencode_command, "acp",
            env=self.env,
            transport_kwargs={"limit": 10 * 1024 * 1024}  # 10MB buffer
        )
        
        # Enter the context manager
        self.connection, self.process = await self._context_manager.__aenter__()
        
        logger.info("ACP subprocess started, initializing connection...")
        
        # Initialize the connection
        await self.connection.initialize(
            protocol_version=1,
            client_capabilities=None,
            client_info=None,
        )
        
        logger.info("ACP connection initialized successfully")
        
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
        
    async def create_session(self, cwd: str = ".", title: Optional[str] = None) -> str:
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
            
        logger.info(f"Creating new session (cwd={cwd})")
        
        response = await self.connection.new_session(cwd=cwd)
        
        session_id = response.session_id
        logger.info(f"Session created: {session_id}")
        
        return session_id
        
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
        
        # Create text content block using helper
        from acp import text_block
        prompt_blocks = [text_block(prompt)]
        
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
                    raise TimeoutError(f"Agent stuck after {max_retries} retries")
                    
                # Send continue message
                logger.info(f"Sending continue message: {CONTINUE_MESSAGE}")
                await client.send_prompt(session_id, CONTINUE_MESSAGE)
                
                # Restart event monitoring
                event_task = asyncio.create_task(
                    _monitor_events(client, session_id, stuck_timeout)
                )
                
    except Exception as e:
        logger.error(f"Error during stuck detection: {e}")
        prompt_task.cancel()
        event_task.cancel()
        try:
            await prompt_task
        except asyncio.CancelledError:
            pass
        try:
            await event_task
        except asyncio.CancelledError:
            pass
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
            # No event within timeout period - we're stuck!
            logger.warning(f"No events for {stuck_timeout}s - stuck detected")
            raise
        except asyncio.CancelledError:
            # Task was cancelled (prompt completed)
            logger.debug("Event monitor cancelled")
            return


async def invoke_opencode_acp(
    prompt: str,
    cwd: str = ".",
    stuck_timeout: int = STUCK_TIMEOUT_SECONDS,
    max_retries: int = MAX_RETRIES,
    opencode_command: str = "opencode",
    env: dict[str, str] | None = None,
    session_id: str | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
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
        
    Returns:
        Response from the agent
        
    Raises:
        TimeoutError: If stuck after max_retries attempts
        RuntimeError: If ACP communication fails
    """
    logger.info("Invoking opencode via ACP")
    
    client = ACPClient(opencode_command=opencode_command, env=env, on_event=on_event)
    
    try:
        # Start the client
        await client.start()
        
        # Create or reuse session
        if session_id:
            logger.info(f"Reusing existing session: {session_id}")
        else:
            session_id = await client.create_session(cwd=cwd)
        
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
        
        return response
        
    finally:
        # Always stop the client
        await client.stop()
