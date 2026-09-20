# 확인된 출력 상한 실패의 단건 재처리

`LONG_OUTPUT_ONCE`는 서버 키 전용 frozen `FAILED_ATTACHMENTS` plan에서 명시적으로
선택하는 고정 예산 정책이다. 실제 운영 호출이나 자동 재처리를 활성화하지 않는다.
일반 분석은 기존 20,000 출력 토큰 / HTTP 180초 / 첨부당 모델 최대 2회다.

엄격히 확인된 20,000 `max_tokens` 실패는 24시간이 지나거나 일반 재시도를 명시해도,
다시 다운로드·파싱한 현재 문서/전체 source/input 지문과 manifest·추출 계약·모델이
같으면 기존 REVIEW를 재사용한다. 모델 호출·새 실패 행·모델 사용량은 추가하지 않는다.
URL만으로 영구 재사용하지 않으므로 같은 URL의 문서 교체나 입력·계약 변경은 정상
분석 경로로 진입한다. 원인 미확인·일시적 실패에는 이 제한을 적용하지 않는다.
명시적인 `LONG_OUTPUT_ONCE` 및 별도 `QUANTITATIVE_PROBE_ONCE` 경로는 유지하며,
이 재사용 규칙 자체는 예산을 늘리거나 추가 호출을 승인하지 않는다.

```json
{
  "queue_name": "BACKFILL",
  "notice_keys": ["PPS-SYN-LONG-OUTPUT"],
  "retry_reviewed": true,
  "review_campaign_key": "SYN-long-output-1",
  "request_token": "SYN-long-output-execution-1",
  "retry_scope": "FAILED_ATTACHMENTS",
  "retry_error_codes": ["HTTP_ERROR"],
  "retry_max_attachments": 1,
  "retry_budget_policy": "LONG_OUTPUT_ONCE",
  "max_total": 1,
  "execution_limit": 1,
  "max_continuations": 1,
  "resume_active": false,
  "dry_run": false
}
```

기존 `POST /api/v1/operations/analysis-backfills/plan` 응답의 고정 정책과 대상 수를
확인한 뒤 같은 operation/segment/chunk로 batch 및 complete한다. 클라이언트는
토큰·timeout·호출 수 숫자를 지정할 수 없다. PIN·부서 cookie·브라우저에 넣은 서버
키는 이 경로의 권한이 아니다. 별도 분석 권한이나 일반 무료 재계산은 바꾸지 않는다.

선택된 현재 manifest의 유일한 실패가 다음 조건을 모두 만족해야 한다.

- 현재 추출 계약의 REVIEW / 저장 HTTP_ERROR 및 내부 고정 HTTP 500 메시지.
- 엄격 검증된 gateway failure의 `NATIVE_STOP_MAX_TOKENS` / `max_tokens`,
  output usage 정확히 20,000. 원시 오류 문장이나 공개 일반 오류 코드만으로 허용하지 않는다.
- 완전한 source/input 처리 기록과 일치하는 저장 문서 지문. 단순 부분 입력은 거절한다.
- 현재 공고가 유효한 OPEN이며 차수·manifest·원래 실패 행이 고정 범위와 일치한다.

실제 호출 직전에도 최신 메타데이터/공고 차수/실패 버전 및 전체 source/input 지문을
재검증한다. 처음 선택한 실패 외에는 다운로드·모델 대상으로 확장하지 않는다.
32,000에서 다시 `max_tokens`가 되어도 이 정책으로 더 늘릴 수 없다.

## 시간·호출·출력 계약

이 정책만 **32,000 토큰 / HTTP 300초 / 모델 최대 1회**다. adaptive thinking 및
medium effort, 모델, source/input caps, 전체 schema/정량/인용 검증은 그대로다.
추론과 최종 JSON은 같은 출력 토큰 예산을 사용한다. 32k가 완전한 결과를 보장하지 않는다.
첫 응답의 schema/quote 검증이 실패하면 REVIEW로 끝나며 교정 모델 호출은 없다.

첨부 시작 예산은 `3×12 + 1×300 + 5 = 341초`로 계산한다. 기존 enrichment 450초와
외부 요청 600초는 유지하며, 파싱 후에도 305초가 남아 있는지 재확인한 뒤 소비한다.
이는 기존 timeout 기반의 진입 예산이며, 네트워크 I/O timeout을 전체 벽시계의 엄격한
강제 종료로 표현하지 않는다. 외부 호출의 취소/완료가 불명확하면 재시도하지 않는다.

## 영속적인 한 번 소비

정책·공고·첨부·manifest·전체 source/input 지문에서 유도한 고유 PK로 기존
ingestion_jobs에 별도의 `LONG_OUTPUT_ONCE` 예약 기록을 삽입하고, 원본 실패 ID를
그 기록에 보존한다. 동일 입력의 새 실패 행/다른 campaign/프로세스도 같은 PK를
사용한다. 32k 결과에는 적용 정책을 기록하여 사용량이 20k로 보고되더라도 다시
대상으로 삼지 않는다. 원래 실패·성공 자료는 수정하지 않는다. 계획/dry-run,
예산 부족, 다운로드/파싱 실패는 모델 권한을 소비하지 않는다.

1. source 확인 후 client 생성 전 커밋: `CONSUMED_NOT_DISPATCHED`. 이 단계에서
   종료됐다면 모델 전송을 시작하지 않은 상태이며, 소비는 취소하지 않는다.
2. 전송 직전 별도 transaction CAS: `DISPATCH_STARTED`. 이후 응답/저장 여부가
   불명확하면 전송·비용을 0으로 추정하지 않는다. CAS 직후 실제 socket 전송 전의
   극히 짧은 중단도 자동으로 구별할 수 없으므로 같은 불명확 범위다.
3. 응답 및 추출 저장·fallback 저장이 모두 실패해도 소비 기록은 별도 커밋으로 남는다.
   동일 manifest/source/input으로 새 실패 행이나 campaign이 생겨도 새 32k 호출은 막는다.

소비 기록은 분석 완료나 사용량 자료가 아니다. notice_keys/window는 비워 두고 일반
API/처리/평가 카운터를 증가시키지 않는다. 실제 사용량은 기존 ANALYSIS child와 첨부
결과에서 확인한다. 예약 상태/고정 경고는 전송 전·전송 결과 불명확 상태를 구분한다.
기존 7일 retention에서 **이 정확한 source만** 제외해 재호출 방지 기록을 보존한다.
다른 운영 로그의 보존 기간은 바꾸지 않는다. 새 DB migration은 없다.

## 검증 및 배포 경계

합성 회귀는 허용/거절 조건, 일반 예산 유지, 341초 admission, 실제 batch 상속,
동일 입력의 다른 실패 버전 동시 소비/CAS, 소비 커밋 응답 유실, 전송 전 중단, 모델 후 저장 실패,
다운로드 중 source 변경, retention 이후 재캠페인 차단을 확인한다.
PostgreSQL 원자성은 기존 disposable CI PostgreSQL에서 검증하며, 미설정 로컬은 skip한다.
실제 공고·모델·운영 계정은 테스트에서 호출하지 않는다.

W13에서는 Validate의 고정 정책 검증과 provider HTTP timeout만 함께 바뀐다. API의
32k 허용과 W13 계약이 모두 배포된 것을 확인한 후 별도의 승인된 단건만 실행한다.
기존 credential, 노드 연결, 실행 원문 저장 금지, retry=false는 보존한다.
