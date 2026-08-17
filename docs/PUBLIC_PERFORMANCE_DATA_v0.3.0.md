# PAI_LOOP 공개 실적 데이터 계약 v0.3.0

## 목적

전사 웹에서 공개된 사업 수행 사실을 검색하고 유사 실적 후보를 찾기 위한
`PUBLIC_DERIVED` 데이터 계약이다. 공개 앱은 검토가 끝난 seed만 읽으며 사내
원천 형식, 원천 반입 코드 또는 로컬 파일에 의존하지 않는다.

`contract_amount_krw`는 공개 계약금액 참고값이다. 공고별 인정실적,
정량점수, 입찰 적격 또는 GO 판단으로 자동 승격하지 않는다.

## 공개 snapshot

- 스키마: `public-performance-seed-1.0.0`
- 데이터셋: `2026.08.17-v3`
- 분류: `PUBLIC_DERIVED`
- 레코드: 1,182건
- 계약연도: 2018~2025년
- 직접식별자 검사: 0건
- 패키지 자산: `src/pai_loop/data/public_performance_seed_v1.json`

각 레코드는 아래 allowlist만 허용한다.

| 필드 | 의미 | 비고 |
|---|---|---|
| `record_key` | 공개 필드 기반 비가역 키 | 원천 식별자 아님 |
| `project_name` | 공개 사업명 | 비식별화 검사 적용 |
| `overview` | 공개·비식별 사업개요 | 연락처·주소·역할자명 제거 |
| `agency` | 발주기관 | 공개 사업 정보 |
| `contract_date` | 계약일 | ISO 날짜만 허용 |
| `contract_year` | 계약연도 | 검색·집계용 파생값 |
| `contract_amount_krw` | 계약금액 | decimal 문자열; 인정실적 아님 |
| `keywords` | 공개 검색어 | 최대 30개 |
| `division` | 수행부서 | 개인 이름 없음 |

주소, 연락처, 담당자, 전자우편, 개인·법인 식별번호, 계약번호, 원천 레코드
식별자, 원천 경로와 첨부 메타데이터는 허용되지 않는다. 자유서술에도
전자우편, 전화번호, 식별번호, URL, 주소형 문구, 역할자명과 긴 번호 패턴을
검사한다.

snapshot은 공개 레코드 canonical JSON의 SHA-256으로 무결성을 검증한다.
API는 digest 불일치, allowlist 외 필드, 중복 키 또는 직접식별자 패턴을 발견하면
fail closed 한다.

## 공개 런타임 경계

```text
reviewed PUBLIC_DERIVED seed
  -> package digest/field validation
  -> read-only search API
  -> browser filters and quantitative evidence candidates

managed PostgreSQL
  <- notices, analyses, human decisions and audit events
```

현재 API는 패키지 자산을 읽으므로 stateless 웹 인스턴스에서도 실적 검색이
동작한다. 공개 저장소에는 seed를 검증·조회하는 코드만 포함한다. seed를 만드는
사내 출판 과정과 원천 어댑터는 별도 승인 경계이며 공개 릴리스에 포함하지 않는다.

새 데이터셋을 릴리스할 때는 다음을 모두 확인한다.

1. 데이터셋 버전과 canonical digest가 일치한다.
2. `direct_identifier_findings == 0`이다.
3. 공개 레코드 키가 모두 고유하다.
4. 레코드 필드가 allowlist와 정확히 일치한다.
5. wheel에 seed와 loader가 있고 local-only adapter가 없다.
6. 공개 diff는 집계와 정책 변경을 먼저 검토한다.

## API

| Method | Endpoint | 반환 범위 |
|---|---|---|
| `GET` | `/api/v1/performance/summary` | 버전·digest·집계·비식별화 건수 |
| `GET` | `/api/v1/performance` | 공개 allowlist 레코드만 반환 |

목록 API는 `q`, `year`, `division`, `limit`, `offset`을 지원한다. `limit`은
최대 200이며 다른 `/api/v1` endpoint와 동일한 서버 인증 경계를 사용한다.

## 운영 Gate

- 새로운 공개 필드나 서술형 데이터가 추가되면 자동 게시하지 말고 정책과
  테스트를 먼저 갱신한다.
- 실적 후보가 존재한다는 사실만으로 유사실적 인정, 인정금액, 입찰 적격,
  정량점수를 확정하지 않는다.
- 외부 웹 배포는 HTTPS, 인증·권한, rate limit, 감사로그를 켠 managed 환경에서만
  수행한다.
