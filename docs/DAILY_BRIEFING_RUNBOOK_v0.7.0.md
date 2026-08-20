# PAI_LOOP 오전 8시 통합 브리핑 운영서 v0.7.0

기준일: 2026-08-19

기준 workflow: `PAI_LOOP 10 - Daily Opportunity Briefing`

현재 운영 상태: `active` (`manifest.json`의 Workflow 10 `publish: true`)

2026-08-17 온라인 E2E에서 7일 PPS 백필, 3년 낙찰 조회, PDF 첨부 분석,
평가·스냅샷 저장, 공개 근거 렌더링과 Teams mock 기록을 확인한 뒤 승격했다.

이 문서는 v0.6.0을 보존한 다음 버전이다. 운영 진입점은 10번 하나뿐이며,
00~04는 비활성 계약시험·rollback 자산이다. Workflow 10을 활성화하면 별도 live
환경변수가 없어도 명시적 workflow 설정으로 매일 08:00 KST 수집을 수행한다.

## 예약 실행 계약

```text
08:00 Asia/Seoul
  → 당일 포함 최근 8개 calendar day PPS 공고 수집·DB upsert
  → backend 조직 keyword profile로 검색어 확장·부서 ranking
  → latest valid 공고 revision별 중복 제거
  → ranking score DESC, published_at DESC notice_keys
  → 완료 수집로그와 Teams mock 로그 7일 retention
  → 조직 ranking 상위 공고의 3년 낙찰 이력 갱신(기본 1건, hard max 3)
  → 신규·정정 전량 우선 + cooled backlog 최대 12건을 durable parent에 예약
  → 실행당 최대 30건, HTTP chunk당 최대 3건 첨부 보강(공고당 최대 1개)·평가·snapshot
  → 최근 7일 브리핑과 Adaptive Card 1.5 조립
  → backend Teams mock 기록(실제 Teams 전송 없음)
```

Daily ingestion은 `교육`, `컨설팅`, `연수`, `포럼`, `위탁 운영`을 기본 검색어로 보내며
`use_profile_keywords: true`, `profile_department_ids: []`를 함께 보낸다. 빈 부서 ID
목록은 backend가 관리하는 전체 조직 profile을 의미한다. 조직도의 keyword catalog를
n8n JSON에 복사하지 않으므로 Git 기준자료가 바뀌면 backend 동기화만으로 반영된다.
Backend contract는 기본 5개와 24개 부서의 첫 unique strong keyword를
중복 제거해 현재 총 29개(상한 30) provider query로 보장한다. Supporting keyword는
외부 query가 아니라 ranking에만 쓴다. n8n 응답 validator는 `keywords_used` 상한
30, baseline 5개, `department_coverage_count: 24`를 함께 검증한다.

일일 provider 범위는 당일 포함 최근 8개 calendar day의 단일 query window이고,
keyword별 `page_size: 999`, `max_pages: 3`이다. 날짜별로 query를 8번 반복하지
않으므로 profile 24개를 포함해 최대 29 provider query만 사용한다. 개별 keyword가
2,997행 상한에 닿으면 조용히 성공하지 않고 `PARTIAL`로 중단한다.
전체 조직 profile의 bounded multi-keyword 수집을 위해 n8n PPS HTTP 경계도 최대
10분으로 제한한다.

## Ingestion 요청·응답

```json
{
  "from_date": "YYYY-MM-DD (당일 포함 8일 전 시작일)",
  "to_date": "YYYY-MM-DD (당일)",
  "keywords": ["교육", "컨설팅", "연수", "포럼", "위탁 운영"],
  "use_profile_keywords": true,
  "profile_department_ids": [],
  "page_size": 999,
  "max_pages": 3,
  "dry_run": false
}
```

