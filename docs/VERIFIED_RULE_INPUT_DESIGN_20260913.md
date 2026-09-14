# 사람 확인 정량 규칙 입력 설계 — 2026-09-13

상태: **설계 제안, 미구현**. 코드 검토 기준은 `d817ce5`다. 같은 브랜치에서 진행하는 행 단위 활성 변경과 구분한다. 이 문서는 운영 API, DB 모델·마이그레이션, 기존 reader, 계약 버전, 저장 기록을 변경하지 않는다. 공개 예시는 합성 `SYN-` 식별자만 사용한다.

## 1. 먼저 바로잡을 주장

| 주장 | 판정 | 코드 근거와 적용 범위 |
|---|---|---|
| 모든 curated 프로필은 모델의 전 첨부 검증 기록을 요구한다 | **코드로 확인: PPS manifest가 있는 AVAILABLE 프로필에 한정** | `quantitative_scoring.py:4215–4235`는 현재 manifest의 모든 유효 accepted 기록과 문서 완전성을 요구한다. 그러나 PPS metadata가 없으면 `:4237–4247`의 최신 `PUBLIC_DOCUMENT_REFERENCE` digest 경로가 있다. MISSING curated 항목은 `:4266–4267`에서 조기 반환하며 점수를 활성화하지 않는다. |
| AVAILABLE 프로필의 행 사유 하나가 공고 전체를 막는다 | **코드로 확인** | `quantitative_scoring.py:3796`은 기존 부분 활성 대상을 REVIEW·단일 표로 제한한다. AVAILABLE 프로필은 `:3953–3961`에서 활성 사유가 있으면 `criteria=[]`로 반환한다. 이미 지원하는 REVIEW 부분 활성까지 없다는 뜻은 아니다. |
| 실적 scope는 명시적 기준일과 완료 실적만 허용한다 | **코드로 확인: 일부 부정확** | `quantitative_performance.py:328–336`은 기준일 미기재를 `UNSPECIFIED`로 보존하고 두 기준일 충돌만 거절한다. `:407–408`은 원문의 완료 여부 무관을 허용한다. lookback, 유사범위 또는 수동 조건, 완료 여부 규칙은 필요하며, 해석 실패는 scoring `:3719–3723`의 행 사유로 연결된다. |
| 재추출하면 회사 사실 binding이 항상 바뀐다 | **코드로 확인: 조건부** | `quantitative_scoring.py:2540–2557`은 후보 dump와 문서 SHA를 해시한다. 같은 값의 재추출은 유지되며, 비어 있는 CASE 상한 필드는 호환성을 위해 제외한다. 실제 후보 내용이나 문서 SHA가 달라지면 재결합이 필요하다. |
| 신용 등록은 신용 항목 하나만 허용한다 | **코드로 확인** | `private_company_evidence.py:150–182`는 동적 프로필만 조회하며 신용 criterion 수와 유효 binding 수가 모두 1이어야 한다. 새 사람 규칙 모델에 해시만 추가해도 기존 API가 자동 호환되지는 않는다. |
| 합계가 규칙 미확정과 증빙 부족을 합친다 | **코드로 확인** | `quantitative_scoring.py:1414–1419`는 REVIEW와 UNSCORABLE의 미확정 배점을 합친다. 다만 REVIEW에는 회사 증빙 키 불일치·수동 실적 조건 미증명도 포함된다(`:1085–1133`). 상태만 보고 REVIEW 전부를 원문 결함으로 분류하면 안 된다. |
| 재무 파생은 항상 ESTIMATED이고 세 비율만 지원한다 | **코드로 확인: 성공한 파생값에 한정** | `quantitative_financial.py:23–27,69–72,143–209`는 세 비율과 ESTIMATED/REVIEW를 지원한다. 증빙 부족 등 실패는 REVIEW다. 검증된 정확한 binding의 회사 사실은 파생값보다 우선하므로 재무 항목 자체가 CONFIRMED 불가능하다는 뜻은 아니다(`quantitative_scoring.py:4481–4502`). |

