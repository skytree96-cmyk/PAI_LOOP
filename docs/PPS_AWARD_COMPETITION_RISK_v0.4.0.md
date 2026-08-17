# PAI_LOOP 경쟁·집중 리스크 v0.4.0

## 목적과 분리 원칙

`GET /api/v1/notices/{notice_key}/award-intelligence`는 저장된 최근 3년 제목 유사 낙찰·개찰 후보로 경쟁 집중 신호를 계산한다. 외부 조달청 API, OpenAI 또는 Teams를 호출하지 않는다.

이 결과는 다음 값과 별도다.

- 참가자격 평가의 `risk_band`
- 정량평가 준비도 및 점수 범위
- 담당자의 `GO / HOLD / NO-GO`
- 법적 독점·시장지배력 또는 전체 시장점유율 판단

점수는 `MODEL_ESTIMATE`일 때만 제공한다. 필수 원천 사실이 부족하거나 중복되면 `UNKNOWN`, `score=null`, `band=UNKNOWN`으로 닫힌다. 누락값을 0점 또는 저위험으로 해석하지 않는다.

## API 계약

응답의 `competition_risk`는 다음 필드를 제공한다.

| 필드 | 의미 |
|---|---|
| `method_version` | `competition-risk-1.0.0` |
| `scope` | `STORED_3Y_SIMILARITY_CANDIDATES` |
| `status` | `MODEL_ESTIMATE` 또는 `UNKNOWN` |
| `score` | 0~100 또는 `null` |
| `band` | `LOW / MODERATE / HIGH / VERY_HIGH / UNKNOWN` |
| `confidence` | `HIGH / MEDIUM / LOW / INSUFFICIENT` |
| `components` | HHI, 상위 수주 비중, 참여기관 수, 후보 제목유사도 |
| `coverage` | 전체 행과 낙찰자·참여기관·일자·유사도 가용 건수 및 비율 |
| `market_claim` | 항상 `NOT_DETERMINED` |
| `warnings` | 점수 보류 또는 해석 한계 |
| `separation_notice` | 참가자격·최종결정과의 분리 고지 |

각 점수 구성요소에는 원천 파생값, 0~100 변환점수, 가중치, `DERIVED_FROM_STORED_FACTS` 상태와 설명이 함께 반환된다. 후보 제목유사도는 점수에는 섞지 않고 신뢰도 상한에만 사용한다.

## 점수 산식

### 1. HHI 구성요소, 45%

저장 후보에서 낙찰자가 명시된 행만 사용한다.

```text
HHI = Σ(수주 빈도 비율 × 100)²
```

HHI 원값을 `(0→0, 1500→30, 2500→60, 10000→100)` 앵커 사이에서 선형 변환한다. 이는 유사 후보 표본의 빈도 집중도이며 전체 시장 HHI가 아니다.

### 2. 상위 수주 비중, 30%

저장 표본에서 최다 수주기관의 비율을 사용한다. 20% 이하는 0점, 80% 이상은 100점으로 두고 사이를 선형 변환한다. 기관명 문자열은 공백만 정리하며 별칭을 임의 병합하지 않는다.

### 3. 참여기관 수, 25%

양의 정수로 저장된 참여기관 수의 중앙값을 사용한다. 중앙값 6곳 이상은 0점, 1곳은 100점으로 두고 다음처럼 제한한다.

```text
participant risk = clamp((6 - median participant count) × 20, 0, 100)
```

단독참여 표본 비율과 평균도 설명 가능한 원천 사실로 반환하지만 중복 가중하지 않는다.

### 합계와 구간

```text
score = 0.45 × HHI risk
      + 0.30 × top winner share risk
      + 0.25 × participant count risk
```

- `LOW`: 0~24.99
- `MODERATE`: 25~49.99
- `HIGH`: 50~74.99
- `VERY_HIGH`: 75~100

## fail-closed 커버리지 문턱

아래 조건을 모두 충족해야 수치점수를 제공한다.

1. 저장 후보 5건 이상
2. 낙찰자 5건 이상이며 전체의 60% 이상
3. 참여기관 수 5건 이상이며 전체의 60% 이상
4. 개찰·낙찰 일자 5건 이상이며 전체의 80% 이상
5. 동일 공개 레코드 키 중복 없음

제목유사도는 점수 제공 문턱이 아니지만 신뢰도를 제한한다.

- 핵심 사실 20건 이상, 최소 커버리지 90% 이상: `HIGH`
- 핵심 사실 10건 이상, 최소 커버리지 80% 이상: `MEDIUM`
- 그 외 점수 가능 표본: `LOW`
- 유사도 미확보 또는 중앙값 40% 미만: 최대 `LOW`
- 유사도 중앙값 40~69.99%: 최대 `MEDIUM`

## 실제 공개 시드 회귀값

공개 인천 공고에 연결된 59건 중 공고 게시일 기준 최근 3년 창에 포함된 57건을 사용한다.

- 유효 낙찰자: 57/57
- 유효 참여기관 수: 57/57
- 유효 일자: 57/57
- HHI: 1,585.10
- 상위 수주 비중: 24.56%
- 참여기관 수 중앙값: 3곳
- 결과: `31.93 / MODERATE`
- 제목유사도 중앙값: 31.75%
- 신뢰도: `LOW`

낮은 후보 유사도 때문에 표본 수가 많더라도 신뢰도를 높이지 않는다. 이 회귀값은 공개 스냅샷 버전이 바뀌면 테스트와 함께 재검토한다.

## 소비 위치

- 공고별 낙찰 인텔리전스 API: 전체 계약 반환
- 7일 daily briefing: `competition_risk` 최상위 필드와 `pricing_intelligence.competition_risk`에 동일 계약 반환
- SPA 낙찰 탭: 점수·구간·신뢰도, HHI, 상위 비중, 참여기관 중앙값과 경고 표시
- 기본 Teams mock 카드: 점수 가능 시 `band · score/100 · confidence`, 부족하면 `UNKNOWN · 표본/커버리지 부족`

Teams 카드는 계속 mock-only이며 이 기능은 외부 전송 권한을 추가하지 않는다.
