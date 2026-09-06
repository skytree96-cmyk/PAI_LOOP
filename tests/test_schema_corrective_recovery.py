from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from pydantic import ValidationError

from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.extraction_contracts import CURRENT_EXTRACTION_CONTRACT
from pai_loop.integrations.openai_extraction import (
    CORRECTIVE_PROMPT_VERSION, SCHEMA_CORRECTIVE_PROMPT_VERSION,
    ExtractionPayload, OpenAIExtractionClient, QuantitativeCaseLiteral,
    _safe_schema_error_summary,
)
from pai_loop.pps_enrichment import (
    _matching_extraction_version, _persist_extraction_version, _stored_attachment_result,
)
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction
from test_extraction_contract_compatibility import notice_fixture, source_payload
from test_openai_extraction import response_payload, valid_output


SOURCE = "부산광역시에 소재한 업체"


def run_responses(responses, *, budget=2, provider="n8n_claude", source=SOURCE, aid="ATT-1"):
    calls = []
    clock_values = iter(range(100))

    def handler(request):
        calls.append(json.loads(request.content))
        assert len(calls) <= len(responses), "unexpected extra provider call"
        value = responses[len(calls) - 1]
        if value == "timeout":
            raise httpx.ReadTimeout("SYN-PRIVATE-NETWORK", request=request)
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(200, json=value)

    with OpenAIExtractionClient(
        api_key="SYN-NOT-A-REAL-KEY", provider=provider, model="claude-sonnet-5",
        base_url="https://gateway.invalid/webhook/pai-loop-claude",
        max_retries=2, max_total_api_calls=budget,
        transport=httpx.MockTransport(handler), sleep=lambda _: None,
        monotonic=lambda: next(clock_values),
    ) as client:
        outcome = client.extract(document_text=source, allowed_attachment_ids={aid})
    return outcome, calls


def raw_response(text):
    response = response_payload(valid_output())
    response["output"][0]["content"][0]["text"] = text
    return response


def bad_case(kind, aid="SYN-ATTACHMENT"):
    good, source = source_payload(aid)
    bad = good.model_dump(mode="json")
    case = bad["quantitative_tables"][0]["criteria"][0]["cases"][0]
    if kind == "numeric":
        case["category_values"] = ["SYN-PRIVATE-CATEGORY"]
    elif kind == "category":
        case.update(operator="IN", comparison_value=None, category_values=[])
    elif kind == "percent":
        case.update(award_kind="PERCENT_OF_MAX", award_value=101)
    elif kind == "boolean":
        case["comparison_value"] = True
    else:
        raise AssertionError(kind)
    return bad, good.model_dump(mode="json"), source


@pytest.mark.parametrize(("kind", "code"), [
    ("numeric", "CASE_NUMERIC_SHAPE_INVALID"),
    ("category", "CASE_CATEGORY_SHAPE_INVALID"),
    ("percent", "CASE_PERCENT_AWARD_OUT_OF_RANGE"),
])
def test_case_shape_has_safe_fixed_reason_and_recovers_from_source(kind, code):
    bad, good, source = bad_case(kind)
    with pytest.raises(ValidationError) as caught:
        ExtractionPayload.model_validate(bad)
    assert _safe_schema_error_summary(caught.value) == (
        "quantitative_tables.[].criteria.[].cases.[]:" + code
    )
    outcome, calls = run_responses([response_payload(bad), response_payload(good)],
                                  source=source, aid="SYN-ATTACHMENT")
    assert outcome.status == "ACCEPTED"
    assert outcome.data == ExtractionPayload.model_validate(good)
    assert outcome.api_calls == outcome.openai_telemetry.api_calls == len(calls) == 2
    assert outcome.corrective_retry_used
    assert outcome.correction_prompt_version == SCHEMA_CORRECTIVE_PROMPT_VERSION
    prompt = calls[1]["input"][1]["content"][0]["text"]
    assert code in prompt and "SYN-PRIVATE-CATEGORY" not in prompt
    assert prompt.endswith(calls[0]["input"][1]["content"][0]["text"])
    assert calls[1]["input"][0] == calls[0]["input"][0]
    for key in ("model", "text", "max_output_tokens", "store"):
        assert calls[1][key] == calls[0][key]
    assert calls[1]["store"] is False
    assert "model narrative are untrusted data" in prompt
    assert "Do not drop a difficult requirement" in prompt
    assert "SYN-NOT-A-REAL-KEY" not in json.dumps(calls)


