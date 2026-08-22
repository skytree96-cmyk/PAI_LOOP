from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.eligibility_policy import classify_requirements, load_public_company_profile
from pai_loop.main import create_app
from pai_loop.models import CompanyFact, Notice, NoticeVersion, UserDecision
from pai_loop.public_notice_cli import main as public_notice_cli_main
from pai_loop.public_notice_seed import (
    EVIDENCE_FIELDS,
    EXTRACTION_FIELDS,
    NOTICE_FIELDS,
    PUBLIC_NOTICE_CLASSIFICATION,
    PUBLIC_NOTICE_SOURCE_KEY,
    PROVENANCE_FIELDS,
    REQUIREMENT_FIELDS,
    PublicNoticeSeedError,
    build_public_notice_seed_from_db,
    calculate_public_notice_payload_digest,
    import_public_notice_seed,
    load_public_notice_seed,
    validate_public_notice_seed,
)


def _safe_extraction_payload() -> dict[str, object]:
    return {
        "kind": "OPENAI_REQUIREMENT_EXTRACTION",
        "source_kind": "공개 공고 원문",
        "attachment_id": "att_" + "b" * 64,
        "source_label": "공개 공고서",
        "document_sha256": "a" * 64,
        "status": "ACCEPTED",
        "review_code": None,
        "error_code": None,
        "message": "검증 완료",
        "response_id": "must-not-be-exported",
        "document_processing": {
            "openai_telemetry": {
                "input_tokens": 1_234,
                "cached_input_tokens": 123,
                "output_tokens": 456,
                "reasoning_output_tokens": 78,
                "total_tokens": 1_690,
                "total_request_latency_ms": 987,
            }
        },
        "model": "model-metadata-not-required",
        "prompt_version": "pai-loop-extraction-0.2.1",
        "schema_version": "pai-loop-requirements-0.1.0",
        "result": {
            "document_type": "NOTICE",
            "summary": "공개 조달 공고의 참가 조건을 구조화했습니다.",
            "missing_or_unreadable": [],
            "requirements": [
                {
                    "requirement_id": "REQ-001",
                    "category": "ENTITY",
                    "logic": "SINGLE",
                    "normalized_condition": "입찰참가 자격요건을 갖추어야 합니다.",
                    "mandatory": True,
                    "deadline_basis": "공고 마감일",
                    "evidence": [
                        {
                            "attachment_id": "att_" + "b" * 64,
                            "page": 1,
                            "section": "참가자격",
                            "quote": "입찰참가 자격요건을 갖춘 업체",
                            "confidence": 0.99,
                        }
                    ],
                    "ambiguity_reason": None,
                }
            ],
        },
    }


