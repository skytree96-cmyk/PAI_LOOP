"""SYN-only offline replay: real domain stages, no DB/provider/network."""
from copy import deepcopy
from datetime import date, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import inspect, select

from pai_loop.integrations.openai_extraction import ExtractionPayload, OpenAIExtractionClient
from pai_loop.models import NoticeVersion
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction
from pai_loop.quantitative_scoring import _current_dynamic_quantitative_profile, quantitative_request_from_candidate_profile
from test_extraction_contract_compatibility import notice_fixture
from test_quantitative_source_revalidation import fixture as native_fixture

SPEC = importlib.util.spec_from_file_location("quantitative_replay_cli",
    Path(__file__).resolve().parents[1] / "scripts/replay-quantitative-sources.py")
replay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replay
SPEC.loader.exec_module(replay)


def native_notice(tmp_path, **fixture_options):
    inputs = native_fixture(**fixture_options)
    notice, metadata, attempt, _ = notice_fixture(legacy=False)
    attempt.notice_id = metadata.notice_id = notice.id
    metadata.source_payload["attachment_manifest"] = inputs["full_manifest"]
    attempt.source_payload = inputs["source_attempt"]
    attempt.file_sha256 = inputs["expected_native_sha256"]
    native = tmp_path / "SYN-source.docx"
    canonical = tmp_path / "SYN-canonical.txt"
    native.write_bytes(inputs["native_bytes"])
    canonical.write_text(inputs["canonical_text"], encoding="utf-8")
    mapping = {"version_id": attempt.id, "native_path": str(native), "canonical_path": str(canonical),
        "native_sha256": inputs["expected_native_sha256"], "canonical_sha256": inputs["expected_canonical_sha256"]}
    return notice, metadata, attempt, mapping


def serialize_row(row):
    return {column.key: (value.isoformat() if isinstance(value, (date, datetime)) else value)
        for column in inspect(type(row)).columns if (value := getattr(row, column.key)) is not None}


@pytest.mark.parametrize("unit", ["등급", None])
def test_actual_source_merge_company_bridge_and_score_with_syn_inputs(unit):
    from test_dense_case_source_binding import credit_fixture, ATT
    from test_quantitative_auto_activation import _company_fact
    notice, metadata, attempt, _ = notice_fixture(legacy=False)
    attempt.notice_id = metadata.notice_id = notice.id
    raw, text = credit_fixture()
    raw["quantitative_tables"][0]["criteria"][0]["unit"] = unit
    aid = attempt.source_payload["attachment_id"]
    payload = ExtractionPayload.model_validate(json.loads(json.dumps(raw).replace(ATT, aid)))
    native_sha = hashlib.sha256(text.encode()).hexdigest()
    record = validate_quantitative_attachment_extraction(payload, source_text=text, attachment_id=aid,
        document_sha256=native_sha, manifest_sha256=attempt.source_payload["current_manifest_sha256"])
    attempt.file_sha256 = native_sha
    attempt.source_payload.update(document_sha256=native_sha, result=payload.model_dump(mode="json"),
        quantitative_validation_record=record.model_dump(mode="json"))
    attempt.source_payload["document_processing"].update(source_text_sha256=native_sha,
        analysis_input_sha256=native_sha, source_characters=len(text))
    request = quantitative_request_from_candidate_profile(_current_dynamic_quantitative_profile(notice))
    fact = _company_fact(fact_key="company.credit_rating", value={"value": "A0", "unit": "등급",
        "fact_binding_sha256": request.criteria[0].fact_binding_sha256})
    before = deepcopy(attempt.source_payload)
    with replay.no_external_effects() as calls:
        missing = replay.replay_notice(notice)
        result = replay.replay_notice(notice, facts=[fact])
    assert not calls
    assert missing["estimate"]["estimated_points"] is None
    assert result["estimate"]["confirmed_points"] == 9
    assert result["selected_sources"] == result["frozen_select_calls"] == 1
    assert result["estimate"]["overall_status"] == "CONFIRMED"
    assert result["stage_funnel"] == {
        "profile_status": "AVAILABLE", "activation_status": "AUTO_ACTIVE",
        "compiled_activation_status": "AUTO_ACTIVE", "criteria": 1, "review_criteria": 0,
        "criterion_status_counts": {"CONFIRMED": 1}, "numeric_criterion_count": 1,
        "numeric_total": True, "overall_status": "CONFIRMED"}
    assert missing["stage_funnel"]["criterion_status_counts"] == {"UNSCORABLE": 1}
    assert missing["stage_funnel"]["numeric_total"] is False
    expected = {"version": "quantitative-replay-golden-1", "absolute_tolerance": 0.0,
        "cases": [{"notice_key": notice.notice_key, "expectation_basis": "OBSERVED_FROZEN_INPUTS",
            "criteria": [{"criterion_id": request.criteria[0].criterion_id, "status": "CONFIRMED", "points": 9.0}],
            "subtotal": {"overall_status": "CONFIRMED", "total_max_points": 9.0,
                         "estimated_points": 9.0, "lower_points": 9.0, "upper_points": 9.0}}]}
    golden = replay.GoldenFile.model_validate(expected)
    assert replay.compare_golden(golden, [result])["all_declared_expectations_match"] is True
    assert replay.compare_golden(golden, [missing])["all_declared_expectations_match"] is False
    assert attempt.source_payload == before


