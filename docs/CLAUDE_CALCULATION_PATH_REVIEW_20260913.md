# Claude 검토: 정량점수 계산까지 가는 최소 경로 (2026-09-13)

코드 기준 `d817ce5`. 읽기 전용 검토이며 코드 수정·push·유료 호출·운영 접근은 없다.
근거 표기: **코드 확인** / **전달받은 측정**(Codex `CODEX_COVERAGE_VERIFICATION_RESULT_20260913.md`) /
**이전 보고 인용**(`QUANTITATIVE_SCORE_INVESTIGATION_20260911.md`) / **확인 불가**.
사용자가 직접 계산한 사례의 원문·기대값과 회사 자료는 받지 못했다.

---

## 0. 결론

"계산기 먼저 vs 입력 구조 먼저"는 잘못된 이분법이다. 병목은 **규칙의 신원(binding)이
모델 추출 결과와 manifest 전체 커버리지에 묶여 있는 구조**다.

1. 계산기는 완성도가 높다. 신용등급·실적·재무·인력을 회사 사실과 결합해
   확정/추정/보류를 내는 경로가 있고 합성 입력으로 끝까지 도달한다
   (`tests/test_quantitative_replay_cli.py` 첫 테스트: 신용등급 A0 → 9점 확정). **코드 확인**
2. 규칙을 계산기에 넣는 입구가 둘 다 모델 커버리지에 잠겨 있다.
   - 동적 경로: manifest 전 첨부의 현행 계약 기록 필요 (`quantitative_scoring.py:3667, 3806`).
   - **사람이 만든 curated 프로필 경로도** `_profile_for_notice` →
     `_current_authoritative_document_state`가 전 첨부의 유효 정량 기록을 요구한다
     (`quantitative_scoring.py:4225-4239`, 기본 `validate_accepted=True` reader 사용). **코드 확인**
3. 커버리지가 완전해져도 계산 도달이 보장되지 않는다. 활성 판정이 공고 단위
   all-or-nothing이라 실적 행 하나의 인정범위가 파서에 안 잡히면
   (`FACT_DIMENSIONS_UNMODELED`, `quantitative_scoring.py:3711-3729`) 신용등급 행까지
   `REVIEW_REQUIRED`가 된다(3949-3960). 이전 보고의 "PERFORMANCE_COUNT 179항목이 여기서
   막힌다"가 이 지점이다. **코드 확인 + 이전 보고 인용**
4. 회사 사실 결합 hash는 **추출 후보 내용 + 문서 digest**로 계산된다
   (`_candidate_fact_binding_sha256`, 2540행). 재추출로 후보 문구가 바뀌면 등록한
   신용등급 증빙이 풀린다. 재추출은 계산을 여는 열쇠이자 회사 입력을 무효화하는 행위다. **코드 확인**

따라서 주장 1(기존 입력으로 계산 완성)은 지금 표본에서 0건이고, 주장 2(입력 구조부터)는
"추출 구조"가 아니라 "규칙 입구 + 활성 판정 구조"로 읽어야 맞다.

---

## 1. 계산이 멈추는 지점 A·B·C

