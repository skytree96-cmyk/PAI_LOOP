# PAI_LOOP 오전 9시 통합 브리핑 운영서 v0.6.0

기준일: 2026-08-17

기준 workflow: `PAI_LOOP 10 - Daily Opportunity Briefing`
배포 기본값: `inactive`, 모든 live gate `false`

이 문서는 v0.5.0을 보존하고, PPS 신규 공고의 batch-analysis 경계를 추가한 다음
버전이다. 운영자는 10번만 실행·모니터링한다. 00~04는 비활성 회귀/rollback
자산이며 동시에 활성화하지 않는다.

## 실행 순서

예약 실행의 의도된 순서는 다음과 같다.

```text
09:00 Asia/Seoul
  → PPS 전일~당일 bounded 수집
  → PPS notice_keys strict validation / 중복 제거
  → 최대 5건(환경 상한 10) batch analysis plan
  → 저장된 ACCEPTED extraction materialize · 평가 · snapshot 집계 validation
  → 7일 단기 운영로그 retention preview/apply
  → 저장된 최근 7일 공고로 award 후보 선정
  → 비-FAIL 상위 최대 3건의 3년 낙찰 refresh
  → daily briefing 재조회
  → 부서 · 적합성 · 정량 · 가격 · 리스크 정규화
  → Adaptive Card 1.5
  → backend Teams mock 기록 또는 local mock
```

Batch-analysis가 열려 있고 응답 검증에 실패하면 낙찰 refresh나 briefing으로
넘어가지 않는다. 분석 gate가 닫혔거나 신규 key가 0개면 명시적 `SKIPPED` 결과를
만들고 다음 단계로 진행한다. 이 endpoint는 저장된 검증 완료 extraction만 사용하며
OpenAI나 원격 첨부 다운로드를 직접 호출하지 않는다.

## Batch-analysis API 계약

```http
POST /api/v1/notices/analysis/batch
Content-Type: application/json
Idempotency-Key: analysis:daily:<YYYY-MM-DD>:<dry-run>
```

```json
{
  "notice_keys": ["PPS notice key"],
  "dry_run": true,
  "force": false
}
```

요청 key는 중복 제거 후 기본 5개, 최대 10개다. 응답 허용 필드는 다음과 같다.

```text
job_id, status, dry_run,
requested, processed, completed, skipped, failed,
document_materialized, evaluations_created, snapshots_refreshed, openai_calls,
results, warnings
```

`status`는 `COMPLETED|PARTIAL`, result status는
`COMPLETED|SKIPPED|FAILED`다. 각 result의 exact field set은 `notice_key`,
`status`, `document_status`, `evaluation_status`, `snapshot_status`, nullable
`analysis_run_id`, `evaluation_id`, `notice_version_id`, `input_sha256`, `reused`,
`materialized_requirements`, `requirement_snapshots`, `score_snapshots`,
`recommendation_snapshots`, `warnings`다.
Idempotency key 자체는 응답에 노출하지 않는다. 다음 invariant를 위반하면
workflow를 중단한다.

- `processed = completed + skipped + failed`;
- `requested`는 실제 요청한 unique key 수와 같다;
- result key는 요청 key의 중복 없는 부분집합이다;
- `dry_run=true`이면 materialized/evaluation/snapshot write count는 모두 0;
- 모든 count는 0 이상의 safe integer;
- 문서본문, 원자 근거, 개인정보, provider 응답은 n8n item에 싣지 않는다.

`dry_run=true`도 실행 추적용 `ANALYSIS` ingestion job 한 건은 남긴다. 이는 문서·평가·
snapshot 업무 레코드 생성이 아니며 7일 단기 운영로그 retention 대상이다.

## 환경변수 Gate

모두 정확히 문자열 `true`일 때만 열린다. 미설정은 `false`다.

