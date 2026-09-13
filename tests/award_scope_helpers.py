"""Explicit synthetic PPS demand-agency evidence for unrelated award API tests."""
from hashlib import sha256

from sqlalchemy import select

from pai_loop.models import Notice, NoticeVersion


def attach_award_scope(client, notice_key, *, demand_agency_name, demand_agency_code=None):
    """Attach one exact test notice's evidence; never infer it from display agency."""
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
        assert notice is not None
        version_no = max((version.version_no for version in notice.versions), default=0) + 1
        session.add(NoticeVersion(notice_id=notice.id, version_no=version_no,
            file_sha256=sha256(f"SYN-award-scope:{notice_key}:{version_no}".encode()).hexdigest(),
            document_complete=False, extraction_status="PENDING", extraction_confidence=0,
            source_payload={
                "kind": "PPS_NOTICE_METADATA",
                "notice_identity": {"bid_notice_no": notice.bid_notice_no, "revision_no": notice.revision_no},
                "notice_metadata": {"demand_agency_name": demand_agency_name,
                                    "demand_agency_code": demand_agency_code},
            }))
        session.commit()
