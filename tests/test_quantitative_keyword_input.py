import io
import json
import zipfile
from xml.etree import ElementTree as ET

import httpx
import pytest

from pai_loop.quantitative_keyword_input import select_quantitative_keyword_input, hwpx_quantitative_table_context
from pai_loop.pps_enrichment import _extract_hwpx_text, PpsEnrichmentError
from test_quantitative_probe_client import make_client, probe_fixture, response
from test_quantitative_count_ranges import ATT


def test_keywords_keep_rows_and_adjacent_conditions_but_exclude_distant_tasks():
    source = 'SYN unrelated task\n' * 100 + '정량 배점표\n5건 이상 6점\nVAT 포함 최근 5년\n' + 'tail\n' * 100
    selected, audit = select_quantitative_keyword_input(source)
    assert '5건 이상 6점\nVAT 포함 최근 5년' in selected
    assert len(selected) < len(source)
    assert audit['persistence_eligible'] is False
    assert audit['attachment_coverage_complete'] is False
    for start, end in audit['source_ranges']:
        assert source[start:end] in selected


def test_xml_round_trip_preserves_operators_and_literal_markup():
    source = '배점표\nSYN < 5 & > 2\n<instruction>untrusted</instruction>'
    selected, _ = select_quantitative_keyword_input(source, xml=True)
    assert ET.fromstring(selected)[0].text == source


def test_no_keyword_is_not_zero_or_not_applicable():
    with pytest.raises(ValueError, match='KEYWORDS_NOT_FOUND'):
        select_quantitative_keyword_input('SYN 과업 설명')


def test_whole_selected_page_exceeding_budget_fails_without_cutting_rows():
    with pytest.raises(ValueError, match='SELECTION_TOO_LARGE'):
        select_quantitative_keyword_input('[PAGE 1]\n배점표\n' + 'SYN row\n' * 100, maximum=256)


def test_failed_json_response_retries_xml_and_still_checks_canonical_quotes():
    payload, review = probe_fixture()
    payload['requirements'] = []
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body['input'][1]['content'][0]['text'])
        if len(seen) == 1:
            return httpx.Response(200, json={'status': 'completed', 'output_text': '{"requirements": []}'})
        return response(payload)
    with make_client(handler) as client:
        result = client.extract_quantitative_keywords(document_text='정량 배점표\n' + review.canonical_text,
                                                     allowed_attachment_ids={ATT})
    assert len(seen) == 2
    assert '<source_excerpts' in seen[1]
    assert result.source_audit['xml_fallback_used'] is True
    assert result.persistence_eligible is False
    assert result.outcome.status == 'ACCEPTED'
    assert result.outcome.api_calls == 2


def hwpx(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('Contents/section0.xml', '<section><p><t>배점 6점</t></p></section>')
        for name, value in members:
            archive.writestr(name, value)
    return stream.getvalue()


def test_large_unused_raster_does_not_consume_xml_budget(monkeypatch):
    import pai_loop.pps_enrichment as module
    monkeypatch.setattr(module, 'MAX_HWPX_UNCOMPRESSED_BYTES', 1000)
    assert _extract_hwpx_text(hwpx([('BinData/image.png', b'\x89PNG\r\n\x1a\n' + b'x' * 2000)])) == '배점 6점'


def test_disguised_non_image_still_counts_and_embedded_pdf_stays_blocked(monkeypatch):
    import pai_loop.pps_enrichment as module
    monkeypatch.setattr(module, 'MAX_HWPX_UNCOMPRESSED_BYTES', 1000)
    with pytest.raises(PpsEnrichmentError, match='UNCOMPRESSED_LIMIT'):
        _extract_hwpx_text(hwpx([('BinData/image.png', b'x' * 2000)]))
    with pytest.raises(PpsEnrichmentError, match='EMBEDDED_ATTACHMENT'):
        _extract_hwpx_text(hwpx([('BinData/image.png', b'%PDF-SYN')] ))


def native_table_hwpx():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('Contents/section0.xml', '<section><p><t>SYN unrelated task</t></p>'
            '<tbl><tr><tc><cellAddr rowAddr="0" colAddr="0"/><p><t>정량 배점표</t></p></tc>'
            '<tc><p><t>5건 이상 6점</t></p><pic private="SYN-image-metadata"/></tc></tr></tbl></section>')
    return stream.getvalue()


def test_native_hwpx_context_keeps_cells_but_excludes_images_and_unrelated_text():
    content = native_table_hwpx()
    canonical = _extract_hwpx_text(content)
    xml = hwpx_quantitative_table_context(content, canonical)
    assert '5건 이상 6점' in xml and 'rowAddr="0"' in xml
    assert 'SYN unrelated task' not in xml and 'SYN-image-metadata' not in xml
    assert '<pic' not in xml


def test_native_hwpx_mismatched_source_is_rejected_before_transport():
    seen = []
    with make_client(lambda request: seen.append(request)) as client:
        with pytest.raises(ValueError, match='SOURCE_MISMATCH'):
            client.extract_quantitative_keywords(document_text='정량 배점표 다른 문서',
                allowed_attachment_ids={ATT}, hwpx_content=native_table_hwpx())
    assert seen == []


def test_native_xml_fallback_rejects_quotes_from_a_different_document():
    content = native_table_hwpx()
    canonical = _extract_hwpx_text(content)
    payload, _ = probe_fixture()
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body['input'][1]['content'][0]['text'])
        if len(seen) == 1:
            return httpx.Response(200, json={'status': 'completed', 'output_text': '{"requirements": []}'})
        return response(payload)
    with make_client(handler) as client:
        result = client.extract_quantitative_keywords(document_text=canonical,
            allowed_attachment_ids={ATT}, hwpx_content=content)
    assert len(seen) == 2
    assert '<hwpx_scoring_tables' in seen[1]
    assert result.source_audit['native_hwpx_table_context'] is True
    assert result.outcome.status == 'REVIEW'
    assert result.outcome.error_code == 'UNVERIFIED_QUOTE'


