from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from conftest import login_department_reader

from pai_loop.main import create_app
from pai_loop.models import AnalysisRun, Notice, NoticeVersion, ScoreSnapshot
from pai_loop.quantitative_scoring import (
    QUANTITATIVE_ENGINE_VERSION,
    QuantitativeEstimateResult,
    public_quantitative_snapshot_projection,
)


PRIVATE_BASIS_MARKER = "SYN-PRIVATE-BASIS-MARKER"
PRIVATE_RULESET = "SYN-PRIVATE-DYNAMIC-RULESET"
PRIVATE_INPUT_SHA256 = "a" * 64
PRIVATE_OUTPUT_SHA256 = "b" * 64
_MISSING = object()


def _public_app(monkeypatch):
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "server-only-secret")
    monkeypatch.setenv("PAI_LOOP_PUBLIC_READ_ONLY", "true")
    return create_app(database_url="sqlite:///:memory:", seed_synthetic=False)


def _notice(*, notice_key: str) -> Notice:
    return Notice(
        notice_key=notice_key,
        bid_notice_no=notice_key,
        revision_no="00",
        title="SYN public quantitative snapshot contract",
        agency="SYN public agency",
        deadline=datetime(2030, 1, 31, tzinfo=timezone.utc),
        status="OPEN",
    )


def _version(
    notice: Notice,
    *,
    version_no: int,
    kind: str,
    digest_character: str,
) -> NoticeVersion:
    version = NoticeVersion(
        notice=notice,
        version_no=version_no,
        file_sha256=digest_character * 64,
        document_complete=True,
        extraction_status="ACCEPTED",
        extraction_confidence=1,
        source_payload={"kind": kind},
    )
    return version


def _run(
    notice: Notice,
    basis: NoticeVersion,
    *,
    label: str,
    generated_at: datetime,
    score: ScoreSnapshot,
) -> AnalysisRun:
    input_sha256 = ("c" if label == "old" else "d") * 64
    score.basis_json = {**score.basis_json, "input_sha256": input_sha256}
    return AnalysisRun(
        notice=notice,
        notice_version=basis,
        status="COMPLETED",
        idempotency_key=f"SYN-public-snapshot-{label}",
        input_sha256=input_sha256,
        basis_versions={"quantitative_engine": QUANTITATIVE_ENGINE_VERSION},
        generated_at=generated_at,
        scores=[score],
    )


def _quantitative_snapshot(
    *,
    value: float | None,
    lower: float,
    upper: float,
    status: str,
    band: str,
    confirmed: float,
    coverage: float,
    public_criteria: object = _MISSING,
) -> ScoreSnapshot:
    basis_json = {
        "input_sha256": PRIVATE_INPUT_SHA256,
        "profile_output_sha256": PRIVATE_OUTPUT_SHA256,
        "ruleset_version": PRIVATE_RULESET,
        "private_note": PRIVATE_BASIS_MARKER,
        "rule_source_status": "AVAILABLE",
        "source_validation_status": "SOURCE_VALIDATED",
        "activation_status": "AUTO_ACTIVE",
        "activation_reasons": [],
        "total_max_points": 20,
        "confirmed_points": confirmed,
        "evidence_coverage_pct": coverage,
    }
    if public_criteria is not _MISSING:
        basis_json["public_criteria"] = public_criteria
    return ScoreSnapshot(
        score_key="quantitative.total",
        score_type="QUANTITATIVE_ESTIMATE",
        value=value,
        lower_value=lower,
        upper_value=upper,
        unit="POINTS",
        status=status,
        band=band,
        confidence=0.75,
        method_version=QUANTITATIVE_ENGINE_VERSION,
        basis_json=basis_json,
    )


def _public_criteria_snapshot() -> dict[str, object]:
    return {
        "schema_version": "public-quantitative-criteria-1.0.0",
        "items": [
            {
                "display_code": "PERFORMANCE_AMOUNT",
                "max_points": 4,
                "estimated_points": 4,
                "lower_points": 4,
                "upper_points": 4,
                "status": "CONFIRMED",
            },
            {
                "display_code": "PERFORMANCE_COUNT",
                "max_points": 6,
                "estimated_points": 6,
                "lower_points": 6,
                "upper_points": 6,
                "status": "CONFIRMED",
            },
            {
                "display_code": "CREDIT_RATING",
                "max_points": 10,
                "estimated_points": 10,
                "lower_points": 10,
                "upper_points": 10,
                "status": "CONFIRMED",
            },
        ],
    }


