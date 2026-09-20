"""Printed interval instructions reach every extraction entry, with no paid calls."""
import json

import httpx
import pytest

from pai_loop.integrations.openai_extraction import EXTRACTION_SCHEMA, OpenAIExtractionClient
from pai_loop.long_output_policy import LONG_OUTPUT_ONCE, LONG_OUTPUT_TIMEOUT_SECONDS, LONG_OUTPUT_TOKENS
from pai_loop.quantitative_probe_policy import (
    QUANTITATIVE_PROBE_ONCE,
    QUANTITATIVE_PROBE_CLIENT_TIMEOUT_SECONDS,
    QUANTITATIVE_PROBE_OUTPUT_TOKENS,
)
from pai_loop.quantitative_review_input import build_quantitative_review_input


SOURCE = "[PAGE 1]\nSYN 인원 평가 3점\n2~4명 3점\nSYN 비율 평가 2점\n20% 이상 40% 미만 2점"


@pytest.fixture(params=["full", "probe", "long_output"])
def sent_prompt(request):
    """Exercise the real request builder; synthetic replies do not test an LLM."""
    requests = []
    dispatches = []

    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json={
            "id": "SYN-interval-response", "model": "SYN-model", "status": "completed",
            "output_text": json.dumps({
                "document_type": "RFP", "requirements": [], "quantitative_tables": [],
                "quantitative_table_not_applicable": None, "missing_or_unreadable": [],
                "summary": "SYN prompt contract fixture",
            }),
        })

    options = dict(
        api_key="SYN-key", model="SYN-model", provider="n8n_claude",
        base_url="https://syn-gateway.test/v1", max_total_api_calls=1, max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    if request.param == "probe":
        options.update(
            budget_policy=QUANTITATIVE_PROBE_ONCE,
            max_output_tokens=QUANTITATIVE_PROBE_OUTPUT_TOKENS,
            timeout_seconds=QUANTITATIVE_PROBE_CLIENT_TIMEOUT_SECONDS,
            before_request=lambda: dispatches.append(True),
        )
    elif request.param == "long_output":
        options.update(
            budget_policy=LONG_OUTPUT_ONCE,
            max_output_tokens=LONG_OUTPUT_TOKENS,
            timeout_seconds=LONG_OUTPUT_TIMEOUT_SECONDS,
            before_request=lambda: dispatches.append(True),
        )
    with OpenAIExtractionClient(**options) as client:
        if request.param == "probe":
            result = client.extract_quantitative_probe(
                review_input=build_quantitative_review_input(SOURCE, reviewed_pages=[1]),
                allowed_attachment_ids={"SYN-ATT"},
            )
            assert result.persistence_eligible is False
            outcome = result.outcome
        else:
            outcome = client.extract(document_text=SOURCE, allowed_attachment_ids={"SYN-ATT"})
    assert outcome.api_calls == len(requests) == 1
    assert len(dispatches) == (0 if request.param == "full" else 1)
    body = requests[0]
    assert body["text"]["format"]["schema"] == EXTRACTION_SCHEMA
    assert body["max_output_tokens"] == (32_000 if request.param == "long_output" else 20_000)
    prompt, transmitted_source = body["input"][1]["content"][0]["text"].rsplit("\n\nSOURCE:\n", 1)
    assert transmitted_source == SOURCE
    return prompt


def test_printed_two_sided_intervals_keep_both_bounds(sent_prompt):
    assert "Use BRACKET for continuous numeric rows with both printed endpoints" in sent_prompt
    assert "ascending, non-overlapping numeric interval tables" in sent_prompt
    assert "min_value=20, max_value=40, min_inclusive=true, max_inclusive=false and points=2" in sent_prompt
    assert "never encode this bounded row as only GTE 20" in sent_prompt
    assert "including personnel counts, 2~4명 3점 has min_value=2, max_value=4" in sent_prompt
    assert "min_inclusive=true, max_inclusive=true and points=3" in sent_prompt


def test_source_order_metric_and_missing_boundaries_are_not_rewritten(sent_prompt):
    assert "Preserve BRACKET array order and CASE_TABLE row_order in printed source order" in sent_prompt
    assert "never reverse or sort source rows" in sent_prompt
    assert "never borrow a boundary from another row or criterion" in sent_prompt
    assert "never invent a complementary band" in sent_prompt
    assert "a percent interval alone does not make a criterion FINANCIAL_RATIO" in sent_prompt
    assert "must never be converted to KRW" in sent_prompt
    assert "unsupported or ambiguous semantics must remain for review" in sent_prompt


def test_existing_supported_case_programs_remain_in_the_prompt(sent_prompt):
    assert "source-ordered descending overlapping GTE cutoffs" in sent_prompt
    assert "rating/category groups, or percentage-of-maximum rows" in sent_prompt
    assert "Preserve these ordered lower cutoffs rather than inventing unprinted upper bounds" in sent_prompt
    assert "BETWEEN with comparison_value=5, comparison_upper_value=6 and empty category_values" in sent_prompt
    assert "Do not replace that valid BETWEEN program merely because a row has two endpoints" in sent_prompt
    assert "does not authorize converting a percentage-of-maximum award into a literal points award" in sent_prompt
    assert "NOT_SUBMITTED with both comparisons null, category_values=[], and POINTS 0" in sent_prompt
    assert "BETWEEN and NOT_SUBMITTED currently apply only to PERFORMANCE_COUNT" in sent_prompt
