from __future__ import annotations

from dataclasses import replace
import json
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from pai_loop import performance_records
from pai_loop.models import CompanyPerformanceRecord, Evidence
from tools.import_private_performance_records import (
    ImportErrorDetail,
    _amount,
    normalize_workbook,
    parse_period,
)


HEADERS = [
    "식별번호",
    "공동계약여부",
    "용역명",
    "용역개요",
    "계약번호",
    "계약일자",
    "계약기간",
    "계약금액",
    "이행비율",
    "실적",
    "발주처",
    "주소",
    "담당부서",
    "담당자",
    "전화번호",
    "키워드",
    "수행부서",
]
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _column_name(index: int) -> str:
    output = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        output = chr(65 + remainder) + output
    return output


def _cell(reference: str, value: object) -> str:
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    return (
        f'<c r="{reference}" t="inlineStr"><is><t>'
        f"{escape(str(value))}</t></is></c>"
    )


def _write_minimal_xlsx(
    path: Path,
    rows: list[list[object]],
    *,
    date1904: bool = False,
) -> None:
    row_xml = []
    for row_number, values in enumerate(rows, start=1):
        cells = "".join(
            _cell(f"{_column_name(column)}{row_number}", value)
            for column, value in enumerate(values, start=1)
        )
        row_xml.append(f'<row r="{row_number}">{cells}</row>')
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(row_xml)}</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        + ('<workbookPr date1904="1"/>' if date1904 else "")
        + '<sheets><sheet name="프로젝트DB" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)


def _row(**overrides: object) -> list[object]:
    values: list[object] = [
        "SYN-ID",
        "",
        "SYN 교육 행사 운영",
        "합성 교육 행사",
        "SYN-CONTRACT",
        "2024.01.01",
        "2024.01.01 ~ 2024.12.31",
        500_000_000,
        100,
        500_000_000,
        "SYN 공공기관",
        "DO-NOT-UPLOAD-ADDRESS",
        "SYN 담당부서",
        "DO-NOT-UPLOAD-PERSON",
        "DO-NOT-UPLOAD-PHONE",
        "교육, 행사, 교육",
        "SYN 수행부서",
    ]
    for column, value in overrides.items():
        values[ord(column) - ord("A")] = value
    return values


def test_private_xlsx_importer_normalizes_amount_share_and_omits_contacts(tmp_path: Path) -> None:
    workbook = tmp_path / "private-source.xlsx"
    _write_minimal_xlsx(
        workbook,
        [
            HEADERS,
            _row(A="SYN-1", E="SYN-C-1", H=500_000_000, I=0.6, J=300_000_000),
            _row(A="SYN-2", E="SYN-C-2", C="SYN 단독 이행", I=1, J=500_000_000),
            _row(A="SYN-PLACEHOLDER", C="프로젝트 없음"),
            _row(A="SYN-3", E="SYN-C-3", C="SYN 보완 대상", G="", I="미확인", K=""),
        ],
    )

    bundle = normalize_workbook(workbook)

    assert bundle.source_data_rows == 4
    assert bundle.placeholder_rows == 1
    assert len(bundle.records) == 3
    assert bundle.validated_rows == 2
    assert bundle.draft_rows == 1
    assert bundle.joint_rows == 1

    shared = bundle.records[0]
    assert shared.fields["contract_amount"] == 500_000_000
    assert shared.fields["gross_contract_amount_krw"] == 500_000_000
    assert shared.fields["recognized_performance_amount_krw"] == 300_000_000
    assert shared.fields["recognized_amount_is_net_of_share"] is True
    assert shared.fields["share_pct"] == 60
    assert shared.joint_procurement is True
    assert shared.fields["keywords"] == ["교육", "행사"]

    sole = bundle.records[1]
    assert sole.fields["share_pct"] == 100
    assert sole.joint_procurement is False
    assert bundle.records[2].fields["record_status"] == "DRAFT"
    assert bundle.records[2].fields["share_pct"] == 0
    assert set(bundle.records[2].issues) >= {
        "MISSING_AGENCY",
        "INVALID_CONTRACT_PERIOD",
        "INVALID_SHARE",
    }

    serialized = json.dumps([item.api_payload() for item in bundle.records], ensure_ascii=False)
    assert "DO-NOT-UPLOAD-ADDRESS" not in serialized
    assert "DO-NOT-UPLOAD-PERSON" not in serialized
    assert "DO-NOT-UPLOAD-PHONE" not in serialized
    assert str(workbook) not in serialized
    assert "private-evidence://company-performance/" in serialized
    assert "#프로젝트DB!A2:Q2" in serialized

    revised_workbook = tmp_path / "private-source-revised.xlsx"
    _write_minimal_xlsx(
        revised_workbook,
        [
            HEADERS,
            _row(A="SYN-1", E="SYN-C-1", H=510_000_000, I=0.6, J=306_000_000),
        ],
    )
    revised = normalize_workbook(revised_workbook)
    assert revised.source_sha256 != bundle.source_sha256
    assert revised.records[0].row_key == shared.row_key
    assert revised.records[0].fields["recognized_performance_amount_krw"] == 306_000_000


