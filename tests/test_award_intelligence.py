from __future__ import annotations

import json
import copy
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop.main import create_app
from pai_loop.award_intelligence import build_award_intelligence
from pai_loop.models import AwardHistoryItem, Notice
from pai_loop.pricing_profiles import pricing_profile_for_document
from pai_loop.public_award_seed import (
    PublicAwardSeedError,
    import_public_award_seed,
    load_public_award_seed,
    validate_public_award_seed,
)
from pai_loop.public_notice_seed import import_public_notice_seed


FIXTURE = Path(__file__).parent / "fixtures" / "award_intelligence_synthetic_v1.json"


def _records() -> list[dict]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["classification"] == "SYNTHETIC_TEST_ONLY"
    for item in payload["records"]:
        item["awarded_at"] = datetime.fromisoformat(item["awarded_at"])
    return payload["records"]


def test_award_intelligence_keeps_facts_and_estimates_separate() -> None:
    result = build_award_intelligence(
        _records(),
        as_of=datetime.fromisoformat("2026-01-01T00:00:00+09:00"),
        target_estimated_price=120_000_000,
    )

    assert result["boundary"] == "STORED_HISTORY_ONLY"
    assert result["record_count"] == 6
    assert result["concentration"]["top_winner"] == {
        "winner_name": "합성 수행사 A", "count": 3, "share": 0.5
    }
    assert result["concentration"]["hhi"] == 3888.89
    assert result["award_rate_distribution"]["median"] == 90.5
    assert result["prediction"]["award_rate"]["status"] == "MODEL_ESTIMATE"
    assert result["prediction"]["award_rate"]["confidence"] == "MEDIUM"
    assert result["prediction"]["award_amount_range"]["basis_kind"] == "NOTICE_ESTIMATED_AMOUNT"
    # Only two explicit submitted prices exist: never substitute award amount.
    assert result["prediction"]["submitted_bid_rate"]["status"] == "INSUFFICIENT_DATA"
    assert result["field_coverage"]["submitted_bid_price"]["available"] == 2
    assert result["field_coverage"]["technical_score"]["available"] == 1


def test_rate_derivation_requires_the_correct_explicit_pair() -> None:
    result = build_award_intelligence([
        {"award_amount": 88, "estimated_price": 100},
        {"submitted_bid_price": 77, "estimated_price": 100},
        {"award_amount": 50, "submitted_bid_price": 49},
    ], as_of=datetime.fromisoformat("2026-01-01T00:00:00+09:00"))

    assert result["records"][0]["award_rate"] == 88
    assert result["records"][0]["award_rate_basis"] == "DERIVED_AWARD_AMOUNT_OVER_ESTIMATED_PRICE"
    assert result["records"][1]["award_rate"] is None
    assert result["records"][1]["submitted_bid_rate"] == 77
    assert result["records"][2]["award_rate"] is None


def test_award_intelligence_api_reads_stored_rows_without_live_client(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("live PPS client must not be constructed")

    monkeypatch.setattr("pai_loop.api.PpsAwardClient", forbidden)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as client:
        created = client.post("/api/v1/notices", json={
            "notice_key": "SYNTHETIC-INTEL",
            "bid_notice_no": "SYN-TARGET",
            "title": "합성 리더십 교육",
            "agency": "합성 발주기관",
            "published_at": "2026-01-01T00:00:00+09:00",
            "deadline": "2026-02-01T00:00:00+09:00",
            "estimated_amount": 120000000,
        })
        assert created.status_code == 201
        with app.state.session_factory() as session:
            notice = session.scalar(select(Notice).where(Notice.notice_key == "SYNTHETIC-INTEL"))
            for index, item in enumerate(_records(), start=1):
                session.add(AwardHistoryItem(
                    target_notice_id=notice.id,
                    external_identity=f"SYNTHETIC-{index}",
                    bid_notice_no=item["bid_notice_no"],
                    title=item["title"],
                    agency=item["agency"],
                    winner_name=item["winner_name"],
                    award_amount=item["award_amount"],
                    award_rate=item["award_rate"],
                    awarded_at=item["awarded_at"],
                    similarity_score=80.0,
                    source="SYNTHETIC_TEST_ONLY",
                ))
            session.commit()

        response = client.get("/api/v1/notices/SYNTHETIC-INTEL/award-intelligence")
        assert response.status_code == 200
        body = response.json()
        assert body["period"] == {"from": "2023-01-01", "to": "2026-01-01", "years": 3}
        assert body["record_count"] == 6
        assert body["prediction"]["submitted_bid_rate"]["status"] == "INSUFFICIENT_DATA"
        assert body["target_amount_basis"]["kind"] == "NOTICE_ESTIMATED_AMOUNT"


def test_grounded_pricing_profile_requires_exact_document_digest() -> None:
    digest = "53b24e9dae63328d4f692e4cbe21e7148e0f24614dedbec5c356e2adbfc84648"
    profile = pricing_profile_for_document(digest)
    assert profile["applicability"] == "EXACT_DOCUMENT_SHA256"
    assert profile["source_anchor"] == {
        "file_label": "2025 AI 활용 역량 강화 교육 운영 용역 제안요청서",
        "pdf_page": 16,
        "printed_page": 30,
        "section": "입찰가격 평점산식",
    }
    assert profile["score_prediction"] is None
    assert pricing_profile_for_document("0" * 64) is None


def test_packaged_public_award_seed_is_safe_tamper_evident_and_idempotent() -> None:
    seed = load_public_award_seed()
    assert seed["classification"] == "PUBLIC_PROCUREMENT_DERIVED"
    assert seed["provenance"]["record_count"] == len(seed["records"]) == 59
    serialized = json.dumps(seed, ensure_ascii=False)
    assert "@" not in serialized
    assert "MUST-NOT" not in serialized

    tampered = copy.deepcopy(seed)
    tampered["records"][0]["award_amount"] = 1
    try:
        validate_public_award_seed(tampered)
    except PublicAwardSeedError:
        pass
    else:
        raise AssertionError("digest tampering must fail closed")

    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app):
        with app.state.session_factory() as session:
            import_public_notice_seed(session)
            first = import_public_award_seed(session)
            second = import_public_award_seed(session)
            assert first.created == 59
            assert second.created == 0
            assert second.existing == 59
