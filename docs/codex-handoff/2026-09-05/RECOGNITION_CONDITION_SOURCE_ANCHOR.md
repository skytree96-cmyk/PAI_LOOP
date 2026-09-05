# 실적 인정조건 근거 앵커 증명 보강 — 2026-09-05

## 배경

정량 후보가 `AUTO_ACTIVE`로 올라가지 못하는 사례를 추적하다가
`RECOGNITION_CONDITION_LITERAL_MISMATCH`가 반복적으로 남는 패턴을 확인했다.
이 검사는 `22adea8`에서 도입된 뒤 변경된 적이 없고, PR #83/#84가 다룬 범위
밖이다.

## 원인 (원문 대조로 확인)

추출 모델은 실적 인정조건에서 원문 문장/절 전체를 `literal`에 그대로 옮긴 뒤,
같은 문장을 **종결 부호만 뺀 형태**로 `evidence.quote`에 인용한다. 즉
`quote`가 `literal`의 부분 문자열이 된다.

- 마침표가 있는 각주: `literal` 길이 = `quote` 길이 + 1
- 쉼표로 끝나는 절: 동일하게 +1
- 종결 부호가 없는 각주: 두 길이가 같고 검사도 통과

`_literal_is_anchored`는 `literal ⊆ quote`만 인정하므로 위 조합은 항상
실패한다. 두 문자열 모두 첨부 원문에 그대로 존재하고 같은 위치를 가리키므로
날조가 아니라 포함 방향만 반대인 경우다.

HWP 셀 분할 복구(`_rebind_candidate_table_cell_literals`)가 evidence 인용문을
원문 줄 전체로 넓혀 이 문제를 **부분적으로** 가려 왔다. 그러나 그 복구는
criterion region, 유일 span, 예약 span 비충돌 등 여러 구조 조건을 모두
만족해야만 동작한다. 같은 criterion 안의 다른 인정조건이 넓은/불일치 앵커를
가지면 복구가 막히고, 종결 부호만 다른 정상 인정조건까지 하드 실패로 남는다.
`tests/test_quantitative_rule_extraction.py`의
`test_busan_hwp_preserves_unbounded_count_share_claim[broad]` 픽스처가 이
상태를 그대로 재현한다.

## 변경

`src/pai_loop/quantitative_rule_extraction.py`

- `_literal_owns_its_source_anchor()` 추가. 다음 네 조건을 모두 만족할 때만
  인정조건 literal의 근거 결속을 증명한 것으로 본다.
  1. `literal`이 첨부 원문에 그대로 존재한다.
  2. `evidence.quote`가 그 `literal` 안에 연속으로 존재한다.
  3. `literal`이 원문에 정확히 1회만 나타난다.
  4. `evidence.quote`도 원문에 정확히 1회만 나타난다.
- `_validate_recognition_conditions()`는 기존 `_literal_is_anchored`가 실패한
  경우에만 위 증명을 추가로 시도한다.
- `_assert_available_candidate_invariants()`는 원문 없이 재검증되는 영속 레코드
  전용 구조 검사이므로 포함 방향을 양쪽 모두 허용한다. 원문 기반 증명은
  첨부 원문을 아는 검증 단계에만 남는다.
- `_TARGETED_RECORD_FINGERPRINT_REVISIONS`에
  `RECOGNITION_CONDITION_LITERAL_MISMATCH` 항목을 추가해, 이 이슈를 저장한
  레코드만 재검증되게 했다. 실행 채점 의미는 바뀌지 않으므로
  `QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION`은 올리지 않았다.

### 여전히 fail-closed로 남는 것

- 원문에 없는 의역 literal
- literal 안에 없는(다른 행에서 따온) 인용문
- 원문에 2회 이상 나타나 위치를 특정할 수 없는 literal 또는 인용문
- 평가행(case/bracket/threshold/criterion) literal 검사는 변경하지 않았다.
  연산자·구간·배점·만점 값은 어떤 경우에도 그대로 유지된다.

## 검증

- `tests/test_quantitative_rule_extraction.py` 신규 회귀:
  - `test_recognition_anchor_may_quote_part_of_an_exact_unique_source_literal`
  - `test_recognition_anchor_part_survives_the_persisted_record_invariants`
  - `test_recognition_anchor_part_still_fails_closed_without_a_source_proof`
    (의역 / 다른 행 인용 / 중복 literal 3케이스)
  - `test_recognition_condition_mismatch_record_is_revalidated_after_the_proof_change`
- `test_busan_hwp_preserves_unbounded_count_share_claim`의 `broad` 케이스는
  이제 `SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION`만으로 fail-closed를 유지한다.
  `disjoint` 케이스는 기존대로 두 코드를 모두 요구한다.
- 전체 suite와 CI 커버리지 게이트 통과.

## 운영 영향

영속된 정량 검증 레코드 중 `RECOGNITION_CONDITION_LITERAL_MISMATCH`를 담은
것만 fingerprint가 달라져 재분석 대상이 된다. 배포만으로 기존 결과가 바뀌지는
않으며, 해당 첨부를 다시 분석해야 새 판정이 반영된다.
