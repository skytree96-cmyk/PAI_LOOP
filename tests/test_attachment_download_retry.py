"""SYN: transient transport failures retry a free public GET a bounded number of times."""
import httpx
import pytest

from pai_loop.pps_enrichment import (
    ATTACHMENT_DOWNLOAD_ATTEMPTS, PpsEnrichmentError, download_public_attachment,
)
from test_release_coverage_gate import _attachment


def _flaky(failures, *, error=httpx.ConnectError, status=200):
    calls = []

    def handler(request):
        calls.append(request.url)
        if len(calls) <= failures:
            raise error("synthetic transport failure", request=request)
        return httpx.Response(status, content=b"%PDF-1.4 SYN", headers={"Content-Type": "application/pdf"})

    return httpx.MockTransport(handler), calls


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_transport_failure_is_retried_until_success(error):
    transport, calls = _flaky(2, error=error)
    waits = []
    assert download_public_attachment(_attachment(), transport=transport, sleep=waits.append) == b"%PDF-1.4 SYN"
    assert len(calls) == 3 == ATTACHMENT_DOWNLOAD_ATTEMPTS
    assert waits == [0.5, 1.5]


def test_retries_are_bounded_and_keep_the_network_code():
    transport, calls = _flaky(99)
    waits = []
    with pytest.raises(PpsEnrichmentError, match="ATTACHMENT_NETWORK_ERROR"):
        download_public_attachment(_attachment(), transport=transport, sleep=waits.append)
    assert len(calls) == ATTACHMENT_DOWNLOAD_ATTEMPTS and len(waits) == ATTACHMENT_DOWNLOAD_ATTEMPTS - 1


@pytest.mark.parametrize("status,code", [(404, "ATTACHMENT_HTTP_404"), (503, "ATTACHMENT_HTTP_503")])
def test_http_status_failures_are_not_retried(status, code):
    transport, calls = _flaky(0, status=status)
    with pytest.raises(PpsEnrichmentError, match=code):
        download_public_attachment(_attachment(), transport=transport, sleep=lambda _s: None)
    assert len(calls) == 1


def test_single_attempt_option_disables_retry():
    transport, calls = _flaky(1)
    with pytest.raises(PpsEnrichmentError, match="ATTACHMENT_NETWORK_ERROR"):
        download_public_attachment(_attachment(), transport=transport, attempts=1, sleep=lambda _s: None)
    assert len(calls) == 1