def test_period_normalizer_repairs_only_explicit_or_unambiguous_ranges() -> None:
    assert parse_period("2024.01.02 ~ 2024.12.31") == (
        date(2024, 1, 2),
        date(2024, 12, 31),
    )
    assert parse_period("2024년 1월 2일 ∼ 2024년 12월 31일") == (
        date(2024, 1, 2),
        date(2024, 12, 31),
    )
    assert parse_period("2024.01.02 ~ 12.31") == (
        date(2024, 1, 2),
        date(2024, 12, 31),
    )
    assert parse_period("24.01.02 ~ 24.12.31") == (
        date(2024, 1, 2),
        date(2024, 12, 31),
    )
    assert parse_period("20240102 ~ 20241231") == (
        date(2024, 1, 2),
        date(2024, 12, 31),
    )
    # A single date, duration, year-crossing abbreviated end, or month-only
    # range does not prove both exact period boundaries.
    assert parse_period("2024.12.31") == (None, None)
    assert parse_period("착공 후 365일") == (None, None)
    assert parse_period("2024.12.01 ~ 01.31") == (None, None)
    assert parse_period("2024.01 ~ 2024.12") == (None, None)


def test_importer_rejects_abbreviated_amount_units_and_1904_epoch(tmp_path: Path) -> None:
    assert _amount("500,000,000원") == 500_000_000
    assert _amount("3억원") is None

    workbook = tmp_path / "date1904.xlsx"
    _write_minimal_xlsx(workbook, [HEADERS, _row()], date1904=True)
    with pytest.raises(ImportErrorDetail, match="1904"):
        normalize_workbook(workbook)


def test_private_evidence_runbook_uses_scoped_attested_import_contract() -> None:
    runbook = (
        PROJECT_ROOT / "docs" / "private-quantitative-evidence-runbook-2026-09-02.md"
    ).read_text(encoding="utf-8")

    assert "--private-token-env PAI_LOOP_PRIVATE_EVIDENCE_TOKEN" in runbook
    assert "--attest-source-semantics" in runbook
    assert "--operator-pin-env" not in runbook
    assert "`DRAFT`, `share_pct=0`" in runbook
    assert "100%일 수 있다" not in runbook
    assert "수동 행으로 점수를 대신 계산하지 않는다" in runbook


def _bulk_payload(
    *,
    recognized_amount: int = 300_000_000,
    project_name: str = "SYN 민간 원본 실적",
) -> dict[str, object]:
    source_sha256 = "a" * 64
    return {
        "source_sha256": source_sha256,
        "sheet_name": "프로젝트DB",
        "schema_version": "kma-private-performance-v1",
        "source_row_count": 1,
        "batch_index": 0,
        "batch_count": 1,
        "verification_attestation": (
            "OPERATOR_CONFIRMED_CERTIFICATE_BACKED_COMPLETED_VAT_INCLUDED_"
            "RECOGNIZED_AMOUNT_NET_OF_SHARE"
        ),
        "records": [
            {
                "source_row": 2,
                "row_key": "b" * 64,
                "fields": {
                    "record_status": "VALIDATED",
                    "project_name": project_name,
                    "agency": "SYN 공공기관",
                    "division": "SYN 수행부서",
                    "contract_date": "2024-01-01",
                    "start_date": "2024-01-01",
                    "end_date": "2024-12-31",
                    "contract_amount": 500_000_000,
                    "gross_contract_amount_krw": 500_000_000,
                    "recognized_performance_amount_krw": recognized_amount,
                    "recognized_amount_is_net_of_share": True,
                    "vat_basis": "INCLUDED",
                    "completed": True,
                    "share_pct": 60,
                    "certificate_status": "ISSUED",
                    "evidence_reference": "private-evidence://client-placeholder",
                    "keywords": ["교육"],
                },
            }
        ],
    }


