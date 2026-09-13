# Claude 독립 검토: 정량점수가 나오지 않는 원인 (2026-09-13)

검토 대상: 브랜치 `docs/claude-quantitative-handoff-0913`, 코드 기준 `d817ce5`.
검토 방식: 인수인계서·조사 문서 3종·과거 조사 4장을 읽고, 정량 경로 코드를 추적한 뒤
**합성 입력으로 로컬 검증만** 수행했다. 유료 호출 0회, 운영 DB·n8n·Render 접근 0회,
코드 수정 0건. 원문·회사 자료·private 재생 파일은 보지 못했다. 아래에서
"코드 확인 / 이전 보고 인용 / 미검증 가설"을 구분한다.

---

## 0. 결론 요약

1. 기존 진단의 구조 서술(후보에 부모 범위 없음, 누락은 문자열 목록)은 맞다.
   그러나 그것이 "정성·가격 결함이 정량 표 전체를 막는" **실제 경로는 아니다.**
   실제 경로는 (a) 누락 문장 게이트가 첨부 단위 INCOMPLETE를 찍는 것,
   (b) 표 총점 검사·UNKNOWN 행이 표 전체를 강등하는 것,
   (c) 표가 둘 이상이면 부분 활성이 꺼지는 것이다. 셋 다 코드와 합성 검증으로 확인했다.
2. **엔진 1.8.2의 OUT_OF_SCOPE 분리는 실제 PPS 동적 경로에서 도달 불가능하다.**
   "30공고 변화 0"은 수정 내용상 당연한 결과이며, 부모 범위 가설을 지지하지도
   반박하지도 못한다.
3. **30공고 표본의 지배 차단은 범위 구조가 아니라 입력 확보(계약 경계·커버리지)다.**
   인수인계서가 스스로 적은 "25행 중 24행 계약/세대 미선택"이 그 증거다.
   이 표본에서는 어떤 범위 설계도 결과를 바꿀 수 없다.
4. 최소 수정 1순위는 누락 문장의 **주어 귀속**이다. 저장 record는 fingerprint
   개정으로 호출 0회 재검증할 수 있다.

---

## 1. 코드로 확인한 원인

### 1.1 누락 문장 게이트가 첨부 단위로 INCOMPLETE를 찍는다

`src/pai_loop/source_gap_policy.py:652` `asserts_scoring_artifact_absence`는
채점 산출물 단어(배점·기준·산식·점수 구간 등)와 부재 표현이 함께 있으면
**주어가 정성이든 가격이든 설문이든 차단**한다. 예외는
`_NON_QUANTITATIVE_SCOPE_EXCLUSION_RE` 한 개의 좁은 정규식뿐이다.

합성 검증 결과(문장은 내가 만든 것이며 원문이 아니다):

| 문장 | 결과 |
|---|---|
| 정성평가(60점) 세부 항목은 평가위원 판단으로 채점되어 정량 테이블에서 제외함 | 무관 (통과) |
| 정성평가 항목(사업이해도, 추진전략)의 등급별 점수 기준은 본문에 제시되지 않음 | **차단** |
| 정성평가 60점의 세부 배점표는 확인되지 않음 | **차단** |
| 입찰가격 평가(20점)는 별도 가격산식으로 계산하므로 정량 테이블에서 제외함 | 무관 |
| 입찰가격 평가 산식은 본 문서에 기재되지 않음 | **차단** |
| 직원 설문 평가의 문항별 배점은 본 문서에 없음 | **차단** |
| 정성평가 세부 기준 및 신용평가 등급별 배점은 본 문서에 포함되어 있지 않음 | 차단 (혼합, 정당) |
| 신용평가등급 배점표의 등급 구간 일부가 누락됨 | 차단 (정량, 정당) |

차단된 문장은 `src/pai_loop/quantitative_rule_extraction.py:7697`에서
`EXTRACTION_DECLARED_INCOMPLETE`(disposition INCOMPLETE)가 된다.
이 이슈는 **table_id·criterion_id가 없다.** 그래서

- 표 자체가 AVAILABLE이어도 record가 INCOMPLETE가 된다.
  유효한 표에 정성 전용 문장 하나만 붙여 검증기를 돌린 결과:
  `record=INCOMPLETE tables=['AVAILABLE'] available=1`.
- 부분 활성 경로가 절대 구제하지 못한다. `_partial_profile_review_criteria`는
  criterion에 묶이지 않은 이슈나 REVIEW가 아닌 이슈가 하나라도 있으면 None을
  반환한다(`src/pai_loop/quantitative_scoring.py:3849`).
