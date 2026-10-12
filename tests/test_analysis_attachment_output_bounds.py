"""Per-attachment output bounds of the analysis batch response (2026-10-12)."""
import pytest

from pai_loop.analysis_api import AnalysisAttachmentEnrichmentOut
from pai_loop.pps_enrichment import MAX_OPENAI_CALLS_PER_NOTICE


def test_attachment_output_accepts_recovery_calls_beyond_two() -> None:
    # 2026-10-10: first extraction + long-output retry + quantitative recovery
    # produced openai_calls=3 for one attachment and the batch answered 500
    # after the calls were already paid for.
    item = {
        "attachment_id": "att-1",
        "media_type": "application/pdf",
        "status": "COMPLETED",
        "reason_code": "ACCEPTED",
        "openai_calls": 3,
    }
    assert AnalysisAttachmentEnrichmentOut.model_validate(item).openai_calls == 3
    with pytest.raises(ValueError):
        AnalysisAttachmentEnrichmentOut.model_validate(
            {**item, "openai_calls": MAX_OPENAI_CALLS_PER_NOTICE + 1}
        )
