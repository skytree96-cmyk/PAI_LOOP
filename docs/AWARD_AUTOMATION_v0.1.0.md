# 진행 중인 저장 공고의 3년 낙찰 이력 일괄 자동 갱신

기준일: 2026-09-13. 이 문서는 코드와 배포 계약이다. `publish: true`와 로컬
검증 성공만으로 원격 활성화나 전체 수집 완료를 주장하지 않는다. 실제 배포,
최초 등록·실행 집계와 n8n 활성 상태는 별도 운영 확인 결과로 기록한다.

W14 `PAI_LOOP 14 - Award History Automation`이 저장 공고의 낙찰 이력 갱신을
전담한다. W10의 `Scheduled Runtime Gates`에서 `awardRefreshEnabled`와
`awardRefreshWriteEnabled`를 `false`로 고정하여 중복 수집을 막는다.
W10의 공고 수집·분석·Teams 경계와 연결 그래프는 유지한다. W00~W04는 계속
`publish: false`이며, 기존 W04를 전체 자동화 용도로 활성화하지 않는다.

## 대상과 완료 의미

- `/plan`은 저장된 모든 공고를 상태 테이블에 등록하되, 실제 수집 대상은
  현재 대표 PPS 공고 중 유효 상태가 `OPEN`이고 마감 시각이 남은 공고다.
  취소·마감·기한 경과·이전 공고로 대체된 공고 및 권위 있는 취소/격리 상태는
  수집하지 않고 `SKIPPED`로 분류한다. 수동 등록 등 PPS 외 출처도 제외한다.
  기존 낙찰 이력·상태·시도 audit는 삭제하지 않는다.
- 대상 안에서는 아직 처리하지 않은 공고를 먼저 처리한다. 수집기는 용역
  공고를 지원하며 지원하지 않는 업종·검색어는 `UNSUPPORTED`로 구분한다.
  마감된 공고는 새 provider 호출 없이 제외하며, 더 최신의 권위 있는
  연장/재공고 정보로 다시 유효한 진행 공고가 되면 자동으로 재등록할 수 있다.
- 각 공고는 최근 3년 낙찰 후보와 개찰 참여업체·기술/가격 점수를 조회한다.
  같은 검색어의 결과를 모든 공고의 확정 실적으로 복제하지 않는다.
  동일 사업 확정이나 정량 평가에 사용할 검증 실적이라는 의미도 아니다.
- 낙찰 조회는 달마다 다른 길이를 고려한 28일 구간으로 나눈다. 기존 회사별
  검색과 같은 경계를 사용하며, 2월을 포함한 30일 구간의 범위 오류와 추가
  분할 조회를 줄인다. 모든 기간을 확인하기 전에는 무결과로 완료하지 않는다.
- `COMPLETED`는 한 공고의 설정된 조회 범위를 정상 처리한 상태다.
  `NO_RESULTS`는 정상 조회 후 결과가 없는 상태이며, `PARTIAL`은 조회 오류나
  호출·페이지 한도로 조회 범위가 남은 상태다. 등록·대기와 완료는 다르다.
- 신규 공고, 제목/업종 변경 공고, 완료 또는 정상 빈 결과 후 30일이 지난
  공고를 자동 등록·재등록한다. 부분/실패는 backend 지수 backoff로 재시도하고
  한 주기에서 3회 실패하면 상태를 남겨 운영자가 확인할 수 있게 한다.

## 비용·한도와 재개

낙찰 자동화는 보호된 backend의 PPS 수집 경로만 호출한다. 문서 분석, Claude,
OpenAI, Teams 호출은 없으며 응답의 `ai_calls`는 반드시 `0`이다. 기존 서버와
DB 자원은 사용하므로 서비스 사용량에 따른 인프라 비용까지 무조건 무료라고
보장하는 계약은 아니다.

2026-09-13 공공데이터포털의 실제 승인 화면에서 관련 낙찰·개찰 operation의
일일 트래픽 1,000건을 확인했다. `이용허락범위 제한 없음`은 데이터 이용허락
문구이며 무제한 API 호출량이 아니다. 자동화는 낙찰·개찰 조회를 합쳐 한국
시각(KST) 당일 1,000회까지 사용하며 별도의 여유분을 빼지 않는다. 예산은
매일 00:00 KST에 새로 계산한다. 기존 `PPS_AWARD`와 `PPS_OUTCOME` audit의
당일 호출 및 미정산 예약도 포함한다. 상태를 저장하지 않는
`/company-awards/search`의 수동 호출은 DB audit에 남지 않으므로 이 집계에
포함되지 않는다. 따라서 provider가 더 먼저 한도를 거부할 수 있으며 이때
부분/실패 상태와 재시도 정보를 보존한다. 이 큐의 한도는 모든 수동 검색과
다른 PPS 업무를 통합 제한한다는 의미가 아니다.

