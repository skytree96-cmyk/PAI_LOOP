from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from pai_loop import api as api_module
from pai_loop.models import BidOutcome, Evaluation, Notice, NoticeVersion, PpsNoticeAuthority, UserDecision
from pai_loop.integrations.openai_extraction import ExtractionPayload, PROMPT_VERSION, SCHEMA_VERSION
from pai_loop.pps_enrichment import PPS_ATTACHMENT_SOURCE, PPS_METADATA_SCHEMA, PPS_PROCESSING_VERSION, _digest
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction


NOW = datetime(2026, 9, 8, 14, 30, tzinfo=timezone.utc)  # 23:30 KST


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


def _notice(session, label, eligibility, *, status="OPEN", deadline=None, cancelled=False, outcome=False, complete=True, title=None):
    deadline = deadline or NOW + timedelta(days=1)
    notice = Notice(
        notice_key=f"PPS-SYN_QUEUE_{label}", bid_notice_no=f"SYN-QUEUE-{label}",
        revision_no="00", title=title or f"SYN {label}", agency="SYN agency",
        status=status, deadline=deadline,
    )
    attachment = {
        "attachment_id": "PPS-ATT-" + hashlib.sha256(label.encode()).hexdigest()[:24],
        "file_name": "SYN 제안요청서.pdf", "media_type": "application/pdf", "slot": 1,
        "url": "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do?bidPbancNo=SYN-QUEUE&fileSeq=1",
    }
    version = NoticeVersion(version_no=1, file_sha256="a" * 64,
                            source_payload={"kind": "PPS_NOTICE_METADATA", "schema_version": PPS_METADATA_SCHEMA,
                                            "attachment_manifest": [attachment] if complete else []})
    notice.versions.append(version)
    session.add(notice)
    session.flush()
    if complete:
        manifest_sha = _digest([attachment])
        record = validate_quantitative_attachment_extraction(
            ExtractionPayload(document_type="RFP", requirements=[], quantitative_tables=[],
                              quantitative_table_not_applicable=None, missing_or_unreadable=[], summary="SYN source"),
            source_text="SYN source", attachment_id=attachment["attachment_id"],
            document_sha256="b" * 64, manifest_sha256=manifest_sha,
        )
        notice.versions.append(NoticeVersion(
            version_no=2, file_sha256="b" * 64,
            source_payload={
                "kind": "OPENAI_REQUIREMENT_EXTRACTION", "source_kind": PPS_ATTACHMENT_SOURCE,
                "attachment_id": attachment["attachment_id"], "manifest_sha256": _digest(attachment),
                "current_manifest_sha256": manifest_sha, "prompt_version": PROMPT_VERSION,
                "schema_version": SCHEMA_VERSION, "processing_version": PPS_PROCESSING_VERSION,
                "status": "ACCEPTED", "quantitative_validation_record": record.model_dump(mode="json"),
                "document_processing": {"source_read_complete": True, "analysis_input_complete": True},
                "result": {"summary": "SYN source"},
            }, extraction_status="ACCEPTED", document_complete=True,
        ))
    if eligibility:
        notice.evaluations.append(Evaluation(
            notice_version_id=version.id, deadline_snapshot_at=deadline,
            eligibility=eligibility, reason_code=eligibility, readiness_score=70,
            readiness_status="GREEN", evidence_coverage=100, risk_score=20,
            risk_band="GO", ruleset_version="SYN-queue", atomic_results=[],
            explanation={}, evaluated_at=NOW - timedelta(days=1),
        ))
    if cancelled:
        session.add(PpsNoticeAuthority(
            bid_notice_no=notice.bid_notice_no, revision_no="00", event_kind="취소공고",
            disposition="CANCELLED", required_fields_complete=True,
            provider_changed_at=NOW - timedelta(hours=1), authority_sha256="b" * 64,
        ))
    if outcome:
        notice.bid_outcomes.append(BidOutcome(
            outcome_key=f"SYN-OUTCOME-{label}", status="WON", source="MANUAL", evidence_json={},
        ))
    return notice


