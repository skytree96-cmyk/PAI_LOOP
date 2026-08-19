# PAI_LOOP n8n workflow contracts

모든 workflow의 기준본은 `workflows/*.json`이며 `manifest.json`의
`publish: false`가 안전 기본값이다. 현재 예외는 live E2E를 마친 W10/W11/W12이며,
각각 `daily-briefing-1.5`, `analysis-backfill-1.2`, `teams-delivery-1.2` 계약으로
`publish: true`다. Export에는 실제 secret, credential ID, 회사정보 또는 실제
Teams webhook URL을 넣지 않는다.

## Workflow map

| No. | Workflow | 책임 | 외부 효과 |
|---:|---|---|---|
| 00 | `PAI_LOOP 00 - Architecture` | 전체 시스템 설명과 결정 계약 | 없음 |
| 01 | `PAI_LOOP 01 - Notice Replay Vertical Slice` | 합성 replay 및 REVIEW 상세 계약 검증 | backend replay/detail 호출(설정 시) |
| 02 | `PAI_LOOP 02 - Live PPS Ingestion` | 제한된 실수집 window를 보호된 backend에 전달 | backend live-ingestion 호출 |
| 03 | `PAI_LOOP 03 - Teams Mock Notification` | Adaptive Card 생성과 mock delivery 기록 | backend mock-log만 호출; Teams 호출 없음 |
| 04 | `PAI_LOOP 04 - Award History Refresh` | 공고별 최근 1~3년 낙찰 유사 후보 갱신 | backend award-history 호출; 응답은 집계만 보존 |
| **10** | **`PAI_LOOP 10 - Daily Opportunity Briefing`** | **09:00 KST 수집·신규/변경 key durable plan·7일 순위·3년 낙찰 refresh·backend Teams mock 기록** | **active; `publish=true`, `daily-briefing-1.5`; 수동 offline preview는 외부 호출 0회** |
| **11** | **`PAI_LOOP 11 - Analysis Backfill and Continuation`** | **15분마다 W10의 남은 DAILY lease를 우선 재개하고 수동 BACKFILL을 같은 bounded 계약으로 처리** | **active; `publish=true`, `analysis-backfill-1.2`, `verified-live-e2e`** |
| **12** | **`PAI_LOOP 12 - Teams Daily Delivery`** | **10:00 KST 저장 브리핑을 별도로 읽어 공개 allowlist HTML을 PAI 봇 채널에 전송** | **active; `publish=true`, `teams-delivery-1.2`, `verified-live-e2e`; fail-closed·at-most-once** |

## 통합 운영 결정

운영자는 09:00 수집·초기 분석의 W10, 15분 continuation의 W11, 10:00 전달 전용
W12를 각각 모니터링한다. W12 장애는 W10/W11을 재실행하거나 중단시키지 않는다.
`00~04`는 삭제하지 않고 비활성 회귀 fixture, 계약 설명, 장애 시 롤백 자료로
남긴다. n8n에서 모든 구현을 한 JSON에 중복 복사하면 공고·낙찰 계약이 서로
달라지기 쉬우므로, backend가 데이터·평가 로직을 소유하고 세 운영 workflow는
수집·continuation·전달 책임만 분리해 가진다.

따라서 “한 번에 활용”한다는 제품 요구는 충족하면서도 다음을 지킨다.

10번은 PPS 응답의 `created_notice_keys + updated_notice_keys` 정확 합집합과 cooled
backlog 최대 3건을 durable DAILY parent에 넣고, 한 실행에서 최대 30건을 3건 단위로
처리한다. 11번은 같은 parent/segment/chunk 계약으로 남은 작업을 재개한다. 저장된
ACCEPTED extraction의 materialize·평가·snapshot 집계가 끝난 뒤 낙찰 refresh와
최종 briefing으로 진행하며, 필요한 원격 첨부 보강만 bounded OpenAI 경계를 사용할
수 있다. 입찰/개찰/낙찰/계약 결과의 완전 자동 환류는 계속 확장 경계다.

- 09:00 수집·초기 lease·7일 피드·카드 조립은 `10`, continuation은 `11`, 실제
  Teams 전송은 `12` 한 곳에서 각각 본다.
- 자격·정량·가격 계산은 API의 테스트 가능한 결정론적 모듈이 소유한다.
- 기존 workflow를 삭제하지 않아 현재 검증 결과와 원격 rollback 경로가 남는다.

## Environment contract

