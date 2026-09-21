from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest

from pai_loop import api as api_module
from pai_loop.models import AnalysisRun, Notice, NoticeVersion, PpsNoticeAuthority, RecommendationSnapshot, UserDecision


NOW = datetime(2026, 9, 13, 3, 0, tzinfo=timezone.utc)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def _seed_portfolio(client):
    with client.app.state.session_factory() as session:
        notices = []
        titles = {
            0: "SYN 경영전략", 1: "SYN 경영전략", 2: "SYN 재무교육",
            3: "SYN 정책연구", 4: "SYN 경영전략 시설공사", 5: "SYN 경영전략",
            # Index 8 carries the regional routing case. Its title has to name
            # the work as well as the place: a bare place name no longer routes
            # (see test_department_ranking's region gate tests), and this test
            # is about routing staying separate from the business rank, not
            # about the gate.
            6: "SYN 경영전략", 7: "SYN 재무교육", 8: "SYN 부산 직원 교육 위탁운영",
            9: "SYN 경영전략", 30: "SYN 경영전략",
        }
        for index in range(31):
            notice = Notice(
                notice_key=f"PPS-SYN_DEPT-{index:02d}" if index == 5 else f"SYN-DEPT-{index:02d}",
                bid_notice_no=f"SYN-DEPT-BID-{index:02d}", revision_no="00",
                title=titles.get(index, f"SYN unrelated {index}"), agency="SYN agency",
                status="CLOSED" if index == 6 else "OPEN",
                deadline=NOW + (timedelta(days=-1) if index == 7 else timedelta(days=2, minutes=index)),
            )
            session.add(notice)
            session.flush()
            notices.append(notice)
        session.add(PpsNoticeAuthority(
            bid_notice_no=notices[5].bid_notice_no, revision_no="00", event_kind="취소공고",
            disposition="CANCELLED", required_fields_complete=True,
            provider_changed_at=NOW, authority_sha256="a" * 64,
        ))

        def decision(index, department, choice, revision=1, *, created_at=NOW):
            session.add(UserDecision(
                notice_id=notices[index].id, department_id=department,
                department_revision=revision, choice=choice, rationale="SYN recorded decision",
                created_at=created_at,
            ))

        decision(0, "management-planning", "GO")
        # Revision wins even if the earlier record has a later timestamp.
        decision(0, "management-planning", "HOLD", 2, created_at=NOW - timedelta(hours=1))
        decision(0, "finance-support", "GO")
        decision(1, "management-planning", "GO")
        decision(1, "management-planning", "CONDITIONAL_GO", 2)
        decision(1, "finance-support", "NO_GO")
        decision(2, "finance-support", "GO")
        decision(2, "management-planning", "NO_GO")
        for index in (5, 6, 9, 30):
            decision(index, "management-planning", "GO")
        decision(9, "finance-support", "GO")
        # Unowned/legacy decisions must not be attributed to a department.
        decision(10, None, "GO", None)
        session.commit()


