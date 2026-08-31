from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from pai_loop.models import CompanyPerformanceRecord
from pai_loop.performance_records import CertificateStatus, VatBasis
from pai_loop.quantitative_performance import (
    derive_performance_value,
    parse_performance_recognition_scope,
)


AMOUNT_LITERAL = (
    "최근 3년간 지자체, 공공기관 등 (교육, 취업, 행사) 용역 수행완료 실적 "
    "단일용역 최고금액 (1건)"
)
COUNT_LITERAL = (
    "최근 3년간 지자체, 공공기관 등 (교육, 취업, 행사) 용역 수행완료 실적 "
    "실적건수 (0.2억원 이상)"
)


def _record(
    key: str,
    *,
    vat_basis: VatBasis = "INCLUDED",
    certificate_status: CertificateStatus = "ISSUED",
    **changes: object,
) -> CompanyPerformanceRecord:
    values: dict[str, object] = {
        "record_key": key,
        "revision": 1,
        "record_status": "VALIDATED",
        "project_name": "교육 행사 운영",
        "agency": "부산광역시교육청",
        "division": "교육정책과",
        "overview": "공공기관 교육 행사",
        "keywords": ["교육", "행사"],
        "contract_date": date(2025, 1, 1),
        "start_date": date(2025, 1, 1),
        "end_date": date(2025, 6, 1),
        "contract_amount": 250_000_000,
        "vat_basis": vat_basis,
        "completed": True,
        "share_pct": 100,
        "certificate_status": certificate_status,
        "evidence_reference": f"SYN-EVIDENCE-{key}",
    }
    values.update(changes)
    return CompanyPerformanceRecord(**values)


def test_generalized_amount_scope_extracts_disjunction_max_and_unspecified_vat() -> None:
    scope = parse_performance_recognition_scope(
        AMOUNT_LITERAL,
        metric_key="company.performance.amount",
    )

    assert scope is not None
    assert scope.lookback_years == 3
    assert scope.similarity_keywords == ("교육", "취업", "행사")
    assert scope.match_mode == "ANY"
    assert scope.counterparty_scope == "PUBLIC_SECTOR"
    assert scope.counterparty_keywords == ("지자체", "공공기관")
    assert scope.completion_required is True
    assert scope.aggregation == "MAX_SINGLE_AMOUNT"
    assert scope.minimum_single_contract_amount_krw == 0
    assert scope.vat_basis == "UNSPECIFIED"


def test_generalized_count_scope_extracts_parenthesized_per_record_minimum() -> None:
    scope = parse_performance_recognition_scope(
        COUNT_LITERAL,
        metric_key="company.performance.count",
    )

    assert scope is not None
    assert scope.similarity_keywords == ("교육", "취업", "행사")
    assert scope.match_mode == "ANY"
    assert scope.aggregation == "COUNT"
    assert scope.minimum_single_contract_amount_krw == 20_000_000
    assert scope.vat_basis == "UNSPECIFIED"