이 문서는 코드 조건을 검토했다. 실제 공고 효과, 회사 사실 충족, 사용자 기대값의 확정 여부는 **확인 불가**이며 private golden이나 합성 성공을 실제 해결 건수로 세지 않는다. private 표본 개수는 별도 검증 보고서의 측정 영역이다.

## 2. 허용할 결과와 신뢰 경계

사람 입력은 모델 입력과 마찬가지로 검증 대상이다. 검토자 이름, 검토 시각, `HUMAN_REVIEWED` 선언은 감사 정보이지 원문·배점·커버리지 검증 결과가 아니다. `QuantitativeCriterion` 형태의 JSON이 모델 검증을 통과해도 원문과 일치한다는 증명이 되지 않는다.

입력 상태와 계산 상태를 분리한다.

| 입력 상태(제안) | 의미 | 계산·저장 경계 |
|---|---|---|
| `DRAFT` | 검토 작성 중 | 운영 점수 입력 불가 |
| `ATTESTED_ONLY` | 사람 확인 선언은 있으나 기계 검증 증명이 부족 | 검증 규칙으로 활성화 불가. 별도 비저장 시나리오만 허용 가능 |
| `SOURCE_VERIFIED` | 고정 원문과 규칙의 기계 검증 통과 | 아직 공고 전체 커버리지·프로그램·선택 권한을 뜻하지 않음 |
| `APPROVED_FOR_NOTICE` | 현재 공고의 전체 증명과 명시 선택 권한이 일치 | 지원되는 항목만 기존 엔진으로 전달 |
| `STALE`, `REVOKED`, `REJECTED` | 출처·정정·검토 또는 검증 상태 무효 | 명시적으로 차단; 과거 승인으로 자동 복귀 금지 |

`APPROVED_FOR_NOTICE`도 회사 사실의 존재·유효성이나 CONFIRMED 점수를 보증하지 않는다. 자격 PASS/REVIEW/FAIL, 참여 결정, 정량 원문 준비 상태, 회사 증빙 상태는 서로 독립이다.

검증기나 배점 DSL이 지원하지 않는 규칙을 사람이 승인했다는 이유로 실행해서는 안 된다. 범위가 원문에 없으면 추정해 넣지 않는다. 명시되지 않은 0건·미제출·가중치·최저점도 생성하지 않는다.

## 3. 제안 모델과 검증 순서

이하 모델명과 상태명은 설계 이름이며 현재 제공되는 API가 아니다. 최초 구현은 순수 검증 함수와 비저장 결과로 시작하고, 영속화와 운영자 API는 별도 승인·검증을 거친다.

### 입력 묶음

`VerifiedRuleDraft`는 strict/extra-forbid 모델로 다음을 받는다.

| 필드 | 의미와 검증 |
|---|---|
| `schema_version`, `notice_key`, `revision_id`, `supersedes_revision_id` | 사람 규칙의 별도 계약·공고·불변 개정 식별자. 기존 모델 추출 계약을 사칭하지 않음 |
| `manifest_sha256` | 서버가 현재 전체 descriptor manifest로 다시 계산한 값과 비교하는 제출 예상값 |
| `reviewed_attachment_ids` | 검토 목록. 중복 금지, 전체 manifest와 집합 일치가 필요하지만 이것만으로 충분하지 않음 |
| `attachments[]` | `attachment_id`, `document_sha256`, descriptor SHA, 원문 객체 참조, 검토 위치/범위. 파일명이나 URL만으로 신원 증명 금지 |
| `tables[]`, `criteria[]` | 표 신원·항목 소유 관계·원문 총점·최저점과 각각의 인용. criterion은 기존 `QuantitativeCriterion`에 대응하는 지원 필드만 허용하되 `fact_binding_sha256`과 검증 상태는 서버 산출 |
| `program` | 표 사이 additive/alternative/stage 관계와 선택 조건·가중치·소계의 원문 증거. 미지원 관계는 입력 보존만 하고 계산 거절 |
| `reviewer_id`, `reviewed_at`, `attestation` | 인증된 검토 주체와 확인 범위. 서버가 기록한 시각과 별도로 보존; 사용자 입력으로 승인 상태를 지정하지 못함 |

