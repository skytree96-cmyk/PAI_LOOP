from __future__ import annotations

import json

import httpx
import pytest

from pai_loop.integrations.openai_extraction import OpenAIExtractionClient


def valid_output(*, attachment_id: str = "ATT-1", quote: str = "부산광역시에 소재한 업체") -> dict:
    return {
        "document_type": "RFP",
        "requirements": [
            {
                "requirement_id": "REQ-REGION-1",
                "category": "REGION",
                "logic": "SINGLE",
                "normalized_condition": "본점이 부산광역시에 소재",
                "mandatory": True,
                "deadline_basis": "입찰공고 마감일",
                "evidence": [
                    {
                        "attachment_id": attachment_id,
                        "page": 3,
                        "section": "참가자격",
                        "quote": quote,
                        "confidence": 0.98,
                    }
                ],
                "ambiguity_reason": None,
            }
        ],
        "missing_or_unreadable": [],
        "summary": "지역 제한 조건 한 건",
    }


def response_payload(output: dict) -> dict:
    return {
        "id": "resp_synthetic",
        "status": "completed",
        "model": "gpt-5.6-luna",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(output, ensure_ascii=False)}],
            }
        ],
    }


def test_strict_store_false_request_and_anchor_validation() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-server-key"
        return httpx.Response(200, json=response_payload(valid_output()))

    with OpenAIExtractionClient(
        api_key="test-server-key",
        transport=httpx.MockTransport(handler),
        base_url="https://api.openai.test/v1",
    ) as client:
        outcome = client.extract(
            document_text="참가자격: 부산광역시에 소재한 업체만 참여할 수 있다.",
            allowed_attachment_ids={"ATT-1"},
        )

    assert outcome.status == "ACCEPTED"
    assert outcome.data is not None
    assert outcome.data.requirements[0].category == "REGION"
    assert captured["store"] is False
    assert captured["max_output_tokens"] == 12_000
    assert captured["text"]["format"]["type"] == "json_schema"
    assert captured["text"]["format"]["strict"] is True
    evidence_schema = captured["text"]["format"]["schema"]["$defs"]["EvidenceAnchor"]
    assert set(evidence_schema["required"]) == {
        "attachment_id", "page", "section", "quote", "confidence"
    }
    assert "PASS" in captured["input"][0]["content"][0]["text"]
    system_prompt = captured["input"][0]["content"][0]["text"]
    assert "human-readable fields in Korean" in system_prompt
    assert "never translate or paraphrase a quote" in system_prompt
    assert "Keep those derived fields concise" in system_prompt
    user_prompt = captured["input"][1]["content"][0]["text"]
    assert "normally 5-120 characters" in user_prompt
    assert "verify each quote can be found verbatim" in user_prompt


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"id": "x", "status": "incomplete", "model": "m", "output": []}, "INCOMPLETE_RESPONSE"),
        (
            {
                "id": "x",
                "status": "completed",
                "model": "m",
                "output": [{"content": [{"type": "refusal", "refusal": "cannot"}]}],
            },
            "MODEL_REFUSAL",
        ),
    ],
)
def test_incomplete_or_refusal_routes_to_r07_review(payload: dict, expected_error: str) -> None:
    client = OpenAIExtractionClient(
        api_key="key",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
        base_url="https://api.openai.test/v1",
    )
    outcome = client.extract(document_text="synthetic document", allowed_attachment_ids={"ATT-1"})
    client.close()
    assert outcome.status == "REVIEW"
    assert outcome.review_code == "R07"
    assert outcome.error_code == expected_error


@pytest.mark.parametrize(
    ("output", "expected_error"),
    [
        (valid_output(attachment_id="UNKNOWN"), "UNKNOWN_ATTACHMENT"),
        (valid_output(quote="원문에 존재하지 않는 문장"), "UNVERIFIED_QUOTE"),
    ],
)
def test_untrusted_anchor_never_reaches_decision_engine(output: dict, expected_error: str) -> None:
    client = OpenAIExtractionClient(
        api_key="key",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=response_payload(output))
        ),
        base_url="https://api.openai.test/v1",
    )
    outcome = client.extract(
        document_text="부산광역시에 소재한 업체",
        allowed_attachment_ids={"ATT-1"},
    )
    client.close()
    assert outcome.status == "REVIEW"
    assert outcome.error_code == expected_error
    assert outcome.data is None


@pytest.mark.parametrize(
    ("document_text", "quote", "accepted"),
    [
        ("부산광역시에 소재한 업체", "부산광역시에 소재한 업체", True),
        ("입찰\u200b참가 자격 등록을 완료해야 합니다.", "입찰참가 자격 등록을 완료해야 합니다.", True),
        ("입 찰 참 가 자 격 등 록 을 완료해야 합니다.", "입찰참가자격등록을 완료해야 합니다.", True),
        ("부 산 업 체", "부산업체", False),
        ("입찰참가자격등록을 완료해야 합니다.", "별도 회사 증빙 불필요", False),
    ],
)
def test_quote_verification_allows_only_substantial_formatting_variance(
    document_text: str,
    quote: str,
    accepted: bool,
) -> None:
    client = OpenAIExtractionClient(
        api_key="key",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json=response_payload(valid_output(quote=quote)),
            )
        ),
        base_url="https://api.openai.test/v1",
    )
    outcome = client.extract(
        document_text=document_text,
        allowed_attachment_ids={"ATT-1"},
    )
    client.close()

    assert (outcome.status == "ACCEPTED") is accepted
    if not accepted:
        assert outcome.error_code == "UNVERIFIED_QUOTE"


def test_http_error_does_not_expose_server_key() -> None:
    client = OpenAIExtractionClient(
        api_key="DO-NOT-LEAK",
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, text="bad key")),
        base_url="https://api.openai.test/v1",
        max_retries=0,
    )
    outcome = client.extract(document_text="synthetic", allowed_attachment_ids=set())
    client.close()
    assert outcome.status == "REVIEW"
    assert outcome.error_code == "HTTP_ERROR"
    assert "DO-NOT-LEAK" not in outcome.model_dump_json()
