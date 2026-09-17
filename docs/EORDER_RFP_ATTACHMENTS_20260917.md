# 전자주문 제안요청서 첨부 연결 (2026-09-17)

`docs/QUANTITATIVE_SOURCE_AVAILABILITY_20260917.md` 6.1 의 구현 기록이다. 그 문서가
측정으로 확정한 것 — 배점표는 공고문이 아니라 제안요청서에 있고, 그 문서는 공고 첨부
슬롯으로 오지 않는다 — 을 전제로 통로를 붙였다.

구현 중에 그 문서의 전제 두 개가 최신이 아님을 확인했다. 3장에 적는다.

## 1. 오퍼레이션 실측 스펙

`getBidPblancListInfoEorderAtchFileInfo` 를 직접 호출해 확정했다(조달청 오픈API,
LLM 비용 0).

| 항목 | 값 |
|---|---|
| 경로 | `ad/BidPublicInfoService/getBidPblancListInfoEorderAtchFileInfo` |
| 필수 파라미터 | `inqryDiv=1` + `inqryBgnDt`/`inqryEndDt` |
| 구간 상한 | 1개월 (초과 시 "입력범위값 초과") |
| 단건 조회 | 불가 (`bidNtceNo` 단건은 resultCode 08) |
| 응답 필드 | `bidNtceNo, bidNtceOrd, atchSno, eorderDocDivNm, eorderAtchFileNm, eorderAtchFileUrl` |

### 1.1 첨부 URL 이 기존 허용목록과 겹치지 않는다

조사 문서 5.2 는 경로가 다르다는 것까지 적었는데, **쿼리 키도 전부 다르다.**

```
공고 첨부   /pn/pnp/pnpe/UntyAtchFile/downloadFile.do
            ?bidPbancNo=…&bidPbancOrd=…&fileSeq=…&fileType=…&prcmBsneSeCd=…

제안요청서  /pn/pnp/pnpe/UntyAtchFile/downloadRfpFile.do
            ?rfpNo=R26DH01234567&rfpOrd=000&rfpUntyAtchFileNo=2
```

겹치는 키가 하나도 없으므로 두 경로의 허용 키를 한 집합으로 합치면 안 된다. 합치면
한쪽의 필수 키가 다른 쪽에서 선택 키가 되어 가드가 느슨해진다. 경로별로 허용 키와
필수 키를 따로 고정했다(`_G2B_ATTACHMENT_PATH_RULES`).

## 2. 변경

### 2.1 수집 (`integrations/pps.py`)

- `iter_notices` 의 날짜 구간 페이징을 `iter_raw_rows` 로 분리했다. 동작은 같고,
  `iter_notices` 가 그 결과를 `normalise_notice` 로 감싼다.
- `iter_eorder_attachments` 를 추가했다. 구간 상한 30일이 강제되고, 응답은 위 여섯
  필드로 줄여서 내보낸다. 제공자가 나중에 담당자 이름 같은 필드를 덧붙여도 저장
  경계까지 따라오지 않는다.

### 2.2 조인 (`api.py`)

- 공고 수집이 끝난 뒤 **같은 구간을 한 번 더** 훑어 `(bidNtceNo, bidNtceOrd)` 로
  조인한다. 키워드마다 반복하지 않는 이유는 이 오퍼레이션이 공고명 필터를 받지 않기
  때문이다 — 키워드가 다섯 개여도 추가 호출은 구간당 한 벌이다.
- 전자주문 조회 실패는 **공고 수집의 실패가 아니다.** `PpsApiError` 를 삼키고 공고
  첨부만으로 진행하되, 응답 warning 에 조인하지 못했음을 남긴다.
- 조인 규모에 상한을 뒀다(`MAX_EORDER_NOTICES_PER_INGESTION = 5000`,
  `MAX_EORDER_ROWS_PER_NOTICE = 4`). 30일 관측치가 공고 1,213건이므로 여유가 있다.

### 2.3 manifest (`pps_enrichment.py`)

조사 문서는 `MAX_ATTACHMENTS_IN_MANIFEST = 10` 상한을 넘을 수 있으니 상한 정책을 먼저
정하라고 했다. **공고가 선언한 슬롯을 밀어내지 않는 쪽으로 정했다.**

- 슬롯 1~10 은 지금처럼 `ntceSpecDocUrl*` 전용이다.
- 제안요청서는 슬롯 11~12 에 별도 정원(`MAX_RFP_ATTACHMENTS_IN_MANIFEST = 2`)으로
  들어간다. 공고 첨부는 한 건도 잘리지 않는다.