항목은 각 bracket/CASE/threshold/formula/recognition condition의 인용과 항목·표·첨부 소유 관계를 갖는다. 여러 첨부가 한 규칙을 완성하면 모든 출처를 명시한다. 원문이 다른 평행 열의 신용 수단을 합치거나, 같은 문구가 반복된 위치에서 편리한 점수를 고르는 것을 허용하지 않는다.

### 실적 범위

`performance_scope`는 현재 `PerformanceRecognitionScope`와 호환되는 명시 필드를 가진다: `metric_key`, `lookback_years`, `similarity_keywords`, `match_mode`, `counterparty_scope`, `counterparty_keywords`, `lookback_anchor_basis`, `minimum_single_contract_amount_krw`, `vat_basis`, `completion_required`, `aggregation`, `consortium_share_rule`, `certificate_required`, `manual_verification_conditions`, `source_literal`.

각 명시 값은 원문 인용과 연결해야 한다. 사람이 `similarity_keywords`를 입력했다고 자동 정당화되지 않으며 기존 문자열 scope 파서를 우회하는 무증명 값 주입도 금지한다. 수동 입력 값과 인용을 비교하는 결정적 검증기가 없는 필드는 ATTESTED_ONLY로 남긴다. 실제 기준일 미기재와 기준일 충돌을 구분한다. 절대 시작일과 상대 lookback이 충돌하거나 현재 모델이 절대 기간을 표현하지 못하면 임의 환산하지 않는다.

참여 인원·연간 계약금액 조건은 숫자가 필요한 규칙임을 보존한다. 현재 원장으로 증명할 수 없으면 자동 건수 파생을 막는다. 조건을 충족한 실적 집계의 별도 검증 자료와 현재 항목 binding이 있는 회사 사실을 요구하는 기존 경계를 유지한다. 미확인 건수는 실제 0건이나 미제출 상태가 아니다.

### 서버가 만드는 증명

`SourceVerificationProof`는 요청에 포함된 `verified=true`를 받아 만드는 객체가 아니다. 고정된 현행 parser와 원문 검증기를 실행한 서버가 산출하며 다음을 보존한다.

- native SHA, canonical UTF-8 SHA, parser 종류·버전, 추출 완전성, 경고, 페이지/섹션 위치 매핑, 검증기 버전
- 원문에서 유일하게 확인된 항목/배점/조건 인용과 출처 범위, 원래 입력 해시, 검증된 규칙 해시
- 첨부별 전체 검토 범위, 정량표·참조표·비적용·미해석 상태와 근거, unresolved references
- 전체 manifest SHA, 표별 합계와 적용 프로그램 검증 결과, 검증 시각

원문 인용 검증은 **재다운로드 시점에만 가능한 것이 아니다**. 로컬 보관 native 파일이 현재 attachment 및 manifest의 문서 신원과 연결되고 해시가 같으면 고정 parser로 즉시 검증할 수 있다. 반대로 출처 연결 없는 로컬 텍스트, 변환본 PDF, 호출자가 제공한 canonical SHA만으로 native 원문 증명을 대체할 수 없다. 변환본은 독립적으로 검증된 변환 출처가 필요하다. 로컬 바이트가 없거나 parser가 처리하지 못하면 ATTESTED_ONLY이며 무료 검증 성공으로 기록하지 않는다.

원문 전체를 고정한 뒤 인용을 검사한다. 발췌 조각의 이어 붙인 지점, 다른 첨부/표/열, 중복 문구를 건너는 인용은 차단한다. renderer·줄바꿈 정규화는 감사 가능한 범위에 한정하고 조건 연산자·금액·점수는 고치지 않는다.

