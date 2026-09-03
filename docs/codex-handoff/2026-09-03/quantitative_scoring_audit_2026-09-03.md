# PAI_LOOP 정량점수 산정 실패 — 코드 감사 보고서

- 감사 범위: `github.com/skytree96-cmyk/PAI_LOOP`, 정적 코드/Git 이력만 (읽기 전용, 토큰 미노출)
- main 기준 커밋: `0796e44df826454966306d53a9b437ae303af5af` (요청서에 명시된 배포 기준 커밋과 **일치 확인**)
- PR #69 브랜치: `codex/flat-hwpx-quantitative-repair-20260903`, 커밋 `815d038` (fetch 확인, 병합 안 됨 확인)
- **수행하지 않은 것(요청 금지사항 준수)**: 코드 수정·커밋·PR·배포 없음. Render/n8n 미실행. 분석/backfill 큐 미호출. 외부 LLM API 미호출. 저장된 공고 재분석 없음. 회사 비공개 원본 파일 외부 반출 없음. GitHub 토큰은 fetch 1회에만 사용 후 즉시 제거함(원격 URL에 토큰 미보관).
- **접근 불가/한계**: 이 환경은 GitHub 정적 클론만 가능하고 Render 운영 DB, n8n 실행 로그, 실제 프로덕션의 공고 R26BK01704704 저장 레코드에는 접근할 수 없다. 따라서 "실제 그 공고가 지금 이 순간 DB에 어떤 값으로 저장돼 있는가"는 사용자가 제시한 화면 진단값을 근거로 코드 로직과 대조하는 방식으로만 검증했다. **이 보고서의 모든 코드 인용은 실제로 확인한 사실이며, 프로덕션 DB 상태에 대한 추정은 별도로 "가설"로 표시한다.**

---

## 1. 한 문장 결론

REVIEW/미산정의 절대다수는 데이터 부족이 아니라 **HWP 표 셀 분리 복구 로직이 "안전하게 연결 불가"로 판정해 `INCOMPLETE`/`REVIEW_REQUIRED`로 fail-closed** 되기 때문이며, 부산교육한마당 20/20은 **엔진이 실제로 계산 가능함을 증명하는 합성(synthetic) 회귀 테스트로는 확인되지만, 그 특정 실제 공고의 라이브 값이 같은 방식으로 산출됐는지는 이 정적 감사로는 확정할 수 없다.**

---

## 2. 현재 정량 파이프라인 구조 (코드로 확인)

