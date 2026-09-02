# 비공개 정량 증빙 운영 런북 — 2026-09-02

## 목적과 적용 경계

이 문서는 비공개 수행실적 워크북과 신용평가 PDF를 원문 업로드 없이 등록하고,
검증된 메타데이터만 사용해 부산 대상 공고 한 건을 다시 계산하는 일반 절차다.
특정 회사, 공고, 파일 또는 운영 장애의 실제 값은 이 문서에 기록하지 않는다.

- 원본 XLSX/PDF는 승인된 운영자 단말에만 둔다. 저장소, 작업 브랜치, 이슈,
  채팅, CI 산출물, 스크린샷 또는 애플리케이션 로그에 복사하지 않는다.
- 원본 파일명과 로컬 경로, 행 내용, 주소·담당자·전화번호, 계약 식별자,
  문서 digest, 운영 PIN/API 키를 출력하거나 문서화하지 않는다.
- "메타데이터 전송"은 원본 바이너리와 문서 본문을 보내지 않는다는 뜻이다.
  워크북에서는 허용된 정규화 필드와 불투명 행 참조를, 신용평가 PDF에서는
  등급·날짜·digest·불투명 참조·검토 확인만 인증된 비공개 API로 전송한다.
- LLM은 이 경로에 관여하지 않는다. 정량 점수는 등록된 사실과 현재 공고의
  검증된 산식을 결정론적으로 결합하며, 최종 입찰 판단은 사람의 책임이다.

## 입력 의미와 사전 검토

워크북은 `프로젝트DB` 시트와 현재 importer가 요구하는 열 구조를 사용한다.
특히 다음 세 열의 의미를 바꾸거나 서로 대체하면 안 된다.

| 원본 열 | 저장 의미 | 운영 규칙 |
|---|---|---|
| H | `contract_amount`, `gross_contract_amount_krw` | 지분 차감 전 계약 총액이다. 화면 호환용 금액도 같은 총액을 유지한다. |
| I | `share_pct` | 회사의 공동수급 이행 지분율이다. 금액 자체가 아니며 H나 J에 다시 합산하지 않는다. |
| J | `recognized_performance_amount_krw` | 실적증명서가 인정한 VAT 포함 금액이며, 이미 회사 지분이 반영된 금액이다. `recognized_amount_is_net_of_share=true`로 저장한다. |

따라서 부산형 금액 기준이 `APPLY_SHARE`여도 J가 있으면 **J × I를 계산하지
않고 J를 그대로 사용**한다. 다시 지분을 곱하면 공동수급 지분을 두 번 차감하는
오류다. H는 원계약 총액을 보존하는 감사·fallback 필드일 뿐, 검증된 J를 덮어쓰지
않는다.

I의 정규화 규칙은 다음과 같다.

- `%`가 없는 `0 < 값 <= 1`은 비율로 보고 100을 곱한다. 따라서 숫자 `1`은
  100%이고, 1%를 뜻하려면 원본에 명시적인 `%` 표현이 필요하다.
- 1보다 크고 100 이하인 값과 명시적인 퍼센트 값은 백분율로 취급한다.
- 0%는 저장할 수 있지만 해당 회사에 귀속되는 금액·건수가 없으므로 산정에서
  기여하지 않는다.
- 100% 미만은 공동수급 행으로 집계한다. 금액은 위 J 규칙을 따르지만, 계약
  **건수**에 지분을 어떻게 적용할지가 공고 원문에서 확정되지 않으면 해당 건수는
  임의로 1건이나 소수 건으로 만들지 않고 `REVIEW`/미산정으로 닫는다.
- I가 비었거나 음수·100 초과 등으로 해석 불가능하면 importer는 해당 행을
  `DRAFT`, `share_pct=0`으로 닫는다. 이 0은 실제 지분이라는 뜻이 아니라 점수
  기여를 막는 안전값이다. 이를 100%로 보정하지 말고 로컬 원본에서 의미를 확인한
  수정본을 다시 적재한다.

