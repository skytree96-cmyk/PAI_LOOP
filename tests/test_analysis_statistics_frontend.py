from __future__ import annotations

import json
import subprocess
from pathlib import Path


APP = Path(__file__).parents[1] / "src/pai_loop/static/app.js"


def test_statistics_preserves_denominators_missing_scores_and_read_failures() -> None:
    source = APP.read_text(encoding="utf-8")
    start = source.index("  function summarizeAnalysisStatistics(")
    end = source.index("  function renderRiskPanel(", start)
    script = source[start:end] + """
const rows = [
  {analysis_state:'PENDING', analysis_reason_code:'NOT_SELECTED', analysis_attempted:false,
   expected_attachments:3, audited_attachments:0, accepted_attachments:0,
   attachment_coverage_complete:false, eligibility:'NOT_EVALUATED', profile_status:'NOT_QUERIED',
   activation_status:'NOT_QUERIED', score_status:'NOT_QUERIED', available_candidates:0,
   review_candidates:0, issues:[], read_errors:[]},
  {analysis_state:'EVALUATED', analysis_reason_code:'ANALYZED', analysis_attempted:true,
   expected_attachments:2, audited_attachments:2, accepted_attachments:2,
   attachment_coverage_complete:true, eligibility:'FAIL', profile_status:'INCOMPLETE',
   activation_status:'REVIEW_REQUIRED', score_status:'REVIEW', estimated_points:null,
   available_candidates:0, review_candidates:3,
   issues:[{code:'SYN-QUOTE',count:2},{code:'SYN-QUOTE',count:1}], read_errors:[]},
  {analysis_state:'REVIEW', analysis_reason_code:'QUOTE_UNVERIFIED', analysis_attempted:true,
   expected_attachments:2, audited_attachments:2, accepted_attachments:1,
   attachment_coverage_complete:false, eligibility:'NOT_EVALUATED', profile_status:'NOT_QUERIED',
   activation_status:'NOT_QUERIED', score_status:'NOT_QUERIED', available_candidates:0,
   review_candidates:0, issues:[], read_errors:['QUANTITATIVE_DIAGNOSTIC_READ_FAILED']},
];
const before = JSON.stringify(rows);
const result = summarizeAnalysisStatistics(rows, {stored_notice_count:9});
if (JSON.stringify(rows) !== before) throw new Error('mutated source rows');
console.log(JSON.stringify(result));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
    report = json.loads(result.stdout)
    assert report["stored_notice_count"] == 9
    assert report["open_notice_count"] == 3
    assert report["attempted_notice_count"] == 2
    assert report["complete_attachment_notice_count"] == 1
    assert report["expected_attachment_count"] == 7
    assert report["audited_attachment_count"] == 4
    assert report["accepted_attachment_count"] == 3
    assert report["quantitative_issue_notice_counts"] == {"SYN-QUOTE": 1}
    assert report["eligibility"] == {"NOT_EVALUATED": 2, "FAIL": 1}
    assert report["read_error_notice_count"] == 1
    assert report["rows"][1]["estimated_points"] is None


def test_statistics_reads_all_pages_without_triggering_analysis_or_exposing_raw_data() -> None:
    source = APP.read_text(encoding="utf-8")
    start = source.index("  async function collectAnalysisStatistics(")
    end = source.index("  function summarizeAnalysisStatistics(", start)
    body = source[start:end]
    assert "/notices?limit=200&offset=${offset}" in body
    assert "page.length < 200" in body
    assert 'item.status === "OPEN"' in body
    assert "if (notice.analysis_attempted)" in body
    assert 'method: "POST", headers' in body
    assert "/analysis/request" not in body
    for forbidden in ("source_payload", "company_fact", "review_candidate_shapes", "localStorage", "sessionStorage"):
        assert forbidden not in body


def test_quantitative_retry_is_explicit_pin_scoped_and_generation_bounded() -> None:
    source = APP.read_text(encoding="utf-8")
    start = source.index("  function renderQuantitativeDiagnosticsControl(")
    end = source.index("  async function collectAnalysisStatistics(", start)
    body = source[start:end]
    assert "data.review_candidate_count > 0" in body
    assert "notice.analysisAttachmentCoverageComplete" in body
    assert 'noticeLifecycleStatus(notice) === "OPEN"' in body
    assert "await manualAnalysisAuthHeaders()" in body
    assert "run_extraction: true, retry_reviewed: true" in body
    assert "MANUAL_ANALYSIS_MAX_POLLS" in body
    assert "force: true" not in body


def test_home_progress_uses_global_counts_and_does_not_turn_missing_into_zero() -> None:
    source = APP.read_text(encoding="utf-8")
    start = source.index("  function renderAnalysisProgress(")
    end = source.index("  function renderNavigationCounts(", start)
    setup = """
const els = Object.fromEntries(['analysisProgress','analysisProgressScope','analysisAttachmentValue','analysisAttachmentDetail','analysisEligibilityValue','analysisEligibilityDetail','analysisScoreValue','analysisScoreDetail'].map(id=>[id,{textContent:''}]));
const state = {dashboard:{lastSync:'2026-09-06T02:38:00Z'}};
const formatNumber = n => String(n);
const formatKstDateTime = n => n;
"""
    script = setup + source[start:end] + """
renderAnalysisProgress({scope:'OPEN_PPS_NOT_CANCELLED', notice_count:282, attempted_notice_count:93,
attachment_count:941, audited_attachment_count:264, accepted_attachment_count:196,
analysis_state_counts:{ANALYZED:35,REVIEW:58,PENDING:189},
eligibility_counts:{PASS:0,FAIL:19,REVIEW:16,NOT_EVALUATED:247},
score_counts:{CONFIRMED:0,ESTIMATED:0,UNSCORABLE:1,REVIEW:34,NOT_EVALUATED:247}, score_range_notice_count:1});
const loaded = JSON.parse(JSON.stringify(els));
renderAnalysisProgress(null);
console.log(JSON.stringify({loaded,missing:els}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
    output = json.loads(result.stdout)
    loaded = output["loaded"]
    assert loaded["analysisAttachmentValue"]["textContent"] == "196 / 941 · 20.8%"
    assert loaded["analysisEligibilityValue"]["textContent"] == "19 / 282 · 6.7%"
    assert loaded["analysisScoreValue"]["textContent"] == "1 / 282 · 0.4%"
    assert "미평가 247" in loaded["analysisEligibilityDetail"]["textContent"]
    assert "일부 미산정 1" in loaded["analysisScoreDetail"]["textContent"]
    assert output["missing"]["analysisScoreValue"]["textContent"] == "—"
    assert "조회 대기" in output["missing"]["analysisProgressScope"]["textContent"]
