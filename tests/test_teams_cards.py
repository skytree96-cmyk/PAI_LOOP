from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pai_loop import teams_cards
from pai_loop.integrations.openai_extraction import PROMPT_VERSION, SCHEMA_VERSION
from pai_loop.models import Notice, NoticeVersion
from pai_loop.pps_enrichment import PPS_METADATA_KIND, PPS_METADATA_SCHEMA, PPS_PROCESSING_VERSION, _digest

# `_summary` decides a notice is still current by comparing its deadline with
# the real clock rather than the `now` handed to the card builder, so a fixed
# calendar date quietly turns these cases red once it passes. Anchor the
# fixture to today and derive every expected date from it.
KST = timezone(timedelta(hours=9), name="Asia/Seoul")
NOW = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _kst_minute(moment: datetime) -> str:
    return moment.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def _kst_day(moment: datetime) -> str:
    return moment.astimezone(KST).strftime("%Y-%m-%d")


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
        assert _kst_minute(NOW + timedelta(days=10)) in text
        assert card["actions"][0]["url"] == "https://example.test/?notice=SYN-TEAMS-CARD"
        notice.title = "SYN 변경 공고"
        notice.deadline += timedelta(days=2)
        session.commit()
        latest = json.dumps(teams_cards.build_notice_card(session, notice, "D_MINUS_5", now=NOW, base_url="https://example.test"), ensure_ascii=False)
        assert "SYN 변경 공고" in latest and _kst_day(NOW + timedelta(days=12)) in latest
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
        blocks = [block.get("text", "") for block in _details(card)["items"]]
        eligibility_start = blocks.index("자격사항 · 확인할 조건")
        action_start = blocks.index("참여 전 처리할 사항")
        checklist_start = blocks.index("제출·계약 확인사항")
        quantitative_start = blocks.index("정량점수")
        assert any(eligibility in text for text in blocks[eligibility_start:action_start])
        assert not any(action in text or checklist in text for text in blocks[eligibility_start:action_start])
        assert any(action in text for text in blocks[action_start:checklist_start])
        assert any(checklist in text for text in blocks[checklist_start:quantitative_start])
        # Display classification does not manufacture an overall company verdict.
        facts = {fact["title"]: fact["value"] for fact in _first(card, "FactSet")["facts"]}
        assert facts["참가자격"] == "미판정"


def _walk(element):
    if isinstance(element, dict):
        yield element
        for key in ("body", "items", "columns", "actions"):
            for child in element.get(key, []):
                yield from _walk(child)


def _first(card, kind):
    return next(element for element in _walk(card) if element.get("type") == kind)


def _details(card):
    return next(element for element in card["body"] if element.get("id") == teams_cards.DETAILS_ID)


def test_card_shows_preview_layout_and_keeps_full_analysis_collapsed(client):
    with client.app.state.session_factory() as session:
        notice = _notice(session)
        card = teams_cards.build_notice_card(session, notice, "REGISTERED", now=NOW, base_url="https://example.test")
    elements = list(_walk(card))
    # Status badges: qualification then deadline, each a Teams-styled container.
    badges = card["body"][4]["columns"]
    assert [column["items"][0]["items"][0]["text"] for column in badges] == [
        "자격 미판정", f"D-10 · {(NOW + timedelta(days=10)).astimezone(KST):%m.%d}"
        + " (" + "월화수목금토일"[(NOW + timedelta(days=10)).astimezone(KST).weekday()] + ")"]
    assert badges[0]["items"][0]["style"] == "emphasis"
    # Three metric tiles never invent a score for an unevaluated notice.
    metrics = card["body"][6]["columns"]
    assert [[block["text"] for block in column["items"][0]["items"]] for column in metrics] == [
        ["준비도", "미산정"], ["리스크", "미산정"], ["입찰 판단", "확인 필요"]]
    details = _details(card)
    assert details["isVisible"] is False
    toggle = next(element for element in elements if element.get("type") == "Action.ToggleVisibility")
    assert toggle["targetElements"] == [teams_cards.DETAILS_ID]
    assert [action["url"] for action in card["actions"]] == [
        "https://example.test/?notice=SYN-TEAMS-CARD", "https://example.test/?notice=SYN-TEAMS-CARD&decision=1"]
    assert "SYN 카드 공고" in card["fallbackText"]
    assert card["msteams"] == {"width": "Full"}


def test_cancelled_card_offers_no_decision_button(client):
    with client.app.state.session_factory() as session:
        notice = _notice(session)
        notice.status = "CANCELLED"
        card = teams_cards.build_notice_card(session, notice, "REGISTERED", now=NOW, base_url="https://example.test")
    assert [action["title"] for action in card["actions"]] == ["근거 상세보기"]


