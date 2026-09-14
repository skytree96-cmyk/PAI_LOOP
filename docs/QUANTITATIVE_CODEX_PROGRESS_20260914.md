# 정량 계산 후속 구현 — 2026-09-14

대상: `feat/quantitative-newpc-resume-0914`, 시작 커밋 `8009d330ca35d052336d70e6582e336b3dba6dc4`(기준 `d817ce5` 위 1커밋). 별도 작업트리에서 진행하며 main 병합·rebase는 하지 않는다. 이전 작업트리의 미커밋 변경은 보존한다.

## 1. 틀렸거나 좁혀야 하는 주장

| 주장 | 확인 결과 |
|---|---|
| 시작 전 테스트 오류는 제품 코드의 회귀다 | 첫 실행은 임시 디렉터리 상위 경로가 없어서 `tmp_path` 설정 27건이 오류였다. 디렉터리만 준비한 뒤 같은 10파일의 391개가 통과했다. 제품 코드는 변경하지 않았다. |
| 과거 부분 회귀 성공이 전체 테스트 완료를 의미한다 | 전체 4,294개는 별도 직렬 실행 결과로 판정한다. 아래 분모들을 합산하지 않는다. |
| SHA와 literal만으로 첨부 원문 전체를 검증할 수 있다 | 첨부 바이트가 없으므로 실제 포함 관계·전체 조건·소유권은 증명하지 못한다. 과제 D의 `SOURCE_VERIFIED`는 제출 literal의 파싱 단계에 한정하며 기존 원문 증명·엔진 활성 자격으로 사용하지 않는다. |
| B 변경으로 전체 요청의 모든 stale 사실이 REVIEW로 바뀐다 | 개별 `_estimate_criterion`에 전달된 불일치 사실만 해당한다. `estimate_quantitative_score`는 다른 binding의 사실을 현재 항목에 전달하지 않으므로, 그 경우는 여전히 연결 사실 없음이다. 선택 규칙과 기존 Busan wrong-binding 금지 테스트를 유지했다. |

## 2. 확인된 환경과 시작 전 재현

Python 3.12.14 독립 venv에 `python -m pip install -e ".[test]"`로 설치했다. pytest 8.4.2, Pydantic 2.13.5, SQLAlchemy 2.0.52, FastAPI 0.141.1, httpx 0.28.1을 사용한다. 운영 연결정보를 읽지 않았으며 PostgreSQL 테스트 URL은 설정하지 않는다.

아래 명령에서 `pytest`는 이 venv의 `python -m pytest`다. 모든 실행은 직렬이며 `-q`를 추가하지 않는다. 명령에 지정한 로그·JUnit XML은 Git 제외 `.local/test-runs/`에 보존한다(C는 터미널 결과로 확인). `--basetemp`의 상위 디렉터리는 실행 전에 생성한다.

```text
pytest -o addopts="--strict-markers --disable-warnings" -rfE tests/test_quantitative_row_activation.py tests/test_performance_recognition_semantics.py tests/test_performance_scope_binding_revision.py tests/test_quantitative_reference_context.py tests/test_quantitative_replay_cli.py tests/test_case_award_evidence.py tests/test_extraction_contract_compatibility.py tests/test_quantitative_performance_generalized.py tests/test_quantitative_performance_scenario.py tests/test_performance_unmodeled_recognition.py --basetemp=.local/pytest/preflight-ready --junitxml=.local/test-runs/preflight-ready.xml
```

결과: **391/391 통과**, 2 warnings, 3.65초. 첫 환경 오류 실행은 364 통과/27 오류였으며 위 성공 수에 합산하지 않는다.

## 3. A — 전체 직렬 테스트

```text
pytest -o addopts="--strict-markers --disable-warnings" -rfE tests --basetemp=.local/pytest/a-full-serial --junitxml=.local/test-runs/a-full-serial.xml
```

결과: **4,294개 수집 = 4,263 통과 / 31 skip / 0 실패 / 0 오류**, 6 warnings, 966.42초. xdist 없이 끝까지 종료했으며 exit code는 0이다. JUnit XML의 failure/error ID 목록은 각각 빈 목록이다.

| PostgreSQL 제외 파일 | skip 수 | 사유 |
|---|---:|---|
| `tests/test_analysis_execution.py` | 8 | `disposable PostgreSQL URL is not configured` |
| `tests/test_postgres_department_accounts.py` | 21 | 동일 |
| `tests/test_postgres_long_output_once.py` | 2 | 동일 |

