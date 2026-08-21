from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from pai_loop.daily_analysis_scope import (
    MAX_MATERIAL_NOTICE_KEYS,
    MATERIAL_SCOPE_VERSION,
    material_scope_fields,
    material_scope_sha256,
    validated_material_scope,
)
from pai_loop.models import IngestionJob, Notice


def _source_ingestion(client: TestClient, material_keys: list[str]) -> str:
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        for index, key in enumerate(material_keys):
            session.add(
                Notice(
                    notice_key=key,
                    bid_notice_no=f"SCOPE-{index}",
                    revision_no="00",
                    title=f"분석 범위 공고 {index}",
                    agency="가상 공공기관",
                    published_at=now,
                    deadline=now + timedelta(days=5),
                    status="OPEN",
                    category="용역",
                )
            )
        job = IngestionJob(
            source="PPS",
            mode="LIVE",
            status="COMPLETED",
            window_json={"from": now.date().isoformat(), "to": now.date().isoformat()},
            request_json={
                "page_size": 100,
                "max_pages": 5,
                **material_scope_fields(material_keys),
            },
            created_count=len(material_keys),
            matched=len(material_keys),
            notice_keys=material_keys,
            warnings=[],
            created_at=now,
            completed_at=now,
        )
        session.add(job)
        session.commit()
        return job.id


def _daily_plan_payload(ingestion_id: str, material_keys: list[str]) -> dict:
    return {
        "queue_name": "DAILY",
        "notice_keys": material_keys,
        "refresh_notice_keys": [],
        "retry_notice_keys": [],
        "request_token": f"w10:test:{ingestion_id}",
        "source_ingestion_job_id": ingestion_id,
        "source_material_notice_keys": list(reversed(material_keys)),
        "dry_run": False,
        "chunk_size": 1,
        "max_total": 3012,
        "execution_limit": 30,
        "max_continuations": 128,
        "include_retryable": False,
        "retry_cooldown_hours": 24,
        "reservation_ttl_hours": 6,
        "resume_active": True,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"material_scope_version": "wrong"},
        {"material_notice_keys": "PPS-A"},
        {"material_notice_keys": ["PPS-A"] * (MAX_MATERIAL_NOTICE_KEYS + 1)},
        {"material_notice_keys": [""]},
        {"material_notice_keys": [" PPS-A"]},
        {"material_notice_keys": ["PPS-B", "PPS-A"]},
        {"material_notice_key_count": True},
        {"material_notice_key_count": 2},
        {"material_notice_keys_sha256": 123},
        {"material_notice_keys_sha256": "0" * 64},
    ],
)
def test_material_scope_validation_rejects_partial_or_forged_fields(
    mutation: dict[str, object],
) -> None:
    payload = material_scope_fields(["PPS-A"])
    payload.update(mutation)

    assert validated_material_scope(payload) is None


def test_daily_plan_persists_exact_pps_ingestion_scope_binding(
    client: TestClient,
) -> None:
    keys = ["PPS-SCOPE-B", "PPS-SCOPE-A"]
    ingestion_id = _source_ingestion(client, keys)

    response = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=_daily_plan_payload(ingestion_id, keys),
    )

    assert response.status_code == 200, response.text
    parent_id = response.json()["job_id"]
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, parent_id)
        assert parent is not None
        config = parent.request_json
        expected = sorted(keys)
        assert config["source_ingestion_job_id"] == ingestion_id
        assert config["source_material_scope_version"] == MATERIAL_SCOPE_VERSION
        assert config["source_material_notice_keys"] == expected
        assert config["source_material_notice_key_count"] == 2
        assert config["source_material_notice_keys_sha256"] == material_scope_sha256(
            expected
        )
        assert set(expected).issubset(parent.notice_keys)


def test_daily_plan_rejects_scope_not_emitted_by_bound_pps_ingestion(
    client: TestClient,
) -> None:
    ingestion_id = _source_ingestion(client, ["PPS-SOURCE-ONLY"])
    mismatched = _daily_plan_payload(ingestion_id, ["PPS-SOURCE-ONLY"])
    mismatched["source_material_notice_keys"] = []

    response = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=mismatched,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "DAILY source material scope does not match the PPS audit"
    )


def test_new_ingestion_rebinds_active_daily_parent_and_appends_coverage(
    client: TestClient,
) -> None:
    first_key = "PPS-MULTI-A"
    first_ingestion = _source_ingestion(client, [first_key])
    first = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=_daily_plan_payload(first_ingestion, [first_key]),
    )
    assert first.status_code == 200, first.text

    second_key = "PPS-MULTI-B"
    second_ingestion = _source_ingestion(client, [second_key])
    second = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=_daily_plan_payload(second_ingestion, [second_key]),
    )
    assert second.status_code == 200, second.text
    assert second.json()["job_id"] == first.json()["job_id"]

    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, first.json()["job_id"])
        assert parent is not None
        assert parent.request_json["source_ingestion_job_id"] == second_ingestion
        assert parent.request_json["source_material_notice_keys"] == [second_key]
        assert {first_key, second_key}.issubset(set(parent.notice_keys))
