"""Regression test: business outcomes must be HTTP 200, not 4xx.

Reported from a real build (2026-08-05). The AI agent authenticated callers with
accountId + the last 4 SSN digits. On a successful match the generated Lambda
returned 200 — correct. On a MISMATCH it returned 400/403/409, and the Amazon
Connect AI agent then told the customer "there is an issue with the tool" instead
of "those digits don't match, try again".

Cause: Connect treats any non-2xx tool response as an execution failure. It never
reads the body, so it cannot relay the outcome. Every negative path had to be
corrected to 200 by hand before the agent worked.

Fixed in two layers:
  1. BUSINESS_OUTCOME_200_RULE in agents/_consistency_rules.py (shared by the
     Lambda, OpenAPI and prompt generators) + the per-generator prompts, whose
     own examples previously taught `create_response(400, ...)`.
  2. ``lint_lambda_status_codes`` — a deterministic AST autofix that rewrites
     4xx → 200 before the asset is streamed or persisted. The prompt is the
     instruction; the linter is the guarantee.

5xx is deliberately preserved: that is a genuine fault, and Connect's retry on
5xx is the behaviour we want.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.asset_linters import (  # noqa: E402
    lint_lambda_status_codes,
    lint_python_source,
)


def _codes(code: str) -> list[int]:
    """Every status code passed to a create_response(...) call."""
    return [int(m) for m in re.findall(r"create_response\((\d+)", code)]


# ---------------------------------------------------------------------------
# The reported case, end to end
# ---------------------------------------------------------------------------

# Condensed from the handler that was reported: caller verification by
# accountNumber + last 4 SSN digits, with attempt tracking and lockout.
REPORTED_HANDLER = '''
import json


def create_response(status_code: int, body: dict) -> dict:
    return {"statusCode": status_code, "body": json.dumps(body)}


def handler(event, context):
    try:
        body = json.loads(event.get("body", "{}"))
        account_number = body.get("accountNumber")
        last_four_digits = body.get("lastFourDigits")

        if not account_number or not last_four_digits:
            return create_response(400, {
                "verified": False,
                "error": "accountNumber and lastFourDigits are required"
            })

        current_attempts = _attempt_tracker.get(account_number, 0)
        if current_attempts >= MAX_ATTEMPTS:
            return create_response(403, {
                "verified": False, "remainingAttempts": 0, "lockout": True
            })

        item = lookup(account_number)
        if not item:
            return create_response(404, {"verified": False, "found": False})

        if last_four_digits == item.get("lastFourDigits", ""):
            return create_response(200, {"verified": True, "lockout": False})
        else:
            return create_response(409, {
                "verified": False, "remainingAttempts": 1, "lockout": False
            })

    except Exception as e:
        return create_response(500, {"verified": False, "error": "Internal server error"})
'''


def test_the_reported_bug_every_business_outcome_becomes_200():
    """400 (missing input), 403 (lockout), 404 (no account), 409 (mismatch) → 200."""
    result = lint_lambda_status_codes(REPORTED_HANDLER)

    codes = _codes(result["fixed_code"])
    assert 400 not in codes
    assert 403 not in codes
    assert 404 not in codes
    assert 409 not in codes
    assert len(result["fixes_applied"]) == 4


def test_the_genuine_fault_path_stays_500():
    """Connect retries 5xx, which is right for a transient fault. Never rewrite it."""
    result = lint_lambda_status_codes(REPORTED_HANDLER)

    assert 500 in _codes(result["fixed_code"]), "the 500 fault path must be preserved"


def test_the_success_path_is_untouched():
    result = lint_lambda_status_codes(REPORTED_HANDLER)

    assert _codes(result["fixed_code"]).count(200) == 5  # 1 original + 4 rewritten


def test_the_rewrite_still_compiles():
    """A repair that breaks the handler would be far worse than the bug."""
    result = lint_lambda_status_codes(REPORTED_HANDLER)

    assert lint_python_source(result["fixed_code"])["ok"]


def test_the_rewrite_changes_only_the_status_literals():
    """Position-exact edits: no reflowing, no lost comments, no lost strings."""
    result = lint_lambda_status_codes(REPORTED_HANDLER)
    before = REPORTED_HANDLER.splitlines()
    after = result["fixed_code"].splitlines()

    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(differing) == 4
    for i in differing:
        # Only the number changed on each differing line.
        assert re.sub(r"\d+", "N", before[i]) == re.sub(r"\d+", "N", after[i])


def test_bodies_and_comments_survive_verbatim():
    """The business payload must be preserved exactly — only the code changes."""
    result = lint_lambda_status_codes(REPORTED_HANDLER)

    for fragment in (
        '"accountNumber and lastFourDigits are required"',
        '"remainingAttempts": 1',
        '"lockout": True',
        'json.loads(event.get("body", "{}"))',
    ):
        assert fragment in result["fixed_code"]


# ---------------------------------------------------------------------------
# Shapes the generators actually emit
# ---------------------------------------------------------------------------

def test_a_bare_statuscode_dict_is_rewritten():
    """Not every handler routes through a create_response helper."""
    code = 'def handler(e, c):\n    return {"statusCode": 404, "body": "{}"}\n'

    result = lint_lambda_status_codes(code)

    assert '"statusCode": 200' in result["fixed_code"]
    assert result["fixes_applied"]


def test_a_status_code_keyword_argument_is_rewritten():
    code = (
        "def handler(e, c):\n"
        '    return create_response(status_code=403, body={"verified": False})\n'
    )

    result = lint_lambda_status_codes(code)

    assert "status_code=200" in result["fixed_code"]


def test_a_helper_named_something_else_is_still_covered():
    """Generators emit _response / make_response / build_response too."""
    code = 'def handler(e, c):\n    return make_response(409, {"available": False})\n'

    result = lint_lambda_status_codes(code)

    assert "make_response(200" in result["fixed_code"]


def test_a_clean_handler_is_returned_untouched():
    """No 4xx means no edit and no re-stream — idempotence matters, because a
    reported fix triggers a full re-stream to the frontend."""
    code = (
        "def handler(e, c):\n"
        '    return create_response(200, {"verified": True})\n'
    )

    result = lint_lambda_status_codes(code)

    assert result["fixed_code"] == code
    assert result["fixes_applied"] == []


def test_running_it_twice_changes_nothing_further():
    once = lint_lambda_status_codes(REPORTED_HANDLER)["fixed_code"]
    twice = lint_lambda_status_codes(once)

    assert twice["fixed_code"] == once
    assert twice["fixes_applied"] == []


def test_201_and_other_2xx_are_left_alone():
    """A create is legitimately 201; only 4xx is the bug."""
    code = 'def handler(e, c):\n    return create_response(201, {"success": True})\n'

    result = lint_lambda_status_codes(code)

    assert "create_response(201" in result["fixed_code"]
    assert result["fixes_applied"] == []


# ---------------------------------------------------------------------------
# Discriminator warning — a 200 the model can't interpret is no better
# ---------------------------------------------------------------------------

def test_a_200_body_with_no_discriminator_warns():
    """Rewriting 404 → 200 on a body of only {"error": ...} would make failure
    indistinguishable from success. Fix the code, but flag the body."""
    code = (
        "def handler(e, c):\n"
        '    return create_response(404, {"message": "not found"})\n'
    )

    result = lint_lambda_status_codes(code)

    assert result["fixes_applied"]
    assert result["warnings"], "a 200 with no outcome field must be flagged"


def test_a_body_with_a_discriminator_does_not_warn():
    code = (
        "def handler(e, c):\n"
        '    return create_response(404, {"found": False, "message": "not found"})\n'
    )

    result = lint_lambda_status_codes(code)

    assert result["fixes_applied"]
    assert result["warnings"] == []


def test_a_body_built_by_a_helper_is_given_the_benefit_of_the_doubt():
    """We cannot see into a helper call; a false warning is worse than none."""
    code = "def handler(e, c):\n    return create_response(404, build_body(x))\n"

    result = lint_lambda_status_codes(code)

    assert result["warnings"] == []


# ---------------------------------------------------------------------------
# Fault tolerance — linting must never break the pipeline
# ---------------------------------------------------------------------------

def test_syntactically_invalid_source_passes_through_unchanged():
    """Syntax is lint_python_source's job. This must not raise or mangle."""
    broken = 'def handler(e, c):\n    return create_response(404, {"a": \n'

    result = lint_lambda_status_codes(broken)

    assert result["fixed_code"] == broken
    assert result["fixes_applied"] == []


