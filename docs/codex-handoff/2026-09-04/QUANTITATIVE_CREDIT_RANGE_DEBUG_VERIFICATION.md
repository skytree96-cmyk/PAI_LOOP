# 정량점수·신용등급 범위 디버깅 및 운영 검증 — 2026-09-04

## 결론

정량점수가 사라진 핵심 원인은 “정량적 평가 세부기준”이라는 제목을 못 찾은 문제가 아니었다. 공고 표의 `A- 이상`, `BBB- 이상 A- 미만` 같은 **문자 등급 범위**를 기존 `CASE_TABLE`의 원문 정확 일치와 계산 단계의 정확 멤버십이 동시에 처리할 수 없는 구조적 Catch-22였다. 이제 각 공고에서 추출한 원문 표를 그 공고에만 적용되는 실행 가능한 산식으로 컴파일하고, 회사 사실을 그 산식에 대입한다.

코드·테스트·배포는 완료했다. 다만 프롬프트 버전 변경으로 기존 운영 분석은 현재값에서 제외됐고, 승인된 4자리 운영 PIN을 확인하지 못해 두 대표 공고의 실제 재분석 요청은 시작하지 않았다. 따라서 현재 운영 화면은 의도대로 `미분석 · 미산정`이며, 부산 공고의 최신 운영 `20/20` 표기는 아직 미검증이다.

## 1. 재현된 원인

1. HWPX의 셀 분리 복구만으로 숫자 실적 구간은 살아났지만 신용등급 범위는 계속 REVIEW가 됐다.
2. LLM이 `A- 이상`을 `A-, A0, A+ …`로 펼치면 원문에 각 값이 없어서 `CASE_CATEGORY_MISMATCH`가 발생했다.
3. 반대로 `A- 이상`을 하나의 category 값으로 두면 검증은 통과해도 회사 값 `A0`와 정확 일치하지 않아 점수를 계산하지 못했다.
4. 같은 표의 기준 하나가 실패하면 표 전체가 차단되어 정상인 실적 기준까지 스코어링에 도달하지 못했다.
5. 부산 표는 등급이 열거되어 있고 정확한 행 배열·각주를 확인하는 전용 geometry guard가 있어 기존 경로에서만 우연히 통과했다. 점수를 하드코딩한 것은 아니지만 일반 공고로 확장 가능한 구조는 아니었다.
6. 코드 수정 뒤에도 프롬프트·스키마·validator 버전이 다른 과거 분석을 현재값으로 재사용하면 화면에 잘못된 점수가 남을 수 있었다.

첨부된 Claude 문서는 원인 확인 자료로 사용했고, 최종 판단은 현재 코드 실행과 회귀 테스트로 다시 검증했다.

## 2. 적용한 해결책

### 공고별 실행 가능한 산식

- 모든 공고에 한 문구를 최우선 규칙으로 강제하지 않는다.
- 각 공고의 첨부 표에서 기준, 단위, 배점, 구간, 원문 인용, 페이지·첨부 식별자를 추출한다.
- 신용등급은 공통 정규화 서열을 사용하되 **배점 구간은 해당 공고 원문에서만** 생성한다.
- `이상/초과/이하/미만`, 한 줄·분리 줄, Unicode 대시, `A`→`A0` 입력을 결정론적으로 처리한다.
- 컴파일된 구간은 겹침·빈틈·알 수 없는 등급·모순·중복 매칭이 있으면 점수를 추정하지 않고 REVIEW로 닫는다.
- 회사 실적·인력·신용평가 사실은 공고별 산식의 입력값일 뿐이며, 공고와 무관한 전역 점수표를 사용하지 않는다.

### 추출 계약

- `CREDIT_RATING` 후보는 기업어음/회사채 열이 아니라 기업신용평가등급 열과 그 배점을 사용한다.
- 한국어 범위 문구를 재작성하지 않고 원문 그대로 보존하도록 프롬프트·스키마 계약을 보강했다.
- 원문에 없는 등급이나 표 구조를 모델이 보충하면 활성화하지 않는다.

### 현재값 freshness

- PPS 첨부 manifest가 있는 공고는 prompt/schema/processing/validator 버전까지 모두 현재여야 최신 분석으로 선택한다.
- 버전이 오래된 `Evaluation`과 `AnalysisRun`은 감사 이력으로 보존하지만 목록·상세의 현재 점수, 참가자격, AI 판단에는 투영하지 않는다.
- 프런트도 API가 PENDING/REVIEW/COLLECTED/VERSIONED일 때 오래된 scalar fallback을 사용하지 않는다.

## 3. 대표 공고 기대값과 현재값

### 2026 부산교육한마당 위탁 용역

- 공고 키: `PPS-R26BK01703600-000-8f0e732a38`
- 산식 입력:
  - 단일 교육·행사 실적 2억원 이상 → 6/6
  - 최근 3년 0.2억원 이상 완료 실적 5건 이상 → 4/4
  - 회사 기업신용평가등급 A0 → 배점의 100% → 10/10
- 결정론적 인수 목표: **정량 20/20**
- 별도 판단 축의 기대값: 참가자격 미충족, AI 판단 NO-GO
- 현재 운영 Chrome 표시: `미분석 · 미산정 · AI 판단 분석 전`

### 학교 역사교육 활성화를 위한 역사 바로알기 홍보 영상 제작_PA202601820

