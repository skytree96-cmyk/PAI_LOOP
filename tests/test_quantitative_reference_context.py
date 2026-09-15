"""SYN-only explicit form chains; no source files, I/O or stored record promotion."""
from dataclasses import replace
import hashlib

import pytest

import pai_loop.quantitative_reference_context as reference
from pai_loop.quantitative_reference_context import (
    PerformanceReferenceOwner, SourceSpan, resolve_performance_reference_context,
    verify_performance_reference_context,
)


FORM4 = """【서식 4】SYN 용역 수행 실적
사업명 / 사업기간 / 계약금액 / 발주처
※ 최근 2년간 완료된 SYN 교육 용역 실적을 기재합니다.
※ 연간 기준 금액을 확인하고 【서식 5】 실적증명서를 제출합니다.
"""
FORM5 = """【서식 5】SYN 실적증명서
신청인 / 실적내용 / 계약금액 / 증명서발급기관
※ 계약금액은 부가세 포함 금액이며 발급기관이 증명합니다.
"""
END = "【서식 6】SYN 가격 제안서\n금액은 별도 입력합니다.\n"
ROW = "SYN 연수 수행 실적: 【서식 4】참조, 배점 10점."


def fixture(*, row=ROW, forms=FORM4 + FORM5 + END, prefix=""):
    source = prefix + "【첨부 1】SYN 정량 기준\n" + row + "\n【첨부 2】SYN 다음 평가\n" + forms
    start = source.index(row)
    table_start = source.index("【첨부 1】")
    table_end = source.index("【첨부 2】")
    owner = PerformanceReferenceOwner("SYN-ATTACHMENT", "PERFORMANCE_COUNT",
        SourceSpan(table_start, table_end, source[table_start:table_end]),
        SourceSpan(start, start + len(row), row))
    return source, owner


def resolve(source, owner, **overrides):
    args = dict(source_text=source, source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        source_attachment_id="SYN-ATTACHMENT", owner=owner)
    args.update(overrides)
    return resolve_performance_reference_context(**args)


def assert_blocked(result, reason=None):
    assert result.status == "BLOCKED"
    if reason:
        assert result.reason == reason
    assert result.edges == result.condition_spans == ()
    assert result.proof_sha256 is None
    assert result.persistence_eligible is False


def test_complete_chain_preserves_all_text_and_anchors_without_changing_the_rule():
    source, owner = fixture()
    original = source, owner
    result = resolve(source, owner)
    assert result.status == "RESOLVED"
    assert [edge.form_key for edge in result.edges] == ["서식:4", "서식:5"]
    assert "".join(span.quote for span in result.condition_spans) == FORM4 + FORM5
    for edge in result.edges:
        assert edge.reference.quote in ("【서식 4】", "【서식 5】")
        for span in (edge.reference, edge.target_heading, edge.target_block):
            assert source[span.start:span.end] == span.quote
    assert verify_performance_reference_context(result, source_text=source, source_attachment_id="SYN-ATTACHMENT")
    assert result.persistence_eligible is False
    assert (source, owner) == original


def test_contents_entry_cannot_replace_the_actual_form_and_does_not_count_as_a_duplicate():
    contents = "목차\n【서식 4】SYN 용역 수행 실적\n【서식 5】SYN 실적증명서\n【서식 6】SYN 가격 제안서\n"
    source, owner = fixture(prefix=contents)
    assert resolve(source, owner).status == "RESOLVED"
    source, owner = fixture(prefix=contents, forms=END)
    assert_blocked(resolve(source, owner), "FORM_MISSING_OR_DUPLICATE")


