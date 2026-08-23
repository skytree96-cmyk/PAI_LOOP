from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from pai_loop.daily_analysis_scope import (
    MAX_MATERIAL_NOTICE_KEYS,
    MATERIAL_SCOPE_VERSION,
    SOURCE_ANALYSIS_ELIGIBILITY_POLICY,
    material_scope_fields,
    material_scope_sha256,
    validated_material_scope,
)
from pai_loop.models import IngestionJob, Notice


def _source_ingestion(
    client: TestClient,
    material_keys: list[str],
    *,
    expired_keys: set[str] | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    expired_keys = expired_keys or set()
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
                    deadline=(
                        now - timedelta(days=1)
                        if key in expired_keys
                        else now + timedelta(days=5)
                    ),
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
        assert config["source_analysis_notice_keys"] == expected
        assert config["source_analysis_notice_key_count"] == 2
        assert config["source_analysis_notice_keys_sha256"] == material_scope_sha256(
            expected
        )
        assert config["source_analysis_excluded_notice_key_count"] == 0
        assert (
            config["source_analysis_eligibility_policy"]
            == SOURCE_ANALYSIS_ELIGIBILITY_POLICY
        )
        assert datetime.fromisoformat(config["source_analysis_scoped_at"]).tzinfo
        assert set(expected).issubset(parent.notice_keys)


def test_daily_plan_audits_expired_source_without_analysing_it(
    client: TestClient,
) -> None:
    active_key = "PPS-SCOPE-ACTIVE"
    expired_key = "PPS-SCOPE-EXPIRED"
    keys = [active_key, expired_key]
    ingestion_id = _source_ingestion(
        client,
        keys,
        expired_keys={expired_key},
    )

    response = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=_daily_plan_payload(ingestion_id, keys),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["planned"] == 1
    assert body["offered"] == 1
    assert body["notice_keys"] == [active_key]
    assert body["chunks"] == [[active_key]]
    assert "SOURCE_MATERIAL_NOT_ANALYZABLE:1" in body["warnings"]

    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, body["job_id"])
        assert parent is not None
        assert parent.request_json["source_material_notice_keys"] == sorted(keys)
        assert parent.request_json["source_analysis_notice_keys"] == [active_key]
        assert parent.request_json["source_analysis_notice_key_count"] == 1
        assert parent.request_json[
            "source_analysis_notice_keys_sha256"
        ] == material_scope_sha256([active_key])
        assert parent.request_json[
            "source_analysis_excluded_notice_key_count"
        ] == 1
        assert parent.notice_keys == [active_key]
        assert session.scalar(
            select(Notice).where(Notice.notice_key == expired_key)
        ) is not None


def test_daily_plan_reuses_terminal_parent_for_all_expired_retry(
    client: TestClient,
) -> None:
    expired_key = "PPS-SCOPE-ALL-EXPIRED"
    ingestion_id = _source_ingestion(
        client,
        [expired_key],
        expired_keys={expired_key},
    )
    payload = _daily_plan_payload(ingestion_id, [expired_key])

    first = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=payload,
    )
    second = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=payload,
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["status"] == "COMPLETED"
    assert first.json()["planned"] == 0
    assert second.json()["job_id"] == first.json()["job_id"]
    with client.app.state.session_factory() as session:
        parents = [
            job
            for job in session.scalars(
                select(IngestionJob).where(
                    IngestionJob.source == "ANALYSIS_BACKFILL"
                )
            ).all()
            if isinstance(job.request_json, dict)
            and job.request_json.get("source_ingestion_job_id") == ingestion_id
        ]
        assert len(parents) == 1


def test_terminal_dry_run_parent_does_not_consume_live_daily_plan(
    client: TestClient,
) -> None:
    expired_key = "PPS-SCOPE-DRY-THEN-LIVE"
    ingestion_id = _source_ingestion(
        client,
        [expired_key],
        expired_keys={expired_key},
    )
    dry_payload = _daily_plan_payload(ingestion_id, [expired_key])
    dry_payload["dry_run"] = True

    dry = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=dry_payload,
    )
    live = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=_daily_plan_payload(ingestion_id, [expired_key]),
    )

    assert dry.status_code == 200, dry.text
    assert live.status_code == 200, live.text
    assert dry.json()["job_id"] != live.json()["job_id"]
    with client.app.state.session_factory() as session:
        dry_parent = session.get(IngestionJob, dry.json()["job_id"])
        live_parent = session.get(IngestionJob, live.json()["job_id"])
        assert dry_parent is not None and dry_parent.mode == "DRY_RUN"
        assert live_parent is not None and live_parent.mode == "LIVE"


def test_any_resume_prunes_expired_unclaimed_key_from_legacy_daily_parent(
    client: TestClient,
) -> None:
    expired_key = "PPS-LEGACY-EXPIRED-UNCLAIMED"
    ingestion_id = _source_ingestion(
        client,
        [expired_key],
        expired_keys={expired_key},
    )
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        parent = IngestionJob(
            source="ANALYSIS_BACKFILL",
            mode="LIVE",
            status="RUNNING",
            window_json={"scope": "OPEN_NOT_SELECTED", "as_of": now.isoformat()},
            request_json={
                "queue_name": "DAILY",
                "dry_run": False,
                "chunk_size": 1,
                "execution_limit": 30,
                "max_continuations": 128,
                "include_retryable": False,
                "retry_cooldown_hours": 24,
                "reservation_ttl_hours": 6,
                "work_generations": {expired_key: 0},
                "source_ingestion_job_id": ingestion_id,
                "source_material_scope_version": MATERIAL_SCOPE_VERSION,
                "source_material_notice_keys": [expired_key],
                "source_material_notice_key_count": 1,
                "source_material_notice_keys_sha256": material_scope_sha256(
                    [expired_key]
                ),
            },
            matched=1,
            notice_keys=[expired_key],
            warnings=[],
            created_at=now,
        )
        session.add(parent)
        session.commit()
        parent_id = parent.id

    response = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "ANY",
            "resume_only": True,
            "request_token": "w11:legacy-expired-resume",
            "dry_run": False,
            "chunk_size": 1,
            "execution_limit": 30,
            "max_continuations": 128,
            "include_retryable": False,
            "retry_cooldown_hours": 24,
            "reservation_ttl_hours": 6,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == parent_id
    assert body["status"] == "COMPLETED"
    assert body["planned"] == 0
    assert body["offered"] == 0
    assert body["chunks"] == []
    assert "INELIGIBLE_UNCLAIMED_PRUNED:1" in body["warnings"]
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, parent_id)
        assert parent is not None
        assert parent.notice_keys == []
        assert parent.request_json["source_analysis_notice_keys"] == []
        assert parent.request_json[
            "source_analysis_excluded_notice_key_count"
        ] == 1


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


def test_daily_plan_fails_closed_when_bound_source_notice_is_missing(
    client: TestClient,
) -> None:
    key = "PPS-SOURCE-MISSING"
    ingestion_id = _source_ingestion(client, [key])
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == key))
        assert notice is not None
        session.delete(notice)
        session.commit()

    response = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=_daily_plan_payload(ingestion_id, [key]),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "DAILY source material notice is missing from the database"
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