def test_previous_native_revalidation_remains_separate_from_stored_score(tmp_path):
    notice, _, attempt, mapping = native_notice(tmp_path)
    before = deepcopy(attempt.source_payload)
    with replay.no_external_effects() as calls:
        result = replay.replay_notice(notice, sources={attempt.id: mapping})
    source = result["source_attempts"][0]
    assert source["native_diagnostic"]["status"] == "VERIFIED"
    assert source["native_diagnostic"]["persistence_eligible"] is False
    assert source["raw_source_diagnostic"]["current_source_candidate"]["available_candidates"] == 1
    assert result["estimate"]["estimated_points"] is None
    funnel = replay.aggregate_stage_funnel([result])
    assert funnel["attachment_diagnostics_included"] is False
    assert funnel["compiled_criteria"] == 0
    assert next(row for row in funnel["table"] if row["stage"] == "runtime_has_numeric_total")["notices"] == 0
    assert attempt.source_payload == before
    assert not calls


@pytest.mark.parametrize("contract_kind", ["CURRENT", "EXACT_PREVIOUS_EXTRACTION", "EXACT_PREVIOUS_PROCESSING"])
def test_modern_credit_replay_reports_recovery_without_replacing_saved_score(tmp_path, monkeypatch, contract_kind):
    from pai_loop.extraction_contracts import (
        CURRENT_EXTRACTION_CONTRACT, PREVIOUS_EXTRACTION_CONTRACT, PREVIOUS_PROCESSING_CONTRACT,
    )
    contract = {"CURRENT": CURRENT_EXTRACTION_CONTRACT,
                "EXACT_PREVIOUS_EXTRACTION": PREVIOUS_EXTRACTION_CONTRACT,
                "EXACT_PREVIOUS_PROCESSING": PREVIOUS_PROCESSING_CONTRACT}[contract_kind]
    with monkeypatch.context() as old:
        old.setattr("pai_loop.quantitative_rule_extraction._bind_enterprise_credit_column",
                    lambda candidate, **kwargs: candidate)
        notice, _, attempt, mapping = native_notice(tmp_path, contract=contract, credit=True)
    before = deepcopy(attempt.source_payload)
    with replay.no_external_effects() as calls:
        result = replay.replay_notice(notice, sources={attempt.id: mapping})
    native = result["source_attempts"][0]["native_diagnostic"]
    assert native["status"] == "VERIFIED"
    assert native["contract"] == contract_kind
    assert native["profile"]["available_candidates"] == 1
    assert native["persistence_eligible"] is native["attachment_coverage_complete"] is False
    assert result["estimate"]["estimated_points"] is None
    assert result["stage_funnel"]["numeric_total"] is False
    assert attempt.source_payload == before
    assert not calls


