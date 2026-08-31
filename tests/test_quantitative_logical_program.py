from __future__ import annotations

import pytest

from pai_loop.integrations.openai_extraction import (
    ExtractionPayload,
    QuantitativeTableCandidate,
)
from pai_loop.quantitative_rule_extraction import (
    AttachmentDocumentBinding,
    ImmutableEvidenceAnchor,
    ImmutableQuantitativeCase,
    ImmutableQuantitativeRecognitionCondition,
    ImmutableQuantitativeRuleCandidate,
    ImmutableQuantitativeTable,
    ImmutableQuantitativeThreshold,
    QuantitativeCandidateProfile,
)
from pai_loop.quantitative_scoring import quantitative_request_from_candidate_profile


ATTACHMENT_ID = "ATT-LOGICAL-PROGRAM"
NOTICE_ATTACHMENT_ID = "ATT-BUSAN-NOTICE"
RFP_ATTACHMENT_ID = "ATT-BUSAN-RFP"
SCOPE_ATTACHMENT_ID = "ATT-BUSAN-SCOPE"


def _anchor(
    page: int,
    quote: str,
    *,
    attachment_id: str = ATTACHMENT_ID,
    section: str | None = None,
) -> ImmutableEvidenceAnchor:
    return ImmutableEvidenceAnchor(
        attachment_id=attachment_id,
        page=page,
        section=section or f"section-{page}",
        quote=quote,
        confidence=1,
    )


def _condition(
    literal: str,
    *,
    page: int,
    attachment_id: str,
    section: str,
) -> ImmutableQuantitativeRecognitionCondition:
    return ImmutableQuantitativeRecognitionCondition(
        literal=literal,
        evidence=_anchor(
            page,
            literal,
            attachment_id=attachment_id,
            section=section,
        ),
    )


def _candidate(
    *,
    table_id: str,
    criterion_id: str,
    metric: str,
    unit: str,
    fact_key: str,
    max_points: float,
    page: int,
    attachment_id: str = ATTACHMENT_ID,
    label: str | None = None,
    literal: str | None = None,
    threshold_literal: str | None = None,
    threshold_value: float = 1,
    section: str | None = None,
    recognition_conditions: tuple[
        ImmutableQuantitativeRecognitionCondition, ...
    ] = (),
) -> ImmutableQuantitativeRuleCandidate:
    literal = literal or f"{criterion_id} 1{unit} 이상 {max_points:g}점"
    threshold_literal = threshold_literal or literal
    return ImmutableQuantitativeRuleCandidate(
        source_attachment_id=attachment_id,
        table_id=table_id,
        criterion_id=criterion_id,
        label=label or criterion_id,
        criterion_literal=literal,
        max_points=max_points,
        scoring_method="THRESHOLD",
        metric=metric,
        unit=unit,
        brackets=(),
        threshold=ImmutableQuantitativeThreshold(
            literal=threshold_literal,
            operator="GTE",
            threshold_value=threshold_value,
            points_if_met=max_points,
            points_if_not_met=0,
            evidence=_anchor(
                page,
                threshold_literal,
                attachment_id=attachment_id,
                section=section,
            ),
        ),
        formula_literal=None,
        recognition_conditions=recognition_conditions,
        required_evidence=(fact_key,),
        evidence=_anchor(
            page,
            literal,
            attachment_id=attachment_id,
            section=section,
        ),
    )


def _table(
    table_id: str,
    page: int,
    candidates: tuple[ImmutableQuantitativeRuleCandidate, ...],
    *,
    attachment_id: str = ATTACHMENT_ID,
    section: str | None = None,
) -> ImmutableQuantitativeTable:
    total = sum(item.max_points for item in candidates)
    return ImmutableQuantitativeTable(
        source_attachment_id=attachment_id,
        table_id=table_id,
        label=f"physical-{table_id}",
        status="AVAILABLE",
        total_points=total,
        total_evidence=_anchor(
            page,
            f"{table_id} subtotal {total:g} points",
            attachment_id=attachment_id,
            section=section,
        ),
        minimum_score=None,
        minimum_evidence=None,
        criterion_ids=tuple(item.criterion_id for item in candidates),
        available_criterion_ids=tuple(item.criterion_id for item in candidates),
        review_criterion_ids=(),
    )


