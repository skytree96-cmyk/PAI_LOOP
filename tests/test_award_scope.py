from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, raiseload, selectinload
from sqlalchemy.orm.attributes import NO_VALUE

from pai_loop.award_scope import (
    AWARD_AGENCY_UNAVAILABLE,
    AwardScope,
    award_agency_is_verifiable,
    award_title_tokens,
    derive_award_keyword,
    filter_notice_awards,
    matches_award_agency,
    normalize_award_agency,
    resolve_notice_award_scope,
)
from pai_loop.database import Base, build_engine
from pai_loop.migrations import (
    AWARD_AGENCY_METADATA_MIGRATION_ID,
    MigrationError,
    apply_additive_migrations,
    pending_migrations,
)
from pai_loop.models import AwardHistoryItem, Notice, NoticeAwardAgencyMetadata, NoticeVersion
from pai_loop.notice_freshness import analysis_basis_is_current
from pai_loop.pps_enrichment import build_notice_metadata


NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)


def _notice() -> Notice:
    return Notice(
        id="SYN-notice", notice_key="PPS-SYN-scope-000", bid_notice_no="SYN-scope",
        revision_no="000", title="SYN 교육 용역", agency="SYN 공고대행기관",
        deadline=NOW + timedelta(days=7), status="OPEN", category="용역",
    )


def _version(no: int, **metadata: object) -> NoticeVersion:
    return NoticeVersion(
        id=f"SYN-version-{no}", notice_id="SYN-notice", version_no=no,
        file_sha256=str(no) * 64,
        source_payload={
            "kind": "PPS_NOTICE_METADATA",
            "notice_identity": {"bid_notice_no": "SYN-scope", "revision_no": "000"},
            "notice_metadata": metadata,
            "attachment_manifest": [],
        },
    )


def _sidecar(**overrides: object) -> NoticeAwardAgencyMetadata:
    values = {
        "id": "SYN-sidecar", "notice_id": "SYN-notice", "notice_version_id": "SYN-version-1",
        "bid_notice_no": "SYN-scope", "revision_no": "000", "observed_at": NOW,
        "source": "PPS_NOTICE_LOOKUP", "demand_agency_name": "SYN 실제수요기관",
        "demand_agency_code": "SYN-DEMAND", "announcing_agency_name": "SYN 공고대행기관",
        "announcing_agency_code": "SYN-ANNOUNCE", **overrides,
    }
    return NoticeAwardAgencyMetadata(**values)


def test_agency_metadata_allowlist_preserves_separate_public_identities() -> None:
    raw = {
        "dminsttNm": " SYN  실제수요기관 ", "dminsttCd": "SYN-DEMAND",
        "ntceInsttNm": "SYN 공고대행기관", "ntceInsttCd": "SYN-ANNOUNCE",
        "ntceInsttOfclNm": "SYN-CONTACT-NOT-ALLOWED", "dminsttOfclTelNo": "SYN-NOT-ALLOWED",
        "untrusted": {"agency": "SYN-UNKNOWN"},
    }
    assert build_notice_metadata(raw) == {
        "demand_agency_name": "SYN 실제수요기관", "demand_agency_code": "SYN-DEMAND",
        "announcing_agency_name": "SYN 공고대행기관", "announcing_agency_code": "SYN-ANNOUNCE",
    }
    assert build_notice_metadata({"dminsttNm": {}, "dminsttCd": 123, "ntceInsttNm": [], "ntceInsttCd": False}) == {}


def test_current_metadata_is_authoritative_without_announcing_or_historical_fallback() -> None:
    notice = _notice()
    notice.versions = [_version(1, demand_agency_name="SYN 과거수요기관"), _version(2, announcing_agency_name="SYN 공고기관")]
    scope = resolve_notice_award_scope(notice)
    assert not scope.available
    assert scope.reason_code == AWARD_AGENCY_UNAVAILABLE
    assert scope.metadata_version_no == 2
    assert scope.announcing_agency_name == "SYN 공고기관"
    assert scope.demand_agency_name is None
    assert not scope.matches_award({"agency": notice.agency})


def test_current_explicit_demand_metadata_wins_and_does_not_merge_sidecar_fields() -> None:
    notice = _notice()
    notice.versions = [_version(1, demand_agency_name=" ＳＹＮ  수요기관 ")]
    notice.award_agency_metadata = [_sidecar()]
    scope = resolve_notice_award_scope(notice)
    assert scope.available and scope.reason_code is None
    assert scope.demand_agency_name == "SYN 수요기관"
    assert scope.demand_agency_code is None
    assert scope.announcing_agency_name is None


def test_plain_scope_input_keeps_its_explicit_versions_contract() -> None:
    notice = SimpleNamespace(
        bid_notice_no="SYN-scope", revision_no="000",
        versions=[_version(1, demand_agency_name="SYN 수요기관")],
    )
    assert resolve_notice_award_scope(notice).demand_agency_name == "SYN 수요기관"