def test_max_single_amount_keeps_vat_sensitivity_metadata_without_summing() -> None:
    scope = parse_performance_recognition_scope(
        AMOUNT_LITERAL,
        metric_key="company.performance.amount",
    )
    assert scope is not None

    derived = derive_performance_value(
        scope,
        [
            _record("A", contract_amount=250_000_000, vat_basis="INCLUDED"),
            _record(
                "B",
                project_name="취업 박람회",
                overview="공공기관 취업 행사",
                keywords=["취업", "행사"],
                contract_amount=300_000_000,
                vat_basis="EXCLUDED",
            ),
            _record(
                "UNRELATED",
                project_name="시설 유지보수",
                overview="",
                keywords=[],
                contract_amount=900_000_000,
            ),
        ],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert derived.status == "ESTIMATED"
    assert derived.value == 300_000_000
    assert (derived.lower_value, derived.upper_value) == (300_000_000, 330_000_000)
    assert derived.matched_record_keys == ("A", "B")
    band_input = derived.score_band_input
    assert band_input is not None
    assert band_input.range_status == "RANGE"
    assert band_input.aggregation == "MAX_SINGLE_AMOUNT"
    assert band_input.source_vat_basis == "UNSPECIFIED"
    assert band_input.observed_record_vat_bases == ("EXCLUDED", "INCLUDED")
    assert band_input.recognized_record_amounts_krw == (250_000_000, 300_000_000)
    assert [item.record_key for item in band_input.record_inputs] == ["A", "B"]
    assert [item.vat_basis for item in band_input.record_inputs] == [
        "INCLUDED",
        "EXCLUDED",
    ]
    assert band_input.sensitivity_dimensions == ("VAT_BASIS",)


def test_count_applies_per_record_minimum_before_counting() -> None:
    scope = parse_performance_recognition_scope(
        COUNT_LITERAL,
        metric_key="company.performance.count",
    )
    assert scope is not None

    derived = derive_performance_value(
        scope,
        [
            _record("AT-MINIMUM", contract_amount=20_000_000),
            _record(
                "SECOND",
                project_name="취업 교육",
                keywords=["취업"],
                contract_amount=25_000_000,
                vat_basis="EXCLUDED",
            ),
            _record("BELOW", contract_amount=19_999_999),
        ],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert derived.status == "ESTIMATED"
    assert derived.value == 2
    assert derived.matched_record_keys == ("AT-MINIMUM", "SECOND")
    assert derived.score_band_input is not None
    assert derived.score_band_input.range_status == "RANGE"
    assert derived.score_band_input.lower_value == 1
    assert derived.score_band_input.upper_value == 2


def test_count_minimum_accepts_spaced_unit_and_malformed_hint_fails_closed() -> None:
    spaced = parse_performance_recognition_scope(
        COUNT_LITERAL.replace("0.2억원", "0.2억 원"),
        metric_key="company.performance.count",
    )
    assert spaced is not None
    assert spaced.minimum_single_contract_amount_krw == 20_000_000

    assert parse_performance_recognition_scope(
        COUNT_LITERAL.replace("0.2억원", "약 0.2억원"),
        metric_key="company.performance.count",
    ) is None


def test_public_counterparty_and_service_purpose_are_enforced_on_real_records() -> None:
    scope = parse_performance_recognition_scope(
        AMOUNT_LITERAL,
        metric_key="company.performance.amount",
    )
    assert scope is not None

    derived = derive_performance_value(
        scope,
        [
            _record("PUBLIC-EDUCATION", contract_amount=250_000_000),
            _record(
                "PRIVATE-EDUCATION",
                agency="민간 주식회사",
                contract_amount=900_000_000,
            ),
            _record(
                "PUBLIC-FACILITY",
                project_name="교육청 전산실 시설 유지보수",
                overview="교육청 전산실 시설 유지보수",
                keywords=[],
                contract_amount=800_000_000,
            ),
            _record(
                "PUBLIC-TRAVEL-AGENCY-SYSTEM",
                project_name="여행사 예약 시스템 유지보수",
                overview="여행사 예약 시스템 유지보수",
                keywords=[],
                contract_amount=700_000_000,
            ),
        ],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert derived.status == "ESTIMATED"
    assert derived.value == 250_000_000
    assert derived.matched_record_keys == ("PUBLIC-EDUCATION",)


def test_ambiguous_public_agency_fails_closed_instead_of_scoring() -> None:
    scope = parse_performance_recognition_scope(
        AMOUNT_LITERAL,
        metric_key="company.performance.amount",
    )
    assert scope is not None

    derived = derive_performance_value(
        scope,
        [_record("UNKNOWN-AGENCY", agency="한빛공사", contract_amount=900_000_000)],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    assert derived.status == "REVIEW"
    assert derived.value is None


def test_max_requires_explicit_amount_word() -> None:
    scope = parse_performance_recognition_scope(
        "최근 3년 AI 교육 관련 수행완료 실적, 단일 용역 최대 6점",
        metric_key="company.performance.amount",
    )
    assert scope is not None
    assert scope.aggregation == "SUM_AMOUNT"


def test_bid_notice_anchor_is_preserved_and_requires_matching_as_of_basis() -> None:
    scope = parse_performance_recognition_scope(
        AMOUNT_LITERAL + " 최근 3년간은 입찰공고일을 기준으로 한다.",
        metric_key="company.performance.amount",
    )
    assert scope is not None
    assert scope.lookback_anchor_basis == "BID_NOTICE_DATE"

    unresolved = derive_performance_value(
        scope,
        [_record("ANCHOR")],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    assert unresolved.status == "REVIEW"

    resolved = derive_performance_value(
        scope,
        [_record("ANCHOR")],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
        as_of_basis="BID_NOTICE_DATE",
    )
    assert resolved.status == "ESTIMATED"
    assert resolved.score_band_input is not None
    assert resolved.score_band_input.lookback_anchor_basis == "BID_NOTICE_DATE"
    assert resolved.score_band_input.evaluation_as_of_basis == "BID_NOTICE_DATE"


def test_oversized_source_literal_fails_closed_without_validation_error() -> None:
    literal = AMOUNT_LITERAL + " " + ("추가원문 " * 400)
    assert len(literal) > 2_000
    assert parse_performance_recognition_scope(
        literal,
        metric_key="company.performance.amount",
    ) is None


def test_unspecified_vat_returns_two_scenario_amount_range() -> None:
    scope = parse_performance_recognition_scope(
        AMOUNT_LITERAL,
        metric_key="company.performance.amount",
    )
    assert scope is not None

    derived = derive_performance_value(
        scope,
        [_record("VAT-EDGE", contract_amount=210_000_000, vat_basis="INCLUDED")],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert derived.status == "ESTIMATED"
    assert derived.lower_value == pytest.approx(210_000_000 / 1.1)
    assert derived.upper_value == 210_000_000
    assert derived.score_band_input is not None
    assert derived.score_band_input.range_status == "RANGE"


def test_unspecified_vat_applies_count_minimum_per_scenario() -> None:
    scope = parse_performance_recognition_scope(
        COUNT_LITERAL,
        metric_key="company.performance.count",
    )
    assert scope is not None

    derived = derive_performance_value(
        scope,
        [_record("VAT-COUNT-EDGE", contract_amount=20_000_000, vat_basis="INCLUDED")],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert derived.status == "ESTIMATED"
    assert derived.lower_value == 0
    assert derived.upper_value == 1
    assert derived.score_band_input is not None
    assert derived.score_band_input.range_status == "RANGE"


def test_review_preserves_only_a_lower_bound_for_score_band_sensitivity() -> None:
    scope = parse_performance_recognition_scope(
        AMOUNT_LITERAL,
        metric_key="company.performance.amount",
    )
    assert scope is not None

    derived = derive_performance_value(
        scope,
        [
            _record("KNOWN", contract_amount=250_000_000),
            _record("UNKNOWN-SHARE", contract_amount=400_000_000, share_pct=None),
        ],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert derived.status == "REVIEW"
    assert derived.value is None
    assert derived.lower_value == pytest.approx(250_000_000 / 1.1)
    assert derived.upper_value is None
    band_input = derived.score_band_input
    assert band_input is not None
    assert band_input.range_status == "LOWER_BOUND_ONLY"
    assert band_input.lower_value == pytest.approx(250_000_000 / 1.1)
    assert band_input.upper_value is None
    assert band_input.sensitivity_dimensions == ("VAT_BASIS", "RECORD_ELIGIBILITY")


def test_unspecified_source_vat_still_rejects_unknown_record_vat() -> None:
    scope = parse_performance_recognition_scope(
        COUNT_LITERAL,
        metric_key="company.performance.count",
    )
    assert scope is not None

    derived = derive_performance_value(
        scope,
        [_record("UNKNOWN-VAT", contract_amount=30_000_000, vat_basis="UNKNOWN")],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert derived.status == "REVIEW"
    assert derived.value is None
    assert derived.score_band_input is None


def test_missing_completion_dimension_remains_fail_closed() -> None:
    assert parse_performance_recognition_scope(
        "최근 3년 (교육, 취업, 행사) 용역 실적",
        metric_key="company.performance.amount",
    ) is None
