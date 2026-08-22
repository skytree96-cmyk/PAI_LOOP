from __future__ import annotations

import json

import httpx
import pytest

from pai_loop.integrations.openai_extraction import (
    CORRECTIVE_PROMPT_VERSION,
    OpenAIExtractionClient,
)


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
        "quantitative_tables": [],
        "quantitative_table_not_applicable": None,
        "missing_or_unreadable": [],
        "summary": "지역 제한 조건 한 건",
    }


def response_payload(output: dict) -> dict:
    return {
        "id": "resp_synthetic",
        "status": "completed",
        "model": "gpt-5.6-luna",
        "service_tier": "default",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(output, ensure_ascii=False)}],
            }
        ],
    }


def response_payload_with_usage(
    output: dict,
    *,
    input_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int = 0,
    output_tokens: int,
    reasoning_tokens: int,
) -> dict:
    payload = response_payload(output)
    payload["usage"] = {
        "input_tokens": input_tokens,
        "input_tokens_details": {
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
        },
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "total_tokens": input_tokens + output_tokens,
    }
    return payload


def quantitative_output(*, total_quote: str = "정량평가 총점 10점") -> dict:
    output = valid_output()
    output["quantitative_tables"] = [
        {
            "table_id": "TABLE-1",
            "label": "정량평가",
            "criteria": [
                {
                    "criterion_id": "Q-CREDIT-1",
                    "label": "신용평가",
                    "criterion_literal": "신용평가 10점",
                    "max_points": 10,
                    "scoring_method": "BRACKET",
                    "metric": "CREDIT_RATING",
                    "unit": "등급",
                    "brackets": [
                        {
                            "label": "A등급",
                            "literal": "A등급 10점",
                            "min_value": None,
                            "max_value": None,
                            "min_inclusive": False,
                            "max_inclusive": False,
                            "points": 10,
                            "evidence": {
                                "attachment_id": "ATT-1",
                                "page": 4,
                                "section": "정량평가",
                                "quote": "A등급 10점",
                                "confidence": 0.99,
                            },
                        }
                    ],
                    "threshold": None,
                    "formula_literal": None,
                    "required_evidence": ["company.credit_rating"],
                    "evidence": {
                        "attachment_id": "ATT-1",
                        "page": 4,
                        "section": "정량평가",
                        "quote": "신용평가 10점",
                        "confidence": 0.99,
                    },
                    "ambiguity_reason": None,
                }
            ],
            "total_points": 10,
            "total_evidence": {
                "attachment_id": "ATT-1",
                "page": 4,
                "section": "정량평가",
                "quote": total_quote,
                "confidence": 0.99,
            },
            "minimum_score": None,
            "minimum_evidence": None,
            "ambiguity_reason": None,
        }
    ]
    return output


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
    assert captured["service_tier"] == "default"
    assert captured["max_output_tokens"] == 12_000
    assert captured["text"]["format"]["type"] == "json_schema"
    assert captured["text"]["format"]["strict"] is True
    payload_schema = captured["text"]["format"]["schema"]
    assert {"quantitative_tables", "quantitative_table_not_applicable"} <= set(
        payload_schema["required"]
    )
    assert set(payload_schema["required"]) == set(payload_schema["properties"])
    assert payload_schema["additionalProperties"] is False
    for definition in payload_schema["$defs"].values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
            assert set(definition["required"]) == set(definition["properties"])
    evidence_schema = captured["text"]["format"]["schema"]["$defs"]["EvidenceAnchor"]
    assert set(evidence_schema["required"]) == {
        "attachment_id", "page", "section", "quote", "confidence"
    }
    assert "PASS" in captured["input"][0]["content"][0]["text"]
    system_prompt = captured["input"][0]["content"][0]["text"]
    assert "GO/NO-GO" in system_prompt
    assert "never use company data" in system_prompt
    assert "estimate attained points" in system_prompt
    assert "human-readable fields in Korean" in system_prompt
    assert "never translate or paraphrase a quote" in system_prompt
    assert "Keep those derived fields concise" in system_prompt
    user_prompt = captured["input"][1]["content"][0]["text"]
    assert "normally 5-120 characters" in user_prompt
    assert "verify each quote can be found verbatim" in user_prompt
    assert "never calculate a company score" in user_prompt
    assert "metric UNKNOWN" in user_prompt
    assert "company.performance.amount" in user_prompt
    assert "never create a new key" in user_prompt
    assert "never reverse 이상/초과/이하/미만" in user_prompt


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
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response_payload(output))

    client = OpenAIExtractionClient(
        api_key="key",
        transport=httpx.MockTransport(handler),
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
    expected_calls = 2 if expected_error == "UNVERIFIED_QUOTE" else 1
    assert calls == outcome.api_calls == expected_calls
    assert outcome.corrective_retry_used is (expected_calls == 2)


def test_unverified_quote_gets_one_bounded_corrective_retry() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        output = (
            valid_output(quote="원문에 없는 재구성 문장")
            if len(calls) == 1
            else valid_output(quote="부산광역시에 소재한 업체")
        )
        return httpx.Response(200, json=response_payload(output))

    client = OpenAIExtractionClient(
        api_key="key",
        transport=httpx.MockTransport(handler),
        base_url="https://api.openai.test/v1",
        max_retries=0,
    )
    outcome = client.extract(
        document_text="참가자격: 부산광역시에 소재한 업체",
        allowed_attachment_ids={"ATT-1"},
    )
    client.close()

    assert outcome.status == "ACCEPTED"
    assert outcome.api_calls == len(calls) == 2
    assert outcome.corrective_retry_used is True
    assert outcome.correction_prompt_version == CORRECTIVE_PROMPT_VERSION
    corrective_text = calls[1]["input"][1]["content"][0]["text"]
    assert "FINAL CORRECTIVE RETRY" in corrective_text
    assert "No fuzzy or semantic matching" in corrective_text


def test_usage_and_wall_latency_are_aggregated_across_corrective_attempts() -> None:
    calls = 0
    clock_values = iter([1.0, 1.123, 2.0, 2.234])

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        output = (
            valid_output(quote="원문에 없는 재구성 문장")
            if calls == 1
            else valid_output(quote="부산광역시에 소재한 업체")
        )
        return httpx.Response(
            200,
            json=response_payload_with_usage(
                output,
                input_tokens=100 if calls == 1 else 50,
                cached_tokens=20 if calls == 1 else 0,
                cache_write_tokens=10 if calls == 1 else 0,
                output_tokens=40 if calls == 1 else 20,
                reasoning_tokens=10 if calls == 1 else 5,
            ),
        )

    client = OpenAIExtractionClient(
        api_key="key",
        transport=httpx.MockTransport(handler),
        base_url="https://api.openai.test/v1",
        max_retries=0,
        monotonic=lambda: next(clock_values),
    )
    outcome = client.extract(
        document_text="참가자격: 부산광역시에 소재한 업체",
        allowed_attachment_ids={"ATT-1"},
    )
    client.close()

    telemetry = outcome.openai_telemetry
    assert outcome.status == "ACCEPTED"
    assert outcome.api_calls == telemetry.api_calls == 2
    assert telemetry.usage_reported_calls == 2
    assert telemetry.usage_unreported_calls == 0
    assert telemetry.input_tokens == 150
    assert telemetry.cached_input_tokens == 20
    assert telemetry.cache_write_tokens == 10
    assert telemetry.output_tokens == 60
    assert telemetry.reasoning_output_tokens == 15
    assert telemetry.total_tokens == 210
    assert telemetry.total_request_latency_ms == 357
    assert telemetry.models == ["gpt-5.6-luna"]
    assert telemetry.service_tiers == ["default"]
    assert [item.request_latency_ms for item in telemetry.attempts] == [123, 234]
    assert [item.attempt for item in telemetry.attempts] == [1, 2]
    serialised = outcome.model_dump_json()
    assert "key" not in serialised


def test_quantitative_anchor_uses_the_same_bounded_corrective_quote_retry() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        total_quote = "원문에 없는 총점 10점" if calls == 1 else "정량평가 총점 10점"
        return httpx.Response(
            200,
            json=response_payload(quantitative_output(total_quote=total_quote)),
        )

    client = OpenAIExtractionClient(
        api_key="key",
        transport=httpx.MockTransport(handler),
        base_url="https://api.openai.test/v1",
        max_retries=0,
    )
    outcome = client.extract(
        document_text=(
            "부산광역시에 소재한 업체\n신용평가 10점\nA등급 10점\n정량평가 총점 10점"
        ),
        allowed_attachment_ids={"ATT-1"},
    )
    client.close()

    assert outcome.status == "ACCEPTED"
    assert outcome.api_calls == calls == 2
    assert outcome.corrective_retry_used is True
    assert outcome.data is not None
    assert outcome.data.quantitative_tables[0].total_points == 10


def test_new_quantitative_fields_are_required_by_strict_output_schema() -> None:
    output = valid_output()
    del output["quantitative_tables"]
    client = OpenAIExtractionClient(
        api_key="key",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=response_payload(output))
        ),
        base_url="https://api.openai.test/v1",
        max_retries=0,
    )
    outcome = client.extract(
        document_text="부산광역시에 소재한 업체",
        allowed_attachment_ids={"ATT-1"},
    )
    client.close()

    assert outcome.status == "REVIEW"
    assert outcome.error_code == "SCHEMA_VALIDATION_ERROR"


def test_total_api_call_budget_includes_transport_retry_and_blocks_third_call() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": "retry"})
        return httpx.Response(
            200,
            json=response_payload(valid_output(quote="원문에 존재하지 않는 문장")),
        )

    client = OpenAIExtractionClient(
        api_key="key",
        transport=httpx.MockTransport(handler),
        base_url="https://api.openai.test/v1",
        max_retries=2,
        sleep=lambda _seconds: None,
    )
    outcome = client.extract(
        document_text="부산광역시에 소재한 업체",
        allowed_attachment_ids={"ATT-1"},
    )
    client.close()

    assert outcome.status == "REVIEW"
    assert outcome.error_code == "UNVERIFIED_QUOTE"
    assert outcome.api_calls == calls == 2
    assert outcome.corrective_retry_used is False
    assert outcome.openai_telemetry.api_calls == 2
    assert outcome.openai_telemetry.usage_reported_calls == 0
    assert outcome.openai_telemetry.usage_unreported_calls == 2
    assert outcome.openai_telemetry.total_tokens is None


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
