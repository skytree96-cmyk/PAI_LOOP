from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pai_loop.main import create_app
from pai_loop.models import AnalysisRun, Notice, NoticeVersion, ScoreSnapshot
from pai_loop.quantitative_scoring import (
    QUANTITATIVE_ENGINE_VERSION,
    QuantitativeEstimateResult,
)


PRIVATE_BASIS_MARKER = "SYN-PRIVATE-BASIS-MARKER"
PRIVATE_RULESET = "SYN-PRIVATE-DYNAMIC-RULESET"
PRIVATE_INPUT_SHA256 = "a" * 64
PRIVATE_OUTPUT_SHA256 = "b" * 64


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
) -> ScoreSnapshot:
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
        basis_json={
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
        },
    )


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

