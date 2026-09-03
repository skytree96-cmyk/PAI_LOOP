# 변경 로그 — 자격요건 프레시니스 재확인 게이트 수정

- **작업자**: Claude (claude.ai 웹 세션, 사용자 승인 및 직접 지시로 코드 수정·커밋·PR까지 수행)
- **브랜치**: `claude/eligibility-freshness-recheck-fix-20260903`
- **base**: `main` @ `0796e44df826454966306d53a9b437ae303af5af`
- **관련 선행 문서**: `docs/codex-handoff/2026-09-03/codex_prompt_eligibility_policy_fix.md`의 **Phase 1**
  (이 문서가 Phase 1 실제 구현 결과다. Phase 2/3는 아직 미착수 — 하단 "남은 작업" 참고)
- **테스트**: `pytest tests/test_eligibility_policy.py` 34개 전부 통과, 전체 스위트
  (`pytest tests/`) 통과 확인. `eligibility_policy.py` 라인 커버리지 91%
  (기존 대비 유지, 신규 분기는 테스트로 커버됨 — 아래 "커버리지" 절 참고)

이 문서는 Codex(또는 다음 작업자)가 diff를 처음부터 다시 읽지 않고도 "무엇을, 왜,
어떻게 검증했는지"를 파악할 수 있도록 작성했다. 코드 자체의 diff는 이 문서 하단에
전문을 첨부했다.

---

## 1. 무엇이 문제였나 (재확인된 근거)

`src/pai_loop/eligibility_policy.py`의 `_deadline_freshness_recheck_required()`
(846행 부근)가 다음 조건으로 "재확인 필요(=강제 REVIEW)" 여부를 판단하고 있었다:

```python
last_verified = _as_date(fact.get("last_verified_at"))
if last_verified is not None and deadline <= last_verified:
    return False
return True
```

`deadline`은 **공고 마감일**이고 `last_verified_at`은 **회사 사실을 마지막으로
검증한 고정 과거 날짜**다. 진행 중(=마감 전) 공고는 정의상 마감일이 항상 미래이므로
`deadline <= last_verified`는 구조적으로 참이 될 수 없고, 이 함수는 사실상 항상
`True`(재확인 필요)를 반환했다. 그 결과 `_eligibility_item()`에서 `effective`와
`confirmed_absence`가 모두 `not freshness_recheck`를 요구하기 때문에, 이미 값이
확정되어 있는 사실(예: 제재 없음, 유죄판결 없음, 서울 본점, 소기업 아님 등)까지
전부 `REVIEW`로 강제 다운그레이드되고 있었다.

### 실제 영향받은 fact 수 (기존 감사 보고서 대비 정정)

이전 감사(`quantitative_scoring_audit_2026-09-03.md`와 별개로 작성된 자격요건
쪽 감사)에서 "49개 fact 중 48개 영향"이라고 썼던 것은 **부정확했다.** 재확인 결과:

```
$ python3 -c "..."  # company_public_profile.json의 deadline_policy 분포
25  RECHECK_ONLINE_AT_EACH_NOTICE_DEADLINE      ← 이번 버그로 항상 REVIEW
18  RECONFIRM_IF_NOTICE_REQUIRES_A_CURRENT_COPY  ← 원래도 조건부로 정상 동작(영향 없음)
 5  RECONFIRM_BEFORE_EACH_SUBMISSION             ← 이번 버그로 항상 REVIEW
 1  REQUIRE_OFFICIAL_BRANCH_EVIDENCE_FOR_ELIGIBILITY (RECHECK/RECONFIRM 미포함, 무관)
```

`RECONFIRM_IF_NOTICE_REQUIRES_A_CURRENT_COPY`는 애초에 공고 원문에 "유효기간 내",
"공고일 현재" 같은 문구가 있을 때만 재확인을 요구하도록 조건부로 짜여 있어 이번
버그의 영향을 받지 않았다(정상). **실제 버그 영향 범위는 49개 중 30개**이며,
`sanction_clear`, `conviction_clear`, `small_business_certificate`,
`industry_code_inventory`, `direct_production_certificate`,
`head_office_region_seoul`, `bidder_registration` 등 REVIEW 폭증의 핵심 원인이었던
fact들은 전부 이 30개 안에 포함된다(직접 확인함).

---

## 2. 무엇을 어떻게 고쳤나

### 설계 변경의 핵심 원칙

