"""Synthetic credit source proof; missing units never become numeric defaults."""
from copy import deepcopy

import pytest

from pai_loop import quantitative_scoring as scoring
from pai_loop.quantitative_rule_extraction import merge_validated_quantitative_records, validate_quantitative_attachment_extraction
from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.source_gap_policy import is_quantitative_irrelevant_gap, is_explicit_qualitative_referenced_form_absence
from test_dense_case_source_binding import credit_fixture, record, anchor, ATT, DOC, MANIFEST


QUALITATIVE_FORM_GAP = (
    "별첨7 세부 평가항목 및 배점기준 서식이 본문에 첨부되지 않아 "
    "정성적 평가 세부기준의 원문 확인 불가"
)


def single_column_credit_without_unit():
    raw, _ = credit_fixture()
    candidate = raw['quantitative_tables'][0]['criteria'][0]
    candidate['unit'] = None
    candidate['criterion_literal'] = 'SYN 기업신용평가등급 9점'
    candidate['evidence'] = anchor(candidate['criterion_literal'])
    for row, percent in zip(candidate['cases'], (100, 90, 80, 70), strict=True):
        row['literal'] = '\n'.join([*row['category_values'], f'배점의 {percent}%'])
        row['evidence'] = anchor(row['literal'])
        row['award_kind'] = 'PERCENT_OF_MAX'
        row['award_value'] = percent
    source = '\n'.join([candidate['criterion_literal'],
                         *(row['literal'] for row in candidate['cases']), '정량평가 합계 9점'])
    return raw, source


def aggregate(stored, raw):
    return merge_validated_quantitative_records([stored], expected_documents={ATT:DOC},
        manifest_sha256=MANIFEST, attachment_profiles={ATT:{
            'document_type':'RFP','source_label':'SYN 제안요청서.pdf',
            'missing_or_unreadable':raw['missing_or_unreadable'],
        }})


@pytest.mark.parametrize("gap", [
    QUALITATIVE_FORM_GAP,
    QUALITATIVE_FORM_GAP.replace(" ", ""),
    QUALITATIVE_FORM_GAP.replace("별첨7", "붙임 23").replace("본문에", "본문에 직접") + ".",
])
def test_only_explicit_qualitative_form_absence_leaves_credit_rules_usable(gap):
    raw, source = single_column_credit_without_unit()
    raw['missing_or_unreadable'] = [gap]
    original = deepcopy(raw)
    stored = record(raw, source)
    assert is_explicit_qualitative_referenced_form_absence(gap)
    assert stored.status == 'INCOMPLETE'  # Original proof and fingerprint stay intact.
    assert stored.available_candidates[0].unit is None
    snapshot = stored.model_dump_json()
    profile = aggregate(stored, raw)
    assert profile.status == 'AVAILABLE', profile.issues
    result = scoring.quantitative_request_from_candidate_profile(profile)
    assert result.activation_status == 'AUTO_ACTIVE', result.activation_reasons
    criterion = result.criteria[0]
    assert criterion.unit == 'RATING'
    assert criterion.metric_key == 'company.credit_rating'
    result.facts = [scoring.QuantitativeFact(
        metric_key=criterion.metric_key, value='A0', status='CONFIRMED',
        evidence_key=criterion.metric_key, fact_binding_sha256=criterion.fact_binding_sha256,
    )]
    assert scoring.estimate_quantitative_score(result).estimated_points == 9
    assert raw == original
    assert stored.model_dump_json() == snapshot


@pytest.mark.parametrize("gap", [
    QUALITATIVE_FORM_GAP.replace('정성적 평가', '정량 평가'),
    QUALITATIVE_FORM_GAP.replace('정성적 평가', '정성 및 신용 평가'),
    QUALITATIVE_FORM_GAP.replace('세부 평가항목', '신용등급 세부 평가항목'),
    QUALITATIVE_FORM_GAP + '. 정량 배점표도 누락됨',
    QUALITATIVE_FORM_GAP + '; 신용등급별 점수를 확인할 수 없음',
    QUALITATIVE_FORM_GAP.replace('첨부되지 않아', '판독되지 않아'),
])
def test_qualitative_note_cannot_hide_other_missing_or_unreadable_source(gap):
    assert not is_quantitative_irrelevant_gap(gap)
    assert not is_explicit_qualitative_referenced_form_absence(gap)
    raw, source = credit_fixture()
    raw['missing_or_unreadable'] = [gap]
    assert aggregate(record(raw, source), raw).status == 'INCOMPLETE'


def test_separate_credit_gap_still_blocks_despite_qualitative_only_form_note():
    raw, source = credit_fixture()
    raw['missing_or_unreadable'] = [QUALITATIVE_FORM_GAP, 'SYN 신용등급 배점표 일부 누락']
    assert aggregate(record(raw, source), raw).status == 'INCOMPLETE'