def test_unsupported_contract_and_changed_canonical_are_diagnosed_without_promotion(tmp_path):
    notice, _, attempt, mapping = native_notice(tmp_path)
    attempt.source_payload["prompt_version"] = "pai-loop-extraction-0.5.5"
    frozen = "SYN old parser projection with no score anchors"
    Path(mapping["canonical_path"]).write_text(frozen, encoding="utf-8")
    mapping["canonical_sha256"] = hashlib.sha256(frozen.encode()).hexdigest()
    attempt.source_payload["document_processing"]["source_text_sha256"] = mapping["canonical_sha256"]
    before = deepcopy(attempt.source_payload)
    with replay.no_external_effects():
        result = replay.replay_notice(notice, sources={attempt.id: mapping})
    source = result["source_attempts"][0]
    assert result["selected_sources"] == 0
    assert source["header_contract"] == "UNSUPPORTED"
    diagnostic = source["raw_source_diagnostic"]
    assert diagnostic["origin"]["prompt_version"] == "pai-loop-extraction-0.5.5"
    assert diagnostic["status"] == "CURRENT_NATIVE_RAW_DIAGNOSED"
    assert diagnostic["current_canonical_matches_stored"] is False
    assert diagnostic["current_source_candidate"]["available_candidates"] == 1
    assert diagnostic["frozen_source_candidate"]["available_candidates"] == 0
    assert diagnostic["persistence_eligible"] is diagnostic["production_score_input"] is False
    assert result["estimate"]["estimated_points"] is None
    assert attempt.source_payload == before


@pytest.mark.parametrize("target", ["native", "raw_schema", "raw_null", "record"])
def test_tampered_inputs_fail_closed_and_do_not_create_company_score(tmp_path, target):
    notice, _, attempt, mapping = native_notice(tmp_path)
    if target == "native":
        Path(mapping["native_path"]).write_bytes(b"SYN substituted bytes")
    elif target == "raw_schema":
        attempt.source_payload["result"]["unknown_decision_field"] = "SYN"
    elif target == "raw_null":
        attempt.source_payload["result"] = None
    else:
        attempt.source_payload["quantitative_validation_record"]["validation_fingerprint_sha256"] = "0" * 64
    with replay.no_external_effects():
        result = replay.replay_notice(notice, sources={attempt.id: mapping})
    source = result["source_attempts"][0]
    if target == "native":
        assert source["raw_source_diagnostic"]["status"] == "NATIVE_SHA256_MISMATCH"
    elif target == "raw_schema":
        assert source["raw_source_diagnostic"]["status"] == "RAW_SCHEMA_INVALID"
    elif target == "raw_null":
        assert source["raw_source_diagnostic"]["status"] == "RAW_PAYLOAD_UNAVAILABLE"
    else:
        assert result["selected_sources"] == 0
    assert result["estimate"]["estimated_points"] is None


@pytest.mark.parametrize("effect", ["provider", "network", "database", "subprocess"])
def test_forbidden_external_attempts_cannot_be_swallowed(effect):
    actions = {
        "provider": lambda: OpenAIExtractionClient.extract(None, text="SYN", attachment_id="SYN"),
        "network": lambda: httpx.Client().get("https://syn.invalid"),
        "database": lambda: sqlite3.connect(":memory:"),
        "subprocess": lambda: subprocess.Popen(["SYN-FORBIDDEN"]),
    }
    with replay.no_external_effects() as calls:
        with pytest.raises(replay.ForbiddenEffect, match=effect):
            actions[effect]()
    assert calls == {effect: 1}


def test_provider_attempt_inside_parser_aborts_replay(tmp_path, monkeypatch):
    notice, _, attempt, mapping = native_notice(tmp_path)
    def forbidden_parser(*args, **kwargs):
        OpenAIExtractionClient.extract(None, text="SYN", attachment_id="SYN")
    monkeypatch.setattr(replay.pps, "extract_pps_document_content", forbidden_parser)
    with replay.no_external_effects():
        with pytest.raises(replay.ForbiddenEffect, match="provider"):
            replay.replay_notice(notice, sources={attempt.id: mapping})


