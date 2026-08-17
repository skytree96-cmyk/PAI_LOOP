# PAI_LOOP 오전 9시 통합 브리핑 운영서 v0.7.0

기준일: 2026-08-17

기준 workflow: `PAI_LOOP 10 - Daily Opportunity Briefing`

현재 운영 상태: `active` (`manifest.json`의 Workflow 10 `publish: true`)

2026-08-17 온라인 E2E에서 7일 PPS 백필, 3년 낙찰 조회, PDF 첨부 분석,
평가·스냅샷 저장, 공개 근거 렌더링과 Teams mock 기록을 확인한 뒤 승격했다.

이 문서는 v0.6.0을 보존한 다음 버전이다. 운영 진입점은 10번 하나뿐이며,
00~04는 비활성 계약시험·rollback 자산이다. Workflow 10을 활성화하면 별도 live
환경변수가 없어도 명시적 workflow 설정으로 매일 09:00 KST 수집을 수행한다.

## 예약 실행 계약

```text
09:00 Asia/Seoul
  → 전일~당일 PPS 공고 수집·DB upsert
  → backend 조직 keyword profile로 검색어 확장·부서 ranking
  → latest valid 공고 revision별 중복 제거
  → ranking score DESC, published_at DESC notice_keys
  → 완료 수집로그와 Teams mock 로그 7일 retention
  → 조직 ranking 상위 공고의 3년 낙찰 이력 갱신(기본 1건, hard max 3)
  → 상위 최대 3건 첨부 보강(공고당 최대 1개)·평가·snapshot
  → 최근 7일 브리핑과 Adaptive Card 1.5 조립
  → backend Teams mock 기록(실제 Teams 전송 없음)
```

Daily ingestion은 `교육`, `컨설팅`을 기본 검색어로 보내며
`use_profile_keywords: true`, `profile_department_ids: []`를 함께 보낸다. 빈 부서 ID
목록은 backend가 관리하는 전체 조직 profile을 의미한다. 조직도의 keyword catalog를
n8n JSON에 복사하지 않으므로 Git 기준자료가 바뀌면 backend 동기화만으로 반영된다.
Backend contract는 기본 `교육`, `컨설팅`과 24개 부서의 첫 unique strong keyword를
중복 제거해 총 26개(상한 30) provider query로 보장한다. Supporting keyword는
외부 query가 아니라 ranking에만 쓴다. n8n 응답 validator는 `keywords_used` 상한
30, 첫 두 baseline, `department_coverage_count: 24`를 함께 검증한다.

일일 provider 범위는 전일~당일, keyword별 `page_size: 100`, `max_pages: 1`이다.
최초 7일 backfill은 일일 schedule에 넣지 않고 운영자가 별도 bounded one-shot으로
수행한다.
전체 조직 profile의 bounded multi-keyword 수집을 위해 n8n PPS HTTP 경계도 최대
10분으로 제한한다.

## Ingestion 요청·응답

```json
{
  "from_date": "YYYY-MM-DD (전일)",
  "to_date": "YYYY-MM-DD (당일)",
  "keywords": ["교육", "컨설팅"],
  "use_profile_keywords": true,
  "profile_department_ids": [],
  "page_size": 100,
  "max_pages": 1,
  "dry_run": false
}
```

Workflow는 기존 응답 계약에 더해 `keywords_used`, `provider_queries`,
`manifests_created`, `manifests_reused`, `attachments_discovered`를 검증한다.
`notice_keys` 순서는 backend가 latest valid revision만 남긴 후 조직 ranking과
게시일로 정렬한 결과이므로, n8n은 이 배열의 앞에서 최대 3건만 분석한다.
`COMPLETED`이면 `provider_queries == keywords_used.length`를 요구한다. 190초 backend
wall budget 등으로 일부 query만 끝나면 `PARTIAL`과 warning을 허용하고, 성공한
`notice_keys`는 계속 award·analysis로 보내되 최종 workflow 출력에 ingestion 상태와
경고를 남긴다.

## 상위 3건 분석·첨부 보강

```json
{
  "notice_keys": ["ranked notice key, 최대 3건"],
  "dry_run": false,
  "force": false,
  "enrich_missing": true,
  "max_notices": 3,
  "max_attachments_per_notice": 1
}
```

응답의 기존 aggregate/result 계약을 유지하고 선택 필드 `enrichment`를 허용한다.
새 backend는 다음 요약을 반환한다.

```text
requested, attempted, completed, skipped, failed,
attachments_discovered, attachments_processed, openai_calls, warnings
```