def test_scope_reads_only_metadata_or_reuses_the_already_loaded_full_relationship() -> None:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            notice = _notice()
            extraction = _version(2)
            extraction.source_payload = {"kind": "OPENAI_REQUIREMENT_EXTRACTION", "result": "SYN-LARGE" * 20_000}
            notice.versions = [_version(1, demand_agency_name="SYN 수요기관"), extraction]
            session.add(notice)
            session.commit()
        with Session(engine) as session:
            notice = session.scalar(select(Notice).options(
                selectinload(Notice.award_scope_versions), raiseload(Notice.versions),
            ))
            assert inspect(notice).attrs.versions.loaded_value is NO_VALUE
            assert [version.version_no for version in notice.award_scope_versions] == [1]
            assert resolve_notice_award_scope(notice).demand_agency_name == "SYN 수요기관"
            assert inspect(notice).attrs.versions.loaded_value is NO_VALUE
            assert not any(isinstance(row, NoticeVersion) and row.version_no == 2 for row in session.identity_map.values())
        with Session(engine) as session:
            notice = session.scalar(select(Notice).options(
                selectinload(Notice.versions), raiseload(Notice.award_scope_versions),
            ))
            assert len(notice.versions) == 2
            assert resolve_notice_award_scope(notice).demand_agency_name == "SYN 수요기관"
            assert inspect(notice).attrs.award_scope_versions.loaded_value is NO_VALUE
    finally:
        engine.dispose()


def test_no_current_metadata_or_mismatching_declared_identity_cannot_resolve_scope() -> None:
    notice = _notice()
    notice.award_agency_metadata = [_sidecar()]
    assert not resolve_notice_award_scope(notice).available
    version = _version(1, demand_agency_name="SYN 실제수요기관")
    version.source_payload["notice_identity"]["revision_no"] = "001"
    notice.versions = [version]
    assert not resolve_notice_award_scope(notice).available


@pytest.mark.parametrize("field,value", [
    ("notice_id", "SYN-other"), ("notice_version_id", "SYN-stale-version"),
    ("bid_notice_no", "SYN-other"), ("revision_no", "001"), ("source", "SYN-UNVERIFIED"),
])
def test_sidecar_requires_exact_notice_current_metadata_and_verified_source(field: str, value: str) -> None:
    notice = _notice()
    notice.versions = [_version(1)]
    notice.award_agency_metadata = [_sidecar(**{field: value})]
    assert not resolve_notice_award_scope(notice).available


def test_latest_bound_sidecar_is_used_and_new_metadata_invalidates_it() -> None:
    notice = _notice()
    notice.versions = [_version(1)]
    notice.award_agency_metadata = [_sidecar()]
    scope = resolve_notice_award_scope(notice)
    assert scope.available and scope.demand_agency_code == "SYN-DEMAND"
    notice.award_agency_metadata.append(_sidecar(
        id="SYN-sidecar-new", observed_at=NOW + timedelta(seconds=1),
        demand_agency_name=None, demand_agency_code=None,
    ))
    assert not resolve_notice_award_scope(notice).available
    notice.versions.append(_version(2))
    assert not resolve_notice_award_scope(notice).available


def test_agency_comparison_preserves_punctuation_and_code_authority() -> None:
    assert normalize_award_agency(" ＳＹＮ　기관 (Ａ) ") == "syn기관(a)"
    assert normalize_award_agency(None) == ""
    assert normalize_award_agency(123) == ""
    scope = AwardScope(demand_agency_name="SYN 기관 (A)", demand_agency_code="SYN-CODE", reason_code=None)
    assert scope.matches_award({"agency": "SYN 변경된명칭", "demand_agency_code": "syn-code"})
    assert not scope.matches_award({"agency": "SYN 기관 (A)", "demand_agency_code": "SYN-OTHER"})
    assert scope.matches_award({"agency": "SYN기관(A)"})
    assert not scope.matches_award({"agency": "SYN 기관 A"})
    assert not scope.matches_award({"agency": "SYN 기관 (A) 분원"})
    assert not matches_award_agency({"agency": "SYN 기관"}, demand_agency_code="SYN-CODE")
    assert matches_award_agency({"demand_agency_name": "SYN 기관"}, demand_agency_name="SYN기관")
    assert not matches_award_agency({"agency": "SYN 기관"})
    stored = AwardHistoryItem(agency="SYN 이전기관명", demand_agency_code="SYN-CODE")
    assert scope.matches_award(stored)
    stored.demand_agency_code = None
    assert not scope.matches_award(stored)


