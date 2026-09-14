# 실제 저장 입력의 계산 경로 대조 — 2026-09-14

코드 기준: `feat/quantitative-newpc-resume-0914`, `be13f0e`. 이번에는 이전 PC의 로컬 private 입력이 실제로 존재함을 확인해 읽었다. 앞선 A–D 보고서의 당시 미검증 기록을 변경하지 않는다. 제품 코드 변경은 없다.

## 1. 먼저 정정할 해석

| 해석 | 확인 결과 |
|---|---|
| 이전의 신규 복구 0건은 모든 항목에 숫자가 없다는 뜻이다 | 저장 경로의 1공고에서 이미 2항목이 ESTIMATED 수치를 반환한다. 전체 점수를 반환하는 공고와 신규 복구가 0건이라는 뜻이다. 이번에도 동일하다. |
| 이전 3사례의 조건부 7행 일치는 실제 회사자료를 넣은 검증이었다 | 기존 스크립트는 7행의 가정 사실을 직접 구성했다. 회사 스냅샷은 binding 감사에만 쓰였고 점수 resolver 입력으로 사용되지 않았다. 이번에는 기대값·가정 사실 없이 회사 스냅샷을 넣었다. |
| 이번 로컬 입력만으로 회사의 실제 증빙 부재를 확정할 수 있다 | 회사 스냅샷은 부분집합이다. 3개 사실 모두 identity/evidence link 필드가 빠져 있다. 운영의 실제 증빙 부재 또는 전체 회사자료 정확도를 뜻하지 않는다. |
| 원문의 대안 자격 관계가 있으면 점수 선택 방식도 확정된다 | 대안 자격이 명시된 대표 사례에서도 평가점수의 합산·선택 규칙은 별도 검증이 필요했다. 자격과 점수를 구분한다. |

## 2. 동일한 49공고 저장 경로

이전 19건 및 30건 입력의 바이트 SHA와 회사 스냅샷 digest가 이전 실행과 일치했다. 중복 공고는 0개다. 기존 파일을 덮거나 누락 필드·ID를 만들어 넣지 않았다.

| 분모 | 결과 |
|---|---|
| 기존 19건 | profile INCOMPLETE 19, runtime REVIEW_REQUIRED/REVIEW 19 |
| 기존 30건 | profile AVAILABLE 1 / INCOMPLETE 29, runtime AUTO_ACTIVE 1 / REVIEW_REQUIRED 29, UNSCORABLE 1 / REVIEW 29 |
| 중복 없는 49건 | 기존과 항목 상태·수치·총점 범위가 같은 공고 49/49 |
| 49건의 전체 점수 | 수치 총점 0/49, 신규 수치 총점 복구 0/49 |
| 49건의 항목 점수 | 1/49 공고에서 2개 항목의 ESTIMATED 수치, 신규 수치 항목 0개 |

첫 차단을 하나씩 배정하면 커버리지 44건, 그 외 규칙 검증 4건, 규칙 AVAILABLE 후 회사 입력 미산정 1건이다(합 49). 서로 겹치는 activation reason 개수를 공고 수로 더하지 않았다.

근거: 로컬 `stored_19.private.json`, `stored_30.private.json`, `stored_comparison.private.json`. 실제 저장 경로는 `scripts/replay-quantitative-sources.py:575` → `src/pai_loop/quantitative_scoring.py:4564`를 사용했다. 코드·입력 해시와 외부효과 guard를 함께 보존했다.

## 3. 대표 3사례의 독립 회사 입력 대조

원본 PDF 3/3의 SHA가 이전 자료와 일치했고 검토 후에도 원본은 불변이었다. 관련 15쪽을 시각 확인했으며 인쇄 쪽수와 PDF 물리 쪽수를 구분했다. 원문·회사 값·내부 식별정보는 이 공개 문서에 넣지 않는다.

저장 경로에서 3사례는 모두 REVIEW다. 별도로 기존에 사람이 구성한 단일 PDF 규칙에 **동일한 실제 회사 스냅샷**을 기존 resolver로 넣어 계산 능력을 대조했다. 이 규칙은 현재 전체 manifest의 소유·커버리지 증명을 갖춘 운영 규칙으로 승격하지 않았다.

독립 진단의 8행 중 2개 재무 항목은 ESTIMATED 수치를 반환했다. 신용 3행은 현재 항목에 결합된 증빙 입력이 없어 UNSCORABLE, 실적 2행은 추가 인정조건으로 REVIEW, 운영실적 1행은 규칙 검토 상태다. 실제 저장 경로의 항목 복구 수와 합산하지 않는다.

실적 literal 2개를 새 `verify_rule_draft`로도 검사했다. 기존 scope와 현재 parser의 해석은 2/2 일치했고 수동조건이 남아 2/2 ATTESTED_ONLY였다. 조건을 삭제하거나 미확인 실적을 확인된 0건으로 바꾸지 않았다.

기존 conditional golden은 SYN-CONDITIONAL 식별자 및 가정 입력을 사용한다. 실제 공고 ID로 비교하면 `GOLDEN_NOTICE_NOT_IN_SNAPSHOT`으로 거절된다. 식별자를 바꿔 통과시키지 않았고 새로운 실제 golden 성공률을 만들지 않았다. 이전 조건부 성공 기록은 그대로 보존한다.

근거: 로컬 `three_case_audit.private.json`, `three_actual_inputs.private.json`, `pdf_source_audit.private.json`, `golden_applicability.private.json`. 회사 resolver는 `quantitative_scoring.py:2230`, `:2324`, `:2441`, `:2498`을 재사용했다. 상태·점수 상세와 PDF 위치는 로컬 private 보고서에만 보존했다.

## 4. 실행·한계·다음 작업

새 파일은 `.local/actual-score-check-20260914/`에 기록했다. 기존 재생 helper를 복사할 때 옛 site-packages 경로 추가만 제거하고 현재 venv/src를 사용했다. 실행 명령은 다음과 같다(`OLD_*`는 이전에 존재한 로컬 입력 경로이며 자료를 새로 만드는 명령이 아니다).

```text
python -B .local/actual-score-check-20260914/run_stored_path.py --code-root . --snapshot OLD_19_SNAPSHOT --output .local/actual-score-check-20260914/stored_19.private.json
python -B .local/actual-score-check-20260914/run_stored_path.py --code-root . --snapshot OLD_30_SNAPSHOT --output .local/actual-score-check-20260914/stored_30.private.json
python -B .local/actual-score-check-20260914/check_three_actual_inputs.py
```

위 재생 19/19, 30/30, 독립 대조 3/3은 실행을 완료한 분모이며 점수 성공률이 아니다. 운영 DB·n8n·Render 접근, 네트워크·유료 모델·다운로드·운영 쓰기는 0회다. 회사 부분집합을 그대로 메모리에 로드했고 snapshot/native/PDF 바이트 불변과 외부효과 guard를 확인했다. 제품 코드 변경이 없어 전체 테스트를 반복하지 않았다.

후속 작업은 회사 증빙의 원래 ID·연결·단위·유효기간·현재 criterion binding을 보존하는 입력 확보와, 원문의 완결된 규칙을 승인된 계산 경로에 연결하는 일이다. 숫자 산식을 새로 늘리거나 유료 재추출을 먼저 실행할 근거는 이번 대조에서 나오지 않았다. 실적 추가조건과 점수 선택 규칙은 증명될 때까지 REVIEW로 유지한다.

main 병합·rebase·배포·워크플로 활성화는 수행하지 않았다. 기존 49건 신규 복구 0건 결론을 삭제하거나 조건부 결과로 대체하지 않는다.
