# PAI_LOOP 분석 backfill·08:00 continuation 운영서 v0.8.0

## 운영 결과

Workflow 10은 매일 08:00 KST 수집 응답의 `created_notice_keys`와
`updated_notice_keys`를 exact union으로 만들고, 저장된 분석 큐의 우선순위 상위
12건을 뒤에 붙인다. 신규·정정 3,000건과 backlog 12건까지 명시적으로 허용하며,
초과 범위는 잘라내지 않고 실패한다.

수집은 `교육`, `컨설팅`, `연수`, `포럼`, `위탁 운영`과 조직 profile 24개를
중복 제거한 현재 29개 keyword를 당일 포함 최근 8개 calendar day 단일 window로
조회한다. 날짜별로 query를 반복하지 않으며 keyword별 999행·최대 3페이지 상한은
그대로 유지한다.

Workflow 10은 수집 범위를 `page_size=999`, `max_pages=3`으로 설정해 검색어별
최대 2,997행을 실제 pagination한다. 조달청 provider가 이 상한보다 많은 행을 보고하면 수집
응답은 warning과 함께 `PARTIAL`이다. Workflow 10은 확보한 created+updated key를
계속 durable queue에 넣되, 페이지 상한 누락을 `COMPLETED`로 표시하지 않는다.

`updated_notice_keys`는 plan의 `refresh_notice_keys`로도 별도 전달한다. 동일한
stable notice key가 이미 terminal이어도 최신 notice metadata/manifest work token이
바뀌었으면 generation을 올려 기존 child 결과는 감사용으로 보존하고 새 chunk에서
다시 분석한다. token은 authoritative `PPS_NOTICE_METADATA`와 notice metadata 시각만
사용하며 분석 출력인 `OPENAI_REQUIREMENT_EXTRACTION` version은 제외한다. 따라서
동일 08:00 요청 재시도는 generation을 재증가시키지 않는다.

daily briefing은 합계 순서를 보존한 `notice_keys`와 함께
`never_attempted_notice_keys`, `retryable_notice_keys`를 서로 겹치지 않는 exact
partition으로 제공한다. Workflow 10은 전체 우선순위에서 backlog 최대 12건을 고르되
실제 retry partition에 속한 key만 `retry_notice_keys`와 수집일 기반 `retry_epoch`로
별도 전달한다. `NOT_SELECTED`는 generation 0의 일반 첫 시도로 남는다. 서버가 현재
reason code와 24시간 cooldown을 다시 검증한 retry key만 generation을 한 번 올린다.
같은 epoch의 HTTP/workflow 재시도는 무증가이므로 active DAILY parent가 하루 이상
이어져도 실패 key를 잃거나 중복 재실행하지 않는다. 구버전 workflow가 미시도 key를
retry로 잘못 표시해도 서버는 해당 key를 일반 첫 시도로 보존한다. 기존 Evaluation은
있지만 AnalysisRun snapshot이 없는 legacy `ANALYZED`도 retryable로 정렬하며, accepted
extraction을 재사용해 OpenAI 호출 없이 판단완료 snapshot을 복구한다.

retry epoch token은 활성 parent뿐 아니라 terminal DAILY parent와 그 effective terminal
child를 durable ledger로 사용한다. 따라서 분석이 끝난 뒤 downstream 단계 실패로 같은
날 Workflow 10 전체를 다시 실행해도 동일 key/epoch는 새 parent에서 `offered=0`이며
provider/OpenAI child를 다시 만들지 않는다. 실제 child가 전혀 실행되지 않은 parent는
token을 소비한 것으로 보지 않아 복구 작업을 막지 않는다.

Workflow 11은 수동 일회성 `OPEN / NOT_SELECTED + 24시간 cooldown 경과 retryable`
backfill과 DAILY continuation을
같은 계약으로 재개한다. 한 n8n 실행이 전량을 붙잡지 않는다. 실행당 최대 30건,
HTTP 호출당 최대 3건을 직렬 처리한다. 2026-08-19 기준 OPEN
`QUOTE_UNVERIFIED` 58건의 일회성 회수는 cooldown 충족 뒤 첫 segment 30건,
15분 continuation의 다음 segment 28건으로 끝낸다. 이후 일일 자동 retry는 최대
12건으로 제한한다.

