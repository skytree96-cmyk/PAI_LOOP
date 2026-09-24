from copy import deepcopy

import pytest
from sqlalchemy import select

from pai_loop.models import Notice, NoticeVersion
from pai_loop.pps_enrichment import PPS_METADATA_KIND, PPS_METADATA_SCHEMA


@pytest.mark.parametrize("change,expected", [
    ({}, "제한경쟁"),
    ({"notice_metadata": {"contract_method": "협상에 의한 계약"}}, "협상에 의한 계약"),
    ({"notice_metadata": {"contract_method": "SYN private contact"}}, None),
    ({"notice_metadata": {}}, None),
    ({"notice_identity": {"bid_notice_no": "SYN-OTHER", "revision_no": "00"}}, None),
    ({"canonical_notice_basis": {"title": "SYN stale title"}}, None),
    ({"schema_version": "old"}, None),
])
def test_contract_method_projects_only_current_known_label(client, change, expected):
    deadline = "2030-01-01T00:00:00Z"
    created = client.post("/api/v1/notices", json={
        "notice_key": "SYN-METHOD", "bid_notice_no": "SYN-METHOD", "revision_no": "00",
        "title": "SYN contract label", "agency": "SYN agency", "deadline": deadline,
    })
    assert created.status_code == 201
    payload = {
        "kind": PPS_METADATA_KIND, "schema_version": PPS_METADATA_SCHEMA,
        "notice_identity": {"bid_notice_no": "SYN-METHOD", "revision_no": "00"},
        "canonical_notice_basis": {"notice_key": "SYN-METHOD", "title": "SYN contract label", "agency": "SYN agency", "deadline": deadline},
        "notice_metadata": {"contract_method": "제한경쟁"}, "SYN_private": "SYN-secret-content",
    }
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == "SYN-METHOD"))
        # A newer metadata observation supersedes even a valid older label.
        for number, source in enumerate([payload, {**deepcopy(payload), **change}], start=1):
            session.add(NoticeVersion(notice_id=notice.id, version_no=number, file_sha256=str(number) * 64,
                                      source_payload=source, extraction_status="METADATA"))
        session.commit()
    for path in ["/api/v1/notices", "/api/v1/notices/SYN-METHOD"]:
        response = client.get(path)
        assert response.status_code == 200, response.text
        body = response.json()
        row = body[0] if isinstance(body, list) else body
        assert row["contract_method"] == expected
        assert "SYN-secret-content" not in response.text