def _logical_profile(*, one_detail_table: bool = False) -> QuantitativeCandidateProfile:
    summary_id = "TABLE-A7"
    summary = (
        _candidate(
            table_id=summary_id,
            criterion_id="summary-awards",
            metric="AWARD_COUNT",
            unit="건",
            fact_key="company.award.count",
            max_points=10,
            page=1,
        ),
        _candidate(
            table_id=summary_id,
            criterion_id="summary-equipment",
            metric="FACILITY_EQUIPMENT_COUNT",
            unit="대",
            fact_key="company.facility_equipment.count",
            max_points=10,
            page=1,
        ),
    )
    detail_specs = (
        ("detail-years", "BUSINESS_YEARS", "년", "company.business.years", 6, 2),
        (
            "detail-certificates",
            "CERTIFICATION_COUNT",
            "건",
            "company.certification.count",
            4,
            3,
        ),
        (
            "detail-personnel",
            "PERSONNEL_COUNT",
            "명",
            "company.personnel.count",
            10,
            4,
        ),
    )
    if one_detail_table:
        detail_table_ids = ("TABLE-Z9",) * 3
        detail_pages = (2,) * 3
    else:
        detail_table_ids = ("TABLE-C2", "TABLE-D4", "TABLE-E8")
        detail_pages = (2, 3, 4)
    detail = tuple(
        _candidate(
            table_id=table_id,
            criterion_id=criterion_id,
            metric=metric,
            unit=unit,
            fact_key=fact_key,
            max_points=max_points,
            page=page,
        )
        for table_id, page, (
            criterion_id,
            metric,
            unit,
            fact_key,
            max_points,
            _original_page,
        ) in zip(detail_table_ids, detail_pages, detail_specs, strict=True)
    )
    tables = [_table(summary_id, 1, summary)]
    if one_detail_table:
        tables.append(_table("TABLE-Z9", 2, detail))
    else:
        tables.extend(
            _table(table_id, page, (candidate,))
            for table_id, page, candidate in zip(
                detail_table_ids, detail_pages, detail, strict=True
            )
        )
    return QuantitativeCandidateProfile(
        status="AVAILABLE",
        manifest_sha256="a" * 64,
        document_bindings=(
            AttachmentDocumentBinding(
                attachment_id=ATTACHMENT_ID,
                document_sha256="b" * 64,
            ),
        ),
        expected_attachment_ids=(ATTACHMENT_ID,),
        processed_attachment_ids=(ATTACHMENT_ID,),
        tables=tuple(tables),
        available_candidates=(*summary, *detail),
        review_candidates=(),
        not_applicable_evidence=(),
        issues=(),
    )


def _credit_candidate(
    *,
    table_id: str,
    criterion_id: str,
    attachment_id: str,
    page: int,
    section: str,
) -> ImmutableQuantitativeRuleCandidate:
    literal = "제안업체 경영상태 신용평가등급 10점"
    case_literal = "AAA, AA+, AA0, AA-, A+, A0, A-, BBB+, BBB0 배점의 100%"
    return ImmutableQuantitativeRuleCandidate(
        source_attachment_id=attachment_id,
        table_id=table_id,
        criterion_id=criterion_id,
        label="경영상태",
        criterion_literal=literal,
        max_points=10,
        scoring_method="CASE_TABLE",
        metric="CREDIT_RATING",
        unit="등급",
        brackets=(),
        threshold=None,
        formula_literal=None,
        cases=(
            ImmutableQuantitativeCase(
                literal=case_literal,
                operator="IN",
                comparison_value=None,
                category_values=(
                    "AAA",
                    "AA+",
                    "AA0",
                    "AA-",
                    "A+",
                    "A0",
                    "A-",
                    "BBB+",
                    "BBB0",
                ),
                award_kind="PERCENT_OF_MAX",
                award_value=100,
                row_order=1,
                evidence=_anchor(
                    page,
                    case_literal,
                    attachment_id=attachment_id,
                    section=section,
                ),
            ),
        ),
        required_evidence=("company.credit_rating",),
        evidence=_anchor(
            page,
            literal,
            attachment_id=attachment_id,
            section=section,
        ),
    )


