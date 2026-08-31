from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import event

import pai_loop.analysis_api as analysis_api
from pai_loop.daily_analysis_scope import MATERIAL_SCOPE_VERSION, material_scope_sha256
from pai_loop.models import IngestionJob


def _job(
    *,
    job_id: str,
    source: str,
    request_json: dict,
    notice_keys: list[str],
    created_at: datetime,
    mode: str = "LIVE",
) -> IngestionJob:
    return IngestionJob(
        id=job_id,
        source=source,
        mode=mode,
        status="COMPLETED",
        window_json={"scope": "SYNTHETIC_QUERY_BOUND"},
        request_json=request_json,
        notice_keys=notice_keys,
        warnings=[],
        created_at=created_at,
        completed_at=created_at + timedelta(minutes=1),
    )


def _loaded_ingestion_job_ids(call):
    loaded: list[str] = []

    def record_loaded(target: IngestionJob, _context) -> None:
        loaded.append(target.id)

    event.listen(IngestionJob, "load", record_loaded)
    try:
        result = call()
    finally:
        event.remove(IngestionJob, "load", record_loaded)
    return result, loaded


def test_backfill_children_filters_parent_before_orm_hydration(
    client: TestClient,
) -> None:
    now = datetime.now(timezone.utc)
    parent_id = "10000000-0000-4000-8000-000000000001"
    target_child_id = "10000000-0000-4000-8000-000000000002"
    unrelated_child_id = "10000000-0000-4000-8000-000000000003"
    direct_child_id = "10000000-0000-4000-8000-000000000004"
    with client.app.state.session_factory() as session:
        session.add_all(
            [
                _job(
                    job_id=target_child_id,
                    source="ANALYSIS",
                    request_json={"parent_job_id": parent_id},
                    notice_keys=["SYN-TARGET"],
                    created_at=now,
                ),
                _job(
                    job_id=unrelated_child_id,
                    source="ANALYSIS",
                    request_json={"parent_job_id": "unrelated-parent"},
                    notice_keys=["SYN-UNRELATED"],
                    created_at=now + timedelta(seconds=1),
                ),
                _job(
                    job_id=direct_child_id,
                    source="ANALYSIS",
                    request_json={"result_json": {"status": "COMPLETED"}},
                    notice_keys=["SYN-DIRECT"],
                    created_at=now + timedelta(seconds=2),
                ),
            ]
        )
        session.commit()

    with client.app.state.session_factory() as session:
        children, loaded = _loaded_ingestion_job_ids(
            lambda: analysis_api._backfill_children(session, parent_id)
        )

    assert [child.id for child in children] == [target_child_id]
    assert loaded == [target_child_id]


def test_retry_epoch_lookup_hydrates_only_matching_parent_and_children(
    client: TestClient,
) -> None:
    now = datetime.now(timezone.utc)
    retry_epoch = "2026-08-31"
    notice_key = "SYN-RETRY-EPOCH-TARGET"
    parent_id = "20000000-0000-4000-8000-000000000001"
    child_id = "20000000-0000-4000-8000-000000000002"
    unrelated_parent_id = "20000000-0000-4000-8000-000000000003"
    unrelated_child_id = "20000000-0000-4000-8000-000000000004"
    with client.app.state.session_factory() as session:
        session.add_all(
            [
                _job(
                    job_id=parent_id,
                    source="ANALYSIS_BACKFILL",
                    request_json={
                        "queue_name": "DAILY",
                        "retry_tokens": {notice_key: retry_epoch},
                        "work_generations": {notice_key: 0},
                    },
                    notice_keys=[notice_key],
                    created_at=now,
                ),
                _job(
                    job_id=child_id,
                    source="ANALYSIS",
                    request_json={
                        "parent_job_id": parent_id,
                        "work_generations": {notice_key: 0},
                    },
                    notice_keys=[notice_key],
                    created_at=now + timedelta(seconds=1),
                ),
                _job(
                    job_id=unrelated_parent_id,
                    source="ANALYSIS_BACKFILL",
                    request_json={
                        "queue_name": "DAILY",
                        "retry_tokens": {notice_key: "2026-08-30"},
                        "work_generations": {notice_key: 0},
                    },
                    notice_keys=[notice_key],
                    created_at=now + timedelta(seconds=2),
                ),
                _job(
                    job_id=unrelated_child_id,
                    source="ANALYSIS",
                    request_json={
                        "parent_job_id": unrelated_parent_id,
                        "work_generations": {notice_key: 0},
                    },
                    notice_keys=[notice_key],
                    created_at=now + timedelta(seconds=3),
                ),
            ]
        )
        session.commit()

    with client.app.state.session_factory() as session:
        consumed, loaded = _loaded_ingestion_job_ids(
            lambda: analysis_api._completed_retry_epoch_keys(
                session,
                [notice_key],
                retry_epoch=retry_epoch,
            )
        )

    assert consumed == {notice_key}
    assert set(loaded) == {parent_id, child_id}


