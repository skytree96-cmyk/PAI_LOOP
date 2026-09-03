"""Reproduce the representative failing notice shape against the current branch.

Shape from the operator's diagnostic dump (PA202601820 / R26BK01704704):
  - PERFORMANCE_AMOUNT : BRACKET, 4 brackets, max 10
  - CREDIT_RATING      : CASE_TABLE, cases 10/8/6/4, max 10
  - total 20, flat HWPX text (no [HWP SECTION n] marker), condition cell and
    score cell on separate lines.
"""
from __future__ import annotations
import sys, json
sys.path.insert(0, "tests")

from test_quantitative_rule_extraction import (  # type: ignore
    anchor, payload_with_table, build, issue_codes, ATTACHMENT_ID,
)


def bracket(label, literal, lo, hi, pts, lo_inc=True, hi_inc=False):
    return {
        "label": label, "literal": literal,
        "min_value": lo, "max_value": hi,
        "min_inclusive": lo_inc, "max_inclusive": hi_inc,
        "points": pts, "evidence": anchor(literal),
    }


def case(literal, cats, pts, order):
    return {
        "literal": literal, "operator": "IN", "comparison_value": None,
        "category_values": cats, "award_kind": "POINTS",
        "award_value": pts, "row_order": order, "evidence": anchor(literal),
    }


SOURCE = "\n".join([
    "정량평가표",
    "용역수행실적(금액) 10점",
    "10억원 이상", "10점",
    "5억원 이상 10억원 미만", "8점",
    "3억원 이상 5억원 미만", "6점",
    "3억원 미만", "4점",
    "신용평가등급 10점",
    "A- 이상", "10점",
    "BBB- 이상 A- 미만", "8점",
    "BB- 이상 BBB- 미만", "6점",
    "BB- 미만", "4점",
    "정량평가 총점 20점",
])

TABLE = {
    "table_id": "QUANT-TABLE-1", "label": "정량평가표",
    "criteria": [
        {
            "criterion_id": "PERF-AMT", "label": "용역수행실적(금액)",
            "criterion_literal": "용역수행실적(금액) 10점", "max_points": 10,
            "scoring_method": "BRACKET", "metric": "PERFORMANCE_AMOUNT", "unit": "억원",
            "brackets": [
                bracket("10억원 이상", "10억원 이상", 10, None, 10),
                bracket("5억원 이상 10억원 미만", "5억원 이상 10억원 미만", 5, 10, 8),
                bracket("3억원 이상 5억원 미만", "3억원 이상 5억원 미만", 3, 5, 6),
                bracket("3억원 미만", "3억원 미만", None, 3, 4, lo_inc=False),
            ],
            "threshold": None, "formula_literal": None,
            "required_evidence": ["company.performance.amount"],
            "evidence": anchor("용역수행실적(금액) 10점"), "ambiguity_reason": None,
        },
        {
            "criterion_id": "CREDIT", "label": "신용평가등급",
            "criterion_literal": "신용평가등급 10점", "max_points": 10,
            "scoring_method": "CASE_TABLE", "metric": "CREDIT_RATING", "unit": "등급",
            "brackets": [], "threshold": None, "formula_literal": None,
            "cases": [
                case("A- 이상", ["A-", "A0", "A+", "AA-", "AA0", "AA+", "AAA"], 10, 1),
                case("BBB- 이상 A- 미만", ["BBB-", "BBB0", "BBB+"], 8, 2),
                case("BB- 이상 BBB- 미만", ["BB-", "BB0", "BB+"], 6, 3),
                case("BB- 미만", ["B+", "B0", "B-", "CCC"], 4, 4),
            ],
            "recognition_conditions": [],
            "required_evidence": ["company.credit_rating"],
            "evidence": anchor("신용평가등급 10점"), "ambiguity_reason": None,
        },
    ],
    "total_points": 20, "total_evidence": anchor("정량평가 총점 20점"),
    "minimum_score": None, "minimum_evidence": None, "ambiguity_reason": None,
}

profile = build(payload_with_table(TABLE), source=SOURCE)
print("status               :", profile.status)
print("available_candidates :", len(profile.available_candidates))
print("review_candidates    :", len(profile.review_candidates))
print("issue_codes          :", sorted(issue_codes(profile)))
for c in profile.available_candidates:
    print("  AVAILABLE ->", c.metric, c.scoring_method, "max", c.max_points)
for c in profile.review_candidates:
    print("  REVIEW    ->", c.metric, c.scoring_method, "max", c.max_points)
