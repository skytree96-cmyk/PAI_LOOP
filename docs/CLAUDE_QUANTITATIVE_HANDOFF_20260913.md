# Claude 교차검토 인수인계: 정량점수가 나오지 않는 원인

2026-09-13. **전체 문제는 미해결이다. 먼저 독립 진단과 수정 설계를 요청한다.** 유료 재추출·배포·운영 변경을 요청하는 문서가 아니다. 사용자에게 전달할 [복사용 프롬프트](CLAUDE_QUANTITATIVE_REVIEW_PROMPT_20260913.md)도 함께 제공한다.

## 1. 읽을 순서와 정확한 검토 기준

1. 이 문서와 [AGENTS.md](../AGENTS.md).
2. [정량 합계·저장·표시 수정 및 남은 차단](QUANTITATIVE_SCOPE_PROGRESS_20260913.md).
3. [유료 호출 없는 원문 재생 조사](QUANTITATIVE_OFFLINE_CORPUS_20260913.md).
4. [9월 9~11일 조사](QUANTITATIVE_SCORE_INVESTIGATION_20260911.md)의 **4장 ‘틀렸던 진단’**. 당시 수치를 현재 통계로 사용하지 않는다.
5. 필요할 때 [원문 재검증 설계](QUANTITATIVE_SOURCE_REVALIDATION_20260913.md)와 아래 코드·테스트를 읽는다. 전체 저장소를 처음부터 요약할 필요는 없다.

인수인계 브랜치: **`docs/claude-quantitative-handoff-0913`**. 최신 검토 코드 기준은 **`d817ce5`**이며 그 뒤 인수인계 문서만 추가했다. 이 브랜치에서 아래 커밋의 코드와 테스트를 모두 읽을 수 있다.

