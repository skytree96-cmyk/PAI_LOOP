from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from importlib.resources import files
from typing import Any, Iterable


PROFILE_RESOURCE = "data/department_keyword_profiles.json"
MAX_USER_KEYWORDS = 20
MAX_USER_KEYWORD_LENGTH = 60
BUSINESS_TOP_MIN_STRONG = 1
BUSINESS_TOP_MIN_SUPPORTING = 2
BUSINESS_REVIEW_SUPPORTING = 1
_FIELD_SEPARATOR = " ⟂ "
_INSTITUTIONAL_EDUCATION_TERMS = (
    "교육지원청",
    "교육대학교",
    "교육대학",
    "교육청",
    "교육부",
)


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _without_institutional_education_terms(value: object) -> str:
    """Remove organization names that are not themselves an education service.

    A bare ``교육`` match in ``교육지원청`` or ``교육부`` describes the buyer,
    not the procured work.  Longer service phrases such as ``교직원 교육`` or a
    category such as ``교육용역`` remain in the text and still match the
    organization-wide education baseline.
    """

    text = _normalize(value)
    for term in _INSTITUTIONAL_EDUCATION_TERMS:
        text = text.replace(_normalize(term), " ")
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=1)
def load_department_keyword_profiles() -> dict[str, Any]:
    """Load and validate the versioned, public organization keyword catalog."""

    resource = files("pai_loop").joinpath(PROFILE_RESOURCE)
    payload = json.loads(resource.read_text(encoding="utf-8"))
    required_root = {"version", "baseline", "departments", "ranking_policy"}
    missing_root = required_root - payload.keys()
    if missing_root:
        raise ValueError(f"department keyword profile root fields missing: {sorted(missing_root)}")
    if not isinstance(payload["departments"], list) or not payload["departments"]:
        raise ValueError("department keyword profiles must contain at least one department")

    ids: set[str] = set()
    required_profile = {
        "id",
        "name",
        "aliases",
        "strong_keywords",
        "supporting_keywords",
        "excluded_keywords",
        "regions",
    }
    for profile in [payload["baseline"], *payload["departments"]]:
        missing = required_profile - profile.keys()
        if missing:
            raise ValueError(f"department keyword profile fields missing: {sorted(missing)}")
        profile_id = str(profile["id"])
        if profile_id in ids:
            raise ValueError(f"duplicate department keyword profile id: {profile_id}")
        ids.add(profile_id)
        title_required = profile.get("title_required_keywords", [])
        if not isinstance(title_required, list) or not all(
            isinstance(item, str) and item.strip() for item in title_required
        ):
            raise ValueError("title_required_keywords must be a list of non-empty strings")
        known_terms = {
            _normalize(item)
            for item in [*profile["strong_keywords"], *profile["supporting_keywords"]]
        }
        unknown_title_required = {
            _normalize(item) for item in title_required
        } - known_terms
        if unknown_title_required:
            raise ValueError(
                "title_required_keywords must also be strong/supporting keywords: "
                f"{sorted(unknown_title_required)}"
            )
    weights = payload["baseline"].get("weights", {})
    expected_weights = {
        "user_keyword",
        "baseline_strong",
        "baseline_supporting",
        "department_strong",
        "department_supporting",
        "region",
        "exclusion",
    }
    if expected_weights - weights.keys():
        raise ValueError("baseline ranking weights are incomplete")
    ranking_policy = payload["ranking_policy"]
    if not isinstance(ranking_policy, dict) or ranking_policy != {
        "business_top": {
            "min_strong_keywords": BUSINESS_TOP_MIN_STRONG,
            "min_supporting_keywords": BUSINESS_TOP_MIN_SUPPORTING,
        },
        "business_review": {
            "strong_keywords": 0,
            "supporting_keywords": BUSINESS_REVIEW_SUPPORTING,
        },
        "region_routing": {"separate_from_business_rank": True},
    }:
        raise ValueError("department ranking policy does not match the executable policy")
    return payload


