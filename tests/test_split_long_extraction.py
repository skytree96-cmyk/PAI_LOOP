"""Long documents are extracted as two halves through the n8n gateway (2026-10-05)."""
from __future__ import annotations

import json

import httpx
import pytest

from pai_loop.integrations.openai_extraction import (
    SPLIT_EXTRACTION_MIN_CHARS,
    OpenAIExtractionClient,
    split_source_text,
)

FILLER = "평가 및 과업 일반 사항을 설명하는 문단입니다. " * 40


def _source() -> str:
    first = "참가자격: 부산광역시에 소재한 업체\n\n" + (FILLER + "\n\n") * 20
    second = "수행실적은 최근 3년 이내 실적으로 평가한다\n\n" + (FILLER + "\n\n") * 20
    text = first + second
    assert len(text) >= SPLIT_EXTRACTION_MIN_CHARS
    return text


def _output(requirement_id: str, quote: str, gaps: list[str]) -> dict:
    return {
        "document_type": "RFP",
        "requirements": [{
            "requirement_id": requirement_id,
            "category": "REGION",
            "logic": "SINGLE",
            "normalized_condition": quote,
            "mandatory": True,
            "deadline_basis": None,
            "evidence": [{"attachment_id": "ATT-1", "page": None, "section": None,
                          "quote": quote, "confidence": 0.95}],
            "ambiguity_reason": None,
        }],
        "quantitative_tables": [],
        "quantitative_table_not_applicable": None,
        "missing_or_unreadable": gaps,
        "summary": f"{requirement_id} 요약",
    }


def _gateway(outputs: list[dict | None], seen: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        output = outputs[len(seen) - 1]
        if output is None:
            return httpx.Response(500, json={"error": "synthetic"})
        return httpx.Response(200, json={
            "id": f"n8n-{len(seen)}", "status": "completed", "model": "claude-sonnet-5",
            "output_text": json.dumps(output, ensure_ascii=False),
            "usage": {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
        })
    return handler


def _client(handler) -> OpenAIExtractionClient:
    return OpenAIExtractionClient(
        api_key="k", model="claude-sonnet-5", provider="n8n_claude",
        base_url="https://n8n.example/webhook/pai-loop-claude",
        transport=httpx.MockTransport(handler),
    )


def test_split_keeps_every_character_and_cuts_at_a_paragraph() -> None:
    text = _source()
    first, second = split_source_text(text)
    assert first + second == text
    assert first.endswith("\n\n")
    assert 0.35 <= len(first) / len(text) <= 0.65


def test_long_document_is_extracted_as_two_halves_and_merged() -> None:
    seen: list[dict] = []
    outputs = [
        _output("R1", "부산광역시에 소재한 업체", ["별첨 서식 미첨부"]),
        _output("R1", "최근 3년 이내 실적", ["별첨 서식 미첨부"]),
    ]
    with _client(_gateway(outputs, seen)) as client:
        outcome = client.extract(document_text=_source(), allowed_attachment_ids={"ATT-1"})

    assert outcome.status == "ACCEPTED", outcome.message
    assert outcome.split_parts == 2
    assert outcome.api_calls == 2 == outcome.openai_telemetry.api_calls
    ids = [item.requirement_id for item in outcome.data.requirements]
    assert ids == ["R1", "p2-R1"]
    assert outcome.data.missing_or_unreadable == ["별첨 서식 미첨부"]
    # Each call carries only its half and is told about the other one.
    sources = [body["input"][1]["content"][0]["text"].split("SOURCE:\n", 1)[1] for body in seen]
    assert "".join(sources) == _source()
    assert "part 1 of 2" in seen[0]["input"][0]["content"][0]["text"]
    assert "part 2 of 2" in seen[1]["input"][0]["content"][0]["text"]


def test_quotes_are_verified_against_the_whole_document() -> None:
    seen: list[dict] = []
    outputs = [
        _output("R1", "부산광역시에 소재한 업체", []),
        # The second half quotes text that exists only in the first half: still verbatim in the attachment.
        _output("R2", "부산광역시에 소재한 업체", []),
    ]
    with _client(_gateway(outputs, seen)) as client:
        outcome = client.extract(document_text=_source(), allowed_attachment_ids={"ATT-1"})
    assert outcome.status == "ACCEPTED"


def test_first_half_failure_stops_without_a_second_paid_call() -> None:
    seen: list[dict] = []
    with _client(_gateway([None, None], seen)) as client:
        outcome = client.extract(document_text=_source(), allowed_attachment_ids={"ATT-1"})
    assert outcome.status == "REVIEW"
    assert outcome.split_parts == 2
    assert len(seen) == 1
    assert outcome.api_calls == 1


def test_short_documents_keep_the_single_call(monkeypatch) -> None:
    seen: list[dict] = []
    with _client(_gateway([_output("R1", "부산광역시에 소재한 업체", [])], seen)) as client:
        outcome = client.extract(document_text="참가자격: 부산광역시에 소재한 업체", allowed_attachment_ids={"ATT-1"})
    assert outcome.status == "ACCEPTED"
    assert outcome.split_parts is None
    assert len(seen) == 1


@pytest.mark.parametrize("value", ["0", "off"])
def test_kill_switch_restores_single_call(monkeypatch, value) -> None:
    monkeypatch.setenv("PAI_SPLIT_LONG_EXTRACTION", value)
    seen: list[dict] = []
    with _client(_gateway([_output("R1", "부산광역시에 소재한 업체", [])], seen)) as client:
        outcome = client.extract(document_text=_source(), allowed_attachment_ids={"ATT-1"})
    assert outcome.split_parts is None
    assert len(seen) == 1