Workflow는 처리 합계, 공고당 첨부 1개 상한, OpenAI 실제 호출 수와 전체
`openai_calls` 일치를 검증한다. 문서 본문·개인정보·provider 원문은 n8n item이나
Teams card에 싣지 않고 DB의 공개 근거 anchor와 상태 요약만 사용한다.
상위 3건의 동기 첨부 처리를 위해 n8n HTTP 경계는 최대 10분으로 제한한다. Backend는
개별 공고 실패를 `FAILED` result와 전체 `PARTIAL`로 반환하므로 성공한 다른 공고의
결과를 버리지 않는다.

낙찰 refresh를 분석보다 먼저 실행하므로 당일 생성되는 가격·경쟁집중·낙찰 snapshot이
새 3년 관측을 즉시 사용한다. 초기 기본값은 비용·실행시간 검증을 위해 ranking 1위
1건이고 workflow hard max는 3건이다. 공고별 HTTP 경계는 10분이다. Online E2E의
실측 시간이 안정적일 때만 명시적 workflow config를 3으로 올린다.

## 명시적 운영 설정과 비상 중지

예약 경로의 기본값은 PPS write, 상위 3건 분석 write, 7일 단기 로그 retention,
상위 3건 낙찰 refresh, backend Teams mock 기록이 모두 활성이다. 이는 n8n host가
`$env` 읽기를 제한해도 동일하게 동작한다.

비상 중지는 다음 한 변수만 사용한다.

```text
PAI_LOOP_EMERGENCY_DISABLE=true
```

정확히 `true`이면 PPS·분석·retention·낙찰·mock 기록 gate가 모두 닫힌다. 변수
적용이 어려우면 n8n UI에서 Workflow 10을 비활성화한다. 실제 Teams 전송은 이
설정과 무관하게 구현되어 있지 않으며 계속 mock이다.

`PAI_LOOP_API_BASE_URL`과 `PAI_LOOP_WEB_BASE_URL`은 유효한 HTTP(S) URL일 때만
사용한다. 없거나 n8n이 `$env` 접근을 금지하면
`https://pai-loop-demo.onrender.com`을 사용한다.

## Credential 7/7 확인

Generic Header Auth credential `PAI_LOOP Render Backend`를 다음 HTTP 노드 모두에
연결한다.

1. `Refresh PPS Notices Behind Gate`
2. `Analyze Evaluate and Snapshot PPS Notices`
3. `Preview or Apply Seven-Day Log Retention`
4. `Fetch Award Candidates from Seven-Day Briefing`
5. `Refresh Bounded Three-Year Award History`
6. `Fetch Ranked Seven-Day Briefing`
7. `Record Consolidated Teams Mock in Backend`

배포기는 원격 workflow의 동일 node name/type에 연결된 credential만 보존한다.
저장소 JSON에는 credential ID나 API key를 기록하지 않는다.

## 활성화 및 재배포 검수 순서

1. backend 배포에서 새 ingestion·analysis contract 테스트가 통과했는지 확인한다.
2. 로컬에서 다음을 실행한다.

   ```powershell
   node scripts/deploy-workflows.mjs --validate-only
   node scripts/test-daily-workflow.mjs
   ```

3. 신규 대규모 변경의 online E2E 전에는 Workflow 10만 임시로 `publish: false`로
   같은 원격 ID에 갱신한다. 현재 검증된 운영 릴리스는 `publish: true`다.

   ```powershell
   node scripts/deploy-workflows.mjs --only=pai-loop-10-daily-opportunity-briefing
   ```
4. E2E 전에는 원격 workflow가 inactive이고 7개 HTTP 노드 credential이 7/7인지
   확인한다.
5. `Run Complete Offline Dry-Run`을 실행해 모든 외부 호출이 0인지 확인한다.
6. 운영자가 전일~당일, page 1, 최대 3건 경계의 online E2E 한 번을 승인·실행한다.
7. DB에서 ingestion job, PPS notice, 첨부 manifest, evaluation/snapshot이 연결되었는지
   확인하고 웹의 진행 공고와 근거 표시를 점검한다.
8. 성공한 뒤 Workflow 10만 활성화한다. 00~04는 계속 inactive로 둔다.

지속 운영 승격은 `manifest.json`에서
`pai-loop-10-daily-opportunity-briefing.publish`만 `true`로 바꾸고 배포한다.
배포 validator는 `operatorEntryPoint: true`인 10번만 publish를 허용하며, 00~04와
deployment smoke workflow는 항상 `false`를 요구한다. 따라서 이후 GitHub 배포도
10번을 다시 비활성화하지 않고 09:00 schedule을 유지한다. Online E2E 전에 이 값을
미리 바꾸지 않는다.

## 보관 범위

7일 retention은 완료된 ingestion job과 Teams mock 같은 단기 운영로그에 적용한다.
공고 원장, 공개 첨부 manifest, 회사 기준자료, 평가·정량·추천 snapshot, 결과·낙찰
관측은 감사·재현을 위한 업무 데이터이므로 7일 정책으로 연쇄 삭제하지 않는다.
