# 부서 추천·지역 라우팅 규칙 v0.4.0

## 목적과 경계

공고의 사업부 적합도와 담당 지역을 설명 가능한 키워드로 분류하되, **사업부 추천**, **추가 검토 후보**, **지역 라우팅**을 서로 다른 결과로 제공한다. 이 결과는 공고 발견과 내부 배분을 위한 보조정보이며 참가자격, 정량점수, 리스크 또는 최종 입찰 결정을 변경하지 않는다.

## 공개 프로필 데이터

- 위치: `src/pai_loop/data/department_keyword_profiles.json`
- 기준 버전: `2026.08.18-1`
- 포함 항목: 그룹, 부서, 동의어, 강한 키워드, 보조 키워드, 제외 신호, 담당 지역, 점수 가중치, 실행 가능한 추천 정책
- 제외 항목: 임직원 이름, 연락처, 계정 및 기타 개인정보
- 전사 기본 키워드: `교육`, `컨설팅`

프로필의 `ranking_policy`와 애플리케이션 상수는 다음 계약으로 일치해야 하며, 불일치하면 프로필 로딩을 실패 처리한다.

```json
{
  "business_top": {
    "min_strong_keywords": 1,
    "min_supporting_keywords": 2
  },
  "business_review": {
    "strong_keywords": 0,
    "supporting_keywords": 1
  },
  "region_routing": {
    "separate_from_business_rank": true
  }
}
```

## 점수와 추천 자격의 분리

| 필드 | 의미 |
|---|---|
| `score` | 사용자 검색어, 전사 공통, 선택 부서, 지역, 제외 신호를 모두 반영해 `0~100`으로 제한한 표시 점수 |
| `raw_score` | 제한하기 전 감사용 합계 |
| `business_score` | `DEPARTMENT_STRONG`과 `DEPARTMENT_SUPPORTING`만 합산한 사업부 적합도 |
| `routing_score` | `REGION`만 합산한 지역 배분 점수 |
| `department_score` | 호환 필드. `BUSINESS`에서는 `business_score`, `REGION`에서는 `routing_score`와 같음 |
| `ranking_scope` | `BUSINESS` 또는 `REGION` |
| `recommendation_tier` | `TOP`, `REVIEW`, `ROUTING`, `NONE` 중 하나 |

점수가 양수라는 이유만으로 추천하지 않는다. 추천 자격은 아래 키워드 개수와 제외 신호를 별도로 적용해 결정한다.

## 결정 규칙

### 사업부 추천 `TOP`

지역그룹이 아닌 사업부 프로필에서 다음 중 하나를 만족해야 한다.

- 강한 키워드가 1개 이상 일치
- 강한 키워드가 없더라도 서로 다른 보조 키워드가 2개 이상 일치

`TOP`만 `top_department_rankings`에 들어가며 `business_score`, 전체 `score`, 부서명 순으로 정렬한다.

### 추가 검토 `REVIEW`

강한 키워드는 없고 보조 키워드가 정확히 1개 일치하면 `REVIEW`다. 이 결과는 `department_review_candidates`에만 들어가며 `top_department_rankings`와 사업부 상위 추천 순위에는 포함하지 않는다.

### 제외 신호의 하드 블록

시설공사, 식자재 등 `matched_exclusions`가 하나라도 있으면 사업부의 `TOP`과 `REVIEW`를 모두 차단해 `NONE`으로 처리한다. 강한 키워드가 함께 있거나 `business_score`가 양수여도 예외가 아니다.

### 지역 라우팅 `ROUTING`

`지역그룹` 프로필은 사업부 추천 대상이 아니다. 담당 지역이 일치할 때만 `ROUTING`으로 분류해 `region_routing`에 별도로 반환한다.

지역 라우팅은 업무의 적합성이나 입찰 권고가 아니라 물리적 담당 경로다. 따라서 비주력 제외 신호가 있어 사업부 추천이 차단된 공고에도 지역 정보가 확인되면 라우팅은 남을 수 있지만, `top_department_rankings`에는 절대 들어가지 않는다.

## API 결과 계약

`GET /api/v1/notices?department_id=organization` 응답은 다음 세 목록을 분리한다.

| 응답 필드 | 포함 대상 | 사업부 상위 순위 참여 |
|---|---|---|
| `top_department_rankings` | 기준을 충족한 `BUSINESS / TOP` | 예 |
| `department_review_candidates` | 보조 키워드 1개의 `BUSINESS / REVIEW` | 아니요 |
| `region_routing` | 지역 일치 `REGION / ROUTING` | 아니요 |

사용자가 특정 사업부를 선택하면 `department_ranking`에 그 사업부의 결과를 그대로 보여준다. 정렬은 같은 판정 그룹 안에서 `TOP` 또는 `ROUTING`, `REVIEW`, `NONE` 순으로 적용한다. 전사 공통 선택에서는 `TOP`, `REVIEW`, 미분류 순이며 지역 라우팅만으로 사업부 우선순위를 올리지 않는다.

일일 브리핑과 Teams 카드도 각각 `사업부 추천`, `추가 검토`, `지역 라우팅`으로 표시한다.

## 문자열 매칭 안전장치

1. 제목, 발주기관, 공고 분류 사이에는 내부 구분자를 넣어 서로 다른 필드의 단어가 하나의 다단어 키워드로 합쳐지지 않게 한다. 예를 들어 기관명의 `공공기관`과 분류의 `교육용역`이 `공공기관 교육`으로 오인되지 않는다.
2. `교육청`, `교육부`, `교육지원청` 등 발주기관명만으로 전사 교육 사업 신호를 만들지 않는다.
3. `title_required_keywords`로 지정된 키워드는 제목 또는 공고 분류의 사업 문맥에서 확인해야 한다.
4. 지역 프로필에서 동일 지명을 부서 키워드와 지역 키워드로 중복 가산하지 않는다.
5. 짧은 영문 약어는 부분문자열이 아닌 토큰으로 일치시킨다.

## 예시

| 공고 신호 | 사업부 결과 | 지역 결과 |
|---|---|---|
| `팀빌딩` | 보조 1개이므로 관련 사업부 `REVIEW` | 지역명이 없으면 없음 |
| `팀빌딩 + 조직문화` | 보조 2개이므로 인재개발센터 `TOP` | 지역명이 없으면 없음 |
| `AI 에이전트 + 시설공사`, 발주기관 `인천광역시` | 제외 신호 때문에 사업부 `NONE` | 중부본부 `ROUTING`은 별도 유지 |
| `승진후보자 역량평가`, 발주기관 `인천광역시` | 역량솔루션본부 `TOP` | 중부본부 `ROUTING` |

## 운영 원칙

1. 조직 개편 시 기존 문서를 삭제하지 않고 프로필과 규칙 문서의 버전을 함께 올린다.
2. 웹 배포와 n8n 브리핑은 동일 프로필 버전 및 세 가지 결과 필드를 사용한다.
3. 키워드 변경에는 TOP, REVIEW, ROUTING, 제외 블록, 필드 경계 오탐 회귀 테스트를 포함한다.
4. 사람이 최종 담당 부서와 입찰 여부를 확인한다.
5. 사용자 검색어에 개인정보나 비공개 고객정보를 입력하지 않는다.