@pytest.mark.parametrize("public_view", [False, True])
def test_dashboard_work_queues_use_explicit_qualification_and_keep_global_totals(
    client, monkeypatch, public_view,
):
    monkeypatch.setattr(api_module, "datetime", FixedDateTime)
    client.app.state.settings = replace(client.app.state.settings, public_read_only=public_view)
    with client.app.state.session_factory() as session:
        for eligibility in ("PASS", "REVIEW", "FAIL", None):
            label = eligibility or "NOT_EVALUATED"
            _notice(session, f"open-{label}", eligibility)
            _notice(session, f"ended-{label}", eligibility, status="CLOSED")
            _notice(session, f"cancelled-{label}", eligibility, status="CLOSED", cancelled=True)
        _notice(session, "recorded", "PASS", status="EXPIRED", outcome=True)
        _notice(session, "five-days-late", "PASS", deadline=datetime(2026, 9, 13, 14, 59, tzinfo=timezone.utc))
        _notice(session, "six-days-early", "REVIEW", deadline=datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc))
        _notice(session, "today-later", "PASS", deadline=NOW + timedelta(minutes=20))
        _notice(session, "today-expired", "PASS", deadline=NOW - timedelta(minutes=20))
        session.commit()

    response = client.get("/api/v1/dashboard")
    assert response.status_code == 200, response.text
    dashboard = response.json()
    assert dashboard["work_queue_counts"] == {
        "fail": 2, "review": 2, "urgent": 4, "result_missing": 3, "cancelled": 2,
        # No department decision exists here, so every qualified open notice is
        # still waiting for a person and no pipeline queue has claimed one.
        "pending_decision": 5, "in_progress": 0,
        "urgent_in_progress": 0, "result_missing_decided": 0,
    }
    assert dashboard["work_queue_denominator"] == 7
    assert dashboard["work_queue_denominator_definition"] == "OPEN_NOT_CANCELLED"
    assert dashboard["cancelled_go_notices"] == []
    assert dashboard["deadline_soon"] == dashboard["work_queue_counts"]["urgent"]
    assert dashboard["totals"]["notices"] == 17
    assert dashboard["totals"]["evaluations"] == 14
    assert dashboard["cancelled_count"] == 4
    assert dashboard["result_missing_count"] == 5
    assert dashboard["analysis_review_backlog_count"] == 3  # Includes unanalysed work.
    response = client.get("/api/v1/notices", params={"limit": 200})
    assert response.status_code == 200, response.text
    rows = response.json()
    by_key = {row["notice_key"]: row for row in rows}
    for eligibility in ("PASS", "REVIEW", "FAIL"):
        row = by_key[f"PPS-SYN_QUEUE_cancelled-{eligibility}"]
        assert row["latest_evaluation"] is None
        assert row["qualification_status"] == "NOT_EVALUATED"
        assert row["recommendation"] is None
        assert row["historical_qualification"] == {
            "eligibility": eligibility, "evaluated_at": "2026-09-07T14:30:00Z",
            "scope": "LAST_VALID_STORED_EVALUATION",
        }
        detail = client.get(f"/api/v1/notices/{row['notice_key']}").json()
        assert detail["historical_qualification"] == row["historical_qualification"]
        assert detail["latest_evaluation"] is None
    assert by_key["PPS-SYN_QUEUE_cancelled-NOT_EVALUATED"]["historical_qualification"] is None
    assert by_key["PPS-SYN_QUEUE_open-PASS"]["historical_qualification"] is None
    assert by_key["PPS-SYN_QUEUE_open-PASS"]["qualification_status"] == "PASS"
    assert by_key["PPS-SYN_QUEUE_open-NOT_EVALUATED"]["qualification_status"] == "NOT_EVALUATED"
    _assert_frontend_queue_parity(rows, dashboard)


