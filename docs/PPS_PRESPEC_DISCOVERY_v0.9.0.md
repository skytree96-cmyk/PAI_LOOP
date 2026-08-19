# PPS 사전규격 조기발견 설계 v0.9.0

## 결론

사전규격은 입찰공고보다 먼저 공개되므로 별도 inbox로 수집하면 공고 당일의 짧은 검토 시간을 줄일 수 있다. 다만 사전규격은 아직 입찰이 아니므로 `Notice`나 GO/REVIEW 판정 큐에 바로 넣지 않는다. `PPS_PRESPEC` 소스와 별도 식별자를 유지하고, 공식 `bidNtceNoList`가 생겼을 때만 실제 입찰공고와 연결한다.

이번 변경에는 공식 용역 사전규격 응답의 개인정보 제거 정규화, 문서 URL 검증, 30일 window·최대 20페이지 bounded client, 제목/기관 로컬 키워드 matching과 회귀 테스트까지 포함한다. 운영 DB migration과 HTTP route는 아래 계약으로 동결했으며 별도 제품·배포 gate에서 추가한다.

## 공식 API

- 공공데이터포털: [조달청 나라장터 사전규격정보서비스](https://www.data.go.kr/data/15129437/openapi.do)
- base: `https://apis.data.go.kr/1230000/ao/HrcspSsstndrdInfoService`
- 용역 목록: `GET /getPublicPrcureThngInfoServc`
- 조회구분: `inqryDiv=1` 등록일시, `2` 사전규격등록번호, `3` 변경일시
- 등록일시 조회: `inqryBgnDt`, `inqryEndDt`, `pageNo`, `numOfRows`
- 공식 식별자: `bfSpecRgstNo`
- 사업명: `prdctClsfcNoNm`
- 주요 공개 필드: `orderInsttNm`, `rlDminsttNm`, `asignBdgtAmt`, `rgstDt`, `chgDt`, `opninRgstClseDt`, `specDocFileUrl1..5`, `bidNtceNoList`

이 목록 operation에는 사업명 검색 파라미터가 없다. 따라서 등록/변경 watermark로 한 번 수집한 뒤, 제목·기관을 로컬에서 `교육`, `컨설팅`, `연수`, `포럼`, `위탁 운영` 및 부서 profile로 filtering/ranking한다. 2026-08-12~19 실조회는 총 935건이어서 100행 기준 약 10페이지였고, 첫 100행 안에서 discovery 검색어 8건을 확인했다. 서버측 제목검색을 흉내 내기 위해 여러 호출을 만들지 않는다.

## 이번 누락 3건의 원천 확인

| 사용자 안내명 | 실제 입찰공고 | 입찰 게시 | 사전규격 | 사전규격 등록 | 원인 |
|---|---|---:|---|---:|---|
| 2026년 의령군 공무원 역량강화교육 용역(협상에 의한 계약) | `R26BK01678610` | 2026-08-12 | `R26BD00261492` | 2026-08-06 | `교육` 조회에는 포함되지만 직찰이라 `bidClseDt`가 공란, 기존 deadline 필수 경계에서 격리 |
| 2026년 YES수성 연수 위탁 용역 | 실제 제목 `2026년 수성구청 직원 YES수성 연수 위탁 용역`, `R26BK01677139` | 2026-08-13 | `R26BD00260521` | 2026-08-05 | `연수`가 provider query에 없었고 직찰 close 공란으로 이중 누락 |
| 제10차 경제안보외교포럼 위탁 운영 | `R26BK01682487` | 2026-08-14 | `R26BD00262106` | 2026-08-10 | `포럼`/`위탁 운영`이 provider query에 없었음 |

세 입찰 모두 2026-08-19 실행의 전일~당일 window 밖이다. 코드의 검색어·직찰 fallback 수정과 별도로 다음 운영 조치가 필요하다.

1. 한 번만 `2026-08-12..2026-08-19` recovery backfill을 실행한다.
2. 이후 마지막 성공 watermark부터 오늘까지 읽되, 중복에 안전한 1일 overlap과 최대 7일 recovery cap을 둔다.
3. cap을 넘는 장애 복구는 날짜를 하루 단위로 분할한다. 검색어별 첫 100건만 읽는 넓은 기간 실행은 금지한다.
4. 사전규격은 최초 30일 backfill 후 등록 watermark와 변경 watermark를 각각 유지한다.

## 정규화·보안 경계

`normalise_pre_specification`은 다음 공개 business 필드만 반환한다.

- `source_kind=PPS_PRESPEC`
- `pre_specification_key=PRESPEC-{bfSpecRgstNo}`와 원본 등록번호
- 사업명·업무구분·참조번호·발주/수요기관
- 배정예산, 접수/등록/변경/의견마감/납품 시각
- SW 사업 여부, 검증된 규격문서 URL, 연결 입찰공고번호

`ofclNm`, `ofclTelNo`와 알 수 없는 provider 필드는 폐기한다. 규격문서는 HTTPS `www.g2b.go.kr/pn/pnz/pnza/UntyAtchFile/downloadFile.do`만 허용하고 query를 `bfSpecRegNo`, `fileType=BFDTL`, `fileSeq`로 제한한다. 서비스 키·raw payload·문서 본문은 저장하지 않는다.

## 운영 DB 계약(후속 migration)

별도 `pre_specifications` 테이블을 사용한다.

- PK `id`, unique `registry_no`, stable `pre_specification_key`
- `title`, `ordering_agency`, `demand_agency`, `business_division`, `reference_no`
- `budget_amount`, `registered_at`, `changed_at`, `opinion_deadline`, `delivery_due`
- `status`: `OPEN_FOR_OPINION`, `OPINION_CLOSED`, `LINKED_TO_BID`
- `linked_bid_notice_nos` JSON, `matched_keywords` JSON
- `source_digest`, `first_seen_at`, `last_seen_at`

문서 URL은 `pre_specification_documents(pre_specification_id, slot, safe_url, source_digest)`로 분리한다. `registry_no + source_digest`가 같으면 no-op이고, `chgDt`/digest가 바뀌면 version을 남긴다. `bidNtceNoList`가 채워지면 `Notice.bid_notice_no`와 연결하되 사전규격 행을 삭제하지 않는다.

## HTTP 계약(후속 route)

- 보호 write: `POST /api/v1/ingestion/pps/pre-specifications`
  - 입력: `from_date`, `to_date`, `mode=REGISTERED|CHANGED`, `page_size<=999`, `max_pages<=20`, `dry_run`
  - 출력: `fetched`, `matched`, `created`, `updated`, `linked`, `quarantined`, `api_calls`, `hit_page_limit`, `next_watermark`
- 공개 read: `GET /api/v1/pre-specifications?status=OPEN_FOR_OPINION&search_keywords=...`
  - DB만 읽고 PPS 실호출을 하지 않는다.
  - 제목·기관 substring match와 부서 ranking 근거를 함께 반환한다.
- exact recovery: `GET /api/v1/pre-specifications/{registry_no}`
  - DB 우선이며 운영자 권한에서만 `refresh=true`를 허용한다.

UI에서는 ‘사전규격/예정’ badge와 의견마감일을 보여주며 GO 판정을 숨긴다. 연결 입찰이 생기면 ‘입찰공고 열기’로 전환한다.

## 승인 기준

- 등록/변경 watermark 각각 멱등성 확인
- page cap 도달 시 `PARTIAL`로 표시하고 watermark 전진 금지
- 담당자 이름·전화와 API key가 DB/API/log에 없음
- 사전규격이 GO/REVIEW 집계와 Teams 입찰 알림에 섞이지 않음
- 연결된 입찰공고가 생겨도 사전규격의 조기발견 이력 보존