이 importer는 허용된 워크북 계약을 전제로 `vat_basis=INCLUDED`,
`completed=true`, `certificate_status=ISSUED`를 기록한다. 그러므로 각 행에서
VAT 포함, 완료, 증명서 발급, H/J/I 의미를 사람이 확인하지 못했다면 실행하지
않는다. H 또는 J가 없거나 의미가 다른 행도 먼저 보완한다.

주소·개인·연락처 열은 요청 모델에 없으며 서버로 보내지 않는다. 원본 내부
식별자는 평문으로 전송하지 않고 로컬에서 불투명 행 키를 만드는 데만 사용한다.
서버에는 원본 경로 대신 `private-evidence://` 형식의 digest 기반 워크북/행 참조가
저장된다.

## 날짜 처리의 알려진 한계

importer가 계약일 또는 계약기간의 시작·종료일을 안전하게 해석하지 못하거나
종료일이 시작일보다 빠르면 그 행은 `DRAFT`로 남는다. 비어 있거나 모호한 날짜를
현재 날짜, 공고 날짜 또는 PDF의 다른 날짜로 추정하지 않는다. `DRAFT` 행은
감사 목적으로 적재될 수 있지만 정량 재계산은 `VALIDATED` 행만 읽으므로 점수에
포함되지 않는다.

날짜를 보완할 때는 운영자가 원문 전체를 다시 확인하고 로컬 워크북 수정본에
명시적인 달력 날짜를 입력한 뒤 전체를 다시 정규화해 교체 적재한다. 원본 SHA에
결합된 서버 행을 직접 수정하거나 PDF 본문을 서버에 올리거나 OCR 추정값으로
`VALIDATED` 승격하지 않는다.

## 로컬 워크북 dry-run과 업로드

### 환경변수 준비

실제 값은 승인된 비밀 저장소 또는 현재 프로세스 환경에만 설정한다. 아래는
값이 아니라 사용할 변수 이름과 형식이다.

| 환경변수 | 값 형식 |
|---|---|
| `PRIVATE_PERFORMANCE_WORKBOOK_PATH` | `<LOCAL_ABSOLUTE_XLSX_PATH>` |
| `PAI_LOOP_BASE_URL` | `https://<APPROVED_HOST>` |
| `PAI_LOOP_PRIVATE_EVIDENCE_TOKEN` | `<HIGH_ENTROPY_PRIVATE_EVIDENCE_SECRET>` |
| `PAI_LOOP_API_KEY` | `<SERVER_TO_SERVER_SECRET_FOR_INTERNAL_RESULT_READ>` |
| `PAI_LOOP_OPERATOR_PIN` | `<SAME_ORIGIN_RECOMPUTE_PIN>` |

비공개 워크북과 신용평가 메타데이터 등록에는 32자 이상의 무작위
`PAI_LOOP_PRIVATE_EVIDENCE_TOKEN`만 사용한다. `PAI_LOOP_API_KEY`는 이후 내부 정량
결과 조회에, 4자리 PIN은 동일 출처 재계산 요청에만 사용한다. 어느 비밀도 브라우저
코드, 명령행 인수 또는 Markdown에 넣지 않는다. 기본 URL은 자격증명이 포함되지
않은 HTTPS URL이어야 한다.

### 1. 무기록 검증

저장소 루트에서 다음 명령을 실행한다. 경로 값은 환경변수로만 전달하며 명령이
원본 경로나 내용을 출력하지 않게 한다.

```powershell
python .\tools\import_private_performance_records.py --workbook "$env:PRIVATE_PERFORMANCE_WORKBOOK_PATH" --dry-run
```

dry-run은 API 호출과 DB 쓰기를 하지 않고 집계만 출력한다. 다음을 운영자가 가진
별도 승인 집계와 대조한다.

- `source_data_rows`, `placeholder_rows`, `normalized_rows`
- `validated_rows`, `draft_rows`, `joint_rows`
- `status=validated`

행 내용이나 원본 digest를 출력해 대조하지 않는다. 예상하지 못한 `DRAFT` 또는
공동수급 집계가 있으면 업로드를 중단하고 로컬 원본을 검토한다.

### 2. 인증된 업로드

dry-run 집계와 H/J/I 및 날짜 검토가 승인된 뒤에만 실행한다. importer에는 비밀값
자체가 아니라 비밀을 읽을 환경변수 이름을 준다.

