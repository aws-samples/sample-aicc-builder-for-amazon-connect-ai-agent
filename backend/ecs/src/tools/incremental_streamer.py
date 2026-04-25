"""
Incremental Code Block Streamer

Detects code blocks in streaming LLM output and progressively streams
their content to the frontend via stream_asset().

This enables users to see code appearing in preview panels as it's generated,
rather than waiting for the entire response to complete.

Usage:
    streamer = IncrementalCodeStreamer(
        asset_type="lambda",
        file_name="handler.py",
        operation_id="create_reservation",
        code_markers=["python"],
        flush_interval=500,
    )

    async for event in agent.stream_async(prompt):
        if "data" in event:
            chunk = event["data"]
            streamer.feed(chunk)

    streamer.finalize()
    code = streamer.get_result()
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class IncrementalCodeStreamer:
    """
    Accumulates streaming text chunks from an LLM response,
    detects code block boundaries (```marker ... ```),
    and periodically calls stream_asset() with partial content.

    Each instance tracks a single (asset_type, file_name, operation_id) target.
    Multiple instances can run concurrently for different asset types.
    """

    def __init__(
        self,
        asset_type: str,
        file_name: str,
        operation_id: str,
        code_markers: list[str] | None = None,
        flush_interval: int = 500,
        suppress_complete: bool = False,
    ):
        """
        Args:
            asset_type: Asset type for stream_asset() (e.g., "lambda", "openapi")
            file_name: File name for stream_asset() (e.g., "handler.py")
            operation_id: Operation ID for stream_asset()
            code_markers: Language markers to detect (e.g., ["python", "yaml"]).
                          Matches ```python, ```yaml, etc.
            flush_interval: Minimum chars accumulated before flushing a partial update.
            suppress_complete: If True, never send is_complete=True. Used in chunked
                generation where a later phase will send the final complete signal.
        """
        self.asset_type = asset_type
        self.file_name = file_name
        self.operation_id = operation_id
        self.code_markers = code_markers or []
        self.flush_interval = flush_interval
        self.suppress_complete = suppress_complete

        # State
        self._buffer = ""           # Entire text seen so far
        self._code_content = ""     # Accumulated code inside current block
        self._in_code_block = False
        self._found_code_block = False
        self._code_complete = False
        self._last_flush_len = 0    # Length at last flush

    @property
    def found_code_block(self) -> bool:
        """Whether a matching code block was detected."""
        return self._found_code_block

    def feed(self, chunk: str) -> None:
        """
        Feed a new text chunk from the LLM stream.

        Internally detects code block boundaries and flushes partial
        content to the frontend at intervals.
        """
        self._buffer += chunk

        if self._code_complete:
            # Already found and closed the code block; ignore further chunks
            return

        if not self._in_code_block:
            # Look for opening marker in the buffer
            for marker in self.code_markers:
                opening = f"```{marker}"
                idx = self._buffer.find(opening)
                if idx != -1:
                    # Found opening marker
                    after_marker = idx + len(opening)
                    # Skip past the newline after the marker
                    rest = self._buffer[after_marker:]
                    newline_idx = rest.find("\n")
                    if newline_idx != -1:
                        self._in_code_block = True
                        self._found_code_block = True
                        # Everything after the marker line is code content
                        self._code_content = rest[newline_idx + 1:]
                        self._last_flush_len = 0
                        logger.debug(
                            f"[IncrementalStreamer] {self.asset_type}/{self.operation_id}: "
                            f"Opened code block (marker={marker})"
                        )
                        # Check if the closing ``` is already in the content
                        self._check_close_and_flush()
                    break
        else:
            # Inside a code block - append the chunk to code content
            self._code_content += chunk
            self._check_close_and_flush()

    def _check_close_and_flush(self) -> None:
        """Check for closing ``` and flush partial content if threshold reached."""
        # Check for closing marker
        close_idx = self._code_content.find("\n```")
        if close_idx == -1:
            # Also check for ``` at the very start (edge case)
            if self._code_content.rstrip().endswith("```"):
                close_idx = self._code_content.rstrip().rfind("```")
                # Only treat as closing if it's on its own line
                before = self._code_content[:close_idx]
                last_newline = before.rfind("\n")
                line_content = before[last_newline + 1:].strip() if last_newline != -1 else before.strip()
                if line_content:
                    # The ``` is not on its own line, not a closing marker
                    close_idx = -1

        if close_idx != -1:
            # Found closing marker
            self._code_content = self._code_content[:close_idx].rstrip("\n")
            self._in_code_block = False
            self._code_complete = True
            # Final flush with is_complete=True
            self._flush(is_complete=True)
            logger.debug(
                f"[IncrementalStreamer] {self.asset_type}/{self.operation_id}: "
                f"Code block closed ({len(self._code_content)} chars)"
            )
        else:
            # Still accumulating - flush if enough new content
            new_chars = len(self._code_content) - self._last_flush_len
            if new_chars >= self.flush_interval:
                self._flush(is_complete=False)

    def _flush(self, is_complete: bool) -> None:
        """Send current content to frontend via stream_asset()."""
        if not self._code_content:
            return

        # In suppress_complete mode, never send is_complete=True
        # A later phase (e.g., chunked merge) will send the final complete signal
        effective_complete = is_complete and not self.suppress_complete

        try:
            from .streaming_callback import stream_asset
            stream_asset(
                self.asset_type,
                self.file_name,
                self._code_content,
                operation_id=self.operation_id,
                is_complete=effective_complete,
            )
            self._last_flush_len = len(self._code_content)
            logger.debug(
                f"[IncrementalStreamer] {self.asset_type}/{self.operation_id}: "
                f"Flushed {self._last_flush_len} chars (complete={is_complete})"
            )
        except ImportError:
            logger.warning(
                f"[IncrementalStreamer] streaming_callback not available"
            )
        except Exception as e:
            logger.warning(
                f"[IncrementalStreamer] Flush error: {e}"
            )

    def finalize(self) -> None:
        """
        Force a final flush if the code block was never properly closed.

        Call this after the stream ends to ensure any accumulated content
        is sent to the frontend.
        """
        if self._in_code_block and self._code_content and not self._code_complete:
            # Code block was opened but never closed - flush what we have
            logger.warning(
                f"[IncrementalStreamer] {self.asset_type}/{self.operation_id}: "
                f"Code block never closed, finalizing with {len(self._code_content)} chars"
            )
            self._code_complete = True
            self._flush(is_complete=True)

    def get_result(self) -> Optional[str]:
        """
        Get the extracted code content.

        Returns:
            The code block content if found and extracted, None otherwise.
        """
        if self._found_code_block and self._code_content:
            return self._code_content.strip()
        return None