@pytest.mark.parametrize("src", ["", "\n", "# just a comment\n", "x = 1"])
def test_degenerate_input_is_safe(src):
    result = lint_lambda_status_codes(src)

    assert result["fixed_code"] == src


def test_non_ascii_content_does_not_shift_offsets():
    """Column offsets are byte-vs-char sensitive; Korean strings are the norm in
    these generated handlers, so pin that the right literal is replaced."""
    code = (
        "def handler(e, c):\n"
        '    return create_response(403, {"verified": False, "message": "인증에 실패했습니다"})\n'
    )

    result = lint_lambda_status_codes(code)

    assert "create_response(200" in result["fixed_code"]
    assert "인증에 실패했습니다" in result["fixed_code"]
    assert lint_python_source(result["fixed_code"])["ok"]


# ---------------------------------------------------------------------------
# The prompts must agree with the linter
# ---------------------------------------------------------------------------

def _read(*parts: str) -> str:
    with open(os.path.join(_SRC, *parts), encoding="utf-8") as f:
        return f.read()


def test_the_shared_golden_rule_exists():
    """All three affected generators import CONSISTENCY_RULES, so the rule lives
    there rather than being duplicated per prompt."""
    rules = _read("agents", "_consistency_rules.py")

    assert "BUSINESS_OUTCOME_200_RULE" in rules


