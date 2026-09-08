# 낙찰 조회 실패 응답 구조 진단

낙찰 이력 조회의 `AWARD_PAGE_INVALID`만으로 정상 빈 응답을 잘못 거부했다고
단정할 수 없다. 이 진단은 파서가 이미 거부한 JSON 응답의 **구조만** 구분한다.
파서 허용 범위, 날짜 분할, 페이지 수, fallback, 재시도 및 deadline은 그대로다.
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
0으로 바꾸지 않는다. 이는 **진단 분류**이며 float0.0이나 items 누락 응답을
파서가 허용한다는 뜻이 아니다.

파싱된 JSON 객체에서 발생한 common-parser/award-page 실패만 대상으로 한다.
HTTP·통신·비JSON 실패는 기존 `window_error_counts`를 따른다. 따라서 빈 shape
집계만으로 전체 조회 성공을 단정하지 않는다. 상태·기존 오류 집계·PARTIAL과
함께 읽는다. 원응답 저장이나 로그 추가는 하지 않는다.

## 합성 검증

- 정상 빈 items의 배열/null/빈문자열/빈객체/item배열 및 숫자·문자열0은 기존대로 성공.
- items 부재, totalCount 자료형·부재, item null/빈문자열, 비객체 행은 기존 오류 유지.
- PRIMARY/FALLBACK 구분,32종·1000행 상한, 초과 횟수, 재조회 초기화 검증.
- 예외 문자열·알 수 없는 키·행 내용에 넣은 합성 비밀 표식이 진단/저장/응답에 없음.
- 실제 서버 인증·합성 활성 계정으로 API 접근 경계와 읽기 전용 동작 검증.
- 기존 refresh 응답을 실제 W10 JS 검증기에 넣는 계약 검사 유지.

코드 준비 과정에서 운영 API/PPS/모델을 호출하지 않았다. 실제 실패 원인이
어떤 응답 형태인지는 아직 확정하지 않았다. 다음 운영 확인은 별도 승인된
최소 범위에서 수행하며, 이 진단 추가 자체가 재조회 승인이나 파서 완화는 아니다.