def _mixed_public_snapshot(*, quantitative_points: int = 40) -> ScoreSnapshot:
    """SYN quantitative 40 + separate 60; no qualitative award is assumed."""
    items = []
    if quantitative_points:
        items.append({
            "display_code": "FINANCIAL_RATIO", "max_points": quantitative_points,
            "estimated_points": quantitative_points, "lower_points": quantitative_points,
            "upper_points": quantitative_points, "status": "CONFIRMED",
        })
    items.append({
        "display_code": "OTHER", "max_points": 60,
        "estimated_points": None, "lower_points": 0, "upper_points": 60,
        "status": "OUT_OF_SCOPE",
    })
    snapshot = _quantitative_snapshot(
        value=quantitative_points or None,
        lower=quantitative_points, upper=quantitative_points,
        status="CONFIRMED" if quantitative_points else "UNSCORABLE",
        band="GREEN" if quantitative_points else "GRAY",
        confirmed=quantitative_points, coverage=100 if quantitative_points else 0,
        public_criteria={"schema_version": "public-quantitative-criteria-1.0.0", "items": items},
    )
    snapshot.basis_json["total_max_points"] = quantitative_points
    snapshot.basis_json["out_of_scope_points"] = 60
    if not quantitative_points:
        snapshot.confidence = 0
    return snapshot


@pytest.mark.parametrize("quantitative_points", [40, 0])
@pytest.mark.parametrize("explicit_excluded_total", [True, False])
def test_public_endpoint_restores_mixed_scope_rows_and_separate_weight(
    monkeypatch, quantitative_points, explicit_excluded_total,
) -> None:
    app = _public_app(monkeypatch)

    def unexpected_reestimate(*_args, **_kwargs):
        raise AssertionError("valid mixed-scope persisted rows must restore without recalculation")

    monkeypatch.setattr("pai_loop.quantitative_scoring.estimate_for_notice", unexpected_reestimate)
    score = _mixed_public_snapshot(quantitative_points=quantitative_points)
    if not explicit_excluded_total:
        del score.basis_json["out_of_scope_points"]
    with TestClient(app) as client:
        login_department_reader(client)
        with app.state.session_factory() as session:
            notice = _notice(notice_key="SYN-PUBLIC-MIXED-SCOPE")
            _version(notice, version_no=1, kind="PPS_NOTICE_METADATA", digest_character="8")
            basis = _version(notice, version_no=2, kind="MATERIALIZED_ANALYSIS", digest_character="9")
            session.add(notice)
            session.flush()
            session.add(_run(
                notice, basis, label="latest",
                generated_at=datetime(2026, 9, 1, tzinfo=timezone.utc), score=score,
            ))
            session.commit()
        response = client.get("/api/v1/notices/SYN-PUBLIC-MIXED-SCOPE/quantitative-estimate")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total_max_points"] == quantitative_points
    assert payload["lower_points"] == payload["upper_points"] == quantitative_points
    assert payload["confirmed_points"] == quantitative_points
    assert payload["estimated_points"] == (quantitative_points or None)
    assert payload["out_of_scope_points"] == 60
    assert payload["overall_status"] == ("CONFIRMED" if quantitative_points else "UNSCORABLE")
    assert payload["readiness_pct"] == (100 if quantitative_points else None)
    assert payload["minimum_score"] is payload["meets_minimum"] is None
    excluded = payload["criteria"][-1]
    assert excluded["status"] == "OUT_OF_SCOPE"
    assert excluded["max_points"] == excluded["upper_points"] == 60
    assert excluded["lower_points"] == 0 and excluded["estimated_points"] is None
    assert excluded["category"] == "PUBLIC_OUT_OF_SCOPE"
    assert "정량 합계에서 제외" in excluded["rationale"]
    assert excluded["source_anchor"] is excluded["fact_binding_sha256"] is None
    for private in (PRIVATE_BASIS_MARKER, PRIVATE_RULESET, PRIVATE_INPUT_SHA256, PRIVATE_OUTPUT_SHA256):
        assert private not in response.text


