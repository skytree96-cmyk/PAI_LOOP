"""SYN timeout observations are bounded labels, never provider outcome claims."""
import json
from typing import get_args

import httpx
import pytest
from pydantic import ValidationError

from pai_loop.extraction_time_budget import ClientTransportErrorCode, safe_transport_error_code
from pai_loop.integrations.openai_extraction import (
    OpenAIAttemptTelemetry, OpenAIExtractionClient, OpenAITelemetry,
)
from pai_loop.long_output_policy import LONG_OUTPUT_ONCE, LONG_OUTPUT_TIMEOUT_SECONDS, LONG_OUTPUT_TOKENS
from pai_loop.models import NoticeVersion
from pai_loop.pps_enrichment import enrich_notice_from_pps
from test_pps_enrichment import _single_hwpx_reuse_case

CANARY = 'SYN-PRIVATE-TRANSPORT-CANARY'


@pytest.mark.parametrize('exception,code', [
    (httpx.ConnectTimeout,'CLIENT_CONNECT_TIMEOUT'),
    (httpx.ReadTimeout,'CLIENT_READ_TIMEOUT'),
    (httpx.WriteTimeout,'CLIENT_WRITE_TIMEOUT'),
    (httpx.PoolTimeout,'CLIENT_POOL_TIMEOUT'),
    (httpx.TimeoutException,'CLIENT_TIMEOUT'),
    (httpx.ConnectError,'CLIENT_REQUEST_ERROR'),
])
def test_transport_phase_is_safely_recorded_without_retry_or_invented_usage(exception, code):
    calls = []
    def handler(request):
        calls.append(request)
        raise exception(CANARY, request=request)
    clock = iter([1.0, 181.062])
    with OpenAIExtractionClient(api_key=CANARY, provider='n8n_claude', model='claude-sonnet-5',
            base_url='https://example.test/'+CANARY, max_retries=3,
            transport=httpx.MockTransport(handler), monotonic=lambda: next(clock),
            sleep=lambda _: pytest.fail('An ambiguous gateway call must not be repeated')) as client:
        outcome = client.extract(document_text=CANARY+' SYN 원문', allowed_attachment_ids={'SYN-ATT'})
    assert len(calls) == outcome.api_calls == 1
    assert outcome.status == 'REVIEW' and outcome.review_code == 'R07' and outcome.error_code == 'NETWORK_ERROR'
    assert outcome.response_id is None and outcome.gateway_failure is None and not outcome.corrective_retry_used
    telemetry = outcome.openai_telemetry
    assert telemetry.usage_reported_calls == 0 and telemetry.usage_unreported_calls == 1
    assert telemetry.input_tokens is telemetry.output_tokens is telemetry.total_tokens is None
    attempt, = telemetry.attempts
    assert attempt.transport_error_code == code and attempt.request_latency_ms == 180062
    assert attempt.response_received is False and attempt.usage is None
    serialized = outcome.model_dump_json()
    assert CANARY not in serialized and 'https://' not in serialized
    assert OpenAITelemetry.model_validate_json(telemetry.model_dump_json()) == telemetry


def test_absent_transport_code_preserves_legacy_attempt_serialized_shape():
    old = dict(attempt=1,request_latency_ms=10,response_received=False,model=None,service_tier=None,usage=None)
    assert OpenAIAttemptTelemetry.model_validate(old).model_dump() == old
    assert OpenAIAttemptTelemetry.model_validate({**old,'transport_error_code':None}).model_dump() == old
    assert json.loads(OpenAITelemetry(attempts=[OpenAIAttemptTelemetry(**old)]).model_dump_json())['attempts'] == [old]


@pytest.mark.parametrize('code', get_args(ClientTransportErrorCode))
def test_safe_reader_only_accepts_the_fixed_transport_codes(code):
    assert safe_transport_error_code(code) == code
    with pytest.raises(ValidationError):
        OpenAIAttemptTelemetry(attempt=1,request_latency_ms=1,response_received=True,transport_error_code=code)