def parse_search_keywords(value: str | Iterable[str] | None) -> list[str]:
    """Parse a safe, bounded comma/newline/pipe-separated user keyword list."""

    if value is None:
        return []
    candidates: list[str] = []
    values = [value] if isinstance(value, str) else list(value)
    for item in values:
        candidates.extend(re.split(r"[,\n|]+", str(item)))

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        keyword = re.sub(r"\s+", " ", candidate).strip()
        normalized = _normalize(keyword)
        if not normalized or normalized in seen:
            continue
        if len(keyword) > MAX_USER_KEYWORD_LENGTH:
            raise ValueError(f"검색 키워드는 {MAX_USER_KEYWORD_LENGTH}자 이하여야 합니다.")
        seen.add(normalized)
        unique.append(keyword)
        if len(unique) > MAX_USER_KEYWORDS:
            raise ValueError(f"검색 키워드는 최대 {MAX_USER_KEYWORDS}개까지 입력할 수 있습니다.")
    return unique


def get_department_profile(department_id: str | None) -> dict[str, Any] | None:
    if not department_id or _normalize(department_id) in {"", "organization", "all"}:
        return None
    catalog = load_department_keyword_profiles()
    needle = _normalize(department_id)
    for profile in catalog["departments"]:
        candidates = [profile["id"], profile["name"], *profile.get("aliases", [])]
        if needle in {_normalize(item) for item in candidates}:
            return profile
    raise KeyError(department_id)


def _contains(text: str, keyword: str) -> bool:
    normalized = _normalize(keyword)
    if not normalized:
        return False
    # Short Latin abbreviations such as AC/DC/AI must match as tokens rather
    # than arbitrary substrings. Korean phrases intentionally use substring
    # matching because particles and compound nouns are common in notice titles.
    if re.fullmatch(r"[a-z0-9][a-z0-9 .+/#-]*", normalized):
        pattern = rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return normalized in text


def _matched_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for item in keywords:
        keyword = str(item).strip()
        normalized = _normalize(keyword)
        if keyword and normalized not in seen:
            seen.add(normalized)
            unique.append(keyword)
    matched: list[str] = []
    for keyword in sorted(unique, key=len, reverse=True):
        if not _contains(text, keyword):
            continue
        normalized = _normalize(keyword)
        # Prefer the most specific phrase inside one keyword tier. For example,
        # "AI 교육" is kept and the shorter "교육" in the same tier is omitted.
        if any(normalized in _normalize(existing) for existing in matched):
            continue
        matched.append(keyword)
    return matched


def _priority(score: float) -> tuple[str, str]:
    if score >= 55:
        return "HIGH", "최우선"
    if score >= 30:
        return "MEDIUM", "우선"
    if score >= 10:
        return "WATCH", "검토"
    return "LOW", "낮음"