이번 직렬 실행에서는 실패가 재현되지 않아 `d817ce5`에 대조할 실패 ID가 없다. 따라서 기준 커밋의 전체 테스트를 재실행하지 않았다. 이전 PC의 ID 미확보 실패와 worker node down 원인은 **확인 불가**이며, 이를 브랜치 회귀 또는 환경 문제로 단정하지 않는다. 이번 결과는 엔진 1.8.6·실적 알고리즘 0.4.1·실적 binding 계약이 있는 시작 커밋의 로컬 직렬 결과다. PostgreSQL 동작의 통과를 의미하지 않는다.

## 4. B — generic/stale 결합 라벨

`src/pai_loop/quantitative_scoring.py`의 `_estimate_criterion`에서 조건 결합 정보가 없는 generic 사실은 **UNSCORABLE**, 값은 있지만 현재 항목과 다른 사실은 **REVIEW**로 분리했다. 각각 결합 정보 부재와 현재 조건 재확인 필요를 설명한다. 점수 산식·사실 선택·증빙 guard·버전은 변경하지 않았다.

SYN은 두 상태 모두 `estimated_points=None`, 기존 하한/상한·confidence·증빙 식별정보 보존을 확인한다. 명시된 원문 최소점이 있는 경우도 비교하며, binding이 맞을 때의 기존 5점과 다른 binding을 현재 항목에 연결하지 않는 요청 경로를 고정한다. `None`은 확인된 0점으로 바꾸지 않는다.

```text
pytest -o addopts="--strict-markers --disable-warnings" -rfE tests/test_quantitative_fact_binding_labels.py --basetemp=.local/pytest/b-syn --junitxml=.local/test-runs/b-syn.xml
```

결과: **6/6 통과**, 2 warnings, 0.13초.

```text
pytest -o addopts="--strict-markers --disable-warnings" -rfE tests/test_quantitative_discrete_brackets.py tests/test_quantitative_financial_binding.py tests/test_busan_education_quantitative_e2e.py tests/test_quantitative_auto_activation.py tests/test_performance_manual_fact_guard.py tests/test_quantitative_scoring.py tests/test_quantitative_mainpage_refresh.py --basetemp=.local/pytest/b-regression --junitxml=.local/test-runs/b-regression.xml
```

결과: **163/163 통과**, 2 warnings, 15.22초. 앞의 SYN 및 A/시작 전 분모에 합산하지 않는다.

## 5. C — 영역 종료 진단

`quantitative_rule_extraction.py`에 저장 모델과 분리된 `QuantitativeRegionEndDiagnostic`과 순수 관측 함수 `diagnose_quantitative_region_ends(payload, source=..., attachment_id=...)`를 추가했다. 반환은 immutable 진단 tuple이며 원문 내용 없이 단계·표/항목 index·영역·종료 위치·종료 원인을 담는다. 위치는 QRE 내부의 0-based 문단 index이며 native 바이트/문자 오프셋이나 PDF 페이지가 아니다.

`end_status`는 실제 구조 경계가 관측되면 **STRUCTURAL_BOUNDARY**, EOF·64줄 상한·빈줄뿐이면 **UNPROVEN**, 유효 영역 자체가 없으면 **UNRESOLVED**다. 경계와 상한이 같은 위치이면 두 원인을 모두 남긴다. 64줄 상한은 임시 헤더 후보에 적용되며 최종 항목/표 영역에 새 상한을 적용하지 않는다. 표 전체와 표 핵심 영역의 종료 사유도 따로 보존한다.

기존 HWP 재결합 경로가 실제 방문한 후보만 진단하므로 비-HWP 또는 표가 없는 입력은 빈 tuple일 수 있다. 빈 진단은 종료 증명 성공을 뜻하지 않는다. STRUCTURAL_BOUNDARY 역시 표 전체의 완결·소유권·참조조건·커버리지 또는 점수 활성 증명이 아니다.

기본 호출은 collector를 켜지 않는다. 기존 region 선택·payload·2-tuple 반환·record schema·fingerprint·추출 계약을 유지하며 `quantitative_reference_context`는 자동 파이프라인에 연결하지 않았다. SYN에서는 C 직전 `f0d4096`의 동일 입력 record JSON SHA와 validation fingerprint도 고정하여 비교한다.

```text
pytest tests/test_quantitative_region_end_diagnostics.py tests/test_quantitative_rule_extraction.py tests/test_extraction_contract_compatibility.py -o addopts="--strict-markers --disable-warnings" -rfE --basetemp=.local/pytest/region-end-final
```