def _busan_detail_candidates(
    *,
    attachment_id: str,
    table_id: str,
    criterion_prefix: str,
    performance_scope: str,
) -> tuple[ImmutableQuantitativeRuleCandidate, ...]:
    section = "정량적 평가 세부기준"
    conditions = (
        _condition(
            "최근 3년간은 입찰공고일을 기준으로 한다.",
            page=34,
            attachment_id=attachment_id,
            section=section,
        ),
        _condition(
            "증빙서류로 용역수행실적 총괄표와 용역실적증명서를 첨부한다.",
            page=34,
            attachment_id=attachment_id,
            section=section,
        ),
    )
    amount_literal = (
        "최근 3년간 지자체, 공공기관 등 "
        f"({performance_scope}) 용역 수행 완료한 실적 "
        "단일계약 2억원 이상"
    )
    count_literal = (
        "최근 3년간 지자체, 공공기관 등 "
        f"({performance_scope}) 용역 수행 완료한 실적 "
        "건수(0.2억원 이상) 5건 이상"
    )
    return (
        _candidate(
            table_id=table_id,
            criterion_id=f"{criterion_prefix}-performance-amount",
            metric="PERFORMANCE_AMOUNT",
            unit="억원",
            fact_key="company.performance.amount",
            max_points=6,
            page=33,
            attachment_id=attachment_id,
            label="용역수행 실적(금액)",
            literal=amount_literal,
            threshold_literal="A. 2억원 이상 6점",
            threshold_value=2,
            section=section,
            recognition_conditions=conditions,
        ),
        _candidate(
            table_id=table_id,
            criterion_id=f"{criterion_prefix}-performance-count",
            metric="PERFORMANCE_COUNT",
            unit="건",
            fact_key="company.performance.count",
            max_points=4,
            page=34,
            attachment_id=attachment_id,
            label="용역수행 실적(건수)",
            literal=count_literal,
            threshold_literal="A. 5건 이상 4점",
            threshold_value=5,
            section=section,
            recognition_conditions=conditions,
        ),
        _credit_candidate(
            table_id=table_id,
            criterion_id=f"{criterion_prefix}-credit-rating",
            attachment_id=attachment_id,
            page=34,
            section=section,
        ),
    )


