"""PPS structured licence/region limits as an eligibility cross-check (2026-10-08). Never a decision source."""
from datetime import datetime, timedelta, timezone

from pai_loop.models import Notice, PpsParticipationRestriction
from pai_loop.pps_restrictions import (
    LICENSE_OPERATION, REGION_OPERATION, check_restrictions, fetch_restrictions, refresh_open_notices,
)

CODES = {"1169", "1261", "1469", "1517", "6529", "9999"}


def test_groups_are_alternatives_and_rows_inside_a_group_are_all_required():
    # "출판사(1517) 또는 인쇄사(1518)"
    assert check_restrictions([[["1517"]], [["1518"]]], [], CODES)["status"] == "CONSISTENT"
    # "1469+6527 또는 1469+6529 또는 9999": the second group is fully met.
    assert check_restrictions([[["1469"], ["6527"]], [["1469"], ["6529"]]], [], CODES)["status"] == "CONSISTENT"
    # No group fully met.
    result = check_restrictions([[["1469"], ["6527"]], [["3230"]]], [], CODES)
    assert result["status"] == "CONFLICT" and "업종" in result["reasons"][0]
    # A permitted-industry alternative satisfies its row.
    assert check_restrictions([[["1262", "1261"]]], [], CODES)["status"] == "CONSISTENT"
    assert check_restrictions([], [], CODES)["status"] == "NO_LIMIT"


def test_eligible_regions_must_include_the_head_office():
    assert check_restrictions([], ["서울특별시"], CODES)["status"] == "CONSISTENT"
    result = check_restrictions([], ["전남광주통합특별시 광양시", "전남광주통합특별시 순천시"], CODES)
    assert result["status"] == "CONFLICT" and "서울" in result["reasons"][0]


def _envelope(items):
    return {"response": {"header": {"resultCode": "00"}, "body": {"totalCount": len(items), "items": items}}}


class _FakeClient:
    def __init__(self, license_rows, region_rows):
        self.rows = {LICENSE_OPERATION: license_rows, REGION_OPERATION: region_rows}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def _request(self, operation, params, *, timeout_seconds=None):
        self.calls.append((operation, params["bidNtceNo"], params["bidNtceOrd"]))
        return _envelope(self.rows[operation])


LICENSE_ROWS = [
    {"lmtGrpNo": "1", "lmtSno": "1", "lcnsLmtNm": "종합여행업/1261", "permsnIndstrytyList": ""},
    {"lmtGrpNo": "2", "lmtSno": "1", "lcnsLmtNm": "국내외여행업/1262", "permsnIndstrytyList": "[종합여행업/1261]"},
]


def test_fetch_parses_codes_and_permitted_industries():
    groups, regions = fetch_restrictions(_FakeClient(LICENSE_ROWS, [{"prtcptPsblRgnNm": "서울특별시"}]), "R26BK1", "0")
    assert groups == [[["1261"]], [["1261", "1262"]]]
    assert regions == ["서울특별시"]


def test_refresh_stores_rows_per_revision_and_detail_reports_the_check(client):
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        notice = Notice(notice_key="PPS-R26BK09990001-000-syn", bid_notice_no="R26BK09990001", revision_no="000",
                        title="SYN 해외연수", category="용역", status="OPEN", agency="SYN 기관",
                        deadline=now + timedelta(days=5), published_at=now - timedelta(days=1))
        session.add(notice)
        session.commit()
        fake = _FakeClient(LICENSE_ROWS, [{"prtcptPsblRgnNm": "전남광주통합특별시 광양시"}])
        result = refresh_open_notices(session, service_key="SYN", client_factory=lambda key: fake)
        assert result == {"targets": 1, "fetched": 1, "failed": 0}
        assert fake.calls[0] == (LICENSE_OPERATION, "R26BK09990001", "000")
        row = session.get(PpsParticipationRestriction, notice.id)
        assert row.license_groups == [[["1261"]], [["1261", "1262"]]] and row.revision_no == "000"
        # Nothing to do on the next run for the same revision.
        assert refresh_open_notices(session, service_key="SYN", client_factory=lambda key: fake)["targets"] == 0
    check = client.get("/api/v1/notices/PPS-R26BK09990001-000-syn").json()["pps_restriction_check"]
    assert check["status"] == "CONFLICT" and "서울" in check["reasons"][0]


def test_detail_without_a_row_reports_nothing(client):
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        session.add(Notice(notice_key="PPS-R26BK09990002-000-syn", bid_notice_no="R26BK09990002", revision_no="000",
                           title="SYN", category="용역", status="OPEN", agency="SYN",
                           deadline=now + timedelta(days=5), published_at=now))
        session.commit()
    assert client.get("/api/v1/notices/PPS-R26BK09990002-000-syn").json()["pps_restriction_check"] is None