Workflow는 기존 응답 계약에 더해 `keywords_used`, `provider_queries`,
`manifests_created`, `manifests_reused`, `attachments_discovered`를 검증한다.
`notice_keys` 순서는 backend가 latest valid revision만 남긴 후 조직 ranking과
게시일로 정렬한 결과다. 분석 대상은 이 배열을 매일 다시 자르는 대신, 같은 실행의
daily briefing이 제공하는 `analysis_queue`에서 고른다. 큐는 최근 7일 OPEN 공고 중
`NOT_SELECTED` 미시도 건을 먼저, 재처리 가능한 실패 건은 가장 오래된 시도부터 최대
50건 제공한다. `never_attempted_notice_keys`와 `retryable_notice_keys`는 순서가
보존된 disjoint partition이고 둘의 연결은 `notice_keys`와 정확히 일치한다. n8n은
신규·정정 key는 최대 3,000건까지 전량 먼저 예약하고, 그 뒤 큐 앞의 backlog 최대
12건을 붙이되 retry partition의 key에만 retry epoch를 붙인다. 첨부
없음·구형 HWP 전용은 manifest가
바뀔 때까지 자동 재시도하지 않아 실패 건이 큐를 독점하지 않는다. 큐 계약이 없는
이전 backend에서만 ingestion `notice_keys`를 호환 fallback으로 사용한다.
`COMPLETED`이면 `provider_queries == keywords_used.length`를 요구한다. 190초 backend
wall budget 등으로 일부 query만 끝나면 `PARTIAL`과 warning을 허용하고, 성공한
`notice_keys`는 계속 award·analysis로 보내되 최종 workflow 출력에 ingestion 상태와
경고를 남긴다. 예약 수집은 `page_size=999`, `max_pages=3`으로 검색어별 최대
2,997행을 pagination한다. 개별 검색어의 provider 총건수가 이 상한을 넘은 경우도
`COMPLETED`로 가장하지 않고 `PARTIAL`과 `max_pages 제한` warning을 남긴다. 따라서
created+updated union은 "이번 실행에서 완전히 수집된 범위"인지 운영자가 구분할 수
있으며 페이지 상한 누락이 조용한 성공으로 처리되지 않는다.

## 미분석 큐 12건 예약·3건 단위 분석

공개 목록은 미분석을 한 상태로 뭉개지 않고 다음 사유를 구분한다.

- `NOT_SELECTED`: 폐기·파일 오류가 아니라 아직 bounded 일일 큐에 선택되지 않음;
- `ATTACHMENT_NONE`: 공개 첨부 manifest에서 분석 가능한 파일을 찾지 못함;
- `HWP_ONLY_UNSUPPORTED`: 구형 binary HWP만 있어 현재 온라인 추출 경계가 지원하지 않음;
- `HWPX_EXTRACT_FAILED` / `PDF_EXTRACT_FAILED`: 해당 형식의 다운로드·본문 추출 실패;
- `QUOTE_UNVERIFIED`: 본문은 읽었지만 LLM 인용문을 원문과 정확히 대조하지 못함;
- `OPENAI_REVIEW`: 구조화 응답이 검증 계약을 통과하지 못함.

HWPX는 XML paragraph/run을 복원해 문단 경계를 유지하고, NFC·zero-width·공백 차이만
허용하는 exact anchor 검증을 사용한다. 편집거리·동의어·문장 의역은 근거로 인정하지
않는다. 구형 HWP는 허위 PASS를 만들지 않고 `HWP_ONLY_UNSUPPORTED`로 격리한다. 운영
고도화 시 별도 격리 변환 worker에서 HWP→HWPX/PDF 변환 후 동일 SHA·근거 검증 경계를
다시 통과시키며, 변환 실패는 REVIEW로 유지한다.

`QUOTE_UNVERIFIED`에 한해서만 원문 그대로의 연속 인용을 다시 요구하는 corrective
extraction을 한 번 허용한다. 첫 호출과 corrective 호출을 합친 hard cap은 공고당
2회이며, 두 번째 응답도 동일한 exact anchor 검증을 통과해야 한다. 퍼지·의미
매칭으로 PASS시키지 않으며 두 번째도 실패하면 계속 `QUOTE_UNVERIFIED / REVIEW`다.

```json
{
  "notice_keys": ["durable lease가 부여한 exact chunk, 최대 3건"],
  "dry_run": false,
  "force": false,
  "enrich_missing": true,
  "max_notices": 3,
  "max_attachments_per_notice": 1
}
```

응답의 기존 aggregate/result 계약을 유지하고 선택 필드 `enrichment`를 허용한다.
새 backend는 다음 요약을 반환한다.