결과: 이 명령으로 수집한 **468/468 통과**, 3 warnings, 9.84초. 새 SYN 23개가 이 실행에 포함되어 있다. 기존 표제·범위 guard 및 계약 호환 테스트도 유지했다. 다른 테스트 실행의 통과 수와 합산하지 않는다.

## 6. D — 사람 확인 실적 literal의 순수 검토

새 `src/pai_loop/verified_rule_input.py`에 `VerifiedRuleDraft`와 `verify_rule_draft`를 구현했다. 입력은 `attachment_sha256`(64자리 소문자 hex), `literal`(최대 2,000자), `metric_key`(`company.performance.count` 또는 `company.performance.amount`)뿐이다. scope·상태·회사 값·binding 직접 입력은 거절한다.

기존 `parse_performance_recognition_scope`가 성공하고 수동조건이 없으면 **SOURCE_VERIFIED**, 파싱 실패 또는 수동조건이 남으면 **ATTESTED_ONLY**를 반환한다. 실패 사유는 각각 `LITERAL_PARSE_UNSUPPORTED`와 `MANUAL_CONDITIONS_REMAIN`이다. 원형 literal과 파서가 정규화한 scope는 구분해 보존하고, 빈 문자열·자료 없음은 scope가 없는 ATTESTED_ONLY로 남긴다. 숫자 0·실적 건수·점수·회사 사실·엔진 binding을 생성하지 않는다.

이 단계의 상태명은 사용자 요청에 따른 제한된 이름이다. `verification_scope=SUPPLIED_LITERAL_PARSE_ONLY`이며 `source_content_verified`, `coverage_verified`, `engine_eligible`, `persistence_eligible`는 모두 **False**로 고정한다. 첨부 SHA는 형식만 확인하며 실제 첨부 바이트와 인용의 일치·전체 인정조건은 확인하지 않는다. ATTESTED_ONLY 역시 검토자 신원이나 실제 승인 이벤트를 증명하지 않는다. 기존 설계의 완전한 SOURCE_VERIFIED 요건을 대체하지 않는다.

입력·결과는 immutable/strict/extra-forbid이며 결과를 역직렬화할 때도 현재 파서 결과와 대조한다. 검증 함수와 역직렬화 경로에서 반환 scope나 상태만 주입해 통과할 수 없다. 기존 parser·API·DB·엔진 계약과 저장·선택 경로는 변경하지 않았다.

```text
pytest -o addopts="--strict-markers --disable-warnings" -rfE --junitxml=.local/test-runs/d-focused.xml tests/test_verified_rule_input.py
```

결과: **50/50 통과**, 2 warnings, 0.21초.

```text
pytest -o addopts="--strict-markers --disable-warnings" -rfE --junitxml=.local/test-runs/d-related.xml tests/test_quantitative_performance_generalized.py tests/test_performance_recognition_semantics.py tests/test_performance_unmodeled_recognition.py tests/test_performance_manual_conditions.py tests/test_performance_scope_binding_revision.py
```

결과: **106/106 통과**, 2 warnings, 0.71초. 위 신규 테스트 실행이나 다른 과제의 분모와 합산하지 않는다.

## 7. 실제 자료·운영 범위

private 자료를 사용하지 않는다. 49공고 재생·조건부 golden은 이번 작업에서 **확인 불가**다. 이전 기록의 신규 복구 0건을 삭제하거나 다른 수치로 대체하지 않는다.

유료 모델 호출, 나라장터 다운로드, 운영 DB/n8n/Render 접근, 회사 자료 인용, 비밀값 출력, main 병합·배포·워크플로 활성화는 하지 않는다. 과제별 커밋과 지정 작업 브랜치 push만 허용 범위로 사용한다.

## 8. 다음 단계와 코드 위치

1. 로컬 disposable PostgreSQL을 준비할 수 있는 환경에서 제외된 31개를 검증한다. 운영 DB는 사용하지 않는다. 이전 병렬 worker 종료의 원인은 당시 실패 ID/로그 없이는 확정하지 않는다.
2. C의 종료 진단을 참조 모듈에 사용할 정책과 별도의 소속·전체 조건 검증을 설계한다. 현재 관측된 경계만으로 자동 연결을 허용하지 않는다.
3. D 다음 단계는 로컬 native 바이트·현재 manifest·literal 포함 관계·범위/소유권을 검증하는 순수 계층이다. 이후 별도의 명시 선택과 회사 사실 결합이 있어야 계산 입력을 만들 수 있다. 이번 함수만으로 기존 커버리지 차단을 해제하지 않는다.
4. private 자료가 있는 PC에서 기존 49공고 재생과 조건부 golden을 원래 입력 그대로 대조한다. 이번 SYN 수치를 실제 공고 복구 수로 사용하지 않는다.

