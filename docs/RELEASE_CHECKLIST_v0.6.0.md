# PAI LOOP v0.6.0 persistent analysis release checklist

## 구현 완료

- [x] 검토된 Git 기준자료 6종을 `reference_data_versions`에 버전·SHA-256과 함께 멱등 동기화한다.
- [x] 공개 회사 profile의 facts/evidence를 마감일 평가 DB에 동기화한다.
- [x] 저장된 최신 ACCEPTED 추출본을 첨부별로 선택하고 의미 중복을 제거한다.
- [x] 적격성·행동 필요만 AtomicRequirement로 만들고 체크리스트·정보는 결과 snapshot으로 분리한다.
- [x] materialization, Evaluation, AnalysisRun, child snapshots를 한 transaction에서 commit/rollback한다.
- [x] 정량 범위, 준비도, 증빙률, 사업 리스크, 경쟁 리스크, 낙찰률·투찰률 예측, 가격산식 상태를 고정 저장한다.
- [x] 부서 추천과 시스템 GO/HOLD/NO_GO 의견을 저장하되 `user_decisions`를 대체하지 않는다.
- [x] `NO_BID / SUBMITTED / WON / LOST`와 실제 금액·점수·실주 사유를 `bid_outcomes`로 멱등 기록할 수 있다.
- [x] additive migration checksum ledger와 PostgreSQL 호환 DDL을 제공한다.
- [x] n8n Workflow 10은 PPS 이후 bounded batch-analysis를 통과해야 retention/낙찰/briefing으로 진행한다.
- [x] 모든 n8n live gate는 기본 false이고 manual dry-run은 외부 호출 0이다.

## 검증 결과

- Python: 180 tests passed, branch coverage 86.86% (`>=85%`).
- focused API: 분석 batch, idempotent reuse, snapshot 조회, daily snapshot 전달, outcome upsert 통과.
- DB: SQLite round-trip/rollback/uniqueness, 기존 schema additive upgrade, PostgreSQL DDL compile 통과.
- migration smoke: `20260817_01_analysis_persistence` 1회 적용 후 pending 0.
- wheel: `pai_loop-0.6.0`, 신규 runtime modules/data 포함, private adapter/DB/secret artifact 0.
- n8n: 7 definitions validate, Workflow 10 offline E2E에서 backend/PPS/award/OpenAI/Teams 호출 0.

## 온라인 배포 확인

- [ ] GitHub PR CI green인 exact commit을 Render에 배포한다.
- [ ] `/healthz` 본문의 `database=ok`, `version=0.6.0`을 확인한다.
- [ ] `/api/v1/reference-data/versions`에서 ACTIVE 6종을 확인한다.
- [ ] 공개 인천 공고 batch live 1회에서 AnalysisRun/Evaluation/snapshot이 생성되는지 확인한다.
- [ ] 같은 batch를 재실행해 `reused=true`이고 신규 snapshot 0인지 확인한다.
- [ ] n8n Workflow 10의 신규 batch HTTP 노드에 `PAI_LOOP Render Backend` credential을 연결한다.
- [ ] n8n은 inactive/live gates false 상태에서 manual dry-run 성공 후 유지한다.

## 전사 운영 전 잔여 Gate

- [ ] 원격 첨부 manifest/download, private object storage, PDF/HWP/HWPX 변환 worker.
- [ ] 개찰·낙찰·계약 결과의 자동 `bid_outcomes` 환류. 현재 API/DB는 준비됐지만 자동 adapter는 TARGET이다.
- [ ] Entra SSO/RBAC와 역할별 내부 snapshot 열람 권한.
- [ ] 유료 PostgreSQL backup/PITR, restore drill, migration release job, 다중 인스턴스 reference lock.
- [ ] 실제 Teams credential/tenant 승인 후 mock 노드를 실제 발송 노드로 교체.