def test_missing_agency_evidence_is_not_an_established_mismatch() -> None:
    expected = {"demand_agency_name": "SYN 기관", "demand_agency_code": "SYN-CODE"}
    assert not award_agency_is_verifiable({}, **expected)
    assert not award_agency_is_verifiable({"agency": "SYN 기관"}, demand_agency_code="SYN-CODE")
    assert not award_agency_is_verifiable({"demand_agency_code": "SYN-CODE"}, demand_agency_name="SYN 기관")
    assert award_agency_is_verifiable({"agency": "SYN 다른기관"}, **expected)
    assert award_agency_is_verifiable({"demand_agency_code": "SYN-OTHER"}, **expected)
    assert award_agency_is_verifiable(AwardHistoryItem(agency="SYN 기관"), **expected)
    assert not award_agency_is_verifiable({"agency": "SYN 기관"})


def test_award_keyword_rules_remove_year_suffixes_and_generic_notice_terms() -> None:
    assert award_title_tokens("2026학년도 2025년도 2024년 2023 긴급 재공고 SYN 교육 과정 위탁운영") == ["syn", "교육", "과정"]
    assert derive_award_keyword("2026학년도 SYN AI 교육 강화 용역") == "syn ai 교육"
    assert len(derive_award_keyword("SYN" * 100)) == 100
    with pytest.raises(ValueError, match="검색 키워드"):
        derive_award_keyword("2026년도 긴급 공고 용역")


def test_common_award_filter_requires_both_current_agency_and_all_keyword_terms() -> None:
    notice = _notice()
    notice.title = "2026학년도 SYN 교육 과정 용역"
    notice.versions = [_version(1, demand_agency_name="SYN 수요기관", demand_agency_code="SYN-CODE")]
    matching = AwardHistoryItem(title="SYN 신규 교육 심화 과정", agency="SYN 변경된명칭", demand_agency_code="SYN-CODE")
    rows = [matching,
            AwardHistoryItem(title=matching.title, agency="SYN 다른기관", demand_agency_code="SYN-OTHER"),
            AwardHistoryItem(title="SYN 교육", agency="SYN 수요기관"),
            AwardHistoryItem(title=matching.title, agency=""),
            {"title": "SYN 교육 심화 과정", "agency": "SYN수요기관"}]
    assert filter_notice_awards(notice, iter(rows)) == [rows[0], rows[4]]
    assert len(rows) == 5 and rows[0] is matching
    notice.title = "2026학년도 용역"
    assert filter_notice_awards(notice, rows) == []
    notice.title = "SYN 교육 과정"
    notice.versions.append(_version(2))
    assert filter_notice_awards(notice, rows) == []


def test_sidecar_persistence_preserves_analysis_basis_and_versions() -> None:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            notice = _notice()
            notice.versions = [_version(1)]
            original_payload = copy.deepcopy(notice.versions[0].source_payload)
            session.add(notice)
            session.commit()
            assert analysis_basis_is_current(notice, "SYN-version-1")
            notice.award_agency_metadata.append(_sidecar())
            session.commit()
            session.expire_all()
            assert resolve_notice_award_scope(notice).demand_agency_name == "SYN 실제수요기관"
            assert len(notice.versions) == 1
            assert notice.versions[0].source_payload == original_payload
            assert analysis_basis_is_current(notice, "SYN-version-1")
    finally:
        engine.dispose()


def test_agency_migration_is_additive_idempotent_and_leaves_legacy_codes_unknown() -> None:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            notice = _notice()
            notice.versions = [_version(1)]
            notice.award_history = [AwardHistoryItem(
                id="SYN-award", external_identity="SYN-award", bid_notice_no="SYN-old",
                revision_no="000", title="SYN 과거교육", agency="SYN 수요기관", winner_name="SYN 기업",
                similarity_score=99, source="PPS",
            )]
            session.add(notice)
            session.commit()
        with engine.begin() as connection:
            connection.exec_driver_sql('DROP TABLE "notice_award_agency_metadata"')
            connection.exec_driver_sql('ALTER TABLE "award_history_items" DROP COLUMN "demand_agency_code"')
        assert AWARD_AGENCY_METADATA_MIGRATION_ID in pending_migrations(engine)
        assert AWARD_AGENCY_METADATA_MIGRATION_ID in apply_additive_migrations(engine)
        assert apply_additive_migrations(engine) == []
        assert pending_migrations(engine) == []
        with Session(engine) as session:
            award = session.scalar(select(AwardHistoryItem))
            assert award.id == "SYN-award" and award.demand_agency_code is None
            assert award.agency == "SYN 수요기관"
            assert session.scalar(select(NoticeVersion)).version_no == 1
        assert "notice_award_agency_metadata" in inspect(engine).get_table_names()
        with engine.begin() as connection:
            connection.exec_driver_sql('DROP TABLE "notice_award_agency_metadata"')
        with pytest.raises(MigrationError, match="notice_award_agency_metadata is missing"):
            pending_migrations(engine)
    finally:
        engine.dispose()
