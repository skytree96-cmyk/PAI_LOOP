from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from pai_loop.analysis_api import (
    AnalysisBackfillPlanRequest,
    _select_backfill_notice_keys,
)
from pai_loop.integrations.openai_extraction import PROMPT_VERSION, SCHEMA_VERSION
from pai_loop import daily_operations
from pai_loop.models import (
    AnalysisRun,
    AwardHistoryItem,
    CompanyFact,
    Evidence,
    Evaluation,
    IngestionJob,
    MockNotification,
    Notice,
    NoticeVersion,
    RecommendationSnapshot,
    ScoreSnapshot,
)
from pai_loop.pps_enrichment import (
    PPS_ATTACHMENT_SOURCE,
    PPS_METADATA_KIND,
    PPS_METADATA_SCHEMA,
    PPS_PROCESSING_VERSION,
)
from pai_loop.public_notice_seed import import_public_notice_seed


def _create_notice(
    client: TestClient,
    *,
    notice_key: str,
    published_at: str,
    title: str = "공공기관 AI 교육 및 컨설팅 용역",
    agency: str = "가상 공공기관",
    deadline: str = "2026-08-31T09:00:00+09:00",
    status: str = "OPEN",
) -> None:
    response = client.post(
        "/api/v1/notices",
        json={
            "notice_key": notice_key,
            "bid_notice_no": notice_key,
            "title": title,
            "agency": agency,
            "published_at": published_at,
            "deadline": deadline,
            "status": status,
            "estimated_amount": 120_000_000,
        },
    )
    assert response.status_code == 201


def test_daily_briefing_is_seven_day_stored_data_view_with_zero_source_calls(
    client: TestClient,
) -> None:
    _create_notice(
        client,
        notice_key="DAILY-RECENT",
        published_at="2026-08-16T08:30:00+09:00",
        title="공공기관 팀빌딩 및 조직문화 교육 위탁운영",
        agency="인천광역시",
    )
    _create_notice(
        client,
        notice_key="DAILY-OLD",
        published_at="2026-08-01T08:30:00+09:00",
    )

    response = client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "as_of": "2026-08-17T09:00:00+09:00"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "Asia/Seoul"
    assert body["window"]["days"] == 7
    assert body["totals"]["observed"] == 1
    assert [item["notice_key"] for item in body["notices"]] == ["DAILY-RECENT"]
    assert body["notices"][0]["fit"]["eligibility"] == "PENDING"
    assert body["notices"][0]["fit"]["risk_score"] is None
    assert body["notices"][0]["fit"]["risk_band"] == "UNKNOWN"
    assert body["notices"][0]["top_departments"]
    assert body["notices"][0]["department_review_candidates"]
    assert body["notices"][0]["region_routing"][0]["department_id"] == "region-central"
    assert all(
        item["ranking_scope"] == "BUSINESS"
        for item in body["notices"][0]["top_departments"]
    )
    assert body["notices"][0]["quantitative_estimate"]["overall_status"] == "REVIEW"
    assert body["notices"][0]["pricing_intelligence"]["record_count"] == 0
    assert (
        body["notices"][0]["pricing_intelligence"]["prediction"]["award_rate"]["status"]
        == "INSUFFICIENT_DATA"
    )
    assert body["notices"][0]["competition_risk"]["status"] == "UNKNOWN"
    assert body["notices"][0]["competition_risk"]["score"] is None
    assert body["notices"][0]["analysis_snapshot"] is None
    assert body["analysis_queue"] == {
        "policy": "NEVER_ATTEMPTED_THEN_OLDEST_RETRY",
        "pending_total": 1,
        "never_attempted_total": 1,
        "retryable_total": 0,
        "deferred_terminal_total": 0,
        "notice_keys": ["DAILY-RECENT"],
        "never_attempted_notice_keys": ["DAILY-RECENT"],
        "retryable_notice_keys": [],
        "limit": 50,
        "note": "미시도 공고를 먼저 처리하고 구버전 분석은 현재 정책으로 점진 갱신하며, 실패 건은 가장 오래된 시도부터 재검토합니다. 첨부 없음·미지원 형식은 manifest가 바뀔 때까지 자동 재시도하지 않습니다.",
    }
    assert body["source_calls"] == {"pps": 0, "openai": 0, "teams": 0}
    assert body["delivery"] == {
        "channel": "teams",
        "mode": "mock",
        "actual_push_sent": False,
    }


