# Codex 작업 지시서 — PAI_LOOP 자격요건(ELIGIBILITY) REVIEW 과다 발생 수정

## 배경 (반드시 먼저 읽을 것)

`src/pai_loop/eligibility_policy.py`의 자격요건 판정 로직이 실제로 PASS/FAIL 판정
가능한 케이스까지 전부 REVIEW로 떨어뜨리고 있다. 운영 스냅샷 기준 REVIEW 1,767건
(영향 공고 234개) 중 절대다수가 "이미 회사 데이터가 있고 매칭 로직도 존재하는데,
프레시니스(재확인) 게이트가 강제로 REVIEW로 덮어쓰는" 케이스다. 이번 작업의 목표는:

1. 이 프레시니스 버그를 고쳐서 이미 구현된 로직이 정상 작동하게 한다 (최우선, 파급 효과 최대)
2. 팀 피드백에서 확정된 신규 매칭 로직/데이터를 반영한다 (지역 예외, 신인도, 물품코드, 법인유형)
3. 체크리스트 항목(서약서류 등)을 "항상 준비 가능"으로 표기하되 준비물 목록으로는 계속 노출한다

## 근거 자료

- 정책 스펙 원본: `PAI_LOOP_AI_판단_정책설정_현수정_260903.xlsx`의 '기준설정' 시트
  (24개 기준코드, P0~P3 우선순위, PASS/FAIL/REVIEW 판정 기준 원문이 각 행 R열에
  상세 서술되어 있음). 같은 파일의 '공고별 확인대상' 시트에는 실제 공고 원문 6,437건이
  기준코드별로 들어있어 회귀 테스트 케이스로 그대로 쓸 수 있다.
- 회사 마스터 데이터: `src/pai_loop/data/company_public_profile.json`
- 설계 문서: `docs/COMPANY_PUBLIC_PROFILE_AND_REQUIREMENT_POLICY_v0.3.0.md`
- 대상 코드: `src/pai_loop/eligibility_policy.py` (POLICY_VERSION 상수를 이번 변경에
  맞춰 반드시 올릴 것, 예: `pai-loop-requirement-policy-2026.09.0X-v7`)
- 기존 테스트: `tests/test_eligibility_policy.py` (프레시니스 관련 테스트가 현재 전무함 — 이번에 반드시 추가)

---

## Phase 1 (최우선, 단독으로도 배포 가치 있음): 프레시니스 재확인 게이트 수정

### 문제

`_deadline_freshness_recheck_required()` (eligibility_policy.py 약 846-874행)가
다음 조건으로 "재확인 필요"를 판단한다:

```python
last_verified = _as_date(fact.get("last_verified_at"))
if last_verified is not None and deadline <= last_verified:
    return False
return True
```

`last_verified_at`은 회사 사실을 마지막으로 확인한 고정 과거 날짜이고, 진행 중인
공고의 `deadline`(마감일)은 정의상 항상 미래다. 따라서 `deadline <= last_verified`는
구조적으로 절대 참이 될 수 없고, `RECHECK_*`/`RECONFIRM_*` 정책이 걸린 사실은
100% `freshness_recheck = True`가 되어 `_eligibility_item()`에서 무조건
`outcome = "REVIEW"`로 강제된다 (`effective`도 `confirmed_absence`도 항상 False가
되기 때문). 회사 프로필의 49개 fact 중 48개가 이 정책을 갖고 있어 사실상 전체
ELIGIBILITY 판정이 무력화되어 있다.

이 버그가 REVIEW를 만들고 있는 대표 기준코드 (모두 실제로는 매칭 로직이 이미
구현되어 있음): `notice_sanction_eligibility`, `conviction_clear`,
`small_business_certificate`, `notice_industry_code_eligibility`,
`direct_production_certificate`, `notice_certification_eligibility`(named permit
경로), `notice_region_eligibility`(서울 게이트 경로).

### 요구되는 수정

`deadline_check_required`(재확인 알림 플래그, 이미 아이템에 노출되고 있음)와
`freshness_recheck`(자동 REVIEW 강제)의 의미를 분리해야 한다.

- **RECHECK/RECONFIRM 정책이 걸린 사실이라도, 현재 값이 유효 범위(`in_effect`) 안에
  있고 증빙 상태가 `VERIFIED*` 또는 `COMPANY_DECLARATION`/`COMPANY_CONFIRMED*`이면
  PASS(또는 매칭 결과에 따른 FAIL_CONFIRMED)로 판정하고, `deadline_check_required=True`는
  "제출 전 재확인 권장" 배지로만 노출한다.** REVIEW로 강제 다운그레이드하지 않는다.
