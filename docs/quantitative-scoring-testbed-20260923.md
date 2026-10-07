# 정량 점수 사슬 로컬 테스트베드 — 2026-09-23

신용등급 하나로 확인했던 계산 사슬을, 저장소가 실제로 지원하는 지표 전체에 대해
합성 데이터로 종단 검증한다. 운영 데이터베이스·외부 서비스·유료 제공자를 부르지
않는다.

검증 구간:

```
회사 사실(CompanyFact + Evidence)
  → resolve_verified_quantitative_facts   키·검증·유효기간·증빙·결속 해시
  → QuantitativeEstimateRequest           기준 + 결합된 사실
  → estimate_quantitative_score           결정론적 채점
  → 항목별 점수와 총점 상태
```

## 실제로 지원하는 것

코드를 읽어 확인한 결과, canonical 등록부(`_CANONICAL_METRIC_REGISTRY`)의 지표
**10종 전부**와 계산식 **6종 전부**가 구현되어 있다. 문서가 아니라 등록부와 채점기를
직접 읽어 확인했고, 테스트가 그 사실을 고정한다.

| 지표 | 사실 키 | canonical 단위 | 검증한 계산식 |
| --- | --- | --- | --- |
| PERFORMANCE_AMOUNT | `company.performance.amount` | KRW | BRACKET · THRESHOLD · FORMULA |
| PERFORMANCE_COUNT | `company.performance.count` | COUNT | BRACKET · THRESHOLD |
| PERSONNEL_COUNT | `company.personnel.count` | PERSON | BRACKET · THRESHOLD |
| CERTIFICATION_COUNT | `company.certification.count` | COUNT | BRACKET · THRESHOLD |
| AWARD_COUNT | `company.award.count` | COUNT | BRACKET · THRESHOLD |
| FACILITY_EQUIPMENT_COUNT | `company.facility_equipment.count` | COUNT | BRACKET · THRESHOLD |
| BUSINESS_YEARS | `company.business.years` | YEAR | BRACKET · THRESHOLD · FORMULA |
| FINANCIAL_RATIO | `company.financial.ratio` | PERCENT | BRACKET · THRESHOLD · FORMULA |
| CREDIT_RATING | `company.credit_rating` | RATING | CATEGORICAL · CASE_TABLE |
| LOCAL_PRESENCE | `company.local_presence` | BOOLEAN | BOOLEAN |

`test_the_testbed_covers_every_canonical_metric` 가 이 표와 등록부를 맞춰 본다.
저장소가 열한 번째 지표를 더하면 이 테스트가 먼저 깨진다.

## 지원하지 않는 것과 그 이유

- **등록부 밖의 지표.** 등록부에 없는 `category` 를 가진 행은 채점되지 않는다. 정성
  평가·총괄 행·투찰가 행이 여기 해당한다. 회사 자료로 답할 수 없는 배점을 0점이나
  만점으로 세지 않기 위해 `OUT_OF_SCOPE` 로 빼 두고 정량 총점에서 제외한다.
- **실적 지표의 인정조건.** `PERFORMANCE_AMOUNT`·`PERFORMANCE_COUNT` 는
  `PerformanceRecognitionScope` 없이는 채점되지 않는다. 인정기간·유사범위·발주처
  범위가 원문에서 나와야 하고, 공공부문 범위는 원문에 근거한 발주처 표현까지
  요구한다. 테스트베드는 그 조건을 합성 원문으로 채워 준다.
- **CASE_TABLE 의 부분 등급표.** 신용등급 CASE_TABLE 은 공개된 등급 순서 22종을
  빠짐없이, 그 순서대로, 배점이 내려가는 형태로 덮어야 컴파일된다. 일부 등급만
  적은 표는 그 등급을 가진 회사만 조용히 채점되지 않으므로 거절된다.

## 변경한 파일

운영 코드(`src/`)는 **한 줄도 바꾸지 않았다.** 새 파일 둘만 더한다.

- `tests/quantitative_testbed.py` — 하네스. `METRIC_PROFILES` 등록 표,
  `build_criterion`, `build_company_fact`, `score`.
- `tests/test_quantitative_testbed.py` — 138개 테스트.

