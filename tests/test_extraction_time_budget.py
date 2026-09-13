"""SYN admission budgets preserve separate provider, client and outer limits."""
import inspect
import json
from pathlib import Path

import httpx

from pai_loop.analysis_api import (
    ANALYSIS_ENRICHMENT_BUDGET_SECONDS, ATTACHMENT_UNIT_WORST_CASE_SECONDS,
    N8N_ANALYSIS_HTTP_TIMEOUT_SECONDS,
)
from pai_loop.extraction_time_budget import (
    DEFAULT_EXTRACTION_CLIENT_TIMEOUT_SECONDS, attachment_start_reservation_seconds,
)
from pai_loop.integrations.openai_extraction import OpenAIExtractionClient
from pai_loop.long_output_policy import LONG_OUTPUT_MAX_CALLS, LONG_OUTPUT_TIMEOUT_SECONDS
from pai_loop.pps_enrichment import (
    ATTACHMENT_TIMEOUT_GUARD_SECONDS, DEFAULT_ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_OPENAI_RESPONSE_TIMEOUT_SECONDS, MAX_OPENAI_CALLS_PER_ATTACHMENT,
    enrich_notice_from_pps,
)


def reservation(timeout, calls):
    return attachment_start_reservation_seconds(
        download_timeout_seconds=DEFAULT_ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS,
        model_timeout_seconds=timeout, max_model_calls=calls,
        guard_seconds=ATTACHMENT_TIMEOUT_GUARD_SECONDS,
    )


def test_shared_200_second_client_wait_leaves_gateway_response_return_margin():
    assert DEFAULT_EXTRACTION_CLIENT_TIMEOUT_SECONDS == DEFAULT_OPENAI_RESPONSE_TIMEOUT_SECONDS == 200
    assert inspect.signature(OpenAIExtractionClient).parameters['timeout_seconds'].default == 200
    assert inspect.signature(enrich_notice_from_pps).parameters['openai_timeout_seconds'].default == 200
    calls = []
    def handler(request):
        calls.append(request.extensions['timeout'])
        return httpx.Response(503, json={})
    with OpenAIExtractionClient(api_key='SYN-key', provider='n8n_claude', model='claude-sonnet-5',
            base_url='https://example.test/gateway', transport=httpx.MockTransport(handler), max_retries=3) as client:
        outcome = client.extract(document_text='SYN 원문', allowed_attachment_ids={'SYN-ATT'})
    assert calls == [{'connect':200, 'read':200, 'write':200, 'pool':200}]
    assert outcome.api_calls == 1
    workflow = json.loads((Path(__file__).parents[1]/'workflows/pai-loop-13-claude-extraction-gateway.json').read_text(encoding='utf-8'))
    provider = next(node for node in workflow['nodes'] if node['name'] == 'Claude Sonnet 5 Native JSON')
    assert provider['parameters']['options']['timeout'] == '={{ $json.gateway_timeout_ms }}'
    assert provider['retryOnFail'] is False
    assert 180 < DEFAULT_EXTRACTION_CLIENT_TIMEOUT_SECONDS


def test_attachment_admission_preserves_441_550_600_and_observed_86_second_example():
    assert MAX_OPENAI_CALLS_PER_ATTACHMENT == 2
    assert reservation(180, 2) == 401  # Prior wait, retained as the comparison basis.
    assert reservation(200, 2) == ATTACHMENT_UNIT_WORST_CASE_SECONDS == 441
    assert ANALYSIS_ENRICHMENT_BUDGET_SECONDS == 550
    assert N8N_ANALYSIS_HTTP_TIMEOUT_SECONDS == 600
    assert 441 < 550 < 600
    # 86 seconds is a historical example, not a guaranteed attachment duration.
    latest_second_start = ANALYSIS_ENRICHMENT_BUDGET_SECONDS - reservation(200, 2)
    assert latest_second_start == 109
    assert latest_second_start - 86 == 23
    assert 550 - 86 >= reservation(200, 2)
    assert 550 - 110 < reservation(200, 2)


def test_long_output_keeps_300_seconds_once_and_normal_300_twice_does_not_fit():
    assert LONG_OUTPUT_TIMEOUT_SECONDS == 300
    assert LONG_OUTPUT_MAX_CALLS == 1
    assert reservation(LONG_OUTPUT_TIMEOUT_SECONDS, LONG_OUTPUT_MAX_CALLS) == 341
    assert reservation(300, 1) < ANALYSIS_ENRICHMENT_BUDGET_SECONDS
    assert reservation(300, MAX_OPENAI_CALLS_PER_ATTACHMENT) == 641
    assert reservation(300, MAX_OPENAI_CALLS_PER_ATTACHMENT) > N8N_ANALYSIS_HTTP_TIMEOUT_SECONDS