@pytest.mark.parametrize("tamper", [
    "wrong-excluded-total", "null-excluded-total", "bool-excluded-total",
    "text-excluded-total", "noncanonical-excluded-total", "unproven-excluded-total",
    "old-combined-total", "excluded-lower-award", "excluded-upper-award",
    "excluded-exact-award", "zero-without-rows", "zero-confirmed",
])
def test_mixed_public_snapshot_rejects_fabricated_scores_and_mismatched_scope(tamper) -> None:
    score = _mixed_public_snapshot(quantitative_points=0 if tamper.startswith("zero-") else 40)
    basis = score.basis_json
    excluded = basis["public_criteria"]["items"][-1]
    if tamper == "wrong-excluded-total":
        basis["out_of_scope_points"] = 59
    elif tamper == "null-excluded-total":
        basis["out_of_scope_points"] = None
    elif tamper == "bool-excluded-total":
        basis["out_of_scope_points"] = True
    elif tamper == "text-excluded-total":
        basis["out_of_scope_points"] = "60"
    elif tamper == "noncanonical-excluded-total":
        basis["out_of_scope_points"] = 60.001
    elif tamper == "unproven-excluded-total":
        del basis["public_criteria"]
    elif tamper == "old-combined-total":
        basis["total_max_points"] = 100
        score.upper_value = 100
        score.value = None
        score.band = "RED"
    elif tamper == "excluded-lower-award":
        excluded["lower_points"] = 1
    elif tamper == "excluded-upper-award":
        excluded["upper_points"] = 59
    elif tamper == "excluded-exact-award":
        excluded["estimated_points"] = 0
    elif tamper == "zero-without-rows":
        del basis["public_criteria"]
        basis["out_of_scope_points"] = 0
    elif tamper == "zero-confirmed":
        score.status = "CONFIRMED"
        score.value = 0
    run = AnalysisRun(
        input_sha256=PRIVATE_INPUT_SHA256,
        basis_versions={"quantitative_engine": QUANTITATIVE_ENGINE_VERSION},
    )
    assert public_quantitative_snapshot_projection(run, score) is None


def _malformed_public_criteria_snapshots() -> list[object]:
    cases: list[object] = []
    cases.append({"schema_version": "public-quantitative-criteria-1.0.0"})
    cases.append(
        {
            "schema_version": "public-quantitative-criteria-1.0.0",
            "items": None,
        }
    )
    missing_estimate = _public_criteria_snapshot()
    del missing_estimate["items"][0]["estimated_points"]
    cases.append(missing_estimate)
    extra_private_field = _public_criteria_snapshot()
    extra_private_field["items"][0]["source_quote"] = PRIVATE_BASIS_MARKER
    cases.append(extra_private_field)
    unknown_display_code = _public_criteria_snapshot()
    unknown_display_code["items"][0]["display_code"] = "PROVIDER-INTERNAL-ID"
    cases.append(unknown_display_code)
    boolean_points = _public_criteria_snapshot()
    boolean_points["items"][0]["max_points"] = True
    cases.append(boolean_points)
    mismatched_total = _public_criteria_snapshot()
    mismatched_total["items"][0]["max_points"] = 5
    cases.append(mismatched_total)
    inconsistent_status = _public_criteria_snapshot()
    inconsistent_status["items"][0]["status"] = "REVIEW"
    cases.append(inconsistent_status)
    huge_number = _public_criteria_snapshot()
    huge_number["items"][0]["max_points"] = 10**400
    cases.append(huge_number)
    too_many = _public_criteria_snapshot()
    too_many["items"] = [copy.deepcopy(too_many["items"][0]) for _ in range(201)]
    cases.append(too_many)
    return [
        pytest.param(value, id=case_id)
        for value, case_id in zip(
            cases,
            (
                "missing-items",
                "null-items",
                "missing-required-estimate",
                "extra-private-field",
                "unknown-display-code",
                "boolean-points",
                "aggregate-mismatch",
                "status-mismatch",
                "huge-number",
                "too-many-items",
            ),
            strict=True,
        )
    ]


