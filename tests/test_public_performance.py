from __future__ import annotations

import copy
import hashlib
import json

import pytest

from pai_loop.public_performance import (
    PUBLIC_RECORD_FIELDS,
    PublicPerformanceSeedError,
    build_public_seed,
    load_public_performance_seed,
    query_public_performance,
    validate_public_performance_seed,
)


def _synthetic_identifiers() -> dict[str, str]:
    return {
        "email": "synthetic.person" + "@" + "example.invalid",
        "phone": "010" + "-" + "2345" + "-" + "6789",
        "compact_phone": "010" + "2345" + "6789",
        "business_id": "123" + "-" + "45" + "-" + "67890",
        "address": "가상시 가상구 테스트로 " + "123",
    }


def _synthetic_source_row() -> dict[str, object]:
    identifiers = _synthetic_identifiers()
    return {
        "project_name": "Synthetic public project",
        "project_overview_redacted": (
            "Public overview 사업 총괄 PM: (가명자) 명사특강: 또가명(분야) "
            "예시명 작가 담당자 SyntheticName "
            + identifiers["email"]
            + " "
            + identifiers["phone"]
            + " "
            + identifiers["business_id"]
            + " "
            + identifiers["address"]
        ),
        "contract_date_iso": "2025-12-31",
        "contract_amount_source": "1234567",
        "agency": "Synthetic agency",
        "keywords_source": "education, consulting, " + identifiers["compact_phone"],
        "division_source": "Synthetic division",
        "source_row": 999,
        "source_record_id": "MUST_NOT_PUBLISH",
        "contract_number": "MUST_NOT_PUBLISH",
        "address": "MUST_NOT_PUBLISH",
        "contact": "MUST_NOT_PUBLISH",
    }


def test_build_public_seed_uses_strict_allowlist_and_redacts_free_text() -> None:
    identifiers = _synthetic_identifiers()
    result = build_public_seed([_synthetic_source_row()], dataset_version="test-v1")
    serialized = json.dumps(result.payload, ensure_ascii=False)
    record = result.payload["records"][0]

    assert tuple(record) == PUBLIC_RECORD_FIELDS
    assert result.output_records == 1
    assert result.direct_identifier_findings == 0
    assert result.payload["aggregate"]["direct_identifier_findings"] == 0
    assert "[비식별]" in serialized
    assert all(value not in serialized for value in identifiers.values())
    assert "MUST_NOT_PUBLISH" not in serialized
    assert "가명자" not in serialized
    assert "또가명" not in serialized
    assert "예시명" not in serialized
    assert not {
        "source_row",
        "source_path",
        "source_record_id",
        "contract_number",
        "address",
        "contact",
        "phone",
        "email",
        "person",
    }.intersection(record)


def test_packaged_public_performance_snapshot_is_complete_and_safe() -> None:
    seed = load_public_performance_seed()
    aggregate = seed["aggregate"]
    records = seed["records"]

    assert seed["classification"] == "PUBLIC_DERIVED"
    assert seed["dataset_version"] == "2026.08.17-v3"
    assert seed["policy_version"] == "public-allowlist-redaction-1.2.0"
    assert aggregate["record_count"] == 1182
    assert aggregate["direct_identifier_findings"] == 0
    assert len(records) == 1182
    assert len({record["record_key"] for record in records}) == 1182
    assert all(tuple(record) == PUBLIC_RECORD_FIELDS for record in records)
    assert sum(aggregate["year_counts"].values()) == aggregate["field_coverage"]["contract_year"]
    assert sum(aggregate["division_counts"].values()) == aggregate["field_coverage"]["division"]
    assert len(aggregate["division_counts"]) >= 20


def test_public_seed_validation_fails_closed_on_tampering() -> None:
    seed = copy.deepcopy(load_public_performance_seed())
    seed["records"][0]["agency"] = "tampered"

    with pytest.raises(PublicPerformanceSeedError, match="digest"):
        validate_public_performance_seed(seed)

    aggregate_tamper = copy.deepcopy(load_public_performance_seed())
    aggregate_tamper["aggregate"]["division_counts"]["unreviewed aggregate value"] = 1
    with pytest.raises(PublicPerformanceSeedError, match="aggregate"):
        validate_public_performance_seed(aggregate_tamper)

    provenance_tamper = copy.deepcopy(load_public_performance_seed())
    provenance_tamper["provenance"]["unexpected"] = "internal metadata"
    with pytest.raises(PublicPerformanceSeedError, match="provenance allowlist"):
        validate_public_performance_seed(provenance_tamper)


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "사업 총괄 PM: (가명자)",
        "명사특강: 또가명(분야)",
        "강연자: 예시명",
        "연사(가명자)",
        "발표자 가명자",
        "교수 또가명",
        "예시명 작가",
        "또가명(교수)",
    ],
)
def test_public_seed_validation_rejects_unredacted_role_names(
    unsafe_text: str,
) -> None:
    seed = copy.deepcopy(load_public_performance_seed())
    seed["records"][0]["overview"] = unsafe_text
    canonical = json.dumps(
        seed["records"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    seed["provenance"]["records_sha256"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()

    with pytest.raises(PublicPerformanceSeedError, match="identifier"):
        validate_public_performance_seed(seed)


def test_public_performance_query_is_bounded_and_allowlisted() -> None:
    result = query_public_performance(year=2025, limit=3, offset=0)

    assert result["total"] == 92
    assert len(result["records"]) == 3
    assert all(tuple(record) == PUBLIC_RECORD_FIELDS for record in result["records"])


def test_public_performance_api_exposes_summary_and_bounded_records(client) -> None:
    summary_response = client.get("/api/v1/performance/summary")
    list_response = client.get("/api/v1/performance", params={"year": 2025, "limit": 2})

    assert summary_response.status_code == 200
    assert summary_response.json()["aggregate"]["record_count"] == 1182
    assert summary_response.json()["aggregate"]["direct_identifier_findings"] == 0
    assert summary_response.json()["aggregate"]["division_counts"]
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 92
    assert len(list_response.json()["records"]) == 2
