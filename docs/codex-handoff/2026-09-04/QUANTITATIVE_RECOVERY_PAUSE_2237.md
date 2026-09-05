# 정량점수 복구 작업 중단 인계 — 2026-09-04 22:37 KST

## 중단 사유

사용자가 Codex 사용량 1% 도달 전에 종료하도록 요청해 운영 재배포 전 안전 중단했다. 이 문서는 완료 보고가 아니다.

## 운영에서 확인한 마지막 상태

- 대상: `2026 부산교육한마당 위탁 용역`
- notice key: `PPS-R26BK01703600-000-8f0e732a38`
- PR #83 merge SHA: `1f0d5fe3826169993016097ed55eac0ef2b187e0`
- Render deploy: `dep-dadbi5tg1s2s73fs3vt0` (LIVE)
- 수동 분석 request: `f3767e29-f28e-4f53-80cb-bfd20653ed54`
- 분석 종료: `COMPLETED / ANALYZED`, attachments `2/2/2`, 모델 호출 2회
- 그러나 정량 프로필은 `INCOMPLETE`, 후보 `0 available / 3 review`; 따라서 운영 화면의 `20 / 20`은 아직 확인되지 않았다.
- 주요 issue: `AMBIGUOUS_RULE` 3건, `AMBIGUOUS_TABLE`, `CASE_TABLE_NOT_DETERMINISTIC`, PDF의 `EXTRACTION_DECLARED_INCOMPLETE`.

## 로컬 WIP 수정

- `quantitative_formula.py`: 신용등급 한 행이 여러 셀/줄로 나뉘고 비최종 조각이 쉼표로 끝나는 경우를 source Counter와 canonical 순서가 정확히 일치할 때만 컴파일.
- `quantitative_rule_extraction.py`: 실제 인쇄 행 전체가 source-wide census로 증명된 경우에만 세 가지 설명성 `ambiguity_reason`을 해제. 표에 없는 하한/default 점수는 생성하지 않음. 부산 표의 요약/상세 동일 소계 설명은 20=(6+4)+10 일치까지 검증.
- `source_gap_policy.py`: `제안요청서 원문(붙임)이 본 SOURCE에 포함되지 않아 세부 기술평가 배점표를 확인할 수 없음`을 RFP sibling 결손으로만 좁게 분류.
- `openai_extraction.py`: prompt `0.5.3`; `ambiguity_reason`은 점수를 바꿀 수 있는 미해결 대안에만 사용하고, 올바른 요약/상세 중복 제거·명시적 기업신용평가 열 선택·인쇄되지 않은 하한 행 부재 설명에는 null을 반환하도록 지시.
- 버전: candidate profile `0.7.14`, attachment validator `0.6.17`.

## 검증 내역

- 신용등급 formula/case-table: 66 passed (에이전트 실행).
- 기존 정량 추출 전체: 변경 전 신용등급 패치 기준 100% passed (에이전트 실행).
- PDF gap 관련 묶음: 46 passed (에이전트 실행).
- 새 운영형 회귀 `test_busan_live_explanatory_notes_do_not_block_source_proven_table`: passed.
- 전체 suite/CI는 아직 실행하지 않았다. WIP를 배포하거나 merge하면 안 된다.

## 다음 시작점

1. 사용자 소유 dirty 파일 `src/pai_loop/static/index.html`, `src/pai_loop/static/styles.css`, `tests/test_frontend_public_contract.py`는 stage/overwrite하지 않는다.
2. 현재 변경 diff를 검토하고 candidate/table ambiguity에 adversarial negative tests를 보강한다.
3. 관련 테스트 전체와 full suite를 실행한다.
4. 승인 브랜치 `codex/quant-minimum-scope-recovery-20260904`에 새 코드 커밋을 push하고 PR/CI 통과 후 merge한다.
5. Render에서 최신 merge commit을 수동 deploy한다.
6. 동일 공고 분석을 새로 실행해 diagnostics가 `AVAILABLE`, attachments `2/2/2`, candidates `3 available / 0 review`, estimate `20 / 20`인지 확인한다.
7. 새 Chrome 탭에서 `정량·리스크` 화면의 `예상 점수 범위 20 / 20`, `배점 근거 확인율 100%`, 각 행 `6 / 4 / 10`을 직접 확인한 뒤에만 최종 완료 문서를 갱신한다.

## 주의

- 첫 운영 요청 body는 `{"run_extraction": true}`였으며 `retry_reviewed`를 사용하지 않았다.
- Chrome의 native `confirm()`이 확장 제어에서 복귀하지 않는 문제가 있어, 첫 요청은 승인된 PIN을 사용한 공식 HTTPS API로 수행했다.
- PIN/API key/DB URL 등 비밀값은 문서·로그·커밋에 남기지 않는다.