- **한 요청의 비용 한도는 늘지 않는다.** 다운로드 바이트·추출 문자·모델 호출·요청당
  신규 첨부 수는 전부 기존 `MAX_ATTACHMENTS_IN_MANIFEST` 에 묶인 채로 두었다. 늘어난
  것은 manifest 정원뿐이다.
- 정원 안에서는 `제안요청서` 가 `기타문서` 보다 먼저다. 실제로 **어느 첨부를 먼저
  읽는지**는 기존 `_scoring_table_reading_order` 가 이미 제안요청서를 최우선(rank 3)
  으로 두고 있어 그대로 동작한다. 슬롯 번호가 뒤여도 읽는 순서는 앞선다.
- 안전하지 않은 행은 공고 첨부와 같은 규칙으로 digest 만 남겨 커버리지에서 사라지지
  않게 했다.

## 3. 조사 문서에서 고쳐야 할 전제

### 3.1 레거시 `.hwp` 는 이미 지원된다 (6.4 는 과제가 아니다)

조사 문서 4.2·6.4 는 "파이프라인은 PDF 와 HWPX 만 읽는다(`HWP_ONLY_UNSUPPORTED`)"를
근거로 제안요청서의 약 45%를 별도 과제로 미뤘다. **이 전제는 최신이 아니다.**

- `.hwp` 는 `_EXTRACTABLE_EXTENSIONS` 에 들어 있어 미지원 분기로 가지 않는다.
- `document_extraction.py` 에 HWP5 리더(`_extract_hwp5`)가 있다. 섹션 압축 해제와
  BinData 분류까지 한다.

30일 구간의 제안요청서에서 24건을 무작위 표본으로 뽑아 **실제 파이프라인 추출기**로
돌렸다.

| 형식 | 표본 | 추출 성공 + 본문에 `배점` | 실패 |
|---|---|---|---|
| `.hwp` | 11 | **11** | 0 |
| `.hwpx` | 13 | 12 | 1 (`DOCUMENT_INPUT_TOO_LARGE`) |
| 합계 | 24 | **23 (96%)** | 1 |

레거시 `.hwp` 는 `.hwpx` 와 같은 비율로 읽힌다. 따라서 조사 문서의 "즉시 사용 가능
622 / 1,165" 는 형식 때문에 절반으로 깎을 이유가 없다.

### 3.2 매직바이트 판정은 이미 되어 있다 (6.1 네 번째 항목)

조사 문서 5.1 은 이름만 `.hwpx` 인 OLE 파일 2건을 찾아 "확장자 대신 매직바이트로
판정해야 한다"고 적었다. 그 판정은 `document_extraction.py:259` 에 이미 있다 —
확장자가 `.hwpx` 인데 내용이 OLE 서명이면 HWP5 리더로 보낸다. 반대 방향(`.hwp` 이름 +
ZIP 내용)도 `_has_exact_hwpx_mimetype` 로 검증한 뒤 HWPX 리더로 보낸다.

5.1 의 2건이 열리지 않은 것은 파일럿 스크립트가 zip/XML 로만 열어봤기 때문이고,
파이프라인의 한계가 아니다. **이 항목으로 고칠 코드는 없다.**

### 3.3 manifest 가 바뀌면 그 공고의 첨부를 **전부** 다시 읽는다 (비용)

조사 문서 6.2 는 20건 재분석에 "약 $2~3" 을 잡았다. 그 추정은 **새로 붙은
제안요청서 1건만 읽는다**고 가정한 값인데, 코드는 그렇게 동작하지 않는다.

- 추출 결과 재사용 조건에 `current_manifest_sha256` 동일성이 들어 있다
  (`pps_enrichment.py:2442`).
- 그 값은 첨부 하나의 해시가 아니라 **manifest 전체**의 해시다
  (`_digest(raw_manifest_values)`, 같은 파일 3987행).

따라서 제안요청서 한 건을 manifest 에 넣으면 그 공고의 **기존 첨부 전부**가
재사용 대상에서 빠지고 다시 유료 추출된다. 이것은 이 PR 이 만든 동작이 아니라
manifest 가 바뀔 때의 기존 동작이다(공고 차수 변경 때도 같다). 다만 이 변경은
그 조건을 **열린 공고 전반에 한꺼번에** 걸리게 한다.

