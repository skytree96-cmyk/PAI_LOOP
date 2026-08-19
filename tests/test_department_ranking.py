from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pai_loop.department_ranking import (
    load_department_keyword_profiles,
    notice_matches_user_keywords,
    parse_search_keywords,
    rank_notice_across_departments,
    rank_notice_for_department,
    rank_notice_review_candidates,
    route_notice_across_regions,
)


def test_profile_catalog_is_versioned_and_contains_no_personnel_fields() -> None:
    catalog = load_department_keyword_profiles()

    assert catalog["version"] == "2026.08.18-1"
    assert catalog["baseline"]["strong_keywords"] == ["교육", "컨설팅"]
    assert len(catalog["departments"]) >= 24
    serialized = json.dumps(catalog, ensure_ascii=False).casefold()
    assert all(token not in serialized for token in ('"person"', '"owner"', '"contact"', '"employee"'))
    assert len({item["id"] for item in catalog["departments"]}) == len(catalog["departments"])
    assert catalog["ranking_policy"] == {
        "business_top": {"min_strong_keywords": 1, "min_supporting_keywords": 2},
        "business_review": {"strong_keywords": 0, "supporting_keywords": 1},
        "region_routing": {"separate_from_business_rank": True},
    }


def test_same_notice_has_different_priority_by_search_owner() -> None:
    notice = {
        "title": "7급 승진후보자 역량평가 및 역량교육 위탁운영",
        "agency": "광역시 인재개발원",
        "category": "교육 용역",
    }

    competency = rank_notice_for_department(**notice, department_id="future-competency-solution")
    membership = rank_notice_for_department(**notice, department_id="customer-membership")

    assert competency["score"] > membership["score"]
    assert competency["department_score"] > 0
    assert "승진후보자" in competency["matched_department_keywords"]
    assert membership["department_score"] == 0
    assert competency["reasons"]
    assert competency["score_breakdown"]


def test_baseline_and_user_keywords_are_explainable() -> None:
    result = rank_notice_for_department(
        title="공공기관 리더십 교육 컨설팅 용역",
        agency="공공기관",
        department_id="talent-development",
        user_keywords="리더십, 조직문화",
    )

    assert result["priority"] in {"HIGH", "MEDIUM"}
    assert result["matched_user_keywords"] == ["리더십"]
    assert set(result["matched_baseline_keywords"]) == {"교육", "컨설팅"}
    assert any(item["source"] == "USER" for item in result["score_breakdown"])


def test_education_baseline_ignores_buyer_name_but_keeps_real_service_context() -> None:
    unrelated = rank_notice_for_department(
        title="신청사 인터넷전화서비스 용역",
        agency="경기도평택교육지원청",
    )
    relevant = rank_notice_for_department(
        title="교직원 교육 운영 용역",
        agency="경기도평택교육지원청",
    )

    assert "교육" not in unrelated["matched_baseline_keywords"]
    assert "교육" in relevant["matched_baseline_keywords"]


def test_agency_legal_suffix_does_not_route_unrelated_work_to_external_cooperation() -> None:
    unrelated = rank_notice_across_departments(
        title="연구장비 유지보수 용역",
        agency="가상대학교 산학협력단",
        limit=30,
    )
    related = rank_notice_across_departments(
        title="지역 산학협력 네트워크 운영",
        agency="가상대학교 산학협력단",
        limit=30,
    )

    assert "customer-external-cooperation" not in {
        item["department_id"] for item in unrelated
    }
    external = next(
        item for item in related if item["department_id"] == "customer-external-cooperation"
    )
    assert "산학협력" in external["matched_department_keywords"]


def test_generic_overseas_training_routes_global_not_ai_future_education() -> None:
    generic = rank_notice_across_departments(
        title="공공기관 임직원 해외연수 운영",
        limit=30,
    )
    university = rank_notice_across_departments(
        title="대학생 글로벌 해외연수 운영",
        limit=30,
    )
    university_review = rank_notice_review_candidates(
        title="대학생 글로벌 해외연수 운영",
        limit=30,
    )

    generic_ids = {item["department_id"] for item in generic}
    university_ids = {item["department_id"] for item in university}
    assert "future-global-education" in generic_ids
    assert "future-ai-education" not in generic_ids
    assert "future-global-education" in university_ids
    assert "future-ai-education" not in university_ids
    assert "future-ai-education" in {
        item["department_id"] for item in university_review
    }


