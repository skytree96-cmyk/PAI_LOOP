# PAI_LOOP 온라인 배포 전략 v0.2.0

기준일: 2026-08-17  
대상: 공모전 공개 데모 이후 전사 웹 서비스  
이 문서는 v0.1.0을 수정하지 않고, 온라인 데이터 전환과 현재 UI 유지 결정을 추가한다.

## 결론

현재의 **FastAPI + 정적 SPA를 하나의 컨테이너로 배포하고, 실행 데이터는
관리형 PostgreSQL에 저장**한다. Streamlit Community Cloud로 UI를 다시 만드는
방안은 채택하지 않는다.

```text
사용자 브라우저
    │ HTTPS + 사용자 인증/권한
    ▼
FastAPI + 현재 SPA (동일 출처)
    │
    ├── 관리형 PostgreSQL  ← 공고·낙찰·회사 공개 증빙·추천·결정·감사
    ├── 관리형 객체 저장소 ← 공고 첨부파일·추출 원문(보존 정책 적용)
    ├── 조달청/OpenAI API
    └── n8n               ← 서버용 자격증명으로 수집·갱신

GitHub
    └── 소스·스키마·규칙·부서별 키워드·공개 가능한 정제 seed만 보관
```

따라서 전사 사용자가 각 PC의 SQLite나 엑셀 파일을 읽는 구조는 허용하지
않는다. 서버 재시작, 재배포, 사용자의 PC 상태와 관계없이 동일한 온라인
데이터를 보게 한다.

## 온라인 데이터 경계

| 위치 | 저장 대상 | 저장 금지/주의 |
| --- | --- | --- |
| GitHub | 애플리케이션 코드, DB 마이그레이션, 판정 규칙, 부서/역할별 검색 키워드 registry, 공개 출처와 라이선스가 확인된 정제 seed, seed 버전·해시 | API 키, 사용자 결정/감사 로그, 실행 중 생성되는 DB, 원본 사내 파일, 비공개 개인정보 |
| 관리형 PostgreSQL | 공고, 추출 결과, 공개 회사 자격·실적, 부서별 추천 결과, 낙찰 후보, 검토 상태, 사용자 결정, 변경 이력 | 원문 바이너리, provider 원본 응답 전체, 불필요한 직접식별자 |
| 관리형 객체 저장소 | 허용된 공고 첨부파일과 필요한 공개 증빙, 콘텐츠 해시, 보존·삭제 정책 | 공개 버킷, 만료 없는 임시 파일, 브라우저에 직접 노출되는 비공개 문서 |
| 배포 플랫폼/n8n secret store | DB URL, PPS/OpenAI/n8n 자격증명, 서버 간 API 자격증명 | GitHub 저장소, 프런트엔드 JS, 로그·워크플로 export |

공개된 회사 실적과 자격요건도 **온라인 DB의 관리 대상**이다. GitHub의
정제 seed는 최초 적재와 재현을 위한 원본이며, 이후 검토 상태·갱신 시각·
사용자 결정은 DB가 기준 시스템(system of record)이 된다. seed 적재는
`seed_version + natural_key` 기반 upsert로 중복 없이 실행하고, 삭제는
자동 동기화하지 않는다.

## 선택지 비교

| 선택지 | 현재 UI 유지 | 영속 데이터 | 인증/운영 | 판단 |
| --- | --- | --- | --- | --- |
| Streamlit Community Cloud 단독 | 불가에 가까움. Streamlit 화면으로 다시 작성해야 함 | 로컬 파일 지속성을 보장하지 않으므로 외부 DB 필수 | secret UI와 OIDC는 있으나 OIDC는 인증만 제공하고 권한은 앱에서 구현해야 함 | **미채택** |
| FastAPI 컨테이너 + 관리형 PostgreSQL | 그대로 유지 | PostgreSQL/객체 저장소에 영속 | 동일 출처, 한 번의 배포, SSO/BFF를 한 경계에 적용 가능 | **권장** |
| Streamlit 프런트 + 별도 FastAPI | 재작성 또는 iframe 필요 | 외부 DB/API 필수 | 배포·CORS·세션·권한 경계가 두 개가 됨 | 현재 제품에는 이점 없음 |

### Streamlit Community Cloud가 이번 제품의 호스트가 아닌 이유

- Community Cloud는 저장소 루트에서 `streamlit run`을 실행하고 Python
  entrypoint를 배포한다. 임의의 FastAPI/uvicorn 컨테이너를 호스팅하는
  서비스가 아니다.
- Community Cloud는 로컬 파일의 지속성을 보장하지 않으며 언제든 삭제될
  수 있다고 명시한다. SQLite를 온라인 기준 데이터로 쓸 수 없다.
- secret은 GitHub가 아니라 앱의 Advanced settings에 넣을 수 있고, private
  repository는 추가 GitHub `repo` OAuth 권한과 deploy key가 필요하다.
- private 앱은 현재 계정당 1개 제한이 있고, 이메일로 viewer를 초대하는
  공유 모델이다. private 앱 iframe은 공식 지원하지 않는다.