| 계층 | 멈추는 지점 | 근거 | 성격 |
|---|---|---|---|
| A 원문 규칙 미확보 | manifest 첨부 중 현행 계약 기록 없음 → 프로필 INCOMPLETE | 첫 차단 커버리지 29/30, CURRENT 헤더 0첨부 (전달받은 측정) | 표본의 지배 원인 |
| A | 구계약 raw 67첨부를 현행 검증기에 넣어도 AVAILABLE 0, REVIEW 후보 70 | 전달받은 측정 | 원문 무표 / 추출 오류 / 검증기 과민 중 어느 것인지 원문 없이 구분 불가 |
| A/B 경계 | 표 총점이 기술평가 전체에 묶이면 정량 행 전부 강등 | `quantitative_rule_extraction.py:7523, 7824` 합성 확인 | 원문은 맞는데 추출·검증이 떨어뜨림 |
| B 표현·검증·계산 불가 | 실적 행 인정범위가 "최근 N년 + 유사범위 + 완료 문구 + 기준일"을 모두 명시하지 않으면 scope None → 공고 전체 REVIEW_REQUIRED | `quantitative_performance.py:362-405`, `quantitative_scoring.py:3711-3729, 3949` | 행 단위 UNSCORABLE이어야 할 실패를 공고 단위로 올림 |
| B | 단위·DSL·fact key 등 행 단위 사유도 공고 전체 차단 | `_profile_activation_reasons` 3730-3784 | 위와 같은 구조 |
| B | 부분 활성은 표 1개·REVIEW 프로필일 때만 | 3796, 3806 합성 확인 | 2단계 평가 공고 부분 활성 불가 |
| B | 신용등급 증빙 등록은 공고당 신용 항목이 정확히 1개일 때만 | `private_company_evidence.py:150-183` | 단계별 반복 항목이면 422 |
| B | 재무비율은 자기자본비율·유동비율·부채비율 3종, 결과는 항상 ESTIMATED | `quantitative_financial.py:24-27, 69-72` | 행 단위 UNSCORABLE. 공고를 막지는 않음 |
| B | 1.8.2 OUT_OF_SCOPE 분리가 동적 경로에서 미도달 | 지난 검토 부록 A, 전달받은 재현 | 표시 계층 |
| C 회사 입력·증빙 | 신용등급은 공고별 binding hash로 재등록. 재추출 시 hash 변경 | `_candidate_fact_binding_sha256`, `_canonical_company_fact_value` 2094-2101 | 운영 절차 부담 |
| C | 실적은 PRIVATE_IMPORT의 VALIDATED 행만, 유사범위는 record.keywords 문자열 포함 판정 | `derive_performance_value` 723-860, `_record_service_matches` | 명부 keyword 품질 = 인정 건수 품질 |
| C | 재무제표·인력명부 fact 존재 여부 | `company.financial.statement`, `company.personnel.roster` | 운영 DB 미확인, 확인 불가 |

커버리지(A)가 지배적이어도 B·C를 미룰 수 없는 이유: 92첨부 재추출이 모두 성공해도
실적 행이 있는 공고는 B의 첫 두 줄에서 다시 멈춘다.

---

## 2. 사람의 계산 과정 vs 코드 (합성 입력 기준)

| 단계 | 신용등급 | 재무비율 | 조건부 실적 건수 |
|---|---|---|---|
| 원문 조건 → 규칙 | CASE_TABLE, 기업신용등급 열만 인정(`_bind_enterprise_credit_column`). "여러 수단 중 높은 등급" 규칙은 표현 불가 → REVIEW | FINANCIAL_RATIO + `parse_financial_recognition_scope`. 비율명 명시 필수, 기준비율 지원 | CASE_TABLE/BRACKET + `parse_performance_recognition_scope`. 기간·유사범위·완료·기준일·건당 최소금액·VAT·지분 규칙 구조화. 참여인원·연간금액 조건은 수동 전용 |
| 회사 사실 선택 | 공고별 binding으로 등록된 CompanyFact 1건(`register_private_credit_rating_for_notice`) | 운영자 재무제표 fact에서 연도 선택·비율 계산 | VALIDATED 실적 중 keyword ALL/ANY 일치, 기준일 창 안, 완료, 증빙참조 있음, VAT 확정, 건당 최소금액 충족만 집계. 불확실 행은 `excluded_uncertain`으로 분리 |
| "몇 건" vs "조건 충족 몇 건" | 해당 없음 | 해당 없음 | 코드는 후자만 센다. 불확실 행이 있으면 하한만 남기고 REVIEW, 하한이 최상위 구간이면 만점 확정 |
| 항목 점수 | `case_table_points` CREDIT_RATING | `_points_for_value` | `case_table_points` DISCRETE 또는 구간 |
| 정량 소계 | `estimate_quantitative_score` 1406-1470: confirmed / lower / upper / unscorable | 동일 | 동일 |
| 상태 | CONFIRMED 가능 | ESTIMATED까지 | 등록부 기반은 ESTIMATED까지, 항목별 확인 증빙이 있어야 CONFIRMED |

정성·가격은 이 경로에 들어오지 않는다(프롬프트가 판단형 행 제외, 들어와도 UNKNOWN metric → review 행).

