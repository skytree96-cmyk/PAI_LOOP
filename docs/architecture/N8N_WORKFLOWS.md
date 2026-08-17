# PAI_LOOP n8n workflow contracts

모든 workflow의 기준본은 `workflows/*.json`이며 `manifest.json`의
`publish: false`가 안전 기본값이다. Export에는 실제 secret, credential ID,
회사정보 또는 실제 Teams webhook URL을 넣지 않는다.

## Workflow map

| No. | Workflow | 책임 | 외부 효과 |
|---:|---|---|---|
| 00 | `PAI_LOOP 00 - Architecture` | 전체 시스템 설명과 결정 계약 | 없음 |
| 01 | `PAI_LOOP 01 - Notice Replay Vertical Slice` | 합성 replay 및 REVIEW 상세 계약 검증 | backend replay/detail 호출(설정 시) |
| 02 | `PAI_LOOP 02 - Live PPS Ingestion` | 제한된 실수집 window를 보호된 backend에 전달 | backend live-ingestion 호출 |
| 03 | `PAI_LOOP 03 - Teams Mock Notification` | Adaptive Card 생성과 mock delivery 기록 | backend mock-log만 호출; Teams 호출 없음 |
| 04 | `PAI_LOOP 04 - Award History Refresh` | 공고별 최근 1~3년 낙찰 유사 후보 갱신 | backend award-history 호출; 응답은 집계만 보존 |
| **10** | **`PAI_LOOP 10 - Daily Opportunity Briefing`** | **09:00 KST 수집·신규 key bounded 분석/평가/snapshot·7일 순위·3년 낙찰 refresh·정량/가격/리스크·backend Teams mock 기록의 단일 운영 진입점** | **inactive; 수동 실행은 backend/PPS/분석/낙찰/OpenAI/Teams 호출 0회** |

## 통합 운영 결정

운영자는 `10`만 실행·모니터링한다. `00~04`는 삭제하지 않고 비활성 회귀
fixture, 계약 설명, 장애 시 롤백 자료로 남긴다. n8n에서 모든 구현을 한 JSON에
중복 복사하면 공고·낙찰 계약이 서로 달라지기 쉬우므로, 하나의 **상위 오케스트레이터**가
PAI_LOOP backend의 버전된 계약을 호출하고 backend가 데이터·평가 로직을 소유한다.

따라서 “한 번에 활용”한다는 제품 요구는 충족하면서도 다음을 지킨다.

10번은 PPS 응답의 신규 `notice_keys`를 backend batch-analysis 경계로 전달한 뒤
저장된 ACCEPTED extraction의 materialize·평가·snapshot 집계가 끝나야 낙찰
refresh와 최종 briefing으로 진행한다. 이 batch 경계의 `openai_calls`는 0이다.
다만 원격 첨부의 자동 획득·파일 변환 자체와 입찰/개찰/낙찰/계약 결과 자동 환류는
여전히 구현된 n8n 경로가 아니다. 아키텍처의 주황 점선과 `TARGET` 배지는 이 남은
목표 경계를 뜻한다.

- 예약 트리거, 재시도, 비용/외부효과 Gate, 7일 피드, 카드 조립은 `10` 한 곳에서 본다.
- 자격·정량·가격 계산은 API의 테스트 가능한 결정론적 모듈이 소유한다.
- 기존 workflow를 삭제하지 않아 현재 검증 결과와 원격 rollback 경로가 남는다.

## Environment contract