def _busan_cross_attachment_profile(
    *,
    with_roles: bool = True,
    alternate_scope: bool = False,
) -> QuantitativeCandidateProfile:
    notice_section = "평가 항목 및 배점"
    notice_table_id = "NOTICE-QUANT-SUMMARY"
    notice_candidates = (
        _candidate(
            table_id=notice_table_id,
            criterion_id="notice-performance-summary",
            metric="AWARD_COUNT",
            unit="건",
            fact_key="company.award.count",
            max_points=10,
            page=1,
            attachment_id=NOTICE_ATTACHMENT_ID,
            label="용역수행실적",
            section=notice_section,
        ),
        _candidate(
            table_id=notice_table_id,
            criterion_id="notice-management-summary",
            metric="CERTIFICATION_COUNT",
            unit="건",
            fact_key="company.certification.count",
            max_points=10,
            page=1,
            attachment_id=NOTICE_ATTACHMENT_ID,
            label="경영상태",
            section=notice_section,
        ),
    )
    rfp_table_id = "RFP-QUANT-DETAIL"
    rfp_candidates = _busan_detail_candidates(
        attachment_id=RFP_ATTACHMENT_ID,
        table_id=rfp_table_id,
        criterion_prefix="rfp",
        performance_scope="교육, 취업, 행사",
    )
    tables = [
        _table(
            notice_table_id,
            1,
            notice_candidates,
            attachment_id=NOTICE_ATTACHMENT_ID,
            section=notice_section,
        ),
        _table(
            rfp_table_id,
            33,
            rfp_candidates,
            attachment_id=RFP_ATTACHMENT_ID,
            section="정량적 평가 세부기준",
        ),
    ]
    available_candidates = [*notice_candidates, *rfp_candidates]
    attachment_ids = [NOTICE_ATTACHMENT_ID, RFP_ATTACHMENT_ID]

    if with_roles:
        bindings = [
            AttachmentDocumentBinding(
                attachment_id=NOTICE_ATTACHMENT_ID,
                document_sha256="c" * 64,
                document_type="NOTICE",
                source_label="공고문(재공고).pdf",
            ),
            AttachmentDocumentBinding(
                attachment_id=RFP_ATTACHMENT_ID,
                document_sha256="d" * 64,
                document_type="RFP",
                source_label="2026 부산교육한마당 위탁 용역 제안 요청서.hwp",
            ),
        ]
    else:
        bindings = [
            AttachmentDocumentBinding(
                attachment_id=NOTICE_ATTACHMENT_ID,
                document_sha256="c" * 64,
            ),
            AttachmentDocumentBinding(
                attachment_id=RFP_ATTACHMENT_ID,
                document_sha256="d" * 64,
            ),
        ]

    if alternate_scope:
        scope_table_id = "SCOPE-QUANT-ALTERNATIVE"
        scope_candidates = _busan_detail_candidates(
            attachment_id=SCOPE_ATTACHMENT_ID,
            table_id=scope_table_id,
            criterion_prefix="scope",
            performance_scope="소프트웨어, 시스템 개발",
        )
        tables.append(
            _table(
                scope_table_id,
                33,
                scope_candidates,
                attachment_id=SCOPE_ATTACHMENT_ID,
                section="정량적 평가 세부기준",
            )
        )
        available_candidates.extend(scope_candidates)
        attachment_ids.append(SCOPE_ATTACHMENT_ID)
        bindings.append(
            AttachmentDocumentBinding(
                attachment_id=SCOPE_ATTACHMENT_ID,
                document_sha256="e" * 64,
                document_type="SCOPE" if with_roles else None,
                source_label="과업내용서.pdf" if with_roles else None,
            )
        )

    return QuantitativeCandidateProfile(
        status="AVAILABLE",
        manifest_sha256="f" * 64,
        document_bindings=tuple(bindings),
        expected_attachment_ids=tuple(attachment_ids),
        processed_attachment_ids=tuple(attachment_ids),
        tables=tuple(tables),
        available_candidates=tuple(available_candidates),
        review_candidates=(),
        not_applicable_evidence=(),
        issues=(),
    )


def test_split_summary_detail_and_continuation_form_one_logical_program() -> None:
    request = quantitative_request_from_candidate_profile(_logical_profile())

    assert request.activation_status == "AUTO_ACTIVE"
    assert request.activation_reasons == []
    assert len(request.criteria) == 3
    assert {item.category for item in request.criteria} == {
        "BUSINESS_YEARS",
        "CERTIFICATION_COUNT",
        "PERSONNEL_COUNT",
    }
    assert sum(item.max_points for item in request.criteria) == 20


def test_equal_subtotal_prefers_the_unique_more_detailed_representation() -> None:
    request = quantitative_request_from_candidate_profile(
        _logical_profile(one_detail_table=True)
    )

    assert request.activation_status == "AUTO_ACTIVE"
    assert len(request.criteria) == 3
    assert sum(item.max_points for item in request.criteria) == 20


