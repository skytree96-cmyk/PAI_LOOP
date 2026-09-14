# 정량 계산 경로 독립 검증 및 제한 구현

작성 완료: 2026-09-14 KST. 요청한 파일명 날짜는 유지했다. 기준 코드는 `d817ce5`, 구현 브랜치는 `feat/quantitative-row-level-activation-0913`이다. 기존 원문 범위 구현 worktree는 수정하지 않았다. 이 문서와 구현은 미커밋이다.

**결론: 행별 계산 차단을 분리했고 합성 사례는 통과했다. 그러나 동일한 실제 저장 입력 49공고에서 새로 복구된 공고는 0건이다. 정량점수 문제가 전체적으로 해결됐다는 주장은 할 수 없다.**

## 1. 틀렸거나 범위를 좁혀야 하는 주장부터

아래 코드 위치는 별도 `PAI_LOOP_coverage_verification_0913` worktree의 **d817ce5 기준**이다. 새 구현 위치는 5장에서 따로 적는다. 원문이나 회사 자료를 인용하지 않았다.

| 주장 | 확인 구분 | 반박·확인 및 코드 근거 |
|---|---|---|
| (가) 사람이 만든 curated 경로도 언제나 전 첨부 모델 기록을 요구한다 | 코드로 확인: 일반화 반박 | PPS manifest가 있는 AVAILABLE curated에는 맞다(`src/pai_loop/quantitative_scoring.py:4215`). 하지만 PPS metadata가 없으면 최신 `PUBLIC_DOCUMENT_REFERENCE`의 digest와 curated digest를 비교하는 경로가 있다(`:4237`). MISSING curated 조기 반환(`:4266`)은 점수 활성화 반례가 아니다. |
| (다) 명시적 단일 기준일과 완료 실적 문구가 모두 필수다 | 코드로 확인: 일부 반박 | 기준일 생략은 `UNSPECIFIED`로 보존한다(`src/pai_loop/quantitative_performance.py:328`). 두 기준일 충돌은 거절한다(`:386`). 완료 여부를 명시적으로 무관으로 정한 조건도 허용한다(`:407`). lookback, 유사범위 또는 수동 조건, 해석 가능한 완료 여부 규칙이 필요한 것은 맞다. |
| (라) 재추출 자체가 회사 사실 binding을 바꾼다 | 코드로 확인: 조건부 | 후보 dump·문서 SHA가 바뀌면 바뀐다. 같은 내용이면 유지된다. CASE의 비어 있는 상한 필드는 호환성을 위해 제외한다(`src/pai_loop/quantitative_scoring.py:2540`). 재추출 이벤트 자체는 해시 입력이 아니다. |
| (바) REVIEW는 전부 원문 규칙 미확정이다 | 코드로 확인: 반박 | 회사 증빙 키 불일치나 수동 실적 조건 미증명도 REVIEW가 된다(`src/pai_loop/quantitative_scoring.py:1085`). REVIEW라는 상태만으로 원문 결함 개수를 세면 오염된다. |
| (사) 재무는 항상 ESTIMATED다 | 코드로 확인: 범위 제한 | 성공한 재무 파생값은 ESTIMATED이고, 실패는 REVIEW다(`src/pai_loop/quantitative_financial.py:69`, `:143`). 정확히 결합된 별도 검증 회사 사실이 있으면 파생값보다 먼저 사용하므로 재무 항목 자체가 CONFIRMED 불가능한 것은 아니다(`src/pai_loop/quantitative_scoring.py:4481`). |

맞는 부분은 다음과 같다.

