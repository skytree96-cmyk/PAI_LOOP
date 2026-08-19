# PPS 실데이터 수집·근거 추출 계약 v0.6.1

## 목적

오전 9시 n8n 실행이 조직 검색어로 조달청 공고를 수집하고, 현재 유효한 상위 공고만 공개 첨부 근거 추출과 결정론적 분석 파이프라인에 전달한다. 원본 provider payload, 담당자 연락처, API key, 첨부 원문은 DB에 저장하지 않는다.

## 수집 계약

`POST /api/v1/ingestion/pps/notices`

- 기존 `keyword`와 `keywords`(최대 30개)를 함께 지원한다.
- `use_profile_keywords=true`이며 부서를 생략하면 조직 공통 discovery `교육`, `컨설팅`, `연수`, `포럼`, `위탁 운영`과 24개 부서별 대표 strong keyword 1개, 총 29개의 고유 검색어를 사용한다. discovery 검색어는 부서 추천 점수를 올리는 vocabulary가 아니라 PPS 누락 방지용 provider query다.
- 명시적 사용자 검색어는 공통 discovery 다음, 부서 확장보다 먼저 배치한다. 따라서 30개 상한에서 사용자가 입력한 검색어가 부서 profile에 밀려 조용히 누락되지 않는다.
- 각 검색어는 조달청 `bidNtceNm` 서버측 필터로 별도 조회한다.
- 같은 공고번호의 수정차수·마감은 최신 유효 건 하나로 축약하며, 이전 DB 행은 `CLOSED`로 전환한다.
- 직찰 공고처럼 `bidClseDt`가 비어 있고 공식 `opengDt`만 있는 경우 개찰시각을 보수적인 최신 검토 경계로 사용하고 metadata에 `deadline_basis=OPENING_FALLBACK`을 남긴다. 전자입찰 마감시각으로 오인하지 않으며, 첨부 분석 후 실제 제출마감 근거를 우선한다.
- 실제 응답에서 확인한 `ntceKindNm=취소공고`는 OPEN 후보에서 제외하고 기존 행을 `CLOSED`로 전환한다.
- 결과 키는 조직 적합도 내림차순, 게시일 최신순이다.
- 전체 수집은 190초 wall budget과 요청당 12초/1회 retry로 제한한다. 일부 검색어만 실행되면 HTTP 200 `PARTIAL`과 경고를 반환한다.
- `department_coverage_count`는 선택한 profile의 기대 coverage이며, 실제 실행 수는 `provider_queries`와 `keywords_used` 길이로 비교한다.

## 저장 경계

각 공고의 `NoticeVersion`에는 아래 allowlist만 멱등 저장한다.

- 공고번호·수정차수
- 실행 검색어 provenance
- 계약/낙찰/가격평가 방식 등 구조화 공고 메타
- `https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do`의 검증된 PDF/HWPX/HWP manifest

담당자 이름, 이메일, 전화, raw provider payload는 저장하지 않는다. manifest URL은 허용 host/path/query key/value를 모두 검증한다.

## 첨부 enrichment 계약

`POST /api/v1/notices/analysis/batch`

- `enrich_missing=true`, `max_notices<=3`, `max_attachments_per_notice=1`이다.
- 최신 PPS metadata manifest에 결합된 ACCEPTED extraction만 재사용한다. manifest가 바뀌면 과거 attachment evidence는 자동 분석에서 제외한다. 명시적 `source_version_ids`는 감사용 override로 유지한다.
- PDF/HWPX는 메모리에서만 최대 8 MiB, redirect 2회, PDF 120쪽, 텍스트 120,000자로 처리한다. 파일로 쓰지 않는다.
- HWP는 같은 manifest의 PDF/HWPX를 우선하고 없으면 결정적 R07 REVIEW로 남긴다.
- OpenAI는 strict schema와 원문 quote 검증을 통과한 구조화 결과만 저장한다.
- 동일 문서 SHA + prompt의 ACCEPTED 및 결정적 HWP REVIEW는 영구 재사용한다. 일시적 REVIEW는 24시간 cooldown 뒤 한 번 재시도하고 새 attempt version을 남긴다.
- 공고당 download 12초 + OpenAI 45초/no retry, batch wall guard 205초다. 공고별 실패는 응답 `PARTIAL`로 흡수한다.
- `enrichment.attempted == completed + skipped + failed` invariant를 보장한다.

## 공개 projection

- 익명 상세에는 strict 검증된 ACCEPTED PPS extraction의 allowlist만 노출한다.
- summary, normalized condition, section, quote 등에 포함된 이메일·전화·담당/문의 이름은 `[비공개]`로 치환하고 잔여 식별자가 있으면 fail closed 한다.
- 내부 materialized `AtomicRequirement.source_excerpt`는 익명 상세에서 노출하지 않는다.
- 공개 requirement-policy도 redacted extraction을 입력으로 사용한다.

## 검증 기록

- 실제 조달청 최근 8일 교육 검색 100건에서 공고종류 집계: 등록공고 71, 변경공고 6, 재공고 20, 취소공고 3.
- 실제 공개 PDF 1건을 저장 없이 메모리 처리: 209,904 bytes, 추출 텍스트 4,324자.
- targeted backend 회귀: 70 tests passed.