- 공고 키: `PPS-R26BK01704704-000-d3a27e912c`
- 목적: 열거형이 아닌 신용등급 범위 표의 범용 경로 확인
- 현재 운영 Chrome 표시: `미분석 · 미산정 · AI 판단 분석 전`
- 인수 조건: 재추출 뒤 `AUTO_ACTIVE`, 불가피한 경우 적어도 검증된 기준의 `PARTIAL_ACTIVE`; REVIEW면 diagnostics의 후보 형태와 이슈 코드를 다시 확인한다.

## 4. 테스트 결과

| 검증 | 결과 | 증빙 |
|---|---|---|
| 신용등급 범위 컴파일·경계 포함/제외 | Pass | `tests/test_quantitative_case_table.py` |
| 알 수 없는 등급·중복·겹침·빈틈·누락 fail-closed | Pass | 같은 테스트의 negative matrix |
| A/A0·Unicode 대시 정규화 | Pass | case-table·private evidence 회귀 |
| 부산 금액 6 + 건수 4 + A0 10 | Pass | `tests/test_busan_education_quantitative_e2e.py` |
| 추출 원문 범위 보존 | Pass | `tests/test_openai_extraction.py`, `tests/test_quantitative_rule_extraction.py` |
| 프롬프트 stale 현재 투영 차단 | Pass | `tests/test_api.py`, `tests/test_frontend_public_contract.py` |
| 전체 저장소 테스트 | **1056 passed** | 최종 병합 코드 로컬 전체 suite |
| GitHub 필수 CI | Pass | PR #73~#76 |

버전:

- 추출 프롬프트: `pai-loop-extraction-0.5.1`
- 정량 엔진: `pai-loop-quantitative-engine-1.7.1`
- 첨부 validator: `pai-loop-quantitative-attachment-validator-0.6.13`

## 5. 병합·배포 증빙

- [PR #73 — UI/UX P0·전체 화면 상세](https://github.com/skytree96-cmyk/PAI_LOOP/pull/73)
- [PR #74 — 신용등급 범위 컴파일러](https://github.com/skytree96-cmyk/PAI_LOOP/pull/74)
- [PR #75 — 추출 원문 범위 보존 계약](https://github.com/skytree96-cmyk/PAI_LOOP/pull/75)
- [PR #76 — 오래된 분석의 현재 투영 차단](https://github.com/skytree96-cmyk/PAI_LOOP/pull/76)
- Render 서비스: `pai-loop-demo`
- 수동 배포 ID: `dep-dactaju7bikc73ffd9v0`
- 배포 소스: `0dcc6c150ef5afe9cd2d32caae9f10faa0fbfb01`
- 결과: `Deploy succeeded · Live`, 1분 15초
- 운영 정적 자산: `styles.css?v=20260904-uiux-p0-v2`, `app.js?v=20260904-uiux-p0-v2`

프롬프트 변경 전에는 대표 공고 2건의 attachment manifest/current audit/accepted가 `2/2/2`였다. 변경 후 manifest는 `2`로 보존되고 current audit/accepted는 `0/0`이 되어 `2/0/0`으로 전환됐다. PR #76 배포 전에는 이 상황에서도 과거 61/100·NO-GO가 남았으나, 배포 후 실제 Chrome에서 `미분석 · 미산정 · 분석 전`으로 바뀐 것을 확인했다.

## 6. 운영 재분석 절차와 미완료 사유

승인된 단건 경로는 다음 하나다.

```text
POST /api/v1/notices/{notice_key}/analysis/request
X-PAI-Manual-Token: <4자리 운영 PIN>
{"run_extraction": true}
```

이번 변경은 프롬프트 버전 상승을 포함하므로 `recompute_current:true`만으로는 처리할 수 없고 첨부 재추출이 필요하다. 비용·중복 실행을 막기 위해 부산 공고 한 건을 먼저 실행하고 완료·20/20을 확인한 뒤 역사영상 공고를 순차 실행해야 한다.

로컬 `PAI_LOOP_PUBLIC_MANUAL_ANALYSIS_TOKEN`은 4자리 PIN 형식이 아니었다. 다른 비밀값을 운영 PIN으로 추정해 전송하는 행위는 승인되지 않은 자격증명 우회이므로 중단했다. 분석 요청·provider 호출은 **0건**이며, PC도 종료하지 않았다.

## 7. 남은 인수 체크리스트

| 항목 | Yes/No | 다음 행동 |
|---|---|---|
| 범위형 신용등급 일반화 코드 | Yes | 병합·배포 완료 |
| 부산 결정론적 20/20 회귀 | Yes | 전체 suite 통과 |
| 오래된 평가 현재 화면 차단 | Yes | 실제 Chrome 확인 |
| 부산 실제 운영 재분석 20/20 | No | 승인된 4자리 PIN으로 `run_extraction:true` 실행 |
| 역사영상 공고 `AUTO_ACTIVE` 확인 | No | 부산 성공 뒤 순차 실행 |
| 범위형/열거형 운영 전체 분포 집계 | No | 운영 diagnostics 권한 범위에서 별도 집계 |
| 부산 전용 geometry guard 제거 | No | native 표 행/열 geometry 보존 전에는 오탐 방지를 위해 유지 |
| 기준 단위 `PARTIAL_ACTIVE` 완화 | No | 표 전체 신뢰를 낮출 수 있어 별도 설계·PR 필요 |
| 완료 메일·PC 종료 | No | 두 공고의 실제 점수 표기 확인 전 실행 금지 |

최종 완료 조건은 부산 운영 화면에서 근거별 `6/6 + 4/4 + 10/10 = 20/20`이 보이고, 참가자격·AI 판단이 별도 축으로 표시되며, 역사영상 공고에서도 범위형 표가 활성화되는 것이다.