| 주장 | 확인 구분 | 확인 내용 및 코드 근거 |
|---|---|---|
| (나) AVAILABLE 프로필의 행 사유가 전체 계산을 막는다 | 코드로 확인 | 기존 부분 활성은 REVIEW·단일 표에 한정(`src/pai_loop/quantitative_scoring.py:3796`). AVAILABLE의 행 사유 하나라도 남으면 criteria 없이 REVIEW_REQUIRED가 된다(`:3953`). 이 경로에 기존 PARTIAL_ACTIVE 반례는 없었다. 기존 REVIEW 부분 활성까지 없다는 뜻은 아니다. |
| (다) 실적 scope 해석 실패가 다른 정상 행까지 막는다 | 코드로 확인 | 파서가 None이면 `FACT_DIMENSIONS_UNMODELED`가 추가되고 기존 공고 단위 차단으로 이어진다(`src/pai_loop/quantitative_scoring.py:3719`). 합성 raw→검증 record→실제 공고 계산으로 재현했다. |
| (마) 신용 등록은 정확히 한 신용 항목을 요구한다 | 코드로 확인 | `_credit_rating_binding_for_notice`는 동적 프로필에서 신용 항목과 binding이 각각 하나여야 한다(`src/pai_loop/private_company_evidence.py:150`, `:177`). 새 사람 입력에 해시만 달면 기존 등록 API가 바로 호환된다는 뜻은 아니다. |
| (바) 합계가 REVIEW와 UNSCORABLE의 미확정 배점을 합친다 | 코드로 확인 | `unscorable_points`에서 두 상태의 최대점수−하한을 합산한다(`src/pai_loop/quantitative_scoring.py:1414`). 이유의 출처는 별도 판단해야 한다. |
| (사) 재무 파생 지원은 세 비율이다 | 코드로 확인 | 지원 목록은 세 비율이다(`src/pai_loop/quantitative_financial.py:23`). |

따라서 (가)(다)의 과도한 일반화까지 전제하지 않고, 확인된 (나)의 경로에 한해 구현했다. 외부 커버리지 계약이나 실적 인정조건은 완화하지 않았다.

## 2. 실제 19+30공고 측정

**private 자료로 확인.** 아래 수치는 공고별 현재 선택된 저장 record의 프로필을 사용한다. unsupported raw의 독립 재검증 후보를 운영 경로에 합치지 않았다. 이전 19와 고정 30의 notice_key 중복은 0이다.

| 입력 집단 | 공고 | profile AVAILABLE / REVIEW / INCOMPLETE | 커버리지 사유 있음 | AVAILABLE/REVIEW이며 행 사유만 남음 | 행 사유+커버리지 혼재 | available 실적 후보 | scope=None |
|---|---:|---|---:|---:|---:|---:|---:|
| 이전 19 | 19 | 0 / 0 / 19 | 15 | 0 | 0 | 0 | 0 |
| 고정 30 | 30 | 1 / 0 / 29 | 29 | 0 | 0 | 2 | 0 |
| 고유 합계 | 49 | 1 / 0 / 48 | 44 | 0 | 0 | 2 | 0 |

커버리지 열은 `CURRENT_ATTACHMENT_COVERAGE_INCOMPLETE`가 있는 공고 수다. 사유는 겹칠 수 있으므로 첫 차단 계층 통계나 합이 49가 되는 분할표가 아니다. 49건 모두를 대상으로 봐도 행 사유+커버리지 혼재는 0이었다. 앞단에서 가려진 raw 행에 결함이 없다는 뜻은 아니다.

| scope=None 원인 분류 | 이전 19 | 고정 30 |
|---|---:|---:|
| lookback 없음 | 0 | 0 |
| 유사범위/수동 조건 없음 | 0 | 0 |
| 완료 여부 규칙 없음 | 0 | 0 |
| 기준일 충돌 | 0 | 0 |
| 최소금액 힌트만 있음 | 0 | 0 |

원인 표의 0은 성공률 추정이 아니다. 관측 가능한 available 실적 후보가 2개뿐이고 둘 다 파싱됐기 때문이다. **이 입력으로 과거의 실적 차단 179개를 재확인하거나 해당 원인 비율을 추정하는 것은 확인 불가**다.