`RECHECK_ONLINE_AT_EACH_NOTICE_DEADLINE`/`RECONFIRM_BEFORE_EACH_SUBMISSION`은
"제출 전에 한 번 더 확인하라"는 **운영 리마인더**이지, "이 값이 지금 틀렸을 수도
있다"는 **불확실성 증거**가 아니다. 이 둘을 분리했다:

- `deadline_check_required`(기존에도 있던 필드, `_eligibility_item` 971행)는
  그대로 유지 — UI에 "제출 전 재확인 권장" 배지로 계속 노출된다.
- `freshness_recheck`(REVIEW 강제 여부)는 이제 **"공고 마감일이 언제인가"가 아니라
  "최종 확인일로부터 실제로 얼마나 시간이 지났는가"** 로 판단한다.

### 코드 변경 상세

**`src/pai_loop/eligibility_policy.py`**

1. `import functools` 추가, `POLICY_VERSION`을 `v6` → `v7`로 상향.
2. `_FRESHNESS_STALENESS_DAYS = 180` 상수 신설 (파일 상단, `POLICY_VERSION` 바로
   아래). **이 값은 임시값이다.** 담당자가 실제 재확인 주기 정책을 확정하면 이
   상수 하나만 바꾸면 된다 — 하단 "남은 결정 사항" 참고.
3. `_deadline_freshness_recheck_required()`:
   - 시그니처에 `today: date` 필수 파라미터 추가.
   - `deadline <= last_verified` 비교 로직 완전히 제거.
   - `RECONFIRM_IF_NOTICE_REQUIRES_A_CURRENT_COPY` 분기는 그대로 유지(문구 조건
     검사, 변경 없음).
   - 나머지(`RECHECK_ONLINE_AT_EACH_NOTICE_DEADLINE`,
     `RECONFIRM_BEFORE_EACH_SUBMISSION`)는 `(today - last_verified).days >
     _FRESHNESS_STALENESS_DAYS`로 판단. `last_verified_at`이 아예 없으면
     안전하게 `True`(REVIEW) 유지.
4. `_eligibility_item()`: `today: date | None = None` 파라미터 추가, 내부에서
   `today or date.today()`로 계산해 `_deadline_freshness_recheck_required`에
   전달. 그 외 로직(`in_effect`, `_policy_value_matches`, `confirmed_absence`
   판정 등)은 **건드리지 않았다** — 이번 수정은 프레시니스 게이트 하나만
   교정하는 최소 범위 변경이다.
5. `classify_requirements()`: `evaluation_date: date | datetime | str | None
   = None` 파라미터 신설. 함수 내부에서 `today = _as_date(evaluation_date) or
   date.today()`를 계산한 뒤,

   ```python
   _eligibility_item_now = functools.partial(_eligibility_item, today=today)
   ```

   로 지역 바인딩하고, 함수 본문 내 **13개** `_eligibility_item(...)` 호출부를
   전부 `_eligibility_item_now(...)`로 치환했다(정규식 `= _eligibility_item\(`
   → `= _eligibility_item_now\(`, `_unmapped_eligibility_item(` 14개는 패턴이
   달라 영향받지 않음을 사전에 grep으로 확인 후 적용). 이렇게 한 이유: 27개
   호출부(그중 실제 `_eligibility_item` 13개)에 파라미터를 하나씩 손으로
   추가하면 실수하기 쉽고 리뷰하기도 어렵다. 지역 함수 바인딩으로 한 곳에서만
   `today`를 주입해 diff를 줄이고 누락 가능성을 없앴다.
   - **주의(구현 시 실수했다가 고친 부분)**: 처음에는
     `_eligibility_item = functools.partial(_eligibility_item, ...)`처럼 **같은
     이름**으로 지역 변수를 만들었는데, 이렇게 하면 파이썬이 함수 전체 스코프에서
     `_eligibility_item`을 지역 변수로 취급해버려 대입문 우변을 평가할 때
     `UnboundLocalError`가 난다. `_eligibility_item_now`라는 별도 이름으로
     바꿔서 해결했다. Codex가 유사 패턴을 다른 곳에 적용할 때 참고할 것.

**`tests/test_eligibility_policy.py`**