def test_daily_briefing_batches_notice_history_and_loads_only_latest_snapshot(
    client: TestClient,
    monkeypatch,
) -> None:
    published_at = "2026-08-16T08:30:00+09:00"
    notice_keys = ("DAILY-BATCH-A", "DAILY-BATCH-B")
    for notice_key in notice_keys:
        _create_notice(
            client,
            notice_key=notice_key,
            published_at=published_at,
        )

    with client.app.state.session_factory() as session:
        for index, notice_key in enumerate(notice_keys):
            notice = session.query(Notice).filter_by(notice_key=notice_key).one()
            version = NoticeVersion(
                notice_id=notice.id,
                version_no=1,
                file_sha256=hashlib.sha256(notice_key.encode()).hexdigest(),
                document_complete=True,
                extraction_status="COMPLETE",
                extraction_confidence=1.0,
                source_payload={"kind": "PUBLIC_DOCUMENT_REFERENCE"},
            )
            session.add(version)
            session.flush()
            old_evaluation = Evaluation(
                notice_id=notice.id,
                notice_version_id=version.id,
                evaluated_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
                deadline_snapshot_at=notice.deadline,
                eligibility="FAIL",
                reason_code="OLD",
                readiness_score=1,
                readiness_status="RED",
                evidence_coverage=1,
                risk_score=99,
                risk_band="NO_GO",
                atomic_results=[{"discarded": "x" * 20_000}],
                explanation={"discarded": "x" * 20_000},
            )
            latest_evaluation = Evaluation(
                notice_id=notice.id,
                notice_version_id=version.id,
                evaluated_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
                deadline_snapshot_at=notice.deadline,
                eligibility="PASS",
                reason_code="CURRENT",
                readiness_score=88,
                readiness_status="GREEN",
                evidence_coverage=92,
                risk_score=12,
                risk_band="GO",
                atomic_results=[{"discarded": "y" * 20_000}],
                explanation={"discarded": "y" * 20_000},
            )
            session.add_all([old_evaluation, latest_evaluation])
            session.flush()
            old_run = AnalysisRun(
                notice_id=notice.id,
                notice_version_id=version.id,
                evaluation_id=old_evaluation.id,
                idempotency_key=f"daily-old-{index}",
                input_sha256=hashlib.sha256(f"old-{index}".encode()).hexdigest(),
                basis_versions={
                    "pipeline": daily_operations.PIPELINE_VERSION,
                    "requirement_policy": daily_operations.POLICY_VERSION,
                    "quantitative_engine": (
                        daily_operations.QUANTITATIVE_ENGINE_VERSION
                    ),
                },
                input_manifest={"discarded": "x" * 40_000},
                output_summary={"marker": f"old-{index}"},
                generated_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
            )
            latest_run = AnalysisRun(
                notice_id=notice.id,
                notice_version_id=version.id,
                evaluation_id=latest_evaluation.id,
                idempotency_key=f"daily-latest-{index}",
                input_sha256=hashlib.sha256(f"latest-{index}".encode()).hexdigest(),
                basis_versions={
                    "pipeline": daily_operations.PIPELINE_VERSION,
                    "requirement_policy": daily_operations.POLICY_VERSION,
                    "quantitative_engine": (
                        daily_operations.QUANTITATIVE_ENGINE_VERSION
                    ),
                },
                input_manifest={"discarded": "y" * 40_000},
                output_summary={"marker": f"latest-{index}"},
                generated_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
            )
            session.add_all([old_run, latest_run])
            session.flush()
            session.add(
                ScoreSnapshot(
                    analysis_run_id=latest_run.id,
                    score_key="readiness",
                    score_type="READINESS",
                    value=88,
                    status="AVAILABLE",
                )
            )
            session.add(
                RecommendationSnapshot(
                    analysis_run_id=latest_run.id,
                    recommendation_key=f"department-{index}",
                    department_id="ai_future_education",
                    rank=1,
                    priority_score=90,
                    recommendation="GO",
                    risk_band="GO",
                )
            )
        session.commit()

    monkeypatch.setattr(daily_operations, "_DAILY_BRIEFING_BATCH_SIZE", 1)
    statements: list[str] = []

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.casefold().split()))

    event.listen(client.app.state.engine, "before_cursor_execute", capture_sql)
    try:
        response = client.get(
            "/api/v1/operations/daily-briefing",
            params={
                "days": 7,
                "limit": 50,
                "as_of": "2026-08-17T09:00:00+09:00",
            },
        )
    finally:
        event.remove(client.app.state.engine, "before_cursor_execute", capture_sql)

    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["observed"] == 2
    notices = {item["notice_key"]: item for item in body["notices"]}
    for index, notice_key in enumerate(notice_keys):
        item = notices[notice_key]
        assert item["fit"]["eligibility"] == "PASS"
        assert item["analysis_snapshot"]["output_summary"] == {
            "marker": f"latest-{index}"
        }
        assert item["analysis_snapshot"]["scores"][0]["value"] == 88
        assert item["analysis_snapshot"]["recommendations"][0][
            "recommendation"
        ] == "GO"

    candidate_queries = [
        statement
        for statement in statements
        if " from notices " in statement and "coalesce(" in statement
    ]
    assert len(candidate_queries) == 1
    assert "notices.title" not in candidate_queries[0]
    notice_batch_queries = [
        statement
        for statement in statements
        if " from notices " in statement and "notices.id in" in statement
    ]
    assert len(notice_batch_queries) == 2
    assert not any("evaluations.atomic_results" in statement for statement in statements)
    assert not any("evaluations.explanation" in statement for statement in statements)
    assert not any("analysis_runs.input_manifest" in statement for statement in statements)


