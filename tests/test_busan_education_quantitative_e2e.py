from __future__ import annotations

from datetime import date, datetime, timezone

from pai_loop.integrations.openai_extraction import (
    EvidenceAnchor,
    ExtractionPayload,
    QuantitativeCaseLiteral,
    QuantitativeRecognitionCondition,
    QuantitativeRuleCandidate,
    QuantitativeTableCandidate,
)
from pai_loop.models import CompanyPerformanceRecord
from pai_loop.quantitative_rule_extraction import (
    AttachmentDocumentBinding,
    build_quantitative_candidate_profile,
)
from pai_loop.quantitative_scoring import (
    QuantitativeFact,
    estimate_quantitative_score,
    quantitative_request_from_candidate_profile,
    resolve_performance_register_facts,
)


ATTACHMENT_ID = "BUSAN-EDUCATION-RFP-HWP"


def _anchor(quote: str, page: int) -> EvidenceAnchor:
    return EvidenceAnchor(
        attachment_id=ATTACHMENT_ID,
        page=page,
        section=f"제안서 평가 {page}페이지",
        quote=quote,
        confidence=1,
    )


def _case(
    literal: str,
    *,
    operator: str,
    row_order: int,
    page: int,
    comparison_value: float | None = None,
    category_values: list[str] | None = None,
    award_kind: str = "POINTS",
    award_value: float,
) -> QuantitativeCaseLiteral:
    return QuantitativeCaseLiteral(
        literal=literal,
        operator=operator,
        comparison_value=comparison_value,
        category_values=category_values or [],
        award_kind=award_kind,
        award_value=award_value,
        row_order=row_order,
        evidence=_anchor(literal, page),
    )


def _condition(literal: str, page: int = 34) -> QuantitativeRecognitionCondition:
    return QuantitativeRecognitionCondition(
        literal=literal,
        evidence=_anchor(literal, page),
    )


