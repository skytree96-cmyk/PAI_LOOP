# 정량 계산 후속 구현 — 2026-09-14

대상: `feat/quantitative-newpc-resume-0914`, 시작 커밋 `8009d330ca35d052336d70e6582e336b3dba6dc4`(기준 `d817ce5` 위 1커밋). 별도 작업트리에서 진행하며 main 병합·rebase는 하지 않는다. 이전 작업트리의 미커밋 변경은 보존한다.

## 1. 틀렸거나 좁혀야 하는 주장

| 주장 | 확인 결과 |
|---|---|
| 시작 전 테스트 오류는 제품 코드의 회귀다 | 첫 실행은 임시 디렉터리 상위 경로가 없어서 `tmp_path` 설정 27건이 오류였다. 디렉터리만 준비한 뒤 같은 10파일의 391개가 통과했다. 제품 코드는 변경하지 않았다. |
| 과거 부분 회귀 성공이 전체 테스트 완료를 의미한다 | 전체 4,294개는 별도 직렬 실행 결과로 판정한다. 아래 분모들을 합산하지 않는다. |
| SHA와 literal만으로 첨부 원문 전체를 검증할 수 있다 | 첨부 바이트가 없으므로 실제 포함 관계·전체 조건·소유권은 증명하지 못한다. 과제 D의 `SOURCE_VERIFIED`는 제출 literal의 파싱 단계에 한정하며 기존 원문 증명·엔진 활성 자격으로 사용하지 않는다. |

## 2. 확인된 환경과 시작 전 재현

Python 3.12.14 독립 venv에 `python -m pip install -e ".[test]"`로 설치했다. pytest 8.4.2, Pydantic 2.13.5, SQLAlchemy 2.0.52, FastAPI 0.141.1, httpx 0.28.1을 사용한다. 운영 연결정보를 읽지 않았으며 PostgreSQL 테스트 URL은 설정하지 않는다.

아래 명령에서 `pytest`는 이 venv의 `python -m pytest`다. 모든 실행은 직렬이며 `-q`를 추가하지 않는다. 로그·JUnit XML은 Git 제외 `.local/test-runs/`에 보존한다. `--basetemp`의 상위 디렉터리는 실행 전에 생성한다.

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

아직 구현하지 않았다. A 완료 후 상태·문구만 분리하고 점수 값 불변을 SYN으로 확인한다.

## 5. C — 영역 종료 진단

아직 구현하지 않았다. EOF/64줄 상한과 실제 경계의 선택 이유를 별도 진단으로 남기고 기존 반환값·fingerprint·추출 계약·저장 기록을 보존한다. 참조 모듈은 자동 파이프라인에 연결하지 않는다.

## 6. D — 사람 확인 실적 literal의 순수 검토

아직 구현하지 않았다. 첨부 SHA·literal·metric만 받고 scope 직접 입력을 허용하지 않는다. 성공 상태를 원문 전체/회사 증빙/점수 승인으로 해석하지 않도록 결과 범위를 명시한다.

## 7. 실제 자료·운영 범위

private 자료를 사용하지 않는다. 49공고 재생·조건부 golden은 이번 작업에서 **확인 불가**다. 이전 기록의 신규 복구 0건을 삭제하거나 다른 수치로 대체하지 않는다.

유료 모델 호출, 나라장터 다운로드, 운영 DB/n8n/Render 접근, 회사 자료 인용, 비밀값 출력, main 병합·배포·워크플로 활성화는 하지 않는다. 과제별 커밋과 지정 작업 브랜치 push만 허용 범위로 사용한다.
