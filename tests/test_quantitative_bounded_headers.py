"""Synthetic lossless header reconstruction, without guessing score signs."""
from copy import deepcopy

import pytest

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.quantitative_rule_extraction import (
    _rebind_numbered_criterion_headers,
    validate_quantitative_attachment_extraction,
)
from test_quantitative_rule_extraction import valid_table


ATTACHMENT = "SYN-HEADER"


def _fixture(metric="PERSONNEL_COUNT"):
    table = valid_table()
    row = table["criteria"][0]
    personnel = metric == "PERSONNEL_COUNT"
    label = "SYN 전문인력 현황" if personnel else "SYN 사업 수행실적"
    body = "SYN 수행인력\n보유상태" if personnel else "SYN 최근 4년 단일 계약 2억원 이상 수행실적"
    unit = "명" if personnel else "건"
    summary = f"다 {label}\n20"
    header = f"7) {label} 평가 기준 및 배점: 20점\n평 가 항 목\n평 가 기 준\n배 점\n{body}"
    row.update(criterion_id="SYN-ROW", metric=metric, unit=unit, label=label,
               criterion_literal=f"{' '.join(body.split())} 평가 기준 및 배점: 20점",
               required_evidence=["company.personnel.count" if personnel else "company.performance.count"])
    row["evidence"]["quote"] = summary
    for bracket in row["brackets"]:
        for key in ("label", "literal"):
            bracket[key] = bracket[key].replace("억원", unit)
        bracket["evidence"]["quote"] = bracket["literal"]
    table["table_id"] = "SYN-TABLE"
    table["minimum_score"] = table["minimum_evidence"] = None

    def rebind(value):
        if isinstance(value, dict):
            return {key: ATTACHMENT if key == "attachment_id" else rebind(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rebind(item) for item in value]
        return value

    raw = {"document_type": "RFP", "requirements": [], "quantitative_tables": [rebind(table)],
           "missing_or_unreadable": [], "summary": "SYN bounded header"}
    source = "\n".join((summary, "", header, *(bracket["literal"] for bracket in row["brackets"]), table["total_evidence"]["quote"]))
    return raw, source, header


def _repair(raw, source):
    return _rebind_numbered_criterion_headers(ExtractionPayload.model_validate(raw), source=source, attachment_id=ATTACHMENT)


@pytest.mark.parametrize("metric", ["PERSONNEL_COUNT", "PERFORMANCE_COUNT"])
def test_exact_numbered_header_reorders_unchanged_body_and_passes_real_validator(metric):
    raw, source, header = _fixture(metric)
    before = deepcopy(raw)
    payload = _repair(raw, source)
    old, new = ExtractionPayload.model_validate(raw).quantitative_tables[0].criteria[0], payload.quantitative_tables[0].criteria[0]
    assert new.criterion_literal == new.evidence.quote == header
    assert new.model_dump(exclude={"criterion_literal", "evidence"}) == old.model_dump(exclude={"criterion_literal", "evidence"})
    assert new.evidence.model_dump(exclude={"quote"}) == old.evidence.model_dump(exclude={"quote"})
    assert raw == before
    record = validate_quantitative_attachment_extraction(
        ExtractionPayload.model_validate(raw), source_text=source, attachment_id=ATTACHMENT,
        document_sha256="a" * 64, manifest_sha256="b" * 64,
    )
    assert record.status == "AVAILABLE", [issue.code for issue in record.issues]
    assert record.available_candidates[0].criterion_literal == header


@pytest.mark.parametrize("defect", [
    "changed_max", "negative_max", "other_title", "blank_boundary", "section_boundary",
    "missing_column", "changed_body", "repeated_body", "repeated_summary", "foreign_claim", "extra_body_line",
])
def test_uncertain_header_cannot_borrow_a_maximum_or_cross_another_claim(defect):
    raw, source, header = _fixture()
    if defect == "changed_max":
        source = source.replace("배점: 20점", "배점: 19점")
    elif defect == "negative_max":
        source = source.replace("배점: 20점", "배점: -20점")
    elif defect == "other_title":
        source = source.replace("7) SYN 전문인력 현황", "7) SYN 별도 인력 현황")
    elif defect in {"blank_boundary", "section_boundary"}:
        source = source.replace("평 가 기 준", "" if defect == "blank_boundary" else "[HWP SECTION 2]")
    elif defect == "missing_column":
        source = source.replace("평 가 항 목\n", "")
    elif defect == "changed_body":
        source = source.replace("SYN 수행인력\n보유상태", "SYN 다른 인력\n보유상태")
    elif defect == "repeated_body":
        source += "\nSYN 수행인력\n보유상태"
    elif defect == "repeated_summary":
        source += "\n" + raw["quantitative_tables"][0]["criteria"][0]["evidence"]["quote"]
    elif defect == "foreign_claim":
        other = deepcopy(raw["quantitative_tables"][0]["criteria"][0])
        other["criterion_id"] = "SYN-OTHER"
        other["evidence"]["quote"] = "SYN 수행인력\n보유상태"
        raw["quantitative_tables"][0]["criteria"].append(other)
    elif defect == "extra_body_line":
        source = source.replace("SYN 수행인력\n보유상태", "SYN 수행인력\nSYN 다른 조건\n보유상태")
    repaired = _repair(raw, source).quantitative_tables[0].criteria[0]
    assert repaired.criterion_literal == raw["quantitative_tables"][0]["criteria"][0]["criterion_literal"]
    assert repaired.evidence.quote != header


@pytest.mark.parametrize("literal", ["4명 이상-12점", "4명 이상 -12점", "4명 이상 감점 -12점", "4명 이상 −12점"])
def test_a_hyphen_or_negative_sign_is_not_positive_award_evidence(literal):
    from pai_loop.integrations.openai_extraction import QuantitativeCaseLiteral, QuantitativeRuleCandidate
    from pai_loop.quantitative_rule_extraction import _case_award_matches_literal

    raw, _source, _header = _fixture()
    candidate = QuantitativeRuleCandidate.model_validate(raw["quantitative_tables"][0]["criteria"][0])
    case = QuantitativeCaseLiteral(operator="GTE", comparison_value=4, category_values=[],
        literal=literal, award_kind="POINTS", award_value=12, row_order=1,
        evidence={**candidate.evidence.model_dump(), "quote": literal})
    assert not _case_award_matches_literal(candidate, case, literal)