@pytest.mark.parametrize("days,text,style", [
    (0, "오늘 마감", "attention"), (3, "D-3", "warning"), (12, "D-12", "accent"), (-1, "마감 ·", "emphasis")])
def test_deadline_badge_escalates_as_the_deadline_nears(days, text, style):
    now = datetime(2026, 10, 1, 1, tzinfo=timezone.utc)
    badge = teams_cards._deadline_badge((now + timedelta(days=days)).astimezone(KST), now)
    assert badge["items"][0]["style"] == style
    assert badge["items"][0]["items"][0]["text"].startswith(text)


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


def test_the_card_reads_the_clock_it_was_given_not_the_wall_clock(client):
    """같은 공고를 같은 시각으로 렌더하면 언제 렌더하든 같은 카드가 나와야 한다.

    마감 경과 판정이 벽시계를 보면, 고정 시각으로 만든 카드가 렌더 시점에 따라
    '현재 검토 제외' 로 뒤집히고 점수·자격 블록이 통째로 빠진다. 같은 공고가 보는
    때마다 다르게 보이는 셈이다.
    """

    with client.app.state.session_factory() as session:
        notice = _notice(session)
        # 마감 전 시각으로 렌더한다. 실제 오늘이 마감 뒤여도 결과는 같아야 한다.
        before = json.dumps(
            teams_cards.build_notice_card(session, notice, "REGISTERED", now=NOW, base_url="https://example.test"),
            ensure_ascii=False,
        )
        assert "현재 검토 제외" not in before
        assert "미판정" in before

        # 마감 뒤 시각을 주면 그때는 검토 대상에서 빠진다.
        after = json.dumps(
            teams_cards.build_notice_card(
                session, notice, "REGISTERED", now=notice.deadline.replace(tzinfo=timezone.utc) + timedelta(days=1),
                base_url="https://example.test",
            ),
            ensure_ascii=False,
        )
        assert "현재 검토 제외" in after


def test_card_shows_pass_when_the_recipient_department_decided_to_participate(client, monkeypatch):
    from pai_loop.models import UserDecision

    real_detail = teams_cards._detail
    monkeypatch.setattr(
        teams_cards, "_detail",
        lambda *args, **kwargs: real_detail(*args, **kwargs).model_copy(update={"qualification_status": "FAIL"}),
    )
    with client.app.state.session_factory() as session:
        notice = _notice(session)
        session.add(UserDecision(notice_id=notice.id, choice="GO", rationale="SYN 참여",
                                 department_id="future-ai-education", department_name="SYN 부서",
                                 department_revision=1))
        session.commit()

        def facts(department_id):
            card = teams_cards.build_notice_card(session, notice, "REGISTERED", now=NOW,
                                                 base_url="https://example.test", department_id=department_id)
            return json.dumps(card, ensure_ascii=False)

        assert "자격 충족" in facts("future-ai-education")
        assert "자격 미충족" in facts("future-ai-capability")
        assert "자격 미충족" in facts(None)


def test_card_from_a_freshly_loaded_pps_notice_reads_every_version(client):
    """The dispatcher loads a notice and renders at once (2026-10-01 incident).

    The PPS authority projection re-selects the same notice with only its
    metadata version. Rendered from a fresh session, that partial collection
    used to become `notice.versions`, so a fully analysed notice went out as
    "0/4 · 분석 대상 아님 · 미판정 · 미산정" while the web showed its result.
    """

    condition = "지방계약법령상 입찰참가 자격요건을 갖춘 업체여야 함."
    factory = client.app.state.session_factory
    with factory() as session:
        notice = Notice(notice_key="PPS-TESTCARD-000", bid_notice_no="TESTCARD", title="PPS 카드 공고",
                        agency="PPS 기관", deadline=NOW + timedelta(days=10), status="OPEN")
        session.add(notice)
        session.commit()
        _append_card_attachment(session, notice, version_no=1, identity="a", conditions=[("ENTITY", condition)])
        notice_id = notice.id
        total_versions = len(notice.versions)
        preloaded = teams_cards.build_notice_card(session, notice, "REGISTERED", now=NOW,
                                                  base_url="https://example.test")
    with factory() as session:
        # Exactly the dispatcher: fetch by id, render immediately.
        fresh_notice = session.get(Notice, notice_id)
        fresh = teams_cards.build_notice_card(session, fresh_notice, "REGISTERED", now=NOW,
                                              base_url="https://example.test")
        assert len(fresh_notice.versions) == total_versions == 2
    assert fresh == preloaded
    reason = fresh["body"][5]["items"][0]["text"]
    assert "일일 분석 대상으로 선택되지 않은" not in reason
