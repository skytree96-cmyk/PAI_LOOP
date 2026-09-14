# PAI_LOOP 정량점수 재개·독립 검토 — 2026-09-14 (새 PC)

작성: 2026-09-14 KST, 새 PC. 기준 커밋 `d817ce5e6157f4cd18b8546bd31f911940240695`. 이메일 첨부 `IMPLEMENTATION_SNAPSHOT_20260914.md`의 복원 패치(SHA256 `0c37f340…44eb60`)를 검증한 뒤 기존 작업이 없는 별도 worktree에 적용했다(`git apply --check` 통과, 14파일). 브랜치 `restore/quantitative-d817ce5-0914`, **미커밋**. 이 문서는 회사 자료·공고 원문·비밀값을 인용하지 않는다. 이번 세션의 유료 모델 호출·나라장터 다운로드·운영 DB/n8n/Render 접근·commit·push·PR·배포·워크플로 활성화는 모두 **0회**다.

## 0. 한 줄 결론

- 복원은 성공했고, 관련 회귀는 수신 패치가 깨뜨린 호환 테스트 1건(§1 마지막 행)을 제외하고 통과했다. 이전 문서의 "옛 binding이 새 REVIEW를 덮어쓸 수 없다"는 유지되지만, **원문 literal에 전각 숫자가 있으면 요청·파생 경로가 고정 실적기간 조건을 놓쳐 자동 집계(ESTIMATED)하는 반례**를 합성으로 재현했고 최소 범위로 수정했다(§3.2, §4).
- 참조 모듈이 자동 파이프라인에 연결됐다는 잘못된 설명은 없다(import 0). 표 종료 증명 회귀 4건을 추가했다(§3.3).
- 49공고 신규 복구 0과 조건부 golden 7/8은 private 자료가 이 PC에 없어 **확인 불가**다. 재구성하거나 수치를 만들지 않았다(§3.4, §6).

## 1. 틀렸거나 좁혀야 하는 주장부터

| 주장 | 판정 | 근거(복원 worktree 기준 파일:행) |
|---|---|---|
| 실적 인정조건은 어느 계산 경로에서든 같은 의미로 읽힌다 | **반박(합성 재현, 수정 완료)** | 결합 경로는 `_performance_scope_literal`로 NFKC 정규화한 literal을 파싱한다(`src/pai_loop/quantitative_scoring.py:2845`, `:2564`). 반면 활성 게이트(`:3733`), 요청 생성(`:4110`), `derive_performance_value`는 원문 literal을 그대로 썼다. 전각 숫자 "２０２５년부터 ２０２６년까지"는 `_FIXED_RECOGNITION_PERIOD_RE`의 `20\d{2}`에 걸리지 않아 수동조건 없이 파싱되고, SYN 실적 1건이 **ESTIMATED 1.0**으로 집계됐다. 같은 문장을 정규화하면 REVIEW다. |
| binding 불일치 시 "generic 값을 점수에 적용하지 않았다" | 범위 제한 | `quantitative_scoring.py:1092–1103`은 결합값이 없는 generic 사실과 옛 의미에 결합된 stale 사실을 구분하지 않고 같은 UNSCORABLE·문구를 낸다. 과대계산은 아니지만 설계 문서의 "원인 분리" 원칙과 어긋난다. |
| 부분 활성의 `retained_total == original_total` 검사가 소계 보존을 증명한다 | 범위 제한 | `:4013–4016`은 같은 후보 집합을 good/review로 나눈 뒤 합을 비교하므로 항등식이다. 실제 소계·대안표·커버리지 증명은 `_profile_activation_reason_partition`의 notice 사유(`:3678–3708`)와 `_logical_quantitative_program`에서 온다. 검사 자체는 무해하다. |
| 회귀 1,253/624 통과가 전체 검증이다 | 범위 제한 | 이 PC에서 수집된 전체 테스트는 141파일 4,294개다. 이번에 확인한 묶음은 §5에 분모별로 적었다. |
| 49건 신규 복구 0, 조건부 golden 7/8 | **확인 불가** | `.local/` private 자료(snapshot·golden·원문)가 이 PC에 없다. 재현 수치를 만들지 않았다. |
| 수신 패치 상태에서 관련 회귀가 모두 통과한다 | **반박(재현)** | `tests/test_extraction_contract_compatibility.py::test_additive_empty_upper_bound_preserves_existing_company_fact_identity`는 bare d817ce5에서 통과하지만 d817ce5+수신 패치에서 실패한다(이 세션의 수정 전 상태를 별도 worktree에서 재현). 원인은 실적 binding에 현재 인정조건 의미를 넣은 의도된 변경(`quantitative_scoring.py:2556–2567`)이 기존 "빈 상한은 회사 사실 identity를 바꾸지 않는다" 호환 테스트에 반영되지 않은 것이다. 테스트를 의도된 계약(빈 상한은 여전히 불변, 실적 binding은 1회 재결합)으로 갱신했다. |