| 커밋/참조 | 내용과 상태 |
|---|---|
| `a615f9a` | 정량 작업의 main 기준점. 현재 원격 main과 같지 않다. |
| `8b82979` | 원문 재검증·제한된 진단 호출. 기존 [초안 PR #154](https://github.com/skytree96-cmyk/PAI_LOOP/pull/154)의 head. |
| `98bc953` | HWPX 동일 문자 노드의 중복 전사 수정, 오프라인 재생 CLI. |
| `d817ce5` | 정량 외 배점의 합산·공개 저장복원·화면 표시 수정. 엔진 1.8.2. |
| `d8a11beeac331b5e15ce32c2b95423c67eeeb6da` | 게시 준비 중 확인한 원격 main. 기준점 이후 낙찰 수집·상단 내비게이션·대시보드 등 14개 커밋이 진행됐다. |

이 브랜치는 **검증 당시의 코드 스냅샷**이다. 최신 main 병합 결과가 아니다. 기존 PR #154를 변경하거나 main을 병합·배포하지 않았다. 후속 구현 시 현재 main을 다시 확인하고 기존 UI·낙찰 수집 변경을 보존해야 한다. 이 스냅샷으로 main 파일을 통째로 덮어쓰지 않는다.

별도 작업 `76d834f`는 수집 키워드에서 ‘위탁 운영·사업기획·회원유치·전사 브랜딩’을 뺀 로컬 커밋이다. 이 인수인계 브랜치에는 포함하지 않았다. 당시 운영 W10의 25개 키워드 적용은 읽기 확인했지만 이번 게시 과정에서 운영 상태를 다시 조회하지 않았으므로 최신 상태로 단정하지 않는다. 해당 운영 변경을 재실행하지 않는다.

## 2. 사용자가 원하는 결과와 작업 제한

- 교육·연수·컨설팅 대상 공고에서 원문에 근거한 **정량점수 / 정량 총배점**을 표시한다. 정성·입찰가격·전체 기술평가 기준과 구분한다.
- 확인되지 않은 실적은 확보 실적으로 세지 않는 보수 계산을 수용했다. 단, 원문 규칙 자체가 미확정인 것을 회사 0점이나 만점으로 바꾸는 뜻은 아니다. 제외 실적과 확인되지 않은 조건은 구분해 표시한다.
- 정량 규칙은 표에만 있지 않다. 문장형 감점·각주·참조 서식도 확인한다. ‘과업지시서’나 ‘정성’ 단어가 있다는 이유로 문서 전체를 버리지 않는다.
- 실제 원문과 저장 응답을 재사용해 먼저 원인과 규모를 측정한다. 유료 추출 호출을 반복하는 방식은 피한다. 이 교차검토에서는 **추가 유료 호출 0회**가 기본이다.
- 운영 DB는 읽기 전용 원칙이며 이번 검토는 GitHub 코드와 공개 가능한 보고로 시작한다. 비밀값·원문·회사 자료를 공개 저장소나 답변에 넣지 않는다.
- 이전의 제한된 유료 실험 승인은 새 호출의 승인이 아니다. main 병합·배포·워크플로 활성화·운영 저장·자동 재시도도 하지 않는다.

## 3. 확인된 수정과 아직 확인되지 않은 효과

### 실제로 수정한 것

**HWPX 파서:** 상위 문단이 자손 셀 문자를 읽은 뒤 셀을 다시 순회해 동일 XML 노드를 중복 전사했다. 노드 소유 문단만 읽게 고쳤다. 서로 다른 셀에 실제로 반복된 문자는 지우지 않는다. 31첨부/14공고에서 503,694 → 412,392자로 줄었다. 별도 33 HWPX의 고유 `hp:t` 비공백 문자 324,571자와 순서를 보존했다. **18.1%는 문자 감소율이며 토큰·비용·정확도 개선율이 아니다.**

**계산·저장·화면:** 기존 `OUT_OF_SCOPE` 행은 준비도 분모에서는 빠지지만 정량 총배점과 상한에는 들어갔다. 엔진 1.8.2는 정량 합계에서 제외하고 원래 배점·사유를 별도 보존한다. 공개 스냅샷의 합계 검사·복원 누락도 고쳤다. 정량 40점 확정 + 별도 60점인 합성 예제는 40/40, 별도 60점이다. 정량 20점이 미검증이면 그 20점은 정량 미확정 범위에 남는다. 전부 범위 외이면 미산정이며, 혼합 평가의 최소점수 충족 여부는 적용 범위가 없으므로 미확정이다.

**고치지 않은 것:** 원문 부모 범위의 구조화·검증, 실제 원문의 잘못된 배점 전사, 모든 회사 인정조건의 완전성. 분류 정규식이나 metric registry를 넓히지 않았고 구계약을 임의로 최신 계약으로 바꾸지 않았다.

### 분모가 다른 측정 결과

| 범위 | 측정 결과 | 해석 한계 |
|---|---|---|
| 당시 전체 930공고 / 2,865첨부 | 현재 저장 계약으로 선택 177첨부, 후보 AVAILABLE 4 / REVIEW 23 | 모든 과거 추출 버전을 합산한 통계가 아님 |
| 기존 19건과 겹치지 않는 신규 30공고 / 163첨부 | 실제 저장 경로 선택 26첨부, 규칙 활성 1공고 / 검토 29공고 | 의도적 층화 표본. 전체 공고 성공률로 일반화 불가 |
| 같은 30공고의 원문+저장 응답 진단 90첨부 | 저장 응답이 정량 묶음을 선언한 25첨부 / 78항목; AVAILABLE 3 / 검토 75 | 원문 전체의 배점 존재율도 운영 활성 수치도 아님 |
| 범위를 원문 대조한 7공고 / 25항목 | 정성 제안 11 + 직원 설문 7 + 가격 7 | 계산 도달 0: 계약/세대 미선택 24, 선택 후 원문·구성 차단 1 |

`d817ce5`의 최종 고정 입력 재생은 **30공고 점수 결과 변화 0개**, 활성 1 / 검토 29 그대로다. 입력 3종과 구현 파일 7개의 해시 일치를 확인했고 외부 호출은 0회였다. 위 수정으로 전체 문제를 해결했다고 주장하면 안 된다.

실제 저장 경로 밖에서도 같은 원문 bytes와 저장 raw를 현재 검증기에 대조할 수 있다. 그러나 이 **진단 결과를 운영 검증 기록이나 회사 점수 입력으로 승격하지 않는다.** 저장 모델 응답이 없는 16시도는 `result=null`이며 HTTP 11·인용 거절 3·schema 거절 1·빈 문서 1이었다. ‘응답 없음’과 ‘응답 오독’을 구분해야 한다.

회사 재생 자료는 식별자·증빙 링크가 일부 생략된 3개 fact 및 1,302개 실적의 부분집합이다. 확정점수 부재를 실제 회사 0점·운영 증빙 전체 부재라고 해석하지 않는다.

## 4. 현재 핵심 가설과 반례

현재 후보 구조는 항목의 **부모 평가범위·평가 단계·소계와의 관계**를 충분히 표현하지 못하고, 누락은 문자열 목록이다. 정성/가격 전용 결함이 정량 표 전체 미완료로 전파되는 구조가 있다. 다만 범위 분리 하나로 비교기호·배점·원문 인용·인정조건 오류까지 해결되지는 않는다. 이 진단을 독립적으로 반박하거나 보완해 달라.

| 원문 검토 사례 | 판단에서 보존할 관계 |
|---|---|
| 홍천군 공직자 맞춤형 역량강화 교육 | 정량 20·정성 60·가격 20. 정성 4항목을 분리할 여지가 있으나 정량 4항목에도 별도 구간·배점·인용 차단이 남음. 참여 인원 ‘명’을 계약 건수 ‘건’이나 회사 직원 수로 바꾸면 안 됨. |
| RISE CREW X RISE HANS 연수, R26BK01726177 | **과업지시서** 10~11쪽에도 신용 정량 10점이 존재. 같은 문서의 유사사업 실적은 정성. ‘실적’이라는 단어로 정량 분류 불가. |
| 한국지역난방공사 김해사업소 구내식당 위탁운영 | 제안평가 정량 10·정성 90, 별도 직원 설문 100과 단계별 80%/20% 가중치. 같은 항목명이 반복됨. 혼합 가감점 2행은 아직 판정 보류. |

이는 이전 작업자의 원문 관찰 보고다. 아래 원문 파일은 Git에 없으므로 Claude가 직접 대조했다고 표현해서는 안 된다. 위탁 운영 키워드 삭제 전 선정된 표본에는 구내식당·주차장 등 비대상 사례도 포함됐다. 이 표본을 앞으로의 대상 분포로 간주하지 않는다.

검토할 설계안은 원문에 연결된 부모 범위·경계·단계·소계를 보존하고, 후보와 누락을 그 범위에 연결하는 것이다. 다음을 모두 검토해 달라.

- 모델이 지정한 ‘정성’ enum만 신뢰하지 않고 동일 첨부의 실제 부모 표제·행 소속·유일한 경계로 증명할 수 있는가?
- 반복 표제, 중첩 표, 다른 ZIP 구성원, 정량/정성 혼합 누락, 별도 가감점에서 잘못 제외되는 반례는 무엇인가?
- 원래 혼합표와 모든 원문 결함은 감사정보로 남기면서, 증명된 정량외 결함만 정량 활성 차단과 분리할 수 있는가?
- 전체 총점에서 제외 배점을 뺀 값을 원문 정량 소계로 꾸미지 않고 원문 소계와 대조하는가?
- 변경 전후에 실제로 몇 항목이 어느 단계까지 이동하는지 어떻게 같은 입력으로 검증할 것인가?

이 설계는 아직 구현하지 않았다. 더 작은 수정이나 다른 근본 원인이 있으면 근거와 함께 제안해 달라.

## 5. 코드·계약·회귀 확인 지점

| 경로 | 핵심 확인 |
|---|---|
| `src/pai_loop/integrations/openai_extraction.py` | `ExtractionPayload`, `QuantitativeRuleCandidate`, strict schema, evidence 순회, correction 구조 불변 검사 |
| `src/pai_loop/source_gap_policy.py` | 실제 누락과 정량 관련성 판정. ‘정성/가격’ 단어가 있다는 이유로 실제 결함을 지우지 않기 |
| `src/pai_loop/quantitative_rule_extraction.py` | `build_quantitative_candidate_profile`, 표 총점/전파, record fingerprint/정확한 계약, manifest merge의 누락 binding |
| `src/pai_loop/extraction_contracts.py` | 정확한 구계약 경계. prompt 0.5.5는 의도적으로 제외됨 |
| `src/pai_loop/quantitative_source_revalidation.py` | 원문/입력 hash와 진단 한계. `DIAGNOSTIC_ONLY`를 승인 증명으로 바꾸지 않기 |
| `src/pai_loop/quantitative_scoring.py` | 동적 profile→request, 합산, `build_public_quantitative_criteria_snapshot`, `public_quantitative_snapshot_projection` |
| `src/pai_loop/analysis_pipeline.py` | 실제 source 선택과 결과 저장. 원문 재생 CLI는 운영 transaction을 호출하지 않음 |
| `src/pai_loop/pps_enrichment.py` | HWPX 노드 소유 및 현재 첨부 추출 경로 |
| `scripts/replay-quantitative-sources.py` | 로컬 고정 입력으로 실제 도메인 단계 재생; network/provider/DB/subprocess 차단 |

현재 계약은 prompt `0.5.7`, schema `0.4.2`, validator `0.6.20`, processing `0.5.2`, candidate profile `0.7.15`, score engine `1.8.2`다. 새 범위 정보를 저장 계약에 넣는다면 증거 순회·correction 불변성·fingerprint·raw 대응·merge 검사를 함께 검토한다. 이전 기록을 새 계약으로 덧씌우지 않는다.

반드시 읽을 금지/회귀 테스트:

`test_source_gap_quantitative_scope.py`, `test_case_award_evidence.py`, `test_quantitative_count_ranges.py`, `test_quantitative_out_of_scope.py`, `test_quantitative_public_snapshot.py`, `test_quantitative_partial_activation.py`, `test_extraction_contract_compatibility.py`, `test_previous_processing_contract.py`, `test_quantitative_source_revalidation.py`, `test_hwpx_paragraph_ownership.py`, `test_quantitative_replay_cli.py`.

특히 비교값을 배점으로 사용, 신용등급 `0`의 숫자 오독, 인용 습관을 표의 열 순서로 간주, 회사 명부가 실제 평가 대상인 사례를 무조건 허위 만점으로 간주, 서로 다른 등급 열의 무차별 합집합을 반복하지 않는다.

## 6. GitHub에서 가능한 검증과 불가능한 검증

코드·합성 회귀는 이 브랜치에서 실행할 수 있다. Python 3.12 및 Node가 필요하다. 기존 365개 집중 검증 명령:

```bash
python -m pip install -e ".[test]"
python -m pytest -o addopts= -q tests/test_quantitative_out_of_scope.py tests/test_quantitative_public_snapshot.py tests/test_quantitative_scoring.py tests/test_quantitative_partial_activation.py tests/test_analysis_pipeline.py tests/test_notice_quantitative_frontend.py tests/test_frontend_public_contract.py tests/test_prespec_frontend.py tests/test_teams_custom_tab.py tests/test_case_award_evidence.py tests/test_source_gap_quantitative_scope.py
```

보고된 검증: `98bc953`에서 집중 995개 + native adapter 22개, `d817ce5`에서 위 365개와 마지막 문구 변경 후 프론트 11개 재검증. PC 1440px·모바일 390px 로컬 Chrome 합성 화면을 확인했다. 서로 다른 커밋의 중복 테스트 수를 합쳐 새로운 전수 통과 건수로 보고하지 않는다. **최신 main 통합 테스트, 새 전체 CI/coverage, 운영 화면 검증은 미수행**이다. 기존 CI는 main push 또는 PR에 반응하므로 이 인수인계 브랜치 게시만으로 CI 통과가 성립하지 않는다.

원문, 저장 raw, DB snapshot, 회사 자료, 브라우저 이미지는 Git에 없다. 로컬 private 자료에는 다음이 있다.

| 자료 이름 | 용도 |
|---|---|
| `cohort_snapshot.private.json`, `source_map_before.private.json`, 회사 snapshot | 동일 입력 재생. GitHub만으로 실제 30건 재현 불가 |
| `scope_totals_final_offline_replay.private.json` / `scope_totals_final_offline_comparison.private.json` | 최종 구현 해시와 수정 전후 비교 |
| `scope_candidate_audit.private.json` | 25행 범위 대조, 누락 84문구 중 원문 확정 4개/나머지 선별 결과 |
| `joint-review/representative_cases.private.json` 및 원문 | 8개 대표 오류와 원문 위치·서식 관계 |

자료가 없으면 **코드로 확인한 사실 / 이전 보고에서 인용한 관찰 / 아직 검증하지 못한 가설**을 구별해 답한다. 필요한 자료는 공고·첨부·페이지·필드 단위로 최소 목록을 요청하고, 회사 자료 전체나 비밀값을 요구하지 않는다. 원문을 추측해 합성 테스트를 만들고 실제 원문 검증으로 부르지 않는다.

## 7. Claude에 요청하는 결과

우선 코드를 변경하지 말고 아래 결과를 간결하게 작성해 달라.

1. 현재 진단의 맞는 부분과 틀리거나 근거가 약한 부분을 코드 위치와 함께 제시.
2. 차단을 입력 확보 / 추출 구조 / 원문 검증 / 회사 증빙 / 합산·저장·표시로 구분. 수치가 없는 원인은 임의로 순위를 매기지 않기.
3. 최소 수정안 1~3개와 각각의 기대 효과, 깨질 수 있는 금지 사례, 검증 방법.
4. 유료 호출 없이 수행할 첫 검증 순서와 GitHub에 없는 최소 추가 자료 목록.

사용자가 이 검토를 Codex와 비교한 뒤 구현 방향을 정할 예정이다. 기존 진단에 동의하는 요약보다 반례·누락·측정 한계를 먼저 지적해 달라.