- `st.login()`은 Microsoft Entra ID 등 OIDC 인증을 지원하지만, 공식 문서도
  OIDC가 **authorization을 제공하지 않는다**고 구분한다. 부서별 역할과
  승인 권한은 별도 구현해야 한다.

Streamlit은 새로운 분석 화면을 빠르게 실험하는 보조 앱에는 적합하다.
그러나 이미 완성된 동일 출처 SPA/API를 유지하려면 중복 계층이 된다.

공식 근거:

- [Community Cloud 파일 구성과 `streamlit run`](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/file-organization)
- [Community Cloud 로컬 파일 비영속성](https://docs.streamlit.io/develop/concepts/connections/connecting-to-data#a-simple-starting-point---using-a-local-sqlite-database)
- [Community Cloud secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
- [private repository 권한](https://docs.streamlit.io/deploy/streamlit-community-cloud/get-started/connect-your-github-account)
- [public/private 공유와 private 앱 제한](https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app)
- [Streamlit OIDC 인증 범위](https://docs.streamlit.io/develop/concepts/connections/authentication)

## FastAPI 호스트 비교

### 1. Render — 공모전 준비가 가장 단순

- GitHub/Docker 기반 FastAPI Web Service, managed TLS, health check, 자동
  배포를 지원한다.
- 무료 Web Service는 15분 무트래픽 후 sleep하고, 다시 뜨는 데 약 1분이
  걸릴 수 있다. 파일시스템 변경은 재배포·재시작·sleep 때 사라진다.
- 무료 Render Postgres는 1GB, 30일 만료, 백업 없음이다. 따라서 공모전
  단기 리허설 외에는 유료 DB 또는 다른 영속 managed DB가 필요하다.
- 추천 용도: **짧은 공모전 공개 데모**. 심사 직전 cold start를 피하고,
  DB 만료일과 별도 백업을 기록한다.

공식 근거: [FastAPI 배포](https://render.com/docs/deploy-fastapi),
[무료 인스턴스 제약](https://render.com/docs/free),
[HTTP health check](https://render.com/docs/health-checks),
[환경변수와 secret](https://render.com/docs/configure-environment-variables).

### 2. Railway — 작은 유료 데모의 편의성이 좋음

- GitHub 또는 Dockerfile에서 FastAPI를 바로 배포하고, 같은 프로젝트에
  PostgreSQL 템플릿과 volume을 붙일 수 있다.
- 2026-08-17 공식 가격 기준 Free는 월 $1 사용 크레딧, Hobby는 월 $5의
  포함 사용량 구조다. DB 템플릿은 공식 문서상 사용자가 운영하는
  **unmanaged** 구성이라 백업·복구를 직접 점검해야 한다.
- 추천 용도: 카드 등록이 가능하고 한 프로젝트에서 앱/DB를 빠르게 다룰
  때의 공모전 데모 또는 소규모 파일럿.

공식 근거: [FastAPI 배포](https://docs.railway.com/guides/fastapi),
[PostgreSQL](https://docs.railway.com/databases/postgresql),
[volume](https://docs.railway.com/volumes/reference),
[가격](https://docs.railway.com/pricing/plans).

### 3. Google Cloud Run — 조직 통제와 확장에 유리

- FastAPI 컨테이너를 공식 지원하고, 요청 기반 scale-to-zero와 IAM을
  제공한다. 서울 리전도 가격표에 포함되어 있다.
- 쓰기 가능한 컨테이너 파일시스템은 인스턴스 메모리를 사용하므로 DB
  저장소가 아니다. Cloud SQL 또는 승인된 외부 managed PostgreSQL을 쓴다.
- 요청 기반 서비스에는 월 무료 사용량이 있지만 billing account가 필요하고,
  Cloud SQL·Artifact Registry·네트워크 비용은 별도로 계산한다.
- 추천 용도: 회사의 Google Cloud 조직/IAM/보안 운영이 준비된 전사 파일럿.
  설정량은 Render/Railway보다 많다.

공식 근거: [FastAPI quickstart](https://docs.cloud.google.com/run/docs/quickstarts/build-and-deploy/deploy-python-fastapi-service),
[container filesystem 계약](https://docs.cloud.google.com/run/docs/container-contract),
[Cloud SQL 연결](https://docs.cloud.google.com/sql/docs/postgres/connect-run),
[Cloud Run 가격](https://cloud.google.com/run/pricing).

## 권장 단계

### A. 공모전 공개 데모

1. 현재 컨테이너를 Render 또는 Railway에 배포한다.
2. SQLite가 아닌 managed PostgreSQL을 연결한다.
3. GitHub 정제 seed를 최초 1회 upsert하고, n8n이 이후 데이터를 갱신한다.
4. 공개 화면은 공개 가능한 GET 데이터만 제공하고 변경 작업은 모두
   서버 인증을 요구한다.
5. 공개 프로필과 내부 프로필을 분리한다. 공개 프로필에는 마스킹된 인력,
   공개 실적·자격, 공개 공고·낙찰 사실만 포함한다.
6. 장애 시 사용할 정적 캡처가 아니라, 같은 온라인 DB를 읽는 고정
   demonstration notice를 준비한다.

### B. 전사 파일럿

1. 회사가 승인한 cloud/region과 managed PostgreSQL/object storage를 선택한다.
2. Entra SSO 또는 회사 access proxy를 앞단에 두고 `viewer`, `reviewer`,
   `approver`, `admin` 권한을 서버에서 검사한다.
3. n8n에는 사용자 계정이 아닌 service identity를 발급한다.
4. schema migration, point-in-time recovery, 보존/삭제, 감사 로그, 비밀 회전,
   모니터링과 경보를 통과시킨다.
5. 이후 동일 웹 URL을 Teams tab으로 연결한다. Teams 봇/알림은 이 웹의
   보조 진입점이며 기준 데이터 저장소가 아니다.

## 현재 코드에 적용한 최소 준비 변경

- 컨테이너가 호스트가 주입하는 `PORT`를 사용하도록 변경했다.
- production 이미지에 `psycopg[binary]`를 설치한다.
- provider가 주는 `postgres://`/`postgresql://` URL을 SQLAlchemy psycopg v3
  URL로 안전하게 정규화한다.
- `/healthz` 컨테이너 health check도 같은 `PORT`를 사용한다.
- `.dockerignore`로 secret, `.local`, SQLite, Office/PDF, build 산출물을 cloud
  build context에서 제외한다.
- `PAI_LOOP_PUBLIC_READ_ONLY=true`에서는 명시적으로 허용한 공개 GET만 익명
  조회할 수 있고, 모든 write/internal route는 서버 API key가 필요하다.
- 실제 공개 인천 공고 seed는 `pai-loop-seed-public-notice --create-schema`로
  멱등 적재하며, 자동 startup seed나 로컬 원본 의존이 없다.
- 공개 실적·회사 자격·부서 키워드는 버전·digest를 검증하는 wheel 자산이다.
- 루트 [`render.yaml`](../render.yaml)은 Singapore 무료 Web Service/PostgreSQL,
  production 안전 기본값, 최초 seed hook, CI 통과 후 자동 배포를 선언한다.

Docker만으로 Render, Railway, Cloud Run 중 어느 하나를 선택할 수 있다.
저장소의 Blueprint는 재현 가능한 배포 명세일 뿐이며, 실제 외부 계정/OAuth와
리소스 생성은 별도 승인 작업이다.

## 배포 전 필수 보안 게이트

현재 `PAI_LOOP_API_KEY`는 n8n 같은 **서버 간 호출용**이다. 브라우저 JS에
넣으면 안 된다. production 모드는 이 키가 없거나 SQLite를 사용하거나 합성
자동 seed가 켜져 있으면 시작하지 않는다.

1. 공모전 배포: 구현된 public-read-only GET allowlist + 응답 필드 allowlist +
   write route 서버 인증. 쓰기 UI도 비활성화한다.
2. 회사 배포: Entra SSO/access proxy + 서버 세션/BFF + RBAC. public-read-only는
   이 인증을 대체하지 않으며 전사 운영에서는 끈다.

추가 게이트:

- [ ] 로컬 SQLite 경로가 deployment 환경변수에 존재하지 않는다.
- [ ] 원격 PostgreSQL 연결은 TLS이며 최소 권한 계정을 사용한다.
- [ ] DB migration을 별도 job으로 실행하고 웹 인스턴스의 동시 `create_all`
  의존을 제거한다.
- [ ] 공개 seed가 직접식별자/secret/내부 메모/원문 바이너리를 포함하지 않는다.
- [ ] 첨부파일은 private bucket + 짧은 signed URL + 보존기한을 사용한다.
- [ ] CORS는 실제 origin만 허용하고, cookie write에는 CSRF 방어를 적용한다.
- [ ] rate limit, OpenAI/PPS 비용 상한, n8n retry/idempotency를 검증한다.
- [ ] 유료 managed DB backup/restore를 실제로 연습한다.
- [ ] 로그에서 API 키, DB URL, 문서 본문, 개인식별자를 redaction한다.
- [ ] GitHub main CI 통과 후에만 자동 배포하고 이전 image rollback을 확인한다.

## 필수 runtime 설정

```text
PAI_LOOP_ENV=production
PAI_LOOP_DATABASE_URL=postgresql://...   # 플랫폼 secret/reference
PAI_LOOP_API_KEY=...                     # n8n 등 서버 간 호출 전용
PPS_API_KEY=...
OPENAI_API_KEY=...
PAI_LOOP_PUBLIC_READ_ONLY=true           # 공모전 공개 데모만
PAI_LOOP_SEED_SYNTHETIC=false
```

`PORT`는 호스팅 플랫폼이 주입한다. 값은 GitHub에 넣지 않고, 배포 플랫폼의
secret/environment reference와 n8n credential store로 관리한다.