## 2. 복원 기록

| 항목 | 결과 |
|---|---|
| 저장소 | `skytree96-cmyk/PAI_LOOP` 새 clone. `docs/quantitative-resume-20260914`(e873238)의 최소 안내 확인 |
| 기준 커밋 | `d817ce5` 존재 확인, worktree 생성 |
| 패치 | 문서의 Python 지침으로 추출, SHA256 일치, `git apply --check` 통과, 적용 후 상태 = 문서의 14파일(수정 5, 신규 9) |
| 비밀값 스캔 | 패치 안 key/secret/token 패턴 1건, 문서 문장의 단어 "secrets"뿐 |
| 위치 | 작업본 `<사용자 홈>\Desktop\PAI\PAI_LOOP_resume_0914\PAI_LOOP_restore_0914`, main clone은 옆 `PAI_LOOP_resume_0914`. `.venv`는 포함하지 않았다. GitHub 작업 브랜치 `feat/quantitative-newpc-resume-0914`에 같은 트리를 커밋했다 |
| 환경 | Python 3.12.14(uv 관리), pytest 8.4.2. 이 PC의 3.13/3.14 설치본에는 `python.exe`가 없어 venv 생성이 실패한다. pytest는 `--basetemp`를 지정해야 한다(기본 임시폴더 권한 오류). 이 세션의 sandbox에서 git.exe는 Desktop/홈 아래에 쓰지 못해 Temp에서 clone한 뒤 복사했다 |

venv 재생성: `uv venv --python 3.12 .venv` 후 `uv pip install --python .venv/Scripts/python.exe -e ".[test]"`.

## 3. 검토 항목별 결론

### 3.1 부분 활성이 전체 배점·필수 근거·대안표/다중표/커버리지 차단을 유지하는가

- **결론: 확인. 반례 없음.**
- 파일:행: `quantitative_scoring.py:3678–3708`(커버리지·source issue·program 사유·FACT_KEY_AMBIGUOUS는 행 제거 전 전체 후보로 계산), `:3975–4018`(`_available_row_partial_plan`: notice 사유가 하나라도 있으면 None, 실패 표가 1개일 때만, 전체 program을 먼저 해석), `:1370–1395`(review 항목의 max를 분모·상한에 유지), `:1414`(미확정 배점 합산).
- 합성 재현: `tests/test_quantitative_row_activation.py` 9건 통과. SYN 결과는 총점 15, 확정 9(신용), 실적 행 REVIEW 0~6, 합계 REVIEW·점수 null이다.
- 최소 수정 범위: 없음. `retained_total` 검사는 항등식이므로 "program 해석이 실제 증명"이라는 주석 정도.

### 3.2 실적 parser의 OR/AND, 발급기관/발주처, 기간·VAT·지분 조건 누락과 옛 binding 덮어쓰기

