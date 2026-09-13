"""SYN source rules may be valid before their company evidence is available."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from pai_loop.quantitative_performance import (
    PerformanceRecognitionScope, derive_performance_value, parse_performance_recognition_scope,
)
from pai_loop.quantitative_scoring import estimate_quantitative_score
from test_quantitative_count_ranges import fact, fixture, request


@pytest.mark.parametrize('clause', [
    '단일규모 계약금액 5천만원 이상, 50인 이상만 해당',
    '단일 계약 당 연간 기준 총 계약금액 1억원 이상',
])
def test_source_rule_reaches_engine_but_only_bound_verified_count_scores(clause):
    payload, source = fixture()
    condition = payload['quantitative_tables'][0]['criteria'][0]['recognition_conditions'][0]
    original = condition['literal']
    condition['literal'] += ', ' + clause
    condition['evidence']['quote'] = condition['literal']
    source = source.replace(original, condition['literal'])
    req = request(payload, source)
    assert req.activation_status == 'AUTO_ACTIVE'
    scope = req.criteria[0].performance_scope
    assert scope.manual_verification_conditions
    assert PerformanceRecognitionScope.model_validate_json(scope.model_dump_json()) == scope
    assert estimate_quantitative_score(req).estimated_points is None

    derived = derive_performance_value(scope, [], as_of=datetime(2026, 8, 10, tzinfo=timezone.utc))
    assert derived.status == 'REVIEW'
    assert derived.value is derived.lower_value is derived.upper_value is None
    assert derived.score_band_input is None

    # This is a SYN verified aggregate attestation, not an inferred register
    # count. The fact identity binds the complete recognition clause too.
    req.facts = [fact(req, value=7)]
    assert estimate_quantitative_score(req).confirmed_points == 5
    req.facts = [fact(req, value=7, fact_binding_sha256='f'*64)]
    assert estimate_quantitative_score(req).estimated_points is None


def test_extra_conditions_cannot_be_forged_or_dropped_from_register_validation():
    source = '최근 3년 해외연수 관련 수행완료 실적 50인 이상'
    scope = parse_performance_recognition_scope(source, metric_key='company.performance.count')
    assert scope is not None
    assert scope.manual_verification_conditions == ('50인 이상',)
    with pytest.raises(ValidationError):
        PerformanceRecognitionScope.model_validate({**scope.model_dump(), 'manual_verification_conditions':['5인 이상']})
    # Old shapes can load, but omitting their new field cannot disable the
    # source-derived guard or create a numeric lower bound.
    old = scope.model_dump()
    old.pop('manual_verification_conditions')
    old_scope = PerformanceRecognitionScope.model_validate(old)
    result = derive_performance_value(old_scope, [], as_of=datetime(2026,8,10,tzinfo=timezone.utc))
    assert result.status == 'REVIEW'
    assert result.lower_value is None


def test_annual_amount_is_not_silently_reused_as_whole_contract_minimum():
    scope = parse_performance_recognition_scope(
        '최근 2년 외국어 관련 용역 완성(준공)된 용역 이행실적 단일 계약 당 연간 기준 총 계약금액 1억원 이상',
        metric_key='company.performance.count',
    )
    assert scope is not None
    assert scope.completion_required
    assert scope.manual_verification_conditions == ('연간 기준 총 계약금액 1억원 이상',)
    assert scope.minimum_single_contract_amount_krw == 0


def test_explicit_single_scale_contract_minimum_is_retained_with_participants():
    scope = parse_performance_recognition_scope(
        '최근 3년 해외연수 관련 수행완료 실적 단일규모 계약금액 5,000만원 이상, 50인 이상',
        metric_key='company.performance.count',
    )
    assert scope is not None
    assert scope.minimum_single_contract_amount_krw == 50_000_000
    assert scope.manual_verification_conditions == ('50인 이상',)


def test_no_similarity_scope_is_accepted_for_automatic_register_count():
    assert parse_performance_recognition_scope(
        '최근 3년 수행완료 실적', metric_key='company.performance.count',
    ) is None
    manual = parse_performance_recognition_scope(
        '최근 3년 수행완료 실적 계약별 50명 이상', metric_key='company.performance.count',
    )
    assert manual is not None
    assert manual.similarity_keywords == ()
    assert derive_performance_value(manual, [], as_of=datetime(2026,8,10,tzinfo=timezone.utc)).value is None