| 단계 | 파일 | 핵심 함수/식별자 |
|---|---|---|
| 1. 첨부 추출 | `src/pai_loop/document_extraction.py` | HWP/HWPX/PDF → 텍스트 라인화 |
| 2. LLM 규칙 추출 | `src/pai_loop/integrations/openai_extraction.py` | `ExtractionPayload`, `QuantitativeRuleCandidate`, `QuantitativeTableCandidate` |
| 3. 원문 검증·표 셀 재결합 | `src/pai_loop/quantitative_rule_extraction.py` | `build_quantitative_candidate_profile`, `_rebind_split_table_cell_literals`, `_rebind_candidate_table_cell_literals`(HWP 섹션 마커 기반, 기존), `_rebind_flat_split_table_cell_literals`(HWPX 무마커, **PR #69 신규**), `_criterion_window_matches`, `_metric_header_matches`, `_metric_header_is_unique` |
| 4. 규칙 활성화 판정 | `src/pai_loop/quantitative_scoring.py` | `QuantitativeEstimateRequest.validate_activation_contract`(334-370행), `rule_source_status`/`source_validation_status`/`activation_status` 3중 상태 |
| 5. 회사 사실 바인딩 | `src/pai_loop/quantitative_scoring.py`, `quantitative_performance.py` | `resolve_performance_register_facts`, `QuantitativeFact` |
| 6. 점수 계산 | `src/pai_loop/quantitative_scoring.py` | `estimate_quantitative_score` |
| 7. snapshot 저장 | `src/pai_loop/analysis_pipeline.py` | 2107-2113행 부근, `method_version=quantitative.engine_version` |
| 8. API 응답 | `src/pai_loop/analysis_api.py` | `QUANTITATIVE_ENGINE_VERSION` 노출 (42, 974, 1508행) |
| 9. UI 표시 | `src/pai_loop/static/app.js`/`index.html` | (이번 감사에서 상세 미열람, 필요시 후속 확인) |

**엔진 버전 확인**: `QUANTITATIVE_ENGINE_VERSION = "pai-loop-quantitative-engine-1.7.0"` — `quantitative_scoring.py:70`. 사용자가 제시한 "quantitative engine: 1.7.0"과 **정확히 일치**.

---

## 3. 코드로 확정된 원인

### 3-1. 상태값 3종 세트는 실제 코드 정의와 일치

`quantitative_scoring.py:334-338`:
```python
rule_source_status: Literal["AVAILABLE","MISSING","INCOMPLETE","NOT_APPLICABLE"]
source_validation_status: SourceValidationStatus = "REVIEW_REQUIRED"
activation_status: ActivationStatus = "REVIEW_REQUIRED"
```
사용자가 제시한 진단값(`rule_source_status: INCOMPLETE`, `source_validation_status: INCOMPLETE`, `activation_status: REVIEW_REQUIRED`)은 실제 코드가 표현 가능한 상태 조합이다. **AUTO_ACTIVE 조건**(352-359행)은 `rule_source_status=="AVAILABLE"` AND `source_validation_status=="SOURCE_VALIDATED"` AND 활성화 사유 없음 AND review_criteria 없음을 전부 요구하므로, 하나라도 어긋나면 최종 결과는 절대 AUTO_ACTIVE가 될 수 없다. 즉 "표는 찾았는데 점수가 안 나온다"는 관찰은 이 계약상 정상적으로 발생 가능한 상태이지 버그가 아니라 **설계된 fail-closed 동작**이다.

### 3-2. blocker 코드 11개 전부 실제 코드에 존재 (추측 아님)

`AMBIGUOUS_TABLE`, `ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT`, `BRACKET_NUMBER_MISMATCH`, `CASE_NUMBER_MISMATCH`, `EXTRACTION_DECLARED_INCOMPLETE`, `MAX_POINTS_LITERAL_MISMATCH`, `SOURCEWIDE_AMBIGUITY_SCOPE_UNSUPPORTED` → `quantitative_rule_extraction.py`. `SOURCE_VALIDATION_ISSUES_PRESENT`, `TABLE_CRITERIA_LINKAGE_INCOMPLETE`, `TABLE_NOT_SOURCE_VALIDATED` → `quantitative_scoring.py`. `TABLE_TOTAL_MISMATCH`는 양쪽 다. 화면의 "review candidates: 2" (PERFORMANCE_AMOUNT, CREDIT_RATING)는 이 blocker 코드들이 실제로 두 후보 각각에 부여된 결과로 보는 것이 코드 구조와 일치한다.

### 3-3. "조건은 나오는데 점수가 없는" 실패의 정확한 지점

`build_quantitative_candidate_profile`(quantitative_rule_extraction.py)이 만드는 `CandidateStatus = Literal["AVAILABLE","REVIEW","INCOMPLETE"]`(110-111행)가 표 단위 최종 상태를 결정한다. 이 상태가 `AVAILABLE`이 아니면 `quantitative_scoring.py`의 `QuantitativeEstimateRequest`로 넘어갈 때 `rule_source_status`가 `AVAILABLE`이 될 수 없고, 이는 다시 `activation_status`가 `AUTO_ACTIVE`/`PARTIAL_ACTIVE`가 될 조건을 원천 차단한다(위 352-370행 검증기). **즉 회사 실적·A0 신용등급 바인딩 이전 단계인 "원문 표 구조 검증" 단계에서 이미 막히는 구조가 코드상 사실이다** (사용자 가설 5번 확인).

### 3-4. `recompute_current`는 실제로 LLM 0회 호출 재검증 경로다

`manual_analysis.py:59-68`:
```python
run_extraction: bool = False
recompute_current: bool = False
retry_reviewed: bool = False
if self.recompute_current and (self.run_extraction or self.retry_reviewed):
    raise ValueError("recompute_current is a zero-provider-call operation")
if self.retry_reviewed and not self.run_extraction:
    raise ValueError("retry_reviewed requires run_extraction")
```
`recompute_current`는 명시적으로 "zero-provider-call"로 주석·검증되어 있다. 이는 **validator 로직만 바뀐 경우 LLM 재호출 없이 기존 추출 결과에 대해 재검증만 가능함을 코드로 확인**시켜준다(질문 8 답). `run_extraction=True`가 있어야 재추출이 발생하고, `retry_reviewed`는 `run_extraction` 없이는 쓸 수 없다(상호 배제 검증 존재).

---

## 4. 아직 확인되지 않은 가설 (코드만으로는 검증 불가)

- **부산교육한마당 실제 라이브 레코드의 저장 경로**: DB 접근 불가로 실제 그 공고의 analysis 레코드가 진짜 엔진 계산 결과인지, 과거의 수동 개입 흔적이 남아있는지는 확인 못 함 (3항 참조 — 다만 코드 전체에서 점수를 직접 대입/오버라이드하는 함수는 발견되지 않았다는 negative evidence는 있음).
- **W11의 "COMPLETED" 집계가 실제로 정량 성공과 무관하게 카운트되는지**: 워크플로 JSON의 validator는 분석 호출 자체의 구조적 성공(requested/processed/completed/skipped/failed 카운트)만 검사하며 quantitative activation 상태를 검사하는 코드는 찾지 못했다(9항 참조). 이는 사용자 가설과 **정황상 일치**하지만, 실제 n8n 실행 로그를 못 봐서 "확정"은 아니고 "코드 설계상 개연성 높음" 수준이다.
- **UI(app.js/index.html)가 REVIEW_REQUIRED를 정확히 어떻게 렌더링하는지**는 이번 감사에서 상세 열람하지 않았다. "배점표 발견 · 검증 보류" 문구의 정확한 출처 파일/줄은 후속 확인 필요.

---

## 5. 부산교육한마당 20/20 생성 경로

`tests/test_busan_education_quantitative_e2e.py` (733줄)를 확인했다. 이 파일은:
- `ATTACHMENT_ID = "BUSAN-EDUCATION-RFP-HWP"`라는 이름을 쓰지만, 실제 나라장터 첨부파일을 읽는 것이 아니라 **Python 코드로 `QuantitativeRuleCandidate`/`ExtractionPayload`를 직접 조립**한 합성(synthetic) 테스트다.
- `record_key`들이 `SYN-PRIVATE-BUSAN-*`, `BUSAN-EDU-*` 형태 — AGENTS.md의 "Synthetic fixtures use `SYN-` identifiers" 규칙과 일치하는 합성 데이터.
- `assert result.total_max_points == 20` (296, 396행)로 20점 만점 구조를 검증한다.

**결론**: 이 테스트는 "엔진이 4점+6점+10점 구조에서 회사 실적·A0 등급을 정확히 반영해 20/20을 계산할 능력이 있다"는 것을 증명하는 **범용 알고리즘 회귀 테스트**이지, 실제 그 공고의 프로덕션 첨부파일을 파싱한 결과가 아니다. 코드 전체에서 `manual_override`/`override_score`/`seed_score`/`hardcod*` 패턴의 점수 대입 함수는 검색되지 않았다(네거티브 근거). **즉 "20/20이 fixture로 이식된 것"이라는 가설을 뒷받침하는 코드는 없지만, "실제 생산 파이프라인이 이 합성 테스트와 동일한 경로로 그 공고를 통과했다"는 것도 이 감사로는 증명할 수 없다.** DB 조회 또는 해당 notice_key의 `/analysis/quantitative-diagnostics`(manual_analysis.py:1065-1085, 읽기 전용 GET) 응답을 실제로 열어봐야 최종 확인 가능하다 — 이번 작업 지시상 호출 금지이므로 미실행.

---

## 6. 대표 실패 공고가 막힌 정확한 단계

`PA202601820`/`R26BK01704704`의 진단값(`rule_source_status: INCOMPLETE`, `source_validation_status: INCOMPLETE`, `activation_status: REVIEW_REQUIRED`, `criteria count: 0`, `review candidates: 2`)을 코드 계약과 대조하면:

- `criteria count: 0`이면서 `review candidates: 2`라는 것은, `CandidateStatus`가 `AVAILABLE`인 확정 기준(criteria)은 하나도 없고, 두 후보(PERFORMANCE_AMOUNT, CREDIT_RATING) 모두 `REVIEW`/`INCOMPLETE`로 표 셀 재결합에 실패했음을 의미한다.
- 이는 **3-3절에서 확인한 대로 "표 구조 검증(source validation)" 단계에서 막힌 것이 맞다** — 회사 실적/A0 신용등급 데이터가 부족해서가 아니다. `PERFORMANCE_AMOUNT`, `CREDIT_RATING` 둘 다 회사 쪽 fact(실적 1,181건, 신용등급 A0)는 이미 준비돼 있다고 사용자가 명시했으므로, 문제는 순수하게 "공고 첨부의 표 셀을 하나의 평가 규칙으로 안전하게 연결하지 못함"에 있다는 사용자의 기술적 가설이 **코드 구조와 부합**한다.

---

## 7. 전체 공고에 영향을 줄 수 있는 실패 유형

| 유형 | 코드 근거 | 영향 |
|---|---|---|
| A. 평가표 자체 미검출 | `ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT` | criteria count 0, 화면에 정량 섹션 자체 미노출 |
| B-1. 표는 찾았으나 배점 리터럴 불일치 | `MAX_POINTS_LITERAL_MISMATCH`, `TABLE_TOTAL_MISMATCH` | criteria REVIEW/INCOMPLETE |
| B-2. 구간/케이스 개수 불일치 | `BRACKET_NUMBER_MISMATCH`, `CASE_NUMBER_MISMATCH` | 해당 후보만 REVIEW |
| B-3. HWP 섹션 마커 없는 HWPX 특유의 셀 분리 | 기존 `_rebind_candidate_table_cell_literals`는 `[HWP SECTION N]` 마커 의존 → HWPX엔 마커 없음 | HWPX 첨부 전반에 구조적으로 영향 (아래 6절 근거) |
| C. 표는 소스 검증됐지만 활성화 계약 불충족 | `validate_activation_contract`(quantitative_scoring.py 352-370) | AUTO_ACTIVE 불가, PARTIAL_ACTIVE로만 가능 |
| D. 다중 표/모호한 스코프 | `SOURCEWIDE_AMBIGUITY_SCOPE_UNSUPPORTED` | 여러 정량표 있는 첨부는 fail-closed |

---

## 8. PR #69 안전성 리뷰 (실제 diff 기준)

**변경 파일**: `src/pai_loop/quantitative_rule_extraction.py`(+210/-2), `tests/test_quantitative_rule_extraction.py`(+26/-2). Validator 버전 `0.6.11 → 0.6.12`(3-2번 계약대로 attachment-scoped 버전만 올림, 전역 무효화 아님 — 코드 주석과 일치).

**핵심 변경**: `_rebind_flat_split_table_cell_literals` 신규 함수. `_rebind_split_table_cell_literals`(기존 진입점) 안에서 HWP 섹션 마커 유무와 무관하게 **가장 먼저** 호출되도록 삽입됨 (3278행 부근).

**가장 중요한 발견 — 이미 존재하던 안전장치를 뒤집는 테스트 변경**:
기존 테스트 `test_split_cells_without_hwp_section_marker_are_not_rebound`(이름 그대로 "HWP 섹션 마커 없으면 재결합 안 함"을 보장하던 네거티브 테스트, `assert profile.status == "INCOMPLETE"`)를 PR #69가 **동일 입력에 대해 `assert profile.status == "AVAILABLE"`을 기대하는 `test_flat_hwpx_split_cells_are_rebound_from_exact_adjacent_score`로 교체**했다. 이것은 "HWPX처럼 섹션 마커가 없는 문서는 안전하게 확신할 수 없으니 재결합하지 않는다"는 기존의 보수적 정책을 의도적으로 뒤집는 변경이며, PR의 진짜 리스크 지점이 바로 이 지점이다.

**요청서가 우려한 안전장치별 실제 확인**:

| 우려 사항 | PR #69에서 실제로 구현됨? | 근거 |
|---|---|---|
| `_criterion_window_matches`만으론 부족 | 부분적으로 맞음 — 이 함수는 criterion literal 승격 시에만 쓰임(2892행 부근 재사용) | diff 내 `_criterion_window_matches(candidate, candidate.evidence.quote)` 1회 호출 |
| `_metric_header_matches`/`_metric_header_is_unique` 검증 필요 | **미구현 — 확인됨.** 이 두 함수는 기존에도 존재하지만(839, 880행) `_candidate_metric_max_header_spans`라는 **완전히 다른 경로**(sourcewide CASE_TABLE 헤더 검색, 3120행에서만 호출)에서만 쓰이고, PR #69의 신규 함수 `_rebind_flat_split_table_cell_literals`는 이 두 함수를 **한 번도 호출하지 않는다** | grep 결과 호출부 3곳뿐이며 전부 기존 경로. PR diff에 해당 함수 신규 호출 없음 |
| `_metric_header_is_unique` 유일성 검증 | 없음(위와 동일 이유) | 상동 |
| row span이 criterion span 뒤에 위치하는지 | **구현됨** — `region = (criterion_span[0], region_end)`로 다음 criterion 앞까지로 범위 제한, `_span_inside_region` 체크 | diff 중 `region_end = resolved_criteria[candidate_index+1][0] if ... else len(lines)` |
| 무관한 인접 행 결합 방지 negative test | 부분 구현 — `test_flat_hwpx_split_cells_do_not_cross_an_unrelated_line` 1건 추가(중간에 "별도 설명" 줄이 끼면 INCOMPLETE 확인) | diff 신규 테스트 |
| 다중 표 방지 | **구현됨** — 함수 최상단에서 `len(payload.quantitative_tables) != 1`이면 즉시 원본 반환 | diff 첫 조건문 |
| 중복 metric(같은 조건 문구 반복) 방지 | **구현됨(다른 메커니즘)** — `_unique_anchor_line_span`이 각 criterion의 리터럴/증거 인용이 소스 내 고유 위치에서만 발견될 때만 span을 반환하는 것으로 보이며(본 감사에서 함수 본문까지는 미열람, 이름과 호출 패턴상 추정), 하나라도 `None`이면 전체 표에 대해 복구를 포기함 | diff 중 `any(span is None for span in criterion_spans)` → 즉시 원본 반환 |
| 배점 합계/역방향 결합 방지 | **구현됨** — 스코어 셀은 `_bracket_row_window_matches`/`_case_row_window_matches`로 후보의 기대 점수·비교연산자 집합과 정확히 일치해야만 채택, `claimed_scores`로 같은 셀 중복 사용도 방지 | diff `Counter(_comparator_terms(condition)) == Counter(_expected_bracket_terms(bracket))`, `claimed_scores` 리스트 |

**종합 평가**: PR #69는 요청서가 우려한 리스크 중 **다중 표, 무관한 인접행, 배점 불일치, 셀 중복 사용**에는 실질적 방어 로직을 갖추고 있다(단정적 신뢰가 아니라 diff로 직접 확인). 다만 **"metric 헤더 자체가 후보의 metric과 일치하는지"를 검증하는 기존 함수(`_metric_header_matches`/`_metric_header_is_unique`)를 이 새 경로에 연결하지 않은 것은 코드로 확인된 실제 공백**이다. 실무적 위험도는 사용자가 우려한 만큼 크지는 않아 보이는데, 그 이유는 (a) 검증 대상 리터럴이 애초에 LLM이 뽑은 `candidate.criterion_literal`/`evidence.quote`의 소스 내 **유일한(unique) 위치**를 기준으로 재구성되므로 완전히 무관한 문서 어딘가의 "정량평가 / 10점" 같은 범용 텍스트가 끼어들 여지는 uniqueness 체크로 상당 부분 걸러지기 때문이다. 그러나 **metric 헤더 일치 확인이 빠진 것은 사실**이므로, 병합 전 반드시 보강 권고.

---

## 9. 가장 작은 수정안 (권고, 미구현)

1. `_rebind_flat_split_table_cell_literals`에 `_metric_header_matches(candidate, criterion_span_window)` 검증을 criterion_literal 승격 조건에 추가 — 기존 함수를 재사용하는 것이므로 신규 로직 최소화.
2. 위 검증을 추가한 새 negative test 1건: metric 키워드가 전혀 없는 범용 "정량평가 / N점" 헤더가 후보의 criterion span과 겹칠 때 복구를 거부하는지 확인.
3. Phase 1 배포 후 `criteria count`/`review candidates` 분포를 로그로 남겨 실제 개선폭 측정 (사용자가 다른 대화에서 요청한 자격요건 REVIEW 수정과 동일한 원칙).

---

## 10. 필요한 테스트 (사용자 제시 목록 대비 현황)

| 테스트 | 현재 존재? | 근거 |
|---|---|---|
| 부산교육한마당 expected 20/20 | **존재(합성)** | `test_busan_education_quantitative_e2e.py` |
| HWPX split-cell 기본 canary | **존재** | PR #69의 `test_flat_hwpx_split_cells_are_rebound_from_exact_adjacent_score` |
| 무관 인접 셀 비결합 negative | **부분 존재(1건)** | `test_flat_hwpx_split_cells_do_not_cross_an_unrelated_line` |
| 다중 표 | 미확인(별도 테스트 못 찾음, 함수 가드는 있음) | 함수 최상단 `!= 1` 체크만 있고 전용 테스트는 diff에 없음 — **추가 필요** |
| 중복 metric | 미확인 | uniqueness 가드는 있으나 전용 negative test는 diff에 없음 — **추가 필요** |
| 배점 합계 불일치 | 기존 `TABLE_TOTAL_MISMATCH` 관련 테스트 존재 가능성 높음(파일이 매우 큼, 이번 감사에서 전수 확인 못 함) | 후속 확인 필요 |
| 회사 fact 미연결 | `resolve_performance_register_facts` 관련 테스트 존재 가능성 높음(미확인) | 후속 확인 필요 |
| 일부 항목만 계산 가능(PARTIAL_ACTIVE) | `validate_activation_contract`의 PARTIAL_ACTIVE 분기 테스트 존재 가능성 높음(미확인) | 후속 확인 필요 |
| 공동수급 지분율 이중 차감 방지 | 미확인 — 사용자가 설명한 "이행비율 100% 미만이면 공동수급, 중복 차감 금지" 로직 자체를 코드에서 찾지 못함 | **정책/데이터 공백일 수 있음, 후속 확인 필요** |
| A0 신용등급 환산 | `resolve_performance_register_facts`/CREDIT_RATING 케이스 테이블 관련 테스트 존재 가능성 높음(미확인) | 후속 확인 필요 |

---

## 11. 기존 데이터 재처리 방법 (코드 계약 기준)

- validator 버전만 바뀐 경우: `recompute_current=True` (LLM 0회 호출, `manual_analysis.py:59-68` 계약대로 `run_extraction`/`retry_reviewed`와 동시 사용 불가).
- 추출 자체를 다시 해야 하는 경우(예: metric 헤더 검증 추가로 새로운 후보가 나올 수 있는 경우): `run_extraction=True` 필요. `retry_reviewed=True`는 `run_extraction=True`가 있어야만 유효.
- **주의**: 이 값들의 정확한 API 파라미터 조합과 `/notices/{notice_key}/analysis/quantitative-diagnostics`(GET, `manual_analysis.py:1065`)의 실제 응답 스키마는 재처리 전 실제로 호출해봐야 하며, 이번 감사에서는 실행하지 않았다(금지 사항).

---

## 12. 사용자가 추가로 정해야 할 정책/데이터

1. **metric 헤더 검증 보강 승인 여부**: 9번 권고안을 채택할지, 다른 방식(예: 헤더 검증 대신 사람 검토 유지)을 택할지.
2. **공동수급 지분율 로직**: 코드에서 명시적 로직을 찾지 못했다 — 실적금액×이행비율 계산식이 실제로 어디서 어떻게 적용되는지 담당자 확인 필요 (제공 안 하면 재구성 금지 원칙, AGENTS.md와 일치).
3. **staleness/재확인 임계값**: (다른 대화에서 다룬 자격요건 REVIEW 이슈와 마찬가지로) 신용등급 A0 등 회사 사실의 최신성 기준을 정책으로 명문화 필요.
4. **W11 "COMPLETED" 재정의 여부**: 정량 성공을 별도로 카운트할지, 현재처럼 파이프라인 반환 성공만 카운트할지 정책 결정 필요.

---

## 13. 수정 전 절대 재가동하면 안 되는 큐 목록

- **W11 Analysis Backfill Queue** (실행 #4562 취소, Unpublished 확인됨 — 사용자 보고 기준, 이번 감사에서 라이브 재확인은 안 함)
- 동일 validator 결함이 있는 상태에서의 모든 backfill/재분석 트리거 — LLM 비용만 소모하고 `criteria count`는 그대로 0일 가능성 높음(3-3, 6절 근거)
- PR #69는 **병합·배포 금지 상태 유지** (metric 헤더 검증 공백이 남아있는 한)

---

### 부록: 확인/미확인 요약

**코드로 명확히 확인됨**: 엔진 버전 1.7.0 일치 / 3중 상태값 계약 일치 / blocker 11개 전부 실존 / 표 검증 단계가 회사 fact 바인딩보다 먼저 막힘 / `recompute_current`가 LLM 0회 호출 계약 / PR#69가 기존 negative test를 뒤집는 방식으로 HWPX 복구를 활성화함 / `_metric_header_matches`류 검증이 PR#69 신규 경로에 미연결됨 / 다중 표·무관 인접행·배점 불일치 방어는 PR#69에 실제로 존재함

**정적 코드 감사로는 확인 불가(가설로 유지)**: 부산 공고 라이브 값의 실제 계산 경로 / W11 COMPLETED 집계가 실제 운영에서 정량 실패를 은폐하는지 / UI 렌더링 세부 경로 / 공동수급 지분율 이중 차감 방지 로직의 실존 여부