- 이전 보고가 든 차단 1위 "추출기의 누락 선언 51/90첨부"가 바로 이 코드다.

부작용: `src/pai_loop/pps_enrichment.py:947`
`_accepted_quantitative_review_is_retryable`도 같은 술어를 쓰므로,
정성 전용 누락 문장이 있는 첨부가 **유료 재시도 대상**이 된다.
같은 완전한 입력을 다시 보내는 낭비가 여기서 생긴다.

### 1.2 표 총점 검사와 UNKNOWN 행이 표 전체를 강등한다

- 항목 만점 합계 ≠ `total_points`이면 `TABLE_TOTAL_MISMATCH`(INCOMPLETE,
  `quantitative_rule_extraction.py:7523`). 표가 INCOMPLETE이면 검증을 통과한
  행까지 전부 review로 강등된다(같은 파일 7824행).
  프롬프트는 정량 소계에 총점을 묶으라고 지시하지만
  (`src/pai_loop/integrations/openai_extraction.py` 1390행 부근),
  모델이 기술평가 전체(예: 100점)에 묶으면 20점짜리 정량 행이 전부 사라진다.
  합성 검증: 총점을 100으로 바꾸자 `record=INCOMPLETE tables=['INCOMPLETE'] available=0`.
- 정성 행이 표에 들어오면 `UNKNOWN_METRIC`(REVIEW, 7252행)은 견딜 수 있지만,
  `required_evidence`가 비거나 placeholder이면 `REQUIRED_EVIDENCE_INCOMPLETE`
  (INCOMPLETE, 7281행)가 함께 붙어 같은 결과가 난다.
  합성 검증: 사업이해도 10점 행(metric UNKNOWN, required_evidence ["UNKNOWN"])을
  추가하자 두 행 모두 INCOMPLETE로 강등.

### 1.3 표가 둘 이상이면 부분 활성이 꺼진다

`_partial_profile_review_criteria`는 `len(profile.tables) != 1`이면 None이다
(`quantitative_scoring.py:3796`). 구내식당처럼 제안평가와 설문이 별도 표이거나
두 첨부에 표가 나뉘면 UNKNOWN 행 하나로 공고 전체가 REVIEW_REQUIRED가 된다.
합성 검증: 기존 부분 활성 픽스처(`tests/test_quantitative_partial_activation.py`)에
빈 AVAILABLE 표 하나를 추가하자 `PARTIAL_ACTIVE` → `REVIEW_REQUIRED`.

### 1.4 입력 확보(커버리지) 게이트

`_current_dynamic_quantitative_profile`(`quantitative_scoring.py:1651`)은
현재 manifest의 **모든** 첨부에 현행 계약 시도가 있어야 하고, 없으면
incomplete로 표시한다. merge에서 `VALIDATED_RECORD_MISSING`(8357행)과
`ATTACHMENT_INCOMPLETE`(8657행)가 INCOMPLETE로 붙는다.
AUTO(3667행)와 PARTIAL(3806행) 모두 `expected == processed`를 요구한다.
163첨부 중 26개만 선택된 표본이라면 대부분 공고는 범위 논리에 닿기 전에 끝난다.

---

## 2. 기존 진단의 오류 또는 근거가 약한 부분

### 2.1 OUT_OF_SCOPE는 동적 경로에서 도달 불가능하다

- `src/pai_loop/quantitative_out_of_scope.py:72`: `metric_in_registry`이면 즉시 None.
- `quantitative_request_from_candidate_profile`는 `_metric_spec(candidate)`가 None인
  후보를 criteria에서 제외한다(변환 실패로 처리). 따라서 엔진에 도달하는 모든 동적
  criterion은 `category ∈ _CANONICAL_METRIC_REGISTRY`다.
- UNKNOWN metric 행은 애초에 available 후보가 되지 못한다(7252행).
- 호출을 가로채 확인: 부분 활성 픽스처와 "사업이해도 및 추진전략의 적정성" 라벨
  변형 모두에서 `out_of_scope_reason`은 `metric_in_registry=True`로만 호출됐고
  결과는 항상 None, `out_of_scope_points=0.0`.
- 관련 테스트는 모두 `QuantitativeCriterion(category="UNKNOWN")`을 손으로 만들어
  엔진에 직접 넣는다(`tests/test_quantitative_out_of_scope.py:113, 263`).

결론: 1.8.2가 실제로 영향을 주는 곳은 curated 카탈로그 프로필뿐이다.
진행 문서의 "화면도 정량 외 행을 별도 배치" 서술은 PPS 공고에는 아직 해당하지 않는다.