def test_no_generator_example_still_teaches_a_4xx_business_outcome():
    """FEW_SHOT_TRUST_RULE says examples must not contradict the rules — but a
    model copies the example it can see. The Lambda prompt used to return 400 on
    a missing field, which is exactly the reported bug.
    """
    prompt = _read("agents", "lambda_generator", "system_prompt.py")

    # A 4xx is allowed only inside a block explicitly marked as the wrong way.
    # The ❌ marker sits on the comment line that opens the block, so track it
    # until the matching ✅ block starts.
    offenders = []
    in_wrong_block = False
    for line in prompt.splitlines():
        if "❌" in line:
            in_wrong_block = True
        elif "✅" in line:
            in_wrong_block = False
        if re.search(r"create_response\(4\d\d", line) and not in_wrong_block:
            offenders.append(line.strip())

    assert not offenders, (
        "these examples teach a 4xx business outcome without marking it wrong:\n"
        + "\n".join(offenders)
    )


def test_the_openapi_prompt_no_longer_maps_outcomes_to_4xx():
    """A declared 404/409 response teaches the model to expect a failure it
    cannot handle, so the OpenAPI templates must not declare them either."""
    prompt = _read("agents", "openapi_generator", "system_prompt.py")

    declared = re.findall(r"^\s+'(4\d\d)':", prompt, re.MULTILINE)

    assert not declared, f"OpenAPI templates still declare 4xx responses: {declared}"


def test_the_orchestrator_prompt_states_the_rule_for_the_spec():
    """The OperationSpec is the contract every generator reads (docs/agentic-ai.md),
    so the orchestrator must record 200 in error_responses[].status_code — a 403
    there would propagate to every asset."""
    prompt = _read("prompts", "system_prompt.py")

    assert "BUSINESS_OUTCOME_200_RULE" in prompt
    assert "error_responses" in prompt


# ---------------------------------------------------------------------------
# Spec layer — the upstream source of truth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [400, 401, 403, 404, 409, 429, "403"])
def test_the_spec_coerces_a_4xx_business_outcome_to_200(bad):
    """Enforced in the model, not just asked for in the prompt: the spec is what
    the Lambda, OpenAPI and prompt generators all read."""
    from tools.spec_manager import ErrorResponse

    assert ErrorResponse(status_code=bad).status_code == 200


@pytest.mark.parametrize("keep", [200, 201, 500, 503])
def test_the_spec_preserves_2xx_and_5xx(keep):
    """5xx must survive — Connect retries it, which is right for a real fault."""
    from tools.spec_manager import ErrorResponse

    assert ErrorResponse(status_code=keep).status_code == keep


def test_the_spec_accepts_the_llms_alias_spellings():
    """The orchestrator is an LLM; statusCode/status/code all reach this field."""
    from tools.spec_manager import ErrorResponse

    for alias in ("status_code", "statusCode", "status", "code"):
        assert ErrorResponse(**{alias: 404}).status_code == 200


def test_a_missing_status_code_stays_none():
    """Absent is not the same as wrong — don't invent a code."""
    from tools.spec_manager import ErrorResponse

    assert ErrorResponse(error_code="NOT_FOUND").status_code is None


# ---------------------------------------------------------------------------
# OpenAPI generator — must not inject 4xx of its own
# ---------------------------------------------------------------------------

def test_the_openapi_generator_declares_no_4xx_responses():
    """It used to add 400 (and 401 when auth was on) unconditionally, so even a
    spec with only 200s produced a document that taught the model to expect a
    failure it cannot handle."""
    from tools.openapi_generator import _build_responses
    from tools.spec_manager import OperationSpec

    spec = OperationSpec(
        operation_id="verifyCaller", operation_type="custom", http_method="POST",
        path="/tools/verify_caller", summary="s", description="d",
        input_fields=[], output_fields=[], requires_authentication=True,
        error_responses=[
            {"status_code": 403, "error_code": "UNAUTHORIZED", "message": "no match"},
            {"status_code": 404, "error_code": "NOT_FOUND", "message": "no account"},
            {"status_code": 500, "error_code": "INTERNAL_ERROR", "message": "boom"},
        ],
    )

    responses = _build_responses(spec, "VerifyCallerResponse")

    assert not [c for c in responses if c.startswith("4")], (
        f"4xx responses declared: {sorted(responses)}"
    )
    assert "200" in responses and "500" in responses
    # The outcomes still reach the model — folded into the 200 description.
    assert "UNAUTHORIZED" in responses["200"]["description"]
    assert "NOT_FOUND" in responses["200"]["description"]