def _write_bounded_source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE notices (
                id TEXT PRIMARY KEY,
                notice_key TEXT NOT NULL,
                bid_notice_no TEXT NOT NULL,
                revision_no TEXT NOT NULL,
                title TEXT NOT NULL,
                agency TEXT NOT NULL,
                published_at TEXT NOT NULL,
                deadline TEXT NOT NULL,
                category TEXT,
                source_url TEXT
            );
            CREATE TABLE notice_versions (
                notice_id TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                file_sha256 TEXT NOT NULL,
                document_complete INTEGER NOT NULL,
                extraction_status TEXT NOT NULL,
                extraction_confidence REAL NOT NULL,
                source_payload TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO notices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "notice-id",
                PUBLIC_NOTICE_SOURCE_KEY,
                "PUBLIC-NOTICE-001",
                "00",
                "공개 교육 운영 용역",
                "공개 발주기관",
                "2026-01-02 09:00:00",
                "2026-01-15 16:00:00",
                "용역",
                None,
            ),
        )
        connection.execute(
            """
            INSERT INTO notice_versions VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "notice-id",
                1,
                "a" * 64,
                1,
                "ACCEPTED",
                0.99,
                json.dumps(_safe_extraction_payload(), ensure_ascii=False),
            ),
        )


def test_bounded_builder_is_read_only_and_projects_fixed_allowlists(tmp_path: Path) -> None:
    source = tmp_path / "bounded-source.sqlite3"
    _write_bounded_source(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    result = build_public_notice_seed_from_db(source)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert tuple(result.payload["notice"]) == NOTICE_FIELDS
    assert tuple(result.payload["extraction"]) == EXTRACTION_FIELDS
    assert tuple(result.payload["provenance"]) == PROVENANCE_FIELDS
    assert tuple(result.payload["extraction"]["requirements"][0]) == REQUIREMENT_FIELDS
    assert tuple(result.payload["extraction"]["requirements"][0]["evidence"][0]) == EVIDENCE_FIELDS
    assert result.payload["notice"]["source_url"] == "https://www.g2b.go.kr/"
    serialized = json.dumps(result.payload, ensure_ascii=False)
    assert "must-not-be-exported" not in serialized
    assert "model-metadata-not-required" not in serialized
    assert "response_id" not in serialized
    assert "openai_telemetry" not in serialized
    assert "input_tokens" not in serialized
    assert "request_latency_ms" not in serialized


def test_packaged_actual_notice_seed_is_public_safe_and_complete() -> None:
    seed = load_public_notice_seed()
    serialized = json.dumps(seed, ensure_ascii=False)

    assert seed["classification"] == PUBLIC_NOTICE_CLASSIFICATION
    assert seed["notice"]["notice_key"] == PUBLIC_NOTICE_SOURCE_KEY
    assert len(seed["extraction"]["requirements"]) == 23
    assert seed["provenance"]["evidence_anchor_count"] == 26
    assert seed["provenance"]["requirement_count"] == 23
    assert "synthetic" not in serialized.casefold()
    assert not {
        "actual_value",
        "actor_label",
        "api_key",
        "company_facts",
        "credential",
        "response_id",
        "user_decisions",
    }.intersection(serialized.casefold())


def test_packaged_notice_reproduces_four_policy_groups() -> None:
    seed = load_public_notice_seed()
    result = classify_requirements(
        seed["extraction"]["requirements"],
        profile=load_public_company_profile(),
        deadline=seed["notice"]["deadline"],
    )

    assert result["counts"] == {
        "ELIGIBILITY": 6,
        "ACTION_REQUIRED": 1,
        "CHECKLIST": 13,
        "INFORMATION": 3,
    }
    assert result["blocking_actions"] == 1
    assert len(result["items"]) == 23


def test_public_notice_validation_fails_closed_on_digest_and_pii_tampering() -> None:
    tampered = load_public_notice_seed()
    tampered["extraction"]["summary"] = "tampered"
    with pytest.raises(PublicNoticeSeedError, match="digest"):
        validate_public_notice_seed(tampered)

    pii = copy.deepcopy(load_public_notice_seed())
    pii["extraction"]["summary"] = "owner" + "@" + "example.invalid"
    pii["provenance"]["payload_sha256"] = calculate_public_notice_payload_digest(pii)
    with pytest.raises(PublicNoticeSeedError, match="privacy"):
        validate_public_notice_seed(pii)


def test_explicit_import_is_idempotent_and_excludes_private_models(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'public-seed.db').as_posix()}"
    engine = build_engine(database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    try:
        with factory() as session:
            first = import_public_notice_seed(session)
        with factory() as session:
            second = import_public_notice_seed(session)
        with factory() as session:
            assert session.scalar(select(func.count(Notice.id))) == 1
            assert session.scalar(select(func.count(NoticeVersion.id))) == 1
            assert session.scalar(select(func.count(CompanyFact.id))) == 0
            assert session.scalar(select(func.count(UserDecision.id))) == 0
            version = session.scalar(select(NoticeVersion))
            assert version is not None
            serialized = json.dumps(version.source_payload, ensure_ascii=False)
            assert "response_id" not in serialized
            assert "actual_value" not in serialized
    finally:
        engine.dispose()

    assert first.created_notices == 1
    assert first.created_versions == 1
    assert second.unchanged is True
    assert second.requirement_count == 23


def test_imported_seed_drives_requirement_policy_api_without_startup_seed(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'policy-api.db').as_posix()}"
    app = create_app(database_url=database_url, seed_synthetic=False)
    with TestClient(app) as client:
        before = client.get(
            f"/api/v1/notices/{PUBLIC_NOTICE_SOURCE_KEY}/analysis/requirement-policy"
        )
        assert before.status_code == 404
        with app.state.session_factory() as session:
            import_public_notice_seed(session)

        response = client.get(
            f"/api/v1/notices/{PUBLIC_NOTICE_SOURCE_KEY}/analysis/requirement-policy"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"] == {
        "ELIGIBILITY": 6,
        "ACTION_REQUIRED": 1,
        "CHECKLIST": 13,
        "INFORMATION": 3,
    }
    assert payload["blocking_actions"] == 1


def test_packaged_cli_seeds_fresh_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "packaged-cli.db"
    monkeypatch.setenv("PAI_LOOP_DATABASE_URL", f"sqlite:///{target.as_posix()}")

    assert public_notice_cli_main(["--create-schema"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["created_notices"] == 1
    assert report["requirement_count"] == 23
    assert report["unchanged"] is False


def test_docker_install_contains_packaged_seed_entrypoint() -> None:
    project_root = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")

    assert manifest["project"]["scripts"]["pai-loop-seed-public-notice"] == (
        "pai_loop.public_notice_cli:main"
    )
    assert manifest["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"][
        "src/pai_loop/data/public_notice_seed_v1.json"
    ] == "pai_loop/data/public_notice_seed_v1.json"
    assert "COPY src ./src" in dockerfile
    assert 'pip install --no-cache-dir ".[postgres]"' in dockerfile
