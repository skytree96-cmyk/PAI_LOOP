# 원문 규칙과 회사 입력 연결 — 2026-09-14

대상: `feat/quantitative-newpc-resume-0914`, 시작 `5270804`. 운영 접근 없이 로컬 구현·검증했다. main을 병합하거나 rebase하지 않는다.

## 1. 먼저 정정할 해석

| 해석 | 확인 결과 |
|---|---|
| 기존 자동 계산에는 회사자료 연결이 없었다 | 자동 경로는 이미 신용·실적·재무·인력 resolver를 호출했다. 별도 원문 진단이 같은 병합 코드를 복제하고 있었으며, 이번에는 공통 함수로 연결했다. |
| 스냅샷에 증빙 ID만 채우면 실제 점수가 복구된다 | 원본 ID 외에도 의미 payload의 일부 필드가 빠져 있다. 사실 3개 모두 identity/link가 없고, 저장 payload digest가 있는 2개도 현재 부분 스냅샷 재구성과 0/2 일치한다. 원래 필드·ID·해시를 추정하거나 새 해시로 덮지 않았다. |
| 원문과 인용이 맞으면 운영 점수로 승인할 수 있다 | 새 연결은 로컬 preview다. 원문 누락·소유권·전체 조건·검토 승인까지 증명하지 않으며 `production_eligible`, `persistence_eligible`, `source_coverage_verified`는 모두 false다. |
| HWP는 현재 코드에서도 전부 미지원이다 | 현재 shared document reader에는 bounded HWP parser가 있다. 확장자만 보고 일괄 차단하지 않고 실제 parser 실패·불완전성을 기준으로 처리한다. 과거 문서의 설명을 현재 동작으로 재사용하지 않는다. |

실제 계산 수치와 SYN 성공을 구분한다. 이전 49공고의 신규 총점 복구 0건 결론은 그대로 유지한다.

## 2. 구현한 연결

`quantitative_scoring.py:4564`의 `bind_quantitative_company_inputs`는 이미 검증·선택된 요청에 기존 회사 resolver 결과를 연결한다. `estimate_for_notice`의 자동 경로와 새 로컬 preview가 동일한 함수를 사용한다. 활성 상태가 AUTO/PARTIAL일 때만 회사자료를 읽고, 요청에 직접 넣은 계산용 사실은 실제 resolver 결과로 교체한다.

회사자료를 tuple로 한 번 고정해 모든 resolver가 동일 입력을 보도록 했다. 이전에는 `Iterable`로 받은 generator가 첫 resolver에서 소진되어 재무·인력 계산 입력이 사라질 수 있었다. 정확한 binding의 CONFIRMED 사실 우선순위, 나머지 원장 파생, 수동조건 REVIEW, 부분 활성의 미확정 배점 분모는 유지했다. 이 수정 커밋은 `454af28`이다.

`quantitative_review_preview.py:113`의 새 입력 경로는 다음 순서로 동작한다.

1. 전체 고정 manifest와 기대 문서 SHA의 집합을 비교하고 중복·누락·외부 첨부를 거절한다.
2. 원본 native 바이트 SHA와 검토 대상으로 고정한 raw JSON SHA를 각각 확인한다. raw를 현재 `ExtractionPayload`로 다시 검증하므로 저장 record·실행 요청·직접 scope·점수·binding 입력을 원문 증명으로 받지 않는다.
3. 기존 고정 native parser로 문서를 읽고, 기존 QRE의 인용·배점·단위·누락 선언 검증과 전체 첨부 병합을 실행한다. 순간적인 새 검증 record는 메모리에서만 쓰며 저장하거나 결과에 노출하지 않는다. 기존 추출 기록을 최신 계약으로 바꾸지 않는다.
4. 기존 활성/부분 활성 compiler를 거쳐 공통 회사자료 연결과 기존 점수 엔진을 실행한다. 미지원 규칙과 부족한 증빙은 그대로 남긴다.

