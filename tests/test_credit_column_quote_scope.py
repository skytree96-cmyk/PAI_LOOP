"""A three-column credit row quoted as its enterprise column alone.

Extraction sometimes quotes one row of a 회사채/기업어음/기업신용평가 table as the
enterprise column only, while quoting its siblings as whole rows. The column
projection is proved from the source itself, so the narrower quote is accepted
when it equals that projection — and only then. Because the projection rebuilds
one contiguous source region from the original row quotes, it must run before
split-cell rebinding narrows a row.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.quantitative_rule_extraction import (
    validate_quantitative_attachment_extraction,
)

from test_dense_case_source_binding import ATT, MANIFEST, credit_fixture


def _record(raw, source):
    return validate_quantitative_attachment_extraction(
        ExtractionPayload.model_validate(raw), source_text=source,
        attachment_id=ATT, document_sha256=hashlib.sha256(source.encode()).hexdigest(),
        manifest_sha256=MANIFEST,
    )


def _enterprise_only(case) -> str:
    """The enterprise column plus the award, as the projection derives it.

    The enterprise cell occupies one source line per declared category value,
    so a wrapped cell keeps every one of its lines.
    """
    lines = case["literal"].splitlines()
    return "\n".join(lines[-(len(case["category_values"]) + 1):])


def test_baseline_three_column_table_is_available():
    raw, source = credit_fixture()
    assert _record(raw, source).status == "AVAILABLE"


@pytest.mark.parametrize("row", [0, 1, 2, 3])
def test_a_row_quoted_as_its_enterprise_column_still_binds(row):
    raw, source = credit_fixture()
    case = raw["quantitative_tables"][0]["criteria"][0]["cases"][row]
    case["evidence"]["quote"] = _enterprise_only(case)
    original = deepcopy(raw)

    record = _record(raw, source)
    assert record.status == "AVAILABLE", record.issues
    assert len(record.available_candidates) == 1
    # The stored raw extraction is never rewritten in place.
    assert raw == original


def test_every_row_may_be_quoted_as_its_enterprise_column():
    raw, source = credit_fixture()
    for case in raw["quantitative_tables"][0]["criteria"][0]["cases"]:
        case["evidence"]["quote"] = _enterprise_only(case)
    assert _record(raw, source).status == "AVAILABLE"


@pytest.mark.parametrize(
    "quote",
    [
        "AAA, AA+, AA0, AA-\nA+, A0, A-, BBB+, BBB0\n9",  # bond column, not enterprise
        "A1, A2+, A20,\nA2-, A3+, A30\n9",                # commercial paper column
        "AAA, AA+, AA0, AA-,\nA+, A0, A-, BBB+, BBB0\n8.1",  # award of another row
        "AAA, AA+, AA0, AA-,\nA+, A0, A-, BBB+, BBB0",    # award dropped
    ],
)
def test_a_narrower_quote_that_is_not_the_projection_is_rejected(quote):
    raw, source = credit_fixture()
    raw["quantitative_tables"][0]["criteria"][0]["cases"][0]["evidence"]["quote"] = quote
    record = _record(raw, source)
    assert not record.available_candidates
    assert record.status != "AVAILABLE"


def test_a_quote_outside_the_source_is_still_rejected():
    raw, source = credit_fixture()
    raw["quantitative_tables"][0]["criteria"][0]["cases"][0]["evidence"]["quote"] = (
        "SYN 기업신용평가등급 A0\n9"
    )
    assert not _record(raw, source).available_candidates
