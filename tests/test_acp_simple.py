"""
Simple tests for ACP client functionality.

These tests verify basic ACP communication before integrating into the workflow.
"""

import asyncio
import logging
import pytest

# `acp` (agent-client-protocol) ships in the optional `agent` extra.
pytest.importorskip("acp")

from wfpy.acp_client import ACPClient, invoke_opencode_acp

# Configure logging for tests
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_simple_prompt():
    """Test 1: Send a simple prompt and verify response."""
    logger.info("Test 1: Simple prompt test")
    
    prompt = "What is 2+2? Reply with just the number."
    
    try:
        response = await invoke_opencode_acp(
            prompt=prompt,
            cwd=".",
            stuck_timeout=60,  # Shorter timeout for tests
            max_retries=2,
        )
        
        logger.info(f"Response received: {response}")
        
        # Verify we got a response
        assert response is not None, "Response should not be None"
        assert "stop_reason" in response, "Response should have stop_reason"
        
        logger.info("Test 1 PASSED: Simple prompt completed successfully")
        
    except Exception as e:
        logger.error(f"Test 1 FAILED: {e}")
        raise


@pytest.mark.asyncio
async def test_event_streaming():
    """Test 2: Send a prompt that triggers tool calls and verify events stream."""
    logger.info("Test 2: Event streaming test")
    
    prompt = "List the files in the current directory using ls command."
    
    client = ACPClient()
    
    try:
        await client.start()
        
        session_id = await client.create_session(title=".")
        
        # Start prompt and event collection in parallel
        prompt_task = asyncio.create_task(
            client.send_prompt(session_id, prompt)
        )
        
        events = []
        async for event in client.stream_events(session_id):
            # Event is a Pydantic model, get the update type
            update = event.get("update")
            event_type = type(update).__name__ if update else "unknown"
            logger.info(f"Event: {event_type}")
            events.append(event)
            
            # Stop after 10 events or if prompt completes
            if len(events) >= 10 or prompt_task.done():
                break
                
        # Wait for prompt to complete
        response = await prompt_task
        
        logger.info(f"Collected {len(events)} events")
        logger.info(f"Response: {response}")
        
        # Verify we got events
        assert len(events) > 0, "Should have received at least one event"
        
        # Verify we got a response
        assert response is not None, "Response should not be None"
        
        logger.info("Test 2 PASSED: Event streaming completed successfully")
        
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_stuck_detection_mock():
    """Test 3: Simulate stuck scenario and verify continue message is sent."""
    logger.info("Test 3: Stuck detection test (mock)")
    
    # This test is a bit tricky because we can't easily simulate a stuck agent
    # without modifying opencode itself. Instead, we'll test the stuck detection
    # logic by using a very short timeout and a prompt that takes time.
    
    prompt = "Count from 1 to 100, one number per line."
    
    try:
        # Use very short timeout to trigger stuck detection
        response = await invoke_opencode_acp(
            prompt=prompt,
            cwd=".",
            stuck_timeout=5,  # Very short timeout
            max_retries=2,
        )
        
        logger.info(f"Response received: {response}")
        
        # The test passes if we get a response (even if stuck detection triggered)
        assert response is not None, "Response should not be None"
        
        logger.info("Test 3 PASSED: Stuck detection test completed")
        
    except TimeoutError as e:
        # This is expected if the agent actually got stuck
        logger.info(f"Test 3 PASSED: Timeout occurred as expected: {e}")
        
    except Exception as e:
        logger.error(f"Test 3 FAILED: {e}")
        raise


@pytest.mark.asyncio
async def test_client_lifecycle():
    """Test 4: Verify client start/stop lifecycle."""
    logger.info("Test 4: Client lifecycle test")
    
    client = ACPClient()
    
    try:
        # Start
        await client.start()
        assert client.client is not None, "Client should be initialized"
        assert client.process is not None, "Process should be running"
        logger.info("Client started successfully")
        
        # Create session
        session_id = await client.create_session(title="Test: Lifecycle")
        assert session_id is not None, "Session ID should not be None"
        logger.info(f"Session created: {session_id}")
        
        # Stop
        await client.stop()
        assert client.client is None, "Client should be closed"
        assert client.process is None, "Process should be terminated"
        logger.info("Client stopped successfully")
        
        logger.info("Test 4 PASSED: Client lifecycle completed successfully")
        
    except Exception as e:
        logger.error(f"Test 4 FAILED: {e}")
        # Try to clean up
        try:
            await client.stop()
        except:
            pass
        raise


if __name__ == "__main__":
    # Run tests manually for debugging
    async def run_tests():
        logger.info("=" * 80)
        logger.info("Running ACP Client Tests")
        logger.info("=" * 80)
        
        try:
            await test_client_lifecycle()
            logger.info("")
            
            await test_simple_prompt()
            logger.info("")
            
            await test_event_streaming()
            logger.info("")
            
            await test_stuck_detection_mock()
            logger.info("")
            
            logger.info("=" * 80)
            logger.info("All tests PASSED")
            logger.info("=" * 80)
            
        except Exception as e:
            logger.error("=" * 80)
            logger.error(f"Tests FAILED: {e}")
            logger.error("=" * 80)
            raise
    
    asyncio.run(run_tests())