def test_cli_requires_explicit_local_paths_and_preserves_existing_output(tmp_path, capsys):
    notice, _, attempt, mapping = native_notice(tmp_path)
    snapshot = tmp_path / "SYN-snapshot.json"
    snapshot.write_text(json.dumps({"notices": [serialize_row(notice)],
        "versions": [serialize_row(row) for row in notice.versions]}), encoding="utf-8")
    source_map = tmp_path / "SYN-map.json"
    source_map.write_text(json.dumps({"sources": [mapping]}), encoding="utf-8")
    output = tmp_path / "SYN-report.json"
    arguments = ["--snapshot", str(snapshot), "--source-map", str(source_map), "--output", str(output)]
    before = snapshot.read_bytes(), source_map.read_bytes()
    assert replay.main(arguments) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["provider_calls"] == result["database_connections"] == result["network_calls"] == 0
    assert result["persistence_entry_called"] is result["previous_contract_restamped"] is False
    assert result["cases"][0]["source_attempts"][0]["native_diagnostic"]["status"] == "VERIFIED"
    assert result["golden_comparison"] is None
    assert result["aggregate"]["stage_funnel"]["notice_denominator"] == 1
    assert (snapshot.read_bytes(), source_map.read_bytes()) == before
    saved = output.read_bytes()
    with pytest.raises(SystemExit):
        replay.main(arguments)
    assert output.read_bytes() == saved
    with pytest.raises(SystemExit):
        replay.main([])
    with pytest.raises(ValueError, match="LOCAL_FILES_ONLY"):
        replay.load_json("https://syn.invalid/snapshot.json")
    reader = replay.FrozenVersionReader(notice)
    with pytest.raises(AssertionError):
        reader.scalars(select(NoticeVersion).where(NoticeVersion.notice_id == "SYN-OTHER"))
    assert "SYN old parser" not in capsys.readouterr().out


def syn_golden():
    """Canonical strict v1 JSON example (SYN only).

    `points` is actual criterion.estimated_points, never maximum or a fact input.
    Every key below is required; subtotal may instead be null for row-only checks.
    REVIEW/UNSCORABLE/OUT_OF_SCOPE row points must be null. Missing rows fail.
    `absolute_tolerance` is in points, 0..0.01 inclusive; relative tolerance is 0.
    Observed means supplied frozen inputs only, not live verification. CONDITIONAL
    is for a user's unverified scenario and is never included in the observed rate.
    A subset of notices/rows is allowed, with explicit unchecked denominators.
    Old private expectation drafts are not implicitly converted into this schema.
    """
    return {
        "version": "quantitative-replay-golden-1", "absolute_tolerance": 0.001,
        "cases": [{"notice_key": "SYN-NOTICE", "expectation_basis": "OBSERVED_FROZEN_INPUTS",
            "criteria": [{"criterion_id": "SYN-CREDIT", "status": "CONFIRMED", "points": 5.0},
                         {"criterion_id": "SYN-PERFORMANCE", "status": "REVIEW", "points": None}],
            "subtotal": {"overall_status": "ESTIMATED", "total_max_points": 10.0,
                         "estimated_points": 5.0, "lower_points": 5.0, "upper_points": 10.0}}],
    }


def syn_replay_results():
    return [{"notice_key": "SYN-NOTICE", "estimate": {
        "criteria": [{"criterion_id": "SYN-CREDIT", "status": "CONFIRMED", "estimated_points": 5.0},
                     {"criterion_id": "SYN-PERFORMANCE", "status": "REVIEW", "estimated_points": None}],
        "overall_status": "ESTIMATED", "total_max_points": 10.0, "estimated_points": 5.0,
        "lower_points": 5.0, "upper_points": 10.0}}]