W14는 `*/10 * * * *`, `Asia/Seoul`로 실행한다. 한 tick에서 진행 중인 공고를
최대 10건 순서대로 처리한다. `1,000 - 실사용 - 미정산 예약`이 50회 미만이면
그 tick은 조회 없이 끝난다. 각 공고를 시작할 때마다 남은 예산과 150회 중
작은 값을 예약하므로 마지막 50~149회도 사용할 수 있다. 한 공고의 실제 사용량을
정산한 뒤 다음 공고의 예산을 다시 계산한다. 예산이 회복되면 다음 tick에서
이어간다. 하루 처리 공고 수는 공고별 실제 조회량에 따라 달라지므로 전량이
당일 완료된다고 약속하지 않는다.

backend는 공고 상태·시도·호출 예약을 영속 저장하고 동시 실행을 직렬화한다.
외부 수집 중에는 DB 잠금을 유지하지 않는다. 실행 중단 시 900초 lease 만료 후
복구하며 실제 사용량을 알 수 없는 당일 예약은 예산에서 보수적으로 유지한다.
최대 10건 전체가 480초 수집 시간을 공유하며, 남은 시간이 60초 미만이면 다음
공고를 시작하지 않는다. 각 collector도 남은 시간 안에서 실행한다. HTTP timeout
520초, n8n 실행 제한 570초는 유지한다. HTTP 자동 재시도·redirect는 꺼져 있다.

## API와 n8n 경계

세 endpoint는 서버 간 `X-PAI-LOOP-API-KEY` 인증을 요구한다. 키나 credential ID를
workflow JSON, 문서 또는 브라우저에 넣지 않는다.

| API | 용도 | W14 요청 |
|---|---|---|
| `GET /api/v1/operations/award-refresh/status` | 조회 전용 진행 집계 | 운영 확인용, schedule은 직접 호출하지 않음 |
| `POST /api/v1/operations/award-refresh/plan` | 전체 미등록·신규·변경·오래된 공고 등록 | `{"refresh_after_days":30}` |
| `POST /api/v1/operations/award-refresh/run` | 진행 공고 최대 10건 순차 claim·수집·정산 | `{"max_notices":10,"daily_api_budget":1000,"per_notice_api_budget":150}` |

공통 집계는 `schema_version`, `total`, `complete`, `no_results`, `partial`,
`pending`, `running`, `failed`, `unsupported`, `skipped`, `unplanned`, `eligible`,
`api_calls_24h`, `budget_reserved_24h`, `ai_calls`다. `/plan`은 `status: PLANNED`,
`enrolled`, `requeued`를 추가한다. `/run`은 `status`, `attempted`, `notice_key`,
`job_id`, `api_calls`, `records`를 추가한다. `attempted`는 0~10의 정수이며
`api_calls`와 `records`는 이번 batch 전체 합계, 공통 집계는 마지막 상태다.
`notice_key`와 `job_id`는 마지막 시도의 값이며 n8n 최종 출력에서는 제거한다.
한 건이라도 실패하면 `FAILED`, 그 외 부분 결과가 있으면 `PARTIAL`, 나머지
처리 완료는 `COMPLETED`다. 시도가 없으면 `IDLE`, `BUSY`,
`DAILY_BUDGET_REACHED`로 구분한다.

기존 응답과의 호환성을 위해 `api_calls_24h`, `budget_reserved_24h` 필드 이름은
유지하지만, 현재 값은 최근 24시간 이동 합계가 아니라 00:00 KST부터의 당일
호출과 미정산 예약이다. W14는 이 당일 값을 기준으로 시작 가능 예산을 판단한다.

W14는 응답의 정확한 필드와 타입을 검증한다. 식별자는 검증 후 제거하며 최종
출력은 숫자 집계와 고정 상태만 남긴다. 알 수 없는 필드, 원문, AI 사용량,
형식 오류는 고정 오류문으로 실패한다. HTTP 오류와 `FAILED` 응답도 n8n 실패로
표시한다. `PARTIAL`, `IDLE`, `BUSY`, `DAILY_BUDGET_REACHED`는 저장된 backend 상태에
따라 다음 예약으로 이어진다. `running > 0`, `eligible == 0`, 예산 부족 시
`/run`을 호출하지 않는다. backend는 두 요청 사이의 경쟁 조건도 다시 검증한다.