def test_daily_briefing_passes_evidence_loaded_company_facts_to_quantitative_bridge(
    client: TestClient,
    monkeypatch,
) -> None:
    _create_notice(
        client,
        notice_key="DAILY-QUANT-FACT-BRIDGE",
        published_at="2026-08-16T08:30:00+09:00",
    )
    with client.app.state.session_factory() as session:
        evidence = Evidence(
            evidence_key="DAILY-QUANT-EVIDENCE",
            name="정량 증빙",
            evidence_type="QUANTITATIVE_FACT",
            status="VERIFIED",
            issued_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        session.add(evidence)
        session.flush()
        session.add(
            CompanyFact(
                fact_key="company.performance.amount",
                value=1_000_000_000,
                effective_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
                verified=True,
                evidence_id=evidence.id,
            )
        )
        session.commit()

    captured: list[tuple[CompanyFact, ...]] = []
    original = daily_operations.estimate_for_notice

    def capture(notice: Notice, company_facts=()):
        facts = tuple(company_facts)
        captured.append(facts)
        return original(notice, facts)

    monkeypatch.setattr(daily_operations, "estimate_for_notice", capture)
    response = client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "as_of": "2026-08-17T09:00:00+09:00"},
    )

    assert response.status_code == 200
    assert len(captured) == 1
    quantitative_fact = next(
        item
        for item in captured[0]
        if item.fact_key == "company.performance.amount"
    )
    assert quantitative_fact.evidence is not None
    assert quantitative_fact.evidence.status == "VERIFIED"


def test_daily_briefing_includes_only_currently_open_notices(client: TestClient) -> None:
    published_at = "2026-08-16T08:30:00+09:00"
    _create_notice(
        client,
        notice_key="DAILY-ACTIVE-OPEN",
        published_at=published_at,
        deadline="2026-08-17T00:00:00+00:00",
    )
    _create_notice(
        client,
        notice_key="DAILY-CLOSED",
        published_at=published_at,
        deadline="2026-08-31T09:00:00+09:00",
        status="CLOSED",
    )
    _create_notice(
        client,
        notice_key="DAILY-EXPIRED-OPEN",
        published_at=published_at,
        deadline="2026-08-16T23:59:59+00:00",
    )

    response = client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "as_of": "2026-08-17T09:00:00+09:00"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["observed"] == 1
    assert body["totals"]["included"] == 1
    assert [item["notice_key"] for item in body["notices"]] == ["DAILY-ACTIVE-OPEN"]