## 4. 커버리지와 대체표 보호

기존 `_profile_for_notice`의 PPS 커버리지 요구를 `reviewed_attachment_ids == manifest IDs` 한 줄로 교체하지 않는다. 기존 reader와 허용 계약은 그대로 둔다. 새 경로는 별도 생산자 증명만 다르게 받고 **최종 불변조건은 같은 수준**으로 만족해야 한다.

필요조건은 다음 모두다.

1. 서버가 현재 manifest를 확정하고 모든 descriptor·첨부 ID를 중복 없이 묶는다.
2. manifest의 모든 첨부에 실제 문서 바이트 신원, 전체 검토 및 지원 검증 결과가 있다. 읽지 못한 첨부·페이지·ZIP member·외부 참조는 남은 결함이다.
3. 정량표가 없다는 사람 선언만으로 해당 첨부를 검증 완료로 분류하지 않는다. 지원되는 비적용/범위 증명과 필요한 교차 참조를 검증하지 못하면 계속 차단한다.
4. 모든 항목이 원문 표·부모 범위·단계에 유일하게 속하고, 각 원문 소계와 전체 프로그램이 정확히 맞는다. 행 단위 REVIEW의 만점도 원문 소계에서 사라지지 않는다.
5. 동일 소계·유사 항목의 여러 표는 단순 합산하거나 하나를 임의 선택하지 않는다. 동일 원문 중복이라는 증명, 명시적인 상하 관계 또는 적용 조건이 필요하다.
6. 단계별 선택, 최고점 선택, 비율 가중치, 정성/가격 배제에 필요한 원문 관계가 미증명이면 프로그램 전체를 차단한다. 빈 AVAILABLE 표를 만들거나 다른 표의 상태를 강제로 바꾸어 검증을 통과시키지 않는다.

사람이 먼저 규칙을 확인하면 모델의 새 raw 없이도 규칙을 만들 수 있다는 방향은 타당하다. 그러나 **새 검증 경로가 이 증명을 실제로 수행하기 전에는 모델 기록 요건을 제거할 수 없다**. 기존 진단용 revalidation 결과도 persistence/coverage 자격이 없으므로 대체 기록으로 사용하지 않는다. 구계약 0.5.5 허용이나 최신 계약 재도장은 별개로 계속 금지한다.

## 5. binding과 선택 권한

### 두 종류의 해시

회사 사실 binding과 검증 실행 감사 해시를 분리한다.

```text
rule_binding_sha256 = SHA256(canonical_json({
  binding_schema: "SYN-proposed-verified-rule-binding-v1",
  rule: verified_criterion_semantics_and_governing_program,
  source_documents: sorted(attachment_id, document_sha256)
}))

verification_sha256 = SHA256(canonical_json({
  rule_binding_sha256, manifest_sha256, descriptor_digests,
  canonical_digests, source_proofs, parser_version, validator_version,
  revision_id, reviewer_id, reviewed_at, approval_event
}))
```

이는 제안 계산식이다. `rule`은 실행에 영향을 주는 metric·단위·유효 시점·인정범위·대체/단계 조건·가중치·배점·상한·최저점을 모두 포함한다. 항목의 범위를 바꾸는 프로그램 정보가 hash 밖에 있어서는 안 된다. 단일 출처이면 source documents는 문서 SHA 하나에 대응하며, 여러 출처이면 모두 포함한다.

canonical JSON은 키 정렬·인코딩·수치 표현·기본값 포함 규칙을 명시적으로 버전 관리한다. 임의 Pydantic 필드 추가로 무관한 해시를 전부 바꾸지 않는다. 검토자/시각만 바뀌거나 모델이 같은 원문을 다시 추출했다는 사실만으로 의미가 같은 사람 규칙 binding을 바꾸지 않는다. 반대로 기준·점수·출처가 바뀌면 새 binding이다. manifest가 바뀌면 규칙 의미가 같더라도 현재 검증/선택 자격은 무효화하여 다시 확인한다.