두 HTTP node는 요청 본문에도 `contentType: json`, `specifyBody: json`,
`jsonBody`를 사용한다. 2026-09-13 예약 실행 점검에서 raw 요청 모드가 HTTP 200의
응답을 JSON 대신 압축 해제 stream 객체로 반환하여 plan validator에서 멈춘
사실을 확인했다. n8n의 [HTTP Request 구현](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/HttpRequest/V3/HttpRequestV3.node.ts)은
raw 요청 모드에서 `useStream=true`를 강제하므로 `responseFormat: json`만으로는
이를 막지 못한다. 기본 JSON 요청 모드로 전환하여 응답 JSON 처리를 유지한다.
수집 예산·요청 필드·인증·응답 allowlist는 그대로다. stream 객체는 계속 거부하며
내부 buffer를 해석하거나 원문을 출력하지 않는다.

수동 `Run Award Automation Offline Fixture` 경로는 합성 응답만 실행하고
환경변수·인증·네트워크를 사용하지 않는다. n8n 성공/실패/수동 실행 payload
저장은 꺼져 있으며, 상태 확인의 기준은 backend의 영속 집계·audit다.

HTTP node의 기존 이름 `Refresh One Queued Award Notice`는 정확한 이름·타입에
기반한 credential 연결을 보존하기 위해 유지한다. 이 node의 현재 실행 단위는
이름에 남은 One과 달리 최대 10건의 batch이며, sticky note와 요청 본문에
실제 한도를 명시한다.

## 배포·운영 확인

1. backend 변경을 배포하고 `/status` 응답 및 additive 상태 테이블을 확인한다.
2. W14 로컬 계약과 아래 검증을 통과시킨다. manifest에는
   `award-refresh-automation-1.0`, `bounded-batch-zero-ai-v2`를 명시한다.
3. 기존 배포 환경의 인증으로
   `node scripts/deploy-workflows.mjs --only=pai-loop-14-award-history-automation`
   를 실행한다. 스크립트는 정확한 W10 이름·HTTP node 이름·타입으로 기존
   Generic Header credential 참조를 선택한다. W14의 정확한 두 HTTP node에만
   연결하고 원격 재조회로 계약·동일 credential을 확인한 뒤 저장된 정확한
   `versionId`를 게시한다. 이미 활성인 W14를 수정한 경우에도 새 draft를 게시하고
   `activeVersionId == versionId`를 재조회하여 실제 예약 실행 버전을 확인한다.
   이 게시 처리의 변경은 W14에만 적용한다.
4. 별도로 현재 원격 W10의 위 두 award runtime flag만 `false`로 바꾸고
   나머지 runtime, credential, 활성 상태가 보존됐는지 확인한다. 로컬 W10
   전체를 덮어쓰는 배포로 다른 작업의 최신 설정을 되돌리지 않는다.
5. `/plan`을 실행해 `unplanned == 0`과 분류별 합계를 확인한다. `/run` 1회와
   첫 예약 실행을 확인하고 실제 처리 건수·잔여·호출량·AI 0회를 보고한다.
   상태가 `pending`인 대상을 완료했다고 표현하지 않는다.

W14 단독 배포는 정확한 두 award endpoint만 호출하는 검증된 독립 그래프이므로
진행 중인 W13 Claude canary와 무관하게 허용된다. W10~W13 배포·활성화 제한은
기존대로 유지된다. 운영 중 `PAI_LOOP_EMERGENCY_DISABLE=true`면 W14는 보호된
API 호출 전에 끝난다. W14만 멈추려면 원격 W14를 비활성화하고 audit를 보존한다.

```bash
node --check scripts/deploy-workflows.mjs
node --check scripts/bind-claude-gateway-credentials.mjs
node scripts/deploy-workflows.mjs --validate-only
node scripts/bind-claude-gateway-credentials.mjs --self-test
node scripts/test-daily-workflow.mjs
node scripts/test-teams-delivery-workflow.mjs
node scripts/test-claude-gateway-workflow.mjs
node scripts/test-award-automation-workflow.mjs
```

W14 검증은 수동 경로의 네트워크 0회, 10건 batch 응답과 초과 시도 거부,
950회 사용 시 시작 허용/951회 사용 시 중단하는 예산 경계, 응답 원문/식별자 제거,
수정된 endpoint·반복·추가 node 거부를 검사한다. 배포 시험은 모든 `fetch`를
합성 응답으로 대체하여 W14 단독 생성·갱신, 정확한 credential 보존, 잘못된
source/타입/기존 binding 거부 및 W10~W13 무변경을 확인한다. 실제 API 호출이나
원격 쓰기를 하지 않는다. CI에도 이 시험을 포함한다.
