"""SYN source-structure regressions; equal text in different cells is not noise."""
import io
import zipfile
from xml.etree import ElementTree

import pytest

from pai_loop.pps_enrichment import (
    PpsEnrichmentError, _hwpx_paragraph_text, extract_pps_document_content,
)


def parse(xml: str) -> str:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('mimetype', 'application/hwp+zip')
        archive.writestr('Contents/section0.xml', f'<section>{xml}</section>')
    result = extract_pps_document_content('SYN 평가.hwpx', stream.getvalue())
    assert result.complete
    return result.text


def test_table_cells_are_not_also_transcribed_by_the_owning_paragraph():
    text = parse('<p><run><t>정량 평가</t><table><row>'
                 '<cell><p><run><t>7건 이상</t></run></p></cell>'
                 '<cell><p><run><t>5점</t></run></p></cell>'
                 '</row></table><t>각주: 자기 지분만 인정</t></run></p>')
    assert text == '정량 평가\n7건 이상\n5점\n각주: 자기 지분만 인정'
    assert text.count('7건 이상') == 1


def test_nested_table_preserves_before_after_text_and_repeated_real_cells():
    text = parse('<p><run><t>첫 표</t><table><cell><p><run><t>5점</t>'
                 '<table><cell><p><run><t>5점</t></run></p></cell></table>'
                 '<t>중첩 각주</t></run></p></cell></table><t>끝</t></run></p>')
    assert text.splitlines() == ['첫 표', '5점', '5점', '중첩 각주', '끝']


def test_equal_paragraphs_and_real_repeated_awards_must_not_be_deduplicated():
    assert parse('<p><t>5점</t></p><p><t>5점</t></p>') == '5점\n5점'


def test_split_word_runs_and_unicode_normalization_are_preserved():
    assert parse('<p><run><t>참가</t></run><run><t>자격</t></run>'
                 '<run><t>  가점</t></run></p>') == '참가자격 가점'


def test_direct_text_minimal_producer_and_nested_direct_paragraphs():
    assert parse('<p>소계<table><cell><p>5점</p></cell></table>각주</p>') == '소계\n5점\n각주'


def test_mixed_runs_and_direct_paragraph_inside_table():
    assert parse('<p><run><t>평가</t><table><cell><p>5점</p></cell></table>'
                 '<t>끝</t></run></p>') == '평가\n5점\n끝'


def test_multiple_fragments_stay_owned_when_a_footnote_contains_a_paragraph():
    assert parse('<p><run><t>실적 인정</t><footnote><p><t>최근 3년</t></p>'
                 '</footnote><t> 부가세 포함</t></run></p>') == '실적 인정\n최근 3년\n부가세 포함'


def test_text_outside_a_paragraph_is_not_promoted_to_source():
    assert parse('<metadata><t>12345</t></metadata><p><t>배점 5점</t></p>') == '배점 5점'


def test_deep_untrusted_containers_do_not_use_python_recursion():
    assert parse('<p>' + '<run>' * 1100 + '<t>5점</t>' + '</run>' * 1100 + '</p>') == '5점'


def test_character_limit_applies_while_accumulating_owned_paragraphs():
    root = ElementTree.fromstring('<section><p><t>12345</t></p><p><t>67890</t></p></section>')
    assert _hwpx_paragraph_text(root, maximum=12) == ['12345', '67890']
    with pytest.raises(PpsEnrichmentError, match='DOCUMENT_TEXT_TOO_LARGE'):
        _hwpx_paragraph_text(root, maximum=11)
