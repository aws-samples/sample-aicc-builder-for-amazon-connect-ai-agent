"""Reproducer for the ECS session-isolation bug.

Before the ContextVar migration, module-level globals in spec_manager,
streaming_callback, project_workspace and the 9 sub-agents were shared
across every concurrent WebSocket session on a single FastAPI process.
A request for user A yielding at an ``await`` would read/write user B's
state after B's message mutated the global.

These tests prove that state set inside ``session_scope(sid)`` is
invisible to a peer session running concurrently on the same event loop.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

# Make ``src`` importable the same way the Docker entrypoint does.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.session_context import (  # noqa: E402
    active_session_ids,
    cleanup_session,
    current_callback_handler,
    current_message_index,
    current_session_id,
    current_streaming_callback,
    fragment_registry_for,
    openapi_fragment_registry_for,
    operation_specs_bucket,
    schema_registry_for,
    session_scope,
    set_workspace_for,
)


@pytest.mark.asyncio
async def test_scalar_contextvars_isolate_across_tasks():
    """current_session_id / callback / streaming / message_index must not bleed."""

    seen: dict[str, dict] = {}

    async def run(sid: str, cb: str, stream: str, msg_idx: int) -> None:
        async with session_scope(
            sid,
            callback_handler=cb,
            streaming_callback=stream,
            message_index=msg_idx,
        ):
            # Yield to let the peer interleave — this is exactly the async
            # reordering that used to corrupt module globals.
            await asyncio.sleep(0)
            seen[sid] = {
                "session_id": current_session_id.get(),
                "callback": current_callback_handler.get(),
                "stream": current_streaming_callback.get(),
                "msg_idx": current_message_index.get(),
            }
            await asyncio.sleep(0.01)
            # After a second yield the values must still be ours.
            assert current_session_id.get() == sid
            assert current_callback_handler.get() == cb

    await asyncio.gather(
        run("A", "cb-A", "stream-A", 1),
        run("B", "cb-B", "stream-B", 2),
    )

    assert seen["A"] == {"session_id": "A", "callback": "cb-A", "stream": "stream-A", "msg_idx": 1}
    assert seen["B"] == {"session_id": "B", "callback": "cb-B", "stream": "stream-B", "msg_idx": 2}


@pytest.mark.asyncio
async def test_operation_specs_bucket_isolated_per_session():
    """operation_specs_bucket must return a different dict per session_id."""

    async def write(sid: str, marker: str) -> None:
        async with session_scope(sid):
            bucket = operation_specs_bucket(current_session_id.get())
            await asyncio.sleep(0)
            bucket["check_reservation"] = {"summary": marker}
            await asyncio.sleep(0.01)
            # After the interleave, what we read back must be ours.
            assert bucket["check_reservation"]["summary"] == marker
            assert operation_specs_bucket(sid)["check_reservation"]["summary"] == marker

    await asyncio.gather(
        write("wash-and-joy", "wash-and-joy-spec"),
        write("sungmin-eye", "sungmin-eye-spec"),
    )

    # Cross-check after both finish.
    assert operation_specs_bucket("wash-and-joy")["check_reservation"]["summary"] == "wash-and-joy-spec"
    assert operation_specs_bucket("sungmin-eye")["check_reservation"]["summary"] == "sungmin-eye-spec"

    cleanup_session("wash-and-joy")
    cleanup_session("sungmin-eye")


@pytest.mark.asyncio
async def test_fragment_and_schema_registries_isolated():
    """Infrastructure/OpenAPI fragment + schema registries must be per-session."""

    async def write(sid: str) -> None:
        async with session_scope(sid):
            frag = fragment_registry_for(sid)
            sch = schema_registry_for(sid)
            openapi = openapi_fragment_registry_for(sid)
            await asyncio.sleep(0)
            frag["proj"] = {"base": sid, "fragments": {}}
            sch["proj"] = f"schema-for-{sid}"
            openapi["api"] = {"base": f"openapi-{sid}", "chunks": {}}
            await asyncio.sleep(0.01)
            assert fragment_registry_for(sid)["proj"]["base"] == sid
            assert schema_registry_for(sid)["proj"] == f"schema-for-{sid}"
            assert openapi_fragment_registry_for(sid)["api"]["base"] == f"openapi-{sid}"

    await asyncio.gather(write("A"), write("B"))

    assert fragment_registry_for("A")["proj"]["base"] == "A"
    assert fragment_registry_for("B")["proj"]["base"] == "B"
    assert schema_registry_for("A")["proj"] == "schema-for-A"
    assert schema_registry_for("B")["proj"] == "schema-for-B"

    cleanup_session("A")
    cleanup_session("B")


@pytest.mark.asyncio
async def test_cleanup_session_purges_all_dicts():
    async with session_scope("tmp-sid"):
        set_workspace_for("tmp-sid", object())
        operation_specs_bucket("tmp-sid")["x"] = {"foo": 1}
        fragment_registry_for("tmp-sid")["p"] = {}
        schema_registry_for("tmp-sid")["p"] = "x"

    assert "tmp-sid" in active_session_ids()
    cleanup_session("tmp-sid")
    assert "tmp-sid" not in active_session_ids()