def test_stage_branches_preserve_curated_runtime_without_fabricating_dynamic_profile():
    # A curated score can exist without a dynamic profile; do not attribute that
    # score to raw-candidate diagnostics or fabricate dynamic compiled criteria.
    score = SimpleNamespace(activation_status="AUTO_ACTIVE", overall_status="CONFIRMED",
        estimated_points=0.0, criteria=[SimpleNamespace(status="CONFIRMED", estimated_points=0.0)])
    stage = replay.stage_funnel(None, None, score)
    assert stage["profile_status"] == stage["compiled_activation_status"] == "NONE"
    assert stage["numeric_total"] is True  # explicit verified zero is numeric
    assert stage["criteria"] == stage["review_criteria"] == 0
    second = replay.stage_funnel(SimpleNamespace(status="AVAILABLE"),
        SimpleNamespace(activation_status="PARTIAL_ACTIVE", criteria=[1], review_criteria=[2]),
        SimpleNamespace(activation_status="PARTIAL_ACTIVE", overall_status="ESTIMATED", estimated_points=5,
            criteria=[SimpleNamespace(status="CONFIRMED", estimated_points=5),
                      SimpleNamespace(status="REVIEW", estimated_points=None)]))
    funnel = replay.aggregate_stage_funnel([{"stage_funnel": stage}, {"stage_funnel": second}])
    assert funnel["notice_denominator"] == 2
    assert funnel["profile_status_counts"] == {"NONE": 1, "AVAILABLE": 1}
    assert funnel["compiled_criteria"] == funnel["compiled_review_criteria"] == 1
    assert funnel["criterion_status_counts"] == {"CONFIRMED": 2, "REVIEW": 1}
    assert all(row["denominator"] == 2 for row in funnel["table"])
    assert replay.aggregate_stage_funnel([])["table"][0] == {"stage": "snapshot_notices", "notices": 0, "denominator": 0}


def test_golden_observed_and_conditional_rates_have_separate_partial_denominators():
    raw, results = syn_golden(), syn_replay_results()
    conditional = deepcopy(raw["cases"][0])
    conditional.update(notice_key="SYN-CONDITIONAL", expectation_basis="CONDITIONAL")
    conditional["criteria"] = conditional["criteria"][:1]
    conditional["subtotal"] = None
    raw["cases"].append(conditional)
    results.extend([{"notice_key": key, "estimate": deepcopy(results[0]["estimate"])}
                    for key in ("SYN-CONDITIONAL", "SYN-UNCHECKED")])
    before = deepcopy(results), deepcopy(raw)
    comparison = replay.compare_golden(replay.GoldenFile.model_validate(raw), results)
    assert comparison["all_declared_expectations_match"] is True
    assert comparison["cohort_notices"] == 3 and comparison["golden_notices"] == 2
    assert comparison["unchecked_cohort_notices"] == 1
    assert comparison["cohort_coverage_complete"] is comparison["live_company_evidence_verified"] is False
    for basis in ("OBSERVED_FROZEN_INPUTS", "CONDITIONAL"):
        assert comparison["rates_by_expectation_basis"][basis] == {
            "expected_cases": 1, "matched_cases": 1, "failed_cases": 0, "declared_case_match_rate": 1.0}
    assert comparison["cases"][1]["unchecked_actual_criterion_count"] == 1
    assert comparison["cases"][1]["criterion_coverage_complete"] is False
    assert (results, raw) == before
    assert "estimated_points" not in json.dumps(comparison["cases"])  # no new facts/expected scores copied


