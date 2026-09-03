# 2026-09-03 Codex 핸드오프

이 폴더는 2026-09-03 하루 동안 진행된 (1) 자격요건 REVIEW 폭증 분석/수정,
(2) 정량점수 산정 실패 감사 작업의 전체 산출물이다. Codex는 이 폴더의
문서만 읽으면 별도 배경 설명 없이 이어서 작업할 수 있어야 한다.

## 읽는 순서

1. **`CHANGELOG_ELIGIBILITY_FRESHNESS_FIX.md`** — 이번 PR에서 **실제로 코드에
   반영된** 변경 사항. 이미 완료됨, 리뷰 후 병합 여부만 결정하면 됨.
2. **`codex_prompt_eligibility_policy_fix.md`** — 자격요건 관련 전체 작업 지시서.
   이 문서의 **Phase 1은 위 CHANGELOG로 이미 구현 완료**됨. **Phase 2, 3은
   미착수** — Codex가 이어서 진행.
3. **`quantitative_scoring_audit_2026-09-03.md`** — 정량점수 산정 실패 원인 감사
   보고서. 코드 수정은 없음, 원인 분석만 완료.
4. **`codex_prompt_quantitative_scoring_fix.md`** — 정량점수 수정 작업 지시서
   (Phase 1~4). **전부 미착수** — Codex가 처음부터 진행.

## 현재 상태 요약표

| 항목 | 상태 | 담당 |
|---|---|---|
| 자격요건 프레시니스 버그 수정 (Phase 1) | ✅ 완료, 이 PR에 포함, 테스트 통과 | Claude (완료) |
| 자격요건 지역 예외/신인도/법인유형/물품코드 (Phase 2) | ⬜ 미착수 | Codex |
| 자격요건 체크리스트 "준비물 표기" (Phase 3) | ⬜ 미착수 | Codex |
| 정량점수 PR #69 metric 헤더 검증 보강·병합 | ⬜ 미착수 | Codex |
| 부산교육한마당 라이브 값 조회·검증 | ⬜ 미착수 (운영 PIN 필요, same-origin 제약 있음) | Codex 또는 운영 담당자 |
| 공동수급 지분율 로직 테스트 보강 | ⬜ 미착수 (로직 자체는 이미 존재 확인됨, `quantitative_performance.py` 780-870행 부근) | Codex |
| n8n W11 COMPLETED 판정 기준 변경 | ⬜ 미착수 | Codex |

## 사용자 확인/승인 이력 (요약)

- 자격요건 REVIEW 폭증: 감사 결과 승인, Phase 1 즉시 진행 지시 받음.
- 정량점수: (1) Busan 라이브 값 조회 허용, (2) PR #69는 보강 후 병합 방향 확정,
  (3) 공동수급 지분율 로직은 "코덱스가 만들었다는데 오류로 없을 수도 있다"는
  전제로 재확인 지시 → **재확인 결과 실제로 존재함을 확인**(감사 보고서 정정),
  (4) n8n W11은 정량 성공 시에만 COMPLETED로 집계하도록 변경 지시.
- 이 모든 배경은 `codex_prompt_quantitative_scoring_fix.md`에 이미 반영되어 있음.

## 주의 사항 (AGENTS.md 준수)

- `main`에 직접 push 하지 않았다. 이 변경은 `claude/eligibility-freshness-recheck-fix-20260903`
  브랜치의 PR로 올라간다. CI 통과 후 병합할 것.
- 회사 비공개 원본 파일(주소, 사업자등록번호 등)은 이번 작업에서 다루지 않았고
  `company_public_profile.json`도 이번 PR에서 수정하지 않았다(로직만 변경).
- Render/n8n 재배포·재실행은 이번 세션에서 하지 않았다.
