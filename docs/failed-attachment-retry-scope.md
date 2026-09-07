# 실패 첨부만 재처리하는 서버 작업

기존 서버 키 전용 `POST /api/v1/operations/analysis-backfills/plan`의 review campaign에
`FAILED_ATTACHMENTS` 범위를 추가한다. 화면·수동 분석 권한·일반 재시도 정책은
변경하지 않는다. 이 변경은 운영 작업을 생성하거나 실행하지 않는다.

```json
{
  "queue_name": "BACKFILL",
  "notice_keys": ["PPS-SYN-FAILED-ATTACHMENTS"],
  "retry_reviewed": true,
  "review_campaign_key": "SYN-failed-attachment-recovery-1",
  "request_token": "SYN-execution-1",
  "retry_scope": "FAILED_ATTACHMENTS",
  "retry_error_codes": ["XLS_PARSE_FAILED"],
  "retry_max_attachments": 1,
  "max_total": 1,
  "execution_limit": 1,
  "dry_run": false
}
```

오류코드는 저장된 진단에서 확인한 값으로 선택한다. 위 코드는 합성 예시이며,
파일 확장자나 화면의 일반 오류 문구로 실제 원인을 추정하지 않는다. 서버의
`FAILED_ATTACHMENT_RETRY_CODES`에 명시된 유한한 목록만 허용한다. 임의 접두사·
정규식·공급자 오류 문장을 받지 않는다. `retry_max_attachments`는 기본 3, 최소 1,
최대 3이다. 정확히 한 공고만 허용하고, 일치 대상이 상한보다 많으면 잘라 실행하지
않고 409로 거절한다. 대상이 비어도 409이며 전체 첨부 처리로 전환하지 않는다.

plan 응답의 `retry_scope: FAILED_ATTACHMENTS`와 `retry_target_count`를 확인한다.
기존 review policy/campaign key 확인도 유지한다. plan 자체는 공급자를 호출하지
않는다. 반환된 기존 operation/segment/chunk/정확한 공고키로 배치를 실행하고
complete한다. W11의 순수 resume는 저장한 범위를 그대로 상속한다. dry-run은
별도 campaign identity로 계획하고 기존 dry-run lease/complete 규칙을 따른다.

## 고정하는 범위

- 현재 manifest의 최신 `REVIEW` 중 허용 오류코드와 일치한 첨부만 선택한다.
  `ACCEPTED`는 정량 검토 후보가 남아 있어도 선택하지 않는다.
- 공고키·차수·첨부별/전체 manifest digest·최신 실패 ID와 버전 번호·실패 내용
  digest를 저장한다. 선택된 binding의 이전 transient REVIEW ID도 함께 고정한다.
  내부 ID·digest를 사용자가 입력하거나 공개 응답에 노출하지 않는다.
- 범위와 오류코드·상한은 campaign request identity에 포함한다. 같은 campaign key로
  바꾸면 409다. 기존 기본 범위는 신규 필드를 identity에서 제외하여 이미 저장된
  V1 campaign의 재전송과 재개를 유지한다.
- 선택되지 않은 첨부는 미처리이거나 24시간이 지난 실패라도 다운로드·모델 호출을
  하지 않는다. 기존 감사와 결과를 재사용하고 부족한 근거는 계속 부족하게 표시한다.
- 선택된 실패 이후의 새 결과가 있으면, 그것도 실패이고 며칠이 지났더라도 같은
  snapshot으로 다시 호출하지 않는다. 이전 ACCEPTED 이력이 새 실패보다 앞서 있어도
  새 세대의 처리 기록을 남겨 다음 continuation의 중복 다운로드를 막는다. 동일한
  기존 내용의 검증된 추출은 기존 안전한 복제 경로로 재사용할 수 있다.

## 기존 보호 정책

선택된 기존 실패만 해당 campaign에서 24시간 재사용을 한 번 우회한다. 전체
cooldown, public manual의 최근 요청/시간당 한도, 서버 인증, 공고 실행 잠금,
claim generation, lease, cached HTTP replay, 공고별 최대 10 execution 제한은 유지한다.
각 선택 첨부의 기존 모델 호출 상한은 2회이므로 최대 3첨부의 상한은 6회다.
이미 저장된 성공/실패와 HTTP 응답은 재전송으로 새 provider 호출을 만들지 않는다.
유료 호출 뒤 정상 결과 저장에 실패해도 오류 기록을 저장할 수 있으면, 선택된 실패
버전 이후의 새 `INTERNAL_ENRICHMENT_ERROR` 기록을 남긴다. 이 경로에서도 과거
`ACCEPTED`를 대신 반환하지 않으며, 같은 snapshot의 재개는 새 오류 기록을 재사용한다.

공고 차수/manifest/기존 실패 행이 달라지거나 공고가 취소·종료되면 중단한다.
실행 중 다음 첨부를 시작하기 전에도 현재 상태와 manifest를 확인한다. 이미 실행된
첨부의 비용·다운로드·감사는 다음 첨부에서 중단되어도 유지한다. 이미 진행 중인
외부 호출은 이후의 공고 변경으로 취소할 수 없으며, 외부 호출 완료와 DB 저장 사이
장애까지 exactly-once로 보장하지 않는다. 그런 불명확한 작업을 새 campaign으로
자동 반복하지 않는다.

검증은 합성 DB·다운로드와 모델 mock을 사용한다. 운영 원본·실제 API·배포는 별도의
승인된 운영 절차에서 확인한다.