- REVIEW로 떨어뜨려야 하는 진짜 사유는 다음으로 한정한다:
  - fact 자체가 없거나(`value` 키 부재)
  - `in_effect`가 False (유효기간 실제 만료, 마감일 밖)
  - 증빙 상태가 `MISSING`/`UNVERIFIED`
  - **staleness 임계값**: `last_verified_at`으로부터 실제 오늘 날짜(시스템 시각) 기준
    경과일이 일정 기간(제안: FACT 유형은 180일, 정성 선언은 365일 — 팀 확인 후 확정)을
    초과한 경우만 "갱신 필요" REVIEW로 전환. (마감일과 비교하는 현재 로직은 완전히 폐기)
- `_eligibility_item()`의 `effective` 계산식을 위 기준으로 재작성하고,
  `confirmed_absence` 판정도 freshness와 독립적으로 동작하도록 분리한다.
- 팀 피드백 2번("결격 사유는 일단 모두 문제없음 처리하고 추후 업데이트")과 정확히
  일치하는 동작이 되어야 한다: 지금 CLEAR인 사실은 즉시 PASS 처리하고, 실제로 제재/결격
  사실이 발생하면 `company_public_profile.json`을 수동 갱신해 그 시점부터 FAIL로
  전환하는 것이 정상 흐름이다.

### 검증

- `tests/test_eligibility_policy.py`에 회귀 테스트 추가: 마감일이 `last_verified_at`보다
  미래인데도 `value=true`/`VERIFIED*`인 fact가 PASS로 판정되는지 확인
  (지금까지 이 케이스에 대한 테스트가 전혀 없었음).
- '공고별 확인대상' 시트에서 `notice_sanction_eligibility`, `conviction_clear`,
  `small_business_certificate`, `notice_industry_code_eligibility`,
  `direct_production_certificate` 원문 샘플(각 기준코드당 최소 5건, 시트에 실제
  원문 텍스트 있음)을 테스트 픽스처로 추가해 PASS/FAIL이 기대대로 나오는지 확인.
- 수정 후 전체 진행 공고 재평가 시 REVIEW 건수가 어느 정도로 줄어드는지 로그로 남길 것
  (기존 1,767건 대비 감소폭 보고).

---

## Phase 2: 팀 피드백 기반 신규 로직/데이터 반영

기준설정 시트 각 행(R열)에 PASS/FAIL/REVIEW 상세 분기가 이미 텍스트로 정의되어
있으니 그대로 구현 규칙으로 옮길 것.

### 2-1. 지역 제한 (`notice_region_eligibility`) — 기준설정 A13행

현재 `_is_seoul_head_office_gate()`만 구현되어 있고, 그 외 지역은 전부
`_unmapped_eligibility_item()`(항상 REVIEW)으로 빠진다.