```powershell
python .\tools\import_private_performance_records.py `
  --workbook "$env:PRIVATE_PERFORMANCE_WORKBOOK_PATH" `
  --base-url "$env:PAI_LOOP_BASE_URL" `
  --private-token-env PAI_LOOP_PRIVATE_EVIDENCE_TOKEN `
  --batch-size 100 `
  --attest-source-semantics
```

`--attest-source-semantics`는 운영자가 원본 전체에서 증명서 기반·완료·VAT 포함 및
J열의 지분 반영 인정금액 의미를 실제로 확인한 뒤에만 지정한다. 플래그가 없으면
업로드는 쓰기 전에 중단된다.

클라이언트는 최대 100행씩 `POST /api/v1/performance-records/private-import`로
보낸다. 원본 XLSX, 로컬 파일명/경로, 연락처 열은 요청에 포함되지 않는다. 성공
출력의 `created`, `updated`, `unchanged` 합계만 검증하고 전체 요청/응답 본문을
로그나 티켓에 붙이지 않는다.

새 원본의 첫 배치가 커밋되면 이전 비공개 원본은 `SUPERSEDED`되고 이전 활성 행은
`ARCHIVED`된다. 새 행은 선언된 모든 배치와 고유 행 수가 확인될 때까지 서버에서
`DRAFT` 상태로 대기한다. 이 대기 구간에는 비공개 원본이 권위 있는 레지스터로
남으므로 수동 행으로 점수를 대신 계산하지 않는다. 마지막 배치에서만 원래
`VALIDATED`로 정규화된 행이 한 번에 활성화되며, 입력 문제 행은 계속 `DRAFT`다.
중간에 실패하면 재계산하지 말고 같은 로컬 원본으로 명령을 그대로 재실행한다.

## A0 신용평가 PDF 메타데이터 등록 계약

### 사전 조건

1. 운영자가 로컬 PDF 전체를 직접 검토하고 등급이 A0이며 문서의 발급일,
   유효 시작일, 만료일을 확인한다.
2. 대상 부산 공고는 저장된 단일 `OPEN` PPS 공고이며, 현재 첨부 manifest에
   기계 검증된 정량표와 하나의 `company.credit_rating` binding이 있어야 한다.
3. 발급일은 공고 마감일 이후일 수 없고, 유효 시작일과 만료일은 한국 시간 기준
   공고 마감일을 포함해야 한다.
4. `HUMAN_REVIEWED_COMPLETE_DOCUMENT` 확인은 실제로 전체 문서를 검토한 경우에만
   사용한다. 이는 발급기관 온라인 진위확인을 했다는 주장이 아니다.

이 단계에서 사용하는 추가 환경변수는 다음과 같다.

| 환경변수 | 값 형식 |
|---|---|
| `PRIVATE_CREDIT_PDF_PATH` | `<LOCAL_ABSOLUTE_PDF_PATH>` |
| `PRIVATE_CREDIT_DOCUMENT_SHA256` | `<LOWERCASE_SHA256_OF_LOCALLY_REVIEWED_PDF>` |
| `PRIVATE_CREDIT_EVIDENCE_REFERENCE` | `private-evidence://credit-rating/<OPAQUE_REFERENCE>` |
| `PRIVATE_CREDIT_ISSUED_ON` | `<ISSUED_ON_YYYY-MM-DD>` |
| `PRIVATE_CREDIT_EFFECTIVE_ON` | `<EFFECTIVE_ON_YYYY-MM-DD>` |
| `PRIVATE_CREDIT_VALID_UNTIL` | `<VALID_UNTIL_YYYY-MM-DD>` |
| `BUSAN_NOTICE_KEY` | `<ONE_OPEN_PPS_NOTICE_KEY>` |

PDF digest는 로컬에서 계산해 환경변수에만 둔다. 다음 명령은 digest를 화면에
출력하지 않는다.

```powershell
$env:PRIVATE_CREDIT_DOCUMENT_SHA256 = (Get-FileHash -LiteralPath $env:PRIVATE_CREDIT_PDF_PATH -Algorithm SHA256).Hash.ToLowerInvariant()
```