@pytest.mark.parametrize("failure", ["unmatched", "duplicate_actual", "status", "null_not_zero", "subtotal"])
def test_golden_unmatched_or_mismatched_expectations_are_failures(failure):
    raw, results = syn_golden(), syn_replay_results()
    if failure == "unmatched":
        raw["cases"][0]["criteria"][0]["criterion_id"] = "SYN-MISSING"
    elif failure == "duplicate_actual":
        results[0]["estimate"]["criteria"].append(deepcopy(results[0]["estimate"]["criteria"][0]))
    elif failure == "status":
        results[0]["estimate"]["criteria"][0]["status"] = "ESTIMATED"
    elif failure == "null_not_zero":
        raw["cases"][0]["criteria"][0]["points"] = 0.0
        results[0]["estimate"]["criteria"][0]["estimated_points"] = None
    else:
        raw["cases"][0]["subtotal"]["upper_points"] = 9.0
    comparison = replay.compare_golden(replay.GoldenFile.model_validate(raw), results)
    assert comparison["all_declared_expectations_match"] is False
    assert comparison["rates_by_expectation_basis"]["OBSERVED_FROZEN_INPUTS"]["failed_cases"] == 1
    assert comparison["rates_by_expectation_basis"]["CONDITIONAL"]["declared_case_match_rate"] is None
    if failure in {"unmatched", "duplicate_actual"}:
        assert comparison["cases"][0]["criteria"][0]["result"] == ("UNMATCHED" if failure == "unmatched" else "AMBIGUOUS")


@pytest.mark.parametrize(("difference", "matches"), [(0.0005, True), (0.002, False)])
def test_golden_uses_explicit_absolute_point_tolerance(difference, matches):
    raw, results = syn_golden(), syn_replay_results()
    results[0]["estimate"]["criteria"][0]["estimated_points"] += difference
    comparison = replay.compare_golden(replay.GoldenFile.model_validate(raw), results)
    assert comparison["relative_tolerance"] == 0
    assert comparison["absolute_tolerance"] == 0.001
    assert comparison["all_declared_expectations_match"] is matches


@pytest.mark.parametrize("invalid", ["extra", "missing", "numeric_string", "bool", "nan", "inf",
    "duplicate_notice", "duplicate_criterion", "unknown_status", "zero_review", "null_confirmed",
    "huge_tolerance", "negative_tolerance", "empty_cases", "empty_case", "invalid_range", "above_max"])
def test_golden_strict_schema_rejects_ambiguous_or_coerced_expectations(tmp_path, invalid):
    raw = syn_golden()
    case, row = raw["cases"][0], raw["cases"][0]["criteria"][0]
    if invalid == "extra":
        row["company_fact"] = "SYN-SHOULD-NOT-BE-ECHOED"
    elif invalid == "missing":
        del row["points"]
    elif invalid in {"numeric_string", "bool", "nan", "inf"}:
        row["points"] = {"numeric_string": "5", "bool": True, "nan": float("nan"), "inf": float("inf")}[invalid]
    elif invalid == "duplicate_notice":
        raw["cases"].append(deepcopy(case))
    elif invalid == "duplicate_criterion":
        case["criteria"].append(deepcopy(row))
    elif invalid == "unknown_status":
        row["status"] = "APPROVED"
    elif invalid == "zero_review":
        row.update(status="REVIEW", points=0.0)
    elif invalid == "null_confirmed":
        row["points"] = None
    elif invalid in {"huge_tolerance", "negative_tolerance"}:
        raw["absolute_tolerance"] = 1.0 if invalid == "huge_tolerance" else -0.1
    elif invalid == "empty_cases":
        raw["cases"] = []
    elif invalid == "empty_case":
        case.update(criteria=[], subtotal=None)
    elif invalid == "invalid_range":
        case["subtotal"].update(lower_points=9.0, upper_points=5.0)
    else:
        case["subtotal"]["upper_points"] = 11.0
    path = tmp_path / "SYN-invalid-golden.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        replay.load_golden(path)
    assert "SYN-SHOULD-NOT-BE-ECHOED" not in str(exc.value)


