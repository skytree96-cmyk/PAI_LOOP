# PAI_LOOP 오전 9시 통합 브리핑 운영안 v0.4.0

## 결론

n8n 화면에서 운영할 대상은 **`PAI_LOOP 10 - Daily Opportunity Briefing` 하나**다.
기존 00~04와 smoke workflow는 지금까지의 API 계약을 재현하는 비활성 시험대이므로
삭제하지 않는다. 이는 사용 경로가 여러 개라는 뜻이 아니라, 운영 진입점 하나와
복구 가능한 내부 시험 자산을 분리한 것이다.

## 현재 배포 상태

- cron: 매일 `09:00`, timezone `Asia/Seoul`;
- Git source: `workflows/pai-loop-10-daily-opportunity-briefing.json`;
- manifest: `publish:false`;
- PPS/OpenAI/Teams 실제 호출: 비활성;
- 수동 테스트: 합성 fixture, 외부 호출 0, 카드 생성까지 완주;
- Teams: 한 장의 Adaptive Card mock만 생성, 실제 push 없음.

## 운영 데이터 흐름

```text
09:00 KST
  → 외부효과 gate
  → 전일~당일 신규 공고 수집
  → 최근 7일 저장 공고 조회
  → 부서 우선순위 + 저장된 적합성
  → 선택적 정량점수 + 3년 낙찰/가격 신호
  → 7일 초과 단기 운영 로그 preview/apply
  → Teams용 통합 카드 한 장
  → 현재: mock / 승인 후: 실제 push
```

새 공고에 문서 분석이 아직 없으면 적합성은 `PENDING`이다. 정량 및 가격 관측이
없으면 `분석 대기`로 표시한다. 점수가 한 값으로 확정되지 않으면 `하한~상한 / 만점`
형태로 표시한다. 반복 수주 신호는 표본 내 상위 수행기관 건수·비중 또는 HHI로
표시하되 반드시 `독점 확정 아님`을 함께 적는다. 아침 시간에 맞추기 위해 근거
없는 숫자를 만들어 넣지 않는다.

## 체크 순서

1. n8n에서 workflow가 **Inactive**인지 확인한다.
2. `Run Complete Offline Dry-Run`을 누른다.
3. 마지막 결과가 `status=DRY_RUN_PASSED`인지 확인한다.
4. `schedule.cron=0 9 * * *`, `timezone=Asia/Seoul`인지 확인한다.
5. `actualTeamsRequestSent=false`, `actualPushSent=false`인지 확인한다.
6. 카드에 fixture 2건, 정량 예상 1건, 가격 신호 1건, 분석 대기 1건이 보이는지 확인한다.

로컬 자동 검수:

```powershell
node scripts/deploy-workflows.mjs --validate-only
node scripts/test-daily-workflow.mjs
python -m pytest tests/test_daily_operations.py
```

## 실제 활성화 전 남은 Gate

다음은 구현됐지만 아직 켜지 않는다.

- backend URL/API key를 n8n credential/env에 연결;
- `PAI_LOOP_DAILY_LIVE_ENABLED=true` 승인;
- 제한된 날짜로 PPS provider 호출량 확인;
- retention preview 결과 검토 후 `PAI_LOOP_RETENTION_LIVE_ENABLED=true` 승인;
- Teams tenant 승인 후 실제 send 노드와 별도 send gate 추가;
- 실패 알림, dead-letter, 실행 로그에서 header/body 마스킹 확인.

Teams 승인이 없으므로 지금 workflow를 활성화해도 실제 push가 나가지 않지만,
PPS 비용과 실행 이력을 불필요하게 만들지 않도록 계속 inactive로 유지한다.

## 7일 보관 해석

7일 이후의 “아침 피드와 단기 운영 로그”는 정리하되 아래 핵심 사실은 보존한다.

- 공고 원장과 버전;
- 자격/정량 평가와 사람의 입찰 결정;
- 최근 3년 낙찰 관측과 가격 근거;
- 결과 환류에 필요한 감사 기록.

이 구분 없이 공고 행 자체를 삭제하면 과거 수행기관 집중도와 가격 예측을 다시
계산할 수 없고 담당자 결정 감사도 끊긴다.
