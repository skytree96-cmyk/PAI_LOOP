# 낙찰 조회 실패 응답 구조 진단

낙찰 이력 조회의 `AWARD_PAGE_INVALID`만으로 정상 빈 응답을 잘못 거부했다고
단정할 수 없다. 이 진단은 파서가 이미 거부한 JSON 응답의 **구조만** 구분한다.
날짜 분할, 페이지 수, fallback, 재시도 및 deadline은 그대로다. 확인된 정상 빈
응답의 제한적인 허용 조건은 아래에 명시한다.
개찰 결과·참여회사 판정에는 적용하지 않는다.

## 조회

기존 refresh 응답은 그대로이며 새 필드는 없다. W10의 엄격한 응답 계약과
워크플로를 변경하지 않는다. 완료된 refresh 응답의 `job_id`로 다음을 읽는다.

`GET /api/v1/ingestion/jobs/{job_id}/award-diagnostics`

- 기존 서버 키 인증만 허용한다. 부서/관리자 쿠키와 폐기된 PIN은 허용하지 않는다.
- 반환은 `job_id`, `diagnostics`뿐이다. 요청 본문 전체, 날짜별 검색값, provider
  원응답, URL, 업체, 연락처, 원문, 문자열 값, resultMsg를 노출하지 않는다.
- 별도 `request_json.award_page_shape_diagnostics`만 엄격한 모델로 검증해 투영한다.
  다른 종류의 작업/없는 작업은404, 변조·불완전 진단은 고정409다.
- 과거 작업의 `diagnostics: null`은 수집하지 않았다는 뜻이다. 기존 실패의
  원응답 구조를 복원할 수 없으며 동일한 전체 조회를 반복하지 않는다.
- GET은 DB 읽기만 하며 provider 호출이나 job 생성을 하지 않는다.

## 기록 범위와 한도

`diagnostics.counts`는 최대32개의 `(phase, error_type, shape)`별 횟수다.
이미 보관한 종류는 계속 합산하고 새 종류가 한도를 넘으면
`suppressed_count`만 늘린다. 각 새 조회는 집계를 초기화한다.

shape의 고정 경로는 response/header/resultCode/body/totalCount/items/item다.
자료형은 `MISSING/NULL/BOOLEAN/INTEGER/NUMBER/STRING/ARRAY/OBJECT/OTHER` 중
하나이며 `MISSING`으로 키 부재를 구분한다. 공급자의 문자열 값이나 키 이름은
복사하지 않는다. `array_length`는 최대1000, `object_rows`는 첫1000개 중 객체 수,
`array_length_capped`는 초과 여부다. 배열이 아니면 배열 길이는 null이며 단일
객체 행은 object_rows=1이다.

`total_count_explicit_zero`는 정수0·숫자0.0 또는 정확한 문자열 `"0"`일 때만
true다. 자료형은 별도 기록한다. null·빈문자열·boolean false·공백 포함 문자열을
0으로 바꾸지 않는다. 이는 **진단 분류**이며 float0.0을 파서가 허용한다는 뜻이 아니다.

공통 파서의 성공 envelope 검증을 통과하고 `totalCount`가 정수0 또는 정확한
문자열 `"0"`인 경우에만 `items` 키 부재를 빈0행으로 허용한다. bool·float·null·
빈문자열·공백·`"00"`·누락·0이 아닌 건수는 이 허용 조건에 해당하지 않는다.
오류 envelope도 빈 결과로 바꾸지 않는다. 기존 items가 있는 응답의 검증 및
개찰 결과/참여회사 경로는 변경하지 않는다.

정상 빈 구간은 fallback 없이 다음 기간으로 진행한다. 같은 기간의 다음 페이지에서
전체 건수가0으로 바뀌면 기존 건수 일관성 검사가 여전히 부분 수집으로 표시한다.
단일 호출 진단은 정상 빈 결과라도 추가 조회 없이 PARTIAL을 유지한다.

파싱된 JSON 객체에서 발생한 common-parser/award-page 실패만 대상으로 한다.
HTTP·통신·비JSON 실패는 기존 `window_error_counts`를 따른다. 따라서 빈 shape
집계만으로 전체 조회 성공을 단정하지 않는다. 상태·기존 오류 집계·PARTIAL과
함께 읽는다. 원응답 저장이나 로그 추가는 하지 않는다.

## 합성 검증

