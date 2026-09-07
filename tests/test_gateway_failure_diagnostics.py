from copy import deepcopy

import httpx
import pytest

from pai_loop.gateway_diagnostics import safe_gateway_failure
from pai_loop.integrations.openai_extraction import OpenAIExtractionClient
from pai_loop.models import Notice, NoticeVersion
from pai_loop.pps_enrichment import enrich_notice_from_pps
from pai_loop.recovery_diagnostics import _notice_projection
from test_pps_enrichment import _single_hwpx_reuse_case
from test_recovery_diagnostics import diagnostic_client, seed, read

CANARY = "SYN-PRIVATE-GATEWAY-CANARY"
FAILURE = {"version": "gateway-failure-v1", "stage": "MODEL_EXECUTION",
           "code": "MODEL_EXECUTION_FAILED", "upstream_http_status": 429}


def outcome(body, *, provider="n8n_claude", status=500):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)
    with OpenAIExtractionClient(api_key="SYN-key", model="claude-sonnet-5", provider=provider,
            base_url="https://example.test/gateway", max_retries=0,
            transport=httpx.MockTransport(handler)) as client:
        result = client.extract(document_text="SYN 공개 원문", allowed_attachment_ids={"SYN-ATTACHMENT"})
    assert len(calls) == result.api_calls == 1
    assert result.status == "REVIEW" and result.error_code == "HTTP_ERROR"
    return result


@pytest.mark.parametrize("stage,code,status", [("INPUT_VALIDATION", "REQUEST_REJECTED", None),
    ("MODEL_EXECUTION", "MODEL_EXECUTION_FAILED", 403), ("MODEL_EXECUTION", "MODEL_EXECUTION_FAILED", 429),
    ("MODEL_EXECUTION", "MODEL_EXECUTION_FAILED", 502), ("MODEL_EXECUTION", "MODEL_EXECUTION_FAILED", None),
    ("OUTPUT_NORMALIZATION", "OUTPUT_REJECTED", None)])
def test_fixed_failure_preserves_http_review_and_does_not_retry(stage, code, status):
    failure = {**FAILURE, "stage": stage, "code": code, "upstream_http_status": status}
    result = outcome({"gateway_error": failure})
    assert result.gateway_failure.model_dump() == failure
    assert result.response_id is None and not result.corrective_retry_used


@pytest.mark.parametrize("changes", [
    {"version": CANARY}, {"stage": CANARY}, {"code": CANARY}, {"message": CANARY},
    {"upstream_http_status": True}, {"upstream_http_status": "429"},
    {"upstream_http_status": 399}, {"upstream_http_status": 600},
    {"stage": "INPUT_VALIDATION", "code": "REQUEST_REJECTED"},
    {"stage": "OUTPUT_NORMALIZATION"},
])
def test_malformed_or_private_failure_is_rejected_as_a_whole(changes):
    result = outcome({"gateway_error": {**FAILURE, **changes}})
    assert result.gateway_failure is None
    assert CANARY not in result.model_dump_json()


@pytest.mark.parametrize("body,provider,status", [
    ({"gateway_error": FAILURE, "message": CANARY}, "n8n_claude", 500),
    ({"gateway_error": FAILURE}, "openai", 500),
    ({"gateway_error": FAILURE}, "n8n_claude", 502),
    ({"message": CANARY}, "n8n_claude", 500),
])
def test_only_exact_gateway_http500_envelope_has_diagnostic_authority(body, provider, status):
    result = outcome(body, provider=provider, status=status)
    assert result.gateway_failure is None
    assert CANARY not in result.model_dump_json()


def test_failure_survives_current_attachment_persistence_and_safe_read_without_recalling_model():
    engine, factory, notice_id, download = _single_hwpx_reuse_case(notice_key="PPS-SYN-GATEWAY-STORED")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={"gateway_error": FAILURE})
    def model_factory(**kwargs):
        return OpenAIExtractionClient(**kwargs, transport=httpx.MockTransport(handler))
    kwargs = dict(notice_id=notice_id, openai_api_key="SYN-key", openai_model="claude-sonnet-5",
        llm_provider="n8n_claude", llm_gateway_base_url="https://example.test/gateway",
        transport=download, openai_client_factory=model_factory)
    with factory() as session:
        result = enrich_notice_from_pps(session, **kwargs)
    with factory() as session:
        stored = session.get(NoticeVersion, result.version_id)
        assert stored.source_payload["gateway_failure"] == FAILURE
        diagnostic = _notice_projection(session.get(Notice, notice_id), None)
        attachment, = diagnostic.attachments
        assert attachment.gateway_failure.model_dump() == FAILURE
        assert attachment.model_http_status == 500
        assert "response_id" not in attachment.model_dump_json()
    with factory() as session:
        repeat = enrich_notice_from_pps(session, **kwargs)
        assert repeat.openai_calls == 0 and len(calls) == 1
    engine.dispose()


def test_read_sanitizer_rejects_stored_extra_private_fields():
    stored = deepcopy(FAILURE)
    stored["execution_id"] = CANARY
    assert safe_gateway_failure(stored) is None


@pytest.mark.parametrize("stale", [False, True])
def test_server_read_keeps_gateway_failure_inside_selected_current_attempt(diagnostic_client, stale):
    def mutate(versions):
        versions[-1].source_payload.update(gateway_failure=FAILURE,
            message="모델 API가 HTTP 500를 반환했습니다.")
        if stale:
            versions[-1].source_payload["current_manifest_sha256"] = CANARY
    seed(diagnostic_client, error="HTTP_ERROR", mutate=mutate)
    denied = read(diagnostic_client, headers={})
    assert denied.status_code == 401 and "gateway_failure" not in denied.text
    response = read(diagnostic_client)
    assert response.status_code == 200
    attachment, = response.json()["notices"][0]["attachments"]
    assert attachment["gateway_failure"] == (None if stale else FAILURE)
    assert CANARY not in response.text
