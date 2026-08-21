# PAI_LOOP Teams 실제 전송 운영 가이드 v0.9.0

## 목적과 경계

`PAI_LOOP 12 - Teams Daily Delivery`는 W10 수집·분석 및 W11 continuation과
분리된 전송 전용 워크플로다. 매일 09:00 Asia/Seoul에 첫 시도하고 10:45까지
15분 간격으로 최대 8회 readiness를 확인한다. `READY`인 실행만 저장된 7일
브리핑을 읽고 Teams 채널 메시지를 만든다. Teams 장애로 W12를 다시 실행해도
PPS 수집, 첨부 추출, OpenAI 분석, 평가 snapshot은 다시 실행되지 않는다.

W10의 08:00 시작과 W12 첫 시도 사이 간격은 60분이다. scheduled 분기는 protected
read-only endpoint에서 오늘 LIVE PPS와 DAILY parent를 확인한다. parent가 비terminal,
`remaining>0`, `in_flight>0`, 부분·실패 결과가 있으면 briefing·reservation·Teams를
모두 건너뛴다. terminal parent도 오늘 PPS ingestion job ID와 그 실행이 영속한
`created_notice_keys + updated_notice_keys` scope digest/count가 정확히 일치하고,
해당 key 전부가 parent plan에 포함된 경우에만 READY다. 오늘 수집 공고와 분석키가
모두 0인 경우도 PPS COMPLETED,
created/updated 0, stored queue 0, active DAILY parent 없음이 모두 맞아야
`READY_EMPTY`다. 수동 live test는 이 scheduled 분기와 별개다.

실제 전송은 n8n 기본 `Microsoft Teams` v2 노드의 `channelMessage/create`를
사용한다. 이 노드는 Adaptive Card 첨부를 직접 지원하지 않으므로 실제 채널에는
sanitized HTML 요약을 보내고, 같은 공개 allowlist 데이터로 만든 Adaptive Card는
검증·미리보기 계약으로 함께 유지한다.

## 최초 안전 상태

manifest의 W12 상태는 다음과 같아야 한다.

```json
{
  "publish": true,
  "contractVersion": "teams-delivery-1.3",
  "promotionState": "verified-live-e2e"
}
```

이 상태에서는 이미 live 전송과 영속 dedupe가 검증된 production schedule/sink를
활성 상태로 유지한다. credential ID나 OAuth token은 Git export에 넣지 않는다.
v1.3의 `Run Live Teams Test` 분기는 구조·상수 marker·fail-closed 계약을
로컬에서 검증하며, 다음 승인된 수동 E2E에서 전송 결과를 별도로 확인한다.

## 이름 기반 Data Table 설정

n8n 프로젝트에 이름이 정확히 `pai_loop_teams_delivery_config`인 Data Table을 하나
만든다. table ID나 tenant/project ID는 workflow JSON에 넣지 않는다. 열은 string 타입
`key`, `value` 두 개이며 다음 6행만 허용한다.

| key | 안전 초기값 | 실제 전송 전 값 |
|---|---:|---:|
| `push_enabled` | `false` | `true` |
| `approval_state` | `PENDING` | `APPROVED` |
| `team_id` | `UNSET` | 승인된 Team UUID |
| `channel_id` | `UNSET` | 승인된 Channel ID |
| `live_test_enabled` | `false` | 최초 수동 live 1건 동안만 `true` |
| `emergency_disabled` | `false` | 긴급 중지 시 `true` |

Workflow 12는 위 이름을 literal name locator로 조회한다. key 누락·중복·미등록 key,
`true/false` 이외 boolean, 허용되지 않은 approval 값, 잘못된 target 형식은 모두
`SKIPPED_CONFIG_INVALID`로 종료한다. 오류 원문이나 설정값은 결과에 복사하지 않는다.