- 정상 빈 items의 배열/null/빈문자열/빈객체/item배열 및 숫자·문자열0은 기존대로 성공.
- 성공 envelope의 items 부재+정확한 정수/문자열0은 빈 결과로 허용하고 fallback하지 않음.
- items 부재+부정확한0/비0, totalCount 자료형·부재, item null/빈문자열, 비객체 행은 기존 오류 유지.
- 빈 기간 뒤 정상 낙찰 행의 수집 연속성과 페이지 중간 건수 변경의 불완전 판정 유지.
- PRIMARY/FALLBACK 구분,32종·1000행 상한, 초과 횟수, 재조회 초기화 검증.
- 예외 문자열·알 수 없는 키·행 내용에 넣은 합성 비밀 표식이 진단/저장/응답에 없음.
- 실제 서버 인증·합성 활성 계정으로 API 접근 경계와 읽기 전용 동작 검증.
- 기존 refresh 응답을 실제 W10 JS 검증기에 넣는 계약 검사 유지.

운영자가 제공한 안전한 구조 진단으로 성공 envelope·정수0·items 부재의 거부를
확인하여 위 형태만 허용했다. 다른 실패의 원인은 이 사실만으로 단정하지 않는다.
코드 준비 과정에서는 운영 API/PPS/모델을 호출하지 않았다. 다음 운영 확인은
별도 승인된 최소 범위에서 수행하며, 이 수정 자체가 전체 재조회 승인은 아니다.

## 명시적 단일 호출 진단

기존 인증을 그대로 사용하는 아래 refresh에 `diagnostic_probe: true`를 명시하면
**실제 HTTP 요청을 최대1회** 실행한다. 모델 호출은 없다. 이것은 기본 false이며
기존 W10 요청·응답 키·일반 조회 동작은 바뀌지 않는다.

`POST /api/v1/notices/{notice_key}/award-history/refresh`

```json
{
  "keyword": "SYN 교육",
  "years": 3,
  "page_size": 100,
  "max_pages_per_window": 1,
  "dry_run": true,
  "include_opening_results": false,
  "diagnostic_probe": true
}
```

- `dry_run=true`, `include_opening_results=false`가 필수다. 다른 조합과 문자열
  `"true"`인 diagnostic_probe는 provider 호출·감사 작업 생성 전에422로 거절한다.
- 요청한 기간 중 **가장 이른 첫30일 구간의 첫 페이지**만 읽는다. `window`는
  요청 기간이며 전체 조회 완료 범위가 아니다. 기존 실패를 재생하거나 특정 실패
  구간을 선택하는 기능은 없다. 다음 실제 확인은 승인된 요청1회로만 진행한다.
- 클라이언트 생성 때 retry budget을0으로 고정하고 HTTP 진입 전에 한 번만
  예약한다. 성공·빈 응답·파싱 실패·HTTP 오류·timeout 모두 다음 페이지/기간,
  7일 fallback, 개찰 조회를 실행하지 않는다. redirect도 따라가지 않는다.
  기존 deadline이 호출 전에 만료되면 실제 호출은0회다. 같은 클라이언트 재사용으로
  예약을 다시 열 수 없다. 새 POST는 별도 요청이므로 **자동 재전송하지 않는다**.
- 응답을 받은 직후 deadline에 도달하더라도 이미 받은 파싱 실패의 안전한 shape를
  기록한다. 파서 허용 범위는 그대로이며 요청 상한 중단을 provider 오류로 만들지 않는다.
- 정상 첫 페이지와 빈 결과도 `status=PARTIAL` 및 고정 경고 접두어
  `AWARD_DIAGNOSTIC_PROBE_SINGLE_CALL:`로 전체 기간 미조회를 표시한다.
  `fetched`는 첫 페이지에서 검색어에 맞은 행 수이고, `created/updated/duplicates/records`
  는 모두0이다. 일반 dry_run의 예상 변경 건수와 달리 이 모드는 변경 계획도 생성하지 않는다.
- 낙찰 이력·개찰 업체·사람의 판단 기록을 만들거나 수정하지 않는다. 감사 job만
  `DRY_RUN/PARTIAL`, `diagnostic_probe=true`, 호출 수 및 기존 안전한 오류/shape 집계로 저장한다.
  원응답, 공급자 문자열·업체정보·URL을 저장하거나 응답에 추가하지 않는다.
- 응답의 `job_id`로 위 서버 키 전용 GET을1회 읽는다. HTTP·통신·비JSON 오류에는
  파싱 가능한 shape가 없으므로 기존 `window_error_counts`와 함께 판단한다.
  이1회가 성공/빈 응답이면 실패 shape를 얻지 못할 수 있으며 과거189회 조회를 반복하지 않는다.

합성 회귀는 HTTP handler의 실제 호출 수를 세어 유효 행·빈 응답·표준 envelope 누락·
거부 페이지·HTTP503·redirect·timeout·비JSON·provider 오류 각각1회, 선행 deadline은0회를
검증한다. 직접 클라이언트의 retries3 설정도1회로 제한되며, 기존 낙찰 및 사람 판단의
전체 열 값 보존, 원응답 표식 비노출, 동일 응답 키와 기본 조회의 다음 기간 처리를 확인한다.
