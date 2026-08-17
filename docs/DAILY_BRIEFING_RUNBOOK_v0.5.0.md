# PAI_LOOP 오전 9시 통합 브리핑 운영안 v0.5.0

## 결론

n8n 운영 진입점은 **`PAI_LOOP 10 - Daily Opportunity Briefing` 하나**다.
기존 00~04와 smoke workflow는 비활성 계약 시험·복구 자산으로 남긴다. 10번만
활성화해야 하며 02/04를 함께 활성화하면 조달청 API가 중복 호출될 수 있다.

v0.5.0은 이전 10번에서 빠졌던 두 실행 경계를 통합한다.

- 최근 7일 브리핑의 비-FAIL 상위 후보 최대 3건에 대한 최근 3년 낙찰 refresh;
- Teams Adaptive Card 한 장의 backend mock 기록.

실제 Teams/Office/Power Automate endpoint를 호출하는 노드는 없다.

## 현재 안전 상태

- workflow manifest: `publish:false`;
- cron: `0 9 * * *`, timezone `Asia/Seoul`;
- 모든 live gate: 환경변수 미설정 시 `false`;
- 수동 dry-run: backend/PPS/낙찰/OpenAI/Teams 호출 모두 0;
- API/Web 기본 origin: `https://pai-loop-demo.onrender.com`;
- backend 인증: n8n Generic Header Auth credential 필요;
- Git export의 secret 값·credential ID: 0.

## 실행 흐름

```text
09:00 KST
  → daily live gate
  → PPS 전일~당일 bounded 수집 + 응답 계약 검증
  → 7일 초과 ingestion/mock 로그 preview 또는 apply
  → 최근 7일 부서 우선순위 브리핑
  → 비-FAIL 후보 최대 3건 선별
  → award gate가 열렸을 때만 3년 낙찰 refresh
      · years=3
      · page_size=100
      · max_pages_per_window=1
      · write gate 전에는 dry_run=true
  → 브리핑 재조회
  → 적합성·정량·가격·리스크·경쟁집중 리스크 카드 생성
  → mock-log gate가 열렸을 때만 backend에 카드 기록
  → 실제 Teams 전송 0 검증
```

새 공고의 문서/제안요청서 추출이 끝나지 않았다면 적합성·정량은 `PENDING` 또는
`REVIEW`로 남는다. 10번은 근거가 없는 점수나 낙찰률을 만들지 않는다. OpenAI 문서
분석 자동화는 별도의 검토 대상이며 이 버전에서 새로 활성화하지 않았다.

## n8n 설정

### 1. Generic Header Auth

n8n에서 `PAI_LOOP Render Backend` credential을 만들고 다음처럼 설정한다.

```text
Credential type: Header Auth
Header name: X-PAI-LOOP-API-KEY
Header value: Render의 PAI_LOOP_API_KEY와 동일한 값
```

다음 6개 HTTP Request 노드에 같은 credential을 선택한다.

1. `Refresh PPS Notices Behind Gate`
2. `Preview or Apply Seven-Day Log Retention`
3. `Fetch Award Candidates from Seven-Day Briefing`
4. `Refresh Bounded Three-Year Award History`
5. `Fetch Ranked Seven-Day Briefing`
6. `Record Consolidated Teams Mock in Backend`

소스 JSON에는 authentication type만 있고 credential ID는 없다. GitHub 배포기는
동일한 node name과 type이 유지된 경우 n8n UI의 credential 연결을 보존한다. 연결이
없는 HTTP 노드는 인증을 생략해 우회하지 않고 실행 단계에서 실패한다.

### 2. 환경변수 Gate

```text
PAI_LOOP_API_BASE_URL=https://pai-loop-demo.onrender.com       # 선택, 동일 fallback 있음
PAI_LOOP_WEB_BASE_URL=https://pai-loop-demo.onrender.com       # 선택, 동일 fallback 있음
PAI_LOOP_DAILY_LIVE_ENABLED=false
PAI_LOOP_RETENTION_LIVE_ENABLED=false
PAI_LOOP_AWARD_REFRESH_ENABLED=false
PAI_LOOP_AWARD_REFRESH_WRITE_ENABLED=false
PAI_LOOP_AWARD_REFRESH_LIMIT=3
PAI_LOOP_TEAMS_MOCK_LOG_ENABLED=false
```

`true`는 대소문자와 공백을 정리한 뒤 정확히 일치할 때만 인정된다. award/retention/
mock gate는 daily gate도 열려 있어야 한다. Variables 기능은 요구하지 않는다.

## 점진적 온라인 검증

1. 모든 gate가 false이고 workflow가 inactive인지 확인한다.
2. `Run Complete Offline Dry-Run`을 실행한다.
3. 결과가 `DRY_RUN_PASSED`, `awardRefresh.status=SKIPPED`,
   `notificationMock.status=MOCK_LOCAL_ONLY`인지 확인한다.
4. `externalCalls`의 6개 값이 모두 0인지 확인한다.
5. credential을 6개 노드에 연결하고 workflow는 계속 inactive로 둔다.
6. 제한된 테스트가 필요할 때만 한 gate씩 열고 예약이 아닌 테스트 복제본 또는
   승인된 일회 실행으로 확인한다.
7. award API 연결 확인은 `AWARD_REFRESH_ENABLED=true`,
   `AWARD_REFRESH_WRITE_ENABLED=false`부터 시작한다.
8. mock 기록 확인은 `TEAMS_MOCK_LOG_ENABLED=true`로 실행하되 실제 Teams 발송은
   여전히 0인지 최종 결과에서 확인한다.
9. 운영 승인 후 마지막에만 10번을 활성화한다. 02/04는 비활성으로 유지한다.

## Backend 계약 주의

현재 Teams mock endpoint는 공고 키를 요구한다. 통합 카드의 첫 공고를 기술적
anchor로 사용해 카드 한 장을 기록한다. 이는 카드가 첫 공고 하나만을 뜻한다는
의미가 아니다. 향후 digest 전용 delivery-log endpoint와 선택적 `notice_key` 모델을
추가하면 이 임시 anchor를 제거할 수 있다.

`daily-briefing`의 `fit`에는 `risk_score`와 `risk_band`가 함께 있어야 카드가 숫자를
표시한다. 경쟁·집중 리스크는 `competition_risk` 계약을 사용하며 참가자격 리스크와
별개다. `UNKNOWN`일 때 점수를 0으로 대체하지 않고, 법적 독점 여부를 판정하지 않는다.

## 자동 검증

```powershell
node scripts/deploy-workflows.mjs --validate-only
node scripts/test-daily-workflow.mjs
```

검증 항목:

- workflow JSON·연결·Code node 문법;
- 정확히 1개의 09:00 KST schedule;
- 6개 backend HTTP 경계의 Generic Header Auth 강제;
- workflow JSON의 `X-PAI-LOOP-API-KEY` 값과 credential ID 부재;
- 수동 경로의 HTTP 도달 0 및 실제 Code node 0-call 실행;
- award batch 최대 5, 기본 3, 3년, dry-run/write 분리;
- Adaptive Card 1.5, 28KB, 안전한 OpenUrl;
- 적합성 리스크와 경쟁·집중 리스크의 분리;
- 실제 Teams 전송·push 0.
