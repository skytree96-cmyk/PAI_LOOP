from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import func, select

from pai_loop.models import AwardHistoryItem, IngestionJob, Notice, NoticeVersion


def target(client, *, verified=True):
    key = 'PPS-SYN-SCOPED-TARGET'
    response = client.post('/api/v1/notices', json={
        'notice_key': key, 'bid_notice_no': 'SYN-SCOPED-TARGET',
        'title': 'SYN 통합교육 운영 용역', 'agency': 'SYN 공고대행기관',
        'deadline': '2099-01-01T00:00:00Z', 'published_at': '2026-09-13T00:00:00Z',
    })
    assert response.status_code == 201
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == key))
        if verified:
            session.add(NoticeVersion(notice_id=notice.id, version_no=1, file_sha256='b' * 64,
                source_payload={'kind': 'PPS_NOTICE_METADATA', 'notice_metadata': {
                    'demand_agency_name': 'SYN 수요기관', 'demand_agency_code': 'SYN-DEMAND',
                    'announcing_agency_name': 'SYN 공고대행기관',
                }}))
        session.commit()
        return key, notice.id


def fact(identity, *, agency='SYN 수요기관', title='SYN 통합교육 운영 용역', code=None):
    return dict(external_identity=identity, bid_notice_no=identity, title=title, agency=agency,
        demand_agency_code=code, winner_name='SYN 낙찰업체', similarity_score=90,
        awarded_at=datetime(2025, 6, 1, tzinfo=timezone.utc), award_amount=12300000)


def test_all_read_surfaces_filter_agency_and_keyword_without_deleting_audit(client):
    key, notice_id = target(client)
    with client.app.state.session_factory() as session:
        for fields in [fact('SYN-MATCH'), fact('SYN-OTHER', agency='SYN 다른기관'),
                       fact('SYN-WRONG-TITLE', title='SYN 시설관리 용역'),
                       fact('SYN-CODE-CONFLICT', code='SYN-OTHER-CODE')]:
            session.add(AwardHistoryItem(target_notice_id=notice_id, **fields))
        session.commit()
    base = '/api/v1/notices/' + key
    history = client.get(base + '/award-history').json()
    intelligence = client.get(base + '/award-intelligence').json()
    detail = client.get(base).json()
    assert [row['bid_notice_no'] for row in history] == ['SYN-MATCH']
    assert len(detail['award_history']) == intelligence['record_count'] == 1
    assert intelligence['annual_award_table']['row_count'] == 1
    assert intelligence['search_criteria'] == {
        'version': 'demand-agency-keyword-v1', 'years': 3, 'status': 'AVAILABLE',
        'demand_agency_name': 'SYN 수요기관', 'keyword': 'syn 통합교육',
    }
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AwardHistoryItem)
                              .where(AwardHistoryItem.target_notice_id == notice_id)) == 4


def test_missing_demand_agency_stops_before_provider_and_job(client, monkeypatch):
    key, _ = target(client, verified=False)
    client.app.state.settings = replace(client.app.state.settings, pps_api_key='SYN-no-network')
    def forbidden(**kwargs):
        raise AssertionError('Unknown demand agency must not call PPS')
    monkeypatch.setattr('pai_loop.api.PpsAwardClient', forbidden)
    response = client.post('/api/v1/notices/' + key + '/award-history/refresh', json={})
    assert response.status_code == 422
    assert 'AWARD_AGENCY_UNAVAILABLE' in response.json()['detail']
    intelligence = client.get('/api/v1/notices/' + key + '/award-intelligence').json()
    assert intelligence['search_criteria']['status'] == 'UNAVAILABLE'
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IngestionJob)) == 0


def test_refresh_rechecks_scope_before_opening_and_persists_code(client, monkeypatch):
    key, notice_id = target(client)
    client.app.state.settings = replace(client.app.state.settings, pps_api_key='SYN-no-network')
    captured, openings = [], []
    class Provider:
        def __init__(self, **kwargs):
            self.request_count = 0
            self.hit_page_limit = False
            self.fallback_window_count = 0
            self.window_errors = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_awards(self, **kwargs):
            captured.append(kwargs)
            self.request_count = 1
            good = fact('SYN-GOOD', code='SYN-DEMAND')
            bad = fact('SYN-BAD', agency='SYN 다른기관', code='SYN-OTHER')
            for row in [good, bad]:
                yield {**row, 'identity': row['external_identity']}
        def fetch_opening_results(self, **kwargs):
            openings.append(kwargs['bid_notice_no'])
            self.request_count += 1
            return []
    monkeypatch.setattr('pai_loop.api.PpsAwardClient', Provider)
    response = client.post('/api/v1/notices/' + key + '/award-history/refresh',
        json={'years': 3, 'include_opening_results': True})
    assert response.status_code == 200, response.text
    assert response.json()['records'] == 1
    assert openings == ['SYN-GOOD']
    assert captured[0]['demand_agency_code'] == 'SYN-DEMAND'
    assert captured[0]['demand_agency_name'] == 'SYN 수요기관'
    assert captured[0]['start'].year == captured[0]['end'].year - 2
    with client.app.state.session_factory() as session:
        rows = list(session.scalars(select(AwardHistoryItem).where(AwardHistoryItem.target_notice_id == notice_id)))
        assert len(rows) == 1 and rows[0].demand_agency_code == 'SYN-DEMAND'


def test_batch_stops_before_next_notice_after_provider_limit(client, monkeypatch):
    client.app.state.settings = replace(client.app.state.settings, pps_api_key='SYN-no-network')
    calls = []
    def one(*args):
        calls.append(1)
        return {'attempted': 1, 'api_calls': 1, 'records': 0, 'status': 'PARTIAL',
                'notice_key': 'SYN-NOTICE', 'job_id': None, 'provider_rate_limited': True}
    monkeypatch.setattr('pai_loop.award_automation._run_one', one)
    result = client.post('/api/v1/operations/award-refresh/run', json={'max_notices': 3})
    assert result.status_code == 200
    assert result.json()['attempted'] == len(calls) == 1