def test_cancelled_historical_qualification_rejects_stale_material_basis(client, monkeypatch):
    monkeypatch.setattr(api_module, "datetime", FixedDateTime)
    with client.app.state.session_factory() as session:
        notice = _notice(session, "stale-history", "PASS", status="CLOSED", cancelled=True)
        notice.versions.append(NoticeVersion(
            version_no=3, file_sha256="c" * 64,
            source_payload={"kind": "PPS_NOTICE_METADATA", "attachment_manifest": []},
        ))
        session.commit()
    row = client.get("/api/v1/notices", params={"status": "ENDED"}).json()[0]
    assert row["provider_disposition"] == "CANCELLED"
    assert row["latest_evaluation"] is None
    assert row["historical_qualification"] is None
    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["cancelled_count"] == 1
    assert dashboard["work_queue_counts"]["cancelled"] == 0


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("coverage_mode", ["empty", "incomplete"])
def test_legacy_evaluation_without_current_attachment_coverage_is_not_qualification(client, monkeypatch, cancelled, coverage_mode):
    monkeypatch.setattr(api_module, "datetime", FixedDateTime)
    with client.app.state.session_factory() as session:
        notice = _notice(session, "missing-coverage", "PASS", status="CLOSED" if cancelled else "OPEN",
                         cancelled=cancelled, complete=coverage_mode != "empty")
        if coverage_mode == "incomplete":
            metadata = notice.versions[0]
            original = metadata.source_payload["attachment_manifest"][0]
            metadata.source_payload = {
                **metadata.source_payload,
                "attachment_manifest": [original, {**original, "attachment_id": "PPS-ATT-" + "f" * 24, "slot": 2}],
            }
        session.commit()
    row = client.get("/api/v1/notices").json()[0]
    assert row["qualification_status"] == "NOT_EVALUATED"
    assert row["historical_qualification"] is None
    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["totals"]["evaluations"] == 1
    assert sum(dashboard["work_queue_counts"].values()) == 0


@pytest.mark.parametrize("path", ["/fail", "/cancelled"])
def test_dashboard_queue_routes_serve_application(client, path):
    from conftest import login_department_reader
    assert 'id="entryLoginForm"' in client.get(path).text
    login_department_reader(client)
    response = client.get(path)
    assert response.status_code == 200
    assert "20260908-login-brand-v1" in response.text


