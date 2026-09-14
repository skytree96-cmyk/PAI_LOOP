"""SYN recognition rules: retain scope meaning before counting any records."""
import pytest

from pai_loop.quantitative_performance import derive_performance_value, parse_performance_recognition_scope
from test_performance_unmodeled_recognition import BASE, AS_OF, _record


def parse(text):
    return parse_performance_recognition_scope(text, metric_key="company.performance.count")


def test_certificate_issuer_is_not_the_client_and_still_requires_issuer_proof():
    text = BASE + "공공기관의 확인을 받은 실적증명서를 제출한다."
    scope = parse(text)
    assert scope is not None
    assert scope.counterparty_scope == "UNSPECIFIED" and scope.counterparty_keywords == ()
    assert scope.certificate_required and scope.manual_verification_conditions
    for value in (scope, parse(BASE).model_copy(update={"source_literal": text})):
        result = derive_performance_value(value, [_record()], as_of=AS_OF)
        assert result.status == "REVIEW" and result.value is result.lower_value is result.upper_value is None
        assert "확인기관" in result.rationale


@pytest.mark.parametrize("prefix,suffix", [
    ("공공기관 발주 용역만 인정. ", "공공기관의 확인을 받은 실적증명서 제출"),
    ("공공기관의 확인을 받은 실적증명서 제출, ", "공공기관 발주 실적만 인정"),
    ("공공기관의 확인을 받은 실적증명서, ", "공공기관 발주 용역만 인정하며 실적증명서 제출"),
])
def test_issuer_exclusion_does_not_hide_a_separate_public_client_restriction(prefix, suffix):
    scope = parse(BASE + prefix + suffix)
    assert scope is not None and scope.counterparty_scope == "PUBLIC_SECTOR"
    assert scope.counterparty_keywords == ("공공기관",)


def test_compound_service_requires_the_shared_qualifier_for_either_channel():
    scope = parse("최근 3년 전화 또는 화상 외국어 과정 수행실적. 수행완료 실적 VAT 포함")
    assert scope is not None and scope.similarity_keywords == ()
    assert scope.similarity_keyword_groups == (("전화", "외국어"), ("화상", "외국어"))
    records = [
        _record("SYN-PHONE-LANGUAGE", project_name="전화 외국어 과정", keywords=["전화", "외국어"]),
        _record("SYN-VIDEO-LANGUAGE", project_name="화상 외국어 과정", keywords=["화상", "외국어"]),
        _record("SYN-PHONE-SAFETY", project_name="전화 안전 과정", keywords=["전화", "안전"]),
        _record("SYN-LANGUAGE-ONLY", project_name="외국어 과정", keywords=["외국어"]),
    ]
    result = derive_performance_value(scope, records, as_of=AS_OF)
    assert result.status == "ESTIMATED" and result.value == 2
    assert result.matched_record_keys == ("SYN-PHONE-LANGUAGE", "SYN-VIDEO-LANGUAGE")
    stale = scope.model_copy(update={"similarity_keyword_groups": (), "similarity_keywords": ("전화",)})
    blocked = derive_performance_value(stale, records, as_of=AS_OF)
    assert blocked.status == "REVIEW" and blocked.value is None


def test_compound_service_grammar_is_not_tied_to_a_notice_or_training_name():
    scope = parse("최근 2년 대면 또는 온라인 안전교육 프로그램 수행실적. 수행완료 실적 VAT 포함")
    assert scope is not None
    assert scope.similarity_keyword_groups == (("대면", "안전교육"), ("온라인", "안전교육"))


def test_conflicting_compound_service_declarations_are_not_chosen_by_order():
    assert parse("최근 3년 전화 또는 화상 외국어 과정 수행실적. 대면 또는 온라인 안전교육 과정 수행실적. 수행완료 실적") is None


