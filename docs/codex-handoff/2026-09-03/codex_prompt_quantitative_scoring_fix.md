# Codex 작업 지시서 — 정량점수 산정 실패 수정

## 선행 문서

이 작업은 `quantitative_scoring_audit_2026-09-03.md` 감사 결과를 근거로 한다.
반드시 먼저 그 문서를 읽고, 이 지시서의 각 항목이 감사의 어느 섹션에 대응하는지
확인한 뒤 작업을 시작할 것. 감사 시점 이후 아래 1개 항목은 재검토를 거쳐
**정정**되었으니 주의: 공동수급 지분율 이중차감 방지 로직은 감사 문서 10절에서
"미확인"으로 남겼으나, 재조사 결과 `quantitative_performance.py`의
`derive_performance_value` 함수(666행경, 실제 분기는 838-845행경)에 이미
구현되어 있음을 확인했다. 아래 Phase 3에서 이 부분은 "신규 구현"이 아니라
"기존 로직 검증"으로 범위를 좁힌다.

## 승인된 방향 (팀 확인 완료)

1. Busan 공고 라이브 값 조회 — **허용됨**. DB/운영 API 읽기 전용 조회 가능.
2. PR #69 — **보강 후 병합**하는 방향으로 확정.
3. 공동수급 지분율 로직 — 이미 존재할 가능성 있음, 없으면 버그로 간주하고 확인.
4. n8n W11 "COMPLETED" 판정 — **정량점수 산정이 실제로 성공한 경우에만** COMPLETED로
   집계하도록 변경.

---

## Phase 1: PR #69 보강 후 병합

### 배경

PR #69(`codex/flat-hwpx-quantitative-repair-20260903`, 커밋 `815d038`)는
HWPX 첨부에서 HWP 섹션 마커 없이 분리된 표 셀을 재결합하는 `_rebind_flat_split_table_cell_literals`
함수를 추가한다. 다중 표 방지, 무관 인접행 방지, 배점 불일치 방지는 이미 잘 구현되어
있음을 diff로 확인했다. 그러나 기존에 존재하는 `_metric_header_matches`(839행),
`_metric_header_is_unique`(880행) 함수를 이 새 경로에서 전혀 호출하지 않는다는
공백을 확인했다 — 이 두 함수는 완전히 다른 경로(`_candidate_metric_max_header_spans`,
3120행에서만 호출)에서만 쓰이고 있다.

### 요구되는 수정

- `_rebind_flat_split_table_cell_literals` 내부에서 각 criterion의 window를
  확정하기 전에, 그 criterion의 `metric` 필드와 실제 window 텍스트가
  `_metric_header_matches(candidate, window)`를 통과하는지 검증하는 단계를 추가한다.
  통과하지 못하면 해당 criterion은 복구하지 않고 원본(REVIEW/INCOMPLETE 상태) 유지.
- 가능하면 `_metric_header_is_unique(candidate, window)`도 함께 확인해, 같은
  metric 키워드가 표 안에 반복 등장하는 경우(예: 서로 다른 두 항목이 우연히 같은
  키워드를 공유하는 경우) 오판을 방지한다.
- **주의**: 이 두 함수는 원래 `CASE_TABLE` + `metric in _METRIC_HEADER_TOKEN_GROUPS`
  전용으로 설계되어 있다(921-942행 `_candidate_metric_max_header_spans`의 가드 조건
  참고). BRACKET 타입 후보에도 동일하게 적용 가능한지, 아니면 BRACKET 전용 검증을
  별도로 만들어야 하는지 먼저 코드로 확인하고 결정할 것 — 억지로 재사용해서
  잘못된 metric에 통과 판정을 내리면 안 된다.

### 테스트 추가 (기존 커버리지 공백 메우기)

PR #69가 이미 추가한 테스트(`test_flat_hwpx_split_cells_are_rebound_from_exact_adjacent_score`,
`test_flat_hwpx_split_cells_do_not_cross_an_unrelated_line`)는 유지한다. 다음을 추가:

1. **다중 표 negative test**: `payload.quantitative_tables`가 2개 이상일 때
   `_rebind_flat_split_table_cell_literals`가 원본을 그대로 반환하는지 확인
   (현재 가드 조건은 있으나 전용 테스트가 diff에 없었음).
2. **중복 metric negative test**: 같은 criterion 리터럴/증거 인용 문구가 소스 내
   두 곳 이상에서 발견될 때 복구를 거부하는지 확인.