def test_department_counts_use_complete_store_and_latest_decision_per_department(client, monkeypatch):
    monkeypatch.setattr(api_module, "datetime", FixedDateTime)
    _seed_portfolio(client)
    response = client.get("/api/v1/dashboard", params={"department_id": "management-planning"})
    assert response.status_code == 200, response.text
    dashboard = response.json()
    stats = dashboard["department_statistics"]
    assert dashboard["totals"]["notices"] == stats["total_notice_count"] == 31
    assert len(dashboard["recent_notices"]) == 10
    assert stats["recommended_count"] == 4
    assert stats["selected_count"] == 5
    assert stats["selected_recommended_count"] == 3
    assert stats["recommended_ratio"] == pytest.approx(4 / 31)
    assert stats["selected_ratio"] == pytest.approx(5 / 31)
    assert stats["selection_rate"] == pytest.approx(3 / 4)
    assert stats["recommendation_scope"] == "OPEN_NOT_CANCELLED"
    assert stats["selection_scope"] == "ALL_STORED_NOTICES"
    assert stats["recommended_definition"] == "OPEN_KEYWORD_TOP_OR_REGION_ROUTING"
    assert stats["selected_definition"] == "LATEST_DEPARTMENT_GO_OR_CONDITIONAL_GO"
    # Independent discovery and human selection counts require no qualification.
    assert dashboard["totals"]["evaluations"] == 0
    assert dashboard["go_count"] == 0

    finance = client.get("/api/v1/dashboard", params={"department_id": "finance-support"}).json()
    finance_stats = finance["department_statistics"]
    assert finance_stats["recommended_count"] == 1
    assert finance_stats["selected_count"] == 3
    assert finance_stats["selected_recommended_count"] == 1
    assert finance_stats["selection_rate"] == 1
    # Department selection does not narrow the global queues or denominator.
    assert finance["totals"] == dashboard["totals"]
    assert finance["work_queue_denominator"] == dashboard["work_queue_denominator"]
    global_queues = ("fail", "review", "urgent", "result_missing", "cancelled")
    assert {key: finance["work_queue_counts"][key] for key in global_queues} == {
        key: dashboard["work_queue_counts"][key] for key in global_queues
    }
    # The work pipeline is deliberately department-scoped: a decision recorded
    # by this department is work only this department can still act on.
    assert finance["work_queue_counts"]["result_missing_decided"] == 0
    assert dashboard["work_queue_counts"]["result_missing_decided"] == 1
    alias = client.get("/api/v1/dashboard", params={"department_id": "경영기획팀"}).json()
    assert alias["department_statistics"] == stats


def test_organization_deduplicates_selections_and_regional_routing_stays_separate(client, monkeypatch):
    monkeypatch.setattr(api_module, "datetime", FixedDateTime)
    _seed_portfolio(client)
    organization = client.get("/api/v1/dashboard").json()["department_statistics"]
    assert organization["department_id"] == "organization"
    assert organization["selected_count"] == 7
    assert organization["selected_recommended_count"] == 5
    region = client.get("/api/v1/dashboard", params={"department_id": "region-busan-gyeongnam"}).json()["department_statistics"]
    assert region["recommended_count"] == 1
    assert region["selected_count"] == 0
    assert region["selection_rate"] == 0
    # Compare all ranked pages, not just the dashboard's ten recent notices.
    rows = []
    for offset in range(0, 31, 7):
        rows.extend(client.get("/api/v1/notices", params={"department_id": "organization", "limit": 7, "offset": offset}).json())
    expected = sum(
        row["status"] == "OPEN" and row["provider_disposition"] != "CANCELLED"
        and bool(row["top_department_rankings"] or row["region_routing"])
        for row in rows
    )
    assert organization["recommended_count"] == expected


def test_empty_and_invalid_department_statistics_do_not_fabricate_rates(client):
    stats = client.get("/api/v1/dashboard", params={"department_id": "management-planning"}).json()["department_statistics"]
    assert stats["total_notice_count"] == stats["recommended_count"] == stats["selected_count"] == 0
    assert stats["recommended_ratio"] is stats["selected_ratio"] is stats["selection_rate"] is None
    assert stats["selection_available"] is True
    invalid = client.get("/api/v1/dashboard", params={"department_id": "SYN-unknown-department"})
    assert invalid.status_code == 422


