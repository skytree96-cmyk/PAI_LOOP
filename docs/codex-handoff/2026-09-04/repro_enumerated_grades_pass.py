import sys; sys.path.insert(0, "tests"); sys.path.insert(0, "docs/codex-handoff/2026-09-04")
import copy
from repro_credit_rating_range_blocks_table import TABLE, anchor, case
from test_quantitative_rule_extraction import payload_with_table, build, issue_codes

# 가설 검증: 신용등급을 "A- 이상" 범위 대신 등급을 전부 열거한 원문이면 통과하는가?
t = copy.deepcopy(TABLE)
t["criteria"][1]["cases"] = [
    case("AAA, AA+, AA0, AA-, A+, A0, A-", ["AAA","AA+","AA0","AA-","A+","A0","A-"], 10, 1),
    case("BBB+, BBB0, BBB-", ["BBB+","BBB0","BBB-"], 8, 2),
    case("BB+, BB0, BB-", ["BB+","BB0","BB-"], 6, 3),
    case("B+ 이하", ["B+ 이하"], 4, 4),
]
SRC = "\n".join([
    "정량평가표",
    "용역수행실적(금액) 10점",
    "10억원 이상", "10점",
    "5억원 이상 10억원 미만", "8점",
    "3억원 이상 5억원 미만", "6점",
    "3억원 미만", "4점",
    "신용평가등급 10점",
    "AAA, AA+, AA0, AA-, A+, A0, A-", "10점",
    "BBB+, BBB0, BBB-", "8점",
    "BB+, BB0, BB-", "6점",
    "B+ 이하", "4점",
    "정량평가 총점 20점",
])
p = build(payload_with_table(t), source=SRC)
print("--- 신용등급 '전부 열거' 원문 (부산형) ---")
print("  status:", p.status, "| available:", len(p.available_candidates), "| review:", len(p.review_candidates))
print("  issues:", sorted(issue_codes(p)))
for c in p.available_candidates: print("  AVAILABLE ->", c.metric)
for c in p.review_candidates:    print("  REVIEW    ->", c.metric)
