"""Live (2026-09-14): an orchestrator narrated tool results for tools it never called."""
from tools.tool_claim_audit import unbacked_tool_claims, audit_notice


def test_a_narrated_result_without_the_call_is_reported():
    text = ("`enforce_openapi_contract_tool` 반환값:\n  \"changes\": [...]\n"
            "검증 결과를 그대로 보고드립니다. `reviewer_agent` 반환 JSON: {\"blocking\": []}")
    assert unbacked_tool_claims(text, ["get_acxd_flow_spec_tool"]) == ["enforce_openapi_contract_tool", "reviewer_agent"]
    assert "enforce_openapi_contract_tool" in (audit_notice(text, []) or "")


def test_a_backed_claim_and_plain_mentions_are_quiet():
    text = "`generate_acxd_application`이 실제로 호출됐고, 반환된 결과 JSON은 다음과 같습니다"
    assert unbacked_tool_claims(text, ["generate_acxd_application"]) == []
    # naming the tool as a plan is not a claim that it ran
    assert unbacked_tool_claims("다음 단계로 generate_acxd_application을 사용할 예정입니다.", []) == []
    assert audit_notice("아무 도구도 언급하지 않는 답변입니다.", []) is None


def test_english_claims_are_detected_too():
    text = "reviewer_agent returned {\"blocking\": []} so the gate is open."
    assert unbacked_tool_claims(text, []) == ["reviewer_agent"]
    assert "Verification notice" in audit_notice(text, [], language="en")
