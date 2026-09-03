# Codex 작업 지시서 — 정량점수 산정 실패 수정

## 선행 문서

이 작업은 `quantitative_scoring_audit_2026-09-03.md` 감사 결과를 근거로 한다.
반드시 먼저 그 문서를 읽고, 이 지시서의 각 항목이 감사의 어느 섹션에 대응하는지
확인한 뒤 작업을 시작할 것.

**이 프롬프트는 2026-09-03에 실제 코드/브랜치를 다시 열어 전면 재검증했다
(자격요건 프롬프트에서 수치 오류가 나왔던 전례가 있어 동일한 방식으로 재확인함).
아래 2개 항목이 이번 재검증에서 정정되었다:**

1. **공동수급 지분율 이중차감 방지 로직 — 정정 폭이 이전 정정보다 더 크다.**
   감사 문서 10절은 "미확인"으로, 이전 버전 프롬프트는 "로직은 있는데 테스트가
   없을 수 있다"로 적었으나, **재확인 결과 전용 단위 테스트 4개가 이미 존재하고
   전부 통과한다**(`tests/test_quantitative_performance_generalized.py`:
   `test_certificate_recognized_amount_is_not_share_adjusted_twice`,
   `test_certificate_gross_basis_amount_applies_share_to_certificate_amount`,
   `test_legacy_gross_amount_still_applies_share_once`,
   `test_certificate_amount_without_explicit_share_basis_fails_closed` —
   `pytest -k "certificate or legacy_gross"` 4 passed 직접 확인함). 아래 Phase 3은
   이제 "신규 구현"도 "테스트 보강"도 아니라 **"파이프라인 연결 여부 확인"** 하나로
   범위가 더 좁아졌다. 정확한 코드 위치도 838-845행이 아니라 **846-862행**이다
   (재확인하며 정확한 줄번호로 교정).
2. **PR #69의 `_metric_header_matches`/`_metric_header_is_unique` 줄번호가
   main 기준이었다.** PR #69 브랜치(`codex/flat-hwpx-quantitative-repair-20260903`)는
   그 앞에 다른 코드가 추가되어 있어 같은 함수들의 실제 줄번호가 main과 다르다
   (main 839/880/921행 → **PR#69 브랜치에서는 868/909/950행**,
   `_rebind_flat_split_table_cell_literals`는 **2864행**). Codex가 실제로 작업할
   브랜치는 PR#69이므로 이 문서의 Phase 1은 PR#69 브랜치 기준 줄번호로 정정했다.

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
HWPX 첨부에서 HWP 섹션 마커 없이 분리된 표 셀을 재결합하는
`_rebind_flat_split_table_cell_literals`(**이 브랜치에서 2864행**) 함수를 추가한다.
다중 표 방지, 무관 인접행 방지, 배점 불일치 방지는 이미 잘 구현되어 있음을 diff로
확인했다. 그러나 기존에 존재하는 `_metric_header_matches`(**이 브랜치에서 868행**,
main에서는 839행 — 브랜치마다 앞부분 삽입으로 줄번호가 다르니 반드시 실제 작업
브랜치에서 grep으로 재확인할 것), `_metric_header_is_unique`(**이 브랜치에서
909행**) 함수를 이 새 경로에서 전혀 호출하지 않는다는 공백을 확인했다 — 이 두
함수는 완전히 다른 경로(`_candidate_metric_max_header_spans`, **이 브랜치에서
950행**에서만 호출)에서만 쓰이고 있다.

### 요구되는 수정

- `_rebind_flat_split_table_cell_literals` 내부에서 각 criterion의 window를
  확정하기 전에, 그 criterion의 `metric` 필드와 실제 window 텍스트가
  `_metric_header_matches(candidate, window)`를 통과하는지 검증하는 단계를 추가한다.
  통과하지 못하면 해당 criterion은 복구하지 않고 원본(REVIEW/INCOMPLETE 상태) 유지.
- 가능하면 `_metric_header_is_unique(candidate, window)`도 함께 확인해, 같은
  metric 키워드가 표 안에 반복 등장하는 경우(예: 서로 다른 두 항목이 우연히 같은
  키워드를 공유하는 경우) 오판을 방지한다.
- **주의**: 이 두 함수는 원래 `CASE_TABLE` + `metric in _METRIC_HEADER_TOKEN_GROUPS`
  전용으로 설계되어 있다(이 브랜치 기준 950행 부근 `_candidate_metric_max_header_spans`의
  가드 조건 참고). BRACKET 타입 후보에도 동일하게 적용 가능한지, 아니면 BRACKET
  전용 검증을 별도로 만들어야 하는지 먼저 코드로 확인하고 결정할 것 — 억지로
  재사용해서 잘못된 metric에 통과 판정을 내리면 안 된다.

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

## Phase 3: 공동수급 지분율 이중차감 로직 — 파이프라인 연결 확인만 (구현·테스트 둘 다 이미 완료됨)

### 확인된 사실 (2026-09-03 재검증, 실행 결과 포함)

`quantitative_performance.py`의 `derive_performance_value` 함수(666행) 내부,
**846-862행**에 다음 분기가 이미 존재한다:

```python
elif scope.consortium_share_rule == "APPLY_SHARE":
    if certificate_amount is not None and certificate_amount_is_net:
        # Certificate-backed 실적금액 is already attributable to
        # the company. Applying share_pct again would double
        # deduct consortium participation.
        recognized_amount = Decimal(certificate_amount)
    else:
        pre_share_amount = (
            certificate_amount if certificate_amount is not None else amount
        )
        ...
        recognized_amount = Decimal(pre_share_amount or 0)
        recognized_amount *= Decimal(str(share)) / Decimal("100")
```

