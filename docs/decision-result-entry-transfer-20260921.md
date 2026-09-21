# 결과 입력 UI 코드 이전

기존 공개 인수인계서의 "구현 코드는 로컬에만 있음" 상태를 갱신하는 문서다.
원래 구현 커밋 `ac6fed67091baf2e570d94096f424f57b5a61e33`의 코드와 테스트를
main `ffdc2cfdaa2d3139e6c7490d700571d57111f422` 위로 충돌 없이 옮겼다.
기존 로컬 작업과 비공개 인수인계 이력은 보존하고, 그 문서 이력은 게시하지 않는다.

## 변경

- 판단 메뉴에 `결과 입력` 항목과 건수를 표시한다.
- 입찰마감 전 `진행 건`과 마감 후 결과 입력 대상의 이동을 설명한다.
- 판단 대기, 진행 건, 마감 임박, 결과 입력 큐에 고유 프런트엔드 경로를 등록한다.
- 서버 진입 경로와 목록 뷰 분류도 함께 등록하여 새로고침과 공고 상세 링크를 지원한다.

구현 파일은 `src/pai_loop/static/index.html`, `src/pai_loop/static/app.js`,
`src/pai_loop/app_access.py`다. 기존 구현과 동작은 동일하며 이전 과정에서 기능을 추가하지 않았다.

## 회사 PC에서 받기

원격 브랜치: `feat/decision-result-entry-0921`.
fetch 설정이 특정 브랜치로 제한된 클론에서도 명시적으로 받을 수 있다.
작업 트리가 깨끗하고 같은 이름의 로컬 브랜치가 없는 새 작업 환경에서:

```sh
git fetch origin feat/decision-result-entry-0921
git switch -c feat/decision-result-entry-0921 FETCH_HEAD
```

이미 같은 이름의 로컬 브랜치가 있다면 자동 덮어쓰기나 강제 재설정 없이
로컬 변경과 원격 이력을 먼저 비교한다. 기존 개발 PC의 같은 이름 브랜치는
비공개 문서 커밋을 포함하므로 무조건 다시 푸시하지 않는다.

## 검증 및 남은 작업

이전 후 위 main 기준으로 다음 7개 테스트 파일 **131개 통과**(2 warnings)를 확인했다.
`node --check src/pai_loop/static/app.js`도 통과했다.

검증 파일:
`test_frontend_public_contract`, `test_top_navigation_frontend`,
`test_teams_sidebar_runtime`, `test_dashboard_work_queues`,
`test_result_entry_frontend`, `test_required_app_login`,
`test_required_app_login_frontend`.

전체 CI와 실제 부서 계정 브라우저 확인 결과는 PR에서 확인한다.
코드 이전과 운영 배포를 구분한다. 이번 이전 작업은 운영 병합·배포를 수행하지 않는다.

브라우저 확인 항목:

- 판단 메뉴의 결과 입력 항목과 건수 표시
- 네 작업 큐 클릭 시 각 큐의 목록으로 이동
- `/result-entry` 직접 진입 및 새로고침
- 공고 상세 링크, 뒤로 가기, 모바일 메뉴 레이아웃

부서 키워드 프로필 변경은 이 작업에 포함하지 않는다.