def _profile():
    total_literal = "정량적 평가 세부기준 합계 20점"
    amount_literal = (
        "최근 3년간 지자체, 공공기관 등 (교육, 취업, 행사) 용역 "
        "수행완료 실적(금액, 6점) 단일용역 최고금액(1건)"
    )
    count_literal = (
        "최근 3년간 지자체, 공공기관 등 (교육, 취업, 행사) 용역 "
        "수행완료 실적(건수, 4점) 실적건수(0.2억원 이상)"
    )
    credit_literal = "제안업체 경영상태(신용평가등급에 의한 평가, 10점)"
    anchor_condition = "① ‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다."
    certificate_condition = "② 증빙서류로 용역수행실적 총괄표, 용역실적증명서를 첨부한다."
    consortium_condition = "③ 공동계약으로 참여한 실적의 경우 공동계약 참여 비율에 따른 금액의 실적"
    amount_cases = [
        _case("A. 2억원 이상 6점", operator="GTE", comparison_value=2, award_value=6, row_order=1, page=33),
        _case("B. 1.5억원 이상 5.5점", operator="GTE", comparison_value=1.5, award_value=5.5, row_order=2, page=33),
        _case("C. 1억원 이상 5점", operator="GTE", comparison_value=1, award_value=5, row_order=3, page=33),
    ]
    count_cases = [
        _case("A. 5건 이상 4점", operator="GTE", comparison_value=5, award_value=4, row_order=1, page=34),
        _case("B. 4건 3.7점", operator="EQ", comparison_value=4, award_value=3.7, row_order=2, page=34),
        _case("C. 3건 3.4점", operator="EQ", comparison_value=3, award_value=3.4, row_order=3, page=34),
        _case("D. 2건 3.1점", operator="EQ", comparison_value=2, award_value=3.1, row_order=4, page=34),
        _case("E. 1건 2.8점", operator="EQ", comparison_value=1, award_value=2.8, row_order=5, page=34),
    ]
    credit_cases = [
        _case(
            "AAA, AA+, AA0, AA-, A+, A0, A-, BBB+, BBB0 배점의 100%",
            operator="IN",
            category_values=["AAA", "AA+", "AA0", "AA-", "A+", "A0", "A-", "BBB+", "BBB0"],
            award_kind="PERCENT_OF_MAX",
            award_value=100,
            row_order=1,
            page=34,
        ),
        _case(
            "BBB-, BB+, BB0, BB- 배점의 95%",
            operator="IN",
            category_values=["BBB-", "BB+", "BB0", "BB-"],
            award_kind="PERCENT_OF_MAX",
            award_value=95,
            row_order=2,
            page=34,
        ),
        _case(
            "B+, B0, B- 배점의 90%",
            operator="IN",
            category_values=["B+", "B0", "B-"],
            award_kind="PERCENT_OF_MAX",
            award_value=90,
            row_order=3,
            page=34,
        ),
        _case(
            "CCC+ 이하 배점의 70%",
            operator="IN",
            category_values=["CCC+ 이하"],
            award_kind="PERCENT_OF_MAX",
            award_value=70,
            row_order=4,
            page=34,
        ),
    ]
    candidates = [
        QuantitativeRuleCandidate(
            criterion_id="performance-amount",
            label="용역수행 실적(금액)",
            criterion_literal=amount_literal,
            max_points=6,
            scoring_method="CASE_TABLE",
            metric="PERFORMANCE_AMOUNT",
            unit="억원",
            brackets=[],
            threshold=None,
            formula_literal=None,
            cases=amount_cases,
            recognition_conditions=[
                _condition(anchor_condition),
                _condition(certificate_condition),
                _condition(consortium_condition),
            ],
            required_evidence=["company.performance.amount"],
            evidence=_anchor(amount_literal, 33),
            ambiguity_reason=None,
        ),
        QuantitativeRuleCandidate(
            criterion_id="performance-count",
            label="용역수행 실적(건수)",
            criterion_literal=count_literal,
            max_points=4,
            scoring_method="CASE_TABLE",
            metric="PERFORMANCE_COUNT",
            unit="건",
            brackets=[],
            threshold=None,
            formula_literal=None,
            cases=count_cases,
            recognition_conditions=[
                _condition(anchor_condition),
                _condition(certificate_condition),
            ],
            required_evidence=["company.performance.count"],
            evidence=_anchor(count_literal, 34),
            ambiguity_reason=None,
        ),
        QuantitativeRuleCandidate(
            criterion_id="credit-rating",
            label="경영상태",
            criterion_literal=credit_literal,
            max_points=10,
            scoring_method="CASE_TABLE",
            metric="CREDIT_RATING",
            unit="등급",
            brackets=[],
            threshold=None,
            formula_literal=None,
            cases=credit_cases,
            required_evidence=["company.credit_rating"],
            evidence=_anchor(credit_literal, 34),
            ambiguity_reason=None,
        ),
    ]
    source = "\n".join(
        [
            total_literal,
            amount_literal,
            *(item.literal for item in amount_cases),
            count_literal,
            *(item.literal for item in count_cases),
            credit_literal,
            *(item.literal for item in credit_cases),
            anchor_condition,
            certificate_condition,
            consortium_condition,
        ]
    )
    payload = ExtractionPayload(
        document_type="RFP",
        requirements=[],
        quantitative_tables=[
            QuantitativeTableCandidate(
                table_id="quantitative-20",
                label="정량적 평가 세부기준",
                criteria=candidates,
                total_points=20,
                total_evidence=_anchor(total_literal, 33),
                minimum_score=None,
                minimum_evidence=None,
                ambiguity_reason=None,
            )
        ],
        quantitative_table_not_applicable=None,
        missing_or_unreadable=[],
        summary="정량평가 20점의 세부 산식",
    )
    profile = build_quantitative_candidate_profile(
        {ATTACHMENT_ID: payload},
        {ATTACHMENT_ID: source},
        expected_attachment_ids=(ATTACHMENT_ID,),
    )
    return profile.model_copy(
        update={
            "manifest_sha256": "a" * 64,
            "document_bindings": (
                AttachmentDocumentBinding(
                    attachment_id=ATTACHMENT_ID,
                    document_sha256="b" * 64,
                ),
            ),
        }
    )