def test_latest_terminal_attempt_query_projects_fields_without_orm_jobs(
    client: TestClient,
) -> None:
    now = datetime.now(timezone.utc)
    notice_key = "SYN-REFRESH-ATTEMPT-TARGET"
    relevant_completed_at = now - timedelta(minutes=15)
    with client.app.state.session_factory() as session:
        session.add_all(
            [
                _job(
                    job_id="30000000-0000-4000-8000-000000000001",
                    source="ANALYSIS",
                    request_json={"parent_job_id": "linked-parent"},
                    notice_keys=[notice_key],
                    created_at=relevant_completed_at - timedelta(minutes=1),
                ),
                _job(
                    job_id="30000000-0000-4000-8000-000000000002",
                    source="ANALYSIS",
                    request_json={"parent_job_id": "other-parent"},
                    notice_keys=["SYN-OTHER-REFRESH"],
                    created_at=now - timedelta(minutes=10),
                ),
                _job(
                    job_id="30000000-0000-4000-8000-000000000003",
                    source="ANALYSIS",
                    request_json={"result_json": {"status": "COMPLETED"}},
                    notice_keys=[notice_key],
                    created_at=now - timedelta(minutes=5),
                ),
            ]
        )
        session.commit()

    refresh_run = SimpleNamespace(generated_at=now - timedelta(hours=1))
    with client.app.state.session_factory() as session:
        attempts, loaded = _loaded_ingestion_job_ids(
            lambda: analysis_api._latest_terminal_analysis_child_attempts(
                session,
                {notice_key: refresh_run},
            )
        )

    assert attempts[notice_key] == analysis_api._utc(relevant_completed_at)
    assert loaded == []


def test_terminal_daily_source_lookup_hydrates_only_bound_source(
    client: TestClient,
) -> None:
    now = datetime.now(timezone.utc)
    source_ingestion_job_id = "40000000-0000-4000-8000-000000000001"
    matching_parent_id = "40000000-0000-4000-8000-000000000002"
    unrelated_parent_id = "40000000-0000-4000-8000-000000000003"
    material_keys = ["SYN-BOUND-SOURCE"]

    def parent_config(source_id: str) -> dict:
        return {
            "queue_name": "DAILY",
            "dry_run": False,
            "source_ingestion_job_id": source_id,
            "source_material_scope_version": MATERIAL_SCOPE_VERSION,
            "source_material_notice_keys": material_keys,
            "source_material_notice_key_count": len(material_keys),
            "source_material_notice_keys_sha256": material_scope_sha256(
                material_keys
            ),
        }

    with client.app.state.session_factory() as session:
        session.add_all(
            [
                _job(
                    job_id=matching_parent_id,
                    source="ANALYSIS_BACKFILL",
                    request_json=parent_config(source_ingestion_job_id),
                    notice_keys=material_keys,
                    created_at=now,
                ),
                _job(
                    job_id=unrelated_parent_id,
                    source="ANALYSIS_BACKFILL",
                    request_json=parent_config("unrelated-source-ingestion"),
                    notice_keys=material_keys,
                    created_at=now + timedelta(seconds=1),
                ),
            ]
        )
        session.commit()

    source_binding = {
        "source_ingestion_job_id": source_ingestion_job_id,
        "source_material_notice_keys": material_keys,
    }
    with client.app.state.session_factory() as session:
        matched, loaded = _loaded_ingestion_job_ids(
            lambda: analysis_api._matching_terminal_daily_source_parent(
                session,
                source_binding,
                dry_run=False,
            )
        )

    assert matched is not None
    assert matched.id == matching_parent_id
    assert loaded == [matching_parent_id]
