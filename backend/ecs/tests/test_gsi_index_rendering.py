"""Regression test: a GSI name must never render character by character.

Found on prod 2026-08-05 (long-input e2e). The agent's own summary showed

    GSI: p, h, o, n, e, -, i, n, d, e, x

instead of ``phone-index``, and the agent then "corrected" a spec that was
actually stored correctly — burning a turn and re-saving over good data.

Cause: ``DataSourceSpec.gsi_indexes`` is ``Optional[list]``, but the model
sometimes passes a single index as a bare string. Pydantic lets a ``str`` through
(nothing coerces it), and every reader did ``for g in gsi_indexes`` — iterating a
string yields its CHARACTERS. Two independent readers had the bug
(``_spec_to_markdown`` and the review summary).

Fixed on both sides: a ``mode="before"`` validator wraps non-list input, and all
readers go through ``_gsi_name_list`` (specs already on NFS still hold the raw
shape, so the readers can't assume the validator ran).
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.spec_manager import DataSourceSpec, _gsi_name_list  # noqa: E402

# The exact value from the prod transcript.
PROD_GSI = "phone-index"
# What the bug produced.
CHAR_SPLIT = "p, h, o, n, e, -, i, n, d, e, x"


def test_the_original_bug_is_reproducible_without_the_helper():
    """Pin WHY this exists: the naive loop really does split a string.

    If this ever stops being true the helper is redundant — but as long as it
    holds, every raw ``for g in gsi`` loop is a latent instance of this bug.
    """
    naive = [g.get("name", "?") if isinstance(g, dict) else str(g) for g in PROD_GSI]
    assert ", ".join(naive) == CHAR_SPLIT


@pytest.mark.parametrize(
    "raw,expected",
    [
        (PROD_GSI, ["phone-index"]),                                # the prod shape
        ("phone-index, reservation-index", ["phone-index", "reservation-index"]),
        ([{"name": "phone-index"}], ["phone-index"]),               # documented shape
        ([{"index_name": "phone-index"}], ["phone-index"]),         # alt key
        (["phone-index", "res-index"], ["phone-index", "res-index"]),  # list of str
        ({"name": "phone-index"}, ["phone-index"]),                 # bare dict
        (None, []),
        ([], []),
        ("", []),
        ("   ", []),
    ],
)
def test_gsi_name_list_handles_every_shape(raw, expected):
    assert _gsi_name_list(raw) == expected


def test_gsi_name_list_never_splits_a_string_into_characters():
    """The assertion that would have caught the prod bug."""
    out = _gsi_name_list(PROD_GSI)
    assert ", ".join(out) != CHAR_SPLIT
    assert out == ["phone-index"]
    assert all(len(n) > 1 for n in out), f"a name was split into single chars: {out}"


@pytest.mark.parametrize("alias", ["gsi", "gsi_indexes", "gsiIndexes", "indexes", "secondaryIndexes"])
def test_a_bare_string_is_normalized_on_input_through_every_alias(alias):
    """The validator must fire regardless of which alias the model used."""
    spec = DataSourceSpec(**{"table_name": "LoyaltyMembers", alias: PROD_GSI})

    assert isinstance(spec.gsi_indexes, list), f"{alias} left a non-list on the model"
    assert spec.gsi_indexes == [{"name": "phone-index"}]
    assert _gsi_name_list(spec.gsi_indexes) == ["phone-index"]


def test_documented_list_shape_is_untouched():
    """The normalizer must not disturb input that was already correct."""
    proper = [{"name": "phone-index", "partition_key": "phoneNumber"}]
    spec = DataSourceSpec(table_name="LoyaltyMembers", gsi_indexes=proper)
    assert spec.gsi_indexes == proper


def test_empty_string_becomes_none_not_a_bogus_index():
    """"" must not turn into [{'name': ''}] — that renders as an empty GSI name."""
    spec = DataSourceSpec(table_name="T", gsi="")
    assert not spec.gsi_indexes


def test_readers_do_not_iterate_gsi_indexes_directly():
    """Guard the source. Any ``for g in ...gsi...`` loop reintroduces the bug;
    readers must go through _gsi_name_list.
    """
    import re

    path = os.path.join(_SRC, "tools", "spec_manager.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()

    # Allow the loop inside the helper itself; flag it anywhere else.
    helper_start = src.index("def _gsi_name_list")
    helper_end = src.index("class FlexibleBaseModel")
    outside = src[:helper_start] + src[helper_end:]

    bad = re.findall(r"for \w+ in [^\n]*gsi[^\n]*:", outside, flags=re.IGNORECASE)
    assert not bad, f"a reader iterates gsi directly — use _gsi_name_list: {bad}"