조사 문서 수치로 어림하면: 열린 공고 221건에 첨부 614개(공고당 약 2.8개),
30일 창에서 겹친 공고 43건 → 재추출 약 120개 + 신규 제안요청서 43개 ≈ 163개.
첨부당 $0.108 이므로 **약 $18** 이다. $2~3 이 아니다.

수치의 전제는 "겹친 43건 전부가 재수집된다"이며, 일일 워크플로의 수집 창은
8일이므로 실제로는 여러 날에 나뉘어 발생한다. 정확한 값은 6.3 실측이 필요하다.

세 가지 선택지가 있고, 어느 것도 코드로 우회할 문제가 아니다.

1. **순서를 지킨다(권장).** 조사 문서 6.2 대로 소수 공고로 통제된 확인을 먼저
   하고, 그 결과를 본 뒤에 일일 워크플로가 나머지를 따라잡게 둔다.
2. 그대로 받아들인다. 재추출은 현재 검증기로 다시 도는 것이므로 완전히 버리는
   비용은 아니다 — 어차피 기존 17건의 `document_complete` 도 정량점수는 0 이었다.
3. `current_manifest_sha256` 을 제안요청서 추가에 둔감하게 만든다. **하지 말 것.**
   그 해시는 "이 추출은 정확히 이 manifest 를 근거로 만들어졌다"는 무결성 결속이고,
   느슨하게 하면 근거가 바뀐 추출을 재사용하게 된다.

### 3.4 새로 보이는 한계: 8MB 상한

표본 1건이 `DOCUMENT_INPUT_TOO_LARGE` 로 실패했다. `DEFAULT_MAX_DOWNLOAD_BYTES`
(8MB)를 넘는 제안요청서가 있다. 표본에서 4% 였다. 상한을 올릴지는 6.2 실측 뒤에
판단하는 것이 맞다 — 지금 올리면 근거 없이 비용 한도만 넓히는 일이다.

## 4. 검증

- 신규 테스트 8개. `tests/test_pps_integration.py` 가 오퍼레이션 파라미터·필드
  허용목록·30일 구간 분할을, `tests/test_pps_enrichment.py` 가 URL 가드의 경로별
  분리와 manifest 정원·읽기 순서를, `tests/test_api.py` 가 수집 엔드포인트에서의
  실제 조인을 덮는다.
- URL 가드는 양방향으로 막히는지 확인했다. 공고 경로에 `rfpNo` 를 써도, 제안요청서
  경로에 `bidPbancNo` 를 써도, 제3의 경로를 써도 `UNSAFE_ATTACHMENT_URL` 이다.

### 4.1 실제 데이터로 통과시킨 전 구간

합성 입력이 아니라 조달청 응답을 그대로 넣어 운영 헬퍼로만 통과시켰다. DB 와
모델 호출은 제외했고 그 외에는 파이프라인이 쓰는 함수 그대로다.

```
PpsClient.iter_eorder_attachments   실제 호출 (400행, 필드 6개로 축소 확인)
  -> build_attachment_manifest      슬롯 11 배정 확인
  -> download_public_attachment     URL 가드·리다이렉트 포함
  -> extract_pps_document_content
```

| 공고 | 결과 |
|---|---|
| 10 | **10 전부 추출 성공, 본문에 `배점` 존재** |

다운로드 거부(`UNSAFE_ATTACHMENT_URL`)도, 추출 실패도 없었다. 3장의 샘플 검사가
추출기만 본 것이었다면 이것은 URL 가드와 다운로더까지 포함한 확인이다.

## 5. 다음

조사 문서 6.2 그대로다. W10 에 붙인 뒤 겹치는 공고를 재분석해 `rule_source_status`
가 `AVAILABLE` 로 바뀌는지 본다. 3.1 때문에 대상이 `.hwpx` 로 제한되지 않으므로
조사 문서가 잡은 22건보다 넓게 잡을 수 있다. 첨부당 약 $0.108 이다.

**3.3 때문에 순서가 중요하다.** 배포 후 일일 워크플로가 먼저 돌면 통제된 확인
기회를 잃고 비용이 한꺼번에 나간다. 통제된 확인을 먼저 끝내는 것을 권한다.

6.3(커버리지 실측)과 6.5(미첨부를 명확한 종료 상태로)는 그대로 남아 있다.
6.4(레거시 `.hwp`)는 3.1 로 닫는다.