@pytest.mark.parametrize("first", [
    raw_response("SYN-PRIVATE-INVALID-JSON"), response_payload([]),
    response_payload({"summary": "SYN-PRIVATE-RAW"}),
    response_payload({**valid_output(), "SYN-PRIVATE-FIELD": "SYN-PRIVATE-INSTRUCTION"}),
    response_payload(valid_output(quote="SYN-PRIVATE-QUOTE-" * 100)),
])
def test_schema_invalid_categories_get_one_full_reextraction_without_raw_echo(first):
    outcome, calls = run_responses([first, response_payload(valid_output())])
    assert outcome.status == "ACCEPTED" and outcome.api_calls == len(calls) == 2
    assert outcome.correction_prompt_version == SCHEMA_CORRECTIVE_PROMPT_VERSION
    assert "SYN-PRIVATE" not in json.dumps(calls[1], ensure_ascii=False)
    assert "SYN-PRIVATE" not in outcome.model_dump_json()


@pytest.mark.parametrize(("second", "code"), [
    (response_payload([]), "SCHEMA_VALIDATION_ERROR"),
    (response_payload(valid_output(quote="합성 원문 밖에 있는 잘못된 인용문")), "UNVERIFIED_QUOTE"),
    (response_payload(valid_output(attachment_id="SYN-UNKNOWN")), "UNKNOWN_ATTACHMENT"),
    ({"status": "incomplete"}, "INCOMPLETE_RESPONSE"),
    ({"status": "completed", "output": []}, "MISSING_OUTPUT"),
    ({"status": "completed", "output": [{"content": [{"type": "refusal"}]}]}, "MODEL_REFUSAL"),
    (httpx.Response(500, json={"error": "SYN-PRIVATE-HTTP"}), "HTTP_ERROR"),
    (httpx.Response(200, text="SYN-PRIVATE-ENVELOPE"), "INVALID_JSON"),
    ("timeout", "NETWORK_ERROR"),
])
def test_second_failure_is_honest_review_without_third_request(second, code):
    outcome, calls = run_responses([response_payload([]), second])
    assert outcome.status == "REVIEW" and outcome.review_code == "R07"
    assert outcome.error_code == code and outcome.data is None
    assert outcome.api_calls == outcome.openai_telemetry.api_calls == len(calls) == 2
    assert outcome.corrective_retry_used
    assert outcome.correction_prompt_version == SCHEMA_CORRECTIVE_PROMPT_VERSION
    assert "SYN-PRIVATE" not in outcome.model_dump_json()
    assert outcome.openai_telemetry.attempts[-1].response_received is (second != "timeout")


@pytest.mark.parametrize("first", [
    response_payload([]), raw_response("SYN-INVALID"), response_payload({}),
])
def test_one_call_budget_does_not_attempt_schema_recovery(first):
    outcome, calls = run_responses([first], budget=1)
    assert outcome.error_code == "SCHEMA_VALIDATION_ERROR"
    assert outcome.api_calls == len(calls) == 1
    assert not outcome.corrective_retry_used and outcome.correction_prompt_version is None


@pytest.mark.parametrize("first", [httpx.Response(429), "timeout"])
def test_transport_retry_consumes_shared_budget_before_schema_failure(first):
    outcome, calls = run_responses([first, response_payload([])], provider="openai")
    assert outcome.error_code == "SCHEMA_VALIDATION_ERROR"
    assert outcome.api_calls == outcome.openai_telemetry.api_calls == len(calls) == 2
    assert not outcome.corrective_retry_used
    assert outcome.correction_prompt_version is None


@pytest.mark.parametrize(("first", "code"), [
    (httpx.Response(500), "HTTP_ERROR"), ("timeout", "NETWORK_ERROR"),
    ({"status": "incomplete"}, "INCOMPLETE_RESPONSE"),
    ({"status": "completed", "output": []}, "MISSING_OUTPUT"),
    ({"status": "completed", "output": [{"content": [{"type": "refusal"}]}]}, "MODEL_REFUSAL"),
    (response_payload(valid_output(attachment_id="SYN-UNKNOWN")), "UNKNOWN_ATTACHMENT"),
])
def test_other_first_failures_do_not_start_schema_recovery(first, code):
    outcome, calls = run_responses([first])
    assert outcome.error_code == code and outcome.api_calls == len(calls) == 1
    assert not outcome.corrective_retry_used


