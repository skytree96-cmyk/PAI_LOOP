# Codex 작업 지시서 — 정량점수 미산정, 실행으로 검증된 근본 원인 (2026-09-04)

## 이 문서가 어제 문서들과 다른 점

`docs/codex-handoff/2026-09-03/`의 감사·프롬프트는 **코드를 읽어서** 낸 결론이다.
이 문서는 **코드를 실제로 실행해서** 낸 결론이다. 실행 결과 어제 결론 중 하나가
불충분했고(PR #69만으로는 안 됨), 어제 "미확인"으로 남긴 질문 하나가 확정됐다
(부산 20/20은 공고 특화 하드코딩에 의존함). 어제 문서와 충돌하면 **이 문서가 우선**한다.

재현 스크립트 2개를 같은 폴더에 두었다. Codex는 작업 전에 반드시 둘을 실행해
아래 결과가 재현되는지 확인하고, 수정 후에도 회귀 canary로 계속 쓸 것:

```bash
git checkout origin/codex/flat-hwpx-quantitative-repair-20260903   # PR #69 브랜치
python docs/codex-handoff/2026-09-04/repro_credit_rating_range_blocks_table.py
#  기대: status INCOMPLETE / available 0 / review 2 / CASE_CATEGORY_MISMATCH, CASE_NUMBER_MISMATCH
python docs/codex-handoff/2026-09-04/repro_enumerated_grades_pass.py
#  기대: status AVAILABLE / available 2 / issues []
```

(스크립트는 `tests/test_quantitative_rule_extraction.py`의 헬퍼를 import하므로
저장소 루트에서 실행할 것.)

## 현재 상태 (2026-09-04 09시 KST 기준, GitHub 직접 확인)

- `main` = `0c07820` (PR #70 자격요건 수정 병합). **어제 이후 정량 관련 코드 변경 0건.**
- PR #69 (`codex/flat-hwpx-quantitative-repair-20260903`, `815d038`) — 여전히 미병합.
  **이 브랜치에서 `pytest tests/` 전체 스위트 통과 확인**(실행함). 병합 자체는 안전.
- PR #71 (어제 문서 정정) — 미병합. 문서만이라 리스크 없음.
- 즉 Phase 1~5 중 코드에 반영된 것은 없다. "왜 안 되는가"는 어제와 동일한 프로덕션
  코드에 대한 질문이며, 아래가 실행으로 확정한 답이다.

---

## 실행으로 확정된 원인 사슬

### 원인 1 — PR #69는 필요조건이지 충분조건이 아니다 (실증)

대표 실패 공고의 형태(실적금액 BRACKET 4구간 10점 + 신용등급 CASE_TABLE 4행 10점
= 20점, HWPX 셀 분리, 섹션 마커 없음)를 PR #69 브랜치에서 그대로 재현했다.

| 입력 | 결과 (PR #69 브랜치) |
|---|---|
| 실적금액 BRACKET **단독** | ✅ `AVAILABLE`, available 1, issues 없음 |
| 신용등급 CASE_TABLE **단독**, 원문이 "A- 이상" 식 범위 표현 | ❌ `INCOMPLETE`, `CASE_CATEGORY_MISMATCH` ×4, `CASE_NUMBER_MISMATCH` ×4 |
| 둘을 한 표에 (실제 공고 형태) | ❌ `INCOMPLETE`, **available 0, review 2** ← 프로덕션 진단값(criteria 0, review candidates 2)과 정확히 일치 |
| 둘을 한 표에, 신용등급 원문이 등급을 **전부 열거** ("AAA, AA+, AA0, …") | ✅ `AVAILABLE`, **available 2**, issues 없음 |

결론: **PR #69의 셀 분리 복구는 정상 작동한다**(실적금액이 단독으로 통과함이 증거).
막히는 것은 신용등급이고, 신용등급이 막히면 같은 표의 실적금액까지 함께 REVIEW로
떨어진다(원인 3).

### 원인 2 — 신용등급 "범위 표현"은 현재 데이터 모델로 검증 불가 (Catch-22)

한국 공공 용역 정량평가표의 신용등급 행은 거의 예외 없이 **범위**로 쓴다:
"A- 이상 10점 / BBB- 이상 A- 미만 8점 / BB- 이상 BBB- 미만 6점 / BB- 미만 4점".
현재 코드는 이 형태를 표현할 방법이 없다:

- CASE_TABLE의 `IN` 연산자는 **추출 검증 단계**에서 모든 `category_values`가 원문
  행에 문자 그대로 들어있어야 통과한다
  (`quantitative_rule_extraction.py` 4977-4995행,
  `_case_literal_contains_exact_category`). LLM이 "A- 이상"을 의미대로
  `["A-","A0","A+","AA-",…]`로 펼치면 원문에 "A0"가 없으니 `CASE_CATEGORY_MISMATCH`.
- 반대로 LLM이 `category_values=["A- 이상"]` 하나로 넣으면 추출 검증은 통과하지만,
  **점수 계산 단계**의 `category_points()`(`quantitative_formula.py` 506-512행)가
  정규화 문자열 **정확 멤버십**만 하므로 회사 등급 `"A0"`은 `{"A- 이상"}`에
  없어 `None` → 미산정.
- `GTE` 연산자 경로는 `comparison_value`가 `Decimal`(숫자)이어야 해서 문자 등급에
  쓸 수 없다(4957-4967행).

즉 **어떤 방식으로 추출해도 신용등급 범위 행은 추출 검증과 점수 계산을 동시에
통과할 수 없다.** 이것이 신용등급 항목이 있는 거의 모든 공고에서 정량점수가
안 나오는 근본 원인이다.

### 원인 3 — 표 단위 fail-closed 때문에 멀쩡한 실적금액까지 같이 죽는다

원인 1의 표에서 실적금액은 자체 이슈가 하나도 없는데도 REVIEW였다. 표 안의 한
기준이라도 원문 검증에 실패하면 표 전체가 `SOURCE_VALIDATED`가 되지 못하고
(`TABLE_NOT_SOURCE_VALIDATED` / `TABLE_CRITERIA_LINKAGE_INCOMPLETE` 계열),
`available_candidates`가 표 단위로 비워진다. 스코어링 엔진에는 `PARTIAL_ACTIVE`
(검증된 항목만 부분 산정)가 있지만, 그 앞단인 추출 검증이 표 단위로 막혀 있어
`PARTIAL_ACTIVE`까지 도달하지 못한다.

### 원인 4 — 부산교육한마당 20/20은 공고 특화 하드코딩에 의존한다 (어제 미확인 → 확정)

`quantitative_rule_extraction.py` 1167-1181행:

```python
# Deliberate 부산-source limitation: ... Only this exact enterprise-credit
# category shape plus its exact terminal footnote proves column ownership.
# Do not generalize it to other credit tables until the extractor preserves
# native table row/column geometry.
_BUSAN_ENTERPRISE_CREDIT_CATEGORY_ROWS = (
    ("AAA", "AA+", "AA0", "AA-", "A+", "A0", "A-", "BBB+", "BBB0"),
    ("BBB-", "BB+", "BB0", "BB-"),
    ("B+", "B0", "B-"),
    ("CCC+ 이하",),
)
_BUSAN_CREDIT_RATING_FOOTNOTE_RE = re.compile(
    r"^\*등급별평점이소수점이하의숫자가있는경우"
    r"소수점다섯째자리에서반올림함[.]?$"
)
```

부산 공고는 (a) 신용등급을 **전부 열거**한 표이고, (b) 그 정확한 행 구성과
각주 문구를 인식하는 전용 상수가 코드에 박혀 있다. 원인 2의 실증(전부 열거하면
통과)과 합치면, **부산이 되고 다른 공고가 안 되는 이유는 알고리즘이 범용이어서가
아니라 부산 표 모양이 우연히 현재 모델이 유일하게 다룰 수 있는 형태이고 그
형태를 전용 코드가 인식하기 때문**이다. 어제 감사가 `manual_override`/`seed_score`
류만 검색해서 이걸 놓쳤다. 점수 자체를 박은 건 아니지만, 원본 요구사항("특정 공고용
하드코딩 금지, 공고마다 다른 표를 범용으로")에는 실질적으로 위배된다.

### 원인 5 (어제와 동일, 변경 없음) — 재계산 없이는 화면이 안 바뀐다

`docs/codex-handoff/2026-09-03/codex_prompt_quantitative_display_fix.md` 참고.
코드가 고쳐져도 기존 스냅샷은 `recompute_current=true`로 재계산해야 화면에 반영된다.

---

## 수정 지시 (우선순위 순, 각각 별도 PR)

### P0 — PR #69 병합 (선행, 저위험)

전체 스위트 통과를 실행으로 확인했다. 어제 문서(Phase 1)의 metric 헤더 검증 보강은
**여전히 권고**하지만, 그것이 없어도 실적금액 BRACKET 경로는 이번 재현에서 오탐
없이 통과했으므로 **보강을 병합의 선행조건으로 걸지 말 것** — 보강은 병합 후
별도 PR로 진행한다. 어제 문서의 줄번호는 PR #69 브랜치 기준(868/909/950/2864행)이다.

### P1 — 신용등급 범위 표현 지원 (핵심, 이게 없으면 정량점수는 계속 안 나온다)

설계 원칙: **문자 등급에 서열(ordinal)을 부여하고, 범위 행을 서열 구간으로
표현·검증·계산**한다. 하드코딩된 부산 상수를 대체하는 것이 목표.

1. **등급 서열표 1곳 신설** (예: `quantitative_formula.py`):
   `AAA > AA+ > AA0 > AA- > A+ > A0 > A- > BBB+ > BBB0 > BBB- > BB+ > BB0 > BB- > B+ > B0 > B- > CCC+ > …`.
   "A"와 "A0"를 같은 서열로 정규화(`_normalize_case_category` 확장). 이 표는
   회사 신용등급 사실(`company.credit_rating`, 현재 A0)을 같은 서열로 매핑하는
   데도 쓴다.
2. **범위 행 파싱**: 원문 행 "A- 이상", "BBB- 이상 A- 미만", "BB- 미만",
   "B+ 이하"를 `(lower_grade, lower_inclusive, upper_grade, upper_inclusive)`로
   파싱하는 함수 추가. 기존 숫자 BRACKET의 `min/max_value + inclusive` 구조와
   동일한 형태로 표현하면 스코어링 쪽 재사용이 쉽다 — 즉 **신용등급은 CASE_TABLE
   `IN`이 아니라 서열형 BRACKET으로 다루는 것**을 우선 검토할 것.
3. **추출 검증**: 범위 행에 대해서는 "모든 category_values가 원문에 있어야 함"
   대신 "파싱된 경계 등급 문자열(예: `A-`, `BBB-`)이 원문 행에 있어야 함"으로
   검증한다. 경계 등급이 서열표에 없는 문자열이면 fail-closed(REVIEW).
4. **점수 계산**: `category_points()`의 정확 멤버십 대신, 회사 등급의 서열이
   어느 구간에 들어가는지로 점수를 정한다. 구간이 겹치거나(중복 매칭) 빈틈이
   있으면(무매칭) 현재 BRACKET의 `BRACKETS_NOT_EXHAUSTIVE_OR_OVERLAPPING`과 같은
   방식으로 fail-closed.
5. **부산 전용 상수 처리**: `_BUSAN_ENTERPRISE_CREDIT_CATEGORY_ROWS`/
   `_BUSAN_CREDIT_RATING_FOOTNOTE_RE`는 새 범용 경로가 부산 공고를 **동일하게
   20/20으로 재현**하는 것을 `test_busan_education_quantitative_e2e.py`로 확인한
   뒤에만 제거한다. 순서를 지키지 않으면 유일하게 되던 공고가 깨진다.
6. **테스트** (최소):
   - 이 폴더의 두 재현 스크립트가 각각 `AVAILABLE 2`로 바뀌는지 (범위형·열거형 모두)
   - 회사 등급 A0에 대해 "A- 이상"→10, "BBB- 이상 A- 미만" 표에서 A0가 8이 아닌
     10으로 가는지 (경계 포함/미포함 정확성)
   - 서열표에 없는 등급 문자열("A**", 오탈자)은 REVIEW로 fail-closed
   - 구간 겹침/빈틈 negative test
   - 부산 e2e 20/20 유지

### P2 — 표 단위 fail-closed를 기준 단위로 완화 (원인 3)

신용등급 행 하나가 REVIEW여도, 자체 이슈가 없는 실적금액 기준은 `available`로
남겨 스코어링의 `PARTIAL_ACTIVE`가 실제로 작동하게 한다. **단, 표 총점 검증
(`TABLE_TOTAL_MISMATCH`)이나 표 자체 앵커 실패처럼 "표 전체의 신뢰가 깨진" 이슈는
계속 표 단위로 막아야 한다.** 기준별로 독립 판정해도 되는 이슈 코드와 표 전체를
막아야 하는 이슈 코드를 명시적으로 두 집합으로 나눠 코드에 적을 것. 이 변경은
P1과 독립적으로도 가치가 있다(신용등급을 못 풀어도 실적금액은 부분 산정됨).

### P3 — 어제 문서의 Phase 2, 4, 5는 그대로 유효

- Phase 2 (부산 라이브 값 확인): 원인 4가 확정됐으므로 "범용 계산인가"라는
  질문의 답은 이미 "아니오, 전용 상수 의존"이다. 다만 라이브 값이 실제로
  `AUTO_ACTIVE`로 저장돼 있는지 확인은 여전히 필요(P1-5의 회귀 기준선).
- Phase 4 (n8n W11 COMPLETED 재정의): 변경 없음.
- Phase 5 (표출/캐시/재계산): 변경 없음. P1·P2 배포 후 반드시 `recompute_current`
  일괄 실행.

---

## Codex가 답해야 할 질문 (작업 후 보고)

1. 운영 DB의 진행 공고 중 신용등급 항목이 **범위 표현**인 공고 수 vs **전부 열거**인
   공고 수. (이 비율이 P1의 실제 효과 크기다. 이 환경에서는 DB 접근이 없어 못 셌다.)
2. P1 적용 후 대표 실패 공고 `PPS-R26BK01704704-000-d3a27e912c`의 `quantitative-estimate`
   응답이 `activation_status=AUTO_ACTIVE`(또는 최소 `PARTIAL_ACTIVE`)로 바뀌는지.
3. 부산 e2e가 부산 전용 상수 제거 후에도 20/20인지.

---

## 이 환경에서 확인하지 못한 것 (정직하게)

- 운영 DB/Render/n8n 미접근 — 실제 공고 253건 중 범위형/열거형 분포는 모른다.
- 재현 스크립트의 LLM 출력 형태(`category_values`를 펼쳤는지, 범위 문자열 하나로
  넣었는지)는 **내가 가정**한 것이다. 실제 LLM이 어느 쪽으로 뽑는지는
  `manual_analysis.py`의 `quantitative-diagnostics`(PIN 필요)에서 `review_candidate_shapes`를
  보면 확인된다. 다만 **어느 쪽이든 원인 2의 Catch-22에 걸린다**는 점은 코드로
  확정이므로 결론은 바뀌지 않는다.