자료: `.local/calculation-path/d817_{19,30}_stored_path_baseline.private.json`, `scope_{19,30}_stored_path_baseline.private.json`, `final_v2_{19,30}_stored_path.private.json`, `final_comparison_v2.private.json`.

## 3. 입력 보존 및 세 버전 비교

**private 자료로 확인.** 이전 19는 `cohort_keys.json`과 `final_cohort.json`의 435개 버전을 사용했다. 후속 고정 snapshot에서 **동일 ID·파일 SHA·raw 전체가 같은 285개에만 상태 필드**를 보완했다. 구기록 계약·payload를 재도장하지 않았다. 상태가 불명인 나머지 150개는 materialization 129, 과거 metadata 11, 현 manifest 밖 추출 10이다. 이 150개를 제거해도 현재 selector ID와 profile이 19/19 동일함을 확인했다. 과거 시점 전체 DB 상태의 완전 복원은 아니다.

| 코드 | 이전 19 AUTO / PARTIAL / REVIEW_REQUIRED | 고정 30 AUTO / PARTIAL / REVIEW_REQUIRED | 행 사유만 남은 공고 | 기준 대비 점수 변경 |
|---|---|---|---:|---:|
| d817ce5 / engine 1.8.2 | 0 / 0 / 19 | 1 / 0 / 29 | 0 | 기준 |
| 기존 원문 범위 worktree / engine 1.8.3 | 0 / 0 / 19 | 1 / 0 / 29 | 0 | 0 |
| 이번 구현 / engine 1.8.4 | 0 / 0 / 19 | 1 / 0 / 29 | 0 | 0 |

기존 AUTO_ACTIVE 1건의 전체 회사점수 상태는 UNSCORABLE이다. 나머지 48건은 REVIEW다. **새 AUTO_ACTIVE 0, 새 PARTIAL_ACTIVE 0, 이번 변경으로 생긴 AUTO_ACTIVE 오탐 0**이다. 기존 1건이 운영에서 올바른 회사점수라는 의미는 아니다.

각 집단의 snapshot 내용·파일 bytes SHA와 회사 부분 snapshot은 동일하다. selector ID·활성 사유·상태·점수는 49/49 동일하다. d817 대비 profile 자체도 변경 0이다. 원문 범위 worktree와의 profile JSON 차이는 schema version과 빈 `evaluation_scope_audits` 기본 필드뿐이다. 계산 결과의 engine version 이외 내용 차이는 0이다.

이 표본에서 원문 범위 보존과 행별 계산 분리는 서로의 직접 대상 수를 바꾸지 않았다. 보편적으로 무관하다는 증명은 아니다. frozen 회사 subset은 운영 전체 회사 자료의 완전성을 보장하지 않는다.

## 4. 과제 순서에 대한 판단

| 제안 | 판단 | 근거 |
|---|---|---|
| 행 분리를 먼저 하면 다수 공고가 복구된다 | 반박: 이번 자료에서는 기대 효과 0 | 직접 대상 0/49. 최소 계산 경로 수정은 완료했으나 대규모 복구라고 설명할 수 없다. |
| 사람이 검토한 첨부 목록을 manifest 대용으로 사용한다 | 거절 | 목록 일치만으로 바이트·정정·숨은 대체표·평가 단계·가중치가 증명되지 않는다. 모델 기록 필요성과 실제 원문 커버리지 필요성은 다른 문제다. |
| 사람 입력 규칙으로 계산 경로를 만든다 | 조건부 동의, 이번에는 설계만 | 현재 전체 manifest와 원문 SHA, 규칙의 근거, 표 소유·대체/단계 관계를 검증한 별도 authority가 필요하다. 실패한 동적 결과 뒤에 자동 fallback하지 않는다. |
| 측정은 계산·사람 입력 구현 뒤로 미룬다 | 반박 | 측정에서 직접 대상 0이 나왔다. 단계별 분모와 상태를 먼저 유지해야 헛수정·유료 재시도를 줄일 수 있다. 이번 CLI에 포함했다. |
| 이제 대규모 재추출한다 | 근거 부족 | 어떤 앞단 증명을 로컬 바이트로 복구할 수 있는지와 새 추출이 필요한지를 먼저 구분해야 한다. 이번 작업은 비용 효과를 실증하지 않았다. |