요청 계약은 다음과 같다. 아래 꺾쇠 값은 형식 설명용 placeholder이며 실제 값을
이 문서에 대입하지 않는다.

```json
{
  "rating": "A0",
  "document_sha256": "<LOWERCASE_SHA256_OF_LOCALLY_REVIEWED_PDF>",
  "evidence_reference": "private-evidence://credit-rating/<OPAQUE_REFERENCE>",
  "issued_on": "<ISSUED_ON_YYYY-MM-DD>",
  "effective_on": "<EFFECTIVE_ON_YYYY-MM-DD>",
  "valid_until": "<VALID_UNTIL_YYYY-MM-DD>",
  "verification_attestation": "HUMAN_REVIEWED_COMPLETE_DOCUMENT"
}
```

`evidence_reference`에는 회사명, 사람 이름, 등록번호, 파일명, 경로 또는 문서번호를
넣지 않는다. `private-evidence` scheme, 식별정보 없는 host/path만 허용되며
자격증명, query, fragment는 금지된다.

인증된 서버 간 실행 예시는 다음과 같다. 모든 가변 값은 환경변수에서 읽는다.

```powershell
$privateEvidenceHeaders = @{ "X-PAI-Private-Evidence-Token" = $env:PAI_LOOP_PRIVATE_EVIDENCE_TOKEN }
$noticeKeyEncoded = [uri]::EscapeDataString($env:BUSAN_NOTICE_KEY)
$creditBody = @{
  rating = "A0"
  document_sha256 = $env:PRIVATE_CREDIT_DOCUMENT_SHA256
  evidence_reference = $env:PRIVATE_CREDIT_EVIDENCE_REFERENCE
  issued_on = $env:PRIVATE_CREDIT_ISSUED_ON
  effective_on = $env:PRIVATE_CREDIT_EFFECTIVE_ON
  valid_until = $env:PRIVATE_CREDIT_VALID_UNTIL
  verification_attestation = "HUMAN_REVIEWED_COMPLETE_DOCUMENT"
} | ConvertTo-Json -Compress
$creditResult = Invoke-RestMethod -Method Post -Uri "$($env:PAI_LOOP_BASE_URL.TrimEnd('/'))/api/v1/operator-evidence/notices/$noticeKeyEncoded/credit-rating" -Headers $privateEvidenceHeaders -ContentType "application/json" -Body $creditBody
```

성공 응답에는 `notice_key`, `fact_key=company.credit_rating`, `rating=A0`,
`binding_status=CREATED|UNCHANGED`, `valid_at_deadline=true`만 있으며 digest와
비공개 참조는 되돌려 주지 않는다. 응답에는 `Cache-Control: no-store`가 적용된다.
공고 없음은 404, 날짜/정량 binding 불충족은 422, 같은 문서·binding에 충돌하는
메타데이터가 있으면 409로 닫힌다.

## 부산 공고 한 건 재계산

이 절차는 회사 사실 등록 후 저장된 공개 첨부 근거만 다시 평가한다. 대상 키는
`BUSAN_NOTICE_KEY=<ONE_OPEN_PPS_NOTICE_KEY>` 형식의 환경변수로만 관리한다.

1. 운영 화면에서 대상이 현재 `OPEN` PPS 공고인지, 최신 attachment manifest의
   감사가 완료됐는지, 현재 평가가 존재하는지 확인한다. 조건이 달라졌으면
   `recompute_current`를 강행하지 말고 필요한 공개 첨부 분석 절차로 되돌아간다.
2. 워크북 업로드 집계와 A0 등록 결과가 승인된 상태인지 확인한다. `DRAFT` 실적은
   이번 계산에 들어가지 않는다는 점을 기록한다.
3. 다음과 같이 한 공고만, 추출 없이 재계산한다. `recompute_current=true`는
   `run_extraction=true` 또는 `retry_reviewed=true`와 함께 보낼 수 없다.

수동 재계산 endpoint는 서버 API 키 경로가 아니라 동일 출처 운영 PIN 경로를
요구한다. PIN 값은 환경변수에서만 읽고 다음 제한 헤더를 별도로 만든다.

