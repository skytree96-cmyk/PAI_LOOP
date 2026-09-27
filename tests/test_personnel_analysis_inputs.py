from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pai_loop.analysis_pipeline import _selected_fact_manifest, run_analysis_pipeline
from pai_loop.models import AnalysisRun, CompanyFact, ScoreSnapshot
from pai_loop.quantitative_personnel import PERSONNEL_ROSTER_FACT_KEY
from test_analysis_pipeline import DEADLINE, _notice, _source_version, db_session


def _roster_fact(**updates: object) -> CompanyFact:
    fields = {
        "id": "SYN-ROSTER-INPUT",
        "fact_key": PERSONNEL_ROSTER_FACT_KEY,
        "value": {
            "schema_version": "pai-loop-personnel-roster-1.0",
            "source_sha256": "a" * 64,
            "snapshot_date": "2026-08-01",
            "verified_through": "2026-08-30",
            "verification_attestation": "HUMAN_REVIEWED_ROSTER_SNAPSHOT",
            "research_grade_basis": "CORRECTED_FINAL",
            "members": [{
                "member_key": "SYN-PRIVATE-MEMBER",
                "joined_on": "2025-01-01",
                "degrees": [{"level": "MASTER", "major": "SYN-PRIVATE-MAJOR"}],
                "credentials": [],
                "credentials_recorded": False,
                "research_grade": "RESEARCHER",
            }],
        },
        "effective_from": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "effective_to": datetime(2026, 8, 30, 14, 59, 59, tzinfo=timezone.utc),
        "verified": True,
        "source": "SYN-PRIVATE-OPERATOR-INPUT",
    }
    fields.update(updates)
    return CompanyFact(**fields)


@pytest.mark.parametrize("field,value", [
    ("schema_version", "SYN-UNSUPPORTED-ROSTER-VERSION"),
    ("source_sha256", "b" * 64),
    ("snapshot_date", "2026-08-02"),
    ("verified_through", "2026-08-29"),
    ("projection_through", "2026-09-30"),
    ("projection_assumption", "CURRENT_ROSTER_UNCHANGED"),
    ("members", []),
])
def test_raw_roster_version_dates_and_members_are_hashed(field, value):
    fact = _roster_fact()
    before = _selected_fact_manifest(
        [fact], fact_keys={PERSONNEL_ROSTER_FACT_KEY}, deadline=DEADLINE,
    )
    fact.value = {**fact.value, field: value}
    after = _selected_fact_manifest(
        [fact], fact_keys={PERSONNEL_ROSTER_FACT_KEY}, deadline=DEADLINE,
    )
    assert before != after
    assert before[0]["company_fact_id"] == after[0]["company_fact_id"] == fact.id
    assert set(after[0]) == {"company_fact_id", "basis_sha256"}


def test_roster_inputs_outside_deadline_are_tracked_without_broadening_other_facts():
    future = datetime(2027, 1, 1, tzinfo=timezone.utc)
    roster = _roster_fact(effective_from=future, effective_to=None)
    financial = _roster_fact(
        id="SYN-FUTURE-FINANCIAL", fact_key="company.financial.ratio",
        effective_from=future, effective_to=None,
    )
    unrelated = _roster_fact(id="SYN-UNRELATED", fact_key="company.unrelated.raw")
    manifest = _selected_fact_manifest(
        [unrelated, financial, roster],
        fact_keys={PERSONNEL_ROSTER_FACT_KEY, "company.financial.ratio"},
        deadline=DEADLINE,
    )
    assert [item["company_fact_id"] for item in manifest] == [roster.id]


@pytest.mark.parametrize("change", ["source_version", "effective_window", "attestation"])
def test_pipeline_tracks_roster_change_even_when_score_output_is_unchanged(
    db_session: Session, change: str,
):
    notice = _notice(db_session, notice_key="SYN-ROSTER-CACHE", title="SYN analysis")
    _source_version(
        notice, version_no=1, attachment_id="SYN-ATTACHMENT", digest_char="c",
        requirements=[],
    )
    roster = _roster_fact()
    db_session.add(roster)
    db_session.commit()
    notice_id, roster_id = notice.id, roster.id
    db_session.expire_all()

    first = run_analysis_pipeline(db_session, notice_id=notice_id)
    first_run = db_session.get(AnalysisRun, first.analysis_run_id)
    first_manifest = copy.deepcopy(first_run.input_manifest)
    first_score = db_session.scalar(select(ScoreSnapshot).where(
        ScoreSnapshot.analysis_run_id == first.analysis_run_id,
        ScoreSnapshot.score_key == "quantitative.total",
    ))
    first_public_criteria = copy.deepcopy(first_score.basis_json.get("public_criteria"))
    first_score_values = (first_score.status, first_score.value,
                          first_score.lower_value, first_score.upper_value)
    assert first_manifest["company_fact_ids"] == [roster_id]
    db_session.rollback()

    unchanged = run_analysis_pipeline(db_session, notice_id=notice_id)
    assert unchanged.reused is True
    assert unchanged.analysis_run_id == first.analysis_run_id
    roster = db_session.get(CompanyFact, roster_id)
    if change == "source_version":
        roster.value = {**roster.value, "source_sha256": "d" * 64}
    elif change == "effective_window":
        roster.effective_from = datetime(2027, 1, 1, tzinfo=timezone.utc)
    else:
        roster.verified = False
    db_session.commit()
    db_session.expire_all()

    changed = run_analysis_pipeline(db_session, notice_id=notice_id)
    assert changed.reused is False
    assert changed.analysis_run_id != first.analysis_run_id
    changed_run = db_session.get(AnalysisRun, changed.analysis_run_id)
    assert changed_run.input_manifest["company_fact_ids"] == [roster_id]
    assert (changed_run.input_manifest["company_fact_basis_sha256s"]
            != first_manifest["company_fact_basis_sha256s"])
    changed_score = db_session.scalar(select(ScoreSnapshot).where(
        ScoreSnapshot.analysis_run_id == changed.analysis_run_id,
        ScoreSnapshot.score_key == "quantitative.total",
    ))
    assert (changed_score.status, changed_score.value,
            changed_score.lower_value, changed_score.upper_value) == first_score_values
    assert changed_score.basis_json.get("public_criteria") == first_public_criteria
    persisted = json.dumps({
        "manifest": changed_run.input_manifest, "score": changed_score.basis_json,
    })
    for private_value in ("SYN-PRIVATE-MEMBER", "SYN-PRIVATE-MAJOR",
                          "SYN-PRIVATE-OPERATOR-INPUT", "source_sha256"):
        assert private_value not in persisted