backend/API와 공개 웹 origin은 고정된
`https://pai-loop-demo.onrender.com`만 사용한다. enable, approval, 유효한 Team ID,
유효한 Channel ID가 모두 있어야 Teams 노드에 도달하며 emergency disable이 우선한다.
수동 live test에는 `live_test_enabled=true`가 추가로 필요하다. Teams sink는 Data Table을
다시 읽지 않고 앞 단계가 형식 검증한 `runtime.target`만 사용한다.

## Teams 메시지 표시 계약

실제 HTML 메시지와 Adaptive Preview는 공고마다 `1. 공고`, `2. 공고` 형식의 번호와
구분선을 사용한다. 각 섹션은 `공고명`, `발주처`, `마감일`, `추정금액`, `참가자격`,
`리스크`, `추천 부서`를 독립 라벨로 표시하며 HTML 라벨은 굵게 렌더링한다.

부서 정보는 다음 순서와 경계를 지킨다.

1. `top_departments`는 정렬된 입력 순서를 유지해 최대 3개까지 부서명과 사업부 점수를
   `추천 부서`에 표시한다.
2. `department_review_candidates`는 추천 부서와 합치지 않고 `추가 검토`로 표시하며,
   각 항목에도 `추가검토` 상태와 점수를 함께 표시한다.
3. `region_routing`은 사업부 추천으로 승격하지 않고 `지역 라우팅`으로 별도 표시한다.
4. 사업부 추천이 없으면 `추천 부서=기준 충족 없음`을 명시한다. 추가 검토나 지역
   라우팅이 있더라도 이 fallback을 바꾸지 않는다.

메시지 builder는 위 표시 필드만 allowlist로 복사한다. provider 원문, 연락처, 계정,
담당자 개인정보와 임의 중첩 필드는 HTML과 Adaptive Preview에 포함하지 않는다. 모든
HTML 값은 control character 정리와 entity escape를 거치며, 공고는 최대 6건, 추천과
추가 검토는 각각 최대 3건, 지역 라우팅은 최대 2건으로 제한한다. HTML과 Preview 카드의
검증 payload가 24KB를 넘으면 correlation 예약과 Teams 호출 전에 fail-closed한다.

## 영속 전송 예약과 중복 억제

scheduled live 실행은 Teams 호출 전에 첫 번째 표시 공고의 보호된 backend mock endpoint에
`teams-daily:{KST 날짜}:{stable daily key}` correlation을 기록한다. stable daily key는
KST 날짜와 형식 검증된 Team/Channel ID로만 만들므로 재확인 사이 briefing 내용이나
순서가 달라도 같은 대상에는 바뀌지 않고, 승인 대상이 바뀌면 별도 예약이 된다.
reservation card에는 n8n execution ID로 만든 owner token만 포함된다. backend DB의
correlation unique constraint와 멱등 응답 때문에 최초 저장 owner와 현재 owner가 같은
실행만 Teams sink로 진행한다. 따라서 scheduled 전송은 하루 최대 1회다. manual live
test는 별도 generation correlation을 사용한다.

이미 저장된 correlation은 `DUPLICATE_PERSISTENT_SUPPRESSED`로 종료한다. 두 실행이
동시에 최초 insert를 시도해 한쪽이 충돌 또는 오류를 받더라도 그 실행은
`RESERVATION_FAILED_NON_BLOCKING`으로 fail-closed되어 Teams를 호출하지 않는다.
따라서 예약 이후 Teams 전송 전에 프로세스가 중단되면 알림이 유실될 수는 있지만,
같은 correlation의 중복 알림은 보내지 않는 at-most-once 경계를 우선한다. Teams
노드 실패 뒤 15분 schedule이 다시 실행돼도 기존 예약이 두 번째 sink 호출을 막는다.

mock 알림 운영 로그는 기존 7일 retention 정책의 대상이다. correlation에는 KST 날짜가
포함되므로 다음 날 새 브리핑은 새 reservation을 사용한다. 같은 날 readiness 미완료는
bounded schedule로 재확인하지만, reservation 이후 Teams 실패 건은 재전송하지 않는다.

## credential과 대상 연결

