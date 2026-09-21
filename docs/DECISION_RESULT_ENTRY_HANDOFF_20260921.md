# 판단 메뉴 `결과 입력` 추가 + 작업 파이프라인 경로 복구 — 공개 인수인계

이 문서는 다른 PC에서 작업을 이어받기 위한 **공개용 요약**이다.
운영 리비전·상태값, 운영 진단 수치, 비공개 공고 식별자, 계정 정보, 로컬 경로·로그는 의도적으로 제외했다.
그 항목이 필요하면 작업 PC에 있는 비공개 원본 인수인계서를 볼 것.

- 작성: 2026-09-21 KST
- 작업 브랜치(미게시): `feat/decision-result-entry-0921`
- 이 문서 브랜치: `docs/decision-result-entry-handoff-20260921`

---

## 0. 지금 상태 — 로컬 작성과 원격 게시를 구분할 것

| 항목 | 상태 |
|---|---|
| 구현 코드 | **작업 PC 로컬 커밋만. 원격에 없음** (푸시 · PR · 배포 전부 미수행) |
| 이 문서 | 원격 게시됨 (`docs/decision-result-entry-handoff-20260921`) |

**즉 이 문서를 읽어도 코드는 아직 받을 수 없다.** 3절의 이동 절차를 먼저 해야 한다.
구현 코드의 병합·배포는 이번 작업 범위가 아니며 진행하지 않았다.

### 관측 시점 주의

본문의 저장소 상태는 **2026-09-21 09:35 KST 관측 기준**이다.
그 시점 원격 `main` = `ffdc2cf` (`#185`, `#186`, `#187` 머지 반영).
이 문서 브랜치는 그 커밋 위에서 잘랐다.

main은 계속 움직이므로, 이어받을 때는 반드시 직접 확인할 것:

```bash
git ls-remote origin refs/heads/main
```

작업 브랜치는 `#183` 이전 시점의 main에서 잘려 있어 현재 main보다 여러 커밋 뒤처져 있다.
**과거 판 문서에 적혀 있던 main/배포 상태는 이미 낡았으니 현재 사실로 인용하지 말 것.**

### 저장소 함정 하나

작업 PC 클론의 `remote.origin.fetch`가 **한 브랜치 refspec으로 고정**돼 있어
`git fetch origin`을 해도 `origin/main`이 갱신되지 않는다.
로컬 `origin/main`을 믿지 말고 위 `ls-remote`로 확인할 것.

## 1. 무엇을 고쳤나

출발점은 "GO로 판단했는데 판단 메뉴에서 그 건이 보이지 않는다"였다.

확인해 보니 판단 자체는 정상 저장돼 있었고, 공고가 **입찰마감을 지나면서**
`진행 건`(입찰마감 전 전용) 큐에서 빠져 `result_missing_decided`(결과 입력) 큐로
이동한 것이었다. 판단 메뉴에는 그 이동을 알려주는 항목이 없어 사라진 것처럼 보였다.

그 과정에서 **더 큰 결함**이 드러났다.

### 파이프라인 큐 4개가 클릭해도 동작하지 않았다

`VIEW_ROUTE_MAP`에 작업 파이프라인 큐 4개
(`pending-decision`, `in-progress`, `urgent-in-progress`, `result-missing-decided`)의
경로가 없어서 `normalizeFrontendView`가 넷 다 `"all"`로 바꿔 버렸다.

- KPI 카드 하이라이트는 원본 인자를 쓰므로 **카드는 눌린 것처럼 보인다**
- 목록 필터는 `state.currentView`(= `"all"`)를 보므로 **아무것도 걸러지지 않는다**

결과적으로 그 네 큐에 속한 공고에 도달할 방법이 없었다. 수정 전 코드로 확인한 증거:

```
setView("result-missing-decided")  ⇒  state.currentView === "all"
```

## 2. 코드 변경 (구현 커밋 1개)

코드 변경은 커밋 하나에 모여 있고, 나머지는 문서 커밋이다.

변경 파일 6개:

```
src/pai_loop/static/index.html
src/pai_loop/static/app.js
src/pai_loop/app_access.py
tests/test_frontend_public_contract.py
tests/test_teams_sidebar_runtime.py
tests/test_top_navigation_frontend.py
```