| 변수 | 의미 |
|---|---|
| `PAI_LOOP_DAILY_LIVE_ENABLED` | 예약 live 경계의 최상위 gate |
| `PAI_LOOP_ANALYSIS_BATCH_ENABLED` | PPS 신규 key batch 호출 허용 |
| `PAI_LOOP_ANALYSIS_BATCH_WRITE_ENABLED` | 문서·평가·snapshot 저장 허용 |
| `PAI_LOOP_ANALYSIS_BATCH_LIMIT` | 기본 5, 허용 1~10 |
| `PAI_LOOP_RETENTION_LIVE_ENABLED` | 7일 초과 단기 로그 실제 삭제 허용 |
| `PAI_LOOP_AWARD_REFRESH_ENABLED` | 3년 낙찰 조회 허용 |
| `PAI_LOOP_AWARD_REFRESH_WRITE_ENABLED` | 낙찰 관측 저장 허용 |
| `PAI_LOOP_AWARD_REFRESH_LIMIT` | 기본 3, 허용 1~5 |
| `PAI_LOOP_TEAMS_MOCK_LOG_ENABLED` | backend mock 기록 허용 |

`PAI_LOOP_API_BASE_URL`과 `PAI_LOOP_WEB_BASE_URL`은 `$env`를 우선하고 없으면
`https://pai-loop-demo.onrender.com`을 사용한다. URL에 사용자명·비밀번호를
넣지 않는다.

## n8n Credential 연결

Generic Header Auth credential `PAI_LOOP Render Backend`를 다음 7개 HTTP Request
노드에 각각 선택한다.

1. `Refresh PPS Notices Behind Gate`
2. `Analyze Evaluate and Snapshot PPS Notices`
3. `Preview or Apply Seven-Day Log Retention`
4. `Fetch Award Candidates from Seven-Day Briefing`
5. `Refresh Bounded Three-Year Award History`
6. `Fetch Ranked Seven-Day Briefing`
7. `Record Consolidated Teams Mock in Backend`

Repository JSON에는 credential 값이나 ID가 없다. 배포기는 동일 node name과 type의
기존 원격 credential 연결만 보존한다. 미연결 상태에서는 인증을 생략하지 않고
실행 단계에서 fail-closed한다.

## 안전한 검수 순서

1. workflow가 inactive인지 확인한다.
2. 저장소에서 아래 offline 검증을 실행한다.

   ```powershell
   node scripts/deploy-workflows.mjs --validate-only
   node scripts/test-daily-workflow.mjs
   ```

3. `Run Complete Offline Dry-Run`만 수동 실행한다.
4. 결과가 `DRY_RUN_PASSED`, `externalCalls` 전부 0인지 확인한다.
5. credential 7개를 연결하되 아직 활성화하지 않는다.
6. batch endpoint backend 테스트와 DB persistence 검증이 통과한 뒤 analysis gate를
   열어 `WRITE_ENABLED=false`인 dry-run 한 번을 별도 승인하에 수행한다.
7. dry-run에서 요청/처리/result key와 write count 0을 확인한다.
8. 문서·평가·snapshot 저장을 승인한 뒤에만 write gate를 연다.
9. Teams 회사 승인 전에는 mock log만 사용하며 실제 전송 노드를 추가하지 않는다.

## 보관과 비용

7일 정책은 브리핑 조회 창과 완료 ingestion/mock 같은 단기 운영로그에 적용한다.
공고 원장, 평가, 결정, 공개 근거와 3년 낙찰 관측은 연쇄 삭제하지 않는다.
Batch 최대 10, award 최대 5라는 상한은 비용·실행시간·provider 장애 확산을 제한한다.
`openai_calls`는 backend가 반환한 실제 count만 표시하며 n8n이 추정하지 않는다.

## 현재 구현과 TARGET 경계

Workflow 10은 PPS key에서 backend batch-analysis로 이어지는 orchestration 경계를
구현한다. 원격 첨부의 자동 다운로드, PDF/HWP 변환 자체와 입찰·개찰·낙찰·계약
결과 자동환류는 아직 `TARGET · 현재 미연결`이다. 아키텍처 이미지와 데모 설명에서
이를 현재 구현처럼 표현하지 않는다.
