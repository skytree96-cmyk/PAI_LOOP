from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from pai_loop.models import CompanyFact, Evidence, Notice
from pai_loop.private_company_evidence import (
    PrivateCreditRatingRegistration,
    _credit_rating_binding_for_notice,
)
from pai_loop.quantitative_formula import CategoryScore
from pai_loop.quantitative_scoring import (
    QuantitativeCriterion,
    resolve_verified_quantitative_facts,
)


NOTICE_KEY = "SYN-PRIVATE-CREDIT-001"
BINDING = "b" * 64
DOCUMENT_SHA256 = "a" * 64
PRIVATE_REFERENCE = "private-evidence://synthetic/credit/SYN-DOCUMENT-001"
PRIVATE_TOKEN = "synthetic-private-evidence-token-00000001"


def _seed_notice(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        session.add(
            Notice(
                notice_key=NOTICE_KEY,
                bid_notice_no="SYN-CREDIT-001",
                title="SYN 신용평가 정량점수 공고",
                agency="SYN 발주기관",
                published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                deadline=datetime(2026, 8, 30, 9, tzinfo=timezone.utc),
                status="OPEN",
            )
        )
        session.commit()


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "rating": "A0",
        "document_sha256": DOCUMENT_SHA256,
        "evidence_reference": PRIVATE_REFERENCE,
        "issued_on": "2026-08-01",
        "effective_on": "2026-08-01",
        "valid_until": "2026-08-31",
        "verification_attestation": "HUMAN_REVIEWED_COMPLETE_DOCUMENT",
    }
    payload.update(overrides)
    return payload


def _operator_headers(token: str = PRIVATE_TOKEN) -> dict[str, str]:
    return {"X-PAI-Private-Evidence-Token": token}


def _enable_production_operator(client: TestClient) -> None:
    client.app.state.settings = replace(
        client.app.state.settings,
        environment="production",
        public_read_only=True,
        public_manual_analysis_enabled=True,
        public_manual_analysis_token="2468",
        api_key="synthetic-server-only-key",
        private_evidence_token=PRIVATE_TOKEN,
    )