```powershell
$manualHeaders = @{
  Origin = $env:PAI_LOOP_BASE_URL.TrimEnd('/')
  "Sec-Fetch-Site" = "same-origin"
  "X-PAI-Manual-Token" = $env:PAI_LOOP_OPERATOR_PIN
}
$recomputeBody = @{ run_extraction = $false; recompute_current = $true } | ConvertTo-Json -Compress
$recomputeResult = Invoke-RestMethod -Method Post -Uri "$($env:PAI_LOOP_BASE_URL.TrimEnd('/'))/api/v1/notices/$noticeKeyEncoded/analysis/request" -Headers $manualHeaders -ContentType "application/json" -Body $recomputeBody
if ($recomputeResult.outcome -eq "QUEUED") { $env:PAI_LOOP_RECOMPUTE_REQUEST_ID = [string]$recomputeResult.request_id }
```

4. `QUEUED`이면 같은 요청 ID를 조회한다. 병렬로 새 요청을 반복하지 않는다.

```powershell
$requestIdEncoded = [uri]::EscapeDataString($env:PAI_LOOP_RECOMPUTE_REQUEST_ID)
$recomputeStatus = Invoke-RestMethod -Method Get -Uri "$($env:PAI_LOOP_BASE_URL.TrimEnd('/'))/api/v1/notices/$noticeKeyEncoded/analysis/requests/$requestIdEncoded" -Headers $manualHeaders
$recomputeStatus | Select-Object outcome, analysis_state, analysis_reason_code, openai_calls
```

`outcome`이 `QUEUED`가 아니게 될 때까지 간격을 두고 조회한다. 정상 재계산은
저장 근거만 사용하므로 `openai_calls=0`이어야 한다. `COMPLETED`가 아니거나
`REVIEW`이면 원문/근거 보완 사유를 해결하기 전 점수를 확정하지 않는다.
조회는 승인된 운영시간 안에서 유한 횟수로 제한한다. 제한 안에 끝나지 않으면
새 요청을 만들지 말고 작업 상태와 서버 상태를 확인하도록 운영 담당자에게
에스컬레이션한다. 최초 POST 응답을 잃은 경우에도 즉시 병렬 재시도하지 않는다.
동일 공고 재요청이 `COOLDOWN`을 반환하고 요청 ID가 없다면 최근 요청이 재사용된
것이므로 운영 화면에서 현재 공고/작업 상태를 새로고침해 확인한다.

5. 정량 결과는 인증된 API에서 가져오되 안전한 집계 필드만 화면에 투영한다.

```powershell
$serverHeaders = @{ "X-PAI-LOOP-API-KEY" = $env:PAI_LOOP_API_KEY }
$estimate = Invoke-RestMethod -Method Get -Uri "$($env:PAI_LOOP_BASE_URL.TrimEnd('/'))/api/v1/notices/$noticeKeyEncoded/quantitative-estimate" -Headers $serverHeaders
$estimate | Select-Object overall_status, total_max_points, confirmed_points, estimated_points, lower_points, upper_points, evidence_coverage_pct
$estimate.criteria | Select-Object category, max_points, estimated_points, lower_points, upper_points, status
```

전체 객체에는 비공개 evidence binding이 있을 수 있으므로 직렬화하거나 공유하지
않는다. 신용등급 항목과 실적 금액/건수 항목이 현재 공고 산식에 맞게 갱신됐는지,
확정되지 않은 항목은 `REVIEW`/`UNSCORABLE` 또는 범위로 남는지 확인한다. 공개
화면/API에는 문서 digest, 불투명 참조, private fact binding이 노출되지 않아야 한다.

## 멱등성, 실패 처리와 롤백

### 워크북

- 행 키는 로컬에서 만든 불투명 정체성으로 유지된다. 같은 워크북/같은 행을
  정확히 재실행하면 새 레코드를 만들지 않고 `unchanged`가 증가한다.
- 검증 완료된 같은 원본 SHA에 다른 행이나 값을 보내면 409로 거부한다. 서버의
  `PRIVATE_IMPORT` 행은 직접 수정하거나 재승격하지 않는다.
