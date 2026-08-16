# PAI_LOOP

**Evidence-first public procurement decision intelligence**

PAI_LOOP는 조달 공고를 놓치지 않고, 공고 문서의 참가조건을 근거 위치와
함께 구조화한 뒤, **공고 마감일 당시 유효한 회사 증빙**과 비교하여 담당자의
입찰 의사결정을 돕는 시스템입니다.

LLM은 조건과 근거 후보를 구조화할 뿐입니다. 최종 적격성은 버전이 고정된
규칙 엔진이 `PASS / REVIEW / FAIL`로 판정하고, 실제 `GO / 보류 / NO-GO`는
사람이 근거와 함께 기록합니다.

![PAI_LOOP architecture](docs/architecture/PAI_LOOP_architecture.png)

## 현재 구현 범위: v0.1.0 foundation

- FastAPI + SQLAlchemy 기반 API와 SQLite/PostgreSQL 교체 가능한 저장 경계
- 마감일 기준 증빙, 증빙 Gate, AND/OR 경로, 연결 REVIEW, `DF-000`을 다루는
  결정론적 평가 엔진
- 정량 준비도·증빙 커버리지·리스크와 적격성을 분리한 결과 계약
- 합성 PASS/REVIEW/FAIL 공고를 중복 없이 재생하는 회귀 fixture
- 데스크톱·모바일·Teams iframe을 고려한 반응형 한국어 대시보드
- GitHub에서 이름 기반으로 안전하게 upsert하는 n8n 워크플로 배포
- n8n 안에서 읽을 수 있는 아키텍처와 공고 replay vertical slice
- 실제 조달청 핵심 API 및 OpenAI Responses API 연결 검증

이 버전은 제품 기반과 재현 가능한 세로 슬라이스입니다. 실제 입찰 자격이나
법률 판단을 대신하지 않으며, 실데이터 수집/첨부 추출/Teams 알림은 이후
단계의 운영 승인과 보안 Gate를 통과해야 합니다.

## 빠른 로컬 실행

Python 3.11 이상이 필요합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
$env:PAI_LOOP_SEED_SYNTHETIC="true"
uvicorn pai_loop.main:app --host 127.0.0.1 --port 8000
```

브라우저에서 `http://127.0.0.1:8000`을 열면 됩니다. API 문서는
`http://127.0.0.1:8000/api/docs`, 상태 확인은 `/healthz`입니다.

합성 데이터를 수동으로 넣으려면:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/ingestion/replay
```

합성 fixture에는 `SYN-` 접두사를 사용하며 실제 회사정보나 개인정보를
포함하지 않습니다.

## 테스트

```powershell
pytest --cov=pai_loop --cov-report=term-missing
node scripts/deploy-workflows.mjs --validate-only
```

CI는 Python 테스트, n8n JSON/연결/Code 문법 검증과 공개 저장소용 비밀값
패턴 검사를 실행합니다.

## API baseline

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/healthz` | 서비스/DB 상태 |
| `GET` | `/api/v1/dashboard` | 요약 및 마감 현황 |
| `GET` | `/api/v1/notices` | 검색/필터 목록 |
| `GET` | `/api/v1/notices/{notice_key}` | 근거·평가·결정 상세 |
| `POST` | `/api/v1/notices/{notice_key}/evaluate` | 버전 규칙 재평가 |
| `POST` | `/api/v1/notices/{notice_key}/decisions` | 담당자 결정 기록 |
| `POST` | `/api/v1/ingestion/replay` | 합성 회귀 fixture 재생 |

## n8n 배포

`main`에 `workflows/**`, `manifest.json` 또는 배포 스크립트 변경이 push되면
GitHub Actions가 workflow를 검증하고 n8n에 이름 기준으로 생성/갱신합니다.
manifest에서 `publish: false`인 워크플로는 배포 후에도 비활성 상태를
강제합니다.

필수 GitHub Actions secrets:

- `N8N_BASE_URL`
- `N8N_API_KEY`

OpenAI·조달청 키는 배포 스크립트가 workflow JSON에 넣지 않습니다. n8n
credential store 또는 서버 환경변수에서만 주입합니다. n8n 화면에서 직접
수정했다면 JSON을 먼저 GitHub에 동기화해야 하며, GitHub를 소스 오브
트루스로 유지합니다.

## 배포 방향

공모전과 첫 사내 파일럿은 **독립 웹 대시보드 + n8n + Teams Workflows
알림 카드**를 권장합니다. 상세 표와 3년 낙찰 이력은 웹에서, Teams는
업무 맥락 안의 알림/진입점으로 사용합니다. 동일 웹앱을 이후 Teams 탭으로
패키징할 수 있습니다. 자세한 내용은
[`docs/DEPLOYMENT_STRATEGY_v0.1.0.md`](docs/DEPLOYMENT_STRATEGY_v0.1.0.md)에
있습니다.

## 보안 경계

- `.env`, `secrets.txt`, API 키, Teams webhook URL, 실제 첨부, 내부 데이터행,
  전체 추출문은 Git에 올리지 않습니다.
- OpenAI 입력은 최소 문단만 전송하고 문서 속 지시문을 신뢰하지 않으며,
  strict schema와 근거-anchor 검증을 거칩니다.
- 외부 URL로 공개하기 전에 인증/권한, HTTPS, rate limit, 감사로그와 저장소
  보존정책을 반드시 켭니다.
- 현재 공개 데모에는 합성 데이터만 사용합니다. 내부 회사 증빙은 private
  배포와 RBAC 전에는 로드하지 않습니다.

보안 기준은 [`docs/SECURITY_AND_PRIVACY_v0.1.0.md`](docs/SECURITY_AND_PRIVACY_v0.1.0.md),
OpenAI 계약은
[`docs/OPENAI_EXTRACTION_CONTRACT_v0.1.0.md`](docs/OPENAI_EXTRACTION_CONTRACT_v0.1.0.md)를
참조하세요.

## 설계 문서

- [Product blueprint](docs/PRODUCT_BLUEPRINT_v0.1.0.md)
- [PPS API catalog](docs/PPS_API_CATALOG_v0.1.0.md)
- [Architecture source and n8n embedding](docs/architecture/README.md)
- [Source-handling register](docs/SOURCE_REGISTER_v0.1.0.md)

원본 기획안·업무인수인계·준비목록은 분석 입력으로만 사용했고 수정하거나
저장소에 복사하지 않았습니다. 새 산출물은 버전이 붙은 별도 파일로 관리합니다.
