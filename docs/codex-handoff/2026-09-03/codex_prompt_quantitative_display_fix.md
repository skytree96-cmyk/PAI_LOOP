# Codex 작업 지시서 — 정량점수 "표출" 로직 (Phase 5, 산정 수정과 함께 반드시 확인)

## 이 문서의 위치

`codex_prompt_quantitative_scoring_fix.md`(Phase 1~4, 이미 실행 중)에 대한
**추가 Phase 5**다. Phase 1~4는 "산정"(계산 엔진)을 고치는 내용이고, 이 문서는
2026-09-03 재조사에서 별도로 확인한 "표출"(화면에 실제로 어떻게 보이는지) 관련
내용을 담는다. **Phase 1~4와 독립적으로 읽어도 되지만, 배포 순서는 서로 영향을
준다** — 아래 참고.

## 결론 먼저

**표출 코드(`app.js`의 `renderQuantitativeEstimate`) 자체는 새로 고칠 필요가
없다.** 실제로 열어서 확인한 결과, 이 함수는 백엔드가 내려주는
`rule_source_status`/`source_validation_status`/`activation_status`/
`activation_reasons`/`readiness_band`/`total_max_points` 등을 있는 그대로
성실하게 반영하도록 짜여 있다(`activation_reasons`를 사람이 읽을 한국어 문구로
매핑하는 `activationReasonLabels` 딕셔너리까지 20개 넘는 사유 코드가 이미 다
커버되어 있음, `app.js` 5014-5037행). 즉 **Phase 1~4가 계산 엔진을 제대로
고치면, 표출 화면은 별도 프론트엔드 수정 없이도 자동으로 올바르게 바뀐다.**

다만 이 성실한 반영 방식 때문에 생기는 **표출 계층 고유의 리스크 2가지**를
찾았다. 둘 다 "계산은 맞는데 화면엔 안 보이거나, 이상하게 보이는" 상황을
만들 수 있다.

---

## 발견 1 — 공개 화면(public view)은 캐시 미스 시 "빈 회사정보로 즉석 계산"으로 조용히 대체된다

### 코드 근거

`quantitative_scoring.py`의 `GET /notices/{notice_key}/quantitative-estimate`
핸들러(`get_notice_quantitative_estimate`, 4543행)의 실제 흐름:

```python
if public_view:
    stored_public_result = _stored_public_quantitative_projection(session, notice)
    if stored_public_result is not None:
        return stored_public_result
company_facts = [] if public_view else list(...)          # public이면 무조건 빈 리스트
performance_records = [] if public_view else list(...)     # public이면 무조건 빈 리스트
result = estimate_for_notice(notice, company_facts, performance_records)
return _public_quantitative_projection(result) if public_view else result
```

`_stored_public_quantitative_projection`(4235행)은 저장된 최신 분석의 점수
스냅샷을 돌려주는데, 다음 조건 중 하나라도 걸리면 `None`을 반환한다(4235-4297행
확인):

- 해당 공고에 `AnalysisRun`이 아직 없음
- `ScoreSnapshot`이 정확히 1개가 아님(0개 또는 여러 개)
- **`score.method_version != QUANTITATIVE_ENGINE_VERSION`** (현재
  `"pai-loop-quantitative-engine-1.7.0"`, `quantitative_scoring.py:70`)

세 번째 조건이 핵심이다. `None`이 반환되면 위 코드는 **캐시가 없다고 사람에게
알리는 대신, `company_facts=[]`로 그 자리에서 바로 재계산**한다. 회사 사실이
하나도 없으니 `estimate_for_notice`(3884행)의 `resolve_verified_quantitative_facts`가
아무것도 매칭하지 못하고, 결과는 사실상 전부 `REVIEW_REQUIRED`/미산정으로
나온다(크래시는 안 남 — `estimate_for_notice`가 fail-closed로 설계되어 있음을
확인함). 즉 **직전까지 정상적으로 점수 범위가 보이던 공고가, 순전히 엔진 버전
문자열이 바뀌었다는 이유만으로 "미산정"으로 보이게 될 수 있다.** 사람 입장에서는
"방금 배포했더니 화면이 오히려 나빠졌다"로 보인다.

### 왜 지금 이 시점에 중요한가