3. **generic 헤더 오탐 방지 test**: metric 키워드가 전혀 없는 "정량평가 / 10점"류
   범용 텍스트가 criterion span과 인접해 있을 때, 이번에 추가한 metric 헤더 검증이
   이를 걸러내는지 확인 (이 테스트는 검증 추가 전에는 실패해야 하고, 추가 후 통과해야 함
   — 즉 이 테스트가 이번 수정의 존재 이유를 증명해야 한다).

### 병합 절차

- `QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION`을 `0.6.12`에서 필요시 `0.6.13`으로
  추가 조정(메트릭 검증 추가로 동작이 바뀌므로).
- `AGENTS.md`의 "Git, documentation, and handoff" 절차대로 task branch → PR → CI
  통과 확인 → 병합. `main`에 직접 push 금지.
- 병합 후 기존 통과하던 회귀 테스트(`test_quantitative_rule_extraction.py` 전체)가
  깨지지 않는지 재확인.

---

## Phase 2: 부산교육한마당 라이브 값 검증 (읽기 전용, 팀 승인됨)

### 목적

감사 문서 5절의 미확인 가설("20/20이 실제 엔진 계산 결과인지")을 확정한다.

### 수행 방법

- 해당 공고의 실제 notice_key(운영 DB에서 "2026 부산교육한마당 위탁 용역" 검색해
  확인)에 대해 `GET`/`POST /notices/{notice_key}/analysis/quantitative-diagnostics`
  (`manual_analysis.py:1063-1080`)를 **운영 PIN을 가진 담당자 계정으로, 같은
  origin에서** 호출한다. 이 엔드포인트는 `_same_origin_request`와
  `_require_manual_operator` 검증이 있어(같은 파일 확인) 외부에서 curl로 직접
  호출할 수 없다 — 반드시 웹 UI 또는 같은 origin의 내부 도구로 조회할 것.
- 응답에서 `rule_source_status`, `activation_status`, `total/lower/upper score`,
  각 criterion의 `evidence_key`/`fact_binding_sha256`을 확인해, 실제로 다음이
  성립하는지 본다:
  - `activation_status`가 `AUTO_ACTIVE` 또는 `PARTIAL_ACTIVE`인지 (REVIEW_REQUIRED면
    애초에 20/20이 화면에 나올 수 없는 계약이므로 이 경우 사용자가 본 20/20은
    다른 화면/다른 시점의 값일 가능성을 재확인해야 함)
  - `engine_version`이 현재 `QUANTITATIVE_ENGINE_VERSION`("pai-loop-quantitative-engine-1.7.0",
    `quantitative_scoring.py:70`)과 일치하는지 (버전이 다르면 과거 계산 결과가
    최신 코드로 재검증되지 않은 채 캐시된 것일 수 있음 — `notice_freshness.py`의
    엔진 버전 비교 로직 확인)
- 이 조사는 **읽기만** 한다. 재분석 트리거(`run_extraction`)나 백필 큐 호출은
  이 단계에서 하지 않는다.

### 결과 보고

- 실제 라이브 값이 `test_busan_education_quantitative_e2e.py`의 합성 시나리오와
  동일한 계산 경로(같은 criteria 구조, 같은 fact binding 방식)로 나왔는지 확인해
  보고서에 추가한다.
- 만약 라이브 값이 REVIEW_REQUIRED이거나 engine_version이 오래된 것이면, 그 자체가
  또 하나의 실패 사례이므로 별도로 기록한다.

---

## Phase 3: 공동수급 지분율 이중차감 로직 — 검증 (신규 구현 아님)

### 확인된 사실

`quantitative_performance.py`의 실적 인정금액 산정 로직(약 780-870행,
`derive_performance_value` 계열 함수 내부)에 이미 다음 분기가 존재한다:

```python
if certificate_amount is not None and certificate_amount_is_net:
    # Certificate-backed 실적금액 is already attributable to the company.
    # Applying share_pct again would double deduct consortium participation.
    recognized_amount = Decimal(certificate_amount)
else:
    ...
    recognized_amount *= Decimal(str(share)) / Decimal("100")
```

이는 정확히 요구된 로직(이행비율 100% 미만 공동수급 실적에서 실적금액과 이행비율을
중복 차감하지 않음)과 일치한다. `share_pct == 0`인 행은 별도로 스킵되고
(제로 지분 행 처리), `scope.consortium_share_rule == "UNSPECIFIED"`이면서
지분율이 100% 미만인 경우는 `excluded_uncertain`으로 빠져 추측 계산을 하지 않는다
(fail-closed 원칙 준수, AGENTS.md와 일치).

### 요구되는 작업

이 로직을 **새로 만들지 말고**, 다음만 확인한다:

