"""Real PostgreSQL atomic one-shot reservations, only the disposable CI service."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Barrier
import pytest

from pai_loop.database import Base, build_session_factory
from pai_loop.extraction_contracts import CURRENT_EXTRACTION_CONTRACT
from pai_loop.long_output_policy import consume_long_output, mark_long_output_dispatch
from pai_loop.models import Notice, NoticeVersion
from test_long_output_once import FAILURE
from test_postgres_department_accounts import postgres_account_engine


@pytest.mark.parametrize("new_failure_version", [False, True])
def test_postgres_only_one_reservation_and_one_dispatch_across_sessions(postgres_account_engine, new_failure_version):
    Base.metadata.create_all(postgres_account_engine)
    factory = build_session_factory(postgres_account_engine)
    with factory() as session:
        notice = Notice(notice_key="PPS-SYN-LONG-PG", bid_notice_no="SYN-LONG-PG", title="SYN", agency="SYN", status="OPEN",
                        deadline=datetime.now(timezone.utc) + timedelta(days=3))
        session.add(notice)
        session.flush()
        version = NoticeVersion(notice_id=notice.id, version_no=1, file_sha256="a" * 64,
            source_payload=dict(kind="OPENAI_REQUIREMENT_EXTRACTION", source_kind="PPS_PUBLIC_ATTACHMENT",
                status="REVIEW", error_code="HTTP_ERROR", message="모델 API가 HTTP 500를 반환했습니다.",
                gateway_failure=deepcopy(FAILURE), document_sha256="a" * 64,
                attachment_id="SYN-ATT-1", current_manifest_sha256="b" * 64, manifest_sha256="c" * 64,
                prompt_version=CURRENT_EXTRACTION_CONTRACT.prompt, schema_version=CURRENT_EXTRACTION_CONTRACT.schema,
                processing_version=CURRENT_EXTRACTION_CONTRACT.processing,
                document_processing=dict(source_read_complete=True, analysis_input_complete=True,
                                         source_text_sha256="d" * 64, analysis_input_sha256="e" * 64)))
        session.add(version)
        session.commit()
        version_id = version.id
        version_ids = [version_id, version_id]
        if new_failure_version:
            newer = NoticeVersion(notice_id=notice.id, version_no=2, file_sha256=version.file_sha256,
                                  source_payload=deepcopy(version.source_payload))
            session.add(newer)
            session.commit()
            version_ids[1] = newer.id
    barrier = Barrier(2)
    def reserve(candidate_id):
        barrier.wait()
        with factory() as session:
            try: return consume_long_output(session, version_id=candidate_id, validate_current=lambda _: True), candidate_id
            except ValueError as exc: return str(exc), candidate_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(pool.map(reserve, version_ids))
    assert sum(result == "LONG_OUTPUT_ALREADY_CONSUMED" for result, _ in reservations) == 1
    claim_id, version_id = next(pair for pair in reservations if pair[0] != "LONG_OUTPUT_ALREADY_CONSUMED")
    barrier = Barrier(2)
    def dispatch():
        barrier.wait()
        with factory() as session:
            try:
                mark_long_output_dispatch(session, claim_id, version_id)
                return "DISPATCH_AUTHORIZED"
            except ValueError as exc: return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatch(), range(2)))
    assert results.count("DISPATCH_AUTHORIZED") == 1
    assert results.count("LONG_OUTPUT_ALREADY_DISPATCHED") == 1