def test_direct_service_count_does_not_use_generic_certificate_words():
    scope = parse("최근 3년 국외연수 실시건수. 당해용역 이행실적은 관련 협회 확인. 수행완료 실적 VAT 포함")
    assert scope is not None and scope.similarity_keywords == ("국외연수",)
    assert parse("최근 3년 당해용역 이행실적은 관련 협회 확인. 수행완료 실적") is None


@pytest.mark.parametrize("clause,reason", [
    ("실적기간 2021.1.1.~공고일 전", "고정 실적기간"),
    ("실적기간 2021-01-01부터2025-12-31", "고정 실적기간"),
    ("실적기간 2021년 1월 1일 ~ 입찰공고일 전일까지", "고정 실적기간"),
    ("실적 인정기간은 2025년 1월부터 공고일까지", "고정 실적기간"),
    ("실적 인정기간은 2025년부터 공고일까지", "고정 실적기간"),
    ("실적 인정기간은 2025년부터 2026년까지", "고정 실적기간"),
    ("실적 인정기간은 2025년 1월부터 2026년 1월까지", "고정 실적기간"),
    ("발주처의 승인을 받은 하도급 실적에 한하여 인정", "하도급"),
    ("하도급 실적은 발주기관의 승인을 받은 경우에 인정", "하도급"),
])
def test_fixed_period_and_subcontract_proof_are_preserved_and_cannot_be_silently_ignored(clause, reason):
    literal = BASE + clause
    current = parse(literal)
    assert current is not None and current.manual_verification_conditions
    stale = parse(BASE).model_copy(update={"source_literal": literal})
    for scope in (current, stale):
        result = derive_performance_value(scope, [_record()], as_of=AS_OF)
        assert result.status == "REVIEW" and reason in result.rationale
        assert result.value is result.lower_value is result.upper_value is None


def test_issue_date_alone_is_not_invented_as_a_recognition_period():
    scope = parse(BASE + "작성일 2021.1.1.")
    assert scope is not None and scope.manual_verification_conditions == ()
    assert derive_performance_value(scope, [_record()], as_of=AS_OF).value == 1


def test_share_only_wording_applies_the_company_share_and_stale_scope_is_rejected():
    clause = "공동수급으로 이행한 경우 제안사 지분율에 해당하는 실적만 인정"
    scope = parse(BASE + clause)
    assert scope is not None and scope.consortium_share_rule == "APPLY_SHARE"
    record = _record("SYN-HALF-SHARE", share_pct=50)
    # Count ownership remains unresolved; the parser must not award half a count.
    result = derive_performance_value(scope, [record], as_of=AS_OF)
    assert result.value is None
    stale = scope.model_copy(update={"consortium_share_rule": "UNSPECIFIED"})
    result = derive_performance_value(stale, [record], as_of=AS_OF)
    assert result.status == "REVIEW" and result.value is None


@pytest.mark.parametrize("clause", [
    "부가세 포함. VAT 별도", "최근 2년 실적도 인정",
    "공동수급 지분율 적용. 공동수급 전체 인정",
])
def test_conflicting_recognition_dimensions_are_never_resolved_by_first_match(clause):
    assert parse(BASE + clause) is None


def test_this_bid_joint_member_score_weights_are_not_past_contract_share_rules():
    literal = BASE + "본 사업 공동수급 구성원의 참여비율에 따라 실적평가 및 경영상태 점수를 합산"
    assert parse(literal) is None
    stale = parse(BASE).model_copy(update={"source_literal": literal})
    result = derive_performance_value(stale, [_record()], as_of=AS_OF)
    assert result.status == "REVIEW" and result.value is None


@pytest.mark.parametrize("extra", [
    "공무원 교육 관련 용역만 인정.", "성인 대상", "성인", "재직자", "청소년", "성인:", "성인：",
])
def test_compound_grammar_does_not_drop_other_scope_qualifiers(extra):
    literal = "최근 3년 " + extra + " 전화 또는 화상 외국어 과정 수행실적. 수행완료 실적 VAT 포함"
    assert parse(literal) is None
    stale = parse(BASE).model_copy(update={"source_literal": literal})
    result = derive_performance_value(stale, [_record()], as_of=AS_OF)
    assert result.status == "REVIEW" and result.value is None