@pytest.mark.parametrize('value', [CANARY, 'READ_TIMEOUT', '', 1, True, {}, [], None])
def test_untrusted_transport_detail_is_not_a_freeform_diagnostic(value):
    assert safe_transport_error_code(value) is None
    if value is not None:
        with pytest.raises(ValidationError):
            OpenAIAttemptTelemetry(attempt=1,request_latency_ms=1,response_received=False,transport_error_code=value)


def test_response_errors_do_not_claim_a_client_timeout():
    with OpenAIExtractionClient(api_key='SYN-key', provider='n8n_claude', model='claude-sonnet-5',
            base_url='https://example.test/gateway', transport=httpx.MockTransport(lambda _:httpx.Response(504,json={}))) as client:
        outcome = client.extract(document_text='SYN 원문',allowed_attachment_ids={'SYN-ATT'})
    assert outcome.error_code == 'HTTP_ERROR'
    attempt, = outcome.openai_telemetry.attempts
    assert attempt.response_received and attempt.transport_error_code is None
    assert 'transport_error_code' not in attempt.model_dump()


def test_long_output_timeout_still_dispatches_once_and_has_unknown_provider_usage():
    dispatched, waits = [], []
    def handler(request):
        waits.append(request.extensions['timeout']['read'])
        raise httpx.ReadTimeout(CANARY,request=request)
    with OpenAIExtractionClient(api_key='SYN-key',provider='n8n_claude',model='claude-sonnet-5',
            base_url='https://example.test/gateway',budget_policy=LONG_OUTPUT_ONCE,
            timeout_seconds=LONG_OUTPUT_TIMEOUT_SECONDS,max_output_tokens=LONG_OUTPUT_TOKENS,
            max_total_api_calls=1,before_request=lambda:dispatched.append('SYN-dispatch'),
            transport=httpx.MockTransport(handler)) as client:
        outcome=client.extract(document_text='SYN 원문',allowed_attachment_ids={'SYN-ATT'})
    assert dispatched == ['SYN-dispatch'] and waits == [300]
    assert outcome.api_calls == 1 and outcome.error_code == 'NETWORK_ERROR'
    assert outcome.openai_telemetry.attempts[0].transport_error_code == 'CLIENT_READ_TIMEOUT'
    assert outcome.openai_telemetry.usage_unreported_calls == 1
    assert outcome.openai_telemetry.total_tokens is None


def test_timeout_code_persists_and_existing_cooldown_reuses_without_model_call():
    engine,factory,notice_id,download=_single_hwpx_reuse_case(notice_key='PPS-SYN-TRANSPORT-STORED')
    calls=[]
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(CANARY,request=request)
    def model_factory(**kwargs):
        return OpenAIExtractionClient(**kwargs,transport=httpx.MockTransport(handler))
    kwargs=dict(notice_id=notice_id,openai_api_key='SYN-key',openai_model='claude-sonnet-5',
        llm_provider='n8n_claude',llm_gateway_base_url='https://example.test/gateway',
        transport=download,openai_client_factory=model_factory)
    try:
        with factory() as session:
            result=enrich_notice_from_pps(session,**kwargs)
        with factory() as session:
            stored=session.get(NoticeVersion,result.version_id).source_payload
            assert stored['error_code']=='NETWORK_ERROR'
            telemetry=OpenAITelemetry.model_validate(stored['document_processing']['openai_telemetry'])
            assert telemetry.attempts[0].transport_error_code=='CLIENT_READ_TIMEOUT'
            assert telemetry.total_tokens is None and telemetry.usage_unreported_calls==1
            assert CANARY not in json.dumps(stored)
        with factory() as session:
            reused=enrich_notice_from_pps(session,**kwargs)
        assert reused.openai_calls==0 and len(calls)==1
    finally:
        engine.dispose()
