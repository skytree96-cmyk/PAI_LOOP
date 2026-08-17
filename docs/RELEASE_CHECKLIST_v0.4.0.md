# PAI_LOOP v0.4.0 오전 9시 운영 흐름 검수

검수일: 2026-08-17 (Asia/Seoul)

## 결과

| 항목 | 결과 | 근거 |
|---|---|---|
| 단일 운영 진입점 | PASS | `PAI_LOOP 10 - Daily Opportunity Briefing` |
| 원격 n8n ID | PASS | `WimY5yV3aWqMZRsx`, exact-name 1건, manifest ID 멱등 갱신 |
| 원격 활성 상태 | PASS | `active=false` |
| 원격 구조 | PASS | 19 nodes, backend HTTP boundary 3 nodes |
| 예약 시간 | PASS | cron `0 9 * * *`, workflow timezone `Asia/Seoul` |
| 수동 E2E | PASS | Code node 실실행: HTTP/PPS/OpenAI/Teams 0회, `DRY_RUN_PASSED` |
| Teams | PASS | Adaptive Card 1.5 생성, `actualTeamsRequestSent=false`, 실제 send 노드 0개 |
| 7일 피드 | PASS | 저장 데이터 `GET /api/v1/operations/daily-briefing?days=7` |
| 7일 로그 정리 | PASS | 기본 preview; 완료 job/mock만 대상, RUNNING/핵심기록 보존 |
| 정량/가격 계약 | PASS | 점수 하한~상한/만점, 낙찰률 범위, 표본 상위 수행기관 건수·비중과 `독점 확정 아님` 표기 |
| credential export | PASS | workflow credential block 0, API key는 `$env` 참조만 사용 |
| Python 공개 후보 테스트/coverage | PASS | 122 passed, total branch coverage 85.09% (gate 85%); local-only source-adapter tests excluded |
| Python focused tests | PASS | `tests/test_daily_operations.py` 3 passed |
| n8n repository validator | PASS | 7 workflow, manual path HTTP 도달 불가, 09:00/비활성/credential 검사 |
| 공개 릴리스 경계 | PASS | 95개 후보 파일에서 실제 secret 4값 exact match 0, generic secret 0; wheel 41파일에 local-only adapter 0 |

## 통합 판단

기존 00~04와 smoke를 원격에서 삭제하지 않았다. 이들은 전부 inactive이고 API
계약 회귀, 장애 진단, rollback 자료다. 운영자가 실행하는 workflow는 10 하나다.
구현 로직을 n8n JSON에 중복하지 않고 테스트 가능한 backend 계약을 호출하므로,
한 번에 작동하면서도 수집·평가·가격 계산이 서로 다른 버전으로 갈라지지 않는다.

## 7일 정책

다음만 7일 범위다.

- 오전 브리핑에 표시하는 최근 공고 창;
- 완료된 ingestion job;
- Teams mock notification.

공고 원장, 버전, 평가, 사용자 결정, 3년 낙찰 관측은 보존한다. 이를 삭제하면
집중도·낙찰률·가격 범위를 재계산할 근거와 담당자 의사결정 감사가 사라진다.

## 현재 의도적으로 비활성인 항목

- PPS 실제 호출;
- OpenAI 실제 호출;
- Teams 실제 push;
- retention 실제 삭제;
- n8n schedule activation.

실운영 전에는 `docs/DAILY_BRIEFING_RUNBOOK_v0.4.0.md`의 Gate 순서대로 별도
승인한다. 특히 Teams tenant 승인 전에는 send node를 추가하지 않는다.
