from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.models import CompanyFact, Evidence, ReferenceDataVersion
from pai_loop.reference_registry import (
    MAX_REFERENCE_PAYLOAD_BYTES,
    REFERENCE_SPECS,
    ReferenceSpec,
    _assert_reference_safe,
    _validate_reference_payload,
    active_reference_metadata,
    packaged_reference_manifest,
    sync_packaged_reference_data,
    sync_public_company_profile,
)


def _session():
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, build_session_factory(engine)()


def test_packaged_reference_registry_sync_is_idempotent_and_materialises_company_profile() -> None:
    engine, session = _session()
    try:
        effective_at = datetime(2026, 8, 17, tzinfo=timezone.utc)
        first = sync_packaged_reference_data(session, effective_at=effective_at)
        profile_first = sync_public_company_profile(session)
        session.commit()

        assert first["created"] == len(REFERENCE_SPECS)
        assert first["unchanged"] == 0
        assert profile_first["facts_created"] >= 4
        assert len(active_reference_metadata(session)) == len(REFERENCE_SPECS)

        second = sync_packaged_reference_data(session, effective_at=effective_at)
        profile_second = sync_public_company_profile(session)
        session.commit()
        assert second["created"] == 0
        assert second["unchanged"] == len(REFERENCE_SPECS)
        assert profile_second["facts_created"] == 0
        assert profile_second["facts_updated"] == 0
        assert profile_second["evidence_updated"] == 0
        assert session.scalar(select(CompanyFact).where(CompanyFact.fact_key == "bidder_registration"))
    finally:
        session.close()
        engine.dispose()


def test_reference_version_label_is_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, session = _session()
    try:
        sync_packaged_reference_data(session)
        session.commit()
        row = session.scalar(
            select(ReferenceDataVersion).where(
                ReferenceDataVersion.dataset_key == "company_public_profile"
            )
        )
        assert row is not None
        row.content_sha256 = "0" * 64
        session.commit()
        with pytest.raises(ValueError, match="immutable"):
            sync_packaged_reference_data(session)
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def test_packaged_manifest_exposes_hashes_not_payloads() -> None:
    manifest = packaged_reference_manifest()
    assert len(manifest) == len(REFERENCE_SPECS)
    assert all(len(item["content_sha256"]) == 64 for item in manifest)
    assert all("payload_json" not in item for item in manifest)


def test_reference_api_exposes_metadata_and_idempotent_sync(client: TestClient) -> None:
    listed = client.get("/api/v1/reference-data/versions")
    assert listed.status_code == 200
    assert listed.json()["active_count"] == len(REFERENCE_SPECS)
    assert listed.json()["payloads_exposed"] is False
    assert all("payload_json" not in item for item in listed.json()["versions"])

    synced = client.post("/api/v1/reference-data/sync")
    assert synced.status_code == 200
    assert synced.json()["status"] == "COMPLETED"
    assert synced.json()["reference"]["unchanged"] == len(REFERENCE_SPECS)


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "not-a-real-but-populated-key"},
        {"source": "C:\\Users\\someone\\private.xlsx"},
        {"contact": "redacted-test-value"},
        {"source_url": "https://example.invalid/data?serviceKey=secret"},
    ],
)
def test_reference_publication_guard_rejects_sensitive_values(payload: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="forbidden|sensitive|credential"):
        _assert_reference_safe(payload)


def test_reference_payload_size_is_bounded() -> None:
    spec = ReferenceSpec("oversized", "unused.json", ("version",), "1.0")
    with pytest.raises(ValueError, match="online JSON limit"):
        _validate_reference_payload(spec, {"value": "x" * MAX_REFERENCE_PAYLOAD_BYTES})


def test_company_profile_sync_retires_removed_managed_facts_and_evidence() -> None:
    engine, session = _session()
    try:
        first_at = datetime(2026, 8, 17, tzinfo=timezone.utc)
        sync_public_company_profile(session, effective_at=first_at)
        session.add_all(
            [
                Evidence(
                    evidence_key="STALE-PUBLIC-EVIDENCE",
                    name="removed",
                    evidence_type="PUBLIC_PROFILE_REFERENCE",
                    status="VERIFIED",
                    valid_from=first_at,
                ),
                CompanyFact(
                    fact_key="removed_public_fact",
                    value=True,
                    effective_from=first_at,
                    verified=True,
                    source="PUBLIC_PROFILE",
                ),
                CompanyFact(
                    fact_key="bidder_registration",
                    value=False,
                    effective_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
                    verified=False,
                    source="PUBLIC_PROFILE",
                ),
            ]
        )
        session.commit()

        second_at = datetime(2026, 8, 18, tzinfo=timezone.utc)
        result = sync_public_company_profile(session, effective_at=second_at)
        session.commit()
        assert result["evidence_retired"] == 1
        assert result["facts_retired"] == 2
        stale_evidence = session.scalar(
            select(Evidence).where(Evidence.evidence_key == "STALE-PUBLIC-EVIDENCE")
        )
        stale_fact = session.scalar(
            select(CompanyFact).where(CompanyFact.fact_key == "removed_public_fact")
        )
        replacement = session.scalar(
            select(CompanyFact).where(
                CompanyFact.fact_key == "bidder_registration",
                CompanyFact.effective_from >= datetime(2026, 8, 1),
            )
        )
        assert stale_evidence is not None and stale_evidence.status == "RETIRED"
        assert stale_evidence.valid_until is not None
        assert stale_fact is not None and stale_fact.effective_to is not None
        assert replacement is not None and replacement.effective_to is not None

        unchanged = sync_public_company_profile(session, effective_at=second_at)
        session.commit()
        assert unchanged == {
            "evidence_created": 0,
            "evidence_updated": 0,
            "evidence_retired": 0,
            "facts_created": 0,
            "facts_updated": 0,
            "facts_retired": 0,
        }
    finally:
        session.close()
        engine.dispose()


def test_company_profile_sync_fails_closed_on_evidence_key_collision() -> None:
    engine, session = _session()
    try:
        session.add(
            Evidence(
                evidence_key="EVIDENCE-BIDDER-REGISTRATION",
                name="unmanaged collision",
                evidence_type="MANUAL",
                status="VERIFIED",
            )
        )
        session.commit()
        with pytest.raises(ValueError, match="collides with another source"):
            sync_public_company_profile(session)
        session.rollback()
    finally:
        session.close()
        engine.dispose()
