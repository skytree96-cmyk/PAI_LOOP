# PAI LOOP Teams Custom Tab

`teams-app/PAI-LOOP-Teams-App.zip`은 기존 Render 웹앱을 Teams 채널·그룹 채팅의
configurable tab으로 추가하는 사내 배포 패키지입니다. 앱은
`https://pai-loop-demo.onrender.com/`을 그대로 사용하며 별도 서버나 데이터 복제본을
만들지 않습니다.

## 패키지 재생성

```powershell
python scripts/build_teams_app.py
```

생성물에는 Teams 규격의 `manifest.json`, 192×192 `color.png`, 투명 배경 32×32
`outline.png`만 ZIP 최상위에 포함됩니다. manifest의 `validDomains`에는 Render 호스트와
TeamsJS 공식 CDN만 등록합니다.

## 회사 Teams에 업로드하고 채널 탭으로 추가

1. Microsoft Teams에서 **앱 → 앱 관리 → 앱 업로드 → 사용자 지정 앱 업로드**를 엽니다.
2. `teams-app/PAI-LOOP-Teams-App.zip`을 선택합니다. 회사 정책으로 업로드 메뉴가 없으면
   Teams 관리자에게 같은 ZIP의 조직 앱 카탈로그 등록을 요청합니다.
3. PAI LOOP를 설치한 뒤 대상 채널 또는 그룹 채팅의 상단 **+** 버튼을 누릅니다.
4. **PAI LOOP**를 선택하면 설정 화면이 iframe으로 열립니다.
5. “준비되었습니다” 문구를 확인하고 **저장**을 누릅니다.
6. 탭 안에서 목록의 공고명 또는 추천 우측 화살표를 눌러 상세 패널이 같은 Teams 탭
   안에서 열리는지 확인합니다. 조달청 원문은 사용자가 확인 버튼을 누를 때만 명시적으로
   새 창을 엽니다.

## iframe·보안 경계

- 모든 응답은 CSP `frame-ancestors`로 자기 자신, `teams.microsoft.com`,
  `*.teams.microsoft.com`, `*.cloud.microsoft`만 허용합니다.
- `X-Frame-Options: DENY|SAMEORIGIN`은 Teams iframe을 막으므로 설정하지 않습니다.
- TeamsJS 2.19.0을 초기화하고, 앱 내부 상세 이동은 URL query + History API로 처리해
  Teams 탭을 벗어나지 않습니다.
- 현재 탭은 공개 안전 데이터만 표시합니다. Entra SSO/RBAC이 연결되기 전까지 내부 판단
  저장과 민감 로그 조회는 기존 서버 API key 경계를 유지합니다.
- “지금 분석”은 API key를 브라우저에 전달하지 않습니다. same-origin 단일 공고 BFF가
  `force=false`, 첨부 1개, 전역 직렬 처리, 시간당 한도, 공고별 cooldown을 강제합니다.

앱 ID나 서비스 URL을 바꾸면 `teams-app/manifest.json`을 수정한 뒤 ZIP을 재생성하고,
기존 조직 앱의 버전을 올려 다시 업로드합니다.
