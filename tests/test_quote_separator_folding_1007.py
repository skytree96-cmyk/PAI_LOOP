"""Anchors that differ from the source only by word separators lost in HWP/PDF text."""

from pai_loop.integrations.openai_extraction import _anchor_quote_in_source as matches
from pai_loop.integrations.openai_extraction import evidence_quote_matches_source as strict

SOURCE = (
    "본 과업 수행에 있어서 발생하는 민 형사 행정상의 제반 법적 분쟁에 대한  책임은 과업수행자가 부담한다. "
    "제안서의 기재사항이나 과업이 허위 모방 표절로 밝혀질 경우 계약을 무효화 할 수 있다. "
    "추정가격 2.5억원, 계약금액 1,000만원 이상 실적. 국가를 당사자로 하는 계약에 관한 법률 제27조"
)


def test_middle_dots_lost_by_extraction_still_anchor() -> None:
    assert matches("발생하는 민·형사·행정상의 제반 법적 분쟁에 대한 책임은", SOURCE)
    assert matches("과업이 허위·모방·표절로 밝혀질 경우", SOURCE)
    assert matches("국가를 당사자로 하는 계약에 관한 법률(제27조)", SOURCE)


def test_numbers_keep_their_separators() -> None:
    assert not matches("추정가격 25억원, 계약금액 1,000만원", SOURCE)
    assert not matches("추정가격 2.5억원, 계약금액 1000만원 이상", SOURCE)
    assert matches("추정가격 2.5억원, 계약금액 1,000만원 이상", SOURCE)


def test_letters_must_still_match_exactly_and_short_anchors_stay_strict() -> None:
    assert not matches("발생하는 민·형사·행정상의 모든 법적 분쟁에 대한 책임은", SOURCE)  # a word changed
    assert not matches("민·형사", SOURCE)  # too short to fold


def test_deterministic_validators_keep_the_strict_predicate() -> None:
    assert not strict("과업이 허위·모방·표절로 밝혀질 경우", SOURCE)
