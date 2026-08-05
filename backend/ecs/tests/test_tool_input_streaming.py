"""Regression test: tool inputs must survive Strands' streaming shape.

Found by the 2026-08-05 prod e2e run. Every ``save_operation_spec`` tool frame
reached the client with an EMPTY input, so the UI's tool cards showed no
operation name ("작업 사양 저장" with no "checkReservation") and the expanded
input panel was hidden entirely (``hasInput`` is false for ``{}``).

Root cause is in how Strands streams tool arguments. In
``strands/event_loop/streaming.py`` the accumulator starts as a STRING::

    current_tool_use["input"] = ""                      # handle_content_block_start
    state["current_tool_use"]["input"] += delta[...]    # handle_content_block_delta
    current_tool_use["input"] = json.loads(...)         # handle_content_block_stop

So for the whole streaming window ``current_tool_use["input"]`` is a partial JSON
*string*; it only becomes a dict at the very end. app.py guarded the capture with
``isinstance(tool_input, dict)``, which is never true while streaming, so
``tool_inputs`` was never populated — and the ``tool_end`` frame reads from that
same map.

Two invariants are pinned here:

  1. A partial-then-complete string sequence must end up captured as a dict
     (parse opportunistically; fragments just fail and are skipped).
  2. What goes out on the wire must ALWAYS be a dict, never the raw string. The
     client calls ``Object.keys(input)``, which on a string returns char indices
     ("0","1","2",…) — that is the garbled char-by-char input the agent itself
     flagged in the transcript.
"""

from __future__ import annotations

import json

import pytest


def capture(tool_inputs: dict, tool_use_id: str, tool_input) -> None:
    """The capture logic from app.py's current_tool_use branch.

    Kept as a standalone mirror because the real one is inline in a ~200-line
    async streaming loop that can't be driven without a live Bedrock stream.
    Any change to the original must be mirrored here or these tests go stale —
    that's the tradeoff for testing it at all.
    """
    if tool_use_id and tool_input:
        if isinstance(tool_input, dict):
            tool_inputs[tool_use_id] = tool_input
        elif isinstance(tool_input, str):
            try:
                parsed = json.loads(tool_input)
                if isinstance(parsed, dict) and parsed:
                    tool_inputs[tool_use_id] = parsed
            except (ValueError, TypeError):
                pass


# The exact shape Strands produces: "" then growing fragments, then the whole
# object. Only the last one is parseable.
COMPLETE = {"operation_id": "checkReservation", "summary": "예약 조회"}
FRAGMENTS = [
    "",
    '{"operation_id"',
    '{"operation_id": "checkRese',
    '{"operation_id": "checkReservation", "summary"',
    json.dumps(COMPLETE, ensure_ascii=False),
]


def test_streamed_string_input_is_captured_as_a_dict():
    """The bug: with an isinstance(dict) guard this map stays EMPTY."""
    tool_inputs: dict = {}
    for frag in FRAGMENTS:
        capture(tool_inputs, "tu-1", frag)

    assert tool_inputs.get("tu-1") == COMPLETE, (
        "tool input was never captured — the UI gets {} and shows no operation name"
    )
    assert tool_inputs["tu-1"]["operation_id"] == "checkReservation"


def test_partial_fragments_alone_capture_nothing():
    """A turn cut off mid-stream must not publish half-parsed arguments."""
    tool_inputs: dict = {}
    for frag in FRAGMENTS[:-1]:  # everything except the complete object
        capture(tool_inputs, "tu-2", frag)

    assert "tu-2" not in tool_inputs


def test_dict_input_is_still_captured():
    """content_block_stop delivers a real dict — that path must keep working."""
    tool_inputs: dict = {}
    capture(tool_inputs, "tu-3", COMPLETE)
    assert tool_inputs["tu-3"] == COMPLETE


def test_a_later_complete_parse_wins():
    """Two tool uses in one turn must not bleed into each other, and the last
    successful parse for an id is the authoritative one."""
    tool_inputs: dict = {}
    capture(tool_inputs, "tu-4", json.dumps({"operation_id": "a"}))
    capture(tool_inputs, "tu-4", json.dumps({"operation_id": "a", "summary": "full"}))
    capture(tool_inputs, "tu-5", json.dumps({"operation_id": "b"}))

    assert tool_inputs["tu-4"] == {"operation_id": "a", "summary": "full"}
    assert tool_inputs["tu-5"] == {"operation_id": "b"}


@pytest.mark.parametrize(
    "hostile",
    [
        '"just a string"',   # valid JSON, not an object
        "[1, 2, 3]",         # valid JSON array
        "42",
        "null",
        "true",
        "{}",                # empty object — nothing to show
        "{not json at all",
    ],
)
def test_non_object_json_is_ignored(hostile):
    """json.loads succeeds on scalars and arrays too. Storing those would put a
    non-dict on the wire and reintroduce the Object.keys() char-index rendering."""
    tool_inputs: dict = {}
    capture(tool_inputs, "tu-6", hostile)
    assert "tu-6" not in tool_inputs


def test_what_goes_on_the_wire_is_always_a_dict():
    """tool_start fires on the FIRST delta, when nothing is parseable yet. It must
    send {} rather than the partial string.

    If a string leaked through, the client's Object.keys(input) would yield
    ("0","1","2",…) and render the input character by character — the display bug
    seen in the prod transcript.
    """
    tool_inputs: dict = {}
    capture(tool_inputs, "tu-7", '{"operation_id"')  # unparseable fragment

    on_the_wire = tool_inputs.get("tu-7", {})

    assert isinstance(on_the_wire, dict)
    assert on_the_wire == {}
    # The failure mode, made explicit: char indices instead of field names.
    assert list(on_the_wire.keys()) != ["0", "1", "2"]


def test_app_py_does_not_reintroduce_the_isinstance_dict_guard():
    """Guard the actual source: the one-line regression that caused this bug was
    ``if tool_use_id and isinstance(tool_input, dict) and tool_input:``. If that
    exact shape comes back, the streamed string is dropped again.
    """
    import os

    app_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app.py")
    with open(app_py, encoding="utf-8") as f:
        src = f.read()

    assert "isinstance(tool_input, dict) and tool_input" not in src, (
        "the dict-only guard is back — streamed tool inputs will be dropped again"
    )
    # And the string branch must still be there.
    assert "isinstance(tool_input, str)" in src