기존 동적 binding 함수는 변경하지 않고 새 규칙은 별도 namespace로 구분한다. 기존 회사 사실을 새 해시로 재도장하거나 generic 회사 사실을 자동 재결합하지 않는다. 동일 의미를 근거로 사실을 다시 쓰려면 별도 명시적 검증·감사 절차가 필요하다.

### 닫힌 선택 정책

현재 `estimate_for_notice`는 동적 프로필이 존재하면 REVIEW이어도 결과를 반환하고, 동적 프로필 자체가 없을 때 curated 경로에 간다(`quantitative_scoring.py:4448–4511`). 새 규칙을 “동적 계산 실패 시 가장 잘 계산되는 fallback”으로 끼우지 않는다.

제안은 공고별 명시적인 authority 선택이다.

| 선택 | 실행 규칙 |
|---|---|
| `DEFAULT` | 기존 동적 우선, 기존 조건의 curated fallback을 그대로 보존 |
| `VERIFIED_REVISION` | 권한 있는 운영자가 특정 승인 revision을 명시 선택. 그 revision의 현재 출처·커버리지·프로그램을 검증한 뒤에만 실행 |
| 선택된 revision이 stale/revoked/검증 실패 | REVIEW_REQUIRED. dynamic·이전 사람 revision·curated로 자동 복귀하지 않음 |
| 여러 승인 revision 또는 동적 규칙과 의미 충돌 | 적용 우선순위·정정 근거가 명시적으로 확정될 때까지 차단. 최신 시각·높은 점수로 선택 금지 |

선택은 공고별 규칙 경로만 지정하며 문서 결함이나 회사 증빙 부족을 승인으로 해제하지 않는다. 동적/사람 규칙에서 좋은 항목만 섞는 것은 새 프로그램 검증 없이는 금지한다. 검토·승인·활성화·철회는 각각 감사 이벤트로 남긴다.

운영자 인증은 기존 비공개 증빙 전용 권한 수준을 최소로 삼는다(`auth.py:77–101`). 부서 쿠키·데모 PIN·공개 브라우저 입력으로 규칙 활성 권한을 주지 않는다. 규칙 편집 권한과 회사 증빙 등록 권한은 기능별로 확인하며, API 키를 브라우저에 전달하지 않는다.

### 신용 등록 API와의 연결

현재 `_credit_rating_binding_for_notice`는 동적 프로필만 사용하고 criterion 한 개를 요구한다. 따라서 새 규칙 경로 구현 시 공통의 **선택된 검증 규칙 resolver**를 별도로 설계해야 한다. 등록 요청은 공고뿐 아니라 선택 revision·criterion을 지정하고, 서버가 현재 권위·해시를 다시 확인해야 한다. 호출자가 임의 binding을 지정하는 방식은 금지한다.

기존 API의 “신용 항목 정확히 하나” 계약은 별도 API 버전 또는 명시적 확장 전까지 유지한다. 단계별 신용 항목이 여러 개이면 일괄 복제하지 않고 각 조건·binding·유효 시점별로 증빙을 결합한다. 규칙 증명 SHA와 회사 증빙 문서 SHA도 구분한다.

## 6. 공개 투영과 검증 골격

공개 표시는 “사람이 입력한 규칙”, “원문 기계 검증 완료”, “일부 항목 보류” 등 실제 상태를 표현한다. 사람 확인을 회사 점수 CONFIRMED나 정량 검토 전체 완료로 번역하지 않는다. 원문 인용, 검토자 신원, 내부 SHA, private 자료 참조, 회사 입력값은 새로운 공개 필드로 추가하지 않는다. 기존 공개 allowlist 계약을 유지하고 새 필드가 필요하면 별도 투영 계약·왕복 검증을 둔다.