PDF reader의 `complete=True`만으로 모든 페이지의 텍스트를 읽었다고 판단하지 않는다. 새 preview에서 텍스트 없는 페이지는 `PDF_PAGE_TEXT_UNVERIFIED`로 차단한다. ZIP 내부의 동일 페이지 증명은 구현되지 않아 ZIP preview는 `NATIVE_MEMBER_TEXT_COVERAGE_UNVERIFIED`로 남긴다. shared parser·QRE fingerprint·기존 계약은 변경하지 않았다. 이 감사도 이미지와 텍스트가 한 페이지에 섞인 모든 누락을 검출한다는 뜻은 아니다.

## 3. 로컬 실행 진입점

`scripts/preview-reviewed-quantitative.py`는 strict `quantitative-review-preview-1` 계획을 받는다. 계획에는 선택 공고 key, 전체 manifest SHA, 첨부별 native SHA, native/raw 파일 경로와 raw SHA만 있다. 회사자료는 별도 frozen snapshot으로 받는다. 실제 마감일·공고일·최신 manifest는 지정 공고 snapshot에서 읽으며 오늘 날짜나 합성 manifest를 만들지 않는다.

```text
python scripts/preview-reviewed-quantitative.py --snapshot LOCAL_NOTICE_SNAPSHOT --company-snapshot LOCAL_COMPANY_SNAPSHOT --review-plan LOCAL_REVIEW_PLAN --output .local/connection-check-20260914/new-preview.private.json
```

SYN 계획 구조는 `tests/test_quantitative_review_preview_cli.py`에 있다. 입력은 모두 로컬 경로이고 출력은 저장소 `.local` 아래 새 파일만 허용한다. 기존 파일 덮어쓰기, URL·UNC 입력, 중복 공고/version, 최신 manifest 불일치, 실행 중 입력 변경은 거절한다. socket·HTTP·provider·DB·subprocess를 guard로 차단한다.

결과의 `local_review_preview`와 `actual_stored_estimate`를 구분한다. preview가 계산돼도 기존 저장 규칙을 승인하거나 대체하지 않는다. 출력에는 원문 anchor와 회사 증빙 설명이 들어갈 수 있으므로 Git/public API에 넣지 않는다. 콘솔에는 집계와 비식별 오류 코드만 출력한다.

## 4. 검증 분모

각 행은 별도 실행이며 중복되므로 합산하지 않는다. Python 3.12.14, 직렬 실행이다.

| 실행 | 명령/대상 | 결과 |
|---|---|---|
| 공통 연결 회귀 | `pytest -o addopts="--strict-markers --disable-warnings" -rfE tests/test_quantitative_company_input_connection.py tests/test_quantitative_financial_binding.py tests/test_performance_scope_binding_revision.py tests/test_quantitative_replay_cli.py tests/test_quantitative_row_activation.py` | 105/105 통과 |
| 새 경로 최종 회귀 | `pytest -o addopts="--strict-markers --disable-warnings" -rfE tests/test_quantitative_review_preview.py tests/test_quantitative_review_preview_cli.py tests/test_quantitative_company_input_connection.py` | 51/51 통과(28·14·9), 2 warnings |
| 관련 전체 회귀 | 아래 PowerShell 선택 명령 | 1,836/1,836 통과, 5 warnings, 78.20초 |
| 기존 19건 저장 입력 재생 | 이전 `run_stored_path.py`, 원래 19건 snapshot 그대로 | 19/19 결과 전체 동일, 수치 총점 0/19 |
| 기존 30건 저장 입력 재생 | 같은 helper, 원래 30건 snapshot 그대로 | 30/30 결과 전체 동일, 수치 총점 0/30 |

```powershell
$connectionTests = @(rg --files tests | Where-Object { $_ -match 'test_(quantitative_|performance_|case_|source_gap_quantitative_scope|extraction_contract_compatibility|private_company_evidence|busan_education_quantitative_e2e|pps_enrichment|notice_quantitative_frontend|frontend_public_contract)' } | Sort-Object)
.venv/Scripts/python.exe -m pytest -o addopts='--strict-markers --disable-warnings' -rfE @connectionTests --basetemp=.local/pytest/connection-regression --junitxml=.local/test-runs/connection-regression.xml
```