- **결론: 규칙 문법(OR+공통조건 AND, 발급기관 분리, 고정기간·하도급·발급기관 수동조건, VAT 충돌, 지분 충돌, 다중 lookback)은 확인. 단 문자 정규화 불일치로 인한 과대계산 반례 1건을 재현하고 수정했다.**
- 파일:행: `src/pai_loop/quantitative_performance.py:213`(`_ISSUER_CONDITION_RE`), `:365`(발급기관 구문 안의 공공기관 언급만 제외), `:384`(`_compound_service_groups`: 닫힌 절만 허용, 잔여 수식어가 있으면 거절), `:413`(`_consortium_share_rule`: 이번 입찰 점수 가중은 None), `:438`(`_unsupported_recognition_reason`), `:470`(parse: VAT 포함·제외 동시 → None, lookback 연수 2종 → None), `:834–870`(derive: 저장 scope를 현재 해석과 재대조), `:953`(그룹 매칭 any(all)).
- 재현(수정 전): SYN BASE + "실적 인정기간은 ２０２５년부터 ２０２６년까지" → parse 성공, 수동조건 () → derive **ESTIMATED 1.0**. 같은 문장을 정규화하면 수동조건 1건 → REVIEW. 저장 scope의 `source_literal`만 바꾼 stale 케이스도 같다.
- 최소 수정(적용): `_normalized_source_text`(NFKC+공백 정규화) 1개를 두고 parse, `_manual_recognition_conditions`, `_unsupported_recognition_reason`, derive의 원문 재대조가 같은 문자열을 보게 했다(`:281`, `:442`, `:458`, `:484`, `:841–868`, `:899`). ASCII 입력의 결과는 바뀌지 않는다(기존 실적 회귀 174건 동일). 실적 알고리즘 `performance-recognition-0.4.1`, 엔진 `1.8.6`.
- 옛 binding: `_candidate_fact_binding_sha256`(`quantitative_scoring.py:2540–2567`)이 현재 해석 scope와 계약 표식을 포함하므로 의미가 바뀐 옛 회사 사실은 결합 불일치로 점수에 쓰이지 않는다(`tests/test_performance_scope_binding_revision.py` 4건 통과). 남은 문제는 §1 2행의 라벨이다. 이 binding 변경은 기존 실적 binding 전체를 1회 무효화하므로 운영 반영 시 재결합 절차가 필요하고, 기존 호환 테스트 1건이 실패하는 상태로 전달됐다(§1 마지막 행, 이번에 갱신).

### 3.3 참조 모듈이 자동 파이프라인에 연결됐다는 설명이 있는가, 실제 통합에 필요한 최소 소유권·표 종료 증명은

- **결론: 연결 없음(설명 정확).** `quantitative_reference_context`를 import하는 곳은 `tests/test_quantitative_reference_context.py`만이다. `scripts/replay-quantitative-sources.py`, `quantitative_rule_extraction.py`, scoring은 참조하지 않는다.
- 모듈의 종료 규칙: 서식 블록은 다음 독립 표제가 있어야 하고(`src/pai_loop/quantitative_reference_context.py:243`, `:261`), 소유 표 영역과 겹치면 차단, 행 문구는 정규화 원문에서 유일해야 한다(`:193`). RESOLVED도 `persistence_eligible=False`(`:72`).
- 실제 native 사례에 붙지 않는 코드 근거: QRE의 후보 영역은 다음 구조 경계가 없으면 `len(lines)`(EOF) 또는 시작+64줄에서 닫힌다(`src/pai_loop/quantitative_rule_extraction.py:2805–2841`). 경계 탐지 창은 4줄이다(`:307`). 표의 끝이 EOF나 줄수 상한으로 정해진 영역은 소유권 증명이 아니다.
- 통합 전 최소 증명: (1) 소유 표의 종료가 다음 표제·다음 표 header·명시적 표 끝 중 하나의 실제 문자 위치로 닫혀야 하며 EOF·N줄 상한·원문 전체 검색은 불가, (2) 행 문구가 첨부 정규화 원문에서 유일, (3) 형제 행 영역이 서로 겹치지 않음, (4) 서식 표제가 목차가 아닌 실제 서식 본문을 가짐, (5) 연결된 서식도 다음 표제로 닫힘. 이번에 (1)(5)를 SYN으로 고정하는 회귀 4건을 추가했다(§4). 근거가 충돌하거나 종료가 모호하면 BLOCKED이며 소비자는 REVIEW를 유지한다.

