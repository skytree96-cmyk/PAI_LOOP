# 개인 관심 공고 알림 앱

현재 `PAI-LOOP-Teams-App.zip`은 기존 채널 탭 패키지입니다. 개인 봇은 실제
Microsoft Entra / Azure Bot 등록 정보를 받은 뒤 별도 패키지로 생성합니다.
이 구현과 패키지 생성만으로 조직 설치나 실서비스 메시지 발송이 완료되지는 않습니다.

## 다른 PC에서 이어서 설정하기

조직 관리자 계정으로 [Microsoft의 Azure 등록 안내](https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/teams/azure-configuration)를
따라 단일 테넌트 앱과 봇을 신규 등록합니다. App ID와 Tenant ID를 확인하고,
클라이언트 암호는 서버 배포 플랫폼의 비밀 설정에 직접 저장합니다.
PC를 계속 켜두는 방식이 아니라 상시 실행 서버가 예약 알림을 처리합니다.

아래 배포 설정을 완료한 뒤, 이 브랜치를 받은 PC의 저장소 루트에서 실행합니다.
꺾쇠 안의 두 값은 실제 등록·배포 값으로 바꿉니다. Python 3.11 이상이면
패키지 생성에 추가 라이브러리가 필요하지 않습니다.

```powershell
python scripts/build_teams_personal_app.py --bot-app-id "<실제 Application ID>" --public-base-url "https://<실제 앱 도메인>" --output "../PAI-Teams-Personal.zip"
```

출력 ZIP을 Teams 관리 센터에 업로드하고 본인에게 개인 앱을 설치합니다.
앱의 **관심 공고 · Teams 개인 알림 → 개인 알림 연결**에서 만든 명령을
봇의 개인 대화에 붙여 넣고, 앱에서 연결 상태를 새로고침합니다.
본인이 선택한 공고 한 건을 관심 등록한 뒤 개인 채팅 수신과 앱 발송 상태를
함께 확인하면 설치 후 첫 검증이 됩니다. 알림 시간과 워커 실행은
[운영 안내](../docs/TEAMS_PERSONAL_FOLLOWUPS.md)를 따릅니다.

## 배포 설정

1. 조직의 Microsoft Entra 테넌트에 단일 테넌트 앱과 Azure Bot을 등록합니다.
   Azure Bot에 Microsoft Teams 채널을 추가하고 개인 앱 설치를 허용합니다.
2. 배포 플랫폼의 비밀 설정으로 아래 값을 지정합니다. 앱 암호를 저장소,
   패키지, 브라우저 코드, 명령줄 또는 로그에 넣지 않습니다.
   - `PAI_TEAMS_BOT_APP_ID`: 등록한 봇의 Microsoft 앱 UUID
   - `PAI_TEAMS_BOT_APP_SECRET`: 해당 앱의 유효한 클라이언트 암호 값
   - `PAI_TEAMS_TENANT_ID`: 허용할 조직 테넌트 UUID
   - `PAI_TEAMS_PUBLIC_BASE_URL`: 앱을 제공하는 실제 HTTPS origin
   - `PAI_TEAMS_TAB_AUTH_ENABLED=true`: HTTPS Teams 탭에서 로그인 쿠키를
     사용할 때만 활성화합니다. SameSite=None, Secure 쿠키를 사용하며 기존
     동일 출처 및 CSRF 검증을 유지합니다. 브라우저가 서드파티 쿠키를 차단하면
     앱에서 제공하는 브라우저 열기 경로로 로그인합니다.
3. Azure Bot 메시지 엔드포인트를 실제 origin의
   `/api/v1/teams/messages`로 설정합니다. 이 경로는 브라우저 쿠키나 API 키
   대신 Bot Connector의 서명된 JWT로 인증합니다. 일반 API의 계정 보호를 유지합니다.
4. 애플리케이션의 additive migration을 적용합니다. 개인 수신자, 세션 연결,
   일회용 코드, 관심 등록 및 발송 큐 테이블이 필요합니다.