def rank_notice_for_department(
    *,
    title: str,
    agency: str = "",
    category: str = "",
    department_id: str | None = None,
    user_keywords: str | Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return an explainable fit score for one notice and one search owner.

    This is discovery priority only. It is deliberately independent from the
    eligibility engine and must never convert a qualification REVIEW/FAIL.
    """

    catalog = load_department_keyword_profiles()
    baseline = catalog["baseline"]
    department = get_department_profile(department_id)
    weights = {**baseline["weights"], **(department.get("weights", {}) if department else {})}
    parsed_user_keywords = parse_search_keywords(user_keywords)
    # A visible separator prevents a multi-word phrase from being fabricated
    # across field boundaries (for example agency ``공공기관`` + category
    # ``교육용역`` must not become the strong keyword ``공공기관 교육``).
    searchable_text = _normalize(_FIELD_SEPARATOR.join((title, agency, category)))
    business_text = _normalize(_FIELD_SEPARATOR.join((title, category)))

    matched_user = _matched_keywords(searchable_text, parsed_user_keywords)
    matched_baseline_strong = _matched_keywords(searchable_text, baseline["strong_keywords"])
    if "교육" in matched_baseline_strong:
        baseline_business_text = _without_institutional_education_terms(
            _FIELD_SEPARATOR.join((title, agency, category))
        )
        if not _contains(baseline_business_text, "교육"):
            matched_baseline_strong.remove("교육")
    matched_baseline_supporting = _matched_keywords(searchable_text, baseline["supporting_keywords"])
    matched_department_strong = _matched_keywords(
        searchable_text, department["strong_keywords"] if department else []
    )
    matched_department_supporting = _matched_keywords(
        searchable_text, department["supporting_keywords"] if department else []
    )
    if department:
        title_required = {
            _normalize(item) for item in department.get("title_required_keywords", [])
        }

        def has_required_business_context(keyword: str) -> bool:
            return _normalize(keyword) not in title_required or _contains(business_text, keyword)

        matched_department_strong = [
            item for item in matched_department_strong if has_required_business_context(item)
        ]
        matched_department_supporting = [
            item for item in matched_department_supporting if has_required_business_context(item)
        ]
    matched_regions = _matched_keywords(searchable_text, department["regions"] if department else [])
    if department and department.get("group") == "지역그룹" and matched_regions:
        region_terms = [_normalize(item) for item in matched_regions]

        def is_duplicate_region_signal(keyword: str) -> bool:
            normalized = _normalize(keyword)
            return any(normalized in region or region in normalized for region in region_terms)

        # A place name is a single location boost. Regional profiles often list
        # both an official name (supporting) and its short form (region); counting
        # both would let geography outrank the actual business owner.
        matched_department_strong = [
            item for item in matched_department_strong if not is_duplicate_region_signal(item)
        ]
        matched_department_supporting = [
            item for item in matched_department_supporting if not is_duplicate_region_signal(item)
        ]
    exclusions = [*baseline["excluded_keywords"], *(department["excluded_keywords"] if department else [])]
    matched_exclusions = _matched_keywords(searchable_text, exclusions)

    breakdown: list[dict[str, Any]] = []

    def add(source: str, keywords: list[str], weight_key: str) -> None:
        weight = float(weights[weight_key])
        breakdown.extend({"source": source, "keyword": keyword, "weight": weight} for keyword in keywords)

    add("USER", matched_user, "user_keyword")
    add("BASELINE_STRONG", matched_baseline_strong, "baseline_strong")
    add("BASELINE_SUPPORTING", matched_baseline_supporting, "baseline_supporting")
    add("DEPARTMENT_STRONG", matched_department_strong, "department_strong")
    add("DEPARTMENT_SUPPORTING", matched_department_supporting, "department_supporting")
    add("REGION", matched_regions, "region")
    add("EXCLUSION", matched_exclusions, "exclusion")

    raw_score = round(sum(float(item["weight"]) for item in breakdown), 1)
    score = round(max(0.0, min(100.0, raw_score)), 1)
    business_score = round(
        sum(
            float(item["weight"])
            for item in breakdown
            if item["source"] in {"DEPARTMENT_STRONG", "DEPARTMENT_SUPPORTING"}
        ),
        1,
    )
    routing_score = round(
        sum(float(item["weight"]) for item in breakdown if item["source"] == "REGION"),
        1,
    )
    ranking_scope = (
        "REGION" if department and department.get("group") == "지역그룹" else "BUSINESS"
    )
    if ranking_scope == "REGION":
        recommendation_tier = "ROUTING" if matched_regions else "NONE"
    elif matched_exclusions:
        # Non-core procurement signals block business recommendations even
        # when a strong department term happens to be present. Geography is
        # handled above as routing metadata, not as a bid recommendation.
        recommendation_tier = "NONE"
    elif department and (
        len(matched_department_strong) >= BUSINESS_TOP_MIN_STRONG
        or len(matched_department_supporting) >= BUSINESS_TOP_MIN_SUPPORTING
    ):
        recommendation_tier = "TOP"
    elif department and (
        not matched_department_strong
        and len(matched_department_supporting) == BUSINESS_REVIEW_SUPPORTING
    ):
        recommendation_tier = "REVIEW"
    else:
        recommendation_tier = "NONE"
    department_score = routing_score if ranking_scope == "REGION" else business_score
    priority, priority_label = _priority(score)

    reasons: list[str] = []
    if matched_user:
        reasons.append(f"입력 검색어 일치: {', '.join(matched_user)}")
    baseline_matches = [*matched_baseline_strong, *matched_baseline_supporting]
    if baseline_matches:
        reasons.append(f"전사 기본 사업 일치: {', '.join(baseline_matches)}")
    department_matches = [*matched_department_strong, *matched_department_supporting]
    if department_matches:
        reasons.append(f"부서 사업영역 일치: {', '.join(department_matches)}")
    if matched_regions:
        reasons.append(f"담당 지역 일치: {', '.join(matched_regions)}")
    if matched_exclusions:
        reasons.append(f"비주력 공고 신호: {', '.join(matched_exclusions)}")
    if not reasons:
        reasons.append("현재 제목·기관·분류에서 등록 키워드 일치가 없습니다.")

    selected = department or baseline
    return {
        "profile_version": catalog["version"],
        "department_id": selected["id"],
        "department_name": selected["name"],
        "group": department.get("group") if department else "전사",
        "ranking_scope": ranking_scope,
        "recommendation_tier": recommendation_tier,
        "top_recommendation_eligible": recommendation_tier == "TOP",
        "review_candidate": recommendation_tier == "REVIEW",
        "score": score,
        "raw_score": raw_score,
        "department_score": department_score,
        "business_score": business_score,
        "routing_score": routing_score,
        "priority": priority,
        "priority_label": priority_label,
        "matched_user_keywords": matched_user,
        "matched_baseline_keywords": baseline_matches,
        "matched_department_keywords": department_matches,
        "matched_regions": matched_regions,
        "matched_exclusions": matched_exclusions,
        "score_breakdown": breakdown,
        "reasons": reasons,
    }


def rank_notice_across_departments(
    *,
    title: str,
    agency: str = "",
    category: str = "",
    user_keywords: str | Iterable[str] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return only policy-qualified business recommendations, best first.

    Region owners and one-supporting-keyword candidates have separate result
    contracts so geography and weak signals cannot enter the business rank.
    """

    catalog = load_department_keyword_profiles()
    rankings = [
        rank_notice_for_department(
            title=title,
            agency=agency,
            category=category,
            department_id=profile["id"],
            user_keywords=user_keywords,
        )
        for profile in catalog["departments"]
    ]
    differentiated = [
        item
        for item in rankings
        if item["ranking_scope"] == "BUSINESS" and item["recommendation_tier"] == "TOP"
    ]
    differentiated.sort(
        key=lambda item: (
            -float(item["department_score"]),
            -float(item["score"]),
            str(item["department_name"]),
        )
    )
    return differentiated[: max(0, limit)]


def rank_notice_review_candidates(
    *,
    title: str,
    agency: str = "",
    category: str = "",
    user_keywords: str | Iterable[str] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return weak business candidates for human review, never as top rank."""

    catalog = load_department_keyword_profiles()
    candidates = [
        rank_notice_for_department(
            title=title,
            agency=agency,
            category=category,
            department_id=profile["id"],
            user_keywords=user_keywords,
        )
        for profile in catalog["departments"]
        if profile.get("group") != "지역그룹"
    ]
    candidates = [item for item in candidates if item["recommendation_tier"] == "REVIEW"]
    candidates.sort(
        key=lambda item: (
            -float(item["business_score"]),
            -float(item["score"]),
            str(item["department_name"]),
        )
    )
    return candidates[: max(0, limit)]


def route_notice_across_regions(
    *,
    title: str,
    agency: str = "",
    category: str = "",
    user_keywords: str | Iterable[str] | None = None,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Return geography-only routing without affecting business ranking."""

    catalog = load_department_keyword_profiles()
    routes = [
        rank_notice_for_department(
            title=title,
            agency=agency,
            category=category,
            department_id=profile["id"],
            user_keywords=user_keywords,
        )
        for profile in catalog["departments"]
        if profile.get("group") == "지역그룹"
    ]
    routes = [item for item in routes if item["recommendation_tier"] == "ROUTING"]
    routes.sort(
        key=lambda item: (
            -float(item["routing_score"]),
            str(item["department_name"]),
        )
    )
    return routes[: max(0, limit)]


def notice_matches_user_keywords(
    *,
    title: str,
    agency: str = "",
    category: str = "",
    user_keywords: str | Iterable[str] | None = None,
) -> bool:
    keywords = parse_search_keywords(user_keywords)
    if not keywords:
        return True
    searchable_text = _normalize(_FIELD_SEPARATOR.join((title, agency, category)))
    return bool(_matched_keywords(searchable_text, keywords))