| 이름 | 사용처 | 규칙 |
|---|---|---|
| `PAI_LOOP_API_BASE_URL` | 01, 02, 03, 04, 10, 11 | API origin. URL 안에 계정정보를 넣지 않는다. W10/W11 기본값은 공개 Render origin이다. |
| `PAI_LOOP_API_KEY` | 02, 03, 04 | `X-PAI-LOOP-API-KEY` 값. item JSON으로 복사하지 않는다. |
| `PAI_LOOP_LIVE_INGESTION_ENABLED` | 02, 04 | 정확히 `true`일 때만 scheduled live request를 허용한다. |
| `PAI_LOOP_DASHBOARD_URL` | 03 | Adaptive Card의 상세보기 base URL. |
| `PAI_LOOP_AWARD_HISTORY_NOTICE_KEY` | 04 | 수동·스케줄 실행의 대상 공고 키. `/`와 제어문자는 허용하지 않는다. |
| `PAI_LOOP_AWARD_HISTORY_KEYWORD` | 04 | 선택 낙찰 검색어. 없으면 backend가 공고명에서 생성한다. |
| `PAI_LOOP_WEB_BASE_URL` | 10 | 카드의 `웹에서 근거 확인` 링크. 자격증명 없는 HTTP(S) origin/path만 허용한다. |
| `PAI_LOOP_EMERGENCY_DISABLE` | 10, 11 | 정확히 `true`이면 scheduled 수집·분석과 continuation을 fail-closed한다. |

10/11번의 API/Web URL은 `$env` 값을 우선하고 없으면 공개 origin
`https://pai-loop-demo.onrender.com`을 사용한다. 검증된 scheduled 경로는
`PAI_LOOP_EMERGENCY_DISABLE=true`가 아니면 live이며, 수동 fixture 경로는 계속
외부 호출 0회다. 모든 backend HTTP Request 노드는 `genericCredentialType/httpHeaderAuth`를
선언하고, 운영자가 n8n의 Generic Header Auth credential `PAI_LOOP Render Backend`를
각 노드에 연결한다. Credential 값과 ID는 환경마다 다르므로 Git export에는 넣지
않는다. 배포기는 동일 노드 이름·타입의 원격 credential 연결만 보존한다. 연결되지
않은 노드는 인증 없이 우회하지 않고 n8n 실행 단계에서 fail-closed한다.

W12는 n8n Variables나 export 상수를 운영 대상으로 쓰지 않는다. 이름이 고정된
Data Table `pai_loop_teams_delivery_config`에서 `push_enabled`, `approval_state`,
`team_id`, `channel_id`, `live_test_enabled`, `emergency_disabled` 여섯 행만 읽는다.
누락·중복·형식 오류가 있으면 Teams 노드 전에 종료한다.

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

03은 회귀 검증용 mock 경계로 유지하며 실제 전송 노드로 교체하지 않는다. 실제
Teams 전송은 책임이 분리된 W12만 수행한다. W12는 승인된 공개 allowlist 필드,
고정 Render 링크, 이름 기반 Data Table Gate, 영속 correlation 예약과 native
Microsoft Teams v2 노드 하나만 사용한다. 카드에 개인정보, 증빙 원문, API key,
credential ID 또는 내부 오류 본문을 넣지 않는다.

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
`manifest.json`은 `contractVersion=daily-briefing-1.5`, `publish:true`이며 현재 원격
배포도 active다. `PAI_LOOP_EMERGENCY_DISABLE=true`이면 scheduled 경로가 외부 호출
전에 닫힌다.

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

검증된 예약 경로는 emergency disable이 닫혀 있지 않을 때만 다음 backend 경계를
사용한다. 수동 offline preview는 이 경로와 연결되지 않는다.

1. `POST /api/v1/ingestion/pps/notices` — 전일~당일 bounded 수집과 strict 응답 검증;
2. `GET /api/v1/operations/daily-briefing?days=7&limit=20` — refresh 후보와 최근 7일
   `analysis_queue` 조립;
3. `POST /api/v1/operations/analysis-backfills/plan` — 생성·정정 key 정확 합집합 전량과
   cooled backlog 최대 3건을 DAILY parent에 추가하고 최대 30건의 exact segment/chunk lease 반환;
4. `POST /api/v1/notices/analysis/batch` — lease에 포함된 key를 호출당 최대 3건씩
   직렬로 첨부 보강·분석·평가·snapshot 처리;
5. `POST /api/v1/operations/analysis-backfills/{job_id}/complete` — exact segment의 모든
   child가 terminal일 때만 lease 해제와 aggregate 반영;
6. `POST /api/v1/operations/retention` — 완료 ingestion/mock 운영로그의 7일 보관 적용;
7. `POST /api/v1/notices/{notice_key}/award-history/refresh` — 비-FAIL 상위 최대 3건,
   최근 3년·100행·구간당 1페이지로 저장 갱신;