내용:

1. `index.html` — 판단 그룹에 `결과 입력` 항목 추가
   (`data-view="result-missing-decided"`, `id="navResultEntryCount"`). 위치는 `진행 건` 바로 아래.
2. `index.html` — `진행 건`의 aria-label을 `"GO로 결정한 입찰마감 전 공고 수"`로 수정.
   기존 문구(`"...결과를 기록하지 않은 공고 수"`)는 마감된 건까지 포함하는 것처럼 읽혀
   이번 혼란의 직접 원인이었다.
3. `app.js` — `titles["in-progress"]` 부제를
   `"GO로 결정한 입찰마감 전 공고. 마감되면 결과 입력으로 넘어갑니다."`로 수정.
4. `app.js` — `VIEW_ROUTE_MAP` / `ROUTE_VIEW_MAP`에 경로 등록:
   `/pending-decision`, `/in-progress`, `/urgent-in-progress`, `/result-entry`.
5. `app.js` — `isNoticeListView`에 `...PIPELINE_QUEUES` 추가 (`?notice=` 딥링크 대응).
6. `app_access.py` — `FRONTEND_PATHS`에 위 4개 경로 추가 (새로고침 · 공유 링크 대응).
7. `app.js` — `renderNavigationCounts()`에서
   `navResultEntryCount` ← `state.dashboard.resultMissingDecidedCount` 렌더.

### 추가한 회귀 테스트 2건

- `tests/test_top_navigation_frontend.py::test_work_pipeline_queues_keep_their_own_view_instead_of_falling_back_home`
  — 파이프라인 큐 4개가 `"all"`로 되돌아가지 않는지
- `tests/test_teams_sidebar_runtime.py` — 신규 경로 4개가 프론트엔드를 서빙하는지

## 3. 코드를 다른 PC로 옮기는 방법 (택1, 둘 다 미실행)

**A. 작업 브랜치 푸시 (권장)** — 작업 PC에서 현재 main 위로 리베이스 후 푸시:

```bash
git ls-remote origin refs/heads/main          # 최신 main SHA 확인
git fetch origin <main SHA>
git rebase <main SHA>
git push -u origin feat/decision-result-entry-0921
```

리베이스 충돌은 없을 것으로 예상한다. 관측 시점 기준으로 `#185`~`#187`이
이 작업이 건드린 6개 파일을 전혀 건드리지 않았다. (실제 리베이스는 아직 하지 않았다.)

**B. 패치 파일** — 푸시를 미룰 때:

```bash
git format-patch <base>..HEAD -o <출력 폴더>
```

코드만 필요하면 구현 커밋 하나만 옮겨도 된다.

## 4. 테스트 결과

- **34개 파일 414건 통과, 실패 0건.** 전부 종료 코드 0, 타임아웃 없음.
  대상은 `frontend|dashboard|decision|nav|route|login|access|queue|result|kpi|uiux|public`
  이름을 가진 테스트 파일.
- 핵심 7개 파일만 따로 돌린 값은 **131건 통과**:
  `test_frontend_public_contract`, `test_top_navigation_frontend`,
  `test_teams_sidebar_runtime`, `test_dashboard_work_queues`,
  `test_result_entry_frontend`, `test_required_app_login`, `test_required_app_login_frontend`.
- **미실행: 나머지 132개 파일** (전체 166개 중).

### 실행 시 주의 2가지

1. **`-q`를 직접 주지 말 것.** `pyproject.toml`이 이미 `addopts = "-q ..."`를 주기 때문에
   `-q`를 또 붙이면 `-qq`가 되어 **pytest가 마지막 합계 줄을 아예 출력하지 않는다.**
   집계를 스크립트로 긁는다면 통과한 파일을 전부 실패/타임아웃으로 오분류하게 된다.
2. **node가 필요하다.** 프론트엔드 계열 테스트는 `node`로 하니스를 돌린다.
   PATH에 node가 없으면 `subprocess` 단계에서 전부 실패하는데, 이는 환경 문제이지 회귀가 아니다.
   파일명이 `*_frontend`가 아닌 것들도 해당한다(예: 대시보드 큐 테스트).