@pytest.mark.parametrize("mutation", ["sha", "source", "owner_quote", "offset", "different_attachment", "outside_table", "sibling_overlap", "unsupported_metric"])
def test_source_and_owner_binding_cannot_be_forged(mutation):
    source, owner = fixture()
    options = {}
    if mutation == "sha": options["source_sha256"] = "0" * 64
    elif mutation == "source":
        options["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
        source += "SYN tampered source"
    elif mutation == "owner_quote": owner = replace(owner, criterion_span=replace(owner.criterion_span, quote="SYN false"))
    elif mutation == "offset": owner = replace(owner, criterion_span=replace(owner.criterion_span, start=owner.criterion_span.start + 1))
    elif mutation == "different_attachment": owner = replace(owner, attachment_id="SYN-OTHER")
    elif mutation == "outside_table": owner = replace(owner, table_span=SourceSpan(0, 5, source[:5]))
    elif mutation == "sibling_overlap": owner = replace(owner, sibling_criterion_spans=(owner.criterion_span,))
    else: owner = replace(owner, metric="CREDIT_RATING")
    assert_blocked(resolve(source, owner, **options))


@pytest.mark.parametrize("forms", [FORM4 + FORM4 + FORM5 + END, FORM4 + END, FORM4 + FORM5,
    FORM4.replace("【서식 5】", "【서식 9】") + FORM5 + END,
    FORM4.replace("【서식 5】", "(서식 5)") + FORM5 + END,
    FORM4 + FORM5.replace("※ 계약금액", "※ 【서식 4】참조. 계약금액") + END,
    FORM4 + "【서식 8】\n" + FORM5 + END,
    FORM4.replace("【서식 5】", "다른 첨부파일의 【서식 5】") + FORM5 + END])
def test_duplicate_missing_cyclic_external_or_unbounded_forms_fail_as_a_whole(forms):
    source, owner = fixture(forms=forms)
    assert_blocked(resolve(source, owner))


def test_no_direct_reference_never_attaches_a_similar_named_form():
    source, owner = fixture(row="SYN 연수 수행 실적: 최근 3년 완료 건수, 배점 5점.")
    result = resolve(source, owner)
    assert result.status == result.reason == "NO_EXPLICIT_REFERENCE"
    assert result.condition_spans == ()


@pytest.mark.parametrize("row", ["SYN 수행 실적: (서식 4) 참조.",
    "SYN 수행 실적: 【서식 4】참조 및 서식 8 참조.",
    "SYN 수행 실적: 【서식 4】참조 및 【서식 5】참조.",
    "SYN 수행 실적: 【서식 4]참조."])
def test_unhandled_or_multiple_references_do_not_become_partial_success(row):
    source, owner = fixture(row=row)
    assert_blocked(resolve(source, owner))


@pytest.mark.parametrize("row", ["SYN 수행 실적: 【서식 4】는 참조하지 않습니다.",
    "SYN 수행 실적: 【서식 4】는 작성 예시로만 참고합니다."])
def test_negated_or_example_references_are_not_recognition_authority(row):
    source, owner = fixture(row=row)
    assert_blocked(resolve(source, owner), "REFERENCE_ACTION_OR_ATTACHMENT_UNPROVEN")


def test_example_form_cannot_substitute_for_an_actual_form():
    source, owner = fixture(forms=FORM4.replace("SYN 용역 수행 실적", "SYN 용역 수행 실적 예시") + FORM5 + END)
    assert_blocked(resolve(source, owner), "FORM_HEADING_UNPROVEN")


def test_incomplete_same_number_form_is_not_discarded_as_a_contents_entry():
    incomplete = FORM4.replace("사업기간 / ", "").replace("최근 2년", "최근 5년")
    source, owner = fixture(forms=incomplete + FORM4 + FORM5 + END)
    assert_blocked(resolve(source, owner), "FORM_INCOMPLETE_OR_AMBIGUOUS")


@pytest.mark.parametrize("where", ["row", "form"])
def test_distant_negation_in_the_owned_region_cannot_be_missed_by_a_short_window(where):
    tail = ("SYN 절차 안내 " * 20) + "단, 위 서식은 제출하지 않습니다."
    source, owner = fixture(row=ROW + tail) if where == "row" else fixture(forms=FORM4 + tail + "\n" + FORM5 + END)
    assert_blocked(resolve(source, owner), "REFERENCE_ACTION_OR_ATTACHMENT_UNPROVEN")


def test_duplicate_normalized_owner_is_ambiguous_including_whitespace_variation():
    source, owner = fixture(prefix=ROW.replace(" ", "\t") + "\n")
    assert_blocked(resolve(source, owner), "OWNER_REGION_AMBIGUOUS")
    assert reference._unique("ababa", "aba") is False  # overlapping repeats count


def test_all_text_fits_only_with_closed_sentence_chunks_and_none_is_truncated():
    added = "※ " + "SYN 인정조건 " * 11 + "확인합니다.\n"
    forms = FORM4.replace("※ 연간", added * 3 + "※ 연간") + FORM5 + END
    source, owner = fixture(forms=forms)
    result = resolve(source, owner)
    assert result.status == "RESOLVED"
    expected = forms[:forms.index("【서식 6】")]
    assert "".join(span.quote for span in result.condition_spans) == expected
    assert all(len(span.quote) <= 500 for span in result.condition_spans)
    # No sentence boundary before the cap: the full condition is refused.
    source, owner = fixture(forms=FORM4.replace("※ 연간", "SYN " * 135 + "※ 연간") + FORM5 + END)
    assert_blocked(resolve(source, owner), "CONDITION_BOUNDARY_OR_ANCHOR_UNPROVEN")


@pytest.mark.parametrize("limit", ["MAX_SOURCE_CHARACTERS", "MAX_REFERENCE_MARKERS", "MAX_CONTEXT_CHARACTERS", "MAX_CONDITIONS"])
def test_resource_limits_fail_without_partial_additions(monkeypatch, limit):
    source, owner = fixture()
    monkeypatch.setattr(reference, limit, 1)
    assert_blocked(resolve(source, owner))


@pytest.mark.parametrize("mutation", ["digest", "edge", "condition", "omitted_condition", "owner", "source", "attachment"])
def test_consumer_recomputes_proof_instead_of_trusting_stored_or_edited_fields(mutation):
    source, owner = fixture()
    result = resolve(source, owner)
    aid = "SYN-ATTACHMENT"
    if mutation == "digest": result = replace(result, proof_sha256="0" * 64)
    elif mutation == "edge": result = replace(result, edges=(replace(result.edges[0], form_key="서식:9"), *result.edges[1:]))
    elif mutation == "condition": result = replace(result, condition_spans=(replace(result.condition_spans[0], quote="SYN false"),))
    elif mutation == "omitted_condition": result = replace(result, condition_spans=result.condition_spans[:-1])
    elif mutation == "owner": result = replace(result, owner=replace(owner, attachment_id="SYN-OTHER"))
    elif mutation == "source": source = source.replace("최근 2년", "최근 3년")
    else: aid = "SYN-OTHER"
    assert not verify_performance_reference_context(result, source_text=source, source_attachment_id=aid)


def test_owner_table_open_to_eof_is_not_a_proven_table_end():
    # A table region that runs to the end of the document has no proven
    # termination. The very same source resolves once the table is closed at
    # the next section title, so the block comes from the open region alone.
    source, owner = fixture()
    assert resolve(source, owner).status == "RESOLVED"
    open_owner = replace(owner, table_span=SourceSpan(
        owner.table_span.start, len(source), source[owner.table_span.start:]))
    assert_blocked(resolve(source, open_owner), "FORM_BOUNDARY_OR_ROLE_UNPROVEN")


@pytest.mark.parametrize("closing", ["next_section", "form_heading"])
def test_table_closed_at_an_explicit_following_title_resolves(closing):
    source, owner = fixture()
    if closing == "form_heading":
        # No intervening section title: the table must end exactly where the
        # standalone form title starts. Nothing of the form may be owned.
        source = source.replace("【첨부 2】SYN 다음 평가\n", "")
        start = source.index(ROW)
        table_start = source.index("【첨부 1】")
        table_end = source.index("【서식 4】SYN")
        owner = PerformanceReferenceOwner("SYN-ATTACHMENT", "PERFORMANCE_COUNT",
            SourceSpan(table_start, table_end, source[table_start:table_end]),
            SourceSpan(start, start + len(ROW), ROW))
    result = resolve(source, owner)
    assert result.status == "RESOLVED"
    assert result.owner.table_span.end <= result.edges[0].target_heading.start
    assert "".join(span.quote for span in result.condition_spans) == FORM4 + FORM5


def test_table_span_that_swallows_the_form_title_is_blocked():
    # An over-long table region that owns even part of the form title is a
    # boundary conflict, not a longer recognition scope.
    source, owner = fixture()
    heading = source.index("【서식 4】SYN")
    end = heading + len("【서식 4】SYN")
    swallowing = replace(owner, table_span=SourceSpan(
        owner.table_span.start, end, source[owner.table_span.start:end]))
    assert_blocked(resolve(source, swallowing), "FORM_BOUNDARY_OR_ROLE_UNPROVEN")