@pytest.mark.parametrize(
    (
        "value",
        "lower",
        "upper",
        "status",
        "band",
        "confirmed",
        "coverage",
        "readiness",
    ),
    [
        (None, 9.7, 20.0, "REVIEW", "RED", 0.0, 0.0, 48.5),
        (17.0, 17.0, 17.0, "CONFIRMED", "GREEN", 17.0, 100.0, 85.0),
    ],
)
def test_public_endpoint_returns_latest_current_sanitised_aggregate_without_reestimate(
    monkeypatch,
    value: float | None,
    lower: float,
    upper: float,
    status: str,
    band: str,
    confirmed: float,
    coverage: float,
    readiness: float,
) -> None:
    app = _public_app(monkeypatch)
    now = datetime(2026, 8, 31, 6, 0, tzinfo=timezone.utc)

    def unexpected_reestimate(*_args, **_kwargs):
        raise AssertionError("a current public snapshot must not be re-estimated")

    monkeypatch.setattr(
        "pai_loop.quantitative_scoring.estimate_for_notice",
        unexpected_reestimate,
    )

    with TestClient(app) as public_client:
        login_department_reader(public_client)
        with app.state.session_factory() as session:
            notice = _notice(notice_key="SYN-PUBLIC-QUANT-CURRENT")
            _version(
                notice,
                version_no=1,
                kind="PPS_NOTICE_METADATA",
                digest_character="1",
            )
            basis = _version(
                notice,
                version_no=2,
                kind="MATERIALIZED_ANALYSIS",
                digest_character="2",
            )
            session.add(notice)
            session.flush()
            session.add_all(
                [
                    _run(
                        notice,
                        basis,
                        label="old",
                        generated_at=now - timedelta(hours=1),
                        score=_quantitative_snapshot(
                            value=4,
                            lower=4,
                            upper=10,
                            status="ESTIMATED",
                            band="RED",
                            confirmed=0,
                            coverage=0,
                        ),
                    ),
                    _run(
                        notice,
                        basis,
                        label="latest",
                        generated_at=now,
                        score=_quantitative_snapshot(
                            value=value,
                            lower=lower,
                            upper=upper,
                            status=status,
                            band=band,
                            confirmed=confirmed,
                            coverage=coverage,
                        ),
                    ),
                ]
            )
            session.commit()

        response = public_client.get(
            "/api/v1/notices/SYN-PUBLIC-QUANT-CURRENT/quantitative-estimate"
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total_max_points"] == 20
    assert payload["confirmed_points"] == confirmed
    assert payload["estimated_points"] == value
    assert payload["lower_points"] == lower
    assert payload["upper_points"] == upper
    assert payload["evidence_coverage_pct"] == coverage
    assert payload["readiness_pct"] == readiness
    assert payload["overall_status"] == status
    assert payload["readiness_band"] == band
    assert payload["rule_source_status"] == "AVAILABLE"
    assert payload["source_validation_status"] == "SOURCE_VALIDATED"
    assert payload["activation_status"] == "AUTO_ACTIVE"
    assert payload["activation_reasons"] == []
    assert payload["source_anchor"] is None
    assert payload["criteria"] == []
    assert payload["evidence_observations"] == []

    for private_value in (
        PRIVATE_BASIS_MARKER,
        PRIVATE_RULESET,
        PRIVATE_INPUT_SHA256,
        PRIVATE_OUTPUT_SHA256,
        "private_note",
    ):
        assert private_value not in response.text


def test_public_endpoint_restores_safe_item_rows_from_current_snapshot(
    monkeypatch,
) -> None:
    app = _public_app(monkeypatch)

    def unexpected_reestimate(*_args, **_kwargs):
        raise AssertionError("a valid public item snapshot must not be re-estimated")

    monkeypatch.setattr(
        "pai_loop.quantitative_scoring.estimate_for_notice",
        unexpected_reestimate,
    )

    with TestClient(app) as public_client:
        login_department_reader(public_client)
        with app.state.session_factory() as session:
            notice = _notice(notice_key="SYN-PUBLIC-QUANT-ITEMS")
            _version(
                notice,
                version_no=1,
                kind="PPS_NOTICE_METADATA",
                digest_character="6",
            )
            basis = _version(
                notice,
                version_no=2,
                kind="MATERIALIZED_ANALYSIS",
                digest_character="7",
            )
            session.add(notice)
            session.flush()
            session.add(
                _run(
                    notice,
                    basis,
                    label="latest",
                    generated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                    score=_quantitative_snapshot(
                        value=20,
                        lower=20,
                        upper=20,
                        status="CONFIRMED",
                        band="GREEN",
                        confirmed=20,
                        coverage=100,
                        public_criteria=_public_criteria_snapshot(),
                    ),
                )
            )
            session.commit()

        response = public_client.get(
            "/api/v1/notices/SYN-PUBLIC-QUANT-ITEMS/quantitative-estimate"
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["criterion_id"] for item in payload["criteria"]] == [
        "PUBLIC-CRITERION-001",
        "PUBLIC-CRITERION-002",
        "PUBLIC-CRITERION-003",
    ]
    assert [item["label"] for item in payload["criteria"]] == [
        "용역수행 실적(금액)",
        "용역수행 실적(건수)",
        "제안업체 경영상태",
    ]
    assert [item["max_points"] for item in payload["criteria"]] == [4, 6, 10]
    assert [item["estimated_points"] for item in payload["criteria"]] == [
        4,
        6,
        10,
    ]
    assert {item["status"] for item in payload["criteria"]} == {"CONFIRMED"}
    for item in payload["criteria"]:
        assert item["category"] == "PUBLIC_QUANTITATIVE"
        assert item["formula"] == "공개 화면에서는 세부 원문 산식을 제외합니다."
        assert item["rule_floor_points"] == 0
        assert item["floor_condition"] is None
        assert item["rule_base_points"] is None
        assert item["base_condition"] is None
        assert item["source_anchor"] is None
        assert item["evidence_key"] is None
        assert item["evidence_reference"] is None
        assert item["evidence_sha256"] is None
        assert item["fact_binding_sha256"] is None
        assert item["assumptions"] == []

    for private_value in (
        PRIVATE_BASIS_MARKER,
        PRIVATE_RULESET,
        PRIVATE_INPUT_SHA256,
        PRIVATE_OUTPUT_SHA256,
        "private_note",
        "PERFORMANCE_AMOUNT",
        "PERFORMANCE_COUNT",
        "CREDIT_RATING",
    ):
        assert private_value not in response.text


def _fallback_result() -> QuantitativeEstimateResult:
    return QuantitativeEstimateResult(
        engine_version=QUANTITATIVE_ENGINE_VERSION,
        ruleset_version="SYN-FALLBACK-PRIVATE-RULESET",
        source_anchor=None,
        rule_source_status="AVAILABLE",
        source_validation_status="SOURCE_VALIDATED",
        activation_status="AUTO_ACTIVE",
        activation_reasons=[],
        overall_status="ESTIMATED",
        total_max_points=10,
        confirmed_points=0,
        estimated_points=None,
        lower_points=2,
        upper_points=8,
        unscorable_points=8,
        evidence_coverage_pct=0,
        readiness_pct=20,
        readiness_band="RED",
        minimum_score=None,
        meets_minimum=None,
        confidence=0.4,
        criteria=[],
        assumptions=["SYN fallback calculation"],
        evidence_observations=[],
        opinion="SYN fallback result",
        separation_notice="SYN scoring remains separate from eligibility.",
    )


@pytest.mark.parametrize(
    "malformed_public_criteria",
    _malformed_public_criteria_snapshots(),
)
def test_malformed_latest_public_item_snapshot_falls_back_without_using_older_run(
    monkeypatch,
    malformed_public_criteria: object,
) -> None:
    app = _public_app(monkeypatch)
    observed_fact_counts: list[int] = []

    def fallback_estimate(_notice, company_facts, *_args):
        observed_fact_counts.append(len(tuple(company_facts)))
        return _fallback_result()

    monkeypatch.setattr(
        "pai_loop.quantitative_scoring.estimate_for_notice",
        fallback_estimate,
    )
    now = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)

    with TestClient(app) as public_client:
        login_department_reader(public_client)
        with app.state.session_factory() as session:
            notice = _notice(notice_key="SYN-PUBLIC-QUANT-MALFORMED")
            _version(
                notice,
                version_no=1,
                kind="PPS_NOTICE_METADATA",
                digest_character="8",
            )
            basis = _version(
                notice,
                version_no=2,
                kind="MATERIALIZED_ANALYSIS",
                digest_character="9",
            )
            session.add(notice)
            session.flush()
            session.add_all(
                [
                    _run(
                        notice,
                        basis,
                        label="old",
                        generated_at=now - timedelta(hours=1),
                        score=_quantitative_snapshot(
                            value=20,
                            lower=20,
                            upper=20,
                            status="CONFIRMED",
                            band="GREEN",
                            confirmed=20,
                            coverage=100,
                            public_criteria=_public_criteria_snapshot(),
                        ),
                    ),
                    _run(
                        notice,
                        basis,
                        label="latest",
                        generated_at=now,
                        score=_quantitative_snapshot(
                            value=20,
                            lower=20,
                            upper=20,
                            status="CONFIRMED",
                            band="GREEN",
                            confirmed=20,
                            coverage=100,
                            public_criteria=malformed_public_criteria,
                        ),
                    ),
                ]
            )
            session.commit()

        response = public_client.get(
            "/api/v1/notices/SYN-PUBLIC-QUANT-MALFORMED/quantitative-estimate"
        )

    assert response.status_code == 200, response.text
    assert observed_fact_counts == [0]
    payload = response.json()
    assert payload["total_max_points"] == 10
    assert payload["criteria"] == []
    assert PRIVATE_BASIS_MARKER not in response.text
    assert "PROVIDER-INTERNAL-ID" not in response.text