**보수 계산·표시 방안.** 엔진은 이미 네 상태를 구분한다: 원문 규칙 미확정(review_criteria, 0~만점),
회사 증빙 없음(UNSCORABLE, 0~만점), 추정(ESTIMATED), 확정(CONFIRMED). 그러나 합계에서
`unscorable_points`가 앞의 둘을 합친다(1416-1422행). 제안:
- 합계 필드를 `rule_unresolved_points`와 `evidence_missing_points`로 분리.
- 실적 행에 "인정 조건 충족 n건 / 등록 N건 / 미확인 제외 k건"을 구조 필드로 노출.
- 이렇게 하면 "확인 안 된 실적은 세지 않는다"를 지키면서 0점 확정으로 읽히지 않는다.

---

## 3. 세 방안 비교

| 항목 | 방안 1: 기존 추출·계산기 연결 보완 | 방안 2: 사람 확인 규칙을 별도 입력으로 | 방안 3: 추출·규칙 구조 개정 |
|---|---|---|---|
| 핵심 수정 | 행 단위 활성 사유를 공고 차단이 아니라 review 행으로 강등, 다표 부분 활성, 신용 항목 1개 제한 완화 | DB 기반 `검증 규칙` 레코드: notice_key, 사람이 읽은 첨부 id·document_sha256, manifest_sha256, 검토한 첨부 목록, QuantitativeCriterion 형태 항목, 실적 scope 명시 필드. `estimate_for_notice`에서 curated보다 앞, 동적 AUTO/PARTIAL 없을 때 사용 | 부모 범위·단계·가중치·수단별 최대값 규칙을 계약에 추가. 새 prompt/schema 세대 |
| 원래 raw·구계약 | 무변경 | 무변경. binding hash는 검증 규칙 + document_sha256로 계산 → 신용등급 등록 API 그대로 동작 | 모든 기존 기록이 구세대 |
| 장점 | 커버리지 완전 공고가 생기면 즉시 효과. 재추출 낭비 감소 | 우선 공고에서 지금 계산 도달. 추출기 정확도를 잴 정답 세트 확보 | 장기적으로 사람 입력 감소 |
| 남는 한계 | 커버리지 차단 공고 0건. 틀린 추출은 못 고침 | 공고당 사람 작업. 인용 자동 검증은 재다운로드 시점에만 가능 | 표본 효과 0(전달받은 측정). 전면 유료 재추출 전제 |
| 무료 검증 | 합성 프로필 + 고정 30·19 재생의 activation 전후 비교 | 사용자 확인 사례를 검증 규칙 + 로컬 회사 자료로 넣어 기대 점수 대조 | raw 재생만 가능 |
| 작은 수정으로 안 되는 반례 | 없음 | 없음 | 구내식당형 2단계·가중치 80/20, RISE형 "여러 신용수단 중 높은 값". 현 스키마로 표현 불가하나 빈도 미확인 |

**판단: 방안 2 + 방안 1 병행, 방안 3 보류.** 방안 2의 검증 규칙이 방안 3 필요성을 판단할 측정 자료가 된다.

---

## 4. 유료 호출 없는 계산 로직 검증

| 측정 | 방법 | 입력 | 현재 가능 여부 |
|---|---|---|---|
| 확인된 규칙 투입 시 계산기 정답률 | 사례별 golden 파일(항목·산식·회사 사실·기대 점수·기대 상태) → `estimate_quantitative_score` 직접 호출. 항목 점수·소계·상태 일치율 | 사용자 확인 사례의 기대값, 로컬 회사 자료 또는 SYN | 기대값 미제공 → 확인 불가 |
| 저장 raw 투입 시 계산 도달률 | 재생 CLI에 단계 깔때기: 프로필 상태 → 활성 상태 → criteria 수 → 항목 상태 분포. 고정 30 + 이전 19 | private snapshot | 필드 존재, 집계만 추가 |
| 원문과 추출 규칙 일치 | 방안 2 검증 규칙과 raw 후보를 항목명·만점·행·단위로 대조 → 정밀도·재현율 | 검증 규칙 + raw | 검증 규칙 입력 후 가능 |

합성 통과는 실제 공고 해결로 세지 않는다.

---

## 5. 다음 구현 과제 3개

