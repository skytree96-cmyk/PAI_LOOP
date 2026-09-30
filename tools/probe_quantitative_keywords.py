"""Bounded local selection / explicit live diagnostic; never persist a score."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pai_loop.integrations.openai_extraction import OpenAIExtractionClient
from pai_loop.pps_enrichment import extract_pps_document_content
from pai_loop.quantitative_keyword_input import (
    hwpx_quantitative_table_context, select_quantitative_keyword_input,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--attachment-id', default='SYN-LOCAL-DIAGNOSTIC')
    parser.add_argument('--execute', action='store_true', help='Allow up to two gateway calls; no database writes')
    args = parser.parse_args()
    if args.source.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('LOCAL_SOURCE_LIMIT')
    native = None
    if args.source.suffix.casefold() == '.hwpx':
        native = args.source.read_bytes()
        extracted = extract_pps_document_content(args.source.name, native)
        if not extracted.complete:
            raise ValueError('LOCAL_SOURCE_INCOMPLETE')
        source = extracted.text
        hwpx_quantitative_table_context(native, source)
    elif args.source.suffix.casefold() == '.txt':
        source = args.source.read_text(encoding='utf-8-sig')
    else:
        raise ValueError('LOCAL_SOURCE_TYPE_UNSUPPORTED')
    _, selection = select_quantitative_keyword_input(source)
    report = {k: selection[k] for k in ('selection_method', 'original_characters',
              'selected_display_characters', 'persistence_eligible', 'attachment_coverage_complete')}
    report.update(database_writes=0, provider_calls=0)
    if args.execute:
        key = os.environ.get('PAI_LOOP_API_KEY')
        gateway = os.environ.get('PAI_LOOP_LLM_GATEWAY_BASE_URL')
        if not key or not gateway:
            raise ValueError('EXISTING_GATEWAY_CREDENTIAL_REQUIRED')
        with OpenAIExtractionClient(api_key=key, provider='n8n_claude',
                model=os.environ.get('PAI_LOOP_CLAUDE_MODEL', 'claude-sonnet-5'),
                base_url=gateway, max_total_api_calls=1, max_retries=0) as client:
            result = client.extract_quantitative_keywords(document_text=source,
                allowed_attachment_ids={args.attachment_id}, hwpx_content=native)
        report.update(status=result.outcome.status, error_code=result.outcome.error_code,
                      provider_calls=result.outcome.api_calls,
                      xml_fallback_used=result.source_audit['xml_fallback_used'],
                      table_count=len(result.outcome.data.quantitative_tables) if result.outcome.data else 0)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        # No exception text: filenames, environment and provider data are private.
        print(json.dumps({'status': 'REVIEW', 'error_class': type(error).__name__}))
        raise SystemExit(1) from None
