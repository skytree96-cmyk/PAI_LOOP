from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pai_loop import teams_cards
from pai_loop.integrations.openai_extraction import PROMPT_VERSION, SCHEMA_VERSION
from pai_loop.models import Notice, NoticeVersion
from pai_loop.pps_enrichment import PPS_METADATA_KIND, PPS_METADATA_SCHEMA, PPS_PROCESSING_VERSION, _digest

NOW = datetime(2026, 9, 13, 0, tzinfo=timezone.utc)


def _notice(session):
    notice = Notice(notice_key="SYN-TEAMS-CARD", bid_notice_no="SYN-CARD", title="SYN 카드 공고",
                    agency="SYN 기관", deadline=NOW + timedelta(days=10), status="OPEN")
    session.add(notice)
    session.commit()
    return notice


def test_card_uses_latest_notice_and_never_invents_missing_scores(client):
    with client.app.state.session_factory() as session:
        notice = _notice(session)
        card = teams_cards.build_notice_card(session, notice, "REGISTERED", now=NOW, base_url="https://example.test")
        text = json.dumps(card, ensure_ascii=False)
        assert "관심 공고 등록" in text
        assert "미산정" in text and "미판정" in text
        assert "확정 0" not in text and "0 / 100" not in text
        assert "2026-09-23 09:00" in text
        assert card["actions"][0]["url"] == "https://example.test/?notice=SYN-TEAMS-CARD"
        notice.title = "SYN 변경 공고"
        notice.deadline += timedelta(days=2)
        session.commit()
        latest = json.dumps(teams_cards.build_notice_card(session, notice, "D_MINUS_5", now=NOW, base_url="https://example.test"), ensure_ascii=False)
        assert "SYN 변경 공고" in latest and "2026-09-25" in latest
        assert "SYN 카드 공고" not in latest


def test_card_does_not_copy_raw_provider_or_evidence_values(client):
    with client.app.state.session_factory() as session:
        notice = _notice(session)
        session.add(NoticeVersion(notice_id=notice.id, version_no=1, file_sha256="a" * 64,
                                 source_payload={"provider_token": "SYN-PRIVATE-SECRET", "result": {"summary": "SYN-PRIVATE-SOURCE"}}))
        session.commit()
        session.expire(notice)
        text = json.dumps(teams_cards.build_notice_card(session, notice, "REGISTERED", now=NOW, base_url="https://example.test"))
        assert "SYN-PRIVATE" not in text
        assert "a" * 64 not in text


@pytest.mark.parametrize("status,confirmed,lower,upper,expected", [
    ("CONFIRMED", 12, 12, 12, "확정 12 / 20점"),
    ("ESTIMATED", None, 8, 13, "추정 8~13 / 20점 · 확정 아님"),
    ("UNSCORABLE", None, 0, 20, "산정 불가"),
    ("REVIEW_REQUIRED", None, 0, 20, "확인 필요"),
])
def test_score_status_is_kept_separate_from_risk_and_eligibility(client, monkeypatch, status, confirmed, lower, upper, expected):
    projection = SimpleNamespace(overall_status=status, confirmed_points=confirmed, lower_points=lower,
                                 upper_points=upper, total_max_points=20, criteria=[], opinion="SYN 공개 점수 요약")
    monkeypatch.setattr(teams_cards, "_stored_public_quantitative_projection", lambda *_: projection)
    with client.app.state.session_factory() as session:
        notice = _notice(session)
        text = json.dumps(teams_cards.build_notice_card(session, notice, "DEADLINE_DAY", now=NOW, base_url="https://example.test"), ensure_ascii=False)
        assert expected in text
        assert "미판정" in text and "경쟁·집중 리스크" in text
        if status in {"UNSCORABLE", "REVIEW_REQUIRED"}:
            assert "추정 0~20" not in text


def test_cancelled_card_does_not_publish_current_score(client, monkeypatch):
    def forbidden(*_):
        raise AssertionError("cancelled notice must not load a current score")
    monkeypatch.setattr(teams_cards, "_stored_public_quantitative_projection", forbidden)
    with client.app.state.session_factory() as session:
        notice = _notice(session)
        notice.status = "CANCELLED"
        text = json.dumps(teams_cards.build_notice_card(session, notice, "REGISTERED", now=NOW, base_url="https://example.test"), ensure_ascii=False)
        assert "현재 검토 제외" in text


@pytest.mark.parametrize("url", ["http://example.test", "https://" + "@".join(("SYN:password", "example.test")), "https://example.test?token=SYN", "https://example.test/path", "https://example.test/#x", "https://example.test:444", "javascript:alert(1)"])
def test_card_detail_link_rejects_non_origin_or_secret_bearing_base(url):
    with pytest.raises(ValueError):
        teams_cards._detail_url(url, "SYN")


def test_source_card_text_cannot_inject_markdown_links_or_mentions():
    result = teams_cards._plain("[SYN](https://example.test) <at>SYN</at>")
    assert "\\[SYN\\]\\(" in result and "\\<at\\>" in result
    assert len(teams_cards._plain("x" * 1000, 100)) == 101