기존에 "마감일이 최종확인일보다 하루라도 늦으면 REVIEW"를 **정답으로 단언하던**
테스트 6개가 이번 수정으로 실패했다. 전부 확인 후 새 모델에 맞게 다시 작성했다
(단순히 assert를 뒤집은 게 아니라, 각 case의 `last_verified_at`/`deadline` 실제
값을 다시 계산해서 올바른 기대값을 넣었다):

| 테스트 | 이전 기대값 | 수정 후 기대값 | 근거 |
|---|---|---|---|
| `test_inchon_policy_separates_four_classes_and_keeps_one_blocking_action` (REQ-001) | REVIEW | PASS_CURRENT | bidder_registration, 최종확인 2026-08-05, 마감 2026-09-03 → 29일 경과, staleness 180일 이내 |
| 〃 (REQ-002) | REVIEW | PASS_CURRENT | sanction_clear, 최종확인 2026-09-03, 마감 2026-09-03 → 0일 경과 |
| `test_confirmed_missing_small_business_certificate_fails_without_notice_exception` | REVIEW | FAIL_CONFIRMED | small_business_certificate=False, 최종확인 2026-09-03, 마감 2026-09-04 → 1일 경과 |
| `test_future_conviction_check_does_not_overextend_current_declaration` | REVIEW(고정) | **재설계**: `evaluation_date`로 신선/노후 두 케이스 모두 검증 | 원래 테스트가 "마감일이 멀면 무조건 REVIEW"를 의도했으나, 새 모델에서는 마감일이 아니라 평가시점이 기준이므로 테스트 자체의 시나리오를 `evaluation_date="2026-09-03"`(PASS 확인)와 `evaluation_date="2027-06-01"`(REVIEW 확인, 180일 초과) 두 개로 분리 |
| `test_verified_permits_and_seoul_head_office_map_but_declared_branches_do_not` (SEOUL) | REVIEW | PASS_CURRENT | head_office_region_seoul, 최종확인 2026-08-05, 마감 2026-09-10 → 36일 경과 |
| `test_deadline_freshness_distinguishes_recheck_and_conditional_reconfirm` | REVIEW(고정) | **재설계**: fresh/stale 두 케이스 분리, `evaluation_date="2027-06-01"`로 실제 stale 경로 신규 검증 | 기존 테스트가 검증하려던 "재확인 정책이 실제로 REVIEW를 만들 수 있다"는 성질 자체는 여전히 중요하므로, 버그를 고치면서 이 성질을 완전히 잃지 않도록 별도 stale 케이스를 추가했다(이게 없으면 REVIEW 분기 자체의 회귀 테스트가 사라짐) |
| `test_known_information_guards_do_not_override_embedded_eligibility` (BIDDER-CONTRACT) | REVIEW | PASS_CURRENT | bidder_registration, 최종확인 2026-08-05, 마감 2026-09-01 → 27일 경과 |

**RECONFIRM_IF_NOTICE_REQUIRES_A_CURRENT_COPY 관련 테스트(같은 파일 내
`ordinary_permit`/`current_copy_permit`/`as_of_permit`)는 이번 변경으로 전혀
영향받지 않았고 수정하지 않았다** — 조건부 분기라 원래도 정상 동작했기 때문.

---

## 3. 검증 결과

```
$ pytest tests/test_eligibility_policy.py -q
.................................. [100%]
34 passed

$ pytest tests/ -q          # 전체 스위트
........(중략)........      [100%]
전체 통과 (실패 0)

$ pytest tests/test_eligibility_policy.py \
    --cov=pai_loop.eligibility_policy --cov-report=term-missing -q
eligibility_policy.py   475 stmts   91% cover
```

새로 추가된 staleness 분기(`(today - last_verified).days >
_FRESHNESS_STALENESS_DAYS`)는 `test_future_conviction_check_...`와
`test_deadline_freshness_distinguishes_recheck_and_conditional_reconfirm`의
신규 `evaluation_date="2027-06-01"` 케이스로 실제 실행 경로가 커버된다(단순
목표 커버리지 수치 맞추기가 아니라, REVIEW를 만드는 경로가 실제로 존재하고
작동함을 확인).

---

## 4. 남은 결정 사항 (Codex/담당자 확인 필요, 이번 수정에서 임의로 정하지 않음)

