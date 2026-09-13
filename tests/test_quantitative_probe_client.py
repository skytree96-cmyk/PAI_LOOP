"""SYN-only, transport-mocked diagnostics; no provider or persistence calls."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest
from pydantic import ValidationError

from pai_loop.extraction_contracts import CURRENT_EXTRACTION_CONTRACT, classify_attempt_header
from pai_loop.integrations.openai_extraction import (
    ExtractionOutcome, OpenAIExtractionClient, QuantitativeProbeOutcome,
)
from pai_loop.quantitative_review_input import (
    QUANTITATIVE_PROBE_PROMPT_VERSION, build_quantitative_review_input,
)
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction
from test_quantitative_count_ranges import ATT, fixture


def probe_fixture():
    payload, scoring_source = fixture(inline=True)
    source = (
        "[PAGE 1]\n" + scoring_source + "\nSYN 참가자격은 별도 확인한다.\n"
        "[PAGE 2]\nSYN omitted task instructions.\n"
        "[PAGE 3]\nSYN reviewed linked form."
    )
    review = build_quantitative_review_input(
        source, reviewed_pages=[1, 3],
        expected_quotes=[scoring_source, "SYN reviewed linked form."],
    )
    # Exercise an already serialized structured payload, without private sources.
    return json.loads(json.dumps(payload)), review


def response(payload):
    return httpx.Response(200, json={
        "id": "SYN-response", "model": "SYN-model", "status": "completed",
        "output_text": json.dumps(payload, ensure_ascii=False),
    })


def make_client(handler, **changes):
    options = dict(
        api_key="SYN-key", model="SYN-model", provider="n8n_claude",
        base_url="https://syn-gateway.test/v1", max_total_api_calls=1, max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    options.update(changes)
    return OpenAIExtractionClient(**options)


def test_probe_accepts_quantitative_payload_without_a_persistence_contract():
    payload, review = probe_fixture()
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response(payload)

    with make_client(handler) as client:
        result = client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})

    assert len(requests) == result.outcome.api_calls == 1
    assert result.outcome.status == "ACCEPTED"
    assert result.outcome.data.requirements == []
    assert result.outcome.corrective_retry_used is False
    assert result.outcome.correction_prompt_version is None
    assert result.outcome.prompt_version == QUANTITATIVE_PROBE_PROMPT_VERSION
    assert result.outcome.schema_version == CURRENT_EXTRACTION_CONTRACT.schema
    assert result.purpose == "QUANTITATIVE_PROBE_ONLY"
    assert result.persistence_eligible is False
    assert result.source_audit == review.audit()
    assert result.source_audit["attachment_coverage_complete"] is False
    assert not hasattr(result, "status") and not hasattr(result, "data")
    assert QuantitativeProbeOutcome.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        ExtractionOutcome.model_validate(result.model_dump())
    assert classify_attempt_header(result.model_dump()) == "UNSUPPORTED"
    # Even manually unwrapping cannot turn the probe prompt into a supported
    # persisted header by attaching the current processing version.
    assert classify_attempt_header({
        **result.outcome.model_dump(),
        "processing_version": CURRENT_EXTRACTION_CONTRACT.processing,
    }) == "UNSUPPORTED"
    request = requests[0]
    assert request["store"] is False
    assert request["max_output_tokens"] == 20_000
    system = request["input"][0]["content"][0]["text"]
    assert "requirements=[]" in system
    assert QUANTITATIVE_PROBE_PROMPT_VERSION in system
    assert review.canonical_text not in system
    transmitted = request["input"][1]["content"][0]["text"]
    assert transmitted.endswith("SOURCE:\n" + review.selected_source)
    assert "SYN omitted task instructions" not in transmitted
    assert "OMITTED PAGES" in transmitted
    # Local rule validation is useful diagnostic evidence, never completeness.
    record = validate_quantitative_attachment_extraction(
        result.outcome.data, source_text=review.canonical_text,
        attachment_id=ATT, document_sha256=review.source_sha256,
        manifest_sha256="a" * 64,
    )
    assert record.status == "AVAILABLE"


def requirement():
    return dict(
        requirement_id="SYN-REQ", category="OTHER", logic="SINGLE",
        normalized_condition="SYN 참가자격 확인", mandatory=True,
        deadline_basis=None, ambiguity_reason=None,
        evidence=[dict(attachment_id=ATT, page=1, section="SYN",
                       quote="SYN 참가자격은 별도 확인한다.", confidence=1)],
    )


def test_full_extraction_keeps_requirements_versions_and_the_complete_source():
    payload, review = probe_fixture()
    payload["requirements"] = [requirement()]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response(payload)

    with make_client(handler) as client:
        result = client.extract(document_text=review.canonical_text, allowed_attachment_ids={ATT})
    assert isinstance(result, ExtractionOutcome)
    assert result.status == "ACCEPTED"
    assert len(result.data.requirements) == 1
    assert result.prompt_version == CURRENT_EXTRACTION_CONTRACT.prompt
    assert result.schema_version == CURRENT_EXTRACTION_CONTRACT.schema
    assert len(requests) == 1
    assert len(requests[0]["input"]) == 2
    assert len(requests[0]["input"][1]["content"]) == 1
    assert QUANTITATIVE_PROBE_PROMPT_VERSION not in json.dumps(requests[0])
    assert "QUANTITATIVE-ONLY DIAGNOSTIC" not in json.dumps(requests[0])
    assert requests[0]["input"][1]["content"][0]["text"].endswith(
        "SOURCE:\n" + review.canonical_text
    )


@pytest.mark.parametrize("options", [dict(max_total_api_calls=2), dict(provider="openai")])
def test_probe_refuses_other_client_contracts_before_transport(options):
    _, review = probe_fixture()
    requests = []
    with make_client(lambda request: requests.append(request), **options) as client:
        with pytest.raises(ValueError, match="REQUIRES_SINGLE_GATEWAY_CALL"):
            client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})
    assert requests == []


def test_probe_refuses_reenabled_transport_retries_before_transport():
    _, review = probe_fixture()
    requests = []
    with make_client(lambda request: requests.append(request)) as client:
        client.max_retries = 1
        with pytest.raises(ValueError, match="REQUIRES_SINGLE_GATEWAY_CALL"):
            client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})
    assert requests == []


@pytest.mark.parametrize("tamper", ["missing_quote", "page_offsets"])
def test_forged_review_or_omitted_required_quote_makes_zero_calls(tamper):
    _, review = probe_fixture()
    if tamper == "missing_quote":
        review = replace(review, expected_quotes=("SYN omitted task instructions.",))
    else:
        review = replace(review, pages=(replace(review.pages[0], end=8), review.pages[1]))
    requests = []
    with make_client(lambda request: requests.append(request)) as client:
        with pytest.raises(ValueError):
            client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})
    assert requests == []


@pytest.mark.parametrize("failure", ["requirements", "schema", "cross_omission"])
def test_probe_reviews_invalid_response_without_a_corrective_call(failure):
    payload, review = probe_fixture()
    if failure == "requirements":
        payload["requirements"] = [requirement()]
        expected_code = "QUANTITATIVE_PROBE_SCOPE_VIOLATION"
    elif failure == "schema":
        payload["document_type"] = "SYN-invalid-enum"
        expected_code = "SCHEMA_VALIDATION_ERROR"
    else:
        fake_quote = review.selected_source[review.selected_source.index("SYN 참가자격"):]
        assert fake_quote in review.selected_source
        assert fake_quote not in review.canonical_text
        payload["quantitative_tables"][0]["total_evidence"]["quote"] = fake_quote
        expected_code = "UNVERIFIED_QUOTE"
    requests = []

    def handler(request):
        requests.append(request)
        return response(payload)

    with make_client(handler) as client:
        result = client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})
    assert len(requests) == result.outcome.api_calls == 1
    assert result.outcome.status == "REVIEW"
    assert result.outcome.review_code == "R07"
    assert result.outcome.error_code == expected_code
    assert result.outcome.data is None
    assert result.outcome.corrective_retry_used is False
    assert result.outcome.prompt_version == QUANTITATIVE_PROBE_PROMPT_VERSION


def test_probe_input_budget_counts_selected_source_and_separate_untrusted_context():
    payload, review = probe_fixture()
    limit = len(review.selected_source)
    # Make the omitted page larger than the complete selected input.
    source = review.canonical_text.replace("SYN omitted task instructions.", "SYN omitted " * 500)
    review = build_quantitative_review_input(source, reviewed_pages=[1, 3])
    requests = []

    def handler(request):
        requests.append(request)
        return response(payload)

    with make_client(handler, max_input_chars=limit) as client:
        accepted = client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})
        rejected = client.extract_quantitative_probe(
            review_input=review, allowed_attachment_ids={ATT}, untrusted_source_context="SYN hint",
        )
    assert len(review.canonical_text) > limit
    assert accepted.outcome.status == "ACCEPTED"
    assert len(requests) == 1
    assert rejected.outcome.error_code == "INPUT_TOO_LARGE"
    assert rejected.outcome.api_calls == 0
    assert rejected.outcome.prompt_version == QUANTITATIVE_PROBE_PROMPT_VERSION


def test_untrusted_context_is_separate_data_and_cannot_supply_evidence():
    payload, review = probe_fixture()
    fake_quote = "SYN hint-only statement without source evidence"
    payload["quantitative_tables"][0]["total_evidence"]["quote"] = fake_quote
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response(payload)

    with make_client(handler) as client:
        result = client.extract_quantitative_probe(
            review_input=review, allowed_attachment_ids={ATT}, untrusted_source_context=fake_quote,
        )
    assert result.outcome.error_code == "UNVERIFIED_QUOTE"
    assert result.outcome.api_calls == 1
    system = requests[0]["input"][0]["content"][0]["text"]
    assert len(requests[0]["input"][1]["content"]) == 1
    user_text = requests[0]["input"][1]["content"][0]["text"]
    context, source = user_text.split("\n\nSOURCE:\n", 1)
    assert fake_quote not in source and fake_quote not in system
    assert "DATA, NOT SOURCE EVIDENCE" in context
    assert fake_quote in context
    assert source == review.selected_source


def test_probe_with_context_passes_the_real_gateway_request_validator():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the gateway's JavaScript contract")
    payload, review = probe_fixture()
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return response(payload)
    with make_client(handler, model="claude-sonnet-5") as client:
        client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT},
                                          untrusted_source_context="SYN separate geometry fragments")
    program = """