def test_busan_rfp_case_tables_score_full_twenty_from_company_data() -> None:
    profile = _profile()
    assert profile.status == "AVAILABLE", profile.issues

    request = quantitative_request_from_candidate_profile(profile)
    assert request.activation_status == "AUTO_ACTIVE", request.activation_reasons
    assert len(request.criteria) == 3
    criteria = {item.metric_key: item for item in request.criteria}
    amount_scope = criteria["company.performance.amount"].performance_scope
    count_scope = criteria["company.performance.count"].performance_scope
    assert amount_scope is not None
    assert count_scope is not None
    assert amount_scope.lookback_anchor_basis == "BID_NOTICE_DATE"
    assert count_scope.lookback_anchor_basis == "BID_NOTICE_DATE"
    assert amount_scope.certificate_required is True
    assert count_scope.certificate_required is True
    assert amount_scope.consortium_share_rule == "APPLY_SHARE"
    facts = [
        QuantitativeFact(
            metric_key="company.performance.amount",
            status="ESTIMATED",
            value=250_000_000,
            lower_value=250_000_000,
            upper_value=250_000_000,
            evidence_key="company.performance.amount",
            fact_binding_sha256=criteria["company.performance.amount"].fact_binding_sha256,
            confidence=0.85,
            rationale="검증 실적대장의 단일 최고금액",
        ),
        QuantitativeFact(
            metric_key="company.performance.count",
            status="ESTIMATED",
            value=6,
            lower_value=6,
            upper_value=6,
            evidence_key="company.performance.count",
            fact_binding_sha256=criteria["company.performance.count"].fact_binding_sha256,
            confidence=0.85,
            rationale="검증 실적대장의 인정 건수",
        ),
        QuantitativeFact(
            metric_key="company.credit_rating",
            status="CONFIRMED",
            value="A0",
            evidence_key="company.credit_rating",
            fact_binding_sha256=criteria["company.credit_rating"].fact_binding_sha256,
            confidence=1,
            rationale="유효 신용평가등급",
        ),
    ]

    result = estimate_quantitative_score(request.model_copy(update={"facts": facts}))

    assert result.total_max_points == 20
    assert (result.lower_points, result.upper_points, result.estimated_points) == (20, 20, 20)
    assert {item.category: item.estimated_points for item in result.criteria} == {
        "PERFORMANCE_AMOUNT": 6,
        "PERFORMANCE_COUNT": 4,
        "CREDIT_RATING": 10,
    }


