from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pai_loop.models import Evaluation, Notice, NoticeVersion, PpsNoticeAuthority, UserDecision


NOW = datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc)


def _notice(session, label, title, *, cancelled=False, ended=False, evaluated=False):
    notice = Notice(
        notice_key=f"PPS-SYN_COVER_{label}", bid_notice_no=f"SYN-COVER-{label}",
        revision_no="00", title=title, agency="SYN 기관", category="용역",
        status="CLOSED" if ended else "OPEN",
        deadline=NOW + timedelta(days=10),
    )
    version = NoticeVersion(version_no=1, file_sha256="a" * 64, source_payload={})
    notice.versions.append(version)
    session.add(notice)
    session.flush()
    if evaluated:
        notice.evaluations.append(Evaluation(
            notice_version_id=version.id, deadline_snapshot_at=notice.deadline,
            eligibility="REVIEW", reason_code="R07", readiness_score=50,
            readiness_status="GRAY", evidence_coverage=0, risk_score=None,
            risk_band="UNKNOWN", ruleset_version="SYN", atomic_results=[],
            explanation={}, evaluated_at=NOW,
        ))
    if cancelled:
        session.add(PpsNoticeAuthority(
            bid_notice_no=notice.bid_notice_no, revision_no="00", event_kind="취소공고",
            disposition="CANCELLED", required_fields_complete=True,
            provider_changed_at=NOW, authority_sha256="c" * 64,
        ))
    return notice


def test_department_coverage_counts_only_actionable_notices(client):
    """Ended and cancelled notices must not inflate a keyword's coverage."""

    with client.app.state.session_factory() as session:
        _notice(session, "live", "초중고 AI 디지털교육 위탁 운영", evaluated=True)
        _notice(session, "live2", "디지털교육 교사 연수 운영")
        _notice(session, "ended", "디지털교육 종료 사업", ended=True)
        _notice(session, "cancelled", "디지털교육 취소 사업", cancelled=True)
        session.commit()

    payload = client.get("/api/v1/dashboard/departments").json()
    assert payload["scope"] == "OPEN_NOT_CANCELLED"
    assert payload["notice_count"] == 2
    by_id = {item["department_id"]: item for item in payload["departments"]}
    education = by_id["future-ai-education"]
    assert education["matched_count"] == 2
    assert education["evaluated_count"] == 1
    counts = {item["keyword"]: item["count"] for item in education["keywords"]}
    assert counts["디지털교육"] == 2
    assert counts["K-12"] == 0  # A registered keyword that brought in nothing.


def test_department_coverage_reports_selection_per_department(client):
    with client.app.state.session_factory() as session:
        chosen = _notice(session, "chosen", "초중고 AI 디지털교육 사업")
        session.flush()
        session.add(UserDecision(
            notice_id=chosen.id, choice="GO", rationale="SYN 근거",
            department_id="future-ai-education", department_name="AI미래교육본부",
            department_revision=1,
        ))
        session.add(UserDecision(
            notice_id=chosen.id, choice="NO_GO", rationale="SYN 근거",
            department_id="future-ai-capability", department_name="AI역량개발본부",
            department_revision=1,
        ))
        session.commit()

    by_id = {
        item["department_id"]: item
        for item in client.get("/api/v1/dashboard/departments").json()["departments"]
    }
    assert by_id["future-ai-education"]["selected_count"] == 1
    assert by_id["future-ai-capability"]["selected_count"] == 0


def test_department_coverage_is_readable_in_public_read_only_mode(client):
    """Coverage repeats public keyword profiles, so it stays browser-readable."""

    from dataclasses import replace

    client.app.state.settings = replace(client.app.state.settings, public_read_only=True)
    response = client.get("/api/v1/dashboard/departments")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["scope"] == "OPEN_NOT_CANCELLED"
    # The public projection carries no company fact or evidence identifier.
    assert "company" not in response.text
    assert "evidence" not in response.text


def test_selection_inside_scope_never_exceeds_the_matched_funnel(client):
    """The merged bar stacks collect → evaluate → recommend → select.

    A department may also select a notice its keywords never matched. That
    surplus must stay out of the scoped count so no stage overflows the stage
    that contains it, while the all-time count keeps reporting it.
    """

    with client.app.state.session_factory() as session:
        matched = _notice(session, "funnel-matched", "초중고 AI 디지털교육 사업", evaluated=True)
        unmatched = _notice(session, "funnel-unmatched", "도로 포장 보수 공사")
        session.flush()
        for notice in (matched, unmatched):
            session.add(UserDecision(
                notice_id=notice.id, choice="GO", rationale="SYN 근거",
                department_id="future-ai-education", department_name="AI미래교육본부",
                department_revision=1,
            ))
        session.commit()

    row = next(
        item
        for item in client.get("/api/v1/dashboard/departments").json()["departments"]
        if item["department_id"] == "future-ai-education"
    )
    assert row["matched_count"] == 1
    assert row["selected_matched_count"] == 1
    assert row["selected_count"] == 2  # The unmatched selection is still reported.
    assert row["selected_matched_count"] <= row["matched_count"]