| 구현/한계 | 근거 |
|---|---|
| generic/stale 직접 계산 라벨 | `src/pai_loop/quantitative_scoring.py:1092` |
| 다른 binding 사실을 현재 항목에서 제외 | `src/pai_loop/quantitative_scoring.py:1318` 이후 사실 선택 |
| 종료 진단 DTO와 상태 | `src/pai_loop/quantitative_rule_extraction.py:326` |
| 저장 기록을 만들지 않는 관측 함수 | `src/pai_loop/quantitative_rule_extraction.py:6003` |
| 실적 입력 제한 | `src/pai_loop/verified_rule_input.py:31` |
| 파싱 범위·권한 제한 및 결과 재검증 | `src/pai_loop/verified_rule_input.py:53` |
| 순수 실적 draft 검토 함수 | `src/pai_loop/verified_rule_input.py:96` |

## 9. 변경 후 최종 관련 회귀

```text
pytest -o addopts="--strict-markers --disable-warnings" -rfE tests/test_quantitative_row_activation.py tests/test_performance_recognition_semantics.py tests/test_performance_scope_binding_revision.py tests/test_quantitative_reference_context.py tests/test_quantitative_replay_cli.py tests/test_case_award_evidence.py tests/test_extraction_contract_compatibility.py tests/test_quantitative_performance_generalized.py tests/test_quantitative_performance_scenario.py tests/test_performance_unmodeled_recognition.py tests/test_quantitative_fact_binding_labels.py tests/test_quantitative_region_end_diagnostics.py tests/test_verified_rule_input.py tests/test_pps_enrichment.py tests/test_quantitative_source_revalidation.py --basetemp=.local/pytest/final-regression --junitxml=.local/test-runs/final-regression.xml
```

결과: **577/577 통과**, 3 warnings, 9.81초. 시작 전 필수 10파일, 새 SYN, 보강·저장 기록 재검증 경로를 같은 실행에서 확인했다. 네트워크/운영에 연결한 테스트가 아니다. 앞의 각 실행과 중복되므로 통과 수를 합산하지 않는다. A의 전체 4,294개 결과는 수정 전 기준이며, 수정 후 전체 테스트를 다시 완료했다는 뜻은 아니다.

추가 확인: `python -m compileall -q src tests tools`, `git diff --cached --check` 통과. `.github/workflows/ci.yml`의 **Verify public-release boundary**, **Scan tracked files for secrets and source artifacts** 두 Python 검사를 로컬에서 그대로 실행해 통과했으며 scan 대상은 추적 파일 434개였다. 이는 원격 CI·PostgreSQL·coverage 85% 게이트의 실행 결과가 아니다.

과제별 커밋: A `8750511`, B `f0d4096`, C `89b2a5c`, D는 새 실적 draft 모듈·SYN·본 최종 보고서를 함께 담는 후속 커밋이다. 시작 커밋의 조상에 main의 후속 award 변경을 합치거나 rebase하지 않았다. 지정 작업 브랜치만 push 대상으로 삼으며 PR 생성·main 병합·배포·워크플로 활성화는 수행하지 않는다.

## 10. 이후 사용자 승인에 따른 실제 입력 대조·연결

위 A–D 당시의 범위와 결과는 그대로 보존한다. 이후 사용자가 실제 입력 대조와 연결을 요청해 로컬 private 입력을 읽었고, [실제 입력 대조](QUANTITATIVE_ACTUAL_INPUT_CHECK_20260914.md) 및 [원문·회사 입력 연결](QUANTITATIVE_CONNECTION_PROGRESS_20260914.md)에 별도로 기록했다.

공통 회사자료 resolver 연결과 generator 소진 수정, 비저장 native/raw 검증 preview 및 로컬 CLI를 구현했다. 새 3파일 회귀 51/51, 관련 회귀 1,836/1,836은 별도 실행 분모다. 49건 저장 결과는 변경 전과 동일하며 새 총점 복구 0건이다. native 입력으로 검증 가능한 30건 중 18건의 별도 preview에서도 신규 총점 복구는 없었다. SYN·preview를 실제 운영 복구로 대체하지 않는다. 운영 접근·유료 호출·배포는 수행하지 않았다.