def test_non_preserved_subtotal_keeps_table_level_conflict_reasons() -> None:
    profile = _logical_profile()
    candidate = next(
        item
        for item in profile.available_candidates
        if item.criterion_id == "detail-certificates"
    )
    changed = candidate.model_copy(
        update={
            "max_points": 5,
            "threshold": candidate.threshold.model_copy(
                update={"points_if_met": 5}
            ),
        }
    )
    table = next(item for item in profile.tables if item.table_id == candidate.table_id)
    changed_table = table.model_copy(
        update={
            "total_points": 5,
            "total_evidence": _anchor(3, "TABLE-D4 subtotal 5 points"),
        }
    )
    profile = profile.model_copy(
        update={
            "available_candidates": tuple(
                changed if item is candidate else item
                for item in profile.available_candidates
            ),
            "tables": tuple(
                changed_table if item is table else item for item in profile.tables
            ),
        }
    )

    request = quantitative_request_from_candidate_profile(profile)

    assert request.activation_status == "REVIEW_REQUIRED"
    assert "ALTERNATIVE_TABLE_AMBIGUOUS" in request.activation_reasons
    assert any(
        reason.startswith("LOGICAL_TABLE_CONFLICT|")
        and reason.endswith("|SUBTOTAL_NOT_PRESERVED")
        for reason in request.activation_reasons
    )
    assert request.criteria == []


def test_duplicate_leaf_identity_keeps_criterion_level_conflict_reasons() -> None:
    profile = _logical_profile()
    first = next(
        item
        for item in profile.available_candidates
        if item.criterion_id == "detail-years"
    )
    second = next(
        item
        for item in profile.available_candidates
        if item.criterion_id == "detail-certificates"
    )
    changed = second.model_copy(update={"criterion_id": first.criterion_id})
    table = next(item for item in profile.tables if item.table_id == second.table_id)
    changed_table = table.model_copy(
        update={
            "criterion_ids": (first.criterion_id,),
            "available_criterion_ids": (first.criterion_id,),
        }
    )
    profile = profile.model_copy(
        update={
            "available_candidates": tuple(
                changed if item is second else item
                for item in profile.available_candidates
            ),
            "tables": tuple(
                changed_table if item is table else item for item in profile.tables
            ),
        }
    )

    request = quantitative_request_from_candidate_profile(profile)

    assert request.activation_status == "REVIEW_REQUIRED"
    assert "ALTERNATIVE_TABLE_AMBIGUOUS" in request.activation_reasons
    assert any(
        reason.startswith(f"LOGICAL_CRITERION_CONFLICT|{ATTACHMENT_ID}|")
        and reason.endswith("|detail-years")
        for reason in request.activation_reasons
    )
    assert request.criteria == []


def test_busan_notice_summary_and_rfp_detail_form_cross_attachment_program() -> None:
    request = quantitative_request_from_candidate_profile(
        _busan_cross_attachment_profile()
    )

    assert request.activation_status == "AUTO_ACTIVE"
    assert request.activation_reasons == []
    assert {item.category for item in request.criteria} == {
        "PERFORMANCE_AMOUNT",
        "PERFORMANCE_COUNT",
        "CREDIT_RATING",
    }
    assert sum(item.max_points for item in request.criteria) == 20
    assert {
        item.source_anchor.document_label
        for item in request.criteria
        if item.source_anchor is not None
    } == {RFP_ATTACHMENT_ID}


def test_same_points_different_performance_scope_alternative_requires_review() -> None:
    request = quantitative_request_from_candidate_profile(
        _busan_cross_attachment_profile(alternate_scope=True)
    )

    assert request.activation_status == "REVIEW_REQUIRED"
    assert "ALTERNATIVE_TABLE_AMBIGUOUS" in request.activation_reasons
    assert any(
        reason.endswith("|EQUAL_SUBTOTAL_ALTERNATIVE")
        for reason in request.activation_reasons
    )
    assert request.criteria == []