def test_golden_loader_rejects_duplicate_json_keys_size_and_nonlocal_paths(tmp_path):
    path = tmp_path / "SYN-golden.json"
    path.write_text('{"cases": [], "cases": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="GOLDEN_DUPLICATE_JSON_KEY"):
        replay.load_golden(path)
    path.write_text(" " * 2_000_001, encoding="utf-8")
    with pytest.raises(ValueError, match="GOLDEN_SIZE_LIMIT"):
        replay.load_golden(path)
    with pytest.raises(ValueError, match="LOCAL_FILES_ONLY"):
        replay.load_golden("https://syn.invalid/golden.json")


@pytest.mark.parametrize("field", ["id", "notice_key"])
def test_duplicate_snapshot_identity_is_rejected_before_any_replay(field):
    rows = [{"id": "SYN-1", "notice_key": "SYN-N1"}, {"id": "SYN-2", "notice_key": "SYN-N2"}]
    rows[1][field] = rows[0][field]
    with pytest.raises(ValueError, match="DUPLICATE_SNAPSHOT_NOTICE_IDENTITY"):
        replay.load_notices({"notices": rows, "versions": []})


def test_golden_unknown_notice_or_duplicate_result_never_silently_passes():
    golden = replay.GoldenFile.model_validate(syn_golden())
    with pytest.raises(ValueError, match="GOLDEN_NOTICE_NOT_IN_SNAPSHOT"):
        replay.compare_golden(golden, [])
    with pytest.raises(ValueError, match="DUPLICATE_REPLAY_NOTICE_KEY"):
        replay.compare_golden(golden, syn_replay_results() * 2)


def test_cli_golden_is_comparison_only_and_mismatch_saves_report_with_failure_exit(tmp_path, capsys, monkeypatch):
    notice, _, attempt, mapping = native_notice(tmp_path)
    snapshot, source_map = tmp_path / "SYN-snapshot.json", tmp_path / "SYN-map.json"
    golden_path, output = tmp_path / "SYN-golden.json", tmp_path / "SYN-report.json"
    snapshot.write_text(json.dumps({"notices": [serialize_row(notice)],
        "versions": [serialize_row(row) for row in notice.versions]}), encoding="utf-8")
    source_map.write_text(json.dumps({"sources": [mapping]}), encoding="utf-8")
    raw = syn_golden()
    raw["cases"][0].update(notice_key=notice.notice_key, expectation_basis="CONDITIONAL", subtotal=None)
    golden_path.write_text(json.dumps(raw), encoding="utf-8")
    original = [path.read_bytes() for path in (snapshot, source_map, golden_path)]
    args = ["--snapshot", str(snapshot), "--source-map", str(source_map), "--golden", str(golden_path),
            "--output", str(output)]
    assert replay.main(args) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["golden_comparison"]["rates_by_expectation_basis"]["CONDITIONAL"]["failed_cases"] == 1
    assert report["golden_comparison"]["rates_by_expectation_basis"]["OBSERVED_FROZEN_INPUTS"]["expected_cases"] == 0
    assert report["cases"][0]["estimate"]["estimated_points"] is None
    assert report["provider_calls"] == report["database_connections"] == report["network_calls"] == 0
    assert report["guard_counts"] == {}
    assert original == [path.read_bytes() for path in (snapshot, source_map, golden_path)]
    assert "SYN-CREDIT" not in capsys.readouterr().out
    # An honestly expected blocked result matches without becoming a numeric score.
    raw["cases"][0].update(expectation_basis="OBSERVED_FROZEN_INPUTS", criteria=[],
        subtotal={"overall_status": "REVIEW", "total_max_points": None, "estimated_points": None,
                  "lower_points": None, "upper_points": None})
    golden_path.write_text(json.dumps(raw), encoding="utf-8")
    args[-1] = str(tmp_path / "SYN-matched-review.json")
    assert replay.main(args) == 0
    matched = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
    assert matched["golden_comparison"]["all_declared_expectations_match"] is True
    assert matched["cases"][0]["estimate"]["estimated_points"] is None
    assert matched["aggregate"]["confirmed_total_count"] == 0
    raw["cases"][0]["notice_key"] = "SYN-OUTSIDE-COHORT"
    golden_path.write_text(json.dumps(raw), encoding="utf-8")
    def must_not_replay(*args, **kwargs):
        pytest.fail("Unknown golden cohort must fail before replaying sources")
    monkeypatch.setattr(replay, "replay_notice", must_not_replay)
    args[-1] = str(tmp_path / "SYN-unknown.json")
    with pytest.raises(ValueError, match="GOLDEN_NOTICE_NOT_IN_SNAPSHOT"):
        replay.main(args)
    assert not Path(args[-1]).exists()
