from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
import pytest

from pai_loop.integrations.awards import PpsAwardClient


@pytest.mark.parametrize('start,end,expected_calls', [
    (date(2026, 2, 20), date(2026, 3, 21), 2),
    (date(2024, 2, 20), date(2024, 3, 21), 2),
    (date(2024, 1, 1), date(2026, 9, 13), 36),
])
def test_award_default_windows_cover_every_day_without_calendar_month_errors(start, end, expected_calls):
    windows = []
    def handler(request):
        query = request.url.params
        beginning = datetime.strptime(query['inqryBgnDt'], '%Y%m%d%H%M').date()
        ending = datetime.strptime(query['inqryEndDt'], '%Y%m%d%H%M').date()
        windows.append((beginning, ending))
        if (ending - beginning).days >= 28:
            return httpx.Response(200, json={'response':{'header':{'resultCode':'07'}}})
        # Earlier explicit-zero windows must not hide a result in the last one.
        items = [{'bidNtceNo':'SYN-FINAL-WINDOW', 'bidNtceNm':'SYN 교육',
                  'bidwinnrNm':'SYN winner'}] if ending == end else []
        return httpx.Response(200, json={'response':{'header':{'resultCode':'00'},
            'body':{'totalCount':len(items), 'items':items}}})

    with PpsAwardClient(service_key='SYN-unused', max_retries=0,
                       transport=httpx.MockTransport(handler)) as provider:
        rows = list(provider.iter_awards(start=start, end=end, keyword='SYN',
                    max_pages_per_window=3, continue_on_window_error=True))
        assert [row['bid_notice_no'] for row in rows] == ['SYN-FINAL-WINDOW']
        assert len(windows) == provider.request_count == expected_calls
        assert windows[0][0] == start and windows[-1][1] == end
        assert all(left[1] + timedelta(days=1) == right[0] for left,right in zip(windows,windows[1:]))
        assert all(1 <= (finish-begin).days+1 <= 28 for begin,finish in windows)
        assert provider.fallback_window_count == 0
        assert provider.window_errors == [] and provider.window_error_counts == []
        assert not provider.hit_incomplete_response and not provider.hit_page_limit