def _append_card_attachment(session, notice, *, version_no, identity, conditions):
    attachment = {"attachment_id": "PPS-ATT-" + identity * 24,
                  "file_name": f"SYN-{identity}-공고문.pdf", "media_type": "application/pdf",
                  "url": "https://www.g2b.go.kr/SYN-attachment", "slot": 1}
    session.add(NoticeVersion(notice_id=notice.id, version_no=version_no,
                             file_sha256=identity * 64, extraction_status="METADATA",
                             source_payload={"kind": PPS_METADATA_KIND, "schema_version": PPS_METADATA_SCHEMA,
                                             "attachment_manifest": [attachment]}))
    payload = {
        "kind": "OPENAI_REQUIREMENT_EXTRACTION", "source_kind": "PPS_PUBLIC_ATTACHMENT",
        "attachment_id": attachment["attachment_id"], "source_label": attachment["file_name"],
        "document_sha256": identity * 64, "status": "ACCEPTED", "prompt_version": PROMPT_VERSION,
        "processing_version": PPS_PROCESSING_VERSION, "schema_version": SCHEMA_VERSION,
        "manifest_sha256": _digest(attachment), "current_manifest_sha256": _digest([attachment]),
        "document_processing": {"source_read_complete": True, "analysis_input_complete": True},
        "result": {"document_type": "NOTICE", "summary": "SYN 현재 문서 요약",
                   "quantitative_tables": [], "quantitative_table_not_applicable": None,
                   "missing_or_unreadable": [], "requirements": [
                       {"requirement_id": f"SYN-{identity}-{index}", "category": category,
                        "normalized_condition": text, "logic": "SINGLE", "mandatory": True,
                        "deadline_basis": "입찰 마감일", "ambiguity_reason": None,
                        "evidence": [{"attachment_id": attachment["attachment_id"], "page": 1,
                                      "section": "SYN 조건", "quote": text, "confidence": 0.98}]}
                       for index, (category, text) in enumerate(conditions)]},
    }
    version = NoticeVersion(notice_id=notice.id, version_no=version_no + 1,
                            file_sha256=identity * 64, document_complete=True,
                            extraction_status="ACCEPTED", extraction_confidence=1, source_payload=payload)
    session.add(version)
    session.commit()
    session.expire(notice)
    return version


def test_card_selects_current_manifest_before_public_requirements(client):
    old = "부정당업자 입찰참가자격 제한을 받고 있지 않아야 함."
    current = "지방계약법령상 입찰참가 자격요건을 갖춘 업체여야 함."
    with client.app.state.session_factory() as session:
        notice = _notice(session)
        _append_card_attachment(session, notice, version_no=1, identity="a", conditions=[("SANCTION", old)])
        _append_card_attachment(session, notice, version_no=3, identity="b", conditions=[("ENTITY", current)])
        # Both historical extractions are publication-safe. Only the current
        # manifest attachment may become a condition in the personal alert.
        detail = teams_cards._detail(notice, public_view=True)
        historical_conditions = [row["normalized_condition"] for analysis in detail.document_analyses
                                 for row in analysis["requirements"]]
        assert old in historical_conditions and current in historical_conditions
        card = teams_cards.build_notice_card(session, notice, "REGISTERED", now=NOW, base_url="https://example.test")
        text = json.dumps(card, ensure_ascii=False)
        assert current in text
        assert old not in text


def test_card_separates_eligibility_from_participation_and_submission(client):
    eligibility = "지방계약법령상 입찰참가 자격요건을 갖춘 업체여야 함."
    action = "제안설명회에 참여해야 하며 불참 시 사업신청 포기로 간주됨."
    checklist = "제안서는 직접 방문하여 제출해야 함."
    with client.app.state.session_factory() as session:
        notice = _notice(session)
        _append_card_attachment(session, notice, version_no=1, identity="a", conditions=[
            ("ENTITY", eligibility), ("SUBMISSION", action), ("SUBMISSION", checklist)])
        card = teams_cards.build_notice_card(session, notice, "REGISTERED", now=NOW, base_url="https://example.test")
        blocks = [block.get("text", "") for block in card["body"]]
        eligibility_start = blocks.index("자격사항 · 확인할 조건")
        action_start = blocks.index("참여 전 처리할 사항")
        checklist_start = blocks.index("제출·계약 확인사항")
        quantitative_start = blocks.index("정량점수")
        assert any(eligibility in text for text in blocks[eligibility_start:action_start])
        assert not any(action in text or checklist in text for text in blocks[eligibility_start:action_start])
        assert any(action in text for text in blocks[action_start:checklist_start])
        assert any(checklist in text for text in blocks[checklist_start:quantitative_start])
        # Display classification does not manufacture an overall company verdict.
        assert card["body"][2]["facts"][2]["value"] == "미판정"


def test_new_review_attempt_cannot_fall_back_to_accepted_requirement(client):
    condition = "지방계약법령상 입찰참가 자격요건을 갖춘 업체여야 함."
    with client.app.state.session_factory() as session:
        notice = _notice(session)
        accepted = _append_card_attachment(session, notice, version_no=1, identity="a", conditions=[("ENTITY", condition)])
        session.add(NoticeVersion(notice_id=notice.id, version_no=3, file_sha256="c" * 64,
                                 extraction_status="REVIEW", source_payload={**accepted.source_payload, "status": "REVIEW"}))
        session.commit()
        session.expire(notice)
        text = json.dumps(teams_cards.build_notice_card(session, notice, "REGISTERED", now=NOW, base_url="https://example.test"), ensure_ascii=False)
        assert condition not in text
        assert "검증된 자격조건 요약이 아직 없습니다" in text