## 5. 미완료 — 브라우저 실물 확인

**자동 테스트만 돌렸고 실제 화면 클릭 확인은 하지 못했다.** 부서 계정 로그인이 필요했기 때문이다.
이어받는 쪽에서 아래를 확인할 것. **전부 이전에는 동작하지 않던 것들이다.**

- [ ] 판단 메뉴에 `결과 입력` 항목과 건수가 보이는가
- [ ] `결과 입력` 클릭 → 제목이 `결과 입력`으로 바뀌고 목록이 해당 큐로 좁혀지는가
- [ ] `진행 건` 클릭 → 대시보드로 튕기지 않고 실제로 필터링되는가
- [ ] KPI 카드 ①②③ 도 이제 목록을 거르는가
- [ ] 주소창에 `/result-entry` 직접 입력 후 새로고침 → 로그인 화면으로 튕기지 않고 화면이 유지되는가
- [ ] 판단 드롭다운이 4개 항목으로 늘어나도 레이아웃이 깨지지 않는가

## 6. 이어받는 순서

1. `git ls-remote origin refs/heads/main`으로 현재 main 확인.
2. 3절 A 또는 B로 구현 코드 이동.
3. 4절 주의사항을 지켜 테스트 재실행 (최소 핵심 7개 파일).
4. 5절 브라우저 확인.
5. 통과하면 PR 생성 → 현재 main 기준. 배포는 머지 후 기존 파이프라인대로.
6. 7절 별건은 **별도 브랜치**로 분리. 이 브랜치에 섞지 말 것.

## 7. 별건 — 부서 키워드 프로필 점검 필요

조사 중, 한 부서의 키워드 프로필이 현재 열린 공고를 사실상 잡아내지 못해
판단 대기 큐가 구조적으로 비는 상태를 확인했다.

**매처 코드는 정상이다.** 프로필이 정확한 연결어를 요구하는데 실제 공고 제목의 표현이
그와 다른 것이 원인이며, 따라서 코드 수정이 아니라 `department_keyword_profiles.json`의
키워드 설계 문제다. 일부 키워드는 열린 공고 제목에 전혀 등장하지 않는 사실상 사문화된 항목이다.

어떤 부서인지, 어떤 키워드가 몇 건을 잡는지 등 구체 수치는 운영 진단값이므로
이 공개 문서에서는 생략한다. 비공개 원본 인수인계서를 참고할 것.

이 건은 "그 부서가 어떤 일을 보게 되는가"를 바꾸는 결정이므로 임의로 수정하지 않았고,
이번 브랜치에도 포함하지 않았다. 담당자 확인 후 별도로 진행할 것.

## 8. 이어받을 때 쓸 프롬프트

```text
PAI_LOOP 작업을 이어서 한다. 먼저 저장소 브랜치
docs/decision-result-entry-handoff-20260921 의
docs/DECISION_RESULT_ENTRY_HANDOFF_20260921.md 를 읽어라.

판단 메뉴에 '결과 입력' 큐를 추가하고, 동작하지 않던 작업 파이프라인 큐 4개의
라우트를 복구한 작업이다. 34개 파일 414건 통과했고 실패는 없다.
구현 코드는 아직 원격에 없다 — 문서 3절의 이동 절차가 먼저다.

진행 시 주의:
- main 상태는 반드시 `git ls-remote origin refs/heads/main` 으로 직접 확인할 것.
  작업 PC 클론은 remote.origin.fetch 가 한 브랜치에 고정돼 있어 로컬 origin/main 이
  갱신되지 않는다. 문서에 적힌 main/배포 상태는 관측 시점 값이므로 현재 사실로 쓰지 마라.
- pytest 에 -q 를 추가로 주지 마라 (pyproject addopts 와 합쳐져 -qq 가 되면
  합계 줄이 사라진다). 프론트엔드 테스트에는 node 가 필요하다.
- 문서 7절의 부서 키워드 프로필 건은 별도 브랜치로 분리하고 이 브랜치에 섞지 마라.

병합·배포·운영 데이터 변경은 내 확인 없이 시작하지 마라.
```