| 이름 | 사용처 | 규칙 |
|---|---|---|
| `PAI_LOOP_API_BASE_URL` | 01, 02, 03, 04 | API origin. URL 안에 계정정보를 넣지 않는다. |
| `PAI_LOOP_API_KEY` | 02, 03, 04 | `X-PAI-LOOP-API-KEY` 값. item JSON으로 복사하지 않는다. |
| `PAI_LOOP_LIVE_INGESTION_ENABLED` | 02, 04 | 정확히 `true`일 때만 scheduled live request를 허용한다. |
| `PAI_LOOP_DASHBOARD_URL` | 03 | Adaptive Card의 상세보기 base URL. |
| `PAI_LOOP_AWARD_HISTORY_NOTICE_KEY` | 04 | 수동·스케줄 실행의 대상 공고 키. `/`와 제어문자는 허용하지 않는다. |
| `PAI_LOOP_AWARD_HISTORY_KEYWORD` | 04 | 선택 낙찰 검색어. 없으면 backend가 공고명에서 생성한다. |
| `PAI_LOOP_DAILY_LIVE_ENABLED` | 10 | 정확히 `true`이고 API URL이 유효할 때만 예약 실행의 backend 수집 분기를 연다. |
| `PAI_LOOP_ANALYSIS_BATCH_ENABLED` | 10 | daily gate와 함께 `true`일 때만 PPS 신규 key의 batch-analysis 호출을 허용한다. |
| `PAI_LOOP_ANALYSIS_BATCH_WRITE_ENABLED` | 10 | analysis gate와 함께 `true`일 때만 문서·평가·snapshot 저장을 허용한다. |
| `PAI_LOOP_ANALYSIS_BATCH_LIMIT` | 10 | 1~10, 기본 5. 한 실행에서 분석할 신규 공고 key 상한이다. |
| `PAI_LOOP_RETENTION_LIVE_ENABLED` | 10 | 위 daily gate도 열린 상태에서 정확히 `true`일 때만 단기 로그 삭제를 허용한다. |
| `PAI_LOOP_AWARD_REFRESH_ENABLED` | 10 | daily gate와 함께 `true`일 때만 7일 상위 후보의 3년 낙찰 API 호출을 허용한다. |
| `PAI_LOOP_AWARD_REFRESH_WRITE_ENABLED` | 10 | award gate와 함께 `true`일 때만 `dry_run=false`로 저장한다. |
| `PAI_LOOP_AWARD_REFRESH_LIMIT` | 10 | 1~5, 기본 3. 한 실행의 낙찰 refresh 후보 상한이다. |
| `PAI_LOOP_TEAMS_MOCK_LOG_ENABLED` | 10 | daily gate와 함께 `true`일 때만 통합 카드를 backend mock-log에 기록한다. 실제 Teams 전송과 무관하다. |
| `PAI_LOOP_WEB_BASE_URL` | 10 | 카드의 `웹에서 근거 확인` 링크. 자격증명 없는 HTTP(S) origin/path만 허용한다. |

10번의 API/Web URL은 `$env` 값을 우선하고 없으면 공개 origin
`https://pai-loop-demo.onrender.com`을 사용한다. 모든 live gate는 `$env`가 없으면
false다. 모든 backend HTTP Request 노드는 `genericCredentialType/httpHeaderAuth`를
선언하고, 운영자가 n8n의 Generic Header Auth credential `PAI_LOOP Render Backend`를
각 노드에 연결한다. Credential 값과 ID는 환경마다 다르므로 Git export에는 넣지
않는다. 배포기는 동일 노드 이름·타입의 원격 credential 연결만 보존한다. 연결되지
않은 노드는 인증 없이 우회하지 않고 n8n 실행 단계에서 fail-closed한다.

## 02 · Live PPS Ingestion

Backend contract:

```text
POST /api/v1/ingestion/pps/notices
X-PAI-LOOP-API-KEY: <credential>
Idempotency-Key: pps:<from>:<to>:<keyword>:<dry|live>
```

```json
{
  "from_date": "YYYY-MM-DD",
  "to_date": "YYYY-MM-DD",
  "keyword": "optional",
  "page_size": 100,
  "max_pages": 3,
  "dry_run": true
}
```

응답은 `job_id`, `status=COMPLETED`, `source=PPS`, `mode=live`, `window`,
`api_calls`, `fetched`, `matched`, `created`, `updated`, `duplicates`,
`quarantined`, `notice_keys`, `next_watermark`, `warnings`, `dry_run`을 반환한다.
Workflow validator는 count를 0 이상의 정수로 검증한다.

Safety gates:

- 수동 실행은 항상 dry-run이다.
- Schedule은 08:10 Asia/Seoul이지만 workflow 자체가 inactive다.
- workflow를 활성화해도 `PAI_LOOP_LIVE_INGESTION_ENABLED=true` 전에는
  request가 강제 dry-run이다.
- page size 최대 100, page count 최대 10, lookback 최대 7일로 제한한다.

## 03 · Teams Mock Notification

현재 backend mock-log contract:

```text
POST /api/v1/notices/{notice_key}/notifications/teams/mock
GET  /api/v1/notifications/mock?limit=<n>
```