- 값 교정·행 제외가 필요하면 로컬 워크북 수정본 전체를 다시 dry-run하고 새 원본
  SHA로 교체 적재한다. 정체성 입력이 같은 행은 기존 레코드의 revision으로 이어지고,
  정체성이 달라진 행은 새 키가 된다. 어느 경우든 새 원본의 첫 배치가 이전 원본
  전체를 보관 처리하고, 마지막 배치 검증 전에는 새 행을 활성화하지 않는다.
- 네트워크 응답이 불확실할 때는 payload를 바꾸지 말고 같은 원본으로 재실행한다.
- 각 최대 100행 API 요청은 한 DB transaction으로 커밋된다. 그러나 전체
  워크북이 여러 배치이면 앞 배치가 성공한 뒤 뒤 배치가 실패할 수 있으므로 전체
  업로드는 단일 transaction이 아니다. 부분 성공 행은 `DRAFT`이며 수동 행으로
  대체 채점되지 않는다.
- 부분 실패는 새 임의 키나 수정된 복사본으로 우회하지 않는다. 같은 원본과 같은
  명령을 재실행하면 이미 받은 배치는 `unchanged`로 수렴하고 남은 배치 뒤에만
  활성화된다. 다른 수정본을 시작하면 진행 중 원본도 `SUPERSEDED`된다.
- importer에는 전역 undo와 개별 비공개 행 PATCH가 없다. 교정은 승인된 로컬
  수정본의 교체 적재로 수행한다. 정확한 이전 상태 복원이 필요한 경우에는 사전
  승인된 백업과 데이터 소유자의 별도 transaction 절차를 사용한다.

### 신용평가와 재계산

- 신용평가 등록 키는 문서 digest와 현재 공고의 fact binding으로 결정된다.
  완전히 같은 요청의 재실행은 `UNCHANGED`; 같은 키에 다른 메타데이터는 409이며
  기존 값을 덮어쓰지 않는다.
- 응답을 받지 못했다면 같은 digest/reference/date payload로 먼저 재시도한다.
  충돌을 피하려고 새 digest나 새 참조를 만들지 않는다.
- 잘못된 신용평가 사실을 발견하면 추가 재계산을 중단한다. 지원되는 삭제 API가
  없으므로 데이터 소유자가 감사 이력을 보존하는 승인된 정정/폐기 transaction을
  수행한 뒤 올바른 사실로 다시 계산한다.
- 재계산은 새 평가 이력을 추가한다. 과거 평가를 삭제해 되돌리는 대신 입력을
  정정하고 같은 한 공고를 다시 계산해 새로운 현재 평가를 만든다.

## 종료 검증 체크리스트

- [ ] 원본 XLSX/PDF가 저장소, 서버 파일시스템, 로그 및 산출물에 복사되지 않았다.
- [ ] dry-run 집계가 승인된 로컬 집계와 일치하며 예상 밖 `DRAFT`/공동수급 행이 없다.
- [ ] 모든 업로드 배치의 `created + updated + unchanged`가 전송 행 수와 일치한다.
- [ ] 같은 워크북을 다시 실행했을 때 기존 행은 `unchanged`이며 중복 생성되지 않는다.
- [ ] H는 계약 총액, J는 VAT 포함·지분 반영 완료 인정액, I는 지분율로 검토됐다.
- [ ] 날짜가 없거나 모호한 행은 `DRAFT`이며 정량 산정에 포함되지 않았다.
- [ ] A0 등록 결과가 `CREATED` 또는 `UNCHANGED`이고 `valid_at_deadline=true`다.
- [ ] 부산 대상은 한 공고뿐이며 재계산 요청의 `openai_calls`가 0이다.
- [ ] 최종 정량 상태와 항목별 범위를 확인했고 미확정 값을 확정점수로 표시하지 않았다.
- [ ] 공개 응답·화면·공유 자료에 private digest/reference/binding 또는 원문 값이 없다.
- [ ] 실제 비밀값, 파일명, 행 내용, 사람/연락처 정보 또는 운영 사건 정보를 이
  런북과 작업 인계에 남기지 않았다.
