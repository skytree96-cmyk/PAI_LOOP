from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import threading

from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop import performance_records, result_learning
from pai_loop.models import BidOutcome, Notice


def _past_notice(client: TestClient, *, key: str = "PPS-LEARNING-001") -> str:
    response = client.post(
        "/api/v1/notices",
        json={
            "notice_key": key,
            "bid_notice_no": key.removeprefix("PPS-"),
            "revision_no": "00",
            "title": "결과 학습 입력 대상 교육 용역",
            "agency": "공개 발주기관",
            "deadline": "2025-01-01T09:00:00+09:00",
            "status": "OPEN",
        },
    )
    assert response.status_code == 201, response.text
    return key


def test_manual_performance_record_is_separate_idempotent_and_editable(client: TestClient) -> None:
    public_before = client.get("/api/v1/performance", params={"limit": 1}).json()
    payload = {
        "idempotency_key": "performance-request-001",
        "record_status": "VALIDATED",
        "project_name": "공공기관 리더십 교육",
        "agency": "공개 발주기관",
        "division": "인재개발부문",
        "overview": "리더십 교육과정 운영",
        "contract_date": "2026-01-10",
        "start_date": "2026-02-01",
        "end_date": "2026-06-30",
        "contract_amount": 120_000_000,
        "vat_basis": "INCLUDED",
        "completed": True,
        "share_pct": 100,
        "certificate_status": "ISSUED",
        "evidence_reference": "실적증명서 PERF-2026-001",
        "keywords": ["리더십", "교육", "리더십"],
    }
    first = client.post("/api/v1/performance-records", json=payload)
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True
    record = first.json()["record"]
    assert record["keywords"] == ["리더십", "교육"]
    assert record["revision"] == 1

    replay = client.post("/api/v1/performance-records", json=payload)
    assert replay.status_code == 201
    assert replay.json()["created"] is False
    assert replay.json()["record"]["id"] == record["id"]

    listing = client.get("/api/v1/performance-records")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    updated = client.patch(
        f"/api/v1/performance-records/{record['id']}",
        json={
            "expected_updated_at": record["updated_at"],
            "contract_amount": 125_000_000,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["contract_amount"] == 125_000_000
    assert updated.json()["revision"] == 2

    stale = client.patch(
        f"/api/v1/performance-records/{record['id']}",
        json={
            "expected_updated_at": record["updated_at"],
            "division": "다른 부문",
        },
    )
    assert stale.status_code == 409

    public_after = client.get("/api/v1/performance", params={"limit": 1}).json()
    assert public_after == public_before
    assert "PERF-2026-001" not in str(public_after)


def test_performance_patch_compare_and_swap_rejects_one_concurrent_writer(
    client: TestClient,
    monkeypatch,
) -> None:
    created = client.post(
        "/api/v1/performance-records",
        json={
            "idempotency_key": "performance-concurrent-create",
            "project_name": "동시 수정 대상 실적",
        },
    ).json()["record"]
    barrier = threading.Barrier(2)
    original = performance_records._same_version

    def synchronised_version(actual, expected):
        matches = original(actual, expected)
        barrier.wait(timeout=5)
        return matches

    monkeypatch.setattr(performance_records, "_same_version", synchronised_version)

    def patch(amount: int):
        return client.patch(
            f"/api/v1/performance-records/{created['id']}",
            json={
                "expected_updated_at": created["updated_at"],
                "contract_amount": amount,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(patch, [10_000_000, 20_000_000]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    listing = client.get("/api/v1/performance-records").json()["records"]
    saved = next(item for item in listing if item["id"] == created["id"])
    assert saved["revision"] == 2
    assert saved["contract_amount"] in {10_000_000, 20_000_000}


def test_performance_validation_fails_closed_without_evidence(client: TestClient) -> None:
    response = client.post(
        "/api/v1/performance-records",
        json={
            "idempotency_key": "performance-request-002",
            "record_status": "VALIDATED",
            "project_name": "근거 없는 실적",
            "agency": "발주기관",
            "division": "부문",
            "contract_date": "2026-01-01",
        },
    )
    assert response.status_code == 422
    assert "근거 참조" in response.text

    whitespace_project = client.post(
        "/api/v1/performance-records",
        json={
            "idempotency_key": "performance-request-003",
            "project_name": "   ",
        },
    )
    assert whitespace_project.status_code == 422
    whitespace_idempotency = client.post(
        "/api/v1/performance-records",
        json={
            "idempotency_key": "        ",
            "project_name": "정상 사업명",
        },
    )
    assert whitespace_idempotency.status_code == 422

    missing_validated_fields = client.post(
        "/api/v1/performance-records",
        json={
            "idempotency_key": "performance-request-004",
            "record_status": "VALIDATED",
            "project_name": "필수 항목 누락 실적",
        },
    )
    assert missing_validated_fields.status_code == 422
    assert all(
        label in missing_validated_fields.text
        for label in ("발주기관", "수행부서", "계약일", "근거 참조")
    )

    invalid_dates = client.post(
        "/api/v1/performance-records",
        json={
            "idempotency_key": "performance-request-005",
            "project_name": "기간 오류 실적",
            "start_date": "2026-12-31",
            "end_date": "2026-01-01",
        },
    )
    assert invalid_dates.status_code == 422
    invalid_keyword = client.post(
        "/api/v1/performance-records",
        json={
            "idempotency_key": "performance-request-006",
            "project_name": "키워드 오류 실적",
            "keywords": ["   "],
        },
    )
    assert invalid_keyword.status_code == 422

    completed_without_end_date = client.post(
        "/api/v1/performance-records",
        json={
            "idempotency_key": "performance-request-007",
            "record_status": "VALIDATED",
            "project_name": "종료일 누락 완료 실적",
            "agency": "발주기관",
            "division": "수행부서",
            "contract_date": "2026-01-01",
            "completed": True,
            "evidence_reference": "실적증명서 REQUEST-007",
        },
    )
    assert completed_without_end_date.status_code == 422
    assert "수행 종료일" in completed_without_end_date.text


def test_performance_filters_pagination_conflicts_and_patch_validation(client: TestClient) -> None:
    records = []
    payloads = (
        {
            "idempotency_key": "performance-filter-001",
            "record_status": "VALIDATED",
            "project_name": "공공 리더십 교육",
            "agency": "기관 가",
            "division": "교육 부문",
            "contract_date": "2026-01-10",
            "evidence_reference": "실적증명서 FILTER-001",
            "keywords": ["리더십"],
        },
        {
            "idempotency_key": "performance-filter-002",
            "record_status": "DRAFT",
            "project_name": "디지털 교육 과정",
            "agency": "기관 나",
            "division": "디지털 부문",
            "overview": None,
            "evidence_reference": None,
        },
        {
            "idempotency_key": "performance-filter-003",
            "record_status": "ARCHIVED",
            "project_name": "조직진단 컨설팅",
            "agency": "기관 다",
            "division": "컨설팅 부문",
        },
    )
    for payload in payloads:
        created = client.post("/api/v1/performance-records", json=payload)
        assert created.status_code == 201, created.text
        records.append(created.json()["record"])

    keyword_filter = client.get(
        "/api/v1/performance-records",
        params={"q": "교육", "record_status": "DRAFT"},
    )
    assert keyword_filter.status_code == 200
    assert keyword_filter.json()["total"] == 1
    assert keyword_filter.json()["records"][0]["project_name"] == "디지털 교육 과정"

    page = client.get(
        "/api/v1/performance-records",
        params={"limit": 1, "offset": 1},
    )
    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert page.json()["offset"] == 1
    assert len(page.json()["records"]) == 1

    idempotency_conflict = client.post(
        "/api/v1/performance-records",
        json={**payloads[0], "project_name": "다른 사업명"},
    )
    assert idempotency_conflict.status_code == 409

    validated = records[0]
    clearing_evidence = client.patch(
        f"/api/v1/performance-records/{validated['id']}",
        json={
            "expected_updated_at": validated["updated_at"],
            "evidence_reference": None,
        },
    )
    assert clearing_evidence.status_code == 422
    clearing_project = client.patch(
        f"/api/v1/performance-records/{validated['id']}",
        json={
            "expected_updated_at": validated["updated_at"],
            "project_name": None,
        },
    )
    assert clearing_project.status_code == 422

    draft = records[1]
    cleared_optional_fields = client.patch(
        f"/api/v1/performance-records/{draft['id']}",
        json={
            "expected_updated_at": draft["updated_at"],
            "agency": None,
            "division": None,
            "overview": "   ",
            "evidence_reference": "   ",
            "keywords": None,
        },
    )
    assert cleared_optional_fields.status_code == 200, cleared_optional_fields.text
    assert cleared_optional_fields.json()["agency"] == ""
    assert cleared_optional_fields.json()["overview"] is None
    assert cleared_optional_fields.json()["keywords"] == []

    missing = client.patch(
        "/api/v1/performance-records/not-a-record",
        json={
            "expected_updated_at": "2026-01-01T00:00:00Z",
            "project_name": "찾을 수 없는 실적",
        },
    )
    assert missing.status_code == 404


def test_result_learning_create_list_update_and_stale_conflict(client: TestClient) -> None:
    notice_key = _past_notice(client)
    payload = {
        "notice_key": notice_key,
        "idempotency_key": "outcome-request-001",
        "record_status": "VALIDATED",
        "status": "LOST",
        "submitted_bid_amount": 110_000_000,
        "submitted_bid_rate": 88.3,
        "winning_bid_amount": 108_000_000,
        "winning_bid_rate": 87.1,
        "technical_score": 80,
        "price_score": 10,
        "total_score": 90,
        "rank": 2,
        "winner_name": "공개 낙찰사",
        "loss_reason": "가격 점수 차이",
        "source_reference": "PPS:R25BK-RESULT-001",
        "operator_note": "제안평가 결과 확인",
        "occurred_at": "2026-02-01T09:00:00+09:00",
    }
    first = client.post("/api/v1/result-learning", json=payload)
    assert first.status_code == 201, first.text
    outcome = first.json()["outcome"]
    assert outcome["record_status"] == "VALIDATED"
    assert outcome["revision"] == 1

    replay = client.post("/api/v1/result-learning", json=payload)
    assert replay.status_code == 201
    assert replay.json()["created"] is False
    assert replay.json()["outcome"]["id"] == outcome["id"]

    listing = client.get("/api/v1/result-learning", params={"q": "교육"})
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 1
    assert listing.json()["records"][0]["latest_outcome"]["status"] == "LOST"

    updated = client.patch(
        f"/api/v1/result-learning/{outcome['id']}",
        json={
            "expected_updated_at": outcome["updated_at"],
            "rank": 3,
            "loss_reason": "기술 및 가격 종합점수 차이",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["outcome"]["rank"] == 3
    assert updated.json()["outcome"]["revision"] == 2

    stale = client.patch(
        f"/api/v1/result-learning/{outcome['id']}",
        json={"expected_updated_at": outcome["updated_at"], "rank": 4},
    )
    assert stale.status_code == 409


def test_result_learning_patch_compare_and_swap_rejects_one_concurrent_writer(
    client: TestClient,
    monkeypatch,
) -> None:
    notice_key = _past_notice(client, key="PPS-LEARNING-CONCURRENT")
    created = client.post(
        "/api/v1/result-learning",
        json={
            "notice_key": notice_key,
            "idempotency_key": "result-concurrent-create",
            "record_status": "DRAFT",
            "status": "NO_BID",
        },
    ).json()["outcome"]
    barrier = threading.Barrier(2)
    original = result_learning._same_version

    def synchronised_version(actual, expected):
        matches = original(actual, expected)
        barrier.wait(timeout=5)
        return matches

    monkeypatch.setattr(result_learning, "_same_version", synchronised_version)

    def patch(note: str):
        return client.patch(
            f"/api/v1/result-learning/{created['id']}",
            json={
                "expected_updated_at": created["updated_at"],
                "operator_note": note,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(patch, ["동시 수정 A", "동시 수정 B"]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    listing = client.get(
        "/api/v1/result-learning",
        params={"scope": "WITH_OUTCOME", "q": "LEARNING-CONCURRENT"},
    ).json()
    saved = listing["records"][0]["latest_outcome"]
    assert saved["revision"] == 2
    assert saved["operator_note"] in {"동시 수정 A", "동시 수정 B"}


def test_result_learning_filters_pagination_and_validation_branches(client: TestClient) -> None:
    lost_key = _past_notice(client, key="PPS-LEARNING-FILTER-001")
    no_bid_key = _past_notice(client, key="PPS-LEARNING-FILTER-002")
    _past_notice(client, key="PPS-LEARNING-FILTER-003")
    future = client.post(
        "/api/v1/notices",
        json={
            "notice_key": "PPS-LEARNING-FUTURE-001",
            "bid_notice_no": "LEARNING-FUTURE-001",
            "revision_no": "00",
            "title": "아직 진행 중인 결과 학습 대상",
            "agency": "미래 발주기관",
            "deadline": "2099-01-01T09:00:00+09:00",
            "status": "OPEN",
        },
    )
    assert future.status_code == 201

    lost = client.post(
        "/api/v1/result-learning",
        json={
            "notice_key": lost_key,
            "idempotency_key": "result-filter-lost-001",
            "record_status": "VALIDATED",
            "status": "LOST",
            "loss_reason": "가격 점수 차이",
            "source_reference": "결과 공문 FILTER-001",
        },
    )
    assert lost.status_code == 201, lost.text
    no_bid = client.post(
        "/api/v1/result-learning",
        json={
            "notice_key": no_bid_key,
            "idempotency_key": "result-filter-no-bid-001",
            "record_status": "DRAFT",
            "status": "NO_BID",
        },
    )
    assert no_bid.status_code == 201, no_bid.text
    no_bid_outcome = no_bid.json()["outcome"]

    ended = client.get("/api/v1/result-learning", params={"scope": "ENDED"})
    assert ended.status_code == 200
    assert ended.json()["total"] == 3
    all_rows = client.get(
        "/api/v1/result-learning",
        params={"scope": "ALL", "limit": 1, "offset": 1},
    )
    assert all_rows.status_code == 200
    assert all_rows.json()["total"] == 4
    assert len(all_rows.json()["records"]) == 1
    with_outcome = client.get(
        "/api/v1/result-learning",
        params={"scope": "WITH_OUTCOME"},
    )
    assert with_outcome.json()["total"] == 2
    lost_only = client.get(
        "/api/v1/result-learning",
        params={"scope": "ALL", "outcome_status": "LOST"},
    )
    assert lost_only.json()["total"] == 1
    draft_only = client.get(
        "/api/v1/result-learning",
        params={"scope": "ALL", "record_status": "DRAFT"},
    )
    assert draft_only.json()["total"] == 1
    text_filter = client.get(
        "/api/v1/result-learning",
        params={"scope": "ALL", "q": "FILTER-002"},
    )
    assert text_filter.json()["total"] == 1

    expected = no_bid_outcome["updated_at"]
    invalid_updates = (
        {
            "expected_updated_at": expected,
            "record_status": "VALIDATED",
            "status": "SUBMITTED",
            "source_reference": "제출 확인",
        },
        {
            "expected_updated_at": expected,
            "record_status": "VALIDATED",
            "status": "WON",
            "source_reference": "낙찰 확인",
        },
        {
            "expected_updated_at": expected,
            "record_status": "VALIDATED",
            "status": "LOST",
            "source_reference": "실주 확인",
        },
        {
            "expected_updated_at": expected,
            "status": "NO_BID",
            "winning_bid_amount": 1,
        },
        {
            "expected_updated_at": expected,
            "status": "SUBMITTED",
            "technical_score": 80,
            "price_score": 10,
            "total_score": 95,
        },
    )
    for payload in invalid_updates:
        response = client.patch(
            f"/api/v1/result-learning/{no_bid_outcome['id']}",
            json=payload,
        )
        assert response.status_code == 422, response.text

    missing_notice = client.post(
        "/api/v1/result-learning",
        json={
            "notice_key": "PPS-NOT-STORED",
            "idempotency_key": "result-missing-notice-001",
            "status": "NO_BID",
        },
    )
    assert missing_notice.status_code == 404
    missing_outcome = client.patch(
        "/api/v1/result-learning/not-an-outcome",
        json={"expected_updated_at": "2026-01-01T00:00:00Z", "status": "NO_BID"},
    )
    assert missing_outcome.status_code == 404
    invalid_keys = (
        {"notice_key": " ", "idempotency_key": "result-invalid-notice", "status": "NO_BID"},
        {"notice_key": lost_key, "idempotency_key": "        ", "status": "NO_BID"},
        {
            "notice_key": lost_key,
            "idempotency_key": "result-invalid-basis",
            "basis_outcome_id": " ",
            "status": "NO_BID",
        },
    )
    for payload in invalid_keys:
        response = client.post("/api/v1/result-learning", json=payload)
        assert response.status_code == 422


def test_result_learning_preserves_automatic_source_as_immutable_basis(client: TestClient) -> None:
    notice_key = _past_notice(client, key="PPS-LEARNING-AUTO-001")
    provider_evidence = {
        "schema_version": "pai-loop-pps-outcome-feedback-1.0.0",
        "provider": "PPS_DATA_GO_KR_1230000",
        "provider_result_sha256": "a" * 64,
    }
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
        assert notice is not None
        automatic = BidOutcome(
            notice_id=notice.id,
            outcome_key="pps-final-award:auto-001",
            status="WON",
            winning_bid_amount=99_000_000,
            winning_bid_rate=87.25,
            rank=1,
            winner_name="한국능률협회",
            reason_code="PPS_EXACT_WINNER_MATCH",
            source="PPS_AUTO_FEEDBACK",
            source_reference="PPS:1230000:result:auto-001",
            evidence_json=provider_evidence,
            observed_at=datetime.now(timezone.utc),
        )
        session.add(automatic)
        session.commit()
        automatic_id = automatic.id
    automatic_record = next(
        item
        for item in client.get(f"/api/v1/notices/{notice_key}/outcomes").json()
        if item["id"] == automatic_id
    )
    ungrounded = client.get(
        "/api/v1/result-learning",
        params={"scope": "WITH_OUTCOME", "q": "LEARNING-AUTO-001"},
    ).json()
    assert ungrounded["records"][0]["latest_outcome"]["record_status"] == "DRAFT"

    direct_edit = client.patch(
        f"/api/v1/result-learning/{automatic_record['id']}",
        json={
            "expected_updated_at": automatic_record["updated_at"],
            "winner_name": "변경 시도",
        },
    )
    assert direct_edit.status_code == 409
    assert "검토본" in direct_edit.text

    review_payload = {
        "notice_key": notice_key,
        "idempotency_key": "automatic-review-request-001",
        "basis_outcome_id": automatic_record["id"],
        "record_status": "VALIDATED",
        "status": "WON",
        "winning_bid_amount": 99_000_000,
        "winning_bid_rate": 87.25,
        "rank": 1,
        "winner_name": "한국능률협회",
        "source_reference": "담당자 확인: 결과 공문 AUTO-001",
        "operator_note": "자동 환류 결과를 담당자가 확인함",
        "occurred_at": "2026-08-01T09:00:00+09:00",
    }
    review = client.post("/api/v1/result-learning", json=review_payload)
    assert review.status_code == 201, review.text
    reviewed = review.json()["outcome"]
    assert reviewed["source"] == "MANUAL_UI"
    assert reviewed["basis_outcome_id"] == automatic_record["id"]
    assert reviewed["basis_source"] == "PPS_AUTO_FEEDBACK"

    replay = client.post("/api/v1/result-learning", json=review_payload)
    assert replay.status_code == 201
    assert replay.json()["created"] is False
    changed_replay = client.post(
        "/api/v1/result-learning",
        json={**review_payload, "winning_bid_amount": 98_000_000},
    )
    assert changed_replay.status_code == 409
    different_basis = client.post(
        "/api/v1/result-learning",
        json={**review_payload, "basis_outcome_id": None},
    )
    assert different_basis.status_code == 409

    updated_review = client.patch(
        f"/api/v1/result-learning/{reviewed['id']}",
        json={
            "expected_updated_at": reviewed["updated_at"],
            "operator_note": None,
        },
    )
    assert updated_review.status_code == 200, updated_review.text
    assert updated_review.json()["outcome"]["operator_note"] is None
    assert updated_review.json()["outcome"]["basis_outcome_id"] == automatic_record["id"]

    other_key = _past_notice(client, key="PPS-LEARNING-AUTO-OTHER")
    wrong_basis = client.post(
        "/api/v1/result-learning",
        json={
            **review_payload,
            "notice_key": other_key,
            "idempotency_key": "automatic-review-wrong-basis",
        },
    )
    assert wrong_basis.status_code == 422

    stored = client.get(f"/api/v1/notices/{notice_key}/outcomes")
    assert stored.status_code == 200
    original = next(item for item in stored.json() if item["id"] == automatic_record["id"])
    assert original["source"] == "PPS_AUTO_FEEDBACK"
    assert original["source_reference"] == "PPS:1230000:result:auto-001"
    assert original["evidence_json"] == provider_evidence

    latest = client.get(
        "/api/v1/result-learning",
        params={"scope": "WITH_OUTCOME", "q": "LEARNING-AUTO-001"},
    )
    assert latest.status_code == 200
    assert latest.json()["records"][0]["latest_outcome"]["id"] == reviewed["id"]


def test_operator_editors_require_scoped_token_in_public_production(client: TestClient) -> None:
    token = "2468"
    client.app.state.settings = replace(
        client.app.state.settings,
        environment="production",
        public_read_only=True,
        public_manual_analysis_enabled=True,
        public_manual_analysis_token=token,
        api_key="server-only-api-key",
    )
    no_token = client.get("/api/v1/performance-records")
    assert no_token.status_code == 401
    no_result_token = client.get("/api/v1/result-learning")
    assert no_result_token.status_code == 401
    wrong_token = client.get(
        "/api/v1/performance-records",
        headers={"X-PAI-Manual-Token": "1357"},
    )
    assert wrong_token.status_code == 401
    wrong_server_key = client.get(
        "/api/v1/performance-records",
        headers={"X-PAI-LOOP-API-KEY": "wrong-server-key"},
    )
    assert wrong_server_key.status_code == 401
    server_access = client.get(
        "/api/v1/performance-records",
        headers={"X-PAI-LOOP-API-KEY": "server-only-api-key"},
    )
    assert server_access.status_code == 200

    headers = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "X-PAI-Manual-Token": token,
    }
    listing = client.get("https://testserver/api/v1/performance-records", headers=headers)
    assert listing.status_code == 200
    cross_site_listing = client.get(
        "https://testserver/api/v1/performance-records",
        headers={**headers, "Sec-Fetch-Site": "cross-site"},
    )
    assert cross_site_listing.status_code == 403
    cross_site_results = client.get(
        "https://testserver/api/v1/result-learning",
        headers={**headers, "Sec-Fetch-Site": "cross-site"},
    )
    assert cross_site_results.status_code == 403
    missing_origin = client.post(
        "https://testserver/api/v1/performance-records",
        headers={key: value for key, value in headers.items() if key != "Origin"},
        json={
            "idempotency_key": "performance-production-no-origin",
            "project_name": "출처 없는 변경",
        },
    )
    assert missing_origin.status_code == 403
    create = client.post(
        "https://testserver/api/v1/performance-records",
        headers=headers,
        json={
            "idempotency_key": "performance-production-001",
            "project_name": "운영자 입력 초안",
        },
    )
    assert create.status_code == 201, create.text
    assert token not in create.text

    cross_origin = client.post(
        "https://testserver/api/v1/result-learning",
        headers={**headers, "Origin": "https://attacker.test"},
        json={
            "notice_key": "missing",
            "idempotency_key": "result-production-001",
            "status": "NO_BID",
        },
    )
    assert cross_origin.status_code == 403


def test_operator_editor_frontend_exposes_forms_without_server_credentials() -> None:
    static = Path(__file__).parents[1] / "src" / "pai_loop" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")

    for element_id in (
        "resultLearningSection",
        "resultLearningForm",
        "resultLearningRateMode",
        "resultLearningRateBasisKind",
        "resultLearningRateBasisAmount",
        "resultLearningRateBasisReference",
        "resultLearningRateStatus",
        "resultLearningRatePolicy",
        "performanceEditorList",
        "performanceRecordForm",
    ):
        assert f'id="{element_id}"' in html
    assert 'apiRequest("/performance-records?limit=200"' in script
    assert '"/result-learning"' in script
    assert 'outcome.source === "MANUAL_UI"' in script
    assert "payload.basis_outcome_id = outcome.id" in script
    assert "자동 환류 원본은 변경하지 않고" in script
    assert 'aria-describedby="resultLearningRateStatus resultLearningRatePolicy"' in html
    assert "우리 투찰금액 ÷ 확인한 기준금액 × 100" in html
    assert "예정가격 또는 기초금액과 출처" in html
    assert "소수 넷째 자리까지 표시" in html
    assert "수기 비율은 근거 참조에 기준가격도 함께" in html
    assert "X-PAI-Manual-Token" in script
    assert "X-PAI-LOOP-API-KEY" not in script
