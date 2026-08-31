# 정량점수 산정 및 운영 배포 인계 — 2026-09-01

## 목표

`2026 부산교육한마당 위탁 용역` 공고에서 첨부 제안요청서의 공고별 정량 산식을 추출하고, 회사 실적·신용평가 데이터를 결합해 정량점수를 표시한다.

대상 식별자:

- notice key: `PPS-R26BK01703600-000-8f0e732a38`
- bid notice no: `R26BK01703600`
- 기대 점수: `20/20`
  - 용역수행 실적(금액): 6점
  - 용역수행 실적(건수): 4점
  - 경영상태/신용평가 A0: 10점

## 구현 완료

핵심 구현 커밋은 `22adea8` (`Fix source-bound quantitative scoring`)이다.

- 공고별 표를 일반화된 CASE_TABLE 산식으로 추출·검증·실행한다.
- NOTICE 요약표와 RFP/SCOPE 상세표를 역할·출처 검증 후 결합한다.
- 실적 금액/건수는 공고일 기준, 공공기관, 교육·취업·행사, 수행완료, 증명서, 공동계약 지분 조건을 적용한다.
- VAT 포함/제외로 배점 경계를 넘으면 임의 점수 대신 REVIEW로 닫는다.
- 신용평가의 복수 열을 혼합하지 않고 해당 회사 유형 열을 사용한다.
- 산식 누락·경계 공백·역할 불명·scope 충돌은 fail-closed 처리한다.
- 정량 엔진 버전은 `pai-loop-quantitative-engine-1.6.0`이다.

실제 HWP 전처리 검증:

- 파일: `『2026 부산교육한마당』 위탁 용역 제안 요청서.hwp`
- 추출 완료: `complete=True`, 경고 없음
- 추출 텍스트: 42,027자
- 32~34쪽 금액표, 건수표, 신용평가표 및 각주가 모델 입력에 포함됨
- `BID_NOTICE_DATE`, 실적증명서 필수, 공동계약 지분 적용 조건을 파싱함

## 테스트 및 CI

- 로컬 전체 테스트: 783개 통과
- 정량/HWP 집중 테스트: 124개 통과
- 부산 합성·실데이터 E2E: `6 + 4 + 10 = 20`
- 원격 커밋:
  - `22adea8`: 정량 산식 구현, CI 성공
  - `7256134`: Render 배포 재시도, CI 성공
  - `056f4ef`: `render.yaml` Blueprint 동기화 주석 추가
  - `c7c95ba`: 만료된 테스트 fixture 29곳을 상대 미래 마감일로 수정, CI 성공

`056f4ef`의 첫 CI 실패는 production 회귀가 아니었다. `tests/test_analysis_api.py`의 고정 마감일 `2026-08-31T18:00:00+09:00` 29곳이 만료되어 28개 planner/lease 테스트가 연쇄 실패했다. SQLite가 offset을 버린 naive `18:00`을 UTC로 해석하면서 UTC 18:00 이후 발생했다. production의 만료 공고 제외 로직은 유지했고 테스트 데이터만 현재 시각 + 30일로 수정했다. 최종 CI는 783개 모두 통과했다.

## 운영 상태 — 차단됨

2026-09-01 03:36~03:51 KST 동안 30초 간격으로 15분 감시한 결과:

- 운영 URL: `https://pai-loop-demo.onrender.com`
- 현재 엔진: `pai-loop-quantitative-engine-1.4.0`
- 현재 상태: `REVIEW`
- 점수: 미표기

`render.yaml`을 직접 변경하고 후속 CI가 성공했는데도 새 배포가 생성되지 않았다. Render 서비스의 Git 연결, Auto Deploy 또는 Blueprint Auto Sync가 꺼져 있거나 분리된 상태일 가능성이 높다. 구버전에서 불필요한 LLM 호출을 만들지 않기 위해 수동 재분석은 실행하지 않았다.

## Chrome 자동화 차단

사용자가 Chrome ChatGPT 확장프로그램 권한을 허용했다. 그러나 Browser Node 샌드박스가 Windows ACL 적용 단계에서 종료된다.

- 최초 확인된 과거 테스트 임시 폴더 21개는 관리자 UAC 승인 후 `takeown` + `icacls /reset` 완료.
- 추가 스캔에서 접근 불가 생성물 디렉터리 52개가 남아 있음.
- 남은 항목은 `.pytest_cache`, `.pytest-tmp-*`, `.local/audit-wheel-*`, `dist-public/isolated`, 설치 QA 산출물이다.
- 소스 파일은 대상이 아니다.
- 추가 52개 소유권/ACL 변경은 별도 명시 승인 범위를 넘어 안전 제한으로 실행하지 않았다.
- 로컬에 `C:\Users\skytr\Desktop\AI SPRINT\fix_test_acl.ps1`이 남아 있으며 최초 21개 경로만 포함하고 비밀정보는 없다.

## 회사에서 재개할 순서

1. 관리자 PowerShell에서 추가 52개 생성물 경로만 정확히 확인하고 소유권 및 ACL 상속을 복구한다. 저장소 전체에 재귀 ACL 변경을 적용하지 않는다.
2. Codex/Chrome 연결을 다시 시작하고 Browser Node가 `agent.browsers.get("chrome")`에 성공하는지 확인한다.
3. 로컬 저장소는 `7256134`에서 멈췄으므로 ACL 복구 후 원격 `main`의 `c7c95ba` 이상으로 fast-forward 한다.
4. Render 대시보드에서 `pai-loop-demo` 서비스의 다음 항목을 확인한다.
   - 연결 저장소/브랜치가 `skytree96-cmyk/PAI_LOOP` / `main`인지
   - Auto Deploy가 `After CI Checks Pass`인지
   - Blueprint Auto Sync가 켜져 있는지
   - Events에 `c7c95ba` 배포가 생성됐는지
5. 이벤트가 없다면 `Manual Deploy > Deploy latest commit`으로 최신 `main`을 배포한다.
6. 공개 정량 API에서 엔진이 `pai-loop-quantitative-engine-1.6.0`으로 바뀐 뒤에만 대상 공고 재분석을 요청한다.
7. 재분석 완료 후 API와 Chrome 화면에서 다음을 확인한다.
   - 금액 6점
   - 건수 4점
   - 신용평가 10점
   - 총점 20/20
8. Chrome 화면에 실제 점수가 표시된 것을 확인한 경우에만 PC 종료 조건을 실행한다.

## 현재 종료 판단

운영 화면에 점수가 표시되지 않았으므로 PC를 종료하지 않았다.
## 종료 및 회사 재개 메모

- 기록 시각: $stamp
- 운영 최종 확인값은 엔진 `1.4.0`, `REVIEW`, 점수 미표기 상태다.
- 정량점수 화면 검증은 완료되지 않았지만, 사용자가 명시적으로 PC 종료를 요청했다.
- PC 종료 후 회사에서 이 문서의 '회사에서 재개할 순서'부터 이어서 진행한다.
- 재개 시 원격 `main` 최신 커밋을 먼저 동기화하고 Render 수동 배포부터 확인한다.