## 지표를 늘리는 방법

채점기도 하네스도 복제하지 않는다. `METRIC_PROFILES` 에 등록 한 줄을 더하면 된다.

```python
"PERSONNEL_COUNT": MetricProfile(
    "PERSONNEL_COUNT", "company.personnel.count", "명", "NUMERIC",
    nominal=12, boundary=5, below=4, threshold=5,
    formulas=("BRACKET", "THRESHOLD"),
),
```

`MATRIX` 와 `BANDED` 가 그 줄을 읽어 정상값·경계값·경계 미만·누락값 테스트를 자동으로
만든다. `build_criterion` 은 등록 이름 대신 즉석에서 만든 `MetricProfile` 도 받으므로,
같은 canonical 지표에 다른 단위·다른 배점표를 얹는 행도 코드 변경 없이 채점된다
(`test_a_new_registration_row_is_scored_without_touching_the_harness` 가 억원 단위로
확인한다).

## 성공 사례

- 지표 10종 × 계산식 전 조합에서 정상값은 `CONFIRMED`, 하한과 상한이 한 점으로
  좁혀진다.
- 구간 경계에 정확히 걸친 값은 위 구간에 든다(`5건 이상` 에서 5건은 충족).
  경계 바로 아래 값은 한 구간 내려간다.
- 같은 금액을 `원` 으로 적든 `억원` 으로 적든 같은 점수가 나온다. 환산 없는
  단위(`점`)는 맨숫자로 읽지 않고 거절한다.
- 공개된 신용등급 22종이 모두 채점된다.
- 마감 당일까지 유효한 증빙은 받아들인다.

## 실패 사례 — 모두 fail-closed

증빙이 하나라도 어긋나면 그 행은 `UNSCORABLE` 이 되고, 점수 범위는 `0~배점` 으로
열린 채 남는다. 0점으로도 만점으로도 접지 않는다.

| 결함 | 결과 |
| --- | --- |
| 평가지표 키 불일치 | UNSCORABLE |
| 결속 해시(`fact_binding_sha256`) 불일치 | UNSCORABLE |
| 결속 해시 없음 | UNSCORABLE |
| 회사 사실 미검증 | UNSCORABLE |
| 회사 사실 유효기간 만료 | UNSCORABLE |
| 증빙 유효기간 만료 | UNSCORABLE |
| 증빙 미검증 | UNSCORABLE |
| 마감일 이후 발급된 증빙 | UNSCORABLE |
| 증빙 없음 | UNSCORABLE |
| 변환 불가 단위 | UNSCORABLE |
| 두 평가항목이 결속 해시 하나를 공유 | REVIEW |

한 행이 막혀도 멀쩡한 행은 그대로 확정된다. 다만 총점은 확정되지 않는다. 확정된
행만 보고 총점을 말하면 남은 배점이 사라진 것처럼 보인다.

## 부분 원문(PARTIAL_SOURCE)

배점표 일부만 확보한 상태에서도 확보한 부분의 개별 점수는 확정된다. 그러나 총점은
`REVIEW` 로 남고, 통과 최소점수는 판정하지 않는다.

```
AUTO_ACTIVE     항목 CONFIRMED/CONFIRMED  확정 20.0점  총점 CONFIRMED
PARTIAL_SOURCE  항목 CONFIRMED/CONFIRMED  확정 20.0점  총점 REVIEW
```

## 실행 방법

```bash
python -m pytest tests/test_quantitative_testbed.py -q
```

한 지표만 볼 때:

```bash
python -m pytest tests/test_quantitative_testbed.py -q -k CREDIT_RATING
```

결과: **138 passed** (2026-09-23 실행).

## 운영에 반영하지 않은 것

- 운영 데이터베이스를 읽지도 쓰지도 않았다. 식별자와 증빙은 전부 `SYN` 합성이다.
- 외부 서비스·유료 추출 제공자를 호출하지 않았다.
- 배포와 운영 설정을 바꾸지 않았다. n8n 워크플로도 건드리지 않았다.
- `src/` 아래 파일을 바꾸지 않았으므로 기존 공개 계약과 보안 경계는 그대로다.