**이전 버전 프롬프트는 "테스트가 있는지 확인해서 없으면 추가하라"고 했으나, 이번
재검증에서 전용 단위 테스트 4개가 이미 존재하고 전부 통과함을 직접 실행해
확인했다** (`tests/test_quantitative_performance_generalized.py`):

| 테스트 | 검증 케이스 | 결과 |
|---|---|---|
| `test_certificate_recognized_amount_is_not_share_adjusted_twice` | `recognized_amount_is_net_of_share=True` → 지분율 재적용 없이 인증금액(2억) 그대로 사용 | PASS |
| `test_certificate_gross_basis_amount_applies_share_to_certificate_amount` | `is_net_of_share=False` → 인증금액(3억)에 지분율(50%) 적용해 1.5억 | PASS |
| `test_legacy_gross_amount_still_applies_share_once` | 인증금액 없이 legacy 계약금액(4억)에 지분율 1회만 적용해 2억 | PASS |
| `test_certificate_amount_without_explicit_share_basis_fails_closed` | `is_net_of_share=None`(불명) → 추측 계산하지 않고 REVIEW로 fail-closed | PASS |

```
$ pytest tests/test_quantitative_performance_generalized.py -k "certificate or legacy_gross" -q
....                                                                     [100%]
```

**즉 이 항목은 "로직 없음"도 "테스트 없음"도 아니고, 유닛 레벨에서는 정확하게
구현·검증까지 끝나 있다.** 사용자가 우려했던 "코덱스가 만들어놨다는데 오류로
없을 수도 있다"는 우려는 **기우였음이 코드 실행으로 확인됐다.**

### 요구되는 작업 (범위를 유닛 로직 검증에서 파이프라인 통합 확인으로 좁힘)

**새 로직도, 새 테스트도 만들지 않는다.** 딱 하나만 확인한다:

1. `derive_performance_value`의 반환값이 실제로 `estimate_quantitative_score`
   (`quantitative_scoring.py`)까지 호출 경로가 이어져 있는지, 그리고 그 결과가
   최종 `QuantitativeFact`/점수 계산에 실제로 쓰이는지 호출 그래프를 추적한다.
   유닛 테스트가 통과한다고 해서 실제 분석 파이프라인(`analysis_pipeline.py` →
   `quantitative_scoring.py` → `quantitative_performance.py`)에서 이 함수가
   호출되고 있다는 보장은 아니므로, 이 연결 하나만 확인하면 Phase 3은 끝난다.
2. 연결이 확인되면 **PR도, 코드 변경도 필요 없다.** "확인 완료"로 보고만 하고
   종료한다. 만약 연결이 끊겨 있다면(예: 다른 함수가 이 함수를 우회해서
   `contract_amount_krw`를 직접 쓰는 경우 등) 그 연결 지점만 최소 범위로 고친다.

---

## Phase 4: n8n W11 완료 판정을 정량 성공 기준으로 변경

### 배경 (2026-09-03 재검증: 추정이 아니라 코드 전문으로 확정)

`workflows/pai-loop-11-analysis-backfill.json`의 `Validate Chunk Result` 노드의
JS 코드 전문을 직접 열어 확인했다. 이 코드는 `status`(COMPLETED/PARTIAL),
`requested`/`processed`/`completed`/`skipped`/`failed`/`openai_calls` 카운트,
`results` 배열 길이, `enrichment.attachments_*` 첨부 감사 수치만 검사하며,
**`quantitative`/`activation_status`/`rule_source_status`/score 관련 키는
단 한 번도 참조하지 않는다.**

이 워크플로가 호출하는 엔드포인트(`POST /api/v1/notices/analysis/batch`,
`analysis_api.py:2797`)의 응답 아이템 스키마 `AnalysisBatchItemOut`
(`analysis_api.py:136-172`)도 직접 확인했다 — `status`, `document_status`,
`evaluation_status`, `snapshot_status`, `score_snapshots`(단순 저장된 스냅샷
**개수**), `analysis_state`, `analysis_reason_code` 등은 있지만, **정량
`activation_status`나 최종 점수 null 여부를 나타내는 필드가 스키마 자체에
없다.** `score_snapshots`는 스냅샷이 몇 건 "저장"됐는지의 카운트일 뿐, 그
스냅샷의 `activation_status`가 `REVIEW_REQUIRED`이고 점수가 null이어도 저장은
되므로 이 카운트만으로는 정량 성공 여부를 알 수 없다.

**즉 이전 버전 프롬프트의 "검사하지 않는 것으로 보인다(정황적)"는 표현은
이제 확정 사실이다: n8n도, 그 워크플로가 호출하는 API 응답 스키마도 정량
성공 여부를 애초에 알 수 있는 구조가 아니다.** 이건 워크플로 JSON만 고쳐서
될 일이 아니라 **API 응답 스키마 확장이 선행되어야 한다.**

### 요구되는 변경

- **1단계(선행, 필수)**: `AnalysisBatchItemOut`에 정량 상태를 나타내는 필드를
  추가한다(예: `quantitative_activation_status: str | None`,
  `quantitative_score_available: bool`). `analysis_pipeline.py`가 이미
  내부적으로 `rule_source_status`/`activation_status`를 계산하고 있으므로
  (2107-2113행 부근, 이번 재검증에서 정확한 변수명까지는 다시 확인하지
  않았으니 Codex가 실제 반환 지점을 재확인할 것), 이를 API 응답 조립 지점까지
  끌어올리기만 하면 된다. 이 스키마 필드가 없으면 아래 워크플로 JSON 변경은
  아예 검사할 대상이 없어 무의미하다.
- **2단계**: 워크플로 JSON의 `Validate Chunk Result`/`Aggregate Backfill
  Progress` 노드가 위 신규 필드를 읽어 실제로 카운트하도록 수정한다.
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
