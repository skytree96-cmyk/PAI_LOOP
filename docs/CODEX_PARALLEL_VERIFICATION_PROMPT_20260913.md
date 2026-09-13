# Codex 병행 검증용 프롬프트 (2026-09-13)

아래 블록을 그대로 Codex에 붙여 넣는다. 지금 진행 중인 다른 작업과 **별도 세션 또는
별도 작업트리**에서 실행하도록 되어 있다. 유료 호출·운영 변경·push·병합은 요구하지 않는다.

---

```text
지금 하고 있는 작업은 계속 진행해도 된다. 이것은 별도의 읽기 전용 검증 요청이다.
새 작업트리(git worktree)에서 수행하고, 현재 작업트리와 진행 중인 커밋은 건드리지 않는다.

대상: PAI_LOOP 저장소, 브랜치 docs/claude-quantitative-review-0913
(코드 기준 d817ce5, 문서만 추가됨). 먼저 다음 두 문서를 읽어라.
- docs/CLAUDE_QUANTITATIVE_REVIEW_20260913.md  (Claude의 독립 검토)
- docs/CLAUDE_QUANTITATIVE_HANDOFF_20260913.md  (기존 인수인계서)

Claude의 검토는 기존 진단과 다른 결론을 냈다. 동의하는 요약이 아니라
**반박 또는 확인**을 해 달라. 각 항목마다 "코드로 확인 / private 자료로 확인 /
확인 불가"를 표시하고, 확인한 경우 파일:행 또는 자료 이름을 적어라.

제약:
- 유료 모델 호출 0회. 나라장터 다운로드도 하지 말고 이미 받은 원문만 쓴다.
- 운영 DB·n8n·Render 쓰기 0회. 읽기도 이번에는 하지 않는다. 로컬 private 파일만 쓴다.
- 코드 수정·push·PR 생성·병합 없음. 보고서 파일 1개만 만든다.
- 원문을 인용하지 말고 통계와 코드 위치만 적는다. 회사 자료·비밀값을 보고서에 넣지 않는다.

검증 항목 (순서대로, 결과는 표로):

1. [코드] OUT_OF_SCOPE 미도달 주장.
   quantitative_request_from_candidate_profile 이 _metric_spec 이 None 인 후보를
   criteria 에서 제외하는지, 그래서 엔진에 도달하는 동적 criterion 의 category 가 항상
   _CANONICAL_METRIC_REGISTRY 안인지 확인하라. 반례가 있으면 코드 경로를 제시하라.
   부록 A 스크립트를 실행하고 출력을 첨부하라.

2. [private 자료] 30공고의 첫 차단 계층 분해.
   scope_totals_final_offline_replay.private.json (또는 최신 동일 입력 재생 출력)에서
   공고별로 아래 순서의 첫 해당 계층을 하나만 배정하라.
     a. 커버리지: VALIDATED_RECORD_MISSING, ATTACHMENT_INCOMPLETE,
        EXTRACTION_CONTRACT_PROOF_INVALID, DOCUMENT/MANIFEST_BINDING_MISMATCH
     b. 누락 게이트: EXTRACTION_DECLARED_INCOMPLETE, ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT
     c. 표 메타: TABLE_TOTAL_MISMATCH, TABLE_TOTAL_INCOMPLETE, TABLE_TOTAL_LITERAL_MISMATCH,
        UNVERIFIED_TABLE_TOTAL_QUOTE
     d. 행 검증: *_LITERAL_MISMATCH, CASE_*, BRACKET_*, REQUIRED_EVIDENCE_INCOMPLETE,
        UNKNOWN_METRIC 등 criterion 단위 이슈
     e. 활성 사유만 남음: profile 은 AVAILABLE/REVIEW 인데 activation_reasons 로 막힘
   출력: 계층별 공고 수(합 30), 그리고 "manifest 전체 첨부에 현행 계약 시도가 있는
   공고 수". Claude 의 주장은 a 가 대부분이라는 것이다. 맞는지 숫자로 답하라.

3. [private 자료] 누락 선언 51건의 주어 분류.
   저장 raw 의 result.missing_or_unreadable 중 현재 asserts_scoring_artifact_absence 가
   True 를 주는 문장을 첨부 단위로 중복 제거해 모아라(51건 근처여야 한다).
   각 문장을 다음으로 분류하라(원문 인용 없이 개수만):
     - 정성/설문/평가위원 전용 (객관 주어 없음)
     - 가격 전용
     - 정량/신용/실적/재무/자격 포함 (혼합 또는 정량)
     - 누락·판독 불가·2차 절 포함
     - 기타
   부록 B 프로브도 실행해 출력을 첨부하라. 첫 두 범주의 합이 Claude 수정안 1의
   최대 기대 효과 상한이다. 그 문장들이 붙은 첨부 중 표가 AVAILABLE 인 첨부 수도 세라.

4. [코드+합성] 표 강등과 2표 조건.
   부록 C, D 스크립트를 실행해 출력을 첨부하라. 특히 다음을 확인하라.
     - 정성 전용 문장 하나로 record=INCOMPLETE, tables=['AVAILABLE'] 이 되는가
     - UNKNOWN 행에 required_evidence 가 placeholder 면 REQUIRED_EVIDENCE_INCOMPLETE 로
       표 전체가 강등되는가
     - 표 2개면 PARTIAL_ACTIVE 가 REVIEW_REQUIRED 로 바뀌는가

5. [private 자료] 3개 대표 사례의 저장 raw 구조.
   홍천 교육, RISE 연수(R26BK01726177), 구내식당 위탁운영에 대해 저장 raw 의
   quantitative_tables 별로 table_id, label, total_points, criteria 수,
   metric 분포, missing_or_unreadable 문장 수와 각 문장의 게이트 판정(True/False)만 적어라.
   총점이 정량 소계인지 전체 기술평가 점수인지 표시하라(원문 인용 없이 숫자만).

6. [판단] 수정안 1의 정책 질문.
   tests/test_source_gap_quantitative_scope.py 의
   test_scope_exclusion_cannot_hide_objective_or_price_source_defects 는
   "가격평가 산식 미제공"을 차단으로 못 박는다. 가격 산식 부재가 정량 소계를 바꿀 수 있는
   실제 사례가 저장 raw 에 있는가? 있으면 개수, 없으면 "없음"이라고 답하라.
   이것은 사용자 결정 자료이며 지금 테스트를 바꾸라는 뜻이 아니다.

7. [코드] fingerprint 개정 경로.
   _TARGETED_RECORD_FINGERPRINT_REVISIONS["EXTRACTION_DECLARED_INCOMPLETE"] 를 올렸을 때
   저장 record 가 유료 호출 없이 재검증되는 실제 경로를 코드로 추적하라.
   pps_enrichment 의 동일 바이트 재사용(api_calls=0)이 재다운로드를 요구하는지,
   아니면 재생 CLI 만으로 되는지, 어느 쪽이 운영에서 실제로 실행되는지 구분하라.

보고서: docs/CODEX_PARALLEL_VERIFICATION_RESULT_20260913.md 로 저장하되 커밋하지 마라.
형식: 항목 번호별 표 → 반박/확인 결론 한 줄 → 근거(파일:행 또는 자료 이름).
Claude 검토의 어떤 문장이 틀렸는지 먼저 쓰고, 그 다음에 맞는 부분을 써라.
```

---

## 사용자용 메모

- Codex의 현재 작업이 정량 코드와 겹친다면 위 프롬프트의 "새 작업트리" 지시가 중요하다.
  같은 작업트리에서 두 작업이 파일을 건드리면 서로의 변경을 덮을 수 있다.
- 결과 파일은 커밋하지 않도록 했다. 비교가 끝난 뒤 사용자가 올릴지 결정한다.
- 항목 2·3의 숫자가 나오면 수정안 1·2 중 무엇을 먼저 구현할지 결정할 수 있다.
  항목 2에서 커버리지가 대부분이면 코드 수정보다 0.5.5 계약 경계 결정이 먼저다.