규칙 미해석, 회사 증빙 없음/불일치, 추정값을 서로 다른 원인으로 보존한다. REVIEW/UNSCORABLE의 상태 이름만으로 원인을 재분류하지 않는다. 보수 하한, 미확정 상한과 확정 점수를 구분하며 미확인 실적을 실제 0건으로 표현하지 않는다.

최초 순수 인터페이스의 역할은 세 단계로 제한한다.

```text
verify_rule_draft(draft, current_manifest, local_native_sources)
  -> VerifiedRuleValidationResult  # 또는 attestation/rejection; 외부 효과 없음
select_rule_revision(validation_result, explicit_authority, current_notice_state)
  -> AuthorizedRulePlan           # stale/conflict이면 실행 계획 없음
build_engine_request(authorized_rule_plan, separately_verified_company_facts)
  -> 기존 엔진 요청               # 선언만으로 CONFIRMED fact를 만들지 않음
```

구현 전에 준비할 합성 회귀 목록은 다음과 같다.

| 합성 검증 | 기대 |
|---|---|
| SYN 로컬 native 및 현재 manifest가 일치하고 지원 단일 표의 모든 규칙 검증 성공 | 네트워크 없이 원문 검증 가능; 회사 값 없으면 확정 점수 없음 |
| reviewed IDs만 일치, native/quote/누락 페이지 증명 부족 | ATTESTED_ONLY 또는 거절, 커버리지 완료 금지 |
| 다른 문서·첨부·인용·CASE 상한·점수 변조 | 거절, 기존 기록 불변 |
| 정정 manifest 또는 선택 revision 철회 | stale/REVIEW, 과거 규칙 fallback 금지 |
| 동일 소계 대체표, 단계·가중치·최고점 선택 미증명 | 프로그램 차단, 좋은 표만 부분 활성 금지 |
| 명시적인 별도 소계의 지원 additive 표 및 행 단위 미해석 | 전체 소계·소유 검증 유지, 허용 범위만 부분 활성 |
| 실적 연간금액/참여인원 미증명, generic 또는 stale 회사 fact | 숫자 집계·만점 승격 금지 |
| 두 신용 항목/새 사람 규칙/기존 단일 신용 API | 임의 선택·자동 호환 금지 |
| 규칙 의미 불변 재추출/검토 시각 변경, 의미·출처 변경 | 전자는 rule binding 유지 가능, 후자는 새 binding; 현재 manifest 검증은 항상 별도 |
| 공개 스냅샷 및 원문/비밀 스캔 | 내부 출처·검토자·회사 값 유출 없음, 구 스냅샷 호환 유지 |

사용자 기대값은 비공개 조건부 golden의 각 전제와 상태를 보존한다. 공개 테스트는 전부 독립 SYN 규칙·회사 사실로 만든다. 사용자 기대점수를 검증기 통과를 위한 입력으로 쓰지 않고, 검증한 입력으로 엔진을 실행한 뒤 기대 결과와 비교한다. 합성 기대값 일치, 실제 원문 규칙 검증, 실제 회사 증빙 확인은 별도 분모로 보고한다.

## 7. 이번 작업의 완료 범위

이 파일은 설계 문서만 추가했다. 제품 코드·API·DB·계약은 구현하지 않았다. 운영/네트워크/모델 호출 및 다운로드는 수행하지 않았다. 기존 원문 범위 작업 트리는 변경하지 않았다.

추가 합성 메모리 확인은 **6/6 통과**했다: 명시 기준일 없는 scope 허용, 완료 여부 무관 허용, 완료 규칙 누락 거절, 상충하는 두 기준일 거절, PPS metadata 없는 curated의 일치 reference SHA 선택, 다른 reference SHA 거절. 이 검증은 회사 자료·DB·엔진 또는 transport 객체 없이 관련 순수 함수를 호출했다. 행 단위 활성 변경의 테스트 및 private 표본 재생은 별도 담당 결과로 기록하며 이 6건에 포함하지 않는다.