def test_daily_analysis_queue_does_not_let_failed_or_terminal_items_starve_new_work(
    client: TestClient,
) -> None:
    published_at = "2026-08-16T08:30:00+09:00"
    for notice_key in ("PPS-NEVER", "PPS-RETRY", "PPS-UNSUPPORTED"):
        _create_notice(client, notice_key=notice_key, published_at=published_at)

    def attachment(suffix: str, media_type: str, token: str) -> dict:
        return {
            "attachment_id": f"PPS-ATT-{token * 24}",
            "file_name": f"공고문{suffix}",
            "media_type": media_type,
            "url": (
                "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
                f"?bidPbancNo={token * 8}&fileSeq=1"
            ),
            "slot": 1,
        }

    pdf_never = attachment(".pdf", "application/pdf", "a")
    pdf_retry = attachment(".pdf", "application/pdf", "b")
    unsupported_terminal = attachment(".bin", "application/octet-stream", "c")
    manifest_sha = hashlib.sha256(
        json.dumps(
            pdf_retry,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    with client.app.state.session_factory() as session:
        notices = {
            item.notice_key: item
            for item in session.query(Notice).filter(Notice.notice_key.like("PPS-%")).all()
        }
        for key, manifest in (
            ("PPS-NEVER", [pdf_never]),
            ("PPS-RETRY", [pdf_retry]),
            ("PPS-UNSUPPORTED", [unsupported_terminal]),
        ):
            session.add(
                NoticeVersion(
                    notice_id=notices[key].id,
                    version_no=1,
                    file_sha256=hashlib.sha256(key.encode()).hexdigest(),
                    document_complete=False,
                    extraction_status="METADATA",
                    extraction_confidence=1.0,
                    source_payload={
                        "kind": PPS_METADATA_KIND,
                        "schema_version": PPS_METADATA_SCHEMA,
                        "attachment_manifest": manifest,
                    },
                )
            )
        session.add(
            NoticeVersion(
                notice_id=notices["PPS-RETRY"].id,
                version_no=2,
                file_sha256="d" * 64,
                document_complete=False,
                extraction_status="REVIEW",
                extraction_confidence=0.0,
                source_payload={
                    "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                    "source_kind": PPS_ATTACHMENT_SOURCE,
                    "attachment_id": pdf_retry["attachment_id"],
                    "manifest_sha256": manifest_sha,
                    "current_manifest_sha256": hashlib.sha256(
                        json.dumps(
                            [pdf_retry],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "prompt_version": PROMPT_VERSION,
                    "processing_version": PPS_PROCESSING_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "status": "REVIEW",
                    "error_code": "UNVERIFIED_QUOTE",
                },
            )
        )
        unsupported_manifest_sha = hashlib.sha256(
            json.dumps(
                unsupported_terminal,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        unsupported_current_manifest_sha = hashlib.sha256(
            json.dumps(
                [unsupported_terminal],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        session.add(
            NoticeVersion(
                notice_id=notices["PPS-UNSUPPORTED"].id,
                version_no=2,
                file_sha256="e" * 64,
                document_complete=False,
                extraction_status="REVIEW",
                extraction_confidence=0.0,
                source_payload={
                    "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                    "source_kind": PPS_ATTACHMENT_SOURCE,
                    "attachment_id": unsupported_terminal["attachment_id"],
                    "source_label": unsupported_terminal["file_name"],
                    "manifest_sha256": unsupported_manifest_sha,
                    "current_manifest_sha256": unsupported_current_manifest_sha,
                    "document_sha256": "e" * 64,
                    "prompt_version": PROMPT_VERSION,
                    "processing_version": PPS_PROCESSING_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "status": "REVIEW",
                    "error_code": "UNSUPPORTED_ATTACHMENT_TYPE",
                },
            )
        )
        session.commit()

    response = client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "as_of": "2026-08-17T09:00:00+09:00"},
    )
    assert response.status_code == 200
    queue = response.json()["analysis_queue"]
    assert queue["notice_keys"] == ["PPS-NEVER", "PPS-RETRY"]
    assert queue["never_attempted_notice_keys"] == ["PPS-NEVER"]
    assert queue["retryable_notice_keys"] == ["PPS-RETRY"]
    assert (
        queue["never_attempted_notice_keys"] + queue["retryable_notice_keys"]
        == queue["notice_keys"]
    )
    assert queue["never_attempted_total"] == 1
    assert queue["retryable_total"] == 1
    assert queue["deferred_terminal_total"] == 1


def test_failed_snapshot_remains_retryable_and_planner_enforces_cooldown(
    client: TestClient,
) -> None:
    notice_key = "PPS-FAILED-SNAPSHOT-RETRY"
    _create_notice(
        client,
        notice_key=notice_key,
        published_at="2026-08-18T08:00:00+09:00",
        deadline=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    )
    attachment = {
        "attachment_id": "PPS-ATT-eeeeeeeeeeeeeeeeeeeeeeee",
        "file_name": "제안요청서.pdf",
        "media_type": "application/pdf",
        "url": (
            "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
            "?bidPbancNo=R26BKFAILED&fileSeq=1"
        ),
        "slot": 1,
    }
    manifest_sha = hashlib.sha256(
        json.dumps(
            attachment,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    with client.app.state.session_factory() as session:
        notice = session.query(Notice).filter_by(notice_key=notice_key).one()
        session.add_all(
            [
                NoticeVersion(
                    notice_id=notice.id,
                    version_no=1,
                    file_sha256="a" * 64,
                    document_complete=False,
                    extraction_status="METADATA",
                    extraction_confidence=1.0,
                    source_payload={
                        "kind": PPS_METADATA_KIND,
                        "schema_version": PPS_METADATA_SCHEMA,
                        "attachment_manifest": [attachment],
                    },
                ),
                NoticeVersion(
                    notice_id=notice.id,
                    version_no=2,
                    file_sha256="b" * 64,
                    document_complete=False,
                    extraction_status="REVIEW",
                    extraction_confidence=0.0,
                    source_payload={
                        "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                        "source_kind": PPS_ATTACHMENT_SOURCE,
                        "attachment_id": attachment["attachment_id"],
                        "source_label": attachment["file_name"],
                        "manifest_sha256": manifest_sha,
                        "current_manifest_sha256": hashlib.sha256(
                            json.dumps(
                                [attachment],
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                        "document_sha256": "b" * 64,
                        "prompt_version": PROMPT_VERSION,
                        "processing_version": PPS_PROCESSING_VERSION,
                        "schema_version": SCHEMA_VERSION,
                        "status": "REVIEW",
                        "review_code": "R07",
                        "error_code": "INTERNAL_ENRICHMENT_ERROR",
                        "result": None,
                    },
                ),
            ]
        )
        session.commit()

    analysed = client.post(
        "/api/v1/notices/analysis/batch",
        json={
            "notice_keys": [notice_key],
            "dry_run": False,
            "enrich_missing": False,
        },
    )
    assert analysed.status_code == 200, analysed.text
    assert analysed.json()["results"][0]["document_status"] == "PARTIAL"
    assert analysed.json()["results"][0]["analysis_reason_code"] == "OPENAI_REVIEW"
    with client.app.state.session_factory() as session:
        notice = session.query(Notice).filter_by(notice_key=notice_key).one()
        runs = session.query(AnalysisRun).filter_by(notice_id=notice.id).all()
        assert len(runs) == 1
        assert runs[0].status == "PARTIAL"

    briefing = client.get(
        "/api/v1/operations/daily-briefing",
        params={
            "days": 7,
            "limit": 50,
            "as_of": "2026-08-19T12:00:00+09:00",
        },
    )
    assert briefing.status_code == 200, briefing.text
    briefing_body = briefing.json()
    item = next(
        (
            row
            for row in briefing_body["notices"]
            if row["notice_key"] == notice_key
        ),
        None,
    )
    assert item is not None, briefing_body
    assert item["analysis_snapshot"]["status"] == "PARTIAL"
    assert item["analysis_coverage"]["reason_code"] == "OPENAI_REVIEW"
    queue = briefing_body["analysis_queue"]
    assert queue["retryable_notice_keys"] == [notice_key]
    assert queue["retryable_total"] == 1

    retry_payload = {
        "queue_name": "DAILY",
        "notice_keys": [notice_key],
        "retry_notice_keys": [notice_key],
        "retry_epoch": "2026-08-19",
        "dry_run": True,
        "include_retryable": True,
        "chunk_size": 1,
        "execution_limit": 1,
        "retry_cooldown_hours": 24,
    }
    too_soon = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=retry_payload,
    )
    assert too_soon.status_code == 200, too_soon.text
    assert too_soon.json()["planned"] == 0
    assert too_soon.json()["offered"] == 0
    assert "RETRY_KEYS_NOT_ELIGIBLE:1" in too_soon.json()["warnings"]

    cooled_at = datetime.now(timezone.utc) - timedelta(hours=25)
    with client.app.state.session_factory() as session:
        notice = session.query(Notice).filter_by(notice_key=notice_key).one()
        for version in notice.versions:
            version.created_at = cooled_at
        session.commit()

    offered = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=retry_payload,
    )
    assert offered.status_code == 200, offered.text
    offered_body = offered.json()
    assert offered_body["planned"] == 1
    assert offered_body["offered"] == 1
    assert offered_body["notice_keys"] == [notice_key]


def test_completed_analyzed_snapshot_stays_out_of_retry_queue(
    client: TestClient,
) -> None:
    notice_key = "MANUAL-INCHON-2025-17"
    with client.app.state.session_factory() as session:
        imported = import_public_notice_seed(session)
        assert imported.requirement_count == 23

    analysed = client.post(
        "/api/v1/notices/analysis/batch",
        json={"notice_keys": [notice_key], "dry_run": False},
    )
    assert analysed.status_code == 200, analysed.text
    assert analysed.json()["results"][0]["analysis_reason_code"] == "ANALYZED"
    with client.app.state.session_factory() as session:
        notice = session.query(Notice).filter_by(notice_key=notice_key).one()
        run = session.query(AnalysisRun).filter_by(notice_id=notice.id).one()
        # This assertion targets the daily queue's terminal snapshot boundary,
        # independent of the historical fixture's partial-document warning.
        run.status = "COMPLETED"
        notice.published_at = datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)
        notice.deadline = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
        notice.status = "OPEN"
        session.commit()

    briefing = client.get(
        "/api/v1/operations/daily-briefing",
        params={
            "days": 7,
            "limit": 50,
            "as_of": "2026-08-19T12:00:00+09:00",
        },
    )
    assert briefing.status_code == 200, briefing.text
    body = briefing.json()
    item = next(row for row in body["notices"] if row["notice_key"] == notice_key)
    assert item["analysis_snapshot"]["status"] == "COMPLETED"
    assert item["analysis_snapshot"]["version_current"] is True
    assert item["analysis_coverage"]["reason_code"] == "ANALYZED"
    assert notice_key not in body["analysis_queue"]["notice_keys"]
    assert notice_key not in body["analysis_queue"]["retryable_notice_keys"]
    with client.app.state.session_factory() as session:
        assert notice_key not in _select_backfill_notice_keys(
            session,
            AnalysisBackfillPlanRequest(include_retryable=True),
            now=datetime(2026, 8, 19, 3, 0, tzinfo=timezone.utc),
        )


def _seed_stale_analysis_snapshot(
    client: TestClient,
    *,
    status: str = "OPEN",
    deadline: datetime | None = None,
    quantitative_engine_only: bool = False,
    public_criteria_contract_only: bool = False,
) -> str:
    notice_key = "MANUAL-INCHON-2025-17"
    with client.app.state.session_factory() as session:
        imported = import_public_notice_seed(session)
        assert imported.requirement_count == 23

    analysed = client.post(
        "/api/v1/notices/analysis/batch",
        json={"notice_keys": [notice_key], "dry_run": False},
    )
    assert analysed.status_code == 200, analysed.text
    with client.app.state.session_factory() as session:
        notice = session.query(Notice).filter_by(notice_key=notice_key).one()
        run = session.query(AnalysisRun).filter_by(notice_id=notice.id).one()
        stale_basis = dict(run.basis_versions or {})
        if quantitative_engine_only:
            stale_basis["quantitative_engine"] = "pai-loop-quantitative-engine-1.7.0"
        elif public_criteria_contract_only:
            stale_basis["pipeline"] = "analysis-pipeline-0.6.2"
            stale_basis["snapshot"] = "analysis-snapshot-0.2.0"
        else:
            # This is the exact public-profile/policy predecessor deployed
            # before the 2026-09-03 company-fact refresh. Keep every other
            # engine version current so selection proves the policy bump alone
            # enters the deterministic refresh queue.
            stale_basis["requirement_policy"] = (
                "pai-loop-requirement-policy-2026.08.27-v4"
            )
        run.basis_versions = stale_basis
        run.status = "COMPLETED"
        notice.published_at = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        notice.deadline = deadline or datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
        notice.status = status
        session.commit()
    return notice_key


def test_public_criteria_contract_stale_open_snapshot_enters_refresh_queues(
    client: TestClient,
) -> None:
    notice_key = _seed_stale_analysis_snapshot(
        client,
        public_criteria_contract_only=True,
    )

    briefing = client.get(
        "/api/v1/operations/daily-briefing",
        params={
            "days": 7,
            "limit": 50,
            "as_of": "2026-08-27T16:30:00+09:00",
        },
    )
    assert briefing.status_code == 200, briefing.text
    body = briefing.json()
    item = next(row for row in body["notices"] if row["notice_key"] == notice_key)
    assert item["analysis_snapshot"]["version_current"] is False
    assert item["analysis_snapshot"]["pipeline_version"] == (
        "analysis-pipeline-0.6.2"
    )
    assert body["analysis_queue"]["retryable_notice_keys"] == [notice_key]
    with client.app.state.session_factory() as session:
        assert _select_backfill_notice_keys(
            session,
            AnalysisBackfillPlanRequest(include_retryable=False),
            now=datetime(2026, 8, 27, 7, 30, tzinfo=timezone.utc),
        ) == [notice_key]


def test_never_attempted_backfill_leads_with_the_furthest_deadline(
    client: TestClient,
) -> None:
    """A notice a bidder can still act on is analysed before one closing sooner.

    A bounded run started for review then samples the useful end of the queue
    instead of whatever arrived last, and observation time stays the tiebreak so
    same-deadline notices keep the newest-first order.
    """

    for notice_key, published_at, deadline in [
        # Newest arrival, but closes first.
        ("DEADLINE-SOON-NEWEST", "2026-08-20T09:00:00+09:00", "2026-08-28T18:00:00+09:00"),
        ("DEADLINE-FAR-OLDEST", "2026-08-10T09:00:00+09:00", "2026-09-30T18:00:00+09:00"),
        ("DEADLINE-MID", "2026-08-15T09:00:00+09:00", "2026-09-10T18:00:00+09:00"),
        # Same deadline as the leader: the newer observation wins the tie.
        ("DEADLINE-FAR-NEWER", "2026-08-18T09:00:00+09:00", "2026-09-30T18:00:00+09:00"),
    ]:
        _create_notice(
            client,
            notice_key=notice_key,
            published_at=published_at,
            deadline=deadline,
        )

    with client.app.state.session_factory() as session:
        selected = _select_backfill_notice_keys(
            session,
            AnalysisBackfillPlanRequest(include_retryable=False),
            now=datetime(2026, 8, 27, 7, 30, tzinfo=timezone.utc),
        )

    assert selected == [
        "DEADLINE-FAR-NEWER",
        "DEADLINE-FAR-OLDEST",
        "DEADLINE-MID",
        "DEADLINE-SOON-NEWEST",
    ]


def test_quantitative_engine_stale_open_snapshot_enters_daily_and_backfill(
    client: TestClient,
) -> None:
    notice_key = _seed_stale_analysis_snapshot(
        client,
        quantitative_engine_only=True,
    )

    briefing = client.get(
        "/api/v1/operations/daily-briefing",
        params={
            "days": 7,
            "limit": 50,
            "as_of": "2026-08-27T16:30:00+09:00",
        },
    )
    assert briefing.status_code == 200, briefing.text
    body = briefing.json()
    item = next(row for row in body["notices"] if row["notice_key"] == notice_key)
    snapshot = item["analysis_snapshot"]
    assert snapshot["version_current"] is False
    assert snapshot["pipeline_version"] == daily_operations.PIPELINE_VERSION
    assert snapshot["policy_version"] == daily_operations.POLICY_VERSION
    assert snapshot["quantitative_engine_version"] == (
        "pai-loop-quantitative-engine-1.7.0"
    )
    assert body["analysis_queue"]["retryable_notice_keys"] == [notice_key]
    with client.app.state.session_factory() as session:
        assert _select_backfill_notice_keys(
            session,
            AnalysisBackfillPlanRequest(include_retryable=False),
            now=datetime(2026, 8, 27, 7, 30, tzinfo=timezone.utc),
        ) == [notice_key]


def test_requirement_policy_v4_open_snapshot_enters_daily_and_backfill_once(
    client: TestClient,
) -> None:
    notice_key = _seed_stale_analysis_snapshot(client)

    briefing = client.get(
        "/api/v1/operations/daily-briefing",
        params={
            "days": 7,
            "limit": 50,
            "as_of": "2026-08-27T16:30:00+09:00",
        },
    )
    assert briefing.status_code == 200, briefing.text
    body = briefing.json()
    item = next(row for row in body["notices"] if row["notice_key"] == notice_key)
    assert item["analysis_snapshot"]["version_current"] is False
    assert item["analysis_snapshot"]["pipeline_version"] == (
        daily_operations.PIPELINE_VERSION
    )
    assert item["analysis_snapshot"]["policy_version"] == (
        "pai-loop-requirement-policy-2026.08.27-v4"
    )
    assert body["analysis_queue"]["retryable_notice_keys"] == [notice_key]
    with client.app.state.session_factory() as session:
        assert _select_backfill_notice_keys(
            session,
            AnalysisBackfillPlanRequest(include_retryable=False),
            now=datetime(2026, 8, 27, 7, 30, tzinfo=timezone.utc),
        ) == [notice_key]

    payload = {
        "queue_name": "DAILY",
        "notice_keys": [notice_key],
        "retry_notice_keys": [notice_key],
        "retry_epoch": "2026-08-27",
        "request_token": "w10:version-refresh-regression",
        "dry_run": True,
        "chunk_size": 1,
        "execution_limit": 5,
        "include_retryable": False,
        "retry_cooldown_hours": 24,
    }
    first = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=payload,
    )
    assert first.status_code == 200, first.text
    planned = first.json()
    assert planned["planned"] == 1
    assert planned["offered"] == 1
    assert planned["chunks"] == [[notice_key]]
    assert planned["offered"] <= payload["execution_limit"]
    assert all(len(chunk) == 1 for chunk in planned["chunks"])

    exact_replay = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=payload,
    )
    assert exact_replay.status_code == 200, exact_replay.text
    replayed = exact_replay.json()
    assert replayed["job_id"] == planned["job_id"]
    assert replayed["segment_id"] == planned["segment_id"]
    assert replayed["chunks"] == planned["chunks"]


def test_failed_version_refresh_observes_backfill_retry_cooldown(
    client: TestClient,
) -> None:
    notice_key = _seed_stale_analysis_snapshot(client)
    with client.app.state.session_factory() as session:
        notice = session.query(Notice).filter_by(notice_key=notice_key).one()
        stale_run = session.query(AnalysisRun).filter_by(notice_id=notice.id).one()
        attempt_at = max(
            datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc),
            stale_run.generated_at.replace(tzinfo=timezone.utc)
            if stale_run.generated_at.tzinfo is None
            else stale_run.generated_at.astimezone(timezone.utc),
        ) + timedelta(minutes=1)
        parent = IngestionJob(
            source="ANALYSIS_BACKFILL",
            mode="LIVE",
            status="FAILED",
            window_json={"scope": "OPEN_NOT_SELECTED"},
            request_json={"queue_name": "BACKFILL"},
            matched=1,
            notice_keys=[notice_key],
            warnings=["SYNTHETIC_REFRESH_FAILURE"],
            completed_at=attempt_at,
            created_at=attempt_at - timedelta(seconds=1),
        )
        session.add(parent)
        session.flush()
        session.add(
            IngestionJob(
                source="ANALYSIS",
                mode="LIVE",
                status="FAILED",
                window_json={"scope": "NOTICE_KEYS"},
                request_json={
                    "parent_job_id": parent.id,
                    "chunk_index": 0,
                    "work_generations": {notice_key: 0},
                },
                matched=1,
                notice_keys=[notice_key],
                warnings=["SYNTHETIC_REFRESH_FAILURE"],
                completed_at=attempt_at,
                created_at=attempt_at,
            )
        )
        session.commit()

    payload = AnalysisBackfillPlanRequest(
        include_retryable=True,
        retry_cooldown_hours=24,
    )
    with client.app.state.session_factory() as session:
        assert _select_backfill_notice_keys(
            session,
            payload,
            now=attempt_at + timedelta(hours=1),
        ) == []
        assert _select_backfill_notice_keys(
            session,
            AnalysisBackfillPlanRequest(
                include_retryable=False,
                retry_cooldown_hours=24,
            ),
            now=attempt_at + timedelta(hours=25),
        ) == []
        assert _select_backfill_notice_keys(
            session,
            payload,
            now=attempt_at + timedelta(hours=25),
        ) == [notice_key]


@pytest.mark.parametrize(
    ("status", "deadline"),
    [
        ("CLOSED", datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)),
        ("OPEN", datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)),
    ],
)
def test_version_stale_closed_or_expired_snapshot_is_not_queued(
    client: TestClient,
    status: str,
    deadline: datetime,
) -> None:
    notice_key = _seed_stale_analysis_snapshot(
        client,
        status=status,
        deadline=deadline,
    )

    briefing = client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "as_of": "2026-08-27T16:30:00+09:00"},
    )
    assert briefing.status_code == 200, briefing.text
    assert notice_key not in briefing.json()["analysis_queue"]["notice_keys"]

    backfill = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "BACKFILL",
            "dry_run": True,
            "chunk_size": 1,
            "execution_limit": 5,
            "include_retryable": True,
            "retry_cooldown_hours": 24,
        },
    )
    assert backfill.status_code == 200, backfill.text
    assert backfill.json()["planned"] == 0
    assert backfill.json()["offered"] == 0