### 3.4 49건 신규 복구 0과 조건부 golden 7행 일치가 각각 입증하는 것

- 49건 0복구: 같은 저장 입력에서 **새 로직이 어느 공고의 활성 상태·점수도 바꾸지 않았다**는 회귀 안전성 증명이다. 계산 제공 능력의 증명이 아니며, 48건 REVIEW의 원인(커버리지 44건 등)이 그대로라는 뜻이다.
- 조건부 golden 7/8: **정확히 결합된 가정 입력이 주어지면 엔진 산술이 사용자 기대와 일치**함을 보인다. 원문 규칙 자동 추출 성공, 회사 사실 확정, 실제 점수 발행의 증명은 아니다.
- 이 PC의 상태: 두 결과 모두 **확인 불가**(private 파일 없음). 코드 SHA·엔진 버전만으로 재현 수치를 만들지 않았다.

### 3.5 먼저 제품에 계산을 제공하려면 무엇을 우선할지

- **권고: 사람 확인 규칙 입력의 순수 검증 함수를 먼저, 참조 자동 연결은 뒤로.**
- 이유: 참조 자동 연결은 QRE의 표 종료 증명(§3.3)이 선행돼야 하고 record fingerprint·계약 변경을 부른다. `docs/VERIFIED_RULE_INPUT_DESIGN_20260913.md`의 1단계 `verify_rule_draft`는 기존 파서·검증기를 재사용하며 저장·API를 바꾸지 않는다.
- 가장 작은 첫 조각: 실적 항목에 한해 사람이 **첨부 SHA와 원문 인용(literal)만 제출**하고, 서버가 `parse_performance_recognition_scope`로 파싱해 수동조건 없이 성공하면 `SOURCE_VERIFIED`, 실패나 수동조건 잔존이면 `ATTESTED_ONLY`로 두는 순수 함수와 SYN 회귀. 사람이 scope 필드를 직접 입력하는 방식은 파서 우회이므로 금지. 회사 증빙 미확정은 규칙 미확정과 별도 코드로 표시하고 "자료 없음"을 원문상 0점으로 바꾸지 않는다.

## 4. 이번 PC에서 한 변경(모두 미커밋)

| 파일 | 변경 | 검증 |
|---|---|---|
| `src/pai_loop/quantitative_performance.py` | `_normalized_source_text` 추가. parse·수동조건·미지원사유·derive 재대조의 정규화 통일. 알고리즘 0.4.0→0.4.1 | 실적 묶음 176 통과 |
| `src/pai_loop/quantitative_scoring.py` | 엔진 1.8.5→1.8.6 | `tests/test_case_award_evidence.py` 기대값 갱신 |
| `tests/test_performance_recognition_semantics.py` | 전각 숫자 고정기간 2케이스: 어느 호출자도 수동조건을 놓치지 않고, stale scope도 REVIEW | 신규 2 통과 |
| `tests/test_quantitative_reference_context.py` | 표 종료 회귀: EOF까지 열린 표 차단, 다음 절 또는 서식 표제에서 닫힌 표는 RESOLVED, 서식 표제를 삼킨 표 차단 | 신규 4 통과 |
| `tests/test_extraction_contract_compatibility.py` | 실적 후보의 binding identity 기대값을 수신 패치의 의도된 계약으로 갱신(빈 상한은 여전히 불변, 실적 binding은 현재 인정조건 포함) | 파일 97 통과 |

바꾸지 않은 것: `quantitative_rule_extraction.py`, record fingerprint, 참조 모듈의 파이프라인 연결, 공개 계약, 복원 패치에 들어 있던 이전 문서들.