def test_cancelled_authority_excludes_still_open_stored_system_go(client, monkeypatch):
    monkeypatch.setattr(api_module, "datetime", FixedDateTime)
    with client.app.state.session_factory() as session:
        for index, mode in enumerate(("active", "cancelled", "stale")):
            notice = Notice(
                notice_key=f"PPS-SYN_DEPT-GO-{index}", bid_notice_no=f"SYN-DEPT-GO-{index}",
                revision_no="00", title="SYN 경영전략", agency="SYN agency",
                status="OPEN", deadline=NOW + timedelta(days=2),
            )
            session.add(notice)
            session.flush()
            version = NoticeVersion(notice_id=notice.id, version_no=1, file_sha256="b" * 64,
                                    source_payload={"kind": "SYN_SOURCE"})
            session.add(version)
            session.flush()
            run = AnalysisRun(
                notice_id=notice.id, notice_version_id=version.id, input_sha256="c" * 64,
                idempotency_key=f"SYN-DEPT-GO-{index}", generated_at=NOW, status="COMPLETED",
            )
            run.recommendations.append(RecommendationSnapshot(recommendation_key="bid:system", rank=0, recommendation="GO"))
            session.add(run)
            if mode == "cancelled":
                session.add(PpsNoticeAuthority(
                    bid_notice_no=notice.bid_notice_no, revision_no="00", event_kind="취소공고",
                    disposition="CANCELLED", required_fields_complete=True,
                    provider_changed_at=NOW, authority_sha256="d" * 64,
                ))
            elif mode == "stale":
                session.add(AnalysisRun(
                    notice_id=notice.id, notice_version_id=version.id, input_sha256="e" * 64,
                    idempotency_key="SYN-DEPT-GO-newer-incomplete", generated_at=NOW + timedelta(minutes=1),
                    status="FAILED",
                ))
        session.commit()
    response = client.get("/api/v1/dashboard")
    assert response.status_code == 200, response.text
    dashboard = response.json()
    assert dashboard["go_count"] == 1
    assert dashboard["recommendation_counts"] == {"GO": 1, "HOLD": 0, "NO_GO": 0}
    assert dashboard["department_statistics"]["recommended_count"] == 2
    rows = client.get("/api/v1/notices").json()
    assert sum(row["recommendation"] == "GO" for row in rows) == dashboard["go_count"]
    _assert_go_queue_parity(rows, dashboard)


def _assert_go_queue_parity(rows, dashboard):
    """Exercise the actual browser queue against immutable API recommendations."""

    source = Path(__file__).resolve().parents[1] / "src/pai_loop/static/app.js"
    script = r'''
const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
const input=JSON.parse(fs.readFileSync(0,"utf8"));
const now=Date.parse("2026-09-13T03:00:00Z");
class FixedDate extends Date { constructor(...args){super(...(args.length?args:[now]));} static now(){return now;} }
const context=vm.createContext({Date:FixedDate,Intl,URL,URLSearchParams,console,
 document:{documentElement:{dataset:{}},getElementById(){return null;},addEventListener(){}},
 window:{location:{search:"",href:"https://example.test/"},matchMedia(){return {matches:false};}}});
const exported=`renderNoticeList=()=>{};renderNoticeSearchScope=()=>{};
 globalThis.ui={state,els,normalizeNotice,normalizeDashboard,applyFilters};`;
vm.runInContext(fs.readFileSync(process.argv[1],"utf8").replace(/\}\)\(\);\s*$/,exported+"\n})();"),context);
const u=context.ui;
for(const key of ["searchInput","eligibilityFilter","recommendationFilter","operatorDecisionFilter",
 "sortSelect","operatorDecisionFilterHelp","priorityKeywordInput","departmentSelect"])
 u.els[key]={value:["searchInput","priorityKeywordInput"].includes(key)?"":"all",options:[{}]};
u.els.departmentSelect.value="organization";u.els.sortSelect.value="deadline";
Object.assign(u.state,{loading:false,source:"api",accessMode:"PUBLIC_READ_ONLY",noticeSearchMode:"stored",currentView:"go"});
u.state.notices=input.rows.map(row=>u.normalizeNotice(row));
const dashboard=u.normalizeDashboard(input.dashboard,u.state.notices);
u.applyFilters();
assert.equal(dashboard.goCount,input.dashboard.go_count);
assert.equal(u.state.filteredNotices.length,dashboard.goCount);
assert.ok(u.state.filteredNotices.every(row=>row.raw.recommendation==="GO" && row.providerDisposition!=="CANCELLED"));
'''
    result = subprocess.run(["node", "-e", script, str(source)],
                            input=json.dumps({"rows": rows, "dashboard": dashboard}),
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