@pytest.mark.parametrize(
    "invalid_state",
    (
        "huge-total",
        "confirmed-without-score",
        "inactive-auto-active",
        "active-review-required",
        "noncanonical-points",
    ),
)
def test_invalid_legacy_aggregate_snapshot_falls_back_fail_closed(
    monkeypatch,
    invalid_state: str,
) -> None:
    app = _public_app(monkeypatch)
    observed_fact_counts: list[int] = []

    def fallback_estimate(_notice, company_facts, *_args):
        observed_fact_counts.append(len(tuple(company_facts)))
        return _fallback_result()

    monkeypatch.setattr(
        "pai_loop.quantitative_scoring.estimate_for_notice",
        fallback_estimate,
    )
    score = _quantitative_snapshot(
        value=None,
        lower=9.7,
        upper=20,
        status="REVIEW",
        band="RED",
        confirmed=0,
        coverage=0,
    )
    if invalid_state == "huge-total":
        score.basis_json["total_max_points"] = 10**400
    elif invalid_state == "confirmed-without-score":
        score.value = None
        score.lower_value = None
        score.upper_value = None
        score.status = "CONFIRMED"
        score.band = "GRAY"
        score.basis_json["total_max_points"] = None
        score.basis_json["confirmed_points"] = None
    elif invalid_state == "inactive-auto-active":
        score.value = None
        score.lower_value = None
        score.upper_value = None
        score.band = "GRAY"
        score.confidence = 0
        score.basis_json["total_max_points"] = None
        score.basis_json["confirmed_points"] = None
    elif invalid_state == "active-review-required":
        score.basis_json["activation_status"] = "REVIEW_REQUIRED"
    elif invalid_state == "noncanonical-points":
        score.lower_value = 9.701

    with TestClient(app) as public_client:
        login_department_reader(public_client)
        with app.state.session_factory() as session:
            notice = _notice(notice_key=f"SYN-LEGACY-{invalid_state}")
            _version(
                notice,
                version_no=1,
                kind="PPS_NOTICE_METADATA",
                digest_character="a",
            )
            basis = _version(
                notice,
                version_no=2,
                kind="MATERIALIZED_ANALYSIS",
                digest_character="b",
            )
            session.add(notice)
            session.flush()
            session.add(
                _run(
                    notice,
                    basis,
                    label="latest",
                    generated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                    score=score,
                )
            )
            session.commit()

        response = public_client.get(
            f"/api/v1/notices/SYN-LEGACY-{invalid_state}/quantitative-estimate"
        )

    assert response.status_code == 200, response.text
    assert observed_fact_counts == [0]
    assert response.json()["total_max_points"] == 10