- **FAIL 추가**: 본점 소재지를 서울이 아닌 특정 시·도(예: 제주, 경기, 인천, 강원 등)로
  단독 한정하는 공고는 회사 본점이 서울이므로 명시적 FAIL(`INELIGIBLE`)로 판정한다.
  ("공고별 확인대상" 시트에 제주 사례 다수 있음, 예: "주된 영업소 소재지가
  제주특별자치도에 소재한 업체")
- **EXCEPTION 분기 추가**: "안정적인 연수운영 장소를 확보(임대·대관 포함)", "교육장
  임차 확약서" 등 시설 임대/확보를 요구하는 문구가 지역 제한과 결합된 경우 자동
  FAIL하지 않고 EXCEPTION(사람 검토 큐)으로 분류한다. 팀 피드백 5번("장소 임대확보
  조항 수용")과 일치.
- 지역 제한이 없거나 서울/전국/수도권을 포함하는 경우는 기존과 동일하게 PASS.
- `company_public_profile.json`의 `declared_branch_region_codes`는 설계 문서에
  명시된 대로 자동 PASS 근거로 쓰지 않는다(공식 지사 증빙 없이는 미사용 유지).

### 2-2. 신인도(신용평가등급) — 기준설정 A12행 인증/면허 판정에 포함

- `company_public_profile.json`에 신용등급 fact가 전혀 없다. 다음 필드를 추가한다:
  `credit_rating`: `{"value": "A0", "evidence_state": "VERIFIED", ...}` 형태로,
  회사가 보유한 실제 등급 문서 근거를 참조해 값을 채운다 (담당자에게 실제 등급·근거
  파일 확인 요청 — 이 값은 코드에서 임의로 만들지 말 것).
- 공고 원문에서 "신용평가등급 BB 이상", "신용등급 BBB 이상 확인서" 등 요구 등급을
  파싱해 등급 순서(AAA > AA > A > BBB > BB > B > CCC ...)를 비교하는 헬퍼 함수를
  추가하고, 요구 등급이 자사 등급(A0) 이하이면 PASS, 초과(AA 이상 요구 등)면 FAIL로
  판정한다. 기준설정 R12행 "신용평가 룰" 문구를 그대로 구현 스펙으로 사용.

### 2-3. 법인·사업자 유형 자격 (`notice_entity_eligibility`) — 기준설정 A9행

현재 완전 미구현(`_unmapped_eligibility_item` 고정). 아래 로직 추가:

- 대기업/중견기업/상호출자제한집단 배제 요건 → `nonprofit_entity`(비영리) 또는 별도
  `enterprise_scale`(예: `SME_OR_NONPROFIT`) fact 신설해 자동 PASS
- "비영리법인 참가 허용" 명시 문구 → `nonprofit_entity=true` 매칭 PASS
- 대학/산학협력단·공공기관·금융기관 한정 등 KMA가 원천적으로 해당 안 되는 기관유형
  단독 요구 → FAIL(`INELIGIBLE`)
- 정성적 서술("전문지식 축적 기관" 등)만 있는 경우는 여전히 REVIEW 유지 (자동판정 대상 아님)

### 2-4. 직접생산확인증명서 (`direct_production_certificate`) — 기준설정 A14행

현재 매칭 로직 자체는 맞게 구현되어 있음(팀 피드백 6번 "예외건 외에는 다 FAIL"과
일치). Phase 1의 프레시니스 수정이 적용되면 자동으로 정상화될 것으로 예상됨 —
별도 로직 변경 불필요, 회귀 테스트로만 확인.

### 2-5. 지정 물품·세부품명 등록 (`notice_specific_product_registration`) — 기준설정 A16행

- 발생 건수 3건으로 낮은 우선순위. `company_public_profile.json`에 등록된
  세부품명번호 목록(예: 서적 5510151001, 노트북컴퓨터 4321150301, 태블릿컴퓨터
  4321150901, 응용과학용소프트웨어 4323260501, 도주차량차단장비 4616151001,
  전부 `제조: N`)을 fact로 추가하고, 공고가 요구하는 10자리 세부품명번호와 대조하는
  매칭 로직 추가. "제조물품 등록 필수" 요구는 회사가 `제조: N`이므로 무조건 FAIL.
  이 항목의 실제 물품 목록 값은 담당자가 실제 등록증 원본을 보고 확정해 전달할 것
  (임의 생성 금지).

---

## Phase 3: 체크리스트 항목 — "전부 PASS로 간주하되 준비물로는 계속 노출"

대상: `integrity_pledge`, `standard_pledges`, `cost_breakdown`, `attendee_documents`,
`safety_health_pledge`, `attendee_limit_two`, `electronic_bidding`,
`direct_visit_submission` (기준설정 A19~A27행, 전부 CHECKLIST/INFORMATION 분류이며
현재도 적격성 FAIL/REVIEW를 만들지 않음 — 이 항목들은 애초에 REVIEW 폭증의 원인이
아니었음을 확인함).

- 이 항목들은 회사가 항상 이행 가능한 표준 행정 절차이므로, 공고 수집 시 기본
  상태를 "차단 없음(NO_BLOCK) + 준비물 목록에 자동 등록"으로 표시하는 방향으로
  UI/상태 라벨을 조정한다.
- **단, 완전히 숨기지는 말 것.** 담당자가 실제로 서약서에 날인하고 제출했는지는
  여전히 별도 업무이므로, "제출 대상 서식 목록(준비물)"으로는 계속 노출하되 이게
  자격 판정에는 전혀 영향을 주지 않는다는 것을 화면상 명확히 구분한다
  (예: 배지 문구를 "회사 역량 확인됨 · 준비물 목록에 포함" 형태로).
- 이 부분은 API/데이터 모델보다 `src/pai_loop/api.py`의 응답 조립 로직과
  `src/pai_loop/static/app.js`/`index.html`의 표시 로직 확인이 필요할 수 있음
  (Phase 1, 2 완료 후 별도로 진행해도 무방).

---

## 공통 요구사항

- 모든 변경에 대해 `tests/test_eligibility_policy.py`에 단위 테스트 추가.
  가능하면 `tests/test_analysis_pipeline.py`, `tests/test_api.py`의 관련 케이스도
  함께 갱신.
- `POLICY_VERSION` 상수 갱신.
- `company_public_profile.json`을 수정할 경우 `_assert_public_safe()` 검증을
  통과해야 하며 민감정보(주소, 대표자명, 사업자등록번호 등)를 절대 포함하지 말 것.
- 변경 후 회귀 확인: 종전에 PASS였던 케이스가 이번 변경으로 FAIL/REVIEW로 바뀌는
  회귀가 없는지 diff 형태로 보고할 것.
- 작업 순서: **Phase 1 단독 커밋 → 검증 → Phase 2 커밋 → 검증 → Phase 3**.
  Phase 1만으로도 REVIEW 대량 감소 효과가 있으므로 먼저 배포 가능한 단위로 분리한다.
