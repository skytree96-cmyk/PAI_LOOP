"""SYN source proofs, not geometry inferred from an extractor's quotation style."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from pai_loop.integrations.openai_extraction import (
    ExtractionPayload, OpenAIExtractionClient, OpenAITelemetry, _corrective_structure_changed,
)
from pai_loop.quantitative_rule_extraction import (
    ValidatedQuantitativeAttachmentRecord, bind_quantitative_case_source_context,
    merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
)
from pai_loop.quantitative_scoring import (
    QuantitativeFact, _compiled_case_table_contract, estimate_quantitative_score,
    quantitative_request_from_candidate_profile,
)
from test_quantitative_count_ranges import fixture as count_fixture

ATT = "SYN-COUNT-RANGE"
DOC = "a" * 64
MANIFEST = "b" * 64


def anchor(quote):
    return dict(attachment_id=ATT, page=1, section="SYN", quote=quote, confidence=1)


def record(raw, source):
    return validate_quantitative_attachment_extraction(
        ExtractionPayload.model_validate(raw), source_text=source, attachment_id=ATT,
        document_sha256=DOC, manifest_sha256=MANIFEST,
    )


def request(stored):
    restored = ValidatedQuantitativeAttachmentRecord.model_validate_json(stored.model_dump_json())
    profile = merge_validated_quantitative_records([restored], expected_documents={ATT: DOC}, manifest_sha256=MANIFEST)
    return quantitative_request_from_candidate_profile(profile)


def dense_count():
    raw, source = count_fixture()
    # Whitespace loss is the only simulated PDF change. Every source character
    # and source-bound recognition restriction remains in its original order.
    return raw, "".join(source.split())


def test_short_count_rows_keep_the_complete_parent_and_program_through_bound_engine():
    raw, source = dense_count()
    original = deepcopy(raw)
    stored = record(raw, source)
    assert stored.status == "AVAILABLE", stored.issues
    assert raw == original
    cases = stored.available_candidates[0].cases
    assert cases[0].literal == original["quantitative_tables"][0]["criteria"][0]["cases"][0]["literal"]
    assert cases[-1].literal == "미제출\n0"
    assert cases[0].evidence.quote == cases[-1].evidence.quote
    assert "실적의건수" in cases[-1].evidence.quote
    assert cases[-1].evidence.quote in source
    candidate = stored.available_candidates[0]
    assert _compiled_case_table_contract(candidate) is not None
    corrupt = cases[0].model_copy(update={"evidence": cases[0].evidence.model_copy(
        update={"quote": "SYN unrelated context"},
    )})
    assert _compiled_case_table_contract(candidate.model_copy(update={
        "cases": (corrupt, *cases[1:]),
    })) is None
    req = request(stored)
    assert req.activation_status == "AUTO_ACTIVE", req.activation_reasons
    criterion = req.criteria[0]
    fact = dict(metric_key=criterion.metric_key, status="CONFIRMED", evidence_key=criterion.metric_key,
                evidence_reference="SYN-attested-fact", evidence_sha256="c" * 64,
                fact_binding_sha256=criterion.fact_binding_sha256)
    for value, expected in [(0, None), (1, 3.5), (6, 4.5), (7, 5)]:
        req.facts = [QuantitativeFact(**fact, value=value)]
        assert estimate_quantitative_score(req).estimated_points == expected
    req.facts = [QuantitativeFact(**fact, submission_status="NOT_SUBMITTED")]
    assert estimate_quantitative_score(req).estimated_points == 0


@pytest.mark.parametrize("mutation", [
    "no-parent", "nonadjacent-parent", "duplicate-program", "missing-row",
    "changed-source-digit", "unrelated-quote", "cross-attachment", "low-confidence",
    "blank-boundary", "section-boundary", "competing-claim", "missing-last-row", "extra-tail-row",
])
def test_short_count_proof_does_not_turn_unowned_or_incomplete_source_into_points(mutation):
    raw, source = dense_count()
    criterion = raw["quantitative_tables"][0]["criteria"][0]
    first = criterion["cases"][0]
    if mutation == "no-parent": criterion["recognition_conditions"] = []
    elif mutation == "nonadjacent-parent": source = source.replace("7건이상5", "다른평가항목7건이상5", 1)
    elif mutation == "duplicate-program": source += source
    elif mutation == "missing-row":
        criterion["cases"].pop(2)
        for index, row in enumerate(criterion["cases"], 1): row["row_order"] = index
    elif mutation == "missing-last-row": criterion["cases"].pop()
    elif mutation == "extra-tail-row": source = source.replace("미제출0", "미제출0추가조건1점", 1)
    elif mutation == "changed-source-digit": source = source.replace("7건이상5", "70건이상5", 1)
    elif mutation == "unrelated-quote": first["evidence"]["quote"] = criterion["criterion_literal"]
    elif mutation == "cross-attachment": first["evidence"]["attachment_id"] = "SYN-OTHER"
    elif mutation == "low-confidence": first["evidence"]["confidence"] = 0.2
    elif mutation == "blank-boundary": source = source.replace("7건이상5", "\n\n7건이상5", 1)
    elif mutation == "section-boundary": source = source.replace("7건이상5", "[HWP SECTION 1]7건이상5", 1)
    elif mutation == "competing-claim":
        other = deepcopy(criterion); other["criterion_id"] = "SYN-OTHER"
        raw["quantitative_tables"][0]["criteria"].append(other)
    stored = record(raw, source)
    assert not stored.available_candidates
    assert request(stored).activation_status == "REVIEW_REQUIRED"


def test_frozen_short_row_cannot_drop_its_parent_proof():
    raw, source = dense_count()
    data = record(raw, source).model_dump(mode="json")
    data["available_candidates"][0]["recognition_conditions"] = []
    with pytest.raises(ValidationError):
        ValidatedQuantitativeAttachmentRecord.model_validate(data)


def response_validation(raw, source):
    # This pure response boundary needs no HTTP client, credentials or network.
    client = object.__new__(OpenAIExtractionClient)
    client.model = "SYN-model"
    return client._validate_response(
        {"status": "completed", "output_text": json.dumps(raw, ensure_ascii=False)},
        document_text=source, allowed_attachment_ids={ATT}, api_calls=0,
        openai_telemetry=OpenAITelemetry(),
    )


def test_gateway_quote_binding_is_idempotent_and_does_not_change_corrective_structure():
    raw, source = dense_count()
    parsed = ExtractionPayload.model_validate(raw)
    bound = bind_quantitative_case_source_context(parsed, source=source)
    assert not _corrective_structure_changed(parsed, bound)
    assert bound == bind_quantitative_case_source_context(bound, source=source)
    assert parsed.model_dump(mode="json") == ExtractionPayload.model_validate(raw).model_dump(mode="json")
    outcome = response_validation(bound.model_dump(mode="json"), source)
    assert outcome.status == "ACCEPTED", outcome.error_code
    assert record(outcome.data.model_dump(mode="json"), source).status == "AVAILABLE"


def test_gateway_binds_raw_short_count_quotes_before_anchor_validation():
    raw, source = dense_count()
    outcome = response_validation(raw, source)
    assert outcome.status == "ACCEPTED", outcome.error_code
    assert not _corrective_structure_changed(ExtractionPayload.model_validate(raw), outcome.data)
    assert record(outcome.data.model_dump(mode="json"), source).status == "AVAILABLE"


@pytest.mark.parametrize("foreign", [False, True])
def test_gateway_binding_does_not_exempt_nonquantitative_or_foreign_evidence(foreign):
    raw, source = dense_count()
    evidence = anchor("SYN unverified unrelated requirement")
    if foreign: evidence["attachment_id"] = "SYN-FOREIGN"
    raw["requirements"] = [dict(requirement_id="SYN-R", category="OTHER", logic="SINGLE",
        normalized_condition="SYN requirement", mandatory=False, deadline_basis=None,
        ambiguity_reason=None, evidence=[evidence])]
    bound = bind_quantitative_case_source_context(ExtractionPayload.model_validate(raw), source=source)
    outcome = response_validation(bound.model_dump(mode="json"), source)
    assert outcome.error_code == ("UNKNOWN_ATTACHMENT" if foreign else "UNVERIFIED_QUOTE")


def credit_fixture(*, dual_award=False):
    raw, _ = count_fixture()
    candidate = raw["quantitative_tables"][0]["criteria"][0]
    header = "SYN 기업신용평가등급 (9점)"
    cluster = "신용평가등급\n평점\n회사채\n기업어음\n기업신용평가등급"
    if dual_award: cluster += "\n비율\n배점"
    columns = [
        (["AAA, AA+, AA0, AA-", "A+, A0, A-, BBB+, BBB0"], ["A1, A2+, A20,", "A2-, A3+, A30"],
         ["AAA, AA+, AA0, AA-,", "A+, A0, A-, BBB+, BBB0"], 100),
        (["BBB-, BB+, BB0, BB-"], ["A3-, B+, B0"], ["BBB-, BB+, BB0, BB-"], 90),
        (["B+, B0, B-"], ["B-"], ["B+, B0, B-"], 80),
        (["CCC+ 이하"], ["C 이하"], ["CCC+ 이하"], 70),
    ]
    rows = []
    for index, (bond, paper, enterprise, percent) in enumerate(columns, 1):
        points = percent * 9 / 100
        awards = [f"{percent}%", f"{points:g}점"] if dual_award else [f"{points:g}"]
        literal = "\n".join([*bond, *paper, *enterprise, *awards])
        rows.append(dict(literal=literal, operator="IN", comparison_value=None, comparison_upper_value=None,
                         category_values=enterprise, award_kind="POINTS", award_value=points,
                         row_order=index, evidence=anchor(literal)))
    candidate.update(label="기업신용평가등급", criterion_literal=header, max_points=9,
                     metric="CREDIT_RATING", unit="등급", cases=rows, recognition_conditions=[],
                     required_evidence=["company.credit_rating"], evidence=anchor(header))
    table = raw["quantitative_tables"][0]
    table.update(total_points=9, total_evidence=anchor("정량평가 합계 9점"))
    body = cluster + "\n" + "\n".join(row["literal"] for row in rows) + "\n[주] SYN 신용등급 적용 주의사항"
    source = header + "\n" + "".join(body.split()) + "\n정량평가 합계 9점"
    return raw, source


@pytest.mark.parametrize("dual_award", [False, True])
@pytest.mark.parametrize("unit", ["등급", None])
def test_three_instrument_columns_project_only_complete_enterprise_values(dual_award, unit):
    raw, source = credit_fixture(dual_award=dual_award)
    raw["quantitative_tables"][0]["criteria"][0]["unit"] = unit
    original = deepcopy(raw)
    stored = record(raw, source)
    assert stored.status == "AVAILABLE", stored.issues
    assert raw == original
    assert stored.available_candidates[0].unit == unit
    for before, after in zip(raw["quantitative_tables"][0]["criteria"][0]["cases"], stored.available_candidates[0].cases, strict=True):
        assert after.evidence.quote == before["literal"]
        assert after.category_values == tuple(before["category_values"])
        assert after.award_value == before["award_value"]
        assert "A1" not in after.literal
    req = request(stored)
    assert req.activation_status == "AUTO_ACTIVE", req.activation_reasons
    criterion = req.criteria[0]
    assert criterion.unit == "RATING"
    assert estimate_quantitative_score(req).estimated_points is None
    req.facts = [QuantitativeFact(metric_key=criterion.metric_key, status="CONFIRMED", value="A0",
                                 evidence_key=criterion.metric_key, fact_binding_sha256=criterion.fact_binding_sha256)]
    assert estimate_quantitative_score(req).estimated_points == 9


@pytest.mark.parametrize("unit", ["", "점", "%", "원", "SYN-UNKNOWN"])
def test_three_column_credit_does_not_repair_explicit_incompatible_unit(unit):
    raw, source = credit_fixture()
    raw["quantitative_tables"][0]["criteria"][0]["unit"] = unit
    stored = record(raw, source)
    assert not stored.available_candidates
    assert request(stored).activation_status == "REVIEW_REQUIRED"


def test_gateway_keeps_credit_literals_for_corrective_comparison_until_domain_validation():
    raw, source = credit_fixture()
    parsed = ExtractionPayload.model_validate(raw)
    bound = bind_quantitative_case_source_context(parsed, source=source)
    assert bound == parsed
    assert not _corrective_structure_changed(parsed, bound)
    outcome = response_validation(bound.model_dump(mode="json"), source)
    assert outcome.status == "ACCEPTED"
    assert outcome.data.quantitative_tables[0].criteria[0].cases[0].literal == parsed.quantitative_tables[0].criteria[0].cases[0].literal
    assert record(outcome.data.model_dump(mode="json"), source).status == "AVAILABLE"


@pytest.mark.parametrize("dual_award", [False, True])
def test_compressed_credit_quotes_keep_the_same_source_proof_in_the_score_consumer(dual_award):
    raw, source = credit_fixture(dual_award=dual_award)
    for row in raw["quantitative_tables"][0]["criteria"][0]["cases"]:
        row["evidence"]["quote"] = "".join(row["literal"].split())
    outcome = response_validation(raw, source)
    assert outcome.status == "ACCEPTED"
    stored = record(outcome.data.model_dump(mode="json"), source)
    assert stored.status == "AVAILABLE", stored.issues
    req = request(stored)
    assert req.activation_status == "AUTO_ACTIVE", req.activation_reasons
    criterion = req.criteria[0]
    req.facts = [QuantitativeFact(
        metric_key=criterion.metric_key, status="CONFIRMED", value="A0",
        evidence_key=criterion.metric_key, fact_binding_sha256=criterion.fact_binding_sha256,
    )]
    assert estimate_quantitative_score(req).estimated_points == 9
    candidate = stored.available_candidates[0]
    first = candidate.cases[0]
    corrupted = first.model_copy(update={"evidence": first.evidence.model_copy(update={
        "quote": first.evidence.quote.replace("BBB0", "BBB-"),
    })})
    assert _compiled_case_table_contract(candidate.model_copy(update={
        "cases": (corrupted, *candidate.cases[1:]),
    })) is None


@pytest.mark.parametrize("mutation", [
    "no-header", "swapped-header", "duplicate-header", "duplicate-body", "duplicate-row",
    "missing-row", "partial-enterprise", "paper-alias", "missing-paper", "invalid-paper",
    "wrong-award", "wrong-percent", "missing-footer", "foreign-row", "unregistered-key",
])
@pytest.mark.parametrize("unit", ["등급", None])
def test_three_column_repair_requires_source_columns_census_and_exact_awards(mutation, unit):
    raw, source = credit_fixture(dual_award=mutation == "wrong-percent")
    candidate = raw["quantitative_tables"][0]["criteria"][0]
    candidate["unit"] = unit
    cases = candidate["cases"]
    first = cases[0]
    if mutation == "no-header": source = source.replace("기업어음", "SYN다른열", 1)
    elif mutation == "swapped-header": source = source.replace("회사채기업어음", "기업어음회사채", 1)
    elif mutation == "duplicate-header": source = source.replace("회사채기업어음기업신용평가등급", "회사채기업어음기업신용평가등급신용평가등급평점회사채기업어음기업신용평가등급", 1)
    elif mutation == "duplicate-body": source += source
    elif mutation == "duplicate-row": source += "\n" + first["literal"]
    elif mutation == "missing-row": candidate["cases"] = cases[:-1]
    elif mutation == "partial-enterprise": first["category_values"] = ["AAA"]
    elif mutation == "paper-alias": cases[-1]["category_values"] = ["C 이하"]
    elif mutation in {"missing-paper", "invalid-paper"}:
        old = first["literal"]
        new = old.replace("A1, A2+, A20,\nA2-, A3+, A30\n", "" if mutation == "missing-paper" else "SYN알수없는어음\n")
        first.update(literal=new, evidence=anchor(new))
        source = source.replace("".join(old.split()), "".join(new.split()))
    elif mutation == "wrong-award": first["award_value"] = 8
    elif mutation == "wrong-percent":
        old = first["literal"]; new = old.replace("100%", "99%")
        first.update(literal=new, evidence=anchor(new)); source = source.replace("100%", "99%", 1)
    elif mutation == "missing-footer": source = source.replace("[주]", "SYN다른조건")
    elif mutation == "foreign-row":
        other = deepcopy(candidate); other.update(criterion_id="SYN-OTHER", metric="LOCAL_PRESENCE")
        raw["quantitative_tables"][0]["criteria"].append(other)
    elif mutation == "unregistered-key": candidate["required_evidence"] = ["company.other"]
    stored = record(raw, source)
    assert not stored.available_candidates
    assert request(stored).activation_status == "REVIEW_REQUIRED"