각 `results[]` 행은 공개 가능한 분석 커버리지 필드 `analysis_state`,
`analysis_reason_code`, `analysis_reason`을 반드시 포함한다. Workflow는 상태를
`ANALYZED / REVIEW / PENDING`으로, 사유 코드를 backend 공개 enum으로 제한하고,
빈 값 또는 500자를 넘는 사유를 거부한다. 따라서 첨부 없음·HWP 미지원·추출 실패처럼
분석 근거가 비어 보이는 이유가 n8n 요약 단계에서 유실되지 않는다.

```text
requested, attempted, completed, skipped, failed,
attachments_discovered, attachments_processed, openai_calls, warnings
```

Workflow는 처리 합계, 공고당 첨부 1개 상한, OpenAI 실제 호출 수와 전체
`openai_calls` 일치 및 공고당 최대 2회 상한을 검증한다. 문서 본문·개인정보·provider 원문은 n8n item이나
Teams card에 싣지 않고 DB의 공개 근거 anchor와 상태 요약만 사용한다.
3건 단위 chunk의 동기 첨부 처리를 위해 n8n HTTP 경계는 최대 10분으로 제한한다. Backend는
개별 공고 실패를 `FAILED` result와 전체 `PARTIAL`로 반환하므로 성공한 다른 공고의
결과를 버리지 않는다.

낙찰 refresh를 분석보다 먼저 실행하므로 당일 생성되는 가격·경쟁집중·낙찰 snapshot이
새 3년 관측을 즉시 사용한다. 초기 기본값은 비용·실행시간 검증을 위해 ranking 1위
1건이고 workflow hard max는 3건이다. 공고별 HTTP 경계는 10분이다. Online E2E의
실측 시간이 안정적일 때만 명시적 workflow config를 3으로 올린다.

## 명시적 운영 설정과 비상 중지

예약 경로의 기본값은 PPS write, 신규·정정 전량과 backlog 12건 durable 분석 예약,
7일 단기 로그 retention,
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

## Credential 9/9 확인

Generic Header Auth credential `PAI_LOOP Render Backend`를 다음 HTTP 노드 모두에
연결한다.

1. `Refresh PPS Notices Behind Gate`
2. `Reserve or Resume Daily Analysis Operation`
3. `Analyze Evaluate and Snapshot PPS Notices`
4. `Finalize Daily Analysis Segment`
5. `Preview or Apply Seven-Day Log Retention`
6. `Fetch Award Candidates from Seven-Day Briefing`
7. `Refresh Bounded Three-Year Award History`
8. `Fetch Ranked Seven-Day Briefing`
9. `Record Consolidated Teams Mock in Backend`

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
4. E2E 전에는 원격 workflow가 inactive이고 9개 HTTP 노드 credential이 9/9인지
   확인한다.
5. `Run Complete Offline Dry-Run`을 실행해 모든 외부 호출이 0인지 확인한다.
6. 운영자가 당일 포함 8일, keyword별 최대 3페이지, 실행당 최대 30건·chunk당 최대
   3건 경계의 online E2E 한 번을 승인·실행한다.
7. DB에서 ingestion job, PPS notice, 첨부 manifest, evaluation/snapshot이 연결되었는지
   확인하고 웹의 진행 공고와 근거 표시를 점검한다.
8. 성공한 뒤 Workflow 10만 활성화한다. 00~04는 계속 inactive로 둔다.

지속 운영 승격은 `manifest.json`에서
`pai-loop-10-daily-opportunity-briefing.publish`만 `true`로 바꾸고 배포한다.
배포 validator는 `operatorEntryPoint: true`인 10번만 publish를 허용하며, 00~04와
deployment smoke workflow는 항상 `false`를 요구한다. 따라서 이후 GitHub 배포도
10번을 다시 비활성화하지 않고 08:00 schedule을 유지한다. Online E2E 전에 이 값을
미리 바꾸지 않는다.

## 보관 범위

7일 retention은 완료된 ingestion job과 Teams mock 같은 단기 운영로그에 적용한다.
공고 원장, 공개 첨부 manifest, 회사 기준자료, 평가·정량·추천 snapshot, 결과·낙찰
관측은 감사·재현을 위한 업무 데이터이므로 7일 정책으로 연쇄 삭제하지 않는다.