def test_public_endpoint_falls_back_when_snapshot_predates_newer_pps_metadata(
    monkeypatch,
) -> None:
    app = _public_app(monkeypatch)
    observed_fact_counts: list[int] = []

    def fallback_estimate(_notice, company_facts, *_args):
        observed_fact_counts.append(len(tuple(company_facts)))
        return _fallback_result()

    monkeypatch.setattr(
        "pai_loop.quantitative_scoring.estimate_for_notice",
        fallback_estimate,
    )

    with TestClient(app) as public_client:
        login_department_reader(public_client)
        with app.state.session_factory() as session:
            notice = _notice(notice_key="SYN-PUBLIC-QUANT-STALE")
            _version(
                notice,
                version_no=1,
                kind="PPS_NOTICE_METADATA",
                digest_character="3",
            )
            stale_basis = _version(
                notice,
                version_no=2,
                kind="MATERIALIZED_ANALYSIS",
                digest_character="4",
            )
            session.add(notice)
            session.flush()
            session.add(
                _run(
                    notice,
                    stale_basis,
                    label="latest",
                    generated_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
                    score=_quantitative_snapshot(
                        value=19,
                        lower=19,
                        upper=19,
                        status="CONFIRMED",
                        band="GREEN",
                        confirmed=19,
                        coverage=100,
                    ),
                )
            )
            session.add(
                _version(
                    notice,
                    version_no=3,
                    kind="PPS_NOTICE_METADATA",
                    digest_character="5",
                )
            )
            session.commit()

        response = public_client.get(
            "/api/v1/notices/SYN-PUBLIC-QUANT-STALE/quantitative-estimate"
        )

    assert response.status_code == 200, response.text
    assert observed_fact_counts == [0]
    payload = response.json()
    assert payload["total_max_points"] == 10
    assert payload["estimated_points"] is None
    assert payload["lower_points"] == 2
    assert payload["upper_points"] == 8
    assert payload["overall_status"] == "ESTIMATED"
    assert payload["ruleset_version"] == "public-quantitative-summary-v1"
    assert PRIVATE_BASIS_MARKER not in response.text
    assert PRIVATE_RULESET not in response.text
    assert "SYN-FALLBACK-PRIVATE-RULESET" not in response.text



