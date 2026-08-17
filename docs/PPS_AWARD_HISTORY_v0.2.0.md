# PPS 3년 낙찰 후보 계약 v0.2.0

## 목적과 비목적

PAI_LOOP는 대상 공고의 제목에서 검색어를 만들거나 담당자가 입력한 검색어로
조달청 용역 최종낙찰 정보를 조회한다. 결과는 과거 계약을 빠르게 탐색하기 위한
**제목 유사 후보**다.

이 기능은 다음을 하지 않는다.

- 과거 후보가 현재 공고와 동일 사업이라고 확정하지 않는다.
- 수주 실적의 인정 여부나 인정금액을 계산하지 않는다.
- 참가자격, 적격성, 준비도, 입찰점수, GO/NO-GO를 변경하지 않는다.
- 사업자번호나 사람·연락처 정보를 수집·저장·노출하지 않는다.

## 공개 데이터 소스

- operation: `getScsbidListSttusServcPPSSrch`
- query division: 공고 게시일 기준 검색
- 필수 검색조건: 시작일, 종료일, 공고명 부분검색어
- 서버 비밀값: `PPS_API_KEY`

service key는 server-side HTTP client에서만 사용한다. query가 포함된 URL,
provider raw payload, response body 전체를 DB나 공개 로그에 저장하지 않는다.

## API 계약

### 후보 새로고침

```text
POST /api/v1/notices/{notice_key}/award-history/refresh
```

```json
{
  "keyword": "선택 검색어",
  "years": 3,
  "page_size": 100,
  "max_pages_per_window": 1,
  "dry_run": true
}
```

| 필드 | 제약 | 설명 |
| --- | --- | --- |
| `keyword` | 선택, 2~100자 | 생략 시 공고명에서 자동 생성 |
| `years` | 1~3, 기본 3 | 공고 게시일 기준 lookback |
| `page_size` | 1~100 | 구간별 페이지 크기 |
| `max_pages_per_window` | 1~3 | 30일 또는 fallback 구간별 상한 |
| `dry_run` | boolean | true이면 후보 행을 저장하지 않음 |

응답은 `job_id`, 대상 공고키, 사용 검색어, 조회기간, API 호출 수, fetch/create/
update/duplicate/record count와 warning을 반환한다. provider key나 raw row는
반환하지 않는다.

### 후보 조회

```text
GET /api/v1/notices/{notice_key}/award-history
GET /api/v1/notices/{notice_key}
```

공고 상세 응답에도 `award_history`가 포함된다. 날짜가 있는 후보는 최신 낙찰일
우선으로 정렬한다.

## 검색어 생성

명시적 검색어가 없으면 대상 공고명에서 다음 규칙으로 최대 세 토큰을 고른다.

1. 숫자·영문·한글 토큰만 남긴다.
2. 연도와 공고·용역·위탁·운영 등 일반 조달 불용어를 제외한다.
3. 한 글자 토큰을 제외한다.
4. 남은 앞 세 토큰을 공백으로 연결하고 100자로 제한한다.

유효 토큰이 없으면 `422`로 중단하고 담당자에게 검색어 입력을 요구한다.
자동 검색어는 편의 기능이므로 결과가 과도하면 핵심 과업명으로 좁혀 재조회한다.

## 제한 조회와 fallback

조회 기준일은 공고 게시일이며, 없으면 현재일이다. 시작일은 기준일에서 요청한
1~3년을 뺀 날이다. leap day는 이전 연도의 2월 28일로 안전하게 보정한다.

1. 전체 기간을 겹치지 않는 최대 30일 window로 분할한다.
2. 각 window를 페이지 상한 내에서 조회한다.
3. 정상 PPS envelope가 아닌 응답 또는 PPS 오류를 받은 30일 window만 7일
   window로 다시 나눈다.
4. 7일 window도 실패하면 해당 안전한 날짜 범위만 수집 감사로그와 warning에
   남기고 다음 구간을 계속한다.
5. 총건수가 페이지 상한을 넘으면 `hit_page_limit` warning을 반환한다.

fallback은 실패를 0건으로 숨기는 장치가 아니다. warning이 하나라도 있으면
담당자가 누락 구간과 검색어·페이지 상한을 검토해야 한다.