def test_cross_attachment_without_authoritative_roles_fails_closed() -> None:
    request = quantitative_request_from_candidate_profile(
        _busan_cross_attachment_profile(with_roles=False)
    )

    assert request.activation_status == "REVIEW_REQUIRED"
    assert "ALTERNATIVE_TABLE_AMBIGUOUS" in request.activation_reasons
    assert any(
        reason.endswith("|CROSS_ATTACHMENT_ROLE_UNVERIFIED")
        for reason in request.activation_reasons
    )
    assert request.criteria == []


def test_logical_program_table_limit_fails_closed() -> None:
    candidates = tuple(
        _candidate(
            table_id=f"TABLE-CAP-{index:02d}",
            criterion_id=f"criterion-cap-{index:02d}",
            metric="AWARD_COUNT",
            unit="건",
            fact_key="company.award.count",
            max_points=1,
            page=index + 1,
        )
        for index in range(17)
    )
    profile = QuantitativeCandidateProfile(
        status="AVAILABLE",
        manifest_sha256="a" * 64,
        document_bindings=(
            AttachmentDocumentBinding(
                attachment_id=ATTACHMENT_ID,
                document_sha256="b" * 64,
            ),
        ),
        expected_attachment_ids=(ATTACHMENT_ID,),
        processed_attachment_ids=(ATTACHMENT_ID,),
        tables=tuple(
            _table(candidate.table_id, index + 1, (candidate,))
            for index, candidate in enumerate(candidates)
        ),
        available_candidates=candidates,
        review_candidates=(),
        not_applicable_evidence=(),
        issues=(),
    )

    request = quantitative_request_from_candidate_profile(profile)

    assert request.activation_status == "REVIEW_REQUIRED"
    assert "LOGICAL_PROGRAM_TABLE_LIMIT_EXCEEDED" in request.activation_reasons
    assert request.criteria == []


def test_extraction_payload_rejects_more_than_sixteen_quantitative_tables() -> None:
    tables = [
        QuantitativeTableCandidate(
            table_id=f"TABLE-INGEST-{index:02d}",
            label="정량표",
            criteria=[],
            total_points=None,
            total_evidence=None,
            minimum_score=None,
            minimum_evidence=None,
            ambiguity_reason="검증 전 fixture",
        )
        for index in range(17)
    ]

    with pytest.raises(ValueError):
        ExtractionPayload(
            document_type="OTHER",
            requirements=[],
            quantitative_tables=tables,
            quantitative_table_not_applicable=None,
            missing_or_unreadable=[],
            summary="",
        )


def test_logical_resolver_work_budget_fails_closed() -> None:
    points = (100, 99, 98, *(1 for _ in range(13)))
    candidates = tuple(
        _candidate(
            table_id=f"TABLE-WORK-{index:02d}",
            criterion_id=f"criterion-work-{index:02d}",
            metric="AWARD_COUNT",
            unit="건",
            fact_key="company.award.count",
            max_points=max_points,
            page=index + 1,
        )
        for index, max_points in enumerate(points)
    )
    profile = QuantitativeCandidateProfile(
        status="AVAILABLE",
        manifest_sha256="a" * 64,
        document_bindings=(
            AttachmentDocumentBinding(
                attachment_id=ATTACHMENT_ID,
                document_sha256="b" * 64,
            ),
        ),
        expected_attachment_ids=(ATTACHMENT_ID,),
        processed_attachment_ids=(ATTACHMENT_ID,),
        tables=tuple(
            _table(candidate.table_id, index + 1, (candidate,))
            for index, candidate in enumerate(candidates)
        ),
        available_candidates=candidates,
        review_candidates=(),
        not_applicable_evidence=(),
        issues=(),
    )

    request = quantitative_request_from_candidate_profile(profile)

    assert request.activation_status == "REVIEW_REQUIRED"
    assert "LOGICAL_RESOLVER_BUDGET_EXCEEDED" in request.activation_reasons
    assert request.criteria == []
