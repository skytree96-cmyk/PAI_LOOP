# PAI_LOOP Release Checklist v0.3.0

기준일: 2026-08-17  
대상: 조직별 검색 우선순위, 공개-safe 실제 데이터와 공모전 온라인 읽기 전용 배포

## 소스 릴리스 후보

| Gate | 결과 | 근거 |
| --- | --- | --- |
| Python 회귀검사 | PASS | 122 tests, branch coverage 85.63% (gate 85%) |
| Python/의존성 | PASS | compileall, `pip check` |
| 정적 웹 | PASS | JS 문법, HTML ID/ARIA, 실제 API 브라우저 E2E, 데스크톱/390px 가로 넘침 검사 |
| n8n | PASS | 배포 스크립트와 6개 workflow JSON/연결/Code 검증 |
| Render 명세 | PASS | `render.yaml` 파싱, production 안전 기본값, CI 후 배포, seed hook |
| 공개 인천 공고 | PASS | 실제 파생 요구조건 23건/근거 26건, 정책 6/1/13/3, blocking 1 |
| 부서별 검색 | PASS | 전사 기본 교육·컨설팅 + 24 profiles + 사용자 키워드, 지역 축 중복 제거 |
| 공개 회사 실적 | PASS | 1,182건, policy 1.2.0, 직접식별자/역할자명 잔존 0, 전체 수행부서 필터 |
| 공개 회사 프로필 | PASS | 원문·등록번호·주소·인명 제외, fact/파일명/digest/유효 메타만 포함 |
| public-read-only | PASS | 공개 allowlist GET, 공개 상세 allowlist, 모든 write/internal route 401 |
| secret/source scan | PASS | 게시 후보와 workspace 실제 secret 값 exact match 0, DB/Office/원본 artifact 0 |
| wheel | PASS | v0.3.0 격리 설치, runtime/seed/static 필수 자산 포함, 새 DB smoke |

## 실제 화면 검수

- 온라인 공개 상태와 쓰기 비활성화 표시
- 실제 인천 공고의 `적격성 6 / 행동 필요 1 / 체크리스트 13 / 정보 3`
- 제안설명회 참석 확인 전 `BLOCK_UNTIL_CONFIRMED`; 참가자격 REVIEW와 분리
- 같은 공고를 부서/사용자 키워드별로 재정렬하고 추천 부서·지역 축 표시
- 회사 실적 검색·연도·수행부서·페이지 이동, 인정실적/금액/점수 미확정 고지
- Teams 실제 전송 없이 mock 경계 유지

## 외부 게시·배포 Gate

- [ ] GitHub Draft PR에 정확한 후보를 게시하고 Actions가 green인지 확인한다.
- [ ] main 병합은 리뷰 이후 별도로 승인한다.
- [ ] Render GitHub OAuth와 무료 Web/PostgreSQL 생성을 승인한다.
- [ ] Docker image build와 `/healthz`, 초기 public notice seed hook을 실제 환경에서 확인한다.
- [ ] Render에 PPS/OpenAI secret을 입력하고 브라우저·로그·GitHub에 노출되지 않음을 확인한다.
- [ ] n8n에 온라인 API base URL과 서버 전용 API key를 credential로 연결한다.
- [ ] 무료 Render DB 만료일/백업 부재/cold start를 데모 운영표에 기록한다.
- [ ] 전사 쓰기 배포 전 Entra SSO/RBAC, 암호화 저장소, 감사·보존·복구를 통과한다.
- [ ] Teams 실제 전송은 회사 승인 후 별도 릴리스로 전환한다.

이 문서의 PASS는 공개-safe 소스 릴리스 후보에 대한 판정이다. 외부 서비스
생성·배포와 전사 운영 승인을 대신하지 않는다.
