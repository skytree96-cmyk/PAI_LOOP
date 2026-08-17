# PAI LOOP 데이터 영속화와 additive migration v0.6.0

## 저장 경계

운영 기준 시스템은 `PAI_LOOP_DATABASE_URL`이 가리키는 관리형 PostgreSQL이다.
로컬 SQLite는 개발·테스트 전용이다. 공개 저장소의 JSON은 검토된 기준 데이터의
배포 원본이며, 온라인 동기화 서비스는 이를 `reference_data_versions`에 해시와
버전을 포함해 멱등 반영한다.

원문 파일, API 키, provider 원응답, 로컬 경로, 개인식별정보는 아래 신규 테이블에
저장하지 않는다. 입력 manifest에는 문서 SHA-256, 공고/버전 식별자와 기준 데이터
버전만 기록한다.

## 운영 테이블

| 테이블 | 역할 | 갱신 방식 |
|---|---|---|
| `reference_data_versions` | 회사 공개 프로필, 실적, 부서 키워드, 정량·가격 규칙 등 판단 기준의 검토본 | `(dataset_key, version)` 멱등 동기화. 같은 dataset의 기존 ACTIVE를 RETIRED로 바꾸고 신규 버전을 ACTIVE로 만드는 한 transaction |
| `analysis_runs` | 한 번의 계산을 재현하는 감사 루트 | 입력 hash와 전체 기준 버전이 같은 경우 `idempotency_key`로 재사용 |
| `requirement_result_snapshots` | 당시 표시한 원자조건·정책 결과 | analysis run과 같은 transaction에서 append |
| `score_snapshots` | 적격성·준비도·정량·가격·경쟁 리스크의 값/범위/상태 | analysis run과 같은 transaction에서 append |
| `recommendation_snapshots` | 부서 우선순위와 입찰 추천 | analysis run과 같은 transaction에서 append |
| `bid_outcomes` | NO_BID/SUBMITTED/WON/LOST 및 실제 금액·점수·실주 사유 | source의 안정적인 `outcome_key`로 upsert, 관측시각 보존 |

`analysis_runs.idempotency_key`는 전역 유일하다. 권장 구성은 공고, 공고 버전,
문서 hash, ruleset/profile 버전, 분석기 버전의 canonical JSON SHA-256이다.
원문 문자열이나 비밀값을 키에 넣지 않는다.

`reference_data_versions`의 ACTIVE 전환은 애플리케이션 transaction으로 처리한다.
PostgreSQL partial unique index는 SQLite와 동일하게 동작하지 않으므로 공통 schema에
사용하지 않았다. 현재 단일-worker 베타는 기존 ACTIVE를 RETIRED 처리한 뒤 새 ACTIVE를
넣는다. 다중 인스턴스 전환 전에는 PostgreSQL advisory lock 또는 dataset별 partial
unique index를 추가해야 한다. 동시 충돌 시 무결성은 유지되지만 한 요청은 재시도될 수 있다.

## 재평가 트리거

다음 입력 중 하나가 바뀌면 새 `analysis_run`을 생성한다.

- 공고 또는 첨부 문서 버전/SHA-256;
- AtomicRequirement materialization 결과;
- 회사 사실·증빙의 값, 상태 또는 유효기간;
- ruleset, 회사 profile, 부서 keyword, 정량 profile, 가격산식, 경쟁 분석 버전;
- 공고 마감일 또는 담당자가 선택한 강제 재검토 사유.

과거 snapshot은 수정하지 않는다. 화면의 현재 결과는 가장 최근 COMPLETED run을
사용하되 이전 run을 감사 이력으로 유지한다. 7일 retention은 완료 ingestion log와
Teams mock에만 적용하며 이 테이블과 기존 notices/evaluations/decisions/award history는
삭제하지 않는다.

## Migration 실행

이번 migration은 기존 컬럼을 변경하거나 삭제하지 않고 신규 테이블만 만드는
`20260817_01_analysis_persistence` additive migration이다.

기존 DB의 사전 확인:

```powershell
$env:PAI_LOOP_DATABASE_URL="postgresql://..."
python -m pai_loop.migrations --check
```

적용:

```powershell
python -m pai_loop.migrations
```

빈 개발 DB만 초기화할 때:

```powershell
$env:PAI_LOOP_DATABASE_URL="sqlite:///./data/pai_loop.db"
python -m pai_loop.migrations --create-base
```

`schema_migrations`는 migration ID, checksum, 적용 시각을 기록한다. 같은 migration의
재실행은 no-op이고 checksum이 다르면 자동 진행하지 않는다.

현재 앱의 `Base.metadata.create_all()`도 신규 테이블을 만들 수는 있지만 기존 테이블의
컬럼 변경·rename·backfill을 수행하지 못하고 migration ledger도 남기지 않는다.
따라서 배포 전 migration 명령을 별도 release step으로 실행해야 한다. 향후 기존
테이블 변경이 필요해지면 Alembic 또는 동등한 revision 기반 도구로 전환하고, 적용 전
PostgreSQL backup/PITR와 restore drill을 완료한다.

## 서비스 transaction 계약

자동 분석 서비스는 다음을 하나의 SQLAlchemy session/commit으로 처리한다.

1. ACCEPTED extraction을 AtomicRequirement로 멱등 materialization;
2. 결정론적 Evaluation 생성;
3. `analysis_runs` 생성;
4. requirement/score/recommendation snapshot 생성.

중간 실패는 전부 rollback한다. 동시 실행이 `idempotency_key` unique constraint에
충돌하면 rollback 후 기존 run을 읽어 재사용한다. `bid_outcomes`는 분석 transaction과
분리할 수 있지만 `(notice_id, outcome_key)` 유일성을 지켜야 한다.