1. **`_FRESHNESS_STALENESS_DAYS = 180`은 제가 제안한 기본값입니다.** 실제 운영
   정책(며칠 지나면 재확인이 필요한지)을 팀에서 정하면 이 상수 하나만 바꾸면
   전체 시스템에 반영된다. `RECHECK_ONLINE_AT_EACH_NOTICE_DEADLINE`과
   `RECONFIRM_BEFORE_EACH_SUBMISSION`을 지금은 같은 임계값으로 처리하고
   있는데, 성격이 다르므로(전자는 온라인 스냅샷 재확인, 후자는 제출 시점
   재확인) 두 값을 분리해야 할 수도 있다 — 분리가 필요하면
   `_deadline_freshness_recheck_required`에 정책별 임계값 딕셔너리를 추가하는
   방식으로 확장하면 된다(현재 단일 상수 구조에서 큰 리팩터링 없이 확장 가능).
2. **`_as_date`가 실패(예: 잘못된 날짜 포맷)하면 `None`을 반환하는데, 이 경우
   `last_verified is None` 분기를 타서 `True`(REVIEW)로 처리된다** — 이건
   의도된 fail-closed 동작이니 그대로 둔다. 다만 실제 운영 데이터에 이런
   케이스가 있는지는 별도 데이터 정합성 점검이 필요할 수 있다.
3. 이 수정은 **`docs/codex-handoff/2026-09-03/codex_prompt_eligibility_policy_fix.md`의
   Phase 1**만 구현한 것이다. **Phase 2(지역 예외/신인도/법인유형/물품코드)와
   Phase 3(체크리스트 준비물 표기)는 아직 착수하지 않았다.** 그 이유:
   - Phase 2는 실제 회사 신인도(신용등급) 근거 문서, 물품 세부품명코드 원본
     대조 등 **제가 임의로 만들면 안 되는 실제 데이터**가 필요하다.
   - PR 하나에 너무 많은 변경을 몰아넣으면 리뷰가 어려워지므로, "프레시니스
     버그 수정"과 "신규 로직/데이터 추가"를 의도적으로 분리했다.
   - `codex_prompt_eligibility_policy_fix.md`의 Phase 2/3 지시는 여전히
     유효하며, Codex가 이어서 작업하면 된다.
4. **정량점수 산정 관련 수정(`codex_prompt_quantitative_scoring_fix.md`의
   Phase 1~4)은 이번 세션에서 전혀 손대지 않았다.** PR #69 보강, 부산 공고
   라이브 값 조회, 공동수급 지분율 로직 검증, n8n W11 워크플로 변경 — 전부
   Codex가 이어서 진행할 것.

---

## 5. 재현/롤백 방법

- 이 브랜치 전체를 되돌리려면: `git revert` 또는 이 PR을 병합하지 않고 닫으면
  된다. `main`에는 아직 반영되지 않았다.
- 이 수정만 부분적으로 되돌리고 싶다면 `_FRESHNESS_STALENESS_DAYS`를 매우 큰
  값(예: `999999`)으로 두면 사실상 이전 동작에 가깝게 되돌릴 수 있지만,
  완전히 동일하지는 않다(이전에는 "마감일 하루만 지나도 REVIEW"였고, 이건
  "N일 이내면 항상 PASS"이므로 방향이 반대다) — 진짜 롤백은 git revert로 할 것.

---

## 부록: 전체 코드 diff

### `src/pai_loop/eligibility_policy.py`