def test_top_departments_only_include_differentiated_matches() -> None:
    rankings = rank_notice_across_departments(
        title="인천광역시 승진 후보자 역량평가 교육",
        agency="인천광역시",
        category="교육용역",
    )

    assert rankings
    assert rankings[0]["department_id"] == "future-competency-solution"
    assert all(item["ranking_scope"] == "BUSINESS" for item in rankings)
    assert all(item["recommendation_tier"] == "TOP" for item in rankings)
    assert "region-central" not in {item["department_id"] for item in rankings}


def test_region_is_single_boost_and_does_not_outrank_business_expertise() -> None:
    rankings = rank_notice_across_departments(
        title="2026년 7급 승진후보자 역량(시책교육) 위탁운영 용역",
        agency="인천광역시인재개발원",
        category="교육용역",
        limit=10,
    )

    assert rankings[0]["department_id"] == "future-competency-solution"
    routes = route_notice_across_regions(
        title="2026년 7급 승진후보자 역량(시책교육) 위탁운영 용역",
        agency="인천광역시인재개발원",
        category="교육용역",
        limit=10,
    )
    central = next(item for item in routes if item["department_id"] == "region-central")
    assert central["ranking_scope"] == "REGION"
    assert central["recommendation_tier"] == "ROUTING"
    assert central["matched_regions"] == ["인천"]
    assert central["matched_department_keywords"] == []
    assert central["department_score"] == 14
    assert rankings[0]["department_score"] > central["department_score"]


def test_one_supporting_keyword_is_review_only_and_two_supporting_terms_are_top() -> None:
    one_supporting = rank_notice_across_departments(
        title="공공기관 팀빌딩 프로그램 운영",
        limit=30,
    )
    review = rank_notice_review_candidates(
        title="공공기관 팀빌딩 프로그램 운영",
        limit=30,
    )
    two_supporting = rank_notice_across_departments(
        title="공공기관 팀빌딩 및 조직문화 프로그램 운영",
        limit=30,
    )

    assert "talent-development" not in {
        item["department_id"] for item in one_supporting
    }
    talent_review = next(
        item for item in review if item["department_id"] == "talent-development"
    )
    assert talent_review["recommendation_tier"] == "REVIEW"
    assert talent_review["review_candidate"] is True
    assert talent_review["top_recommendation_eligible"] is False
    talent_top = next(
        item for item in two_supporting if item["department_id"] == "talent-development"
    )
    assert talent_top["recommendation_tier"] == "TOP"
    assert talent_top["top_recommendation_eligible"] is True
    assert talent_top["review_candidate"] is False


def test_keyword_phrase_cannot_be_fabricated_across_agency_and_category_fields() -> None:
    top = rank_notice_across_departments(
        title="공공기관 팀빌딩 프로그램 운영",
        agency="가상 공공기관",
        category="교육용역",
        limit=30,
    )
    review = rank_notice_review_candidates(
        title="공공기관 팀빌딩 프로그램 운영",
        agency="가상 공공기관",
        category="교육용역",
        limit=30,
    )

    assert "talent-public-sector" not in {item["department_id"] for item in top}
    assert "talent-development" in {item["department_id"] for item in review}


def test_region_owner_receives_region_weight() -> None:
    central = rank_notice_for_department(
        title="인천광역시 공무원 교육 운영",
        agency="인천광역시",
        department_id="region-central",
    )
    busan = rank_notice_for_department(
        title="인천광역시 공무원 교육 운영",
        agency="인천광역시",
        department_id="region-busan-gyeongnam",
    )

    assert central["score"] > busan["score"]
    assert "인천" in central["matched_regions"]