5. `scripts/build_teams_personal_app.py`에 실제 `--bot-app-id`,
   `--public-base-url`, `--output` ZIP 경로를 전달하여 패키지를 생성합니다.
   출력 ZIP은 manifest와 기존 두 아이콘을 포함합니다. 기존 패키지를 덮어쓰지 않습니다.
   생성기는 [공식 v1.28 스키마](https://developer.microsoft.com/json-schemas/teams/v1.28/MicrosoftTeams.schema.json)에서
   허용하지 않는 이전 `packageName`, `configurableTabs.supportedPlatform` 속성을 제거합니다.
6. Teams 관리 센터에서 패키지를 조직 앱으로 업로드하고 대상 사용자에게
   개인 앱 설치를 허용합니다. 사용자 개인 범위에 설치되어야 DM이 가능합니다.
7. 앱에 로그인하고 개인 알림 연결 화면의 `연결 CODE` 전체 명령을 복사해
   앱 봇의 개인 대화에 붙여 넣습니다. 앱 화면에서 연결 완료를 확인합니다.
   이후 공고별 관심 등록을 하면 등록자 본인의 개인 대화가 수신 대상이 됩니다.

## 신원 및 운영 경계

- 기존 계정은 부서 공동 계정이므로 계정 ID를 개인 Teams 사용자로 취급하지 않습니다.
  연결은 현재 로그인 **세션**과 Teams에서 검증한 테넌트·사용자 ID에 묶입니다.
  재로그인 또는 세션 만료 뒤에는 다시 연결해야 기존 개인 관심 공고를 관리할 수 있습니다.
  로그인 세션 만료 자체는 이미 동의한 개인 알림 예약을 중단하지 않습니다.
- 코드는 192비트 무작위 값이며 10분 만료, 한 번 사용 후 폐기됩니다.
  DB에는 해시만 저장하고 이전 코드는 재발급 시 무효화합니다. 코드를 다른 사람에게
  전달하면 그 사람의 Teams가 연결되므로 본인의 개인 봇 대화에만 붙여 넣습니다.
- 연결 해제 또는 봇 제거 시 수신자를 비활성화하고 연결 세션들을 해제합니다.
  해당 수신자의 큐는 실제 발송 직전 활성 상태를 확인해야 합니다.
- 브라우저에서 보낸 Teams context, 사용자 ID, 이메일 또는 대화 ID로는 연결하지 않습니다.
  알림 대상의 ID나 대화 참조는 브라우저 응답에 포함하지 않습니다.
- 현재는 Microsoft public cloud의 `smba.trafficmanager.net` 서비스 주소만 허용합니다.
  GCC/GCC High/DoD는 별도 인증·서비스 주소 검증 구현 전까지 지원하지 않습니다.
- 토큰은 고정 Microsoft 로그인 엔드포인트에서 받고, 메시지는 검증한 Connector
  주소에만 전송합니다. HTTP redirect를 따라가지 않습니다.
- 메시지 전송 뒤 timeout 또는 연결 단절은 `UNKNOWN`입니다. 실제 전달되었을
  가능성이 있어 자동 재전송하지 않고 운영 확인 대상으로 보존합니다.
  429/5xx는 제한된 재시도 대상이며 영구 4xx는 실패로 남깁니다.
- 연결 콜백은 분석 정보나 코드 내용으로 답장하지 않습니다. 연결 완료 여부는 앱에서
  확인합니다. 첫 실제 메시지는 사용자가 공고를 관심 등록했을 때 예약됩니다.

## 확인 근거

Microsoft의 [Bot Connector 인증 절차](https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication?view=azure-bot-service-4.0)에 따라
RS256 서명, 고정 issuer, 앱 audience, 만료·시작 시각, `serviceUrl` 일치 및
`msteams` key endorsement를 확인합니다. 단일 테넌트 토큰 발급도 이 절차를 따릅니다.
실제 JWT의 서비스 주소 클레임은 Microsoft의
[ChannelValidation 구현](https://github.com/microsoft/botbuilder-python/blob/main/libraries/botframework-connector/botframework/connector/auth/channel_validation.py)에서
사용하는 소문자 `serviceurl`을 지원하며, 문서의 `serviceUrl` 별칭도 서명 검증 뒤
받습니다. 두 키가 함께 있으면 모두 Activity의 `serviceUrl`과 일치해야 합니다.

[Teams proactive messages](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages)에 따라 개인 범위 설치와
수신 대화 참조가 필요하며, 실제 수신자 참조는 인증된 개인 봇 메시지에서만 저장합니다.