def test_private_credit_registration_is_authenticated_idempotent_and_scoreable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_notice(client)
    _enable_production_operator(client)
    monkeypatch.setattr(
        "pai_loop.private_company_evidence._credit_rating_binding_for_notice",
        lambda _notice: BINDING,
    )
    endpoint = (
        f"https://testserver/api/v1/operator-evidence/notices/{NOTICE_KEY}/credit-rating"
    )

    assert client.post(endpoint, headers={"Origin": "https://testserver"}, json=_payload()).status_code == 401
    assert client.post(
        endpoint,
        headers={
            "X-PAI-Manual-Token": "2468",
        },
        json=_payload(),
    ).status_code == 401

    created = client.post(
        endpoint,
        headers=_operator_headers(),
        json=_payload(rating="A"),
    )
    assert created.status_code == 200, created.text
    assert created.headers["cache-control"] == "no-store"
    assert created.json() == {
        "notice_key": NOTICE_KEY,
        "fact_key": "company.credit_rating",
        "rating": "A0",
        "binding_status": "CREATED",
        "valid_at_deadline": True,
    }
    assert DOCUMENT_SHA256 not in created.text
    assert PRIVATE_REFERENCE not in created.text

    unchanged = client.post(
        endpoint,
        headers=_operator_headers(),
        json=_payload(rating="A"),
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["binding_status"] == "UNCHANGED"

    with client.app.state.session_factory() as session:
        assert session.scalar(
            select(func.count(Evidence.id)).where(
                Evidence.evidence_key.like("PRIVATE-CREDIT-%")
            )
        ) == 1
        assert session.scalar(
            select(func.count(CompanyFact.id)).where(
                CompanyFact.source == "PRIVATE_DOCUMENT"
            )
        ) == 1
        fact = session.scalar(
            select(CompanyFact)
            .where(CompanyFact.source == "PRIVATE_DOCUMENT")
            .options(selectinload(CompanyFact.evidence))
        )
        assert fact is not None and fact.evidence is not None
        assert fact.verified is True
        assert fact.source == "PRIVATE_DOCUMENT"
        assert fact.value == {
            "value": "A0",
            "unit": "등급",
            "fact_binding_sha256": BINDING,
        }
        assert fact.evidence.sha256 == DOCUMENT_SHA256
        assert fact.evidence.source_location == PRIVATE_REFERENCE
        assert fact.evidence.metadata_json["verification_assurance"] == (
            "DOCUMENT_REVIEWED_NOT_ISSUER_AUTHENTICATED"
        )

        criterion = QuantitativeCriterion(
            criterion_id="credit-rating",
            category="CREDIT_RATING",
            label="기업신용평가등급",
            max_points=10,
            metric_key="company.credit_rating",
            unit="등급",
            formula_type="CATEGORICAL",
            formula="공고 원문 신용평가 등급표",
            categories=[CategoryScore(values=("A0",), points=10)],
            required_evidence_keys=["company.credit_rating"],
            fact_binding_sha256=BINDING,
        )
        resolved = resolve_verified_quantitative_facts(
            [criterion],
            [fact],
            as_of=datetime(2026, 8, 30, 9, tzinfo=timezone.utc),
        )
        assert len(resolved) == 1
        assert resolved[0].status == "CONFIRMED"
        assert resolved[0].value == "A0"


@pytest.mark.parametrize("rating", ("AA", "BBB", "BB", "B", "CCC"))
def test_private_credit_input_rejects_unmarked_grade_families(rating: str) -> None:
    with pytest.raises(ValueError):
        PrivateCreditRatingRegistration.model_validate(_payload(rating=rating))


def test_private_credit_input_normalizes_unicode_minus() -> None:
    payload = PrivateCreditRatingRegistration.model_validate(_payload(rating="A−"))

    assert payload.rating == "A-"


def test_private_credit_registration_rejects_expired_or_conflicting_metadata(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_notice(client)
    _enable_production_operator(client)
    monkeypatch.setattr(
        "pai_loop.private_company_evidence._credit_rating_binding_for_notice",
        lambda _notice: BINDING,
    )
    endpoint = (
        f"https://testserver/api/v1/operator-evidence/notices/{NOTICE_KEY}/credit-rating"
    )

    expired = client.post(
        endpoint,
        headers=_operator_headers(),
        json=_payload(valid_until="2026-08-29"),
    )
    assert expired.status_code == 422
    with client.app.state.session_factory() as session:
        assert session.scalar(
            select(func.count(Evidence.id)).where(
                Evidence.evidence_key.like("PRIVATE-CREDIT-%")
            )
        ) == 0
        assert session.scalar(
            select(func.count(CompanyFact.id)).where(
                CompanyFact.source == "PRIVATE_DOCUMENT"
            )
        ) == 0

    assert client.post(
        endpoint,
        headers=_operator_headers(),
        json=_payload(),
    ).status_code == 200
    conflict = client.post(
        endpoint,
        headers=_operator_headers(),
        json=_payload(rating="A+"),
    )
    assert conflict.status_code == 409
    assert DOCUMENT_SHA256 not in conflict.text
    assert PRIVATE_REFERENCE not in conflict.text


def test_credit_binding_is_derived_from_one_source_bound_notice_criterion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice = SimpleNamespace(versions=[])
    profile = object()
    monkeypatch.setattr(
        "pai_loop.private_company_evidence._current_dynamic_quantitative_profile",
        lambda _notice: profile,
    )
    monkeypatch.setattr(
        "pai_loop.private_company_evidence.quantitative_request_from_candidate_profile",
        lambda _profile: SimpleNamespace(
            criteria=[
                SimpleNamespace(
                    metric_key="company.credit_rating",
                    fact_binding_sha256=BINDING,
                )
            ]
        ),
    )

    assert _credit_rating_binding_for_notice(notice) == BINDING

    monkeypatch.setattr(
        "pai_loop.private_company_evidence.quantitative_request_from_candidate_profile",
        lambda _profile: SimpleNamespace(criteria=[]),
    )
    with pytest.raises(HTTPException) as exc_info:
        _credit_rating_binding_for_notice(notice)
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize(
    "reference",
    [
        "C:/private/certificate.pdf",
        "file:///private/certificate.pdf",
        "https://public.invalid/certificate.pdf",
        "private-evidence://user:password@synthetic/document",
        "private-evidence://synthetic/document?token=secret",
        "private-evidence://synthetic/document#private-hash",
    ],
)
def test_private_credit_registration_rejects_non_private_references(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
) -> None:
    _seed_notice(client)
    _enable_production_operator(client)
    monkeypatch.setattr(
        "pai_loop.private_company_evidence._credit_rating_binding_for_notice",
        lambda _notice: BINDING,
    )
    response = client.post(
        f"https://testserver/api/v1/operator-evidence/notices/{NOTICE_KEY}/credit-rating",
        headers=_operator_headers(),
        json=_payload(evidence_reference=reference),
    )
    assert response.status_code == 422
