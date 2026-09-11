"""Bundle-load normalization: flows generated before a live contract fix stay deployable."""
from tools.acxd_bundle import normalize_flow_for_service


def test_kb_node_gets_name_from_placeholder_and_loses_unknown_keys():
    flow = {"flowId": "Fallback", "nodes": {
        "n1": {"type": "knowledge_base", "metadata": {
            "knowledgeBase": {"knowledgeBaseId": "{KB:SunnyHotelFAQ}", "scopeTags": []}, "maxRetries": 2}},
        "n2": {"type": "end", "metadata": {}},
    }}
    out = normalize_flow_for_service(flow)
    assert out["nodes"]["n1"]["metadata"]["knowledgeBase"] == {
        "knowledgeBaseId": "{KB:SunnyHotelFAQ}", "name": "SunnyHotelFAQ"}
    # non-SDK metadata (`maxRetries`) is dropped on load — the SDK would drop it anyway
    assert "maxRetries" not in out["nodes"]["n1"]["metadata"]
    assert out["nodes"]["n2"] == {"type": "end"}


def test_explicit_name_is_kept_and_non_dicts_pass_through():
    flow = {"nodes": {"a": {"type": "knowledge_base", "metadata": {
        "knowledgeBase": {"knowledgeBaseId": "kb-1", "name": "Given"}}}}}
    assert normalize_flow_for_service(flow)["nodes"]["a"]["metadata"]["knowledgeBase"]["name"] == "Given"
    assert normalize_flow_for_service("not-a-flow") == "not-a-flow"
