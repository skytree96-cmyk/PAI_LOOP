"""SYN native files, actual offline connector, no providers or persistence."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from test_quantitative_replay_cli import native_notice, serialize_row

SPEC = importlib.util.spec_from_file_location("review_preview_cli",
    Path(__file__).resolve().parents[1] / "scripts/preview-reviewed-quantitative.py")
cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cli
SPEC.loader.exec_module(cli)


def setup_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    notice, metadata, attempt, mapping = native_notice(tmp_path)
    snapshot = tmp_path / "SYN-snapshot.json"
    snapshot.write_text(json.dumps({"notices": [serialize_row(notice)],
        "versions": [serialize_row(metadata), serialize_row(attempt)]}), encoding="utf-8")
    raw = attempt.source_payload["result"]
    payload = tmp_path / "SYN-payload.json"
    payload.write_text(json.dumps(raw), encoding="utf-8")
    aid = attempt.source_payload["attachment_id"]
    plan = {"version": "quantitative-review-preview-1", "cases": [{"notice_key": notice.notice_key,
        "manifest_sha256": cli.replay.digest(metadata.source_payload["attachment_manifest"]),
        "expected_documents": {aid: mapping["native_sha256"]}, "attachments": [{"attachment_id": aid,
            "native_path": mapping["native_path"], "payload_path": str(payload), "payload_sha256": cli.replay.digest(raw)}]}]}
    plan_path = tmp_path / "SYN-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    output = tmp_path / ".local/SYN-preview.json"
    args = ["--snapshot", str(snapshot), "--review-plan", str(plan_path), "--output", str(output)]
    return plan, plan_path, output, args


def test_cli_uses_native_and_raw_keeps_stored_estimate_separate(tmp_path, monkeypatch, capsys):
    _, _, output, args = setup_case(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert cli.main(args) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["inputs_unchanged"] and not report["external_effect_attempts"]
    assert not report["production_eligible"] and not report["persistence_eligible"]
    row = report["results"][0]
    assert row["local_review_preview"]["attachment_checks"][0]["code"] == "NATIVE_AND_RULES_CHECKED"
    assert "actual_stored_estimate" in row and row["actual_stored_estimate"]["estimated_points"] is None
    assert len(report["input_bytes_sha256"]) == 4
    assert all(path.read_bytes() == content for path, content in before.items())
    console = capsys.readouterr()
    assert "notice_key" not in console.out and not console.err


@pytest.mark.parametrize("fault,code", [("manifest", "MANIFEST_SHA256_MISMATCH"),
    ("duplicate", "DUPLICATE_PLAN_NOTICE_KEY"), ("unknown", "PLAN_NOTICE_NOT_IN_SNAPSHOT"),
    ("compiled", "ValidationError")])
def test_cli_rejects_unsafe_plans_without_output(tmp_path, monkeypatch, capsys, fault, code):
    plan, path, output, args = setup_case(tmp_path, monkeypatch)
    if fault == "manifest":
        plan["cases"][0]["manifest_sha256"] = "0" * 64
    elif fault == "duplicate":
        plan["cases"].append(deepcopy(plan["cases"][0]))
    elif fault == "unknown":
        plan["cases"][0]["notice_key"] = "SYN-UNKNOWN"
    else:
        plan["cases"][0]["criteria"] = [{"SYN-PRIVATE-SENTINEL": "must never echo"}]
    path.write_text(json.dumps(plan), encoding="utf-8")
    assert cli.main(args) == 2 and not output.exists()
    error = capsys.readouterr().err
    assert json.loads(error)["error"] == code and "SYN-PRIVATE-SENTINEL" not in error


@pytest.mark.parametrize("target", ["outside", "existing", "input", "url", "unc"])
def test_cli_output_is_new_and_private(tmp_path, monkeypatch, capsys, target):
    _, plan, output, args = setup_case(tmp_path, monkeypatch)
    target_path = {"outside": str(tmp_path / "SYN-public.json"), "existing": str(output),
        "input": str(plan), "url": "https://syn.invalid/result", "unc": "//SYN-host/result"}[target]
    if target == "existing":
        output.parent.mkdir()
        output.write_text("SYN original", encoding="utf-8")
    original = plan.read_bytes()
    args[-1] = target_path
    assert cli.main(args) == 2
    assert plan.read_bytes() == original
    if target == "existing":
        assert output.read_text(encoding="utf-8") == "SYN original"
    assert "error" in capsys.readouterr().err


def test_cli_blocks_external_attempt_and_suppresses_parser_stderr(tmp_path, monkeypatch, capsys):
    _, _, output, args = setup_case(tmp_path, monkeypatch)
    def forbidden(**kwargs):
        import socket
        print("SYN-PRIVATE-PARSER-TEXT", file=sys.stderr)
        socket.create_connection(("syn.invalid", 443))
    monkeypatch.setattr(cli, "preview_reviewed_quantitative_inputs", forbidden)
    assert cli.main(args) == 2 and not output.exists()
    assert "SYN-PRIVATE-PARSER-TEXT" not in capsys.readouterr().err


def test_cli_detects_input_changed_during_preview(tmp_path, monkeypatch, capsys):
    _, plan, output, args = setup_case(tmp_path, monkeypatch)
    actual = cli.preview_reviewed_quantitative_inputs
    def tampering(**kwargs):
        result = actual(**kwargs)
        plan.write_text("SYN changed concurrently", encoding="utf-8")
        return result
    monkeypatch.setattr(cli, "preview_reviewed_quantitative_inputs", tampering)
    assert cli.main(args) == 2 and not output.exists()
    assert json.loads(capsys.readouterr().err)["error"] == "INPUT_CHANGED"


@pytest.mark.parametrize("fault,code", [("version", "DUPLICATE_NOTICE_VERSION_NUMBER"),
    ("schema", "CURRENT_FROZEN_METADATA_REQUIRED")])
def test_cli_requires_unambiguous_current_frozen_metadata(tmp_path, monkeypatch, capsys, fault, code):
    _, _, output, args = setup_case(tmp_path, monkeypatch)
    snapshot = Path(args[1])
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    if fault == "version":
        data["versions"][1]["version_no"] = data["versions"][0]["version_no"]
    else:
        newer = deepcopy(data["versions"][0])
        newer.update(id="SYN-NEWER-METADATA", version_no=3)
        newer["source_payload"]["schema_version"] = "SYN-obsolete"
        data["versions"].append(newer)
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(args) == 2 and not output.exists()
    assert json.loads(capsys.readouterr().err)["error"] == code