8. 같은 daily-briefing GET 재조회 — 저장 refresh가 있으면 당일 카드에 반영;
9. `POST /api/v1/notices/{anchor}/notifications/teams/mock` — 통합 카드 한 건의 backend
   mock 감사 기록. 실제 Teams 전송은 W12가 별도로 수행한다.

Leased batch-analysis 요청은 `notice_keys`, `dry_run`, `force:false`와 exact
`operation_id`, `segment_id`, `chunk_index`만 전송한다. 응답은
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

W10에는 실제 Teams 노드가 없다. backend mock 기록은 Teams 네트워크 호출이 아니며,
W12 장애나 재실행은 W10 수집·분석 경로에 역으로 연결되지 않는다.

## 11 · Analysis Backfill and Continuation

W11은 `settings.timezone=Asia/Seoul`, 15분 schedule, `analysis-backfill-1.2` 계약으로
`publish:true`, `promotionState=verified-live-e2e`다. Scheduled 실행은
`queue_name=ANY`, `resume_only=true`로 기존 active parent만 찾고 DAILY를 우선한다.
수동 실행은 BACKFILL parent를 만들거나 재개할 수 있다.

plan 응답의 `job_id`, `segment_id`, `chunks`, `chunk_indices`를 exact claim으로 사용해
호출당 최대 3건, 실행당 최대 30건을 직렬 처리한다. 모든 leased child가 terminal일
때만 `/complete`를 호출한다. 동일 request token 재시도는 같은 lease/chunks를
돌려주고, stale lease·부분 실패·최대 128 continuation은 backend 감사 상태로 남긴다.
W11은 공고 수집이나 Teams 전송을 수행하지 않는다.

## 12 · Teams Daily Delivery

W12는 `settings.timezone=Asia/Seoul`, 매일 10:00 schedule,
`teams-delivery-1.2` 계약으로 `publish:true`,
`promotionState=verified-live-e2e`다. W10/W11을 호출하지 않고 저장된 7일 브리핑을
고정 공개 origin `https://pai-loop-demo.onrender.com`에서 한 번 읽어 공고 최대
6건의 sanitized HTML을 native Microsoft Teams v2
`channelMessage/create` 노드 하나로 `PAI 봇` 채널에 전송한다. Adaptive Card는 같은
공개 allowlist의 offline preview 계약으로 유지한다.

Data Table 여섯 Gate가 모두 유효하고 `push_enabled=true`,
`approval_state=APPROVED`, `emergency_disabled=false`여야 실제 sink에 도달한다.
수동 live test는 `live_test_enabled=true`도 요구한다. 전송 전 backend mock endpoint에
`teams-daily:{KST 날짜}:{sanitized payload fingerprint}` correlation을 예약하며,
최초 owner만 전송한다. 중복·예약 오류·Teams 오류는 raw 본문 없이 fail-closed하고
자동 retry하지 않는 at-most-once 경계를 사용한다. 따라서 Teams 장애는 W10/W11의
수집·분석을 재실행하지 않는다. 상세 운영·긴급 중지는
[`../TEAMS_DELIVERY_RUNBOOK_v0.9.0.md`](../TEAMS_DELIVERY_RUNBOOK_v0.9.0.md)를 따른다.

## 7일 보관 범위

7일은 **아침 피드 조회 창**과 완료 ingestion job/mock 알림 같은 단기 운영 로그에
적용한다. 공고 원장, 평가, 사용자 결정, 3년 낙찰 관측을 연쇄 삭제하지 않는다.
이 기록은 감사, 가격 관측, 결과 환류에 필요하다. `/operations/retention`은 기본
`dry_run=true`이고 RUNNING job은 어떤 경우에도 삭제하지 않는다.

## Validation

```powershell
node scripts/deploy-workflows.mjs --validate-only
node scripts/test-daily-workflow.mjs
node scripts/test-teams-delivery-workflow.mjs
```

추가 계약 테스트는 다음을 확인한다.

- manual/scheduled/live-enable 조합별 `dry_run` Gate;
- PPS response count와 배열 필드;
- W10/W11의 호출당 최대 3건·실행당 최대 30건, exact segment/chunk lease와 집계 합계;
- 낙찰 response의 exact field set, `COMPLETED|PARTIAL`, 기간·집계·경고 계약;
- `PARTIAL` 응답의 경고 필수 및 후보 행/PII가 최종 출력에서 제거되는지;
- Adaptive Card 1.5 및 Teams message attachment wrapper;
- W10/W11/00~04의 실제 Teams 호출 0개와 W12의 native Microsoft Teams v2 sink 정확히 1개;
- W12 offline preview 외부 호출 0개, Data Table 6-key Gate, 24KB payload,
  persistent reservation과 중복 억제;
- secret value와 credential ID 0개.