def _assert_frontend_queue_parity(rows, dashboard):
    """Execute the actual normalizer and clickable-list filter on API responses."""
    source_path = Path(__file__).resolve().parents[1] / "src/pai_loop/static/app.js"
    script = r'''
const fs=require("node:fs"), vm=require("node:vm"), assert=require("node:assert/strict");
const input=JSON.parse(fs.readFileSync(0,"utf8"));
const source=fs.readFileSync(process.argv[1],"utf8");
const now=Date.parse("2026-09-08T14:30:00Z");
class FixedDate extends Date { constructor(...args){super(...(args.length?args:[now]));} static now(){return now;} }
const context=vm.createContext({Date:FixedDate,Intl,URL,URLSearchParams,console,
  document:{documentElement:{dataset:{}},getElementById(){return null;},addEventListener(){}},
  window:{location:{search:"",href:"https://example.test/"},matchMedia(){return {matches:false};}}});
const exported=`
renderNoticeList=()=>{}; renderNoticeSearchScope=()=>{};
globalThis.ui={state,els,normalizeNotice,normalizeDashboard,deriveDashboard,applyFilters,
dashboardEligibilityStatus,analysisStatusPill,analysisRecommendationPill,noticeStatusScopeForView};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/,exported+"\n})();"),context);
const u=context.ui;
for(const key of ["searchInput","eligibilityFilter","recommendationFilter","operatorDecisionFilter",
  "sortSelect","operatorDecisionFilterHelp","priorityKeywordInput","departmentSelect"])
  u.els[key]={value:["searchInput","priorityKeywordInput"].includes(key)?"":"all",options:[{}]};
u.els.departmentSelect.value="organization";
u.els.sortSelect.value="deadline";
Object.assign(u.state,{loading:false,source:"api",accessMode:"PUBLIC_READ_ONLY",noticeSearchMode:"stored"});
const notices=input.rows.map(x=>u.normalizeNotice(x));
u.state.notices=notices;
const data=u.normalizeDashboard(input.dashboard,notices), before=JSON.stringify(notices);
for(const [queue,key,field] of [["fail","fail","failCount"],["review","review","reviewCount"],
 ["urgent","urgent","urgentCount"],["result-missing","result_missing","resultMissingCount"],
 ["cancelled","cancelled","cancelledCount"],["go","go","goCount"]]) {
  u.state.currentView=queue;
  u.applyFilters();
  assert.equal(data[field],key==="go"?input.dashboard.go_count:input.dashboard.work_queue_counts[key],`${queue} card`);
  assert.equal(u.state.filteredNotices.length,data[field],`${queue} list`);
  u.els.searchInput.value="SYN";
  u.applyFilters();
  assert.equal(u.state.filteredNotices.length,data[field],`${queue} scoped search`);
  u.els.searchInput.value="";
}
for(const n of notices.filter(x=>x.noticeKey.includes("NOT_EVALUATED")))
  assert.equal(u.dashboardEligibilityStatus(n),"NOT_EVALUATED");
for(const n of notices.filter(x=>x.providerDisposition==="CANCELLED")) {
  const label=u.analysisStatusPill(n);
  assert.match(label,/취소 공고/);
  assert.match(label,/당시/);
  assert.match(u.analysisRecommendationPill(n),/추천 비활성/);
}
u.state.currentView="cancelled";u.els.eligibilityFilter.value="PASS";u.applyFilters();
assert.equal(u.state.filteredNotices.length,1);
assert.equal(u.dashboardEligibilityStatus(u.state.filteredNotices[0]),"PASS");
assert.equal(data.totalNotices,17);
assert.equal(JSON.stringify(notices),before);
assert.equal(u.noticeStatusScopeForView("fail"),"ALL");
assert.equal(u.noticeStatusScopeForView("cancelled"),"ENDED");
'''
    result = subprocess.run(
        ["node", "-e", script, str(source_path)],
        input=json.dumps({"rows": rows, "dashboard": dashboard}),
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def _decide(session, notice, choice, *, department_id="future-ai-education", revision=1):
    session.add(UserDecision(
        notice_id=notice.id, choice=choice, rationale="SYN 판단 근거",
        department_id=department_id, department_name="SYN 부서",
        department_revision=revision, created_at=NOW - timedelta(hours=2),
    ))


def test_pipeline_queues_follow_this_department_decision(client, monkeypatch):
    """The four work cards must track the department's own recorded decision.

    A notice another department judged still waits for this one, a GO notice
    leaves the waiting queue for the in-progress queue, and a cancelled GO is
    surfaced for the banner instead of staying in any active queue.
    """

    monkeypatch.setattr(api_module, "datetime", FixedDateTime)
    with client.app.state.session_factory() as session:
        # Titles carry this department's own registered keyword: the waiting
        # queue is what our keywords brought in, not every qualified notice.
        waiting = _notice(session, "pipe-waiting", "REVIEW", title="SYN 디지털교육 위탁 운영")
        other_department = _notice(
            session, "pipe-other-dept", "REVIEW", title="SYN 디지털교육 교사 연수"
        )
        _notice(session, "pipe-foreign-topic", "REVIEW", title="SYN 도로 포장 정비")
        running = _notice(session, "pipe-running", "PASS", deadline=NOW + timedelta(days=20))
        closing = _notice(session, "pipe-closing", "PASS", deadline=NOW + timedelta(days=2))
        recorded = _notice(session, "pipe-recorded", "PASS", deadline=NOW + timedelta(days=20), outcome=True)
        awaiting_result = _notice(session, "pipe-awaiting-result", "PASS", status="CLOSED")
        cancelled = _notice(session, "pipe-cancelled", "PASS", cancelled=True)
        session.flush()
        _decide(session, other_department, "GO", department_id="future-ai-capability")
        for notice in (running, closing, recorded, awaiting_result, cancelled):
            _decide(session, notice, "GO")
        session.commit()
        waiting_key = waiting.notice_key
        cancelled_key = cancelled.notice_key

    dashboard = client.get(
        "/api/v1/dashboard", params={"department_id": "future-ai-education"}
    ).json()
    queues = dashboard["work_queue_counts"]
    assert queues["pending_decision"] == 2, (waiting_key, "another department never decides for us")
    assert dashboard["pending_decision_definition"] == "DEPARTMENT_KEYWORD_AND_QUALIFIED_AND_UNDECIDED"
    assert queues["in_progress"] == 2  # running + closing; the recorded one is done.
    assert queues["urgent_in_progress"] == 1  # only the notice closing within five days.
    assert queues["result_missing_decided"] == 1
    assert [item["notice_key"] for item in dashboard["cancelled_go_notices"]] == [cancelled_key]
    assert dashboard["cancelled_go_notices"][0]["changed_at"] is not None


def test_cancelled_go_banner_stays_empty_for_other_departments(client, monkeypatch):
    monkeypatch.setattr(api_module, "datetime", FixedDateTime)
    with client.app.state.session_factory() as session:
        cancelled = _notice(session, "pipe-foreign-cancelled", "PASS", cancelled=True)
        session.flush()
        _decide(session, cancelled, "GO", department_id="future-ai-capability")
        session.commit()

    dashboard = client.get(
        "/api/v1/dashboard", params={"department_id": "future-ai-education"}
    ).json()
    assert dashboard["cancelled_go_notices"] == []
    assert dashboard["work_queue_counts"]["in_progress"] == 0