@pytest.mark.parametrize('supplier_label,expected', [
    ('SYN 제안요청서.pdf','AVAILABLE'),
    ('SYN 참고 제안요청서.pdf','INCOMPLETE'),
    ('SYN 공고서.pdf','INCOMPLETE'),
])
def test_proven_qualitative_scope_can_supply_credit_table_to_notice_sibling(supplier_label,expected):
    raw, source = single_column_credit_without_unit()
    raw['missing_or_unreadable'] = [QUALITATIVE_FORM_GAP]
    supplier = record(raw, source)
    aid = 'SYN-NOTICE'
    gap = '제안요청서, 과업내용서 등 별첨 세부 평가기준 문서가 본 SOURCE에 포함되어 있지 않아 정량적 평가표 내용을 확인할 수 없음'
    notice_raw = ExtractionPayload(document_type='NOTICE', requirements=[], quantitative_tables=[],
                                   missing_or_unreadable=[gap], summary='SYN notice')
    declaring = validate_quantitative_attachment_extraction(notice_raw, source_text='SYN 공고문',
        attachment_id=aid, document_sha256='d'*64, manifest_sha256=MANIFEST)
    before = supplier.model_dump_json(), declaring.model_dump_json()
    profile = merge_validated_quantitative_records([supplier,declaring],
        expected_documents={ATT:DOC,aid:'d'*64}, manifest_sha256=MANIFEST,
        attachment_profiles={
            ATT:{'document_type':'RFP','source_label':supplier_label,'missing_or_unreadable':raw['missing_or_unreadable']},
            aid:{'document_type':'NOTICE','source_label':'SYN 공고서.pdf','missing_or_unreadable':[gap]},
        })
    assert profile.status == expected, profile.issues
    assert (supplier.model_dump_json(),declaring.model_dump_json()) == before
    result = scoring.quantitative_request_from_candidate_profile(profile)
    assert len(result.criteria) == (1 if expected == 'AVAILABLE' else 0)


@pytest.mark.parametrize('defect', ['fingerprint','digest','manifest','no_profile','changed_gap','other_issue'])
def test_qualitative_scope_projection_never_bypasses_record_integrity(defect):
    raw, source = single_column_credit_without_unit()
    raw['missing_or_unreadable'] = [QUALITATIVE_FORM_GAP]
    stored = record(raw, source)
    if defect == 'fingerprint': stored = stored.model_copy(update={'validation_fingerprint_sha256':'c'*64})
    elif defect == 'digest': stored = stored.model_copy(update={'document_sha256':'c'*64})
    elif defect == 'manifest': stored = stored.model_copy(update={'manifest_sha256':'c'*64})
    elif defect == 'changed_gap': raw['missing_or_unreadable'] = []
    elif defect == 'other_issue':
        raw['quantitative_tables'][0]['criteria'][0]['max_points'] = 19
        stored = record(raw, source)
    profile = (merge_validated_quantitative_records([stored], expected_documents={ATT:DOC}, manifest_sha256=MANIFEST)
               if defect == 'no_profile' else aggregate(stored, raw))
    assert profile.status == 'INCOMPLETE'
    assert scoring.quantitative_request_from_candidate_profile(profile).criteria == []


@pytest.mark.parametrize('unit', ['원', '%', '점', '', 'SYN-UNKNOWN'])
def test_explicit_wrong_or_empty_credit_unit_is_never_repaired(unit):
    raw, source = credit_fixture()
    stored = record(raw, source)
    candidate = stored.available_candidates[0].model_copy(update={'unit':unit})
    assert scoring._metric_spec(candidate) is None


@pytest.mark.parametrize('defect', ['metric','fact','method','missing_row','overlap','numeric','quote','award','foreign'])
def test_implicit_unit_requires_complete_source_bound_rating_rows(defect):
    raw, source = credit_fixture()
    candidate = record(raw, source).available_candidates[0].model_copy(update={'unit':None})
    if defect == 'metric': candidate = candidate.model_copy(update={'metric':'PERFORMANCE_AMOUNT'})
    elif defect == 'fact': candidate = candidate.model_copy(update={'required_evidence':('SYN-OTHER',)})
    elif defect == 'method': candidate = candidate.model_copy(update={'scoring_method':'FORMULA'})
    elif defect == 'missing_row': candidate = candidate.model_copy(update={'cases':candidate.cases[:-1]})
    else:
        first = candidate.cases[0]
        if defect == 'overlap': first = candidate.cases[1].model_copy(update={'row_order':1})
        elif defect == 'numeric': first = first.model_copy(update={'comparison_value':1})
        elif defect == 'quote': first = first.model_copy(update={'evidence':first.evidence.model_copy(update={'quote':'SYN unrelated'})})
        elif defect == 'award': first = first.model_copy(update={'award_value':8})
        elif defect == 'foreign': first = first.model_copy(update={'evidence':first.evidence.model_copy(update={'attachment_id':'SYN-OTHER'})})
        candidate = candidate.model_copy(update={'cases':(first,*candidate.cases[1:])})
    assert scoring._metric_spec(candidate) is None
    assert scoring._compiled_case_table_contract(candidate) is None
