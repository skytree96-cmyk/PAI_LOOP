"""Explicit probe waiting budget; every request is handled by MockTransport."""
import json

import httpx
import pytest

from pai_loop.extraction_contracts import classify_attempt_header
from pai_loop.quantitative_probe_policy import (
    QUANTITATIVE_PROBE_ONCE, QUANTITATIVE_PROBE_REQUEST_SCOPE,
    QUANTITATIVE_PROBE_CLIENT_TIMEOUT_SECONDS,
)
from test_quantitative_probe_client import ATT, make_client, probe_fixture, response


def probe_once(handler, **changes):
    options = dict(budget_policy=QUANTITATIVE_PROBE_ONCE, max_output_tokens=20_000,
                   timeout_seconds=320, max_total_api_calls=1, before_request=lambda: None)
    options.update(changes)
    return make_client(handler, **options)


@pytest.mark.parametrize("changes", [
    {"provider":"openai"}, {"max_output_tokens":True}, {"max_output_tokens":20_000.0},
    {"max_output_tokens":32_000}, {"max_output_tokens":19_999},
    {"timeout_seconds":200}, {"timeout_seconds":300}, {"timeout_seconds":True},
    {"timeout_seconds":float("nan")}, {"timeout_seconds":float("inf")},
    {"timeout_seconds":"320"}, {"max_total_api_calls":True}, {"max_total_api_calls":1.0},
    {"max_total_api_calls":2}, {"before_request":None}, {"before_request":False},
])
def test_probe_once_rejects_nonexact_constructor_contract(changes):
    calls = []
    with pytest.raises(ValueError, match="invalid QUANTITATIVE_PROBE_ONCE execution contract"):
        probe_once(lambda request: calls.append(request), **changes)
    assert calls == []


def test_probe_once_transmits_scope_with_320_read_timeout_and_durable_hook_first():
    payload, review = probe_fixture()
    events, requests = [], []
    def handler(request):
        assert events == ["DISPATCH_RESERVED"]
        events.append("TRANSPORT")
        requests.append(request)
        return response(payload)
    with probe_once(handler, before_request=lambda: events.append("DISPATCH_RESERVED")) as client:
        assert client.max_retries == 0
        assert client._client.timeout.read == QUANTITATIVE_PROBE_CLIENT_TIMEOUT_SECONDS == 320
        result = client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})
    assert events == ["DISPATCH_RESERVED", "TRANSPORT"]
    assert requests[0].extensions["timeout"]["read"] == 320
    body = json.loads(requests[0].content)
    assert body["budget_policy"] == QUANTITATIVE_PROBE_ONCE
    assert body["request_scope"] == QUANTITATIVE_PROBE_REQUEST_SCOPE
    assert body["max_output_tokens"] == 20_000
    assert result.outcome.status == "ACCEPTED" and result.outcome.api_calls == 1
    assert result.persistence_eligible is False
    assert result.source_audit["attachment_coverage_complete"] is False
    assert classify_attempt_header(result.outcome.model_dump()) == "UNSUPPORTED"


def test_opt_in_adds_only_the_two_gateway_policy_fields():
    payload, review = probe_fixture()
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return response(payload)
    for factory in (make_client, probe_once):
        with factory(handler) as client:
            client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})
    assert bodies[1] == {**bodies[0], "budget_policy":QUANTITATIVE_PROBE_ONCE,
                         "request_scope":QUANTITATIVE_PROBE_REQUEST_SCOPE}


@pytest.mark.parametrize("entry", ["public", "internal"])
def test_full_extraction_rejects_probe_policy_before_dispatch(entry):
    _, review = probe_fixture()
    events = []
    with probe_once(lambda request: events.append("TRANSPORT"),
                    before_request=lambda: events.append("RESERVED")) as client:
        extract = client.extract if entry == "public" else client._extract
        with pytest.raises(ValueError, match="REQUIRES_PROBE_ENTRY"):
            extract(document_text=review.canonical_text, allowed_attachment_ids={ATT})
    assert events == []


def test_probe_rejects_long_output_policy_before_dispatch():
    _, review = probe_fixture()
    events = []
    with make_client(lambda request: events.append("TRANSPORT"), budget_policy="LONG_OUTPUT_ONCE",
        max_output_tokens=32_000, timeout_seconds=300, before_request=lambda: events.append("RESERVED")) as client:
        with pytest.raises(ValueError, match="REQUIRES_SINGLE_GATEWAY_CALL"):
            client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})
    assert events == []


@pytest.mark.parametrize("failure", ["http500", "timeout", "schema"])
def test_probe_once_failure_does_not_retry_or_correct(failure):
    payload, review = probe_fixture()
    calls, reserved = [], []
    def handler(request):
        calls.append(request)
        if failure == "http500":
            return httpx.Response(500, json={"gateway_error": dict(version="gateway-failure-v1",
                stage="MODEL_EXECUTION", code="MODEL_EXECUTION_FAILED", upstream_http_status=None)})
        if failure == "timeout":
            raise httpx.ReadTimeout("SYN private response unknown", request=request)
        return response({**payload, "document_type":"SYN invalid schema"})
    with probe_once(handler, max_retries=9, before_request=lambda: reserved.append(True)) as client:
        result = client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})
    assert len(calls) == len(reserved) == result.outcome.api_calls == 1
    assert result.outcome.status == "REVIEW" and result.outcome.data is None
    assert result.outcome.corrective_retry_used is False
    assert result.outcome.correction_prompt_version is None
    assert result.outcome.openai_telemetry.total_tokens is None
    assert "SYN private response unknown" not in result.model_dump_json()


def test_reservation_failure_prevents_transport_and_repeated_dispatch():
    payload, review = probe_fixture()
    reserved, calls = [], []
    def reserve():
        if reserved:
            raise ValueError("SYN_ALREADY_DISPATCHED")
        reserved.append(True)
    with probe_once(lambda request: calls.append(request) or response(payload), before_request=reserve) as client:
        client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})
        with pytest.raises(ValueError, match="SYN_ALREADY_DISPATCHED"):
            client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["ordinary_probe", "ordinary_full", "long_full"])
def test_existing_budget_request_and_timeout_remain_unchanged(mode):
    payload, review = probe_fixture()
    requests, reserved = [], []
    options = {} if mode != "long_full" else dict(budget_policy="LONG_OUTPUT_ONCE",
        max_output_tokens=32_000, timeout_seconds=300, before_request=lambda: reserved.append(True))
    with make_client(lambda request: requests.append(request) or response(payload), **options) as client:
        if mode == "ordinary_probe":
            result = client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT}).outcome
        else:
            result = client.extract(document_text=review.canonical_text, allowed_attachment_ids={ATT})
    assert result.status == "ACCEPTED" and result.api_calls == 1
    body = json.loads(requests[0].content)
    assert "request_scope" not in body
    assert requests[0].extensions["timeout"]["read"] == (300 if mode == "long_full" else 200)
    assert body["max_output_tokens"] == (32_000 if mode == "long_full" else 20_000)
    assert body.get("budget_policy") == ("LONG_OUTPUT_ONCE" if mode == "long_full" else None)
    assert len(reserved) == (1 if mode == "long_full" else 0)