## 정규화 allow-list

저장 가능한 필드는 다음으로 제한한다.

| 분류 | 필드 |
| --- | --- |
| provider identity | 공고번호, 차수, 분류번호, 재입찰번호의 안정 조합 |
| 사업 정보 | 공고명, 발주기관, 낙찰 법인명 |
| 경쟁·가격 | 참가 업체 수, 낙찰금액, 낙찰률 |
| 일자 | 개찰일, 최종 낙찰일 |
| 파생·출처 | 제목 유사도, `PPS` source |

다음 provider 필드는 정규화 단계에서 읽더라도 반환 객체에 넣지 않는다.

- 사업자등록번호 또는 그에 준하는 업체 식별번호;
- 대표자 이름;
- 주소;
- 전화번호;
- 조달 담당자·공무원 이름 또는 연락처;
- 그 밖의 provider raw 필드.

필수 공개 필드인 identity, 공고번호, 공고명, 낙찰 법인명이 없는 row는
격리한다.

## 제목 유사도

유사도는 대상 공고명과 후보 공고명에서 연도·불용어를 제거한 뒤 두 값을
혼합한다.

```text
similarity = 100 × (0.35 × token Jaccard + 0.65 × character 3-gram Dice)
```

- 같은 정규화 제목은 100이다.
- 붙여 쓴 한글 복합어도 3-gram으로 일부 겹침을 포착한다.
- 0은 사업적으로 무관하다는 판정이 아니라 제목 표면 유사도가 없다는 뜻이다.
- 임계값만으로 후보를 자동 삭제하거나 동일사업으로 확정하지 않는다.

검색어 일치와 유사도는 서로 다른 역할이다. 검색어는 PPS 후보집합을 제한하고,
유사도는 반환된 후보의 제목 검토 순서를 돕는다.

## 멱등성·감사

대상 공고 ID와 provider identity 조합은 유일하다.

- 처음 보는 identity는 `created`;
- 저장 필드가 달라졌으면 `updated`;
- 같으면 `duplicates`;
- provider 응답 안의 중복 identity도 duplicate count에 포함한다.

모든 실행은 `PPS_AWARD` 수집 job으로 기간, 검색어, bounded 요청조건, count,
warning, 완료 상태를 기록한다. service key, raw payload, 개인·연락처 필드는
기록하지 않는다. `dry_run`도 감사 job은 남기되 award-history table은 바꾸지
않는다.

## n8n 자동화 경계

`PAI_LOOP 04 - Award History Refresh`는 PPS를 직접 호출하지 않고 보호된
PAI_LOOP backend의 refresh endpoint만 호출한다.

- manual trigger는 항상 `dry_run=true`다.
- schedule 또는 sub-workflow의 live 저장은
  `PAI_LOOP_LIVE_INGESTION_ENABLED=true`일 때만 가능하다.
- notice key는 실행 input을 우선하고 승인된 환경변수를 fallback으로 사용한다.
- years 1~3, page size 최대 100, window당 page 최대 3으로 제한한다.
- 최종 workflow output은 aggregate count와 warning만 허용한다.
- PII 모양 필드가 응답에 나타나면 성공으로 전달하지 않고 실패시킨다.
- `PARTIAL` 결과에는 누락/fallback warning이 반드시 있어야 한다.
- manifest의 `publish:false`로 배포 후에도 inactive를 유지한다.

## 검증 기준

release 전 최소 다음을 확인한다.

- current-array와 legacy-wrapper PPS envelope parsing;
- 30일 window와 7일 fallback 경계;
- page limit과 실패 subwindow warning;
- 공개 필드 보존 및 금지 필드 폐기;
- dry-run 무변경;
- 같은 요청의 멱등 갱신;
- 공고 상세과 전용 endpoint의 동일 후보 노출;
- 현재 알고리즘으로 재계산한 유사도 불일치 0건;
- response·DB·로그·workflow export의 secret/PII 부재.

실데이터 검증에서도 non-empty 후보, fallback, 멱등 처리, 점수 일관성을
확인했다. 개별 공고명·기관명·수주자명은 공개 검증 문서에 기록하지 않는다.