Phase 1~4 작업 중 계산 로직 자체(`estimate_quantitative_score`,
`resolve_performance_register_facts`, `derive_performance_value` 등)를 건드리는
변경이 있다면, `QUANTITATIVE_ENGINE_VERSION` 상수를 올릴지 말지 판단해야 하는
순간이 온다. **이 버전을 올리는 순간, 그동안 쌓인 모든 공개 화면 캐시가
한꺼번에 무효화되고 위 경로를 탄다.** (참고: Phase 1의 PR #69는
`QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION`이라는 **별개의 상수**를 쓰므로 이
문제와 무관하다 — 두 버전 상수를 혼동하지 말 것.)

### 요구되는 수정

1. `_stored_public_quantitative_projection`이 `None`을 반환하는 경우,
   `get_notice_quantitative_estimate`가 곧바로 "빈 회사정보로 즉석 계산"하지
   않도록 분기를 추가한다. 대안(택1, Codex가 코드베이스 관례에 맞는 쪽으로 결정):
   - (a) `method_version` 불일치를 이유로 캐시를 버릴 때, 완전히 새 캐시가
     생성되기 전까지는 **이전 캐시(버전 불일치 감수하고)를 그대로 보여주되
     "엔진 갱신, 재분석 대기 중" 같은 경고 문구를 얹어 반환**하거나,
   - (b) 명시적으로 "이 공고는 최신 엔진으로 아직 재분석되지 않았습니다"라는
     `rule_source_status="INCOMPLETE"` 계열의 안내형 응답을 반환하고, 절대
     `company_facts=[]`로 즉석 계산하지 않는다.
   - **어느 쪽이든, public_view 경로에서 회사 데이터 없이 즉석 계산해서 그
     결과를 마치 정상 산정인 것처럼 내려주는 지금의 동작은 없앤다.**
2. `QUANTITATIVE_ENGINE_VERSION`을 실제로 올리기로 결정한 시점에는, 그 배포
   직후 캐시가 대량으로 깨지는 것을 감안해 **영향받는 공고들을
   `recompute_current=true`(LLM 0회 호출)로 일괄 재계산하는 절차**를 배포
   체크리스트에 명시한다(원본 감사 문서 8번 질문과 동일한 메커니즘 재사용).
3. 이 분기에 대한 테스트를 추가한다: 저장된 스냅샷의 `method_version`이 현재
   상수와 다를 때 `get_notice_quantitative_estimate`가 (a) 크래시하지 않고,
   (b) 빈 회사정보로 조용히 계산한 결과를 정상 결과인 것처럼 반환하지 않는지
   확인.

---

## 발견 2 — "산정"과 "표출"이 실제로 다시 연결되려면 재계산이 실행되어야 한다

이건 버그라기보다 **배포 순서 문제**다. Phase 1~4가 계산 로직을 아무리 정확히
고쳐도, 이미 저장되어 있는 기존 공고들의 `ScoreSnapshot`/`AnalysisRun`은 저절로
다시 계산되지 않는다. 화면(표출)에 새 결과가 뜨려면 해당 공고가 실제로
재분석되어야 한다.

### 요구되는 작업

1. Phase 1~4 배포가 끝나면, 영향받은 공고 표본(최소한 이 전체 작업의 출발점이었던
   `PPS-R26BK01704704-000-d3a27e912c`와 부산교육한마당 공고 포함)에 대해
   `recompute_current=true`로 재계산을 실행하고, **`/notices/{key}/quantitative-estimate`
   응답이 실제로 바뀌었는지 직접 호출해 확인**한다. 코드가 맞다고 주장하는 것과
   화면에 실제로 반영되는 것은 다른 문제이므로 이 확인 없이는 작업이 끝난 게
   아니다.
2. 이 확인 결과(재계산 전/후 응답 diff)를 handoff 문서에 남긴다.

---

## 공통 요구사항

- 발견 1의 수정은 별도 PR로 분리한다(계산 로직 Phase 1~4와 섞지 말 것 — 표출
  계층 변경이라 리뷰 관점이 다르다).
- `pytest tests/` 전체 통과 확인.
- 이 문서에서 다루는 코드(`quantitative_scoring.py`의
  `get_notice_quantitative_estimate`/`_stored_public_quantitative_projection`,
  `app.js`의 `renderQuantitativeEstimate` 부근)는 이번 재조사에서 실제로 열어
  확인한 것이며, 줄번호는 2026-09-03 `main`(커밋 `0c07820`, PR #70 병합 직후)
  기준이다. Codex가 작업을 시작하는 시점에 Phase 1~4가 이미 일부 병합되어
  있다면 줄번호가 달라질 수 있으니 grep으로 재확인할 것(자격요건/정량 프롬프트
  양쪽에서 반복적으로 발생한 문제이므로 습관화할 것).