@pytest.mark.parametrize(
    ("header_name", "header_value"),
    [
        (
            "X-PAI-Private-Evidence-Token",
            "synthetic-private-evidence-token-00000001",
        ),
        ("X-PAI-LOOP-API-KEY", "server-only-api-key"),
    ],
)
def test_private_authority_search_query_is_rejected_and_scrubbed_before_logging(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    header_name: str,
    header_value: str,
) -> None:
    client.app.state.settings = replace(
        client.app.state.settings,
        environment="production",
        api_key="server-only-api-key",
        private_evidence_token="synthetic-private-evidence-token-00000001",
    )
    observed_queries: list[bytes] = []
    original_operator_access = performance_records._operator_access

    def observe_operator_access(request, *, mutation: bool):
        observed_queries.append(request.scope["query_string"])
        return original_operator_access(request, mutation=mutation)

    monkeypatch.setattr(
        performance_records,
        "_operator_access",
        observe_operator_access,
    )
    private_marker = "SYN-PRIVATE-QUERY-MARKER"
    response = client.get(
        "https://testserver/api/v1/performance-records",
        headers={header_name: header_value},
        params={"q": private_marker},
    )

    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert private_marker not in response.text
    assert observed_queries == [b""]


def test_private_bulk_import_requires_operator_or_server_key_and_is_idempotent(
    client: TestClient,
) -> None:
    client.app.state.settings = replace(
        client.app.state.settings,
        environment="production",
        public_read_only=True,
        public_manual_analysis_enabled=True,
        public_manual_analysis_token="2468",
        api_key="server-only-api-key",
        private_evidence_token="synthetic-private-evidence-token-00000001",
    )
    private_marker = "SYN-PRIVATE-ROW-DO-NOT-ECHO"
    payload = _bulk_payload(project_name=private_marker)
    source_sha256 = str(payload["source_sha256"])
    private_headers = {
        "X-PAI-Private-Evidence-Token": (
            "synthetic-private-evidence-token-00000001"
        )
    }
    missing_auth = client.post(
        "/api/v1/performance-records/private-import",
        json=payload,
    )
    assert missing_auth.status_code == 401
    assert missing_auth.headers["cache-control"] == "no-store"
    assert private_marker not in missing_auth.text
    assert source_sha256 not in missing_auth.text

    pin_headers = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "X-PAI-Manual-Token": "2468",
    }
    pin_import = client.post(
        "https://testserver/api/v1/performance-records/private-import",
        headers=pin_headers,
        json=payload,
    )
    assert pin_import.status_code == 401
    assert pin_import.headers["cache-control"] == "no-store"
    assert private_marker not in pin_import.text
    assert source_sha256 not in pin_import.text

    invalid_payload = _bulk_payload(project_name=private_marker)
    invalid_payload["verification_attestation"] = private_marker
    invalid = client.post(
        "https://testserver/api/v1/performance-records/private-import",
        headers=private_headers,
        json=invalid_payload,
    )
    assert invalid.status_code == 422
    assert invalid.headers["cache-control"] == "no-store"
    assert invalid.json() == {
        "detail": "비공개 증빙 입력 형식을 확인해 주세요.",
        "code": "PRIVATE_EVIDENCE_VALIDATION_ERROR",
    }
    assert private_marker not in invalid.text
    assert source_sha256 not in invalid.text

    private_import = client.post(
        "https://testserver/api/v1/performance-records/private-import",
        headers=private_headers,
        json=payload,
    )
    assert private_import.status_code == 200, private_import.text
    assert private_import.json()["created"] == 1

    headers = {"X-PAI-LOOP-API-KEY": "server-only-api-key"}
    first = client.post(
        "/api/v1/performance-records/private-import", headers=headers, json=payload
    )
    assert first.status_code == 200, first.text
    assert first.json() == {
        "evidence_registered": False,
        "created": 0,
        "updated": 0,
        "unchanged": 1,
        "archived": 0,
        "activated": 0,
        "import_complete": True,
    }
    assert payload["source_sha256"] not in first.text

    replay = client.post(
        "/api/v1/performance-records/private-import", headers=headers, json=payload
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == {
        "evidence_registered": False,
        "created": 0,
        "updated": 0,
        "unchanged": 1,
        "archived": 0,
        "activated": 0,
        "import_complete": True,
    }

    changed = client.post(
        "/api/v1/performance-records/private-import",
        headers=headers,
        json=_bulk_payload(
            recognized_amount=310_000_000,
            project_name=private_marker,
        ),
    )
    assert changed.status_code == 409
    assert changed.headers["cache-control"] == "no-store"
    assert private_marker not in changed.text
    assert source_sha256 not in changed.text

    with client.app.state.session_factory() as session:
        evidence = session.scalar(
            select(Evidence).where(Evidence.evidence_type == "PRIVATE_PERFORMANCE_WORKBOOK")
        )
        assert evidence is not None
        assert evidence.evidence_type == "PRIVATE_PERFORMANCE_WORKBOOK"
        assert evidence.source_location.startswith("private-evidence://")
        assert evidence.metadata_json == {
            "classification": "PRIVATE_SOURCE_METADATA",
            "raw_document_stored": False,
            "schema_version": "kma-private-performance-v1",
            "sheet_name": "프로젝트DB",
            "row_count": 1,
            "verification_attestation": (
                "OPERATOR_CONFIRMED_CERTIFICATE_BACKED_COMPLETED_VAT_INCLUDED_"
                "RECOGNIZED_AMOUNT_NET_OF_SHARE"
            ),
            "expected_batch_count": 1,
            "received_batches": [0],
        }
        record = session.scalar(select(CompanyPerformanceRecord))
        assert record is not None
        assert record.contract_amount == 500_000_000
        assert record.gross_contract_amount_krw == 500_000_000
        assert record.recognized_performance_amount_krw == 300_000_000
        assert record.recognized_amount_is_net_of_share is True
        assert record.share_pct == 60
        assert record.revision == 2
        assert record.evidence_reference.endswith("#프로젝트DB!A2:Q2")


def test_manual_pin_cannot_validate_or_access_private_performance_records(
    client: TestClient,
) -> None:
    client.app.state.settings = replace(
        client.app.state.settings,
        environment="production",
        public_read_only=True,
        public_manual_analysis_enabled=True,
        public_manual_analysis_token="2468",
        api_key="server-only-api-key",
        private_evidence_token="synthetic-private-evidence-token-00000001",
    )
    pin_headers = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "X-PAI-Manual-Token": "2468",
    }
    private_headers = {
        "X-PAI-Private-Evidence-Token": (
            "synthetic-private-evidence-token-00000001"
        )
    }
    draft_payload = {
        "idempotency_key": "manual-boundary-draft-001",
        "record_status": "DRAFT",
        "project_name": "SYN 공개 수동 초안",
        "agency": "SYN 공개 기관",
        "division": "SYN 공개 부서",
        "contract_date": "2024-01-01",
        "evidence_reference": "SYN 공개 근거",
    }
    created = client.post(
        "https://testserver/api/v1/performance-records",
        headers=pin_headers,
        json=draft_payload,
    )
    assert created.status_code == 201, created.text
    draft = created.json()["record"]

    create_marker = "SYN-PRIVATE-VALIDATED-CREATE-MARKER"
    rejected_create = client.post(
        "https://testserver/api/v1/performance-records",
        headers=pin_headers,
        json={
            **draft_payload,
            "idempotency_key": "manual-boundary-validated-001",
            "record_status": "VALIDATED",
            "project_name": create_marker,
        },
    )
    assert rejected_create.status_code == 403
    assert rejected_create.headers["cache-control"] == "no-store"
    assert create_marker not in rejected_create.text

    promote_marker = "SYN-PRIVATE-VALIDATED-PROMOTE-MARKER"
    rejected_promotion = client.patch(
        f"https://testserver/api/v1/performance-records/{draft['id']}",
        headers=pin_headers,
        json={
            "expected_updated_at": draft["updated_at"],
            "record_status": "VALIDATED",
            "overview": promote_marker,
        },
    )
    assert rejected_promotion.status_code == 403
    assert rejected_promotion.headers["cache-control"] == "no-store"
    assert promote_marker not in rejected_promotion.text

    private_marker = "SYN-PRIVATE-PIN-VISIBILITY-MARKER"
    imported = client.post(
        "https://testserver/api/v1/performance-records/private-import",
        headers=private_headers,
        json=_bulk_payload(project_name=private_marker),
    )
    assert imported.status_code == 200, imported.text
    with client.app.state.session_factory() as session:
        private_record = session.scalar(
            select(CompanyPerformanceRecord).where(
                CompanyPerformanceRecord.source == "PRIVATE_IMPORT"
            )
        )
        assert private_record is not None
        private_record_id = private_record.id

    pin_listing = client.get(
        "https://testserver/api/v1/performance-records",
        headers=pin_headers,
    )
    assert pin_listing.status_code == 200
    assert pin_listing.headers["cache-control"] == "no-store"
    assert pin_listing.json()["total"] == 1
    assert pin_listing.json()["records"][0]["id"] == draft["id"]
    assert private_marker not in pin_listing.text

    patch_marker = "SYN-PRIVATE-PIN-PATCH-MARKER"
    pin_patch = client.patch(
        f"https://testserver/api/v1/performance-records/{private_record_id}",
        headers=pin_headers,
        json={
            "expected_updated_at": "2026-09-02T00:00:00Z",
            "project_name": patch_marker,
        },
    )
    assert pin_patch.status_code == 403
    assert pin_patch.headers["cache-control"] == "no-store"
    assert private_marker not in pin_patch.text
    assert patch_marker not in pin_patch.text

    strong_patch = client.patch(
        f"https://testserver/api/v1/performance-records/{private_record_id}",
        headers=private_headers,
        json={
            "expected_updated_at": "2026-09-02T00:00:00Z",
            "project_name": patch_marker,
        },
    )
    assert strong_patch.status_code == 409
    assert strong_patch.headers["cache-control"] == "no-store"
    assert private_marker not in strong_patch.text
    assert patch_marker not in strong_patch.text