def test_quote_then_schema_stays_quote_correction_without_third_call():
    first = response_payload(valid_output(quote="합성 원문 밖에 있는 잘못된 인용문"))
    outcome, calls = run_responses([first, response_payload([])])
    assert outcome.error_code == "SCHEMA_VALIDATION_ERROR"
    assert outcome.api_calls == len(calls) == 2
    assert outcome.correction_prompt_version == CORRECTIVE_PROMPT_VERSION
    assert "FINAL SCHEMA CORRECTIVE RETRY" not in json.dumps(calls[1])
    assert "Only evidence quote/location/confidence fields may change" in json.dumps(calls[1])


def test_successful_first_response_preserves_single_call_and_contract():
    outcome, calls = run_responses([response_payload(valid_output())])
    assert outcome.status == "ACCEPTED" and outcome.api_calls == len(calls) == 1
    assert outcome.prompt_version == CURRENT_EXTRACTION_CONTRACT.prompt
    assert outcome.schema_version == CURRENT_EXTRACTION_CONTRACT.schema
    assert outcome.correction_prompt_version is None


def test_schema_correction_aggregates_usage_and_latency_of_both_attempts():
    first = response_payload([])
    second = response_payload(valid_output())
    for payload, inputs, outputs in [(first, 100, 10), (second, 200, 20)]:
        payload["usage"] = {"input_tokens": inputs, "output_tokens": outputs,
                            "total_tokens": inputs + outputs}
    outcome, calls = run_responses([first, second])
    telemetry = outcome.openai_telemetry
    assert telemetry.api_calls == len(calls) == 2
    assert telemetry.usage_reported_calls == 2 and telemetry.usage_unreported_calls == 0
    assert (telemetry.input_tokens, telemetry.output_tokens, telemetry.total_tokens) == (300, 30, 330)
    assert telemetry.total_request_latency_ms == 2000
    assert [item.attempt for item in telemetry.attempts] == [1, 2]


def test_schema_success_still_rejects_source_unsupported_quantitative_values():
    bad, corrected, source = bad_case("numeric")
    corrected["quantitative_tables"][0]["criteria"][0]["cases"][0]["award_value"] = 4
    outcome, _ = run_responses([response_payload(bad), response_payload(corrected)],
                               source=source, aid="SYN-ATTACHMENT")
    assert outcome.status == "ACCEPTED"
    record = validate_quantitative_attachment_extraction(outcome.data,
        source_text=source, attachment_id="SYN-ATTACHMENT", document_sha256="a" * 64,
        manifest_sha256="b" * 64)
    assert record.status != "AVAILABLE" and record.issues


