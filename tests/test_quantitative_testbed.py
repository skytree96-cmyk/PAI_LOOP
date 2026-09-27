"""정량 점수 사슬의 종단 검증. 합성 데이터만 쓴다.

여기서 검증하는 것은 점수 자체가 아니라 점수를 낼 자격이다. 증빙이 없거나 공고에
결합되지 않았거나 마감 뒤에 발급됐다면 0점도 만점도 아닌 '모름'이어야 한다. 모름을
0점으로 접으면 될 공고를 버리고, 만점으로 접으면 안 될 입찰에 들어간다.

하네스는 ``tests/quantitative_testbed.py`` 에 있다. 새 지표는 그 파일의
``METRIC_PROFILES`` 에 한 줄을 더하면 되고, 이 파일의 표들이 자동으로 늘어난다.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from pai_loop.quantitative_scoring import _CANONICAL_METRIC_REGISTRY
from quantitative_testbed import (
    AS_OF,
    CREDIT_BANDS,
    METRIC_PROFILES,
    OTHER_BINDING,
    MetricProfile,
    build_company_fact,
    build_criterion,
    score,
)

#: (지표, 계산식) 조합 전체. 지표를 등록하면 이 표가 저절로 늘어난다.
MATRIX = [
    pytest.param(metric, kind, id=f"{metric}-{kind}")
    for metric, profile in METRIC_PROFILES.items()
    for kind in profile.formulas
]
#: 구간·임계처럼 경계가 값으로 정의되는 계산식만.
BANDED = [param for param in MATRIX if param.values[1] != "FORMULA"]


# --- 요구사항 1: 합성 식별자와 합성 증빙만 쓴다 -----------------------------


def test_every_identifier_in_the_testbed_is_synthetic():
    criterion = build_criterion("CREDIT_RATING", "CASE_TABLE")
    fact = build_company_fact("CREDIT_RATING")
    assert criterion.criterion_id.startswith("SYN-")
    assert fact.id.startswith("SYN-")
    assert fact.source == "SYN_TESTBED"
    assert fact.evidence.source_location.startswith("SYN://")
    assert "SYN" in criterion.source_anchor.document_label


def test_the_testbed_never_reaches_a_database_or_a_provider():
    # 하네스가 부르는 것은 순수 계산 함수뿐이다. 세션도 엔진도 추출 제공자도 없다.
    import quantitative_testbed as harness

    source = Path(harness.__file__).read_text(encoding="utf-8")
    for forbidden in ("Session", "create_engine", "database", "httpx", "openai", "requests"):
        assert forbidden not in source, f"테스트베드가 {forbidden} 을(를) 건드립니다"


# --- 요구사항 2: 지표마다 정상값·경계값·누락값 ------------------------------


@pytest.mark.parametrize(("metric", "kind"), MATRIX)
def test_a_nominal_value_is_confirmed_from_its_own_evidence(metric, kind):
    profile = METRIC_PROFILES[metric]
    run = score([build_criterion(metric, kind)], [build_company_fact(metric, profile.nominal)])
    estimate = run.criterion(f"SYN-{metric}")
    assert estimate.status == "CONFIRMED"
    # 확정은 범위가 한 점으로 좁혀졌다는 뜻이다.
    assert estimate.lower_points == estimate.upper_points
    assert run.result.overall_status == "CONFIRMED"


@pytest.mark.parametrize(("metric", "kind"), BANDED)
def test_a_value_exactly_on_the_edge_takes_the_upper_band(metric, kind):
    profile = METRIC_PROFILES[metric]
    run = score([build_criterion(metric, kind)], [build_company_fact(metric, profile.boundary)])
    estimate = run.criterion(f"SYN-{metric}")
    assert estimate.status == "CONFIRMED"
    # 경계에 정확히 걸친 값은 위 구간에 든다. False 는 경계가 아니라 미충족이다.
    assert estimate.lower_points == (0.0 if profile.boundary is False else 10.0)


@pytest.mark.parametrize(
    ("metric", "kind"),
    [param for param in BANDED if METRIC_PROFILES[param.values[0]].below is not None],
)
def test_a_value_just_under_the_edge_drops_a_band(metric, kind):
    profile = METRIC_PROFILES[metric]
    run = score([build_criterion(metric, kind)], [build_company_fact(metric, profile.below)])
    estimate = run.criterion(f"SYN-{metric}")
    assert estimate.status == "CONFIRMED"
    assert estimate.lower_points < 10.0


@pytest.mark.parametrize(("metric", "kind"), MATRIX)
def test_a_missing_value_is_unscorable_rather_than_zero(metric, kind):
    run = score([build_criterion(metric, kind)], [])
    estimate = run.criterion(f"SYN-{metric}")
    assert estimate.status == "UNSCORABLE"
    # 0점도 만점도 아니다. 하한 0, 상한 배점으로 범위가 열려 있어야 한다.
    assert (estimate.lower_points, estimate.upper_points) == (0.0, estimate.max_points)
    assert run.result.overall_status == "UNSCORABLE"


def test_a_continuous_formula_is_clamped_to_the_row_maximum():
    # 산식이 배점을 넘겨도 그 행의 만점 위로는 올라가지 않는다.
    run = score(
        [build_criterion("FINANCIAL_RATIO", "FORMULA")],
        [build_company_fact("FINANCIAL_RATIO", 5_000.0)],
    )
    assert run.criterion("SYN-FINANCIAL_RATIO").lower_points == 10.0


def test_the_registered_unit_is_converted_not_assumed():
    # 같은 canonical 지표라도 원문 단위가 다르면 환산해서 비교한다.
    in_won = score(
        [build_criterion("PERFORMANCE_AMOUNT", "BRACKET")],
        [build_company_fact("PERFORMANCE_AMOUNT", 300_000_000, unit="원")],
    )
    in_eok = score(
        [build_criterion("PERFORMANCE_AMOUNT", "BRACKET")],
        [build_company_fact("PERFORMANCE_AMOUNT", 3, unit="억원")],
    )
    assert in_won.criterion("SYN-PERFORMANCE_AMOUNT").lower_points == 10.0
    assert in_eok.criterion("SYN-PERFORMANCE_AMOUNT").lower_points == 10.0


def test_an_unconvertible_unit_is_refused_instead_of_read_as_a_bare_number():
    run = score(
        [build_criterion("PERFORMANCE_AMOUNT", "BRACKET")],
        [build_company_fact("PERFORMANCE_AMOUNT", 300_000_000, unit="점")],
    )
    assert run.criterion("SYN-PERFORMANCE_AMOUNT").status == "UNSCORABLE"


@pytest.mark.parametrize("rating", [value for band in CREDIT_BANDS for value in band])
def test_every_published_credit_rating_is_scored(rating):
    # 등급 도메인에 구멍이 있으면 그 등급을 가진 회사만 조용히 채점되지 않는다.
    run = score(
        [build_criterion("CREDIT_RATING", "CASE_TABLE")],
        [build_company_fact("CREDIT_RATING", rating)],
    )
    assert run.criterion("SYN-CREDIT_RATING").status == "CONFIRMED"


# --- 요구사항 3: 결속·검증·유효기간 위반 ------------------------------------


UNBOUND_CASES = {
    "평가지표 키 불일치": dict(fact_key="company.other.metric"),
    "결속 해시 불일치": dict(binding=OTHER_BINDING),
    "결속 해시 없음": dict(binding=None),
    "회사 사실 미검증": dict(verified=False),
    "회사 사실 만료": dict(effective_to=AS_OF - timedelta(days=1)),
    "증빙 유효기간 만료": dict(evidence_valid_until=AS_OF - timedelta(days=1)),
    "증빙 미검증": dict(evidence_status="PENDING"),
    "마감일 이후 발급 증빙": dict(evidence_issued_at=AS_OF + timedelta(days=1)),
    "증빙 없음": dict(with_evidence=False),
}


@pytest.mark.parametrize(
    ("reason", "defect"), list(UNBOUND_CASES.items()), ids=list(UNBOUND_CASES)
)
def test_a_defective_fact_never_reaches_the_score(reason, defect):
    run = score(
        [build_criterion("CREDIT_RATING", "CASE_TABLE")],
        [build_company_fact("CREDIT_RATING", **defect)],
    )
    estimate = run.criterion("SYN-CREDIT_RATING")
    assert estimate.status == "UNSCORABLE", reason
    assert (estimate.lower_points, estimate.upper_points) == (0.0, 10.0)
    assert estimate.rationale


@pytest.mark.parametrize(
    ("reason", "defect"), list(UNBOUND_CASES.items()), ids=list(UNBOUND_CASES)
)
def test_a_defect_in_one_row_does_not_sink_a_sound_row(reason, defect):
    run = score(
        [
            build_criterion("CREDIT_RATING", "CASE_TABLE"),
            build_criterion("LOCAL_PRESENCE", "BOOLEAN"),
        ],
        [build_company_fact("CREDIT_RATING", **defect), build_company_fact("LOCAL_PRESENCE")],
    )
    assert run.status_of("CREDIT_RATING") == "UNSCORABLE", reason
    assert run.status_of("LOCAL_PRESENCE") == "CONFIRMED"
    # 총점은 확정되지 않는다. 확정된 행만 보고 총점을 말하면 남은 배점이 사라진다.
    assert run.result.overall_status != "CONFIRMED"


def test_an_evidence_still_valid_at_the_deadline_is_accepted():
    # 마감 당일까지 유효한 증빙을 미리 버리면 될 공고를 버린다.
    run = score(
        [build_criterion("CREDIT_RATING", "CASE_TABLE")],
        [build_company_fact("CREDIT_RATING", evidence_valid_until=AS_OF + timedelta(days=1))],
    )
    assert run.criterion("SYN-CREDIT_RATING").status == "CONFIRMED"


# --- 요구사항 4: 모르면 UNSCORABLE 또는 REVIEW --------------------------------


def test_two_rows_sharing_one_binding_go_to_review_not_to_a_guess():
    # 어느 행에 붙일 사실인지 정해지지 않았다. 아무 쪽에나 붙이면 배점을 지어낸 것이다.
    run = score(
        [
            build_criterion("CREDIT_RATING", "CASE_TABLE", criterion_id="SYN-A"),
            build_criterion("CREDIT_RATING", "CATEGORICAL", criterion_id="SYN-B"),
        ],
        [build_company_fact("CREDIT_RATING")],
    )
    assert [item.status for item in run.result.criteria] == ["REVIEW", "REVIEW"]
    assert run.result.overall_status == "REVIEW"


def test_nothing_unscorable_is_ever_counted_as_confirmed_points():
    run = score(
        [
            build_criterion("CREDIT_RATING", "CASE_TABLE"),
            build_criterion("LOCAL_PRESENCE", "BOOLEAN"),
        ],
        [build_company_fact("LOCAL_PRESENCE")],
    )
    assert run.result.confirmed_points == 10.0
    assert run.result.unscorable_points == 10.0
    assert run.result.total_max_points == 20.0


def test_a_row_outside_the_canonical_registry_is_set_aside_not_scored():
    # 정성평가 행까지 채점하려 들면 회사 자료로 답할 수 없는 배점을 지어내게 된다.
    unknown = MetricProfile(
        "SYN_QUALITATIVE",
        "company.credit_rating",
        "등급",
        "CATEGORICAL",
        nominal="AA0",
        boundary="A-",
        formulas=("CASE_TABLE",),
    )
    qualitative = build_criterion(unknown, "CASE_TABLE", criterion_id="SYN-Q").model_copy(
        update={"label": "SYN 사업 이해도 및 수행 계획"}
    )
    run = score([qualitative], [build_company_fact(unknown)])
    assert run.criterion("SYN-Q").status == "OUT_OF_SCOPE"
    assert run.result.out_of_scope_points == 10.0
    # 정량 총점에서 빠진다. 정성 배점을 0점으로도 만점으로도 세지 않는다.
    assert run.result.total_max_points == 0.0


def test_a_qualitative_wording_never_sets_aside_a_canonical_row():
    # 등록된 지표를 가진 행은 문구가 정성적이어도 채점한다. 이 보호가 없으면
    # 배점표 문구 하나로 계산 가능한 행이 조용히 사라진다.
    scorable = build_criterion("CREDIT_RATING", "CASE_TABLE").model_copy(
        update={"label": "SYN 재무 건전성 및 수행 능력"}
    )
    run = score([scorable], [build_company_fact("CREDIT_RATING")])
    assert run.criterion("SYN-CREDIT_RATING").status == "CONFIRMED"


# --- 요구사항 5: 부분 원문이면 총점은 검토 필요 -------------------------------


def test_partial_source_keeps_the_total_under_review_though_rows_are_confirmed():
    criteria = [
        build_criterion("CREDIT_RATING", "CASE_TABLE"),
        build_criterion("LOCAL_PRESENCE", "BOOLEAN"),
    ]
    facts = [build_company_fact("CREDIT_RATING"), build_company_fact("LOCAL_PRESENCE")]

    full = score(criteria, facts)
    partial = score(criteria, facts, activation="PARTIAL_SOURCE")

    # 개별 행은 두 경우 모두 똑같이 확정된다. 확보한 원문에서 계산한 값이니까.
    assert [item.status for item in partial.result.criteria] == ["CONFIRMED", "CONFIRMED"]
    assert partial.result.confirmed_points == full.result.confirmed_points == 20.0
    # 그러나 배점표 일부를 아직 못 봤으므로 총점은 확정되지 않는다.
    assert full.result.overall_status == "CONFIRMED"
    assert partial.result.overall_status == "REVIEW"


def test_partial_source_never_claims_a_minimum_is_met():
    run = score(
        [build_criterion("CREDIT_RATING", "CASE_TABLE")],
        [build_company_fact("CREDIT_RATING")],
        activation="PARTIAL_SOURCE",
    )
    assert run.result.minimum_score is None
    assert run.result.meets_minimum is None


# --- 요구사항 6: 지표를 늘려도 채점기를 복제하지 않는다 -----------------------


def test_the_testbed_covers_every_canonical_metric():
    # 저장소가 지표를 늘리면 이 테스트가 먼저 깨진다. 조용히 빠진 지표가 없도록.
    assert set(METRIC_PROFILES) == set(_CANONICAL_METRIC_REGISTRY)
    for metric, profile in METRIC_PROFILES.items():
        assert profile.fact_key == _CANONICAL_METRIC_REGISTRY[metric]["fact_key"]
        assert profile.formulas, f"{metric} 에 계산식이 등록되지 않았습니다"


def test_a_new_registration_row_is_scored_without_touching_the_harness():
    # 채점기도 하네스도 건드리지 않는다. 등록 정보 한 줄이면 사슬 전체가 돈다.
    row = MetricProfile(
        "PERFORMANCE_AMOUNT",
        "company.performance.amount",
        "억원",
        "NUMERIC",
        nominal=3,
        boundary=2,
        below=1,
        threshold=200_000_000,
        formulas=("BRACKET",),
    )
    top = score([build_criterion(row, "BRACKET", criterion_id="SYN-EOK")], [build_company_fact(row)])
    low = score(
        [build_criterion(row, "BRACKET", criterion_id="SYN-EOK")],
        [build_company_fact(row, row.below)],
    )
    assert top.criterion("SYN-EOK").lower_points == 10.0
    assert low.criterion("SYN-EOK").lower_points == 0.0


def test_every_formula_type_in_the_repository_is_exercised():
    exercised = {kind for profile in METRIC_PROFILES.values() for kind in profile.formulas}
    assert exercised == {
        "BRACKET",
        "THRESHOLD",
        "FORMULA",
        "CATEGORICAL",
        "CASE_TABLE",
        "BOOLEAN",
    }


# --- 요구사항 8: 기존 계약을 약화하지 않는다 ----------------------------------


def test_the_evidence_contract_is_the_production_one():
    # 하네스가 증빙 요건을 느슨하게 흉내 냈다면, 운영에서 거절될 사실이 여기서는
    # 통과해 테스트가 거짓 안심을 준다.
    evidence = build_company_fact("CREDIT_RATING").evidence
    assert evidence.evidence_type == "QUANTITATIVE_FACT"
    assert evidence.status == "VERIFIED"
    assert len(evidence.sha256) == 64
    assert evidence.source_location
    assert set(evidence.metadata_json) >= {
        "quantitative_fact_key",
        "fact_binding_sha256",
        "company_fact_payload_sha256",
    }


def test_scoring_is_deterministic_for_the_same_input():
    criteria = [
        build_criterion(metric, profile.formulas[0])
        for metric, profile in METRIC_PROFILES.items()
    ]
    facts = [build_company_fact(metric) for metric in METRIC_PROFILES]
    first = score(criteria, facts).result.model_dump()
    second = score(criteria, facts).result.model_dump()
    assert first == second