다음 구현 우선순위는 세 대표 사례의 **원문 규칙→지원 DSL→회사 사실→항목 점수**를 사람 검증 규칙 설계에 맞춰 연결하는 것이다. 사용자가 이미 제공한 기대값은 다시 요청하지 않는다. 조건부 예상과 확정 사실을 구분하고, 실제 데이터 연결 여부를 별도로 검증한다. 동시에 44건의 커버리지 사유는 앞단 증명의 과제로 남겨 둔다. 사람 확인 선언만으로 이를 지우지 않는다.

## 5. 이번에 구현한 범위

| 변경 | 확인 구분 및 결과 | 새 브랜치 근거 |
|---|---|---|
| 행/공고 사유 분리 | 코드로 확인. 기존 정렬된 진단 코드 목록을 유지한다. 커버리지·source issue·fact ambiguity·논리 프로그램 사유는 공고 차단이다. | `src/pai_loop/quantitative_scoring.py:3657` |
| AVAILABLE의 행 오류 부분 활성 | 코드+SYN 확인. 전체 원래 프로그램을 먼저 검사하고 실패 행만 review criterion으로 투영한다. 정상 행과 binding은 유지, review 최대점수도 분모에 남는다. 원본을 수정하지 않는다. | `src/pai_loop/quantitative_scoring.py:3966`, `:4012` |
| 여러 표의 부분 활성 | 제한 구현. AVAILABLE 전체 프로그램이 이미 증명되고 실패 소유 표가 하나인 경우만 허용한다. 기존 source-REVIEW 경로의 보조표는 명시적 0점·빈 AVAILABLE 표로 한정한다. 미증명 대체표·양수/미상 총점·혼재 인용·최저점·다중 실패 표는 차단한다. | `src/pai_loop/quantitative_scoring.py:3803` |
| 단계별 CLI 통계 | 코드+SYN 확인. 동적 profile/compiled request와 실제 scorer(별도 curated 경로 가능)의 상태를 나눠 기록한다. 합성 raw 재검증 결과를 runtime 성공으로 세지 않는다. | `scripts/replay-quantitative-sources.py:456`, `:473` |
| `--golden` 비교 | 코드+SYN 확인. 항목 status/estimated_points와 명시 소계를 비교한다. 조건부/관측 기대값 분모 분리, null≠0, 절대오차 최대 0.01, 미매칭 실패, 중복·NaN·문자 숫자·미지 공고 거절, 실패 시 결과 저장 후 exit 1. 기대값은 계산 입력이 아니다. | `scripts/replay-quantitative-sources.py:84`, `:155`, `:177`; SYN 형식 `tests/test_quantitative_replay_cli.py:221` |
| 사람 확인 규칙 경로 | 설계 완료, 구현하지 않음. attestation과 기계 원문 증명 분리, 명시 authority, stale/revoked 차단, fact 재결합, 공개 라벨, 대체표 대응 포함. 이미 있는 정확한 로컬 바이트로도 검증 가능하며 재다운로드 필수는 아니다. | `docs/VERIFIED_RULE_INPUT_DESIGN_20260913.md` |

중요한 검증 한계: 빈 AVAILABLE 설문표 양성 테스트는 **소비자 호환 합성 검사**다. 실제 raw→현재 추출 검증기는 빈 criteria를 `TABLE_CRITERIA_MISSING`으로 거절한다. 이 실제 경로의 음성 테스트도 추가했고 유지된다. 따라서 “실제 두 표 공고가 복구됐다”고 해석하면 안 된다. REVIEW 프로필의 비어 있지 않은 추가 표까지 일반적으로 허용하는 확장은 보류했다.