def test_dashboard_score_counts_use_latest_snapshot_and_exclude_ended_notices(monkeypatch) -> None:
    app = _public_app(monkeypatch)
    now = datetime.now(timezone.utc)
    with TestClient(app) as client:
        login_department_reader(client)
        with app.state.session_factory() as session:
            notice = _notice(notice_key="PPS-TEST-PROGRESS-SNAPSHOT")
            basis = _version(notice, version_no=1, kind="TEST_SOURCE", digest_character="e")
            run = _run(notice, basis, label="progress", generated_at=now,
                       score=_quantitative_snapshot(value=None, lower=10, upper=20,
                           status="UNSCORABLE", band="RED", confirmed=0, coverage=0))
            session.add(run)
            session.commit()
            notice_id, version_id = notice.id, basis.id
        response = client.get("/api/v1/dashboard")
        assert response.status_code == 200
        stats = response.json()["analysis_statistics"]
        assert stats["notice_count"] == 1
        assert stats["score_range_notice_count"] == 1
        assert stats["score_counts"]["UNSCORABLE"] == 1
        assert stats["eligibility_counts"]["NOT_EVALUATED"] == 1
        for secret in (PRIVATE_BASIS_MARKER, PRIVATE_INPUT_SHA256, PRIVATE_OUTPUT_SHA256, PRIVATE_RULESET):
            assert secret not in response.text
        with app.state.session_factory() as session:
            session.add(AnalysisRun(notice_id=notice_id, notice_version_id=version_id,
                status="FAILED", idempotency_key="SYN-progress-superseding-failure",
                input_sha256="f" * 64, generated_at=now + timedelta(seconds=1), output_summary={}))
            session.commit()
        stats = client.get("/api/v1/dashboard").json()["analysis_statistics"]
        assert stats["score_range_notice_count"] == 0
        assert stats["score_counts"]["NOT_EVALUATED"] == 1
        with app.state.session_factory() as session:
            notice = session.get(Notice, notice_id)
            notice.status = "CLOSED"
            session.commit()
        stats = client.get("/api/v1/dashboard").json()["analysis_statistics"]
        assert stats["notice_count"] == 0
        assert sum(stats["score_counts"].values()) == 0