1. W12를 `publish=false` 상태로 n8n에 배포한다.
2. `Send Sanitized Teams Briefing` 노드에서 `Microsoft Teams account`
   (`Microsoft Teams OAuth2 API`) credential을 선택한다.
3. credential이 실제로 접근 가능한 Team과 Channel을 UI에서 확인한 뒤 ID를 설정표의
   `team_id`, `channel_id` 값에 저장한다. `push_enabled=false`, approval `PENDING`은
   그대로 둔다.
4. `Run Offline Teams Preview`로 전체 수동 실행하여 `PREVIEW_READY`와
   `sourceCalls.configTable/backend/teams=0/0/0`을 확인한다.
5. `Run Live Teams Test`를 실행하여 설정표 조회 1회 뒤 `DELIVERY_SKIPPED`로 닫히는지
   확인한다. 이 분기는 `manual-live-test` 상수를 사용하며 n8n 실행 엔진 mode를 판별하지
   않는다. `Every Day 09:00 KST` Schedule Trigger는 수동 live test에 사용하지 않는다.
6. credential과 대상을 검토한 운영자가 설정표를 `push_enabled=true`,
   `approval_state=APPROVED`, `live_test_enabled=true`로 바꾸고 `Run Live Teams Test`를
   시작점으로 수동 live test를 정확히 1회 실행한다.
7. 결과를 확인한 즉시 `live_test_enabled=false`로 복원한다. publish 전까지는
   `push_enabled=false`로 다시 닫는다.

배포 스크립트는 이후 동일한 노드 이름과 타입이 유지되는 경우 원격 credential
binding을 보존한다. 다른 타입 또는 다른 이름의 노드로 credential을 복사하지 않는다.

## 1건 live 검증

운영 승인 후 아래 항목을 한 번에 확인한다.

- scheduled: 설정 Data Table 1회, readiness 1회, backend briefing 1회,
  영속 correlation 예약 1회, Teams 호출 1회
- manual live test: 설정 Data Table 1회, backend briefing 1회, 영속 correlation
  예약 1회, Teams 호출 1회(ready endpoint 0회)
- 최종 `status=DELIVERY_SENT`
- `delivery.status=SENT`
- Teams가 반환한 `messageId` 존재
- `actualTeamsRequestAttempted=true`
- `actualTeamsRequestSent=true`
- 카드/HTML에 원본 첨부, API 키, OAuth token, 회사 비공개 증빙행이 없음
- 같은 날 payload가 바뀐 뒤 재실행해도
  `DUPLICATE_PERSISTENT_SUPPRESSED`이고 Teams 호출 0회
- 테스트 직후 `live_test_enabled=false`와 publish 전 `push_enabled=false` 복원

Teams가 실패하면 최종 상태는 `DELIVERY_FAILED_NON_BLOCKING`이어야 한다. raw 오류
본문은 남기지 않고 `TEAMS_NODE_ERROR`만 남긴다. Teams 노드는 자체 retry가 없고,
이미 확보한 reservation도 유지하므로 같은 날 같은 payload를 자동 재전송하지 않는다.
reservation 자체가 실패하면 `DELIVERY_RESERVATION_FAILED_NON_BLOCKING`이어야 하며
Teams 호출은 0회다.

## promotion

credential 연결, 대상 확인, live 1건 성공, 중복 억제 확인이 모두 끝난 뒤에만 다음
두 값을 동시에 바꾼다.

```json
{
  "publish": true,
  "promotionState": "verified-live-e2e"
}
```

`publish=true/awaiting-live-e2e` 또는 `publish=false/verified-live-e2e` 조합은 배포
validator가 거부한다.

## 긴급 중지와 복구

긴급 중지는 설정표의 `emergency_disabled=true`로 바꾸거나 W12를 비활성화한다.
W10과 W11은 별도 워크플로이므로 Teams 중지만으로 수집·분석을 중단할 필요는 없다.
복구할 때는 대상과 credential을 다시 확인하고 offline preview, `Run Live Teams Test`
기반 live 1건, duplicate 억제 순서로 재검증한다.