def test_incomplete_attachment_coverage_is_exposed_as_retryable(
    client: TestClient,
    monkeypatch,
) -> None:
    notice_key = "PPS-INCOMPLETE-COVERAGE-RETRY"
    _create_notice(
        client,
        notice_key=notice_key,
        published_at="2026-08-18T08:00:00+09:00",
    )
    monkeypatch.setattr(
        daily_operations,
        "public_analysis_reason",
        lambda *_args, **_kwargs: SimpleNamespace(
            state="REVIEW",
            reason_code="ATTACHMENT_COVERAGE_INCOMPLETE",
            reason="one manifest attachment is not covered",
            attempted=True,
        ),
    )

    response = client.get(
        "/api/v1/operations/daily-briefing",
        params={
            "days": 7,
            "limit": 50,
            "as_of": "2026-08-19T12:00:00+09:00",
        },
    )

    assert response.status_code == 200, response.text
    queue = response.json()["analysis_queue"]
    assert notice_key in queue["notice_keys"]
    assert notice_key in queue["retryable_notice_keys"]


def test_daily_briefing_exposes_competition_risk_without_mixing_eligibility(
    client: TestClient,
) -> None:
    _create_notice(
        client,
        notice_key="DAILY-RISK",
        published_at="2026-08-16T08:30:00+09:00",
        title="SYN AI 교육 용역",
        agency="SYN 공고기관",
    )
    winners = ["합성 수행사 A", "합성 수행사 B", "합성 수행사 A", "합성 수행사 C", "합성 수행사 B", "합성 수행사 A"]
    participants = [2, 3, 2, 4, 3, 2]
    with client.app.state.session_factory() as session:
        notice = session.query(Notice).filter_by(notice_key="DAILY-RISK").one()
        notice.versions.append(NoticeVersion(
            version_no=1, file_sha256="f" * 64,
            source_payload={
                "kind": PPS_METADATA_KIND,
                "notice_identity": {"bid_notice_no": notice.bid_notice_no, "revision_no": notice.revision_no},
                "notice_metadata": {"demand_agency_name": "SYN 수요기관"},
                "attachment_manifest": [],
            },
        ))
        for index, (winner, participant_count) in enumerate(zip(winners, participants), start=1):
            session.add(
                AwardHistoryItem(
                    target_notice_id=notice.id,
                    external_identity=f"DAILY-RISK-{index}",
                    bid_notice_no=f"SYN-DAILY-{index}",
                    title=f"SYN AI 교육 {index}",
                    agency="SYN 수요기관",
                    winner_name=winner,
                    participant_count=participant_count,
                    award_amount=90_000_000 + index,
                    award_rate=90 + index / 10,
                    awarded_at=datetime(2025, index, 1, tzinfo=timezone.utc),
                    similarity_score=80,
                    source="SYNTHETIC_TEST_ONLY",
                )
            )
        session.add(AwardHistoryItem(
            target_notice_id=notice.id, external_identity="SYN-OTHER-AGENCY", bid_notice_no="SYN-OTHER",
            title="SYN AI 교육", agency="SYN 다른기관", winner_name="SYN 제외업체",
            participant_count=99, award_rate=1, similarity_score=100,
            awarded_at=datetime(2025, 1, 1, tzinfo=timezone.utc), source="SYNTHETIC_TEST_ONLY",
        ))
        session.commit()

    response = client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "as_of": "2026-08-17T09:00:00+09:00"},
    )
    assert response.status_code == 200
    item = response.json()["notices"][0]
    assert item["fit"]["risk_band"] == "UNKNOWN"
    assert item["award_snapshot"]["observations"] == 6
    assert item["pricing_intelligence"]["record_count"] == 6
    assert item["competition_risk"]["score"] == 62.83
    assert item["competition_risk"]["band"] == "HIGH"
    assert item["pricing_intelligence"]["competition_risk"] == item["competition_risk"]