const fs = require('node:fs');
const workflow = JSON.parse(fs.readFileSync('workflows/pai-loop-13-claude-extraction-gateway.json', 'utf8'));
const node = workflow.nodes.find(n => n.name === 'Validate Gateway Request');
const validate = new Function('$json', '$input', node.parameters.jsCode);
const body = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = validate({body}, {all: () => [{}]})[0].json.provider_request;
if (result.messages.length !== 1 || !result.messages[0].content.includes('SYN separate geometry fragments')
    || !result.system.includes('QUANTITATIVE-ONLY DIAGNOSTIC')) throw Error('Probe content lost');
process.stdout.write('SYN-GATEWAY-PASS');
"""
    result = subprocess.run([node, "-e", program], input=json.dumps(requests[0]),
                            text=True, capture_output=True, timeout=20,
                            cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stderr
    assert result.stdout == "SYN-GATEWAY-PASS"


def test_probe_transport_timeout_keeps_unknown_usage_and_probe_version():
    _, review = probe_fixture()
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("SYN private error", request=request)

    with make_client(handler) as client:
        result = client.extract_quantitative_probe(review_input=review, allowed_attachment_ids={ATT})
    assert len(requests) == result.outcome.api_calls == 1
    assert result.outcome.status == "REVIEW"
    assert result.outcome.error_code == "NETWORK_ERROR"
    assert result.outcome.prompt_version == QUANTITATIVE_PROBE_PROMPT_VERSION
    assert result.outcome.openai_telemetry.total_tokens is None
    assert result.outcome.openai_telemetry.attempts[0].response_received is False
    assert "SYN private error" not in result.model_dump_json()


def test_probe_wrapper_rejects_production_completion_claims():
    _, review = probe_fixture()
    outcome = ExtractionOutcome(status="REVIEW", message="SYN", api_calls=0)
    with pytest.raises(ValidationError, match="PROBE_CONTRACT_REQUIRED"):
        QuantitativeProbeOutcome(source_audit=review.audit(), outcome=outcome)
    outcome.prompt_version = QUANTITATIVE_PROBE_PROMPT_VERSION
    for field in ("persistence_eligible", "attachment_coverage_complete"):
        with pytest.raises(ValidationError, match="PROBE_CONTRACT_REQUIRED"):
            QuantitativeProbeOutcome(source_audit={**review.audit(), field: True}, outcome=outcome)