1. 이 함수(`_eligible_record_keys` 587행 또는 `derive_performance_value` 666행 —
   정확한 소속 함수를 재확인할 것)에 대한 **전용 단위 테스트**가
   `tests/test_quantitative_performance*.py` 또는 관련 테스트 파일에 존재하는지
   확인. 다음 3가지 케이스가 커버되어야 한다:
   - `certificate_amount_is_net=True`인 경우 지분율이 재적용되지 않고 인증금액
     그대로 쓰이는 케이스
   - `certificate_amount_is_net`가 없거나 False인 경우 원 계약금액에 지분율이
     정확히 곱해지는 케이스
   - `share_pct=0`인 행이 카운트/합계에서 제외되는 케이스
2. 위 테스트가 없다면 추가한다. 있다면 실제로 통과하는지 실행해서 확인만 하고
   보고한다.
3. 이 로직이 실제로 정량점수 계산 파이프라인(`estimate_quantitative_score`)까지
   제대로 연결되어 있는지 호출 경로를 추적해 확인한다 — 로직 자체는 맞아도 호출이
   안 되고 있으면 의미가 없다.
4. 만약 실행 결과 실제로 버그(예: `certificate_amount_is_net`가 데이터베이스에
   한 번도 `True`로 저장된 적이 없어서 이 분기가 사실상 죽은 코드인 경우 등)를
   발견하면, 그 지점만 최소 범위로 수정한다. 로직 재작성 금지 — 이미 맞게
   설계되어 있다.

---

## Phase 4: n8n W11 완료 판정을 정량 성공 기준으로 변경

### 배경

현재 `workflows/pai-loop-11-analysis-backfill.json`의 완료 판정은 분석 API 호출
자체의 구조적 성공(`requested`/`processed`/`completed`/`skipped`/`failed` 카운트,
JS 검증 코드 확인됨)만 검사하며, `activation_status`가 실제로 `AUTO_ACTIVE`/
`PARTIAL_ACTIVE`인지는 검사하지 않는 것으로 보인다(코드상 정황적 근거, n8n 실행
로그 직접 확인은 아님).

### 요구되는 변경

- `analysis_pipeline.py`가 분석 결과를 반환할 때(2107-2113행 부근,
  `quantitative_engine`/`rule_source_status`/`activation_status`가 이미 포함되어
  있음을 확인했음), 이 값들을 n8n 워크플로 응답 스키마에도 명시적으로 포함시킨다
  (현재 포함되어 있는지 먼저 확인 — `workflows/pai-loop-11-analysis-backfill.json`의
  응답 검증 JS 코드를 실제로 열어서 quantitative 관련 필드를 검사하는지 확인할 것).
- 워크플로의 "COMPLETED" 카운트 로직을 다음과 같이 나눈다:
  - `analysis_completed`: 기존 의미 그대로(분석 API 호출 자체가 성공) — 이름을
    명확히 구분해 혼동 방지.
  - `quantitative_scored`(신규): `activation_status in {"AUTO_ACTIVE","PARTIAL_ACTIVE"}`
    이고 `total/lower/upper score`가 null이 아닌 경우만 카운트.
- 운영 리포트/일일 브리핑에 두 카운트를 **분리해서** 노출한다. 기존
  `docs/DAILY_BRIEFING_RUNBOOK_v0.7.0.md`에 이 변경을 반영해 문서를 갱신한다
  (AGENTS.md "동일 PR에서 관련 문서 갱신" 원칙).
- **주의**: `AGENTS.md`에 따라 `manifest.json`의 `publish: true` 워크플로만 활성화
  대상이며, 이 변경 자체가 워크플로를 재실행할 권한을 주는 것은 아니다. 코드/JSON
  변경만 하고 실제 n8n 재배포·재실행은 사용자의 별도 지시가 있을 때까지 하지 않는다.

---

## 공통 요구사항

- 각 Phase는 별도 브랜치/PR로 분리한다: Phase 1(PR#69 보강), Phase 2는 코드 변경이
  아니므로 PR 없이 조사 보고서만 제출, Phase 3(테스트 추가/버그 수정, 있다면),
  Phase 4(워크플로 변경).
- 모든 변경 후 `python -m pytest --cov=pai_loop --cov-report=term-missing --cov-fail-under=85`
  통과 확인.
- workflows/manifest.json 변경 시 `node scripts/deploy-workflows.mjs --validate-only`
  등 AGENTS.md에 명시된 검증 스크립트 전부 실행.
- 최종 handoff에 변경 파일, 테스트 결과, PR 링크, 그리고 **Phase 2에서 확인한
  부산 공고의 실제 라이브 값**을 반드시 포함해 보고할 것.