def test_retention_defaults_to_preview_and_preserves_canonical_records(
    client: TestClient,
) -> None:
    _create_notice(
        client,
        notice_key="RETENTION-NOTICE",
        published_at="2026-08-16T08:30:00+09:00",
    )
    with client.app.state.session_factory() as session:
        notice = session.query(Notice).filter_by(notice_key="RETENTION-NOTICE").one()
        old = datetime.now(timezone.utc) - timedelta(days=8)
        session.add(
            IngestionJob(
                source="FIXTURE",
                mode="DRY_RUN",
                status="COMPLETED",
                window_json={"from": "2026-08-01", "to": "2026-08-01"},
                request_json={},
                notice_keys=[],
                warnings=[],
                completed_at=old,
                created_at=old,
            )
        )
        session.add(
            MockNotification(
                notice_id=notice.id,
                status="MOCK_RECORDED",
                correlation_id="old-daily-mock",
                card={"type": "AdaptiveCard", "body": []},
                created_at=old,
            )
        )
        session.commit()

    preview = client.post(
        "/api/v1/operations/retention",
        json={"retention_days": 7},
    )
    assert preview.status_code == 200
    assert preview.json()["dry_run"] is True
    assert preview.json()["eligible"] == {
        "ingestion_jobs": 1,
        "mock_notifications": 1,
    }
    assert preview.json()["deleted"] == {
        "ingestion_jobs": 0,
        "mock_notifications": 0,
    }

    applied = client.post(
        "/api/v1/operations/retention",
        json={"retention_days": 7, "dry_run": False},
    )
    assert applied.status_code == 200
    assert applied.json()["deleted"] == {
        "ingestion_jobs": 1,
        "mock_notifications": 1,
    }
    assert "notices" in applied.json()["preserved"]
    assert client.get("/api/v1/notices/RETENTION-NOTICE").status_code == 200


def test_retention_never_deletes_running_job(client: TestClient) -> None:
    old = datetime.now(timezone.utc) - timedelta(days=30)
    with client.app.state.session_factory() as session:
        session.add(
            IngestionJob(
                source="FIXTURE",
                mode="DRY_RUN",
                status="RUNNING",
                window_json={"from": "2026-07-01", "to": "2026-07-01"},
                request_json={},
                notice_keys=[],
                warnings=[],
                completed_at=old,
                created_at=old,
            )
        )
        session.commit()

    response = client.post(
        "/api/v1/operations/retention",
        json={"retention_days": 7, "dry_run": False},
    )
    assert response.status_code == 200
    assert response.json()["eligible"]["ingestion_jobs"] == 0
    assert response.json()["deleted"]["ingestion_jobs"] == 0