대표 SYN 결과: 정상 신용 CASE 9점 확정, 실적 행은 REVIEW·점수 null·0~6점, 전체 만점 15점, 합계 REVIEW·점수 null·9~15점. 이는 사용자 회사의 실제 점수나 3건의 golden 달성을 뜻하지 않는다. 사용자 3건 private 기대표는 조건부 사실과 해석 이견이 남아 있어 운영 확정값으로 승격하지 않았다. 실제 3건과 새 CLI golden의 완전 일치율은 이번 작업에서 **확인 불가**다.

## 6. 검증 결과 및 남은 한계

| 테스트 묶음 | 결과 |
|---|---:|
| `test_quantitative_row_activation`, `test_quantitative_auto_activation`, `test_quantitative_partial_activation`, `test_quantitative_logical_program` | 79 passed |
| `test_quantitative_out_of_scope`, `test_quantitative_public_snapshot`, `test_analysis_pipeline`, `test_notice_quantitative_frontend`, `test_frontend_public_contract`, `test_case_award_evidence`, `test_quantitative_financial`, `test_quantitative_financial_binding` | 307 passed |
| `test_quantitative_replay_cli` | 44 passed |
| `test_quantitative_scoring`, `test_quantitative_performance_generalized`, `test_quantitative_performance_scenario`, `test_quantitative_review_input`, `test_private_company_evidence`, `test_extraction_contract_compatibility`, `test_quantitative_case_table`, `test_quantitative_rule_extraction` | 584 passed |
| 중복 실행을 제외한 관련 21개 파일 합계 | **1,014 passed** |

Python 3.12.14, pytest 실행. `compileall`과 `git diff --check`도 통과했다. 기존 의존성 deprecation 경고만 있었으며 실패는 없다. 별도 독립 메모리 프로브는 주장 반례 6/6, 결합·전역 차단·불변성 7/7 통과했다(1,014에 합산하지 않음). 반복 실행한 초기 77개와 최종 79개도 중복 합산하지 않았다.

실제 저장 경로 재생은 최종 v2 코드 SHA 7개가 현재 파일과 일치하며 각 guard가 비어 있고 외부 호출 0이다. snapshot 내용·bytes, 원본 payload 불변을 확인했다. 로컬 테스트의 일회용 SQLite 외 운영 DB 접근은 없었다. 유료 호출·다운로드·운영 DB/n8n/Render 접근·push/PR/병합/배포 0회다.

전체 저장소 CI의 PostgreSQL·85% coverage·wheel gate를 통과했다고 주장하지 않는다. 실제 브라우저 PC/모바일 화면을 새로 검증하지 않았고 공개 계약/API 회귀 테스트를 실행했다. UI 파일 변경은 없다. 엔진 버전은 기존 분리 worktree의 1.8.3과 충돌하지 않도록 1.8.4로 올렸다. 원문 범위 변경을 이 브랜치에 통합하거나 최신 main과 재통합하지 않았다.

## 7. 최종 로컬 상태

기준/HEAD: `d817ce5e6157f4cd18b8546bd31f911940240695`. 새 커밋과 PR 없음. 기존 worktree의 미커밋 원문 범위 변경은 그대로 두었다.

`git status --short`:

```text
 M scripts/replay-quantitative-sources.py
 M src/pai_loop/quantitative_scoring.py
 M tests/test_case_award_evidence.py
 M tests/test_quantitative_replay_cli.py
?? docs/CODEX_CALCULATION_PATH_VERIFICATION_20260913.md
?? docs/VERIFIED_RULE_INPUT_DESIGN_20260913.md
?? tests/test_quantitative_row_activation.py
```

`git log --oneline -5`:

```text
d817ce5 Separate quantitative totals from other evaluation points
98bc953 Fix HWPX paragraph ownership and add offline quantitative replay
8b82979 fix: revalidate frozen quantitative sources and bound diagnostic probes
a615f9a Merge pull request #152: Correct the review explanation for unscored quantitative results
309e204 Describe unscored public results without implying a provisional score
```