19건·30건은 중복 0으로 총 49건이다. 원래 snapshot 바이트와 회사 digest가 동일하며, 상태·항목 점수·범위를 포함한 점수 결과가 49/49 동일하다. 기존 1공고의 잠정 항목 수치 2개는 유지되고 신규 총점 복구는 0건이다. 외부효과 시도는 0회다. 근거는 로컬 `connection-check-20260914/runtime_comparison.private.json`과 각 재생 출력이다.

### 보관 native와 변경 없는 raw의 실제 preview

위 30건 중 전체 기대 문서 SHA를 실제 저장 입력에서 확인할 수 있는 18건을 새 CLI로 실행했다. 나머지 12건은 현재 첨부 native digest 미확정 9건, 현행 metadata schema 미확인 3건으로 계획에서 제외했다. hash·ID·현재 첨부 목록을 복원하거나 축소하지 않았다.

| 분모 | 새 로컬 preview | 기존 저장 경로 |
|---|---|---|
| 실행 가능한 18공고 | AUTO_ACTIVE 1 / REVIEW_REQUIRED 17 | 동일 |
| 같은 18공고의 숫자 항목 | 1공고에서 2항목, 수치 총점 0 | 동일 |
| 18공고에 딸린 첨부 101개 | native/raw 대조 실행 74, ZIP member 페이지 미증명 4, 입력 누락 19, parser 불완전 4 | preview 진단 분모이며 공고 수와 합산 금지 |

이 실행에는 사람의 새 수정이나 승인으로 간주하지 않은 **변경 없는 저장 raw**를 넣었다. 표/규칙 AVAILABLE 수를 native/raw 대조 실행 74개로 바꾸어 해석하지 않는다. 입력 바이트 불변, 외부효과 시도 0회를 확인했다. 18건은 앞의 30건의 부분집합이므로 49건과 합산하지 않으며, 새로운 실제 총점 복구는 여전히 0건이다.

숫자 항목 집합·항목 점수·활성 상태·총점 범위의 정확 대조도 18/18 동일했다(`cohort30_unchanged_raw_comparison.private.json`).

근거: 로컬 `cohort30_unchanged_raw_plan.private.json`, `cohort30_plan_assessment.private.json`, `cohort30_unchanged_raw_preview.private.json`, `cohort30_unchanged_raw_summary.private.json`. 원문·회사 입력·공고 ID·첨부 경로는 Git에 넣지 않는다.

## 5. 남은 작업과 운영 경계

실제 총점 확정에는 원래 증빙 ID·원본 위치·값/단위/유효기간·의미 payload·현재 항목 binding이 보존된 회사 입력이 필요하다. 누락 필드를 추정해 생성하거나 미확인 실적을 0건으로 처리하지 않는다. 원문 참조/전체 조건과 승인 절차가 미증명인 규칙은 운영 경로로 자동 승격하지 않는다.

이 연결은 로컬 원문 검증부터 계산까지 실행할 수 있게 하지만 저장·운영 API·사람 승인/철회 모델을 구현한 것은 아니다. `quantitative_reference_context`와 D의 literal-only 상태를 자동 파이프라인에 연결하지 않았다. 다음 단계는 증명 가능한 원본 회사 입력을 같은 CLI에 넣어 확정/잠정/미산정 항목을 재검증하는 일이다.

유료 호출·나라장터 다운로드·운영 DB/n8n/Render 접근·main 병합·배포·워크플로 활성화는 하지 않았다. 제품 전체 테스트·PostgreSQL·원격 CI를 이번 실행의 1,836개 통과로 대체하지 않는다.

추가 검사: `python -m compileall -q src tests tools`, `git diff --cached --check` 통과. `.github/workflows/ci.yml`의 public-release boundary 및 secret/source scan을 로컬에서 그대로 실행해 통과했다(추적 파일 441개). private 산출물은 Git ignored다.