### 2.2 "정성 행을 표에서 분리한다"는 전제가 약하다

프롬프트는 판단형 행을 아예 빼라고 지시한다. 누수는 주로 **누락 문장**과
**총점 바인딩**으로 들어온다. 실제 빈도는 raw 없이 확인하지 못했다(미검증).

### 2.3 표본의 지배 차단은 커버리지다

인수인계서의 "24/25 계약/세대 미선택"과 1.4의 코드가 맞물린다. 부모 범위 설계는
이 표본에서 0건을 움직인다. 29건 REVIEW를 **첫 차단 이슈 코드별로 분해한 표**가
아직 없다. 재생 출력에 필드는 이미 있다(`profile.issue_counts`,
`compiled_request.activation_reasons`, `source_attempts[].stored_record_issues`).

### 2.4 차단 계층 구분 (수치 없는 순위는 매기지 않음)

| 계층 | 코드 위치 | 상태 |
|---|---|---|
| 입력 확보 | `quantitative_scoring.py:1651`, merge 8357/8657 | 표본의 대부분 (보고 인용 24/25) |
| 추출 구조 | 누락 게이트 652, 총점 7523, 증빙키 7281, 단일표 3796 | 보고 인용 51/90첨부 + 합성 확인 |
| 원문 검증 | literal·anchor 불일치 | 보고 인용 14·12·10건 |
| 회사 증빙 | frozen 부분집합 | 판단 보류 |
| 합산·저장·표시 | 엔진 1.8.2 | 맞지만 미도달 |

---

## 3. 최소 수정안과 회귀 위험

### 수정안 1 (권장, 가장 작음): 누락 문장의 주어 귀속

`asserts_scoring_artifact_absence`에 아래 술어를 추가한다.

> 문장의 주어가 정성·설문·평가위원(선택적으로 가격)뿐이고,
> 정량·신용·실적·재무·자격·필수 같은 객관 주어가 없고,
> 누락·판독·2차 절이 없으면 → 무관(False).

- 혼합 문장·누락·판독 불가는 계속 차단.
- 원문 감사는 저장된 `result.missing_or_unreadable`에 이미 남는다. 새 필드 불필요.
- `_TARGETED_RECORD_FINGERPRINT_REVISIONS["EXTRACTION_DECLARED_INCOMPLETE"]`를
  `typed-notice-reference-gaps-v4`로 올리면 해당 이슈를 가진 저장 record만
  무효화된다. 동일 바이트 재사용 경로(`pps_enrichment.py:3153`, `api_calls=0`)나
  재생 CLI로 **호출 0회** 재검증된다.
- 부수 효과: 유료 재시도 대상(1.1의 부작용)도 함께 줄어든다.

회귀 위험:
- `tests/test_source_gap_quantitative_scope.py`의
  `test_scope_exclusion_cannot_hide_objective_or_price_source_defects`가
  "가격평가 산식 미제공"을 차단으로 못 박고 있다. 가격 산식 부재는 정량 소계를
  바꾸지 못하므로 정책 선택으로 보이나, 이를 풀지는 **사용자 결정**이다.
  보수안은 정성·설문만 면제한다.
- 기대 효과는 51개 선언 중 몇 개가 뒤집히는지로 측정한다. 숫자를 미리 약속하지 않는다.

### 수정안 2: 다단계 표의 부분 활성 허용

`_partial_profile_review_criteria`의 단일 표 조건을
"review 행을 가진 표는 정확히 하나, 나머지 표는 모두 AVAILABLE이며 기계 프로필이
`_profile_activation_reasons`를 통과"로 완화한다. 기계 프로필 검사가
`_logical_quantitative_program`을 돌리므로 대체표 모호성은 유지된다.

회귀 위험: review 표와 AVAILABLE 표가 같은 소계의 대체표일 때 이중 계산.
소계가 같으면 거절하는 가드가 필요하다. 커버리지가 완전한 공고에서만 효과가 있다.

### 수정안 3 (선택): set-aside 도달 가능화 또는 문서 정정

부분 활성 review 행 중 이슈가 `UNKNOWN_METRIC`·`UNKNOWN_SCORING_METHOD`뿐이고
`out_of_scope_reason`이 사유를 주는 행만 OUT_OF_SCOPE로 보낸다.
literal·anchor 실패가 있는 행은 제외해야
`test_review_source_rows_remain_in_quantitative_upper_bound`
("검증 실패는 정성의 증거가 아니다")와 충돌하지 않는다.
구현하지 않는다면 진행 문서에 "OUT_OF_SCOPE는 현재 curated 프로필에만 적용된다"고 정정한다.

