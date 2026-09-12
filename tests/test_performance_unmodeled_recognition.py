"""Source-bound safeguards; SYN register rows never imply missing dimensions."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from pai_loop.quantitative_performance import (
    derive_performance_value,
    parse_performance_recognition_scope,
)
from pai_loop.quantitative_scoring import (
    QuantitativeCriterion, ScoreBracket, resolve_performance_register_facts,
)


BASE = "최근 3년간 (교육, 행사) 용역 수행완료 실적 VAT 포함 "
AS_OF = datetime(2026, 8, 10, tzinfo=timezone.utc)


def _record(key: str = "SYN-PERFORMANCE", **changes: object) -> SimpleNamespace:
    values = dict(
        record_key=key, revision=1, record_status="VALIDATED",
        project_name="SYN 교육 행사", keywords=["교육", "행사"],
        agency="SYN 기관", overview="SYN 사업 소개, 연간 100명 교육 운영",
        end_date=date(2025, 6, 1), start_date=date(2024, 1, 1),
        contract_date=date(2024, 1, 1), completed=True,
        evidence_reference="SYN-contract-evidence", vat_basis="INCLUDED",
        share_pct=100, contract_amount=200_000_000,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def _criterion(scope) -> QuantitativeCriterion:
    return QuantitativeCriterion(
        criterion_id="SYN-PERFORMANCE", category="SYN", label="SYN performance",
        max_points=5, metric_key=scope.metric_key, formula_type="BRACKET",
        formula="SYN source band", performance_scope=scope,
        brackets=[ScoreBracket(bracket_id="SYN-TOP", label="SYN top band",
                               min_value=1, points=5)],
        fact_binding_sha256="a" * 64,
    )


@pytest.mark.parametrize("metric", ["company.performance.count", "company.performance.amount"])
@pytest.mark.parametrize(
    ("clause", "reason"),
    [
        ("단일 계약 5천만원 이상, 50인 이상만 해당", "참여 인원"),
        ("단일규모 계약금액5천만원 이상,50인 이상만 해당", "참여 인원"),
        ("계약별 참여 인원 50명 이상", "참여 인원"),
        ("계약별 최소 50명 인정", "참여 인원"),
        ("단일 계약 당 연간 기준 총 계약금액 1억원 이상", "연간 계약금액"),
        ("연간 계약금액 1억원 이상만 인정", "연간 계약금액"),
    ],
)
def test_unmodeled_source_dimensions_block_new_and_stored_scopes(
    metric: str, clause: str, reason: str
) -> None:
    assert parse_performance_recognition_scope(BASE + clause, metric_key=metric) is None
    prior_scope = parse_performance_recognition_scope(BASE, metric_key=metric)
    assert prior_scope is not None
    # A prior parser could have persisted a scope that ignored this clause.
    prior_scope = prior_scope.model_copy(update={"source_literal": BASE + clause})
    derived = derive_performance_value(
        prior_scope, [_record(f"SYN-ROW-{index}") for index in range(20)], as_of=AS_OF,
    )

    assert derived.status == "REVIEW"
    assert reason in derived.rationale
    assert derived.value is derived.lower_value is derived.upper_value is None
    assert derived.score_band_input is None
    assert derived.matched_record_keys == ()
    facts = resolve_performance_register_facts(
        [_criterion(prior_scope)],
        [_record()], as_of=AS_OF,
    )
    assert len(facts) == 1
    assert facts[0].status == "REVIEW"
    assert facts[0].lower_value is None  # Never a full-score-saturating lower bound.


@pytest.mark.parametrize(
    "clause", ["단일 계약 5천만원 이상", "실적 50건 이상", "50인승 버스 운영"]
)
def test_guard_preserves_supported_money_count_and_vehicle_wording(clause: str) -> None:
    scope = parse_performance_recognition_scope(BASE + clause, metric_key="company.performance.count")
    assert scope is not None
    derived = derive_performance_value(scope, [_record()], as_of=AS_OF)

    assert derived.status == "ESTIMATED"
    assert derived.value == 1


@pytest.mark.parametrize("anchor", [
    "공고일 기준", "공고일자를 기준", "입찰공고일을 기준",
    "본입찰공고일 기준", "최근입찰공고일 기준",
])
def test_explicit_notice_anchor_requires_notice_date(anchor: str) -> None:
    scope = parse_performance_recognition_scope(BASE + anchor, metric_key="company.performance.count")
    assert scope is not None
    assert scope.lookback_anchor_basis == "BID_NOTICE_DATE"
    mismatched = derive_performance_value(
        scope, [_record()], as_of=AS_OF, as_of_basis="SUBMISSION_DEADLINE",
    )
    assert mismatched.status == "REVIEW"
    assert mismatched.value is mismatched.lower_value is None
    facts = resolve_performance_register_facts(
        [_criterion(scope)],
        [_record()], as_of=datetime(2026, 9, 10, tzinfo=timezone.utc),
        bid_notice_at=AS_OF,
    )
    assert facts[0].status == "ESTIMATED"
    assert facts[0].value == 1


@pytest.mark.parametrize("anchor", ["본입찰공고일 기준", "최근입찰공고일 기준"])
def test_attached_notice_qualifier_never_uses_later_submission_deadline(anchor: str) -> None:
    scope = parse_performance_recognition_scope(BASE + anchor, metric_key="company.performance.count")
    assert scope is not None
    assert scope.lookback_anchor_basis == "BID_NOTICE_DATE"
    facts = resolve_performance_register_facts(
        [_criterion(scope)],
        [_record("SYN-AFTER-NOTICE", end_date=date(2026, 8, 20))],
        as_of=datetime(2026, 9, 10, tzinfo=timezone.utc), bid_notice_at=AS_OF,
    )

    assert facts[0].status == "UNSCORABLE"
    assert facts[0].value is facts[0].lower_value is None


def test_stored_scope_cannot_silently_omit_explicit_notice_anchor() -> None:
    scope = parse_performance_recognition_scope(BASE, metric_key="company.performance.count")
    assert scope is not None
    scope = scope.model_copy(update={"source_literal": BASE + "공고일 기준"})
    derived = derive_performance_value(scope, [_record()], as_of=AS_OF)

    assert derived.status == "REVIEW"
    assert "저장된 인정조건" in derived.rationale
    assert derived.value is derived.lower_value is None


@pytest.mark.parametrize("notice_form", ["입찰공고일", "본입찰공고일", "최근입찰공고일"])
def test_completion_before_notice_excludes_same_day_without_shifting_lookback_start(notice_form: str) -> None:
    scope = parse_performance_recognition_scope(
        BASE + f"공고일 기준, {notice_form} 전일까지 완료된 실적만 인정",
        metric_key="company.performance.count",
    )
    assert scope is not None
    derived = derive_performance_value(
        scope,
        [
            _record("SYN-BEFORE-LOOKBACK", end_date=date(2023, 8, 9)),
            _record("SYN-LOOKBACK-START", end_date=date(2023, 8, 10)),
            _record("SYN-DAY-BEFORE", end_date=date(2026, 8, 9)),
            _record("SYN-NOTICE-DAY", end_date=date(2026, 8, 10)),
            _record("SYN-AFTER-NOTICE", end_date=date(2026, 8, 11)),
        ],
        as_of=AS_OF, as_of_basis="BID_NOTICE_DATE",
    )

    assert derived.status == "ESTIMATED"
    assert derived.value == 2
    assert derived.matched_record_keys == ("SYN-DAY-BEFORE", "SYN-LOOKBACK-START")
    assert derived.score_band_input.evaluation_as_of_date == date(2026, 8, 10)


def test_prior_day_clause_without_verified_notice_basis_stays_review() -> None:
    scope = parse_performance_recognition_scope(
        BASE + "입찰공고일 전일까지 완료된 실적만 인정",
        metric_key="company.performance.count",
    )
    assert scope is not None
    derived = derive_performance_value(scope, [_record()], as_of=AS_OF)

    assert derived.status == "REVIEW"
    assert "공고일 전일까지" in derived.rationale
    assert derived.value is derived.lower_value is None


def test_notice_date_completion_remains_included_without_explicit_prior_day_clause() -> None:
    scope = parse_performance_recognition_scope(BASE + "공고일 기준", metric_key="company.performance.count")
    assert scope is not None
    derived = derive_performance_value(
        scope, [_record(end_date=date(2026, 8, 10))],
        as_of=AS_OF, as_of_basis="BID_NOTICE_DATE",
    )
    assert derived.status == "ESTIMATED"
    assert derived.value == 1


def test_conflicting_explicit_notice_and_deadline_anchors_fail_closed() -> None:
    assert parse_performance_recognition_scope(
        BASE + "공고일 기준, 제출 마감일 기준", metric_key="company.performance.count",
    ) is None