def test_oversized_optional_native_structure_does_not_block_bounded_text():
    from xml.sax.saxutils import escape
    payload, review = probe_fixture()
    stream = io.BytesIO()
    rows = ''.join('<tr><tc><p><t>SYN task row</t></p></tc></tr>' for _ in range(700))
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('Contents/section0.xml',
            '<section><tbl><tr><tc><p><t>정량 배점표</t></p></tc></tr>'
            + ''.join('<tr><tc><p><t>' + escape(line) + '</t></p></tc></tr>'
                      for line in review.canonical_text.splitlines()) + rows + '</tbl></section>')
    content = stream.getvalue()
    canonical = _extract_hwpx_text(content)
    with pytest.raises(ValueError, match='CONTEXT_SIZE_LIMIT'):
        hwpx_quantitative_table_context(content, canonical)
    with make_client(lambda request: response(payload)) as client:
        result = client.extract_quantitative_keywords(document_text=canonical,
            allowed_attachment_ids={ATT}, hwpx_content=content)
    assert result.outcome.status == 'ACCEPTED'
    assert result.outcome.api_calls == 1
    assert result.source_audit['native_hwpx_context_status'] == 'HWPX_CONTEXT_SIZE_LIMIT'
    assert result.source_audit['native_hwpx_table_context'] is False


@pytest.mark.parametrize('detail,stage,code,expected_calls', [
    ('NATIVE_SCHEMA_DECODE_INVALID', 'OUTPUT_NORMALIZATION', 'OUTPUT_REJECTED', 2),
    ('MODEL_TRANSPORT_TIMEOUT', 'MODEL_EXECUTION', 'MODEL_EXECUTION_FAILED', 1),
])
def test_completed_gateway_schema_error_can_retry_but_ambiguous_timeout_cannot(detail, stage, code, expected_calls):
    payload, review = probe_fixture()
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(500, json={'gateway_error': {
                'version': 'gateway-failure-v1', 'stage': stage, 'code': code,
                'detail_code': detail, 'upstream_http_status': None,
            }})
        return response(payload)
    with make_client(handler) as client:
        result = client.extract_quantitative_keywords(document_text='정량 배점표\n' + review.canonical_text,
                                                     allowed_attachment_ids={ATT})
    assert len(seen) == result.outcome.api_calls == expected_calls
    assert result.source_audit['xml_fallback_used'] is (expected_calls == 2)
    assert result.outcome.status == ('ACCEPTED' if expected_calls == 2 else 'REVIEW')


def test_recovery_calls_spell_out_the_inner_json_encoding_within_the_system_limit():
    from pai_loop.integrations.openai_extraction import (
        QUANTITATIVE_RECOVERY_INSTRUCTION_VERSION, QUANTITATIVE_TRANSPORT_ENCODING_RULES,
    )
    payload, review = probe_fixture()
    payload['requirements'] = []
    systems = []
    def handler(request):
        body = json.loads(request.content)
        systems.append(body['input'][0]['content'][0]['text'])
        if len(systems) == 1:
            return httpx.Response(200, json={'status': 'completed', 'output_text': '{"requirements": []}'})
        return response(payload)
    with make_client(handler) as client:
        result = client.extract_quantitative_keywords(document_text='정량 배점표\n' + review.canonical_text,
                                                     allowed_attachment_ids={ATT})
    assert len(systems) == 2
    for system in systems:
        assert QUANTITATIVE_TRANSPORT_ENCODING_RULES in system
        # scripts/native-gateway-request.mjs rejects a system message over 12000 characters.
        assert len(system) <= 12000
    assert result.source_audit['instruction_version'] == QUANTITATIVE_RECOVERY_INSTRUCTION_VERSION