def test_internal_support_profiles_are_present_but_conservatively_weighted() -> None:
    catalog = load_department_keyword_profiles()
    profile_ids = {item["id"] for item in catalog["departments"]}
    assert {"management-planning", "finance-support"} <= profile_ids

    planning = rank_notice_for_department(
        title="공공기관 경영전략 및 성과관리 컨설팅",
        department_id="management-planning",
    )
    business = rank_notice_for_department(
        title="공공기관 경영전략 및 성과관리 컨설팅",
        department_id="talent-development",
    )

    planning_weights = [
        item["weight"]
        for item in planning["score_breakdown"]
        if item["source"].startswith("DEPARTMENT")
    ]
    assert planning["department_score"] > business["department_score"]
    assert max(planning_weights) <= 10


def test_user_keyword_parser_is_bounded_and_filter_uses_or_semantics() -> None:
    assert parse_search_keywords(" AI 교육, 리더십\nAI 교육 | ESG ") == ["AI 교육", "리더십", "ESG"]
    assert notice_matches_user_keywords(
        title="공공기관 ESG 경영 컨설팅",
        user_keywords="리더십,ESG",
    )
    assert not notice_matches_user_keywords(
        title="공공기관 ESG 경영 컨설팅",
        user_keywords="해외연수,승진후보자",
    )

    with pytest.raises(ValueError, match="최대 20개"):
        parse_search_keywords(",".join(f"키워드{i}" for i in range(21)))
    with pytest.raises(ValueError, match="60자"):
        parse_search_keywords("가" * 61)


def test_exclusion_signal_cannot_create_negative_display_score() -> None:
    result = rank_notice_for_department(
        title="청사 시설공사 및 식자재 구매",
        department_id="talent-development",
    )

    assert result["score"] == 0
    assert result["raw_score"] < 0
    assert set(result["matched_exclusions"]) == {"시설공사", "식자재"}


def test_exclusion_blocks_business_recommendation_but_not_separate_region_routing() -> None:
    payload = {
        "title": "AI 에이전트 교육센터 시설공사",
        "agency": "인천광역시",
        "category": "시설공사",
    }
    selected = rank_notice_for_department(
        **payload,
        department_id="talent-development",
    )
    top = rank_notice_across_departments(**payload, limit=30)
    review = rank_notice_review_candidates(**payload, limit=30)
    routes = route_notice_across_regions(**payload, limit=10)

    assert selected["matched_exclusions"] == ["시설공사"]
    assert selected["business_score"] > 0
    assert selected["score"] == 0
    assert selected["recommendation_tier"] == "NONE"
    assert "talent-development" not in {item["department_id"] for item in top}
    assert "talent-development" not in {item["department_id"] for item in review}
    assert routes[0]["department_id"] == "region-central"
    assert routes[0]["recommendation_tier"] == "ROUTING"