d817ce5→현재 트리 전체 diff(수신 패치 14파일 + 이 문서 + 갱신한 호환 테스트 = 16파일)는 `restore_plus_newpc_0914.patch`로 저장했고 SHA256은 옆의 `restore_plus_newpc_0914.patch.sha256`에 있다. 위치는 `<사용자 홈>\Desktop\PAI\PAI_LOOP_resume_0914\`(로컬 전용, 저장소에는 넣지 않음). 적용 절차는 수신 패치와 같다(깨끗한 d817ce5 worktree에서 `git apply --check` 후 `git apply`).

## 5. 테스트(모두 이 PC, Python 3.12.14, `--basetemp` 지정)

| 묶음 | 결과 |
|---|---|
| 복원 직후 관련 12파일(행 활성·실적 의미·binding·참조·재생 CLI·award·자동/부분 활성·논리 프로그램·실적 시나리오·private evidence) | 325 통과 |
| 참조 모듈 파일(신규 4 포함) | 46 통과 |
| 수정 후 실적 묶음 6파일(신규 2 포함) | 176 통과 |
| 수정 후 관련 19파일 재실행 | 961 통과, 1 실패. 실패 1건은 수신 패치가 깨뜨린 호환 테스트(§1 표 마지막 행). 의도된 계약으로 갱신한 뒤 해당 파일 97 통과 |
| 전체 141파일 4,294개 | **미확정.** 직렬 실행은 17%에서 약 60분 이상이 예상되어 중단. pytest-xdist 8 worker 병렬 실행은 99%까지 진행했으나 worker 2개가 비정상 종료(node down)하고 요약 없이 멈춰 중단(그 사이 실패 표시 1건, 테스트 ID 미확인). 전체 통과를 주장하지 않는다 |
| `git diff --check`, `compileall` | 통과 |

묶음 사이에 중복이 있으므로 합산하지 않는다. PostgreSQL·CI gate는 실행하지 않았다.

## 6. 재개 프롬프트 4)의 분리 보고

| 항목 | 실제 저장 입력(49공고) | SYN |
|---|---|---|
| 신규 계산 공고 수 | 실행 불가(private 없음) | 해당 없음 |
| 확인된 항목 배점 | 확인 불가 | 신용 9/15 확정 |
| 미확정 배점 | 확인 불가 | 실적 6/15(0~6) |
| 오산 수 | 확인 불가 | 수정 후 0. 수정 전 코드에서 전각 숫자 반례 1건은 실적 1건을 과대 집계했다 |

이전 결과(49건 신규 복구 0)는 지우거나 조건부 성공으로 대체하지 않는다.

## 7. 미해결과 다음 단계

1. binding 불일치 라벨 분리(generic vs stale): `quantitative_scoring.py:1092–1103` 상태·문구만.
2. QRE 표 종료 증명: 후보 영역 끝이 EOF나 64줄 상한에서 온 경우를 "종료 미증명"으로 표시하는 필드를 추가하고 참조 모듈의 입력 조건으로 사용. 별도 SYN 문서로 양성·음성 회귀 필요.
3. `verify_rule_draft` 1단계(실적 literal 파싱 검증) 구현(§3.5).
4. private 자료가 있는 PC에서 49건 재생과 조건부 golden을 엔진 1.8.6으로 재실행해 변화 0을 확인. 실제 원문에 전각 문자가 있으면 REVIEW가 늘 수 있고 그것은 과대계산 제거다.
5. 전체 4,294 테스트를 직렬로 끝까지 실행해 결과를 확정(§5). 병렬 실행에서 worker가 죽은 파일과 실패 1건의 ID를 먼저 찾아야 한다(`-q` 없이 `-rfE`로 실행). 그 뒤 커밋·push 여부를 사용자 승인으로 결정.

## 8. 멘토 질문 보강

기존 5개 질문에 하나를 더한다. "원문 literal 정규화(전각·공백·기호)의 책임을 추출 단계와 계산 단계 중 어디에 둘 것인가, 그리고 두 단계의 정규화 버전을 어떻게 함께 관리할 것인가?" 이번 반례는 두 단계가 다른 정규화를 써서 생겼다.