Workflow가 만드는 request는 `card`, `channel=teams`, `delivery_mode=mock`,
`correlation_id`로 구성한다. 응답 상태는 `MOCK_RECORDED`이며 correlation ID는
최대 120자, 카드 직렬화 크기는 최대 28KB다. API URL이 없으면
`Log Mock Locally`가 동일한 결과 계약을 만든다. 두 경로 모두 최종 결과에
`actualTeamsRequestSent=false`를 명시한다.

실제 Teams 전송으로 교체할 때는 다음 Gate를 모두 만족해야 한다.

1. `Build Adaptive Card`의 `teamsWebhookPayload`만 전송 입력으로 사용한다.
2. Teams Workflows webhook URL은 n8n credential store에 저장한다.
3. 카드 action URL은 승인된 dashboard host만 허용한다.
4. retry, rate limit, dead-letter, correlation ID와 Teams message ID를 기록한다.
5. 카드에 개인정보, 증빙 원문, API key 또는 내부 stack trace를 넣지 않는다.
6. 실제 전송 노드는 mock-log 노드와 분리해 코드리뷰에서 외부 효과가 보이게 한다.

## 04 · Award History Refresh

Backend contract:

```text
POST /api/v1/notices/{notice_key}/award-history/refresh
X-PAI-LOOP-API-KEY: <credential>
Idempotency-Key: award:<notice>:<date>:<years>:<keyword>:<dry|live>
```

```json
{
  "keyword": "optional",
  "years": 3,
  "page_size": 100,
  "max_pages_per_window": 1,
  "dry_run": true
}
```

응답은 `job_id`, `status=COMPLETED|PARTIAL`, `notice_key`, `keyword`,
`window`, `api_calls`, `fetched`, `created`, `updated`, `duplicates`,
`records`, `dry_run`, `warnings`만 허용한다. `PARTIAL`은 일부 7일 재조회
구간도 실패해 후보 집합이 불완전할 수 있다는 뜻이며, 이때 `warnings`가
반드시 1개 이상 있어야 한다. Workflow는 이 집계만 반환하고 낙찰 후보 행,
업체명, 사업자 식별자, 대표자, 주소, 전화, 담당자 필드를 실행 출력에 남기지
않는다.

Safety gates:

- 수동 실행은 항상 dry-run이다.
- 08:40 Asia/Seoul schedule과 다른 workflow 호출은 기본 dry-run이다.
- live 요청은 workflow를 활성화하고
  `PAI_LOOP_LIVE_INGESTION_ENABLED=true`를 설정한 경우에만 가능하다.
- `years` 최대 3, page size 최대 100, 30일 구간별 page 최대 3으로 제한한다.
- 결과는 제목 기반 후보이며 동일 사업 또는 경쟁사 확정 이력이 아니다.
  유사도와 원 공고를 사람이 검토한 뒤 의사결정에 사용한다.
- workflow export의 HTTP 노드는 보호된 PAI_LOOP backend만 호출한다.
  조달청 서비스키와 provider 원문은 backend 경계를 넘지 않는다.

## 10 · Daily Opportunity Briefing

운영 시간은 workflow `settings.timezone=Asia/Seoul`, cron `0 9 * * *`이다.
`manifest.json`은 `publish:false`이므로 현재 원격 배포도 inactive다.

### 완전 오프라인 수동 검수

`Run Complete Offline Dry-Run`은 다음 경로만 지난다.

```text
합성 7일 공고 2건
  → 부서·적합성·정량/가격/리스크 필드 정규화
  → 통합 Adaptive Card 1.5 한 장
  → 로컬 push mock
  → 09:00/7일/bounded 분석·낙찰 skip/0-call/28KB 계약 검증
```

이 경로에서 HTTP Request 노드는 그래프상 도달 불가능하다. repository validator와
`scripts/test-daily-workflow.mjs`가 실제 Code node를 순서대로 실행해 다음을 검증한다.

- backend/PPS/낙찰/OpenAI/Teams source call 0;
- batch-analysis gate false, 요청 key 0, 문서·평가·snapshot write 0;
- `actualTeamsRequestSent=false`, `actualPushSent=false`;
- 정량·가격·리스크 필드가 없으면 숫자를 만들지 않고 `분석 대기`/`UNKNOWN`;
- 카드 최대 28KB, Adaptive Card 1.5;
- 7일 창과 매일 09:00 KST schedule.

### 예약 실행의 닫힌 Gate