def test_busan_rfp_scores_twenty_through_real_performance_register_resolver() -> None:
    request = quantitative_request_from_candidate_profile(_profile())
    assert request.activation_status == "AUTO_ACTIVE", request.activation_reasons
    criteria = {item.metric_key: item for item in request.criteria}
    records = [
        CompanyPerformanceRecord(
            record_key=f"BUSAN-EDU-{index}",
            revision=1,
            record_status="VALIDATED",
            project_name=f"공공기관 교육 행사 운영 {index}",
            agency="부산광역시교육청",
            division="교육정책과",
            overview="교육 프로그램 및 행사 운영",
            contract_date=date(2024 + (index % 2), 1, index),
            start_date=date(2024 + (index % 2), 1, index),
            end_date=date(2024 + (index % 2), 6, index),
            contract_amount=250_000_000 if index == 1 else 30_000_000,
            vat_basis="INCLUDED",
            completed=True,
            share_pct=100,
            certificate_status="ISSUED",
            evidence_reference=f"실적증명서-{index}",
            keywords=["교육", "행사"],
            source="MANUAL",
            created_by="KMA 입찰팀",
            updated_by="KMA 입찰팀",
        )
        for index in range(1, 7)
    ]
    records.append(
        CompanyPerformanceRecord(
            record_key="BUSAN-EDU-UNCERTAIN-AGENCY",
            revision=1,
            record_status="VALIDATED",
            project_name="공공 교육 행사 운영 추가 실적",
            agency="한빛공사",
            division="교육사업부",
            overview="교육 프로그램 및 행사 운영",
            contract_date=date(2025, 3, 1),
            start_date=date(2025, 3, 1),
            end_date=date(2025, 7, 1),
            contract_amount=500_000_000,
            vat_basis="INCLUDED",
            completed=True,
            share_pct=100,
            certificate_status="ISSUED",
            evidence_reference="SYN-EVIDENCE-UNCERTAIN-AGENCY",
            keywords=["교육", "행사"],
            source="MANUAL",
            created_by="KMA 입찰팀",
            updated_by="KMA 입찰팀",
        )
    )
    register_facts = resolve_performance_register_facts(
        request.criteria,
        records,
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
        bid_notice_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    assert {item.metric_key for item in register_facts} == {
        "company.performance.amount",
        "company.performance.count",
    }
    amount_fact = next(
        item
        for item in register_facts
        if item.metric_key == "company.performance.amount"
    )
    count_fact = next(
        item
        for item in register_facts
        if item.metric_key == "company.performance.count"
    )
    assert (amount_fact.status, count_fact.status) == ("ESTIMATED", "ESTIMATED")
    assert amount_fact.lower_value is not None and amount_fact.lower_value > 200_000_000
    assert amount_fact.upper_value is None
    assert (count_fact.lower_value, count_fact.upper_value) == (6, None)

    credit_fact = QuantitativeFact(
        metric_key="company.credit_rating",
        status="CONFIRMED",
        value="A0",
        evidence_key="company.credit_rating",
        fact_binding_sha256=criteria["company.credit_rating"].fact_binding_sha256,
        confidence=1,
        rationale="유효 신용평가등급",
    )
    result = estimate_quantitative_score(
        request.model_copy(update={"facts": [*register_facts, credit_fact]})
    )

    assert result.total_max_points == 20
    assert (result.lower_points, result.upper_points, result.estimated_points) == (
        20,
        20,
        20,
    )


def _vat_edge_estimate(metric_key: str, amount: int):
    request = quantitative_request_from_candidate_profile(_profile())
    criterion = next(
        item for item in request.criteria if item.metric_key == metric_key
    )
    record = CompanyPerformanceRecord(
        record_key=f"VAT-EDGE-{metric_key}-{amount}",
        revision=1,
        record_status="VALIDATED",
        project_name="공공기관 교육 행사 운영",
        agency="부산광역시교육청",
        division="교육정책과",
        overview="교육 프로그램 및 행사 운영",
        keywords=["교육", "행사"],
        contract_date=date(2025, 1, 1),
        start_date=date(2025, 1, 1),
        end_date=date(2025, 6, 1),
        contract_amount=amount,
        vat_basis="INCLUDED",
        completed=True,
        share_pct=100,
        certificate_status="ISSUED",
        evidence_reference="실적증명서-VAT-EDGE",
        source="MANUAL",
        created_by="KMA 입찰팀",
        updated_by="KMA 입찰팀",
    )
    facts = resolve_performance_register_facts(
        [criterion],
        [record],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
        bid_notice_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    result = estimate_quantitative_score(
        request.model_copy(update={"criteria": [criterion], "facts": facts})
    )
    return facts[0], result.criteria[0], result


def test_busan_unspecified_vat_same_case_row_shows_exact_points() -> None:
    fact, criterion, result = _vat_edge_estimate(
        "company.performance.amount", 220_000_000
    )

    assert (fact.lower_value, fact.upper_value) == (200_000_000, 220_000_000)
    assert (criterion.status, criterion.estimated_points) == ("ESTIMATED", 6)
    assert (criterion.lower_points, criterion.upper_points) == (6, 6)
    assert (result.overall_status, result.estimated_points) == ("ESTIMATED", 6)


def test_busan_unspecified_vat_crossing_case_rows_requires_review() -> None:
    fact, criterion, result = _vat_edge_estimate(
        "company.performance.amount", 210_000_000
    )

    assert (
        fact.lower_value is not None
        and 190_000_000 < fact.lower_value < 200_000_000
    )
    assert fact.upper_value == 210_000_000
    assert (criterion.status, criterion.estimated_points) == ("REVIEW", None)
    assert (criterion.lower_points, criterion.upper_points) == (5.5, 6)
    assert (result.overall_status, result.estimated_points) == ("REVIEW", None)


def test_busan_unspecified_vat_with_undefined_zero_case_requires_review() -> None:
    fact, criterion, result = _vat_edge_estimate(
        "company.performance.count", 20_000_000
    )

    assert (fact.lower_value, fact.upper_value) == (0, 1)
    assert (criterion.status, criterion.estimated_points) == ("REVIEW", None)
    assert (criterion.lower_points, criterion.upper_points) == (0, 4)
    assert (result.overall_status, result.estimated_points) == ("REVIEW", None)


def test_private_import_is_authoritative_over_duplicate_manual_performance() -> None:
    request = quantitative_request_from_candidate_profile(_profile())
    criterion = next(
        item
        for item in request.criteria
        if item.metric_key == "company.performance.amount"
    )
    manual = CompanyPerformanceRecord(
        record_key="SYN-MANUAL-DUPLICATE",
        revision=1,
        record_status="VALIDATED",
        project_name="공공기관 교육 행사 운영",
        agency="부산광역시교육청",
        overview="교육 행사",
        keywords=["교육", "행사"],
        contract_date=date(2025, 1, 1),
        start_date=date(2025, 1, 1),
        end_date=date(2025, 6, 1),
        contract_amount=550_000_000,
        vat_basis="INCLUDED",
        completed=True,
        share_pct=100,
        certificate_status="ISSUED",
        evidence_reference="SYN-MANUAL-EVIDENCE",
        source="MANUAL",
    )
    private = CompanyPerformanceRecord(
        record_key="SYN-PRIVATE-AUTHORITATIVE",
        revision=1,
        record_status="VALIDATED",
        project_name="공공기관 교육 행사 운영",
        agency="부산광역시교육청",
        overview="교육 행사",
        keywords=["교육", "행사"],
        contract_date=date(2025, 1, 1),
        start_date=date(2025, 1, 1),
        end_date=date(2025, 6, 1),
        contract_amount=110_000_000,
        vat_basis="INCLUDED",
        completed=True,
        share_pct=100,
        certificate_status="ISSUED",
        evidence_reference="private-evidence://synthetic/performance/1",
        source="PRIVATE_IMPORT",
    )

    fact = resolve_performance_register_facts(
        [criterion],
        [manual, private],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
        bid_notice_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )[0]

    assert fact.status == "ESTIMATED"
    assert (fact.lower_value, fact.upper_value) == (100_000_000, 110_000_000)


def test_pending_private_import_suppresses_manual_scoring_fail_closed() -> None:
    request = quantitative_request_from_candidate_profile(_profile())
    criterion = next(
        item
        for item in request.criteria
        if item.metric_key == "company.performance.amount"
    )
    manual = CompanyPerformanceRecord(
        record_key="SYN-MANUAL-WOULD-SCORE",
        revision=1,
        record_status="VALIDATED",
        project_name="공공기관 교육 행사 운영",
        agency="부산광역시교육청",
        overview="교육 행사",
        keywords=["교육", "행사"],
        contract_date=date(2025, 1, 1),
        start_date=date(2025, 1, 1),
        end_date=date(2025, 6, 1),
        contract_amount=550_000_000,
        vat_basis="INCLUDED",
        completed=True,
        share_pct=100,
        certificate_status="ISSUED",
        evidence_reference="SYN-MANUAL-EVIDENCE",
        source="MANUAL",
    )
    pending_private = CompanyPerformanceRecord(
        record_key="SYN-PRIVATE-PENDING",
        revision=1,
        record_status="DRAFT",
        project_name="공공기관 교육 행사 운영",
        agency="부산광역시교육청",
        overview="교육 행사",
        keywords=["교육", "행사"],
        contract_date=date(2025, 1, 1),
        start_date=date(2025, 1, 1),
        end_date=date(2025, 6, 1),
        contract_amount=550_000_000,
        vat_basis="INCLUDED",
        completed=True,
        share_pct=100,
        certificate_status="ISSUED",
        evidence_reference="private-evidence://synthetic/performance/pending",
        source="PRIVATE_IMPORT_PENDING_VALIDATED",
    )

    fact = resolve_performance_register_facts(
        [criterion],
        [manual, pending_private],
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
        bid_notice_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )[0]

    assert fact.status == "REVIEW"
    assert fact.value is None


def test_private_draft_keeps_non_top_busan_band_in_review() -> None:
    request = quantitative_request_from_candidate_profile(_profile())
    criterion = next(
        item
        for item in request.criteria
        if item.metric_key == "company.performance.amount"
    )
    records = [
        CompanyPerformanceRecord(
            record_key="SYN-PRIVATE-LOWER-BOUND",
            revision=1,
            record_status="VALIDATED",
            project_name="공공기관 교육 행사 운영",
            agency="부산광역시교육청",
            overview="교육 행사",
            keywords=["교육", "행사"],
            contract_date=date(2025, 1, 1),
            start_date=date(2025, 1, 1),
            end_date=date(2025, 6, 1),
            contract_amount=110_000_000,
            vat_basis="INCLUDED",
            completed=True,
            share_pct=100,
            certificate_status="ISSUED",
            evidence_reference="private-evidence://synthetic/performance/lower",
            source="PRIVATE_IMPORT",
        ),
        CompanyPerformanceRecord(
            record_key="SYN-PRIVATE-UNRESOLVED",
            revision=1,
            record_status="DRAFT",
            project_name="공공기관 교육 행사 운영",
            agency="부산광역시교육청",
            overview="교육 행사",
            keywords=["교육", "행사"],
            contract_date=date(2025, 1, 1),
            start_date=date(2025, 1, 1),
            end_date=date(2025, 6, 1),
            contract_amount=900_000_000,
            vat_basis="INCLUDED",
            completed=True,
            share_pct=100,
            certificate_status="ISSUED",
            evidence_reference="private-evidence://synthetic/performance/draft",
            source="PRIVATE_IMPORT_DRAFT",
        ),
    ]

    fact = resolve_performance_register_facts(
        [criterion],
        records,
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
        bid_notice_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )[0]
    result = estimate_quantitative_score(
        request.model_copy(update={"criteria": [criterion], "facts": [fact]})
    )

    assert fact.status == "REVIEW"
    assert fact.lower_value == 100_000_000
    assert fact.upper_value is None
    assert result.overall_status == "REVIEW"
    assert result.estimated_points is None


def test_private_draft_allows_busan_twenty_when_lower_bounds_saturate() -> None:
    request = quantitative_request_from_candidate_profile(_profile())
    criteria = {item.metric_key: item for item in request.criteria}
    records = [
        CompanyPerformanceRecord(
            record_key=f"SYN-PRIVATE-BUSAN-{index}",
            revision=1,
            record_status="VALIDATED",
            project_name=f"공공기관 교육 행사 운영 {index}",
            agency="부산광역시교육청",
            overview="교육 프로그램 및 행사 운영",
            keywords=["교육", "행사"],
            contract_date=date(2025, 1, index),
            start_date=date(2025, 1, index),
            end_date=date(2025, 6, index),
            contract_amount=250_000_000 if index == 1 else 30_000_000,
            vat_basis="INCLUDED",
            completed=True,
            share_pct=100,
            certificate_status="ISSUED",
            evidence_reference=f"private-evidence://synthetic/performance/{index}",
            source="PRIVATE_IMPORT",
        )
        for index in range(1, 7)
    ]
    records.append(
        CompanyPerformanceRecord(
            record_key="SYN-PRIVATE-BUSAN-DRAFT",
            revision=1,
            record_status="DRAFT",
            project_name="공공기관 교육 행사 운영 추가 실적",
            agency="부산광역시교육청",
            overview="교육 프로그램 및 행사 운영",
            keywords=["교육", "행사"],
            contract_date=date(2025, 7, 1),
            start_date=date(2025, 7, 1),
            end_date=date(2025, 8, 1),
            contract_amount=30_000_000,
            vat_basis="INCLUDED",
            completed=True,
            share_pct=100,
            certificate_status="ISSUED",
            evidence_reference="private-evidence://synthetic/performance/unresolved",
            source="PRIVATE_IMPORT_DRAFT",
        )
    )

    performance_facts = resolve_performance_register_facts(
        request.criteria,
        records,
        as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
        bid_notice_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    assert {fact.status for fact in performance_facts} == {"ESTIMATED"}
    assert all(fact.upper_value is None for fact in performance_facts)

    credit_fact = QuantitativeFact(
        metric_key="company.credit_rating",
        status="CONFIRMED",
        value="A0",
        evidence_key="company.credit_rating",
        fact_binding_sha256=criteria["company.credit_rating"].fact_binding_sha256,
        confidence=1,
        rationale="유효 신용평가등급",
    )
    result = estimate_quantitative_score(
        request.model_copy(update={"facts": [*performance_facts, credit_fact]})
    )

    assert result.overall_status == "ESTIMATED"
    assert (result.lower_points, result.upper_points, result.estimated_points) == (
        20,
        20,
        20,
    )