def test_keyword_profile_and_ranked_notice_api(client: TestClient) -> None:
    profile_response = client.get("/api/v1/departments/keyword-profiles")
    assert profile_response.status_code == 200
    assert profile_response.json()["baseline"]["strong_keywords"] == ["교육", "컨설팅"]
    assert len(profile_response.json()["departments"]) >= 24

    notices = [
        {
            "notice_key": "RANK-COMPETENCY",
            "bid_notice_no": "RANK-001",
            "title": "7급 승진후보자 역량평가 교육 및 팀빌딩 위탁운영",
            "agency": "인천광역시인재개발원",
            "deadline": "2027-09-01T09:00:00Z",
            "category": "교육용역",
        },
        {
            "notice_key": "RANK-GLOBAL",
            "bid_notice_no": "RANK-002",
            "title": "대학생 글로벌 해외연수 운영",
            "agency": "국립대학교",
            "deadline": "2027-08-01T09:00:00Z",
            "category": "교육용역",
        },
    ]
    for payload in notices:
        assert client.post("/api/v1/notices", json=payload).status_code == 201

    competency = client.get(
        "/api/v1/notices",
        params={"department_id": "future-competency-solution", "limit": 20},
    )
    assert competency.status_code == 200
    body = competency.json()
    assert body[0]["notice_key"] == "RANK-COMPETENCY"
    assert body[0]["department_ranking"]["department_name"] == "역량솔루션본부"
    assert body[0]["department_ranking"]["reasons"]
    assert body[0]["top_department_rankings"][0]["department_id"] == "future-competency-solution"
    assert body[0]["department_review_candidates"]
    assert body[0]["region_routing"][0]["department_id"] == "region-central"
    assert all(
        item["recommendation_tier"] == "TOP"
        for item in body[0]["top_department_rankings"]
    )
    assert all(
        item["recommendation_tier"] == "REVIEW"
        for item in body[0]["department_review_candidates"]
    )
    assert all(
        item["recommendation_tier"] == "ROUTING"
        for item in body[0]["region_routing"]
    )

    global_owner = client.get(
        "/api/v1/notices",
        params={"department_id": "future-global-education", "limit": 20},
    ).json()
    assert global_owner[0]["notice_key"] == "RANK-GLOBAL"

    filtered = client.get(
        "/api/v1/notices",
        params={
            "department_id": "organization",
            "search_keywords": "승진후보자,ESG",
            "limit": 20,
        },
    )
    assert filtered.status_code == 200
    assert [item["notice_key"] for item in filtered.json()] == ["RANK-COMPETENCY"]
    assert filtered.json()[0]["department_ranking"]["matched_user_keywords"] == ["승진후보자"]


def test_ranked_notice_api_rejects_unknown_department_and_oversized_keywords(
    client: TestClient,
) -> None:
    unknown = client.get("/api/v1/notices", params={"department_id": "not-a-department"})
    assert unknown.status_code == 422
    assert "부서 또는 검색 키워드" in unknown.json()["detail"]

    oversized = client.get("/api/v1/notices", params={"search_keywords": "가" * 61})
    assert oversized.status_code == 422


def test_selected_department_api_ranks_top_before_one_keyword_review(client: TestClient) -> None:
    for payload in (
        {
            "notice_key": "RANK-REVIEW-FIRST-DEADLINE",
            "bid_notice_no": "RANK-REVIEW-001",
            "title": "공공기관 팀빌딩 프로그램 운영",
            "agency": "가상 공공기관",
            "deadline": "2027-01-01T09:00:00Z",
            "category": "교육용역",
        },
        {
            "notice_key": "RANK-TOP-LATER-DEADLINE",
            "bid_notice_no": "RANK-TOP-001",
            "title": "공공기관 팀빌딩 및 조직문화 프로그램 운영",
            "agency": "가상 공공기관",
            "deadline": "2027-12-01T09:00:00Z",
            "category": "교육용역",
        },
    ):
        assert client.post("/api/v1/notices", json=payload).status_code == 201

    response = client.get(
        "/api/v1/notices",
        params={"department_id": "talent-development", "limit": 20},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["notice_key"] for item in body[:2]] == [
        "RANK-TOP-LATER-DEADLINE",
        "RANK-REVIEW-FIRST-DEADLINE",
    ]
    assert body[0]["department_ranking"]["recommendation_tier"] == "TOP"
    assert body[1]["department_ranking"]["recommendation_tier"] == "REVIEW"


def test_daily_teams_card_separates_business_review_and_region_labels() -> None:
    workflow_path = (
        Path(__file__).parents[1]
        / "workflows"
        / "pai-loop-10-daily-opportunity-briefing.json"
    )
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    card_node = next(
        item
        for item in workflow["nodes"]
        if item["name"] == "Build Consolidated Teams Adaptive Card"
    )
    source = card_node["parameters"]["jsCode"]

    assert "item.top_departments?.[0]" in source
    assert "item.department_review_candidates?.[0]" in source
    assert "item.region_routing?.[0]" in source
    assert "title: '사업부 추천'" in source
    assert "title: '추가 검토'" in source
    assert "title: '지역 라우팅'" in source
    assert "부서 우선순위" not in source
