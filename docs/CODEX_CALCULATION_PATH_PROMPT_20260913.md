# Codex 전달용 프롬프트: 계산 경로 검증과 구현 착수 (2026-09-13)

아래 블록을 그대로 Codex에 붙여 넣는다. 1부는 검증, 2부는 검증이 확인된 경우에만 진행하는 구현이다.

---

```text
PAI_LOOP 정량점수 조사의 방향을 검토한 Claude 문서를 반박·확인하고, 확인되는 범위에서 구현에 착수해 달라.

저장소: skytree96-cmyk/PAI_LOOP
읽을 문서(브랜치 docs/claude-quantitative-review-0913):
- docs/CLAUDE_CALCULATION_PATH_REVIEW_20260913.md   ← 이번 검토
- docs/CODEX_COVERAGE_VERIFICATION_RESULT_20260913.md ← 네가 작성한 커버리지 검증(로컬 worktree)
코드 기준: d817ce5. 네가 로컬에 가진 미커밋 원문 범위 구현은 별도 worktree에 그대로 두고 건드리지 마라.

공통 제약:
- 유료 모델 호출 0회, 새 다운로드 0회, 운영 DB·n8n·Render 접근 0회.
- 0.5.5 허용, 구기록 최신 계약 재도장, 커버리지·원문·합계 검증의 통째 완화는 금지.
- 회사 자료·원문 인용·비밀값을 보고서나 테스트에 넣지 마라. 합성(SYN) 입력만 커밋한다.
- 각 항목에 "코드로 확인 / private 자료로 확인 / 확인 불가"를 표시하고 틀린 주장을 먼저 써라.
- push·PR·병합·배포는 하지 마라. 로컬 브랜치와 보고서 파일까지만 만든다.

================ 1부. 검증 (결과는 표로) ================

Claude의 핵심 주장:
 (가) 사람이 만든 curated 프로필 경로도 모델 커버리지에 잠겨 있다.
     _profile_for_notice → _current_authoritative_document_state(quantitative_scoring.py:4184-4283)가
     _current_manifest_attempts(versions)(validate_accepted 기본 True)로 전 첨부의 유효 정량 기록을 요구한다.
 (나) 활성 판정이 공고 단위 all-or-nothing이다. _profile_activation_reasons(3656-3784)의 행 단위 사유
     (FACT_DIMENSIONS_UNMODELED, UNSUPPORTED_UNIT, UNIT_NOT_SOURCE_BOUND, BOUND_UNIT_INCONSISTENT,
     FACT_KEY_UNREGISTERED, FACT_EVIDENCE_KEY_UNREGISTERED, UNSUPPORTED_SCORING_DSL,
     BRACKETS_NOT_EXHAUSTIVE_OR_OVERLAPPING, SOURCE_ANCHOR_INCOMPLETE)가 하나라도 있으면
     quantitative_request_from_candidate_profile(3949-3960)이 criteria=[]로 REVIEW_REQUIRED를 돌려준다.
     _partial_profile_review_criteria는 profile.status=="REVIEW"이고 표 1개일 때만 동작하므로(3796)
     AVAILABLE 프로필의 행 단위 실패는 구제되지 않는다.
 (다) 실적 행의 scope 파서 parse_performance_recognition_scope(quantitative_performance.py:362-405)는
     최근 N년 + 유사범위 키워드(또는 수동 조건) + 완료 문구 + 단일 기준일이 모두 있어야 값을 돌려준다.
     하나라도 없으면 None → (나)에 의해 공고 전체 차단.
 (라) 회사 사실 결합 hash _candidate_fact_binding_sha256(2540)은 추출 후보 dump + document_sha256이다.
     재추출로 후보 문구가 바뀌면 private_company_evidence로 등록한 신용등급 fact가 풀린다.
 (마) 신용등급 등록은 _credit_rating_binding_for_notice(private_company_evidence.py:150-183)가
     공고당 신용 항목 정확히 1개를 요구한다.
 (바) 합계에서 unscorable_points(quantitative_scoring.py:1416-1422)가 "원문 규칙 미확정(review 행)"과
     "회사 증빙 없음(UNSCORABLE)"을 합친다.
 (사) 재무비율 파생은 항상 ESTIMATED(quantitative_financial.py:69-72)이고 3개 비율만 지원한다.

검증 항목:
 1. [코드] (가)~(사) 각각 파일:행을 재확인하고 반례를 찾아라. 특히 (가)는 curated 프로필이 실제로
    전 첨부 유효 기록 없이 활성화되는 경로가 있는지, (나)는 AVAILABLE 프로필에서 행 단위 사유만으로
    PARTIAL_ACTIVE가 되는 경로가 있는지 찾아라.
 2. [private] 이전 19공고 + 고정 30공고 재생 출력에서, 프로필이 AVAILABLE 또는 REVIEW인데
    activation_reasons에 (나)의 행 단위 코드만 있는 공고 수를 세라. 이것이 과제 1의 직접 대상이다.
    커버리지 사유(CURRENT_ATTACHMENT_COVERAGE_INCOMPLETE)와 섞인 공고는 따로 세라.
 3. [private] 같은 출력에서 metric이 PERFORMANCE_COUNT/AMOUNT인 available 후보 중
    parse_performance_recognition_scope가 None을 돌려주는 후보 수와, None의 이유
    (lookback 없음 / 키워드 없음 / 완료 문구 없음 / 기준일 충돌 / 최소금액 힌트만 있음)를 분류하라.
    원문 인용 없이 개수만.
 4. [private] 네가 구현한 원문 범위 보존 코드가 있는 worktree에서 같은 19+30 재생을 돌려,
    d817과 비교해 활성 수 외에 "행 단위 사유만 남은 공고 수"가 달라졌는지 적어라.
    달라지지 않았다면 그 구현이 과제 1·2와 독립임을 확인하는 뜻이다.
 5. [판단] Claude의 과제 순서(1 행 단위 강등 → 2 사람 확인 규칙 입력 → 3 측정·표시 분리, 재추출은 그 뒤)를
    반박하라. 특히 과제 2가 "사람이 검토한 첨부 목록 = manifest"를 커버리지 대체로 쓰는 것이
    안전한지, 대체표(alternative table) 위험을 어떻게 막을지 답하라.

보고서: docs/CODEX_CALCULATION_PATH_VERIFICATION_20260913.md (커밋하지 마라).

================ 2부. 구현 착수 (1부에서 (가)(나)(다)가 확인된 경우에만) ================

새 로컬 브랜치 feat/quantitative-row-level-activation-0913 (d817ce5 기준, 미커밋 원문 범위 worktree와 분리).
아래 순서로 구현하고 각 단계마다 집중 테스트를 돌려라. push 금지.

 A. 과제 1: 행 단위 활성 사유 강등
    - _profile_activation_reasons를 공고 사유(CURRENT_ATTACHMENT_COVERAGE_INCOMPLETE,
      SOURCE_VALIDATION_ISSUES_PRESENT, FACT_KEY_AMBIGUOUS, 논리 프로그램 사유)와
      행 사유로 분리하는 순수 함수를 추가한다. 기존 함수의 반환값(정렬된 코드 목록)은 유지한다.
    - quantitative_request_from_candidate_profile에서 공고 사유가 없고 행 사유만 있으면
      해당 행을 QuantitativeReviewCriterion(issue_codes=행 사유)으로 옮기고 PARTIAL_ACTIVE로 낸다.
      나머지 행은 기존 변환 그대로. 표 총점 검증(candidate_total == table.total_points)은 유지한다.
    - _partial_profile_review_criteria의 단일 표 조건을 "review 행을 가진 표는 하나, 나머지 표는
      AVAILABLE이며 기계 프로필이 활성 사유 없음"으로 완화한다. 동일 소계의 대체표가 있으면 거절한다.
    - 금지: review 행에 점수를 넣는 것, 공고 사유가 있는데 부분 활성하는 것, 총점 검증 완화.
    - 테스트: 합성으로 (신용 CASE 행 + 완료 문구 없는 실적 행) → PARTIAL_ACTIVE, 신용 CONFIRMED,
      실적 REVIEW 0~만점. 2표(정량표 + 빈 AVAILABLE 설문표) → PARTIAL 유지. 커버리지 미완 → 여전히
      REVIEW_REQUIRED. test_quantitative_auto_activation.py, test_quantitative_partial_activation.py,
      test_quantitative_out_of_scope.py, test_quantitative_public_snapshot.py, test_analysis_pipeline.py,
      test_notice_quantitative_frontend.py, test_frontend_public_contract.py를 돌려 결과를 적어라.
    - 재생: 고정 19+30 입력으로 d817 대비 activation_counts를 비교하고 AUTO_ACTIVE 오탐이 0인지 확인.

 B. 과제 3(측정 부분만): 재생 CLI에 단계 깔때기 출력 추가
    - scripts/replay-quantitative-sources.py 집계에 공고별 (profile.status, activation_status,
      len(criteria), len(review_criteria), 항목 status 분포)를 더하고 aggregate에 깔때기 표를 넣는다.
    - --golden <json> 옵션: 사례별 기대 (criterion_id → points/status, 소계)를 받아 일치율을 출력한다.
      golden 파일 형식은 tests/test_quantitative_replay_cli.py에 SYN 예제로 문서화한다.
    - 외부 효과 guard(no_external_effects)와 원본 불변 단언은 그대로 유지한다.

 C. 과제 2(설계만): 사람 확인 규칙 입력 경로
    - 구현하지 말고 설계 문서 docs/VERIFIED_RULE_INPUT_DESIGN_20260913.md를 써라:
      모델 필드(notice_key, attachment_id, document_sha256, manifest_sha256, reviewed_attachment_ids,
      criteria[QuantitativeCriterion 형태], performance_scope 명시 필드, 검토자, 검토 시각),
      binding hash 계산식(검증 규칙 dump + document_sha256), estimate_for_notice 우선순위,
      _profile_for_notice의 커버리지 요구 대체 규칙, 공개 투영 라벨, 대체표 위험 대응,
      인용 검증이 가능한 시점(재다운로드 시)과 그 전의 attestation 처리.
    - 사용자의 기대값 3건이 들어오면 SYN fixture로 재현 테스트를 붙일 수 있게 골격만 잡아라.

마지막에 브랜치 상태(git status --short, git log --oneline -5)와 테스트 결과 요약을 보고서 끝에 붙여라.
```