def test_old_scope_cannot_hide_conflicting_compound_groups():
    literal = BASE + "전화 또는 화상 외국어 과정 수행실적. 대면 또는 온라인 안전교육 과정 수행실적."
    stale = parse(BASE).model_copy(update={"source_literal": literal})
    assert parse(literal) is None
    assert derive_performance_value(stale, [_record()], as_of=AS_OF).value is None


def test_compound_scope_does_not_discard_a_trailing_learner_qualifier():
    literal = "최근 3년 전화 또는 화상 외국어 과정 수행실적 중 성인만 인정. 수행완료 실적 VAT 포함"
    assert parse(literal) is None
    stale = parse(BASE).model_copy(update={"source_literal": literal})
    assert derive_performance_value(stale, [_record()], as_of=AS_OF).value is None


@pytest.mark.parametrize("amount_label", ["계약금액", "금액"])
def test_closed_form_instruction_keeps_annual_conditions_manual(amount_label):
    literal = "5) 주요 사업 내역은 최근 2년 이내 전화 또는 화상 외국어 과정 수행실적 중 연간 기준 " + amount_label + " 1억원 이상 용역 수행 실적만 작성. 수행완료 실적 VAT 포함"
    scope = parse(literal)
    assert scope is not None and scope.similarity_keyword_groups
    assert scope.manual_verification_conditions
    assert derive_performance_value(scope, [_record()], as_of=AS_OF).value is None


@pytest.mark.parametrize("clause", [
    "당해 사업 공동수급 구성원의 참여비율에 따라 실적평가 및 경영상태 점수를 합산",
    "공동수급체로 참여하는 경우 구성원별 실적평가 점수에 참여비율을 적용하여 합산",
])
def test_joint_member_score_weighting_does_not_depend_on_this_bid_prefix(clause):
    assert parse(BASE + clause) is None
    stale = parse(BASE).model_copy(update={"source_literal": BASE + clause})
    assert derive_performance_value(stale, [_record()], as_of=AS_OF).value is None


def test_generic_performance_word_does_not_conflict_with_direct_service_scope():
    scope = parse("최근 3년 국외연수 실시건수. 사업수행 수행실적. 수행완료 실적 VAT 포함")
    assert scope is not None and scope.similarity_keywords == ("국외연수",)


def test_joint_contract_amount_reports_only_the_bidders_share():
    scope = parse(BASE + "공동수급 실적의 계약 금액란에 제안사의 지분만 기재")
    assert scope is not None and scope.consortium_share_rule == "APPLY_SHARE"


@pytest.mark.parametrize("clause", [
    "실적 인정기간은 2025년부터 2026년까지",
    "실적 인정기간 2024. 1. 1. ~ 공고일 전일",
])
def test_fullwidth_digits_cannot_hide_a_manual_condition_from_any_caller(clause):
    # The fact-binding path already NFKC-normalizes the literal. The request,
    # activation and derivation paths must see the same condition, otherwise a
    # full-width fixed period would be auto-aggregated while its binding says REVIEW.
    fullwidth = clause.translate(str.maketrans("0123456789", "０１２３４５６７８９"))
    ascii_scope, wide_scope = parse(BASE + clause), parse(BASE + fullwidth)
    assert ascii_scope is not None and wide_scope is not None
    assert wide_scope.manual_verification_conditions == ascii_scope.manual_verification_conditions != ()
    assert wide_scope.model_dump(exclude={"source_literal"}) == ascii_scope.model_dump(exclude={"source_literal"})
    stale = parse(BASE).model_copy(update={"source_literal": BASE + fullwidth})
    for scope in (wide_scope, stale):
        result = derive_performance_value(scope, [_record()], as_of=AS_OF)
        assert result.status == "REVIEW" and "고정 실적기간" in result.rationale
        assert result.value is result.lower_value is result.upper_value is None