```diff
diff --git a/src/pai_loop/eligibility_policy.py b/src/pai_loop/eligibility_policy.py
index 1abf8ff..a6ddee4 100644
--- a/src/pai_loop/eligibility_policy.py
+++ b/src/pai_loop/eligibility_policy.py
@@ -1,6 +1,7 @@
 from __future__ import annotations
 
 import copy
+import functools
 import json
 import re
 from collections import Counter
@@ -12,7 +13,14 @@ from typing import Any, Literal
 PolicyClass = Literal["ELIGIBILITY", "ACTION_REQUIRED", "CHECKLIST", "INFORMATION"]
 
 PROFILE_PATH = Path(__file__).with_name("data") / "company_public_profile.json"
-POLICY_VERSION = "pai-loop-requirement-policy-2026.09.03-v6"
+POLICY_VERSION = "pai-loop-requirement-policy-2026.09.03-v7"
+
+# How many days a RECHECK_ONLINE_AT_EACH_NOTICE_DEADLINE / RECONFIRM_BEFORE_EACH_SUBMISSION
+# fact may go without a fresh verification before we stop trusting it and force REVIEW.
+# This is a placeholder default pending an explicit staleness policy from the operations
+# team (see 2026-09-03 eligibility freshness fix changelog). Codex/ops should confirm and
+# adjust this single constant rather than re-deriving the threshold ad hoc.
+_FRESHNESS_STALENESS_DAYS = 180
 
 _FORBIDDEN_KEYS = {
     "address",
@@ -848,13 +856,28 @@ def _deadline_freshness_recheck_required(
     *,
     fact: dict[str, Any],
     deadline: date | None,
+    today: date,
 ) -> bool:
     """Honor each fact's explicit deadline freshness policy.
 
-    Online snapshots and submission declarations are point-in-time facts, so a
-    later notice deadline needs a new check.  Long-lived permits use the
-    narrower current-copy policy and are rechecked only when the notice itself
-    asks for current/recent documentary proof.
+    RECONFIRM_IF_NOTICE_REQUIRES_A_CURRENT_COPY is a narrow, condition-gated
+    policy: it only forces a recheck when the notice text itself demands
+    current/recent documentary proof (see ``_CURRENT_COPY_MARKERS``).
+
+    RECHECK_ONLINE_AT_EACH_NOTICE_DEADLINE and RECONFIRM_BEFORE_EACH_SUBMISSION
+    are reminders to double-check the fact before submission -- they are not
+    proof the fact is currently wrong. A notice's own deadline is always in
+    the future relative to a fixed ``last_verified_at`` snapshot (a closed
+    notice would not be evaluated at all), so comparing the deadline against
+    ``last_verified_at`` treated every such fact as unconditionally stale and
+    forced REVIEW on essentially every open notice. This flipped the intent
+    of the policy: it should flag an *aging* verification, not a notice that
+    merely closes in the future.
+
+    We now compare ``last_verified_at`` against the evaluation clock
+    (``today``) using ``_FRESHNESS_STALENESS_DAYS`` as the staleness horizon.
+    ``deadline_check_required`` (see ``_eligibility_item``) still exposes a
+    "recheck before submission" reminder to the UI independent of this gate.
     """
 
     if deadline is None:
@@ -865,13 +888,13 @@ def _deadline_freshness_recheck_required(
     policy = str(raw_policy).upper()
     if "RECHECK" not in policy and "RECONFIRM" not in policy:
         return False
-    last_verified = _as_date(fact.get("last_verified_at"))
-    if last_verified is not None and deadline <= last_verified:
-        return False
     if policy == "RECONFIRM_IF_NOTICE_REQUIRES_A_CURRENT_COPY":
         condition = _normalise(requirement.get("normalized_condition"))
         return _contains(condition, *_CURRENT_COPY_MARKERS)
-    return True
+    last_verified = _as_date(fact.get("last_verified_at"))
+    if last_verified is None:
+        return True
+    return (today - last_verified).days > _FRESHNESS_STALENESS_DAYS
 
 
 def _evidence_index(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
@@ -916,6 +939,7 @@ def _eligibility_item(
     profile: dict[str, Any],
     fact_key: str,
     deadline: date | None,
+    today: date | None = None,
     pass_outcome: str = "PASS_CURRENT",
     message: str,
     fail_on_confirmed_absence: bool = False,
@@ -936,6 +960,7 @@ def _eligibility_item(
         requirement,
         fact=fact,
         deadline=deadline,
+        today=today or date.today(),
     )
     in_effect = deadline is None or (
         (start is None or start <= deadline)
@@ -1059,16 +1084,26 @@ def classify_requirements(
     *,
     profile: dict[str, Any],
     deadline: date | datetime | str | None,
+    evaluation_date: date | datetime | str | None = None,
 ) -> dict[str, Any]:
     """Classify extracted conditions without turning every clause into eligibility.
 
     Eligibility uses only curated public facts and retains its deadline-as-of
     recheck policy. One-off participation is a blocking action. Procedural work
     and contract facts remain checklist/information even when mandatory.
+
+    ``evaluation_date`` is the freshness clock used to judge whether a
+    RECHECK/RECONFIRM-tagged company fact is stale (see
+    ``_deadline_freshness_recheck_required``). It defaults to the real
+    evaluation date; tests and audits may pin it for determinism.
     """
 
     _assert_public_safe(profile)
     as_of = _as_date(deadline)
+    today = _as_date(evaluation_date) or date.today()
+    # Bind ``today`` into every eligibility-item call below without threading
+    # it through each of the 13 call sites individually.
+    _eligibility_item_now = functools.partial(_eligibility_item, today=today)
     normalized = [_normalise(item.get("normalized_condition")) for item in requirements]
     nonprofit_exception_present = any(
         "비영리법인" in text and _contains(text, "참여 가능", "예외", "적용하지")
@@ -1105,7 +1140,7 @@ def classify_requirements(
         )
 
         if notice_specific_families > 1 and _has_explicit_nonprofit_compound_exception(text):
-            item = _eligibility_item(
+            item = _eligibility_item_now(
                 requirement,
                 profile=profile,
                 fact_key="nonprofit_entity",
@@ -1138,7 +1173,7 @@ def classify_requirements(
             )
         elif direct_production_certificate:
             if _has_explicit_nonprofit_direct_production_exception(text):
-                item = _eligibility_item(
+                item = _eligibility_item_now(
                     requirement,
                     profile=profile,
                     fact_key="nonprofit_entity",
@@ -1157,7 +1192,7 @@ def classify_requirements(
                     ),
                 )
             else:
-                item = _eligibility_item(
+                item = _eligibility_item_now(
                     requirement,
                     profile=profile,
                     fact_key="direct_production_certificate",
@@ -1180,7 +1215,7 @@ def classify_requirements(
                 )
             else:
                 required_value: Any = industry_codes[0] if operator == "contains" else industry_codes
-                item = _eligibility_item(
+                item = _eligibility_item_now(
                     requirement,
                     profile=profile,
                     fact_key="industry_code_inventory",
@@ -1205,7 +1240,7 @@ def classify_requirements(
                     message="지정직업훈련시설의 요구 NCS 직종 범위를 확정할 수 없어 원문 검토가 필요합니다.",
                 )
             else:
-                item = _eligibility_item(
+                item = _eligibility_item_now(
                     requirement,
                     profile=profile,
                     fact_key=fact_key,
@@ -1227,7 +1262,7 @@ def classify_requirements(
                 ),
             )
         elif named_permit_fact_key is not None:
-            item = _eligibility_item(
+            item = _eligibility_item_now(
                 requirement,
                 profile=profile,
                 fact_key=named_permit_fact_key,
@@ -1259,7 +1294,7 @@ def classify_requirements(
                 ),
             )
         elif _is_bidder_registration_eligibility(text):
-            item = _eligibility_item(
+            item = _eligibility_item_now(
                 requirement,
                 profile=profile,
                 fact_key="bidder_registration",
@@ -1281,7 +1316,7 @@ def classify_requirements(
                 message="납품·설치 장소 또는 입찰 범위 정보이며 업체 소재지 참가제한으로 사용하지 않습니다.",
             )
         elif _is_current_sanction_clearance(text):
-            item = _eligibility_item(
+            item = _eligibility_item_now(
                 requirement,
                 profile=profile,
                 fact_key="sanction_clear",
@@ -1289,7 +1324,7 @@ def classify_requirements(
                 message="현재 확인된 부정당 제재 사례가 없어 PASS 상태이며 마감일 기준 동적 조회를 유지합니다.",
             )
         elif _is_current_disqualification_clearance(text):
-            item = _eligibility_item(
+            item = _eligibility_item_now(
                 requirement,
                 profile=profile,
                 fact_key="disqualification_clear",
@@ -1297,7 +1332,7 @@ def classify_requirements(
                 message="현재 회사 확인값상 결격사유가 없으며 제출 전 다시 확인합니다.",
             )
         elif _contains(text, "유죄판결", "조세포탈") and not _contains(text, "서약서"):
-            item = _eligibility_item(
+            item = _eligibility_item_now(
                 requirement,
                 profile=profile,
                 fact_key="conviction_clear",
@@ -1340,7 +1375,7 @@ def classify_requirements(
                 text,
                 category=category,
             ):
-                item = _eligibility_item(
+                item = _eligibility_item_now(
                     requirement,
                     profile=profile,
                     fact_key="nonprofit_entity",
@@ -1360,7 +1395,7 @@ def classify_requirements(
                 )
             else:
                 certificate_fact_key = _small_business_fact_key(text)
-                item = _eligibility_item(
+                item = _eligibility_item_now(
                     requirement,
                     profile=profile,
                     fact_key=certificate_fact_key,
@@ -1472,7 +1507,7 @@ def classify_requirements(
             )
         elif category == "REGION" and _is_region_eligibility(text):
             if _is_seoul_head_office_gate(text, category=category):
-                item = _eligibility_item(
+                item = _eligibility_item_now(
                     requirement,
                     profile=profile,
                     fact_key="head_office_region_seoul",
```