| 과제 | 해결할 계산 실패 | 수정 위치 | 무료 성공 기준 | 입력 복구 의존 |
|---|---|---|---|---|
| 1. 행 단위 실패의 강등 | 실적 행 scope 미해석·단위·DSL 사유 하나로 신용·재무 행까지 공고 전체 REVIEW_REQUIRED | `_profile_activation_reasons`를 공고 사유(커버리지·manifest·대체표)와 행 사유로 분리, 행 사유는 `_partial_profile_review_criteria` 경로로 review 행 처리, 다표 허용 (`quantitative_scoring.py:3656-3911`) | 합성: 신용 행 + 완료 문구 없는 실적 행 → PARTIAL_ACTIVE, 신용 확정·실적 REVIEW. 고정 19·30 재생에서 AUTO 오탐 0, PARTIAL 증가. `test_quantitative_auto_activation.py`의 REVIEW_REQUIRED 단언은 PARTIAL로 갱신 | 병행 가능. 재추출 전에 끝내야 재추출 효과가 계산에 닿음 |
| 2. 사람 확인 규칙 입력 경로 | 사람이 규칙을 읽어도 모델 커버리지 전까지 계산 불가. curated JSON은 배포 필요·전 첨부 기록 요구 | 새 모델+마이그레이션, 운영자 전용 API(`private_company_evidence.py` 패턴), `estimate_for_notice` 우선순위, `_profile_for_notice`의 전 첨부 기록 요구를 "사람이 검토한 첨부 목록 = manifest"로 대체, 공개 투영에 "사람 검증 규칙" 표시 | 사용자 사례 3건을 SYN 공고 fixture에 검증 규칙으로 넣고 신용등록 API로 회사 사실 결합 → 기대 점수 재현, 공개 스냅샷 왕복, raw·계약 무변경 | 불필요 |
| 3. 측정 장치와 보수 표시 | 규칙 미확정과 증빙 없음이 합계에서 합쳐짐. 계산 도달률을 잴 수 없음 | `estimate_quantitative_score` 합계 분리 필드, 실적 행 인정 건수 구조 필드, 재생 CLI 깔때기와 golden 비교 옵션, `_public_criteria_match_aggregate` 갱신 | 기존 스냅샷 복원 불변, 고정 표본 깔때기 표 산출, golden 사례 통과 | 병행 가능 |

재추출은 별도 운영 결정으로 남긴다. 재추출로 얻는 것은 첨부별 현행 계약 raw와 검증 기록이며,
그 뒤 경로는 프로필 merge → 활성 판정 → 항목 변환 → 회사 사실 결합 → 합계다.
과제 1 없이 재추출하면 실적 행이 있는 공고는 활성 판정에서 다시 멈추고,
과제 2 없이는 사용자가 이미 읽은 공고도 계산할 수 없다. **순서: 과제 1·2 먼저, 재추출 범위 결정은 그 다음.**

---

## 부록. 지난 커버리지 후속 검토 요지 (Codex 검증 결과 반영)

- 첨부별 실패 원인은 저장 payload에 대부분 기록되어 있으나 선택 reader `_current_manifest_attempts`가
  UNSUPPORTED 헤더·세대 교체·record 무효를 모두 `None`으로 접는다(`pps_enrichment.py:657, 671, 675`).
  Codex 정정: 최신 invalid 뒤 이전 valid CURRENT는 선택될 수 있고, 공고 단위 상태와 recorded count는 남는다.
- Codex 정정 수용: "항상 재다운로드", "무료 경로 2개뿐", "마감 공고 모든 경로 금지", "Issue 필드 추가 시
  모든 record fingerprint 변경"은 과장이었다. 다운로드 전 재사용 경로(`_stored_attachment_result`)가 있고,
  issue가 없는 record의 fingerprint는 바뀌지 않는다.
- 전달받은 측정: 활성 22공고·118첨부, 재추출 후보 상한 92첨부, 그 밖 차단 15첨부/10공고,
  NO_ATTEMPT의 동일 공고 재사용 후보 0, 구계약 raw 67의 AVAILABLE 0(무표 증거 아님).
- 누락 문장 면제·다표 부분 활성은 이 표본에서 우선 근거가 없었다. 다만 과제 1의 다표 허용은
  커버리지가 완전해진 뒤의 전제 조건이므로 계산 경로 관점에서 다시 포함했다.