workflow가 나중에 활성화되더라도 `PAI_LOOP_DAILY_LIVE_ENABLED=true` 전에는
합성 preview로만 끝난다. Gate가 열린 예약 실행만 다음 backend 경계를 사용한다.

1. `POST /api/v1/ingestion/pps/notices` — 전일~당일 bounded 수집과 strict 응답 검증;
2. `POST /api/v1/notices/analysis/batch` — PPS가 반환한 중복 제거 key 중 최대 5건
   (환경 상한 10)의 문서분석·평가·snapshot. 별도 write gate 전에는 `dry_run=true`;
3. `POST /api/v1/operations/retention` — 기본 preview, 별도 retention gate 후 적용;
4. `GET /api/v1/operations/daily-briefing?days=7&limit=20` — refresh 후보를 위한 저장 데이터 조립;
5. `POST /api/v1/notices/{notice_key}/award-history/refresh` — 비-FAIL 상위 최대 3건,
   최근 3년·100행·구간당 1페이지. 별도 write gate 전에는 `dry_run=true`;
6. 같은 daily-briefing GET 재조회 — 저장 refresh가 있으면 당일 카드에 반영;
7. `POST /api/v1/notices/{anchor}/notifications/teams/mock` — 통합 카드 한 건의 backend
   mock 기록. 별도 gate가 닫혀 있으면 로컬 mock만 생성한다.

Batch-analysis 요청은 `{notice_keys, dry_run, force:false}`만 전송한다. 응답은
`job_id`, `status=COMPLETED|PARTIAL`, `dry_run`, `requested`, `processed`,
`completed`, `skipped`, `failed`, `document_materialized`, `evaluations_created`,
`snapshots_refreshed`, `openai_calls`, `results`, `warnings`의 제한된 집계 계약이다.
`processed=completed+skipped+failed`, 결과 key는 요청 key의 중복 없는 부분집합이어야
한다. `dry_run=true`에서 세 write 집계는 반드시 0이며 문서본문·원자 근거·PII는
n8n item으로 반환하지 않는다. 각 result는 상태와 nullable run/evaluation/version
ID, input SHA-256, reused flag, materialized requirement·requirement snapshot·score
snapshot·recommendation snapshot count만 반환한다. Backend idempotency key 자체는
노출하지 않는다.

Dry-run도 감사·장애 추적용 `ANALYSIS` ingestion job 한 건은 기록하지만 문서
materialize, evaluation, snapshot 업무 레코드는 생성하지 않는다. 이 job은 7일
단기 운영로그 retention 대상이다.

마지막 mock endpoint의 `anchor`는 통합 카드 첫 공고와 기록을 연결하기 위한 기술적
앵커이며 카드 전체가 그 공고 하나만을 뜻하지 않는다. 현재 backend 계약을 재사용한
최소변경 방식이다. 추후 digest 전용 delivery-log가 생기면 이 앵커를 제거한다.

실제 Teams webhook 노드는 없다. backend mock 기록은 Teams 네트워크 호출이 아니다.
회사 승인 후 마지막 mock 경계와 분리된 send 노드·credential·별도 gate를 추가한다.

### 7일 보관 범위

7일은 **아침 피드 조회 창**과 완료 ingestion job/mock 알림 같은 단기 운영 로그에
적용한다. 공고 원장, 평가, 사용자 결정, 3년 낙찰 관측을 연쇄 삭제하지 않는다.
이 기록은 감사, 가격 관측, 결과 환류에 필요하다. `/operations/retention`은 기본
`dry_run=true`이고 RUNNING job은 어떤 경우에도 삭제하지 않는다.

## Validation

```powershell
node scripts/deploy-workflows.mjs --validate-only
node scripts/test-daily-workflow.mjs
```

추가 계약 테스트는 다음을 확인한다.

- manual/scheduled/live-enable 조합별 `dry_run` Gate;
- PPS response count와 배열 필드;
- batch-analysis 최대 10개 key, dry-run write 0, 집계 합계와 결과 key 일치;
- 낙찰 response의 exact field set, `COMPLETED|PARTIAL`, 기간·집계·경고 계약;
- `PARTIAL` 응답의 경고 필수 및 후보 행/PII가 최종 출력에서 제거되는지;
- Adaptive Card 1.5 및 Teams message attachment wrapper;
- 실제 Teams·Office·Power Automate URL을 향하는 HTTP 노드 0개;
- secret value와 credential ID 0개.
