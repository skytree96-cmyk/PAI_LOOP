# PAI_LOOP 채널별 워크플로우 아키텍처 v1.6

팀원·멘토 공유용 16:9 아키텍처 도식이다. 기존 `PAI_LOOP_architecture.svg/png`는
수정하지 않았고, 아래 파일을 다음 버전으로 추가했다.

- `PAI_LOOP_workflow_architecture_v1.6.svg`: 편집 가능한 1920×1080 기준본
- `PAI_LOOP_workflow_architecture_v1.6.png`: 메신저·문서 공유용 렌더링본

## 읽는 순서

1. **n8n**: 매일 08:00 신규·변경 공고 전체를 분석 큐에 넣고, 최근 7일 우선순위와
   3년 낙찰 신호를 조합한다. 기존 미분석 89건은 정기 실행과 섞지 않는
   `ONE-TIME BACKFILL` 경로로 `89/89` 처리 완료했으며, 도식은 이 별도 경로를 표시한다.
2. **WEB**: GitHub `main`을 Render에 배포한 FastAPI/SPA가 문서를 추출하고,
   OpenAI Responses API의 구조화 결과를 결정론적 규칙으로 평가해 근거와 함께 표시한다.
3. **TEAMS**: 지금은 카드 payload를 mock으로만 남긴다. 회사 Tenant 승인과
   Credential 등록 후 Teams Workflows/Bot push와 담당자 결과 환류를 연결한다.
4. **DATA**: PostgreSQL 안에서 판단 기준 영역과 판단 완료·학습 후보 영역을 논리적으로
   분리한다. Git의 검토된 immutable seed를 hash/version으로 기준 DB에 동기화하고,
   계산 snapshot은 append-only로 보존한다.

## 과장하지 않은 현재 경계

- PDF/HWPX는 문서 처리 경로에 포함되지만 구형 binary HWP 자동 변환은 아직 없어
  안전하게 `REVIEW`로 보낸다.
- Teams는 실제 전송 전이며 회사 승인 이후 연결할 `TARGET`이다.
- 낙찰/실주 결과 자동 환류는 `TARGET`이고, 현재 학습 후보 데이터는 사람의 검수 없이
  자동으로 모델을 학습시키거나 판단 기준을 덮어쓰지 않는다.
- 최근 7일은 화면·아침 briefing 범위다. 공고·평가·snapshot·결정·낙찰 이력은
  PostgreSQL에 장기 보존하며, 7일 후 삭제되는 것은 단기 운영로그뿐이다.

## 렌더링

SVG는 Pretendard Variable을 우선 사용하고, font asset이 없으면 맑은 고딕·Noto Sans KR
순으로 fallback한다. PNG를 다시 만들 때는 SVG와 동일한 1920×1080 viewport를 사용한다.
