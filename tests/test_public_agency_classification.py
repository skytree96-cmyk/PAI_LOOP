"""발주처가 공공 조달 주체인지 판정하는 근거.

실적 인정조건이 공공부문 발주를 요구할 때, 판정하지 못한 발주처는 계산을 멈춘다.
멈춤 자체는 옳지만, 이름만 봐도 설립 근거가 드러나는 기관까지 멈추면 검증된 실적이
통째로 버려진다. 관측 증거(나라장터 발주 이력)를 먼저 보고, 그 다음 기관 형태를 본다.
확인되지 않은 곳은 여전히 모름으로 남긴다.
"""

from __future__ import annotations

import json

import pytest

from pai_loop.quantitative_performance import (
    _public_agency_registry,
    _public_sector_agency_status,
)


class _Record:
    def __init__(self, agency: str) -> None:
        self.agency = agency


def status(agency: str) -> bool | None:
    return _public_sector_agency_status(_Record(agency))


def test_registry_carries_a_basis_for_every_entry():
    from importlib.resources import files

    payload = json.loads(
        files("pai_loop").joinpath("data/public_agency_registry.json").read_text(encoding="utf-8")
    )
    assert payload["version"] == "public-agency-registry-1"
    assert payload["observed_agencies"], "관측 발주기관이 비어 있으면 근거가 없다"
    for entry in payload["observed_agencies"]:
        assert entry["name"].strip()
        assert entry["basis"] in {"PPS_NOTICE_AGENCY", "PPS_AWARD_AGENCY"}
    for entry in payload.get("operator_reviewed") or []:
        assert entry["name"].strip()
        assert entry["basis"] == "OPERATOR_REVIEWED"
        assert isinstance(entry["public"], bool)
        assert entry.get("reason", "").strip(), "운영자 판단은 이유를 남긴다"
    public_names, private_names = _public_agency_registry()
    assert public_names
    assert not (public_names & private_names)


@pytest.mark.parametrize("agency", ["케이티", "POSCO홀딩스"])
def test_an_operator_reviewed_private_body_is_private_not_unknown(agency):
    # 민간이라고 아는 것을 모름으로 두면 그 발주처 하나가 공고 전체의 실적 집계를
    # 멈춘다. 민간 판정은 해당 실적만 조용히 제외한다.
    assert status(agency) is False


@pytest.mark.parametrize(
    "agency",
    [
        "한국문화예술교육진흥원",   # 진흥원
        "서울올림픽기념국민체육진흥공단",  # 공단
        "한국교육학술정보원",       # 정보원
        "아동권리보장원",           # 보장원
        "국방기술품질원",           # 품질원
        "한국원자력안전기술원",     # 기술원
        "청암대학교 산학협력단",    # 산학협력단
    ],
)
def test_institution_form_in_the_name_is_public(agency):
    assert status(agency) is True


@pytest.mark.parametrize(
    "agency",
    ["국토교통부", "인사혁신처", "특허청", "고용노동부", "재외동포청", "한국영상대학교"],
)
def test_named_bodies_come_from_the_registry_not_a_suffix(agency):
    # 부·처·청은 '발주처'·'사업본부'와 글자가 겹친다. 접미사로 가르지 않고
    # 이름 자체를 레지스트리에 둔다.
    assert status(agency) is True


@pytest.mark.parametrize(
    "agency",
    [
        "서울올림픽기념국민체육진 흥공단",  # 이름 가운데 공백이 들어갔다
        "한국산업인력공단 국가직무 능력표준원",
    ],
)
def test_import_noise_does_not_hide_the_institution_form(agency):
    assert status(agency) is True


@pytest.mark.parametrize(
    "agency",
    [
        "한빛공사",          # 공기업일 수도, 건설공사 업체일 수도 있다
        "한국테스트연구소",  # 기업부설연구소가 같은 말을 쓴다
        "테스트교육원",      # 사설 교육원이 같은 말을 쓴다
        "테스트박물관",      # 사립 박물관이 있다
    ],
)
def test_a_form_that_private_bodies_also_use_is_not_decided_by_the_name(agency):
    # 형태가 갈리는 말은 레지스트리나 운영자 확인에 맡긴다.
    assert status(agency) is None


@pytest.mark.parametrize(
    "agency",
    ["전라북도 군산시", "강원도 춘천시", "제주도 서귀포시", "전북특별자치도 전주시"],
)
def test_region_prefixes_cover_names_recorded_before_renaming(agency):
    assert status(agency) is True


@pytest.mark.parametrize(
    "agency",
    ["주식회사 테스트", "㈜테스트", "유한회사 테스트", "테스트 개인사업자"],
)
def test_private_markers_stay_private(agency):
    assert status(agency) is False


@pytest.mark.parametrize(
    "agency",
    [
        "알 수 없는 어떤 곳",
        "",
        "발주처",              # 기관이 아니라 일반 명사다
        "테스트사업본부",      # 민간 조직도 같은 말을 쓴다
    ],
)
def test_an_unconfirmed_counterparty_is_never_assumed_public(agency):
    # 모르는 것을 공공으로 간주하면 점수가 부풀려진다. 호출부가 계산을 멈추도록
    # 모름을 그대로 돌려준다.
    assert status(agency) is None


def test_a_private_marker_outranks_an_institution_suffix():
    # 형태만으로 공공이라 부르지 않는다. 민간 표기가 있으면 그것이 우선이다.
    assert status("주식회사 한국테스트연구원") is False