def test_private_bulk_import_stays_draft_until_all_batches_arrive_and_replaces_prior_source(
    client: TestClient,
) -> None:
    client.app.state.settings = replace(
        client.app.state.settings,
        api_key="server-only-api-key",
    )
    headers = {"X-PAI-LOOP-API-KEY": "server-only-api-key"}
    first = _bulk_payload()
    first["source_row_count"] = 2
    first["batch_count"] = 2
    response = client.post(
        "/api/v1/performance-records/private-import",
        headers=headers,
        json=first,
    )
    assert response.status_code == 200, response.text
    assert response.json()["import_complete"] is False
    assert response.json()["activated"] == 0
    with client.app.state.session_factory() as session:
        pending = session.scalar(select(CompanyPerformanceRecord))
        assert pending is not None
        assert pending.record_status == "DRAFT"
        assert pending.source == "PRIVATE_IMPORT_PENDING_VALIDATED"

    second = _bulk_payload(recognized_amount=400_000_000)
    second["source_row_count"] = 2
    second["batch_index"] = 1
    second["batch_count"] = 2
    second["records"][0]["source_row"] = 3
    second["records"][0]["row_key"] = "c" * 64
    response = client.post(
        "/api/v1/performance-records/private-import",
        headers=headers,
        json=second,
    )
    assert response.status_code == 200, response.text
    assert response.json()["import_complete"] is True
    assert response.json()["activated"] == 2
    with client.app.state.session_factory() as session:
        active = list(
            session.scalars(
                select(CompanyPerformanceRecord).where(
                    CompanyPerformanceRecord.record_status == "VALIDATED"
                )
            ).all()
        )
        assert len(active) == 2
        assert {item.source for item in active} == {"PRIVATE_IMPORT"}

    replacement = _bulk_payload(recognized_amount=310_000_000)
    replacement["source_sha256"] = "d" * 64
    response = client.post(
        "/api/v1/performance-records/private-import",
        headers=headers,
        json=replacement,
    )
    assert response.status_code == 200, response.text
    assert response.json()["archived"] == 2
    assert response.json()["activated"] == 1
    with client.app.state.session_factory() as session:
        active_count = session.scalar(
            select(CompanyPerformanceRecord).where(
                CompanyPerformanceRecord.record_status == "VALIDATED"
            ).with_only_columns(func.count())
        )
        assert active_count == 1