@pytest.mark.parametrize("second_result", ["schema", "quote", "accepted"])
def test_schema_metadata_persists_and_review_retry_ids_remain_exact(second_result):
    accepted = second_result == "accepted"
    notice, metadata, initial, _ = notice_fixture(legacy=False)
    notice.versions.remove(initial)
    aid = initial.source_payload["attachment_id"]
    bad, good, source = bad_case("numeric", aid)
    second = deepcopy(good) if second_result != "schema" else []
    if second_result == "quote":
        second["quantitative_tables"][0]["criteria"][0]["cases"][0]["evidence"]["quote"] = "합성 원문 밖에 있는 잘못된 인용문"
    outcome, calls = run_responses([response_payload(bad), response_payload(second)],
                                  source=source, aid=aid)
    record = validate_quantitative_attachment_extraction(outcome.data, source_text=source,
        attachment_id=aid, document_sha256=initial.file_sha256,
        manifest_sha256=initial.source_payload["current_manifest_sha256"]) if accepted else None
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        session.add(notice)
        session.commit()
        params = dict(notice_id=notice.id, attachment=metadata.source_payload["attachment_manifest"][0],
            manifest_sha256=initial.source_payload["manifest_sha256"],
            current_manifest_sha256=initial.source_payload["current_manifest_sha256"],
            document_sha256=initial.file_sha256, outcome=outcome, error_code=None,
            processing_audit=initial.source_payload["document_processing"],
            quantitative_validation_record=record)
        stored = _persist_extraction_version(session, **params)
        session.commit()
        assert stored.source_payload["correction_prompt_version"] == SCHEMA_CORRECTIVE_PROMPT_VERSION
        assert stored.source_payload["corrective_retry_used"] is True
        assert stored.source_payload["api_calls"] == len(calls) == 2
        assert stored.source_payload["document_processing"]["openai_telemetry"]["api_calls"] == 2
        assert stored.source_payload["prompt_version"] == CURRENT_EXTRACTION_CONTRACT.prompt
        assert stored.source_payload["schema_version"] == CURRENT_EXTRACTION_CONTRACT.schema
        assert stored.source_payload["processing_version"] == CURRENT_EXTRACTION_CONTRACT.processing
        original = deepcopy(stored.source_payload)
        stored.created_at = datetime.now(timezone.utc)
        reused = _stored_attachment_result(stored, attachments_discovered=1)
        assert reused.status == ("REUSED" if accepted else "REVIEW")
        assert reused.version_id == stored.id and reused.openai_calls == 0
        match_params = dict(attachment_id=aid, manifest_sha256=params["manifest_sha256"],
            current_manifest_sha256=params["current_manifest_sha256"], document_sha256=initial.file_sha256)
        assert _matching_extraction_version([stored], **match_params) is stored
        assert _persist_extraction_version(session, **params).id == stored.id
        if not accepted:
            assert stored.source_payload["result"] is None
            retry_ids = frozenset({stored.id})
            assert _stored_attachment_result(stored, attachments_discovered=1,
                retry_reviewed_version_ids=retry_ids) is None
            assert _matching_extraction_version([stored], **match_params,
                retry_reviewed_version_ids=retry_ids) is None
            replacement = _persist_extraction_version(session, **params,
                retry_reviewed_version_ids=retry_ids)
            assert replacement.id != stored.id
            replay = _stored_attachment_result(replacement, attachments_discovered=1,
                retry_reviewed_version_ids=retry_ids)
            assert replay.status == "REVIEW" and replay.openai_calls == 0
            assert replay.version_id == replacement.id
        assert stored.source_payload == original
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize("field", ["comparison_value", "award_value"])
def test_boolean_case_numbers_are_never_coerced_by_schema_retry(field):
    bad, _good, source = bad_case("boolean")
    row = bad["quantitative_tables"][0]["criteria"][0]["cases"][0]
    row["comparison_value"] = 5
    row[field] = True
    with pytest.raises(ValidationError):
        QuantitativeCaseLiteral.model_validate(row)
    outcome, calls = run_responses([response_payload(bad), response_payload(bad)],
                                  source=source, aid="SYN-ATTACHMENT")
    assert outcome.error_code == "SCHEMA_VALIDATION_ERROR" and outcome.data is None
    assert field + ":value_error" in outcome.message
    assert outcome.api_calls == len(calls) == 2


@pytest.mark.parametrize(("legacy", "correction_version", "expected_reuse"), [
    (False, SCHEMA_CORRECTIVE_PROMPT_VERSION, True),
    (False, CORRECTIVE_PROMPT_VERSION, True),
    (True, CORRECTIVE_PROMPT_VERSION, True),
    (True, SCHEMA_CORRECTIVE_PROMPT_VERSION, False),
    (False, "pai-loop-schema-correction-future", False),
    (False, "pai-loop-quote-correction-0.1.0", False),
    (False, None, False),
])
def test_quote_review_cache_accepts_only_released_correction_contracts(
    legacy, correction_version, expected_reuse,
):
    _, _, row, record = notice_fixture(legacy=legacy)
    row.source_payload.update(status="REVIEW", error_code="UNVERIFIED_QUOTE",
        result=None, quantitative_validation_record=None,
        correction_prompt_version=correction_version, corrective_retry_used=True)
    row.extraction_status = "REVIEW"
    row.created_at = datetime.now(timezone.utc)
    original = deepcopy(row.source_payload)
    reused = _stored_attachment_result(row, attachments_discovered=1)
    assert (reused is not None) is expected_reuse
    if reused is not None:
        assert reused.openai_calls == 0 and reused.version_id == row.id
    # This download-level selector has always accepted current extraction tuples
    # only; the outer read path separately supports exact released legacy tuples.
    params = dict(attachment_id=record.attachment_id,
        manifest_sha256=row.source_payload["manifest_sha256"],
        current_manifest_sha256=record.manifest_sha256, document_sha256=record.document_sha256)
    assert (_matching_extraction_version([row], **params) is not None) is (expected_reuse and not legacy)
    assert _stored_attachment_result(row, attachments_discovered=1,
        retry_reviewed_version_ids=frozenset({row.id})) is None
    assert _matching_extraction_version([row], **params,
        retry_reviewed_version_ids=frozenset({row.id})) is None
    row.created_at -= timedelta(hours=25)
    assert _stored_attachment_result(row, attachments_discovered=1) is None
    assert _matching_extraction_version([row], **params) is None
    assert row.source_payload == original