`QUOTE_UNVERIFIED`만 strict corrective extraction을 한 번 허용한다. 원문 그대로의
연속 인용을 요구하고 두 번째 결과도 exact anchor 검증을 다시 통과시킨다. 퍼지 또는
의미 매칭은 금지하며, 첫 호출을 포함한 hard cap은 공고당 2회다. 따라서 일일 backlog
12건의 추가 OpenAI 요청 상한은 24회이고, 58건 일회성 회수의 이론상 상한은 116회다.
실패한 두 번째 응답은 계속 `QUOTE_UNVERIFIED / REVIEW`로 남긴다.

## 저장 계약

관리형 PostgreSQL의 `ingestion_jobs`가 operation 감사 원장이다.

- 부모: `source=ANALYSIS_BACKFILL`, `queue_name=DAILY|BACKFILL`, 전체 계획 key,
  `planned/attempted/remaining`, continuation 제한을 보존한다.
- 활성 segment: 부모 `request_json`의 `lease_id`, `lease_started_at`,
  `leased_keys`, `leased_chunks`, `next_chunk_index`에 원자적으로 보존한다.
- 자식: 각 `/notices/analysis/batch` 호출이 `source=ANALYSIS` row로 남고,
  `parent_job_id + segment_id + chunk_index + notice_keys`와 sanitised 결과를 기록한다.
- 분석 결과: 기존 analysis run·평가·requirement/score/recommendation snapshot DB에
  멱등 저장한다. 큐 감사 row와 판단 결과 snapshot은 서로 대체하지 않는다.

## API 순서

### 1. 부모·segment 예약

수동 backfill 최초 실행은 다음 계약을 사용한다.

```json
{
  "queue_name": "BACKFILL",
  "dry_run": false,
  "chunk_size": 3,
  "max_total": 3000,
  "execution_limit": 30,
  "max_continuations": 128,
  "include_retryable": true,
  "retry_cooldown_hours": 24,
  "reservation_ttl_hours": 6,
  "request_token": "w11:<n8n execution id>:manual",
  "resume_active": true,
  "resume_only": false
}
```

`POST /api/v1/operations/analysis-backfills/plan`은 새 부모를 만들거나 기존 부모를
재사용한다. 응답의 `segment_id`, `chunks`, `chunk_indices`가 이번 실행의 유일한
작업 권한이다. 활성 lease가 있으면 두 번째 planner에는 `offered=0`과 빈 chunk를
반환한다. 단, 같은 n8n execution의 HTTP 재시도는 stable `request_token`이 lease
owner와 정확히 일치하므로 response loss 이전의 같은 `segment_id`, chunks,
chunk indices를 재반환한다. 다른 execution token은 같은 lease를 fan-out하지 못한다.
`queue_name=ANY`에서도 exact lease owner token은 일반 DAILY-first 선택보다 우선한다.
따라서 BACKFILL plan 응답 유실 직후 새 DAILY parent가 생겨도 동일 W11 retry는 원래
BACKFILL lease를 찾아 복구한다.

최초 부모의 조회·생성 경계는 PostgreSQL 고정 namespace advisory transaction
lock으로 전역 직렬화한다. 따라서 Workflow 10, Workflow 11 schedule, 수동 실행이
동시에 들어와도 같은 미처리 key를 가진 부모 두 개를 만들지 않는다. 다른 queue의
활성 부모가 이미 예약한 미처리 key도 두 번째 부모 계획에서 제외한다. SQLite
회귀 환경은 동일 계약을 프로세스 전역 잠금으로 검증한다.

Planner와 `/complete`는 같은 arbitration lock → parent row lock 순서를 사용한다.
active parent 조회·append·segment lease는 중간 commit 없이 한 transaction으로
끝나므로 08:00 신규 append와 15분 finalize가 겹쳐도 terminal parent에 새 key가
유실되지 않는다.

15분 schedule은 `queue_name=ANY`, `resume_only=true`를 사용한다. 활성 부모가
없으면 `NO_ACTIVE`를 반환하며 audit row를 만들지 않는다. 여러 부모가 있으면
DAILY를 BACKFILL보다 먼저 고른다.

### 2. exact chunk 실행

각 chunk는 다음 세 값을 모두 전송해야 한다.

```json
{
  "operation_id": "parent UUID",
  "segment_id": "active lease UUID",
  "chunk_index": 17,
  "notice_keys": ["정확히 lease에 매핑된 최대 3개 key"],
  "max_notices": 3
}
```

