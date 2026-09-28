"""FAQ source documents become knowledge-base articles with their WHOLE answer.

Live (5-use-case run, 2026-09-28): the FAQ generator writes the answer's
details under '## ' headings of its own, inside the answer it saves. The ACXD
parser ended the answer at the first of them, so every SELC and GreenCart
article kept a single 18-58 character lead sentence and the knowledge base
answered "반품 정책이 어떻게 되나요?" and "세척 서비스는 어떤 종류가 있나요?"
with nothing. Daon, Hanul and TableNow wrote bold labels instead and were whole.
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.acxd_generation_context import _article_from_content, _load_faq_articles  # noqa: E402
from tools.validate_acxd_consistency import normalize_bundle_knowledge_articles  # noqa: E402

# Shortened from the live GreenCart document (01_return_policy.md).
GREENCART_RETURN_POLICY = """# 그린카트 반품 정책 안내 (14일 이내 반품)

## 질문 (Question)
반품 정책이 어떻게 되나요? 반품은 언제까지 가능하고 배송비는 누가 부담하나요?

## 답변 (Answer)
그린카트의 반품 정책을 안내해 드립니다.

## 반품 가능 기간
- 반품은 **배송완료일로부터 14일 이내**에 신청하실 수 있습니다.

## 배송비 부담 기준
- **단순변심**으로 반품하시는 경우: 왕복 배송비 **3,000원을 고객님께서 부담**하시게 됩니다.

배송완료가 아니거나 14일이 지난 경우에는 반품이 어려우며, 이 경우 상담원 연결을 도와드리겠습니다.

## 관련 정보 (Related Information)
- 교환·반품 불가 품목

## 메타데이터 (Metadata)
- 카테고리: 정책 (반품)
- 키워드: 반품, 반품정책, 14일
- 최종 업데이트: 2026-09-28
"""

# The form the Hanul run wrote: bold labels, no headings inside the answer.
HANUL_DOCUMENTS = """# 서류 제출 방법

## 질문 (Question)
서류는 어떻게 제출하나요?

## 답변 (Answer)
청구 접수 후 필요한 서류는 다음 방법으로 제출하실 수 있습니다.

**제출 방법**
- **모바일 앱**: 서류를 촬영하여 첨부합니다.

## 관련 정보 (Related Information)
- 청구 절차

## 메타데이터 (Metadata)
- 키워드: 서류, 제출
"""


def test_the_answer_keeps_the_sub_sections_the_generator_wrote_inside_it():
    article = _article_from_content(GREENCART_RETURN_POLICY)

    answer = article["answer"]
    assert answer.startswith("그린카트의 반품 정책을 안내해 드립니다.")
    assert "**반품 가능 기간**" in answer and "14일 이내" in answer
    assert "**배송비 부담 기준**" in answer and "3,000원" in answer
    assert answer.endswith("상담원 연결을 도와드리겠습니다.")
    # the document's own trailing sections are not part of the answer
    assert "관련 정보" not in answer and "카테고리" not in answer and "교환·반품 불가" not in answer
    assert not any(line.startswith("#") for line in answer.splitlines())
    assert article["question"] == "반품 정책이 어떻게 되나요? 반품은 언제까지 가능하고 배송비는 누가 부담하나요?"
    assert article["tags"] == ["반품", "반품정책", "14일"]


def test_an_answer_without_headings_is_read_as_before():
    article = _article_from_content(HANUL_DOCUMENTS)

    assert article["answer"] == ("청구 접수 후 필요한 서류는 다음 방법으로 제출하실 수 있습니다.\n\n"
                                 "**제출 방법**\n- **모바일 앱**: 서류를 촬영하여 첨부합니다.")


def test_the_answer_ends_at_the_metadata_when_related_information_is_missing():
    document = GREENCART_RETURN_POLICY.replace("## 관련 정보 (Related Information)\n- 교환·반품 불가 품목\n\n", "")

    answer = _article_from_content(document)["answer"]
    assert answer.endswith("상담원 연결을 도와드리겠습니다.")
    assert "카테고리" not in answer


def test_english_headings_and_the_minimal_document():
    assert _article_from_content("## Question\nq\n## Answer\na\n") == {"question": "q", "answer": "a", "tags": []}
    document = ("## Question\nWhen are you open?\n\n## Answer\nEvery day.\n\n## Hours\n"
                "Nine to six.\n\n## Related Information\n- Holidays\n\n## Metadata\n- Keywords: hours\n")
    article = _article_from_content(document)
    assert article["answer"] == "Every day.\n\n**Hours**\nNine to six."
    assert article["tags"] == []


def test_a_json_document_is_taken_as_it_is():
    document = json.dumps({"question": "Q?", "answer": "## Not a heading here", "tags": ["t"]})
    assert _article_from_content(document) == {"question": "Q?", "answer": "## Not a heading here", "tags": ["t"]}


def test_the_loader_reads_through_the_storage_calls_it_is_given():
    contents = {
        "assets/s/faq/knowledge_base/01_return_policy.md": GREENCART_RETURN_POLICY,
        "assets/s/faq/knowledge_base/cover.png": "not text",
        "assets/s/openapi/openapi.yaml": "## Question\nq\n## Answer\na\n",
    }
    articles = _load_faq_articles("s", list_assets=lambda _sid: list(contents), get_asset=contents.get)

    assert len(articles) == 1 and "3,000원" in articles[0]["answer"]


def _kb(*articles):
    return {"knowledge_bases": [{"name": "greencart-faq", "type": "articles", "articles": [
        {"question": {"text": question}, "responses": [{"type": "text", "body": body}]}
        for question, body in articles]}]}


def test_packaging_restores_an_article_the_old_parser_cut_and_keeps_an_edited_one():
    faq = [_article_from_content(GREENCART_RETURN_POLICY),
           {"question": "영수증 발행이 되나요?", "answer": "네, 발행됩니다. 주문 상세에서 신청하세요."}]
    bundle = _kb(
        ("반품 정책이 어떻게 되나요?  반품은 언제까지 가능하고 배송비는 누가 부담하나요?",
         "그린카트의 반품 정책을 안내해 드립니다."),
        ("영수증 발행이 되나요?", "주문 내역 화면에서 발행할 수 있습니다."),
    )

    notes = normalize_bundle_knowledge_articles(bundle, faq)

    articles = bundle["knowledge_bases"][0]["articles"]
    assert articles[0]["responses"][0]["body"] == faq[0]["answer"]
    # not a prefix of its FAQ answer: an edit, left as it is
    assert articles[1]["responses"][0]["body"] == "주문 내역 화면에서 발행할 수 있습니다."
    assert len(notes) == 1 and "restored to its FAQ answer" in notes[0]
    assert normalize_bundle_knowledge_articles(bundle, faq) == []


def test_packaging_leaves_whole_articles_and_unknown_questions_alone():
    faq = [_article_from_content(HANUL_DOCUMENTS)]
    whole = _article_from_content(HANUL_DOCUMENTS)["answer"]
    bundle = _kb(("서류는 어떻게 제출하나요?", whole), ("다른 질문", "청구"))

    assert normalize_bundle_knowledge_articles(bundle, faq) == []
    assert normalize_bundle_knowledge_articles(bundle, []) == []
    assert normalize_bundle_knowledge_articles({"knowledge_bases": [{"name": "x"}]}, faq) == []