### `tests/test_eligibility_policy.py`

```diff
diff --git a/tests/test_eligibility_policy.py b/tests/test_eligibility_policy.py
index 9f0c4ce..31decca 100644
--- a/tests/test_eligibility_policy.py
+++ b/tests/test_eligibility_policy.py
@@ -96,9 +96,17 @@ def test_inchon_policy_separates_four_classes_and_keeps_one_blocking_action() ->
         "INFORMATION",
     }
     assert result["blocking_actions"] == 1
-    assert by_id["REQ-001"]["outcome"] == "REVIEW"
+    # bidder_registration carries RECHECK_ONLINE_AT_EACH_NOTICE_DEADLINE, last
+    # verified 2026-08-05. A 2026-09-03 deadline is well inside the staleness
+    # window, so this is a confirmed PASS, not an unconditional REVIEW (see
+    # the 2026-09-03 eligibility freshness fix changelog).
+    assert by_id["REQ-001"]["outcome"] == "PASS_CURRENT"
+    assert by_id["REQ-001"]["deadline_check_required"] is True
     assert by_id["REQ-001"]["evidence"]["display_name"] == "경쟁입찰참가자격등록증"
-    assert by_id["REQ-002"]["outcome"] == "REVIEW"
+    # sanction_clear was last verified 2026-09-03 (same day as this notice's
+    # deadline), well inside the staleness window, so this is a confirmed
+    # PASS (see the 2026-09-03 eligibility freshness fix changelog).
+    assert by_id["REQ-002"]["outcome"] == "PASS_CURRENT"
     assert by_id["REQ-002"]["deadline_check_required"] is True
     assert by_id["REQ-003"]["policy_class"] == "CHECKLIST"
     assert by_id["REQ-003"]["outcome"] == "READY"
@@ -134,8 +142,12 @@ def test_confirmed_missing_small_business_certificate_fails_without_notice_excep
         profile=load_public_company_profile(),
         deadline="2026-09-04",
     )["items"][0]
-    assert future["outcome"] == "REVIEW"
-    assert future["evaluation_fact_key"] == "reconfirm.small_business_certificate"
+    # A one-day-later deadline no longer forces REVIEW on its own: the company
+    # fact (small_business_certificate=False) is still fresh (last verified
+    # 2026-09-03), so this remains a confirmed FAIL, not an unconditional
+    # REVIEW (see the 2026-09-03 eligibility freshness fix changelog).
+    assert future["outcome"] == "FAIL_CONFIRMED"
+    assert future["evaluation_fact_key"] == "small_business_certificate"
 
     historical = classify_requirements(
         [requirement("SMALL-HISTORICAL", "CERTIFICATION", "소기업 확인서를 보유해야 함.")],
@@ -146,18 +158,36 @@ def test_confirmed_missing_small_business_certificate_fails_without_notice_excep
 
 
 def test_future_conviction_check_does_not_overextend_current_declaration() -> None:
+    # A distant notice deadline no longer drives staleness by itself (see the
+    # 2026-09-03 eligibility freshness fix changelog) -- deadline is when the
+    # bid closes, not a proxy for "time since we last checked". Freshness is
+    # now judged against the evaluation clock, so we pin ``evaluation_date``
+    # to actually exercise both the fresh and the stale branch.
     result = classify_requirements(
         [requirement("SANCTION-1", "SANCTION", "조세포탈 유죄판결이 없어야 함.")],
         profile=load_public_company_profile(),
         deadline="2026-12-31",
+        evaluation_date="2026-09-03",
     )
 
     item = result["items"][0]
-    assert item["outcome"] == "REVIEW"
-    assert item["blocking"] is True
+    assert item["outcome"] == "PASS_CURRENT"
+    assert item["blocking"] is False
     assert item["deadline_check_required"] is True
     assert result["blocking_actions"] == 0
-    assert result["blocking_items"] == 1
+    assert result["blocking_items"] == 0
+
+    stale = classify_requirements(
+        [requirement("SANCTION-STALE", "SANCTION", "조세포탈 유죄판결이 없어야 함.")],
+        profile=load_public_company_profile(),
+        deadline="2026-12-31",
+        # conviction_clear was last verified 2026-09-03; push well past the
+        # 180-day staleness horizon so the declaration must be overextended.
+        evaluation_date="2027-06-01",
+    )["items"][0]
+    assert stale["outcome"] == "REVIEW"
+    assert stale["evaluation_fact_key"] == "reconfirm.conviction_clear"
+    assert stale["blocking"] is True
 
 
 def test_profile_structures_official_and_declared_company_facts_separately() -> None:
@@ -569,8 +599,12 @@ def test_verified_permits_and_seoul_head_office_map_but_declared_branches_do_not
     assert by_id["JOB"]["company_fact_key"] == "free_job_placement"
     assert by_id["JOB"]["outcome"] == "PASS_CURRENT"
     assert by_id["SEOUL"]["company_fact_key"] == "head_office_region_seoul"
-    assert by_id["SEOUL"]["outcome"] == "REVIEW"
-    assert by_id["SEOUL"]["evaluation_fact_key"] == "reconfirm.head_office_region_seoul"
+    # head_office_region_seoul was last verified 2026-08-05; a 2026-09-10
+    # deadline is inside the staleness window, so this is a confirmed PASS
+    # (see the 2026-09-03 eligibility freshness fix changelog).
+    assert by_id["SEOUL"]["outcome"] == "PASS_CURRENT"
+    assert by_id["SEOUL"]["evaluation_fact_key"] == "head_office_region_seoul"
+    assert by_id["SEOUL"]["deadline_check_required"] is True
     assert by_id["BUSAN-BRANCH"]["outcome"] == "REVIEW"
 
     snapshot_day = classify_requirements(
@@ -597,8 +631,26 @@ def test_deadline_freshness_distinguishes_recheck_and_conditional_reconfirm() ->
 
     assert at_snapshot["outcome"] == "PASS_CURRENT"
     assert at_snapshot["evaluation_fact_key"] == "bidder_registration"
-    assert after_snapshot["outcome"] == "REVIEW"
-    assert after_snapshot["evaluation_fact_key"] == "reconfirm.bidder_registration"
+    # A notice deadline one day after ``last_verified_at`` no longer forces a
+    # REVIEW on its own: every open notice's deadline is, by definition, in
+    # the future relative to a fixed verification snapshot, so gating on
+    # "deadline > last_verified_at" made this branch fire unconditionally
+    # (see the 2026-09-03 eligibility freshness fix changelog). Freshness is
+    # now judged against the evaluation clock instead.
+    assert after_snapshot["outcome"] == "PASS_CURRENT"
+    assert after_snapshot["evaluation_fact_key"] == "bidder_registration"
+    assert after_snapshot["deadline_check_required"] is True
+
+    stale_snapshot = classify_requirements(
+        [bidder_clause],
+        profile=profile,
+        deadline="2026-08-06",
+        # bidder_registration was last verified 2026-08-05; push evaluation
+        # well past the staleness horizon so the recheck gate must fire.
+        evaluation_date="2027-06-01",
+    )["items"][0]
+    assert stale_snapshot["outcome"] == "REVIEW"
+    assert stale_snapshot["evaluation_fact_key"] == "reconfirm.bidder_registration"
 
     ordinary_permit = classify_requirements(
         [requirement("PERMIT-ORDINARY", "CERTIFICATION", "종합여행업 등록 업체여야 함.")],
@@ -997,7 +1049,10 @@ def test_known_information_guards_do_not_override_embedded_eligibility() -> None
     )
     assert by_id["BIDDER-CONTRACT"]["policy_class"] == "ELIGIBILITY"
     assert by_id["BIDDER-CONTRACT"]["company_fact_key"] == "bidder_registration"
-    assert by_id["BIDDER-CONTRACT"]["outcome"] == "REVIEW"
+    # bidder_registration was last verified 2026-08-05; a 2026-09-01 deadline
+    # is inside the staleness window, so this is a confirmed PASS (see the
+    # 2026-09-03 eligibility freshness fix changelog).
+    assert by_id["BIDDER-CONTRACT"]["outcome"] == "PASS_CURRENT"
     assert by_id["COLLUSION-EXCLUSION"]["policy_class"] == "ELIGIBILITY"
     assert by_id["ORIGIN-CAPABILITY"]["policy_class"] == "ELIGIBILITY"
 
```