부모 범위·단계·소계를 계약에 넣는 큰 설계는 위 1·2 뒤에도 남는 사례
(총점 바인딩, 같은 항목명 반복)에만 필요하며, 지금 표본으로는 효과를 측정할 수 없다.

---

## 4. 유료 호출 없는 검증 순서

1. 기존 private 재생 출력에서 **공고별 첫 차단 계층**을 집계한다.
   커버리지 미완(VALIDATED_RECORD_MISSING·ATTACHMENT_INCOMPLETE·
   EXTRACTION_CONTRACT_PROOF_INVALID) / 누락 게이트(EXTRACTION_DECLARED_INCOMPLETE) /
   표 총점(TABLE_TOTAL_*) / 행 literal / 활성 사유. 이 표가 지금 빠진 측정이다.
2. 저장 raw의 누락 선언 51건 텍스트를 뽑아 현재와 수정안 1의
   `asserts_scoring_artifact_absence`를 비교하고, 뒤집히는 문장만 원문 대조 목록에 올린다.
3. 단위 회귀: `test_source_gap_quantitative_scope.py`에 정성·설문 전용 문장(무관)과
   혼합 문장(차단) 추가, `test_quantitative_partial_activation.py`에 2표 사례 추가,
   fingerprint 개정 테스트, 그리고 인수인계서의 11개 파일 365개.
4. 같은 고정 입력으로 재생을 다시 돌려 `activation_counts` 전후를 비교한다.
   위험 방향 이동(혼합 누락이 무관으로 바뀜)이 0인지도 확인한다.
5. 0.5.5 계약 경계 허용 여부는 별도 결정으로 남긴다. 코드 수정이 아니라 재추출 비용 판단이다.

이번 검토에서 실행한 것: 이 브랜치 체크아웃, `.venv-pai`(Python 3.13, CI의 3.12 아님)로
`test_quantitative_out_of_scope.py`, `test_quantitative_partial_activation.py`,
`test_source_gap_quantitative_scope.py`, `test_quantitative_scoring.py`,
`test_case_award_evidence.py` **173개 통과**. node가 없어 프론트 계약 테스트는 미실행.

---

## 5. 필요한 최소 추가 자료 (원문·회사 자료 불필요)

- 30공고 재생 출력의 공고별 요약만: `notice_key`, `profile.status`,
  `profile.issue_counts`, `compiled_request.activation_reasons`,
  첨부별 `header_contract`·`selected_by_pipeline`·`stored_record_status`·`stored_record_issues`.
- 누락 선언 51건의 문장 텍스트(첨부 ID, document_type 포함)와 그중 원문 확정 4건 표시.
- 홍천·RISE·구내식당 3건의 저장 raw에서 `quantitative_tables`의
  table_id·label·total_points·total_evidence.quote·criteria[].{label, max_points, metric}과
  `missing_or_unreadable`.
- 30공고 중 manifest 전체 첨부에 현행 계약 시도가 있는 공고 수.
  이 수가 1에 가깝다면 활성 1·검토 29는 커버리지만으로 설명된다.

---

## 부록. 재현 스크립트 (합성 입력, 외부 호출 없음)

저장소 루트에서 `PYTHONIOENCODING=utf-8 PYTHONPATH=src python <파일>`로 실행한다.
`tests/`의 기존 픽스처를 재사용한다.

### A. OUT_OF_SCOPE 미도달 확인

```python
import sys; sys.path.insert(0, "tests"); sys.path.insert(0, "src")
import pai_loop.quantitative_scoring as qs
from test_quantitative_partial_activation import _mixed_review_profile
calls = []
orig = qs.out_of_scope_reason
def spy(**kw):
    calls.append(kw); return orig(**kw)
qs.out_of_scope_reason = spy
prof = _mixed_review_profile()
rc = prof.review_candidates[0].model_copy(update={"label": "사업이해도 및 추진전략의 적정성"})
prof2 = prof.model_copy(update={"review_candidates": (rc,)})
for name, p in (("fixture", prof), ("qualitative-label", prof2)):
    req = qs.quantitative_request_from_candidate_profile(p)
    res = qs.estimate_quantitative_score(req)
    print(name, req.activation_status, [(e.label, e.status) for e in res.criteria], res.out_of_scope_points)
print("calls:", [(c["label"], c["metric_in_registry"]) for c in calls])
# 기대: 모든 호출이 metric_in_registry=True, OUT_OF_SCOPE 행 0개, out_of_scope_points 0.0
```

