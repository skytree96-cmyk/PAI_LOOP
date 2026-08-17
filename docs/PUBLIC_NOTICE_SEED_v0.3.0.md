# PAI_LOOP 실제 공고 공개 시드 v0.3.0

## 목적

공모전 및 전사 온라인 환경에서 로컬 PC 없이도 실제 공고 분석 화면을 재현한다. 대상은
`MANUAL-INCHON-2025-17`로 관리한 인천광역시인재개발원 공고 1건이다. 데이터 분류는
`PUBLIC_PROCUREMENT_DERIVED`이며 합성 데이터가 아니다.

공개 시드는 다음 실제 공개정보만 포함한다.

- 공고 식별자, 제목, 발주기관, 게시일, 마감일, 공고 분류
- API 키가 없는 공식 나라장터 URL
- 최신 `ACCEPTED` OpenAI 추출의 요약
- 구조화 공고 요구조건 23건과 공고문 근거 앵커 26건
- 원문 라벨, 문서 SHA-256, 추출 버전·신뢰도 등 출처 메타데이터

## 공개 금지 경계

다음 항목은 빌드 쿼리에서 선택하지 않으며 시드 스키마에도 필드가 없다.

- 원본 DB, 원문 문서 파일, 로컬 파일 경로
- 담당자·대표자 이름, 전화, 이메일, 주소, 등록번호
- 회사 `actual_value`, 회사 fact, 증빙 원문
- 사용자 결정, 담당자 의견, 근거 메모
- OpenAI response ID, API 키, GitHub/n8n/조달청 자격증명

빌더는 SQLite를 `mode=ro`와 `query_only`로 열고 고정 allowlist만 투영한다. 생성 후
PII·로컬 경로·일반적인 비밀값 패턴을 스캔하고, 전체 공개 페이로드의 SHA-256을 기록한다.
검사 실패 시 파일을 생성하지 않는다.

## 재생성

원본은 `.local` 아래에 두며 Git에 올리지 않는다. 명령은 공고문이나 근거 인용을 터미널에
출력하지 않고 건수와 digest만 출력한다.

```powershell
python tools/build_public_notice_seed.py `
  --source-db .local/pai_loop_app.db `
  --output src/pai_loop/data/public_notice_seed_v1.json
```

기대 집계는 요구조건 23건, 근거 앵커 26건, 개인정보 탐지 0건이다.

## 온라인 DB 명시적 적재

애플리케이션 시작 시 자동 적재하지 않는다. 관리형 PostgreSQL 스키마가 준비된 뒤 운영자가
한 번 명시적으로 실행한다. 연결 문자열은 명령행 인수가 아니라 호스팅 환경의 비밀 환경변수로
전달한다.

```powershell
$env:PAI_LOOP_DATABASE_URL = '<managed-postgresql-url>'
pai-loop-seed-public-notice
```

이 entrypoint는 `src`와 패키지 데이터가 복사·설치되는 Docker 이미지 안에서도 동일하게
실행된다. `tools/import_public_notice_seed.py`는 로컬 개발에서 같은 packaged entrypoint를
호출하는 얇은 wrapper다. 빈 데모 DB에서만 `--create-schema`를 사용할 수 있다. 같은 명령을 반복해도 공고와 추출 버전은
중복되지 않는 멱등 upsert다. 기존 사용자 결정·회사 fact·평가는 생성하거나 수정하지 않는다.

## 정책 화면 회귀 기준

적재 직후 `GET /api/v1/notices/MANUAL-INCHON-2025-17/analysis/requirement-policy`는
현재 공개 회사 프로필과 함께 다음 구성을 재현한다.

| 구분 | 건수 | 의미 |
|---|---:|---|
| 적격성 | 6 | 회사 근거와 마감일 기준을 확인 |
| 행동 필요 | 1 | 제안설명회 참석 확인 전 차단 |
| 체크리스트 | 13 | 제출·발표·서약 등 수행 확인 |
| 정보 | 3 | 전자입찰·총액·계약기간 정보 |

이 분리는 공고 원문 추출과 회사 적격성 판단을 섞지 않는다. 공고 추출은 공개 근거를 제공하고,
실제 회사 상태의 동적 재확인과 담당자 결정은 관리형 비공개 저장소에서 별도로 수행한다.