서버는 부모 row를 잠그고 현재 lease와 exact chunk map을 확인한다. 같은 index의
다른 key, 다른 segment, 다른 index의 중복 key, non-stale RUNNING claim은 409로
거절한다. 이미 terminal인 exact 동일 요청은 저장된 sanitised 응답을 돌려주므로
n8n HTTP retry가 OpenAI 호출을 중복시키지 않는다.

### 3. segment 완료

모든 chunk를 직렬 처리한 뒤 다음 body로 완료한다.

```json
{"segment_id":"active lease UUID"}
```

`POST /api/v1/operations/analysis-backfills/{job_id}/complete`는 exact 현재 lease만
허용한다. leased chunk 전부가 terminal이고 RUNNING child가 0일 때만 lease를
해제한다. 하나라도 누락·진행 중이면 409와 `lease_retained=true`를 반환한다.
완료 응답은 다음 segment를 즉시 fan-out하지 않는다. 다음 15분 planner가 새
lease를 받아 실행 간 wall-time을 제한한다.

완료 commit 뒤 HTTP 응답만 유실된 경우 `last_finalized_segment_id`와 sanitised
aggregate가 durable parent에 남는다. 새 active lease가 없는 동안 같은 exact
`segment_id` 재요청은 mutation 없이 200과 현재 동일 aggregate를 반환한다. 다른
segment 또는 이미 다음 lease가 열린 뒤의 오래된 segment는 계속 409다.

## 실패·재개 정책

- 활성 lease 중 겹친 schedule: `offered=0`; 병렬 OpenAI fan-out 없음.
- 6시간 이상 응답 없는 lease: `STALE_LEASE_RECOVERED`를 기록하고 새 segment로
  다시 제공한다. 이전 `segment_id` 요청은 거절한다. 단, 모든 child가 이미
  terminal이면 새 segment를 만들지 않고 부모를 자동 완료해 ANY poll 정체를 막는다.
- 정정으로 generation이 바뀐 뒤 남은 오래된 RUNNING child는
  `SUPERSEDED_STALE_ANALYSIS_CLAIM`으로 terminal 처리하고 `requeue_notice_keys`를
  남긴다. 최신 generation 재처리는 유지하되 RUNNING audit orphan은 남기지 않는다.
- child provider 오류: FAILED/PARTIAL 자식 감사에 오류와 호출 수를 남긴다.
- retryable 추출/OpenAI 상태: daily briefing 큐가 24시간 cooldown 뒤 후보로
  다시 내고, DAILY exact union 뒤 최대 12건을 자동 포함한다.
- continuation 128회 초과: `DEAD_LETTER / MAX_CONTINUATIONS_EXCEEDED`; 3,012건은
  실행당 30건 기준 101회에 끝나므로 정상 범위 안이며, silent truncate
  또는 무한 반복 대신 운영자 검토로 전환한다.
- 08:00에 이전 DAILY가 남아 있으면 새 created+updated key를 기존 미처리 key보다
  앞에 합치며, 이미 자식 감사가 있는 key는 다시 추가하지 않는다.

## n8n 배포·승격 체크

저장소 검증은 다음과 같다.

```powershell
node scripts/test-daily-workflow.mjs
node scripts/deploy-workflows.mjs --validate-only
pytest tests/test_analysis_api.py tests/test_daily_operations.py -q
```

첫 배포 직후 확인한다.

1. Workflow 10의 backend HTTP 노드 9개가 같은 Generic Header Auth를 사용한다.
2. Workflow 11의 backend HTTP 노드 3개도 같은 credential을 사용한다.
3. Workflow 11은 `publish:false`, `promotionState=awaiting-live-e2e` 상태에서 수동
   첫 segment 최대 30건만 검증한다.
4. plan/batch/complete 응답의 planned·attempted·remaining과 child audit를 확인한다.
5. E2E가 합격한 뒤에만 Workflow 11을 `publish:true`,
   `promotionState=verified-live-e2e`로 바꾸고 재배포한다.
6. 00~04와 deployment smoke workflow는 계속 비활성으로 둔다.

실제 Teams credential은 회사 승인 전까지 mock이다. Workflow 10 카드에는 이번
segment와 전체 `planned / attempted / remaining`을 표시하고, Workflow 11 결과는
`ANALYSIS_OPERATION_PROGRESS|COMPLETED|DEAD_LETTER` 이벤트를 내보낸다. 실제 Teams
push 승격은 별도 승인 사항이다.