### B. 누락 문장 게이트 프로브

```python
import sys; sys.path.insert(0, "src")
from pai_loop.source_gap_policy import is_quantitative_irrelevant_gap as irr, asserts_scoring_artifact_absence as blk
S = [
 "정성평가(60점) 세부 항목은 평가위원 판단으로 채점되어 정량 테이블에서 제외함",
 "정성평가 항목(사업이해도, 추진전략, 운영계획, 사후관리)의 등급별 점수 기준은 본문에 제시되지 않음",
 "정성평가 60점의 세부 배점표는 확인되지 않음",
 "입찰가격 평가(20점)는 별도 가격산식으로 계산하므로 정량 테이블에서 제외함",
 "입찰가격 평가 산식은 본 문서에 기재되지 않음",
 "직원 만족도 설문(100점)은 발주기관 직원 대상 설문 결과로 산정되어 정량 테이블에서 제외함",
 "직원 설문 평가의 문항별 배점은 본 문서에 없음",
 "정성평가 세부 기준 및 신용평가 등급별 배점은 본 문서에 포함되어 있지 않음",
 "신용평가등급 배점표의 등급 구간 일부가 누락됨",
]
for s in S:
    print("BLOCK" if blk(s) else "pass ", irr(s), "|", s)
```

### C. 표 단위 강등 확인

```python
import sys; sys.path.insert(0, "tests"); sys.path.insert(0, "src")
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction as V
from test_quantitative_rule_extraction import ATTACHMENT_ID, VALID_SOURCE, payload_with_table, valid_table, anchor
def run(name, payload, source=VALID_SOURCE):
    r = V(payload, source_text=source, attachment_id=ATTACHMENT_ID, document_sha256="a"*64, manifest_sha256="b"*64)
    print(name, r.status, [t.status for t in r.tables], len(r.available_candidates), sorted({(i.code, i.disposition) for i in r.issues}))
run("baseline", payload_with_table())
run("qual-only gap", payload_with_table().model_copy(update={"missing_or_unreadable": ["정성평가 항목(사업이해도, 추진전략)의 등급별 점수 기준은 본문에 제시되지 않음"]}))
run("price-only gap", payload_with_table().model_copy(update={"missing_or_unreadable": ["입찰가격 평가 산식은 본 문서에 기재되지 않음"]}))
src = VALID_SOURCE + "기술평가 총점 100점\n"
t = valid_table(); t["total_points"] = 100; t["total_evidence"] = anchor("기술평가 총점 100점")
run("total=whole 100", payload_with_table(t), src)
src2 = VALID_SOURCE.replace("정량평가 총점 20점", "사업이해도 10점\n기술평가 총점 30점")
t = valid_table(); t["total_points"] = 30; t["total_evidence"] = anchor("기술평가 총점 30점")
t["criteria"].append({"criterion_id":"QUAL-1","label":"사업이해도","criterion_literal":"사업이해도 10점","max_points":10,
  "scoring_method":"UNKNOWN","metric":"UNKNOWN","unit":None,"brackets":[],"threshold":None,"formula_literal":None,
  "cases":[],"recognition_conditions":[],"required_evidence":["UNKNOWN"],"evidence":anchor("사업이해도 10점"),"ambiguity_reason":None})
run("judgment row UNKNOWN", payload_with_table(t), src2)
```

### D. 2표 부분 활성 확인

```python
import sys; sys.path.insert(0, "tests"); sys.path.insert(0, "src")
import pai_loop.quantitative_scoring as qs
from pai_loop.quantitative_rule_extraction import ImmutableQuantitativeTable, ImmutableEvidenceAnchor
from test_quantitative_partial_activation import _mixed_review_profile, ATTACHMENT_ID
prof = _mixed_review_profile()
t2 = ImmutableQuantitativeTable(source_attachment_id=ATTACHMENT_ID, table_id="TABLE-STAGE-2", label="직원설문", status="AVAILABLE",
    total_points=0.0, total_evidence=ImmutableEvidenceAnchor(attachment_id=ATTACHMENT_ID, page=3, section="x", quote="설문 총점", confidence=1),
    minimum_score=None, minimum_evidence=None, criterion_ids=(), available_criterion_ids=(), review_criterion_ids=())
for name, p in (("single", prof), ("two tables", prof.model_copy(update={"tables": (*prof.tables, t2)}))):
    req = qs.quantitative_request_from_candidate_profile(p)
    print(name, req.activation_status, req.activation_reasons)
# 기대: single → PARTIAL_ACTIVE, two tables → REVIEW_REQUIRED
```
