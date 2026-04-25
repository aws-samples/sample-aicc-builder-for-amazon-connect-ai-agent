"""
Heartbeat Utilities for Sub-Agents

Provides background heartbeat functionality to prevent WebSocket timeouts
during long-running LLM generation tasks.
"""

import asyncio
import logging
from typing import Callable, Optional, Any

logger = logging.getLogger(__name__)

# Default heartbeat interval in seconds
DEFAULT_HEARTBEAT_INTERVAL = 10


class HeartbeatManager:
    """
    Manages background heartbeat tasks to keep WebSocket connections alive
    during long-running Sub-Agent operations.

    Usage:
        heartbeat = HeartbeatManager(
            callback_handler=get_callback_handler(),
            agent_name="openapi_generator",
            project_name="my-project",
            interval=10
        )

        async with heartbeat:
            async for event in agent.stream_async(prompt):
                heartbeat.update_progress(len(response))
                # ... process event
    """

    def __init__(
        self,
        callback_handler: Any,
        agent_name: str,
        project_name: str = "",
        interval: int = DEFAULT_HEARTBEAT_INTERVAL
    ):
        self.callback_handler = callback_handler
        self.agent_name = agent_name
        self.project_name = project_name
        self.interval = interval
        self.stop_event: Optional[asyncio.Event] = None
        self.task: Optional[asyncio.Task] = None
        self.heartbeat_count = 0
        self.current_progress = 0

    def send_heartbeat(self, message: str) -> bool:
        """Send heartbeat via callback handler."""
        if self.callback_handler and hasattr(self.callback_handler, 'add_ws_event'):
            try:
                self.callback_handler.add_ws_event({
                    "type": "subagent_progress",
                    "agent": self.agent_name,
                    "status": "running",
                    "project_name": self.project_name,
                    "message": message
                })
                logger.info(f"[HEARTBEAT:{self.agent_name}] {message}")
                return True
            except Exception as e:
                logger.warning(f"[HEARTBEAT:{self.agent_name}] Failed: {e}")
                return False
        return False

    async def _heartbeat_loop(self):
        """Background task that sends periodic heartbeats."""
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=self.interval
                )
                # Event was set, stop the loop
                break
            except asyncio.TimeoutError:
                # Timeout occurred, send heartbeat
                self.heartbeat_count += 1
                self.send_heartbeat(
                    f"Generating... ({self.current_progress} chars, heartbeat #{self.heartbeat_count})"
                )

    def update_progress(self, chars_generated: int):
        """Update current progress for heartbeat messages."""
        self.current_progress = chars_generated

    async def start(self):
        """Start the background heartbeat task."""
        self.stop_event = asyncio.Event()
        self.heartbeat_count = 0
        self.current_progress = 0

        # Send initial heartbeat
        self.send_heartbeat("Starting generation...")

        # Start background task
        self.task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"[HEARTBEAT:{self.agent_name}] Started background heartbeat")

    async def stop(self):
        """Stop the background heartbeat task."""
        if self.stop_event:
            self.stop_event.set()

        if self.task:
            try:
                await asyncio.wait_for(self.task, timeout=1.0)
            except asyncio.TimeoutError:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass

        logger.info(f"[HEARTBEAT:{self.agent_name}] Stopped after {self.heartbeat_count} heartbeats")

    async def __aenter__(self):
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.stop()
        return False


def create_heartbeat_manager(
    callback_handler: Any,
    agent_name: str,
    project_name: str = "",
    interval: int = DEFAULT_HEARTBEAT_INTERVAL
) -> HeartbeatManager:
    """
    Factory function to create a HeartbeatManager.

    Args:
        callback_handler: The callback handler from the parent agent
        agent_name: Name of the sub-agent (e.g., "openapi_generator")
        project_name: Project/operation name for context
        interval: Heartbeat interval in seconds (default: 10)

    Returns:
        HeartbeatManager instance
    """
    return HeartbeatManager(
        callback_handler=callback_handler,
        agent_name=agent_name,
        project_name=project_name,
        interval=interval
    )
