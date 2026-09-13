"""SYN-only offline replay: real domain stages, no DB/provider/network."""
from copy import deepcopy
from datetime import date, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess

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
SPEC.loader.exec_module(replay)


def native_notice(tmp_path):
    inputs = native_fixture()
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


def test_actual_source_merge_company_bridge_and_score_with_syn_inputs():
    from test_dense_case_source_binding import credit_fixture, ATT
    from test_quantitative_auto_activation import _company_fact
    notice, metadata, attempt, _ = notice_fixture(legacy=False)
    attempt.notice_id = metadata.notice_id = notice.id
    raw, text = credit_fixture()
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
    replay.main(arguments)
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["provider_calls"] == result["database_connections"] == result["network_calls"] == 0
    assert result["persistence_entry_called"] is result["previous_contract_restamped"] is False
    assert result["cases"][0]["source_attempts"][0]["native_diagnostic"]["status"] == "VERIFIED"
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
