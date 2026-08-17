# PAI_LOOP 아키텍처 도식

팀·멘토 공유용 최신 도식은
[`PAI_LOOP_workflow_architecture_v1.6.svg`](PAI_LOOP_workflow_architecture_v1.6.svg)와
[`PAI_LOOP_workflow_architecture_v1.6.png`](PAI_LOOP_workflow_architecture_v1.6.png)다.
Pretendard를 적용한 1920×1080 도식으로 n8n·WEB·Teams·DATA 채널, API 사용
지점, 판단 기준 DB와 판단 완료·학습 후보 DB의 유지 방식을 한 화면에 표시한다.

`PAI_LOOP_architecture.svg`와 `PAI_LOOP_architecture.png`는 n8n 편집기 Sticky Note용
1600×900 운영 도식으로 계속 유지한다.

공유 도식 v1.6은 **오전 9시 통합 n8n 진입점**, 당일 신규·정정 공고 전량의
재개 가능한 분석, 기존 미분석 공고의 일회성 backfill, 최근 7일 아침 피드, 최근 3년
낙찰 refresh, 정량·가격·적합성·경쟁집중 리스크와 backend Teams mock 기록 흐름을
표시한다. 후보는 동일 사업 또는 경쟁사 확정 이력을 뜻하지 않으며 가격 신호도 관측
기반 예상이다.

도식은 **현재 연결된 경로**와 **목표 아키텍처**를 함께 보여준다. PDF/HWPX 원격
첨부 획득·구조화는 현재 연결돼 있지만 구형 binary HWP 변환은 아직 제한이며 안전하게
REVIEW로 남긴다. Teams 실제 push와 입찰·개찰·낙찰·계약 결과의 자동 환류는 회사
승인과 외부 연결 전까지 `TARGET`이다. 현재 구현 범위는 PPS 수집, PDF/HWPX 분석,
결정론 평가와 snapshot, 정량/부서랭킹, 낙찰 refresh, 7일 피드·단기로그, 웹 조회와
backend Teams mock 기록이다.

## 표시 방식

- n8n의 `PAI_LOOP 00 - Architecture` 워크플로는 Sticky Note의 Markdown 이미지 문법으로 GitHub raw PNG를 읽는다.
- URL에는 인증정보나 서명 토큰을 넣지 않는다.
- 저장소를 비공개로 전환하면 raw 이미지는 n8n 편집기에서 보이지 않을 수 있다. 이때도 워크플로 아래의 실제 노드 맵과 설명 Sticky Note가 독립적인 대체 표현으로 남는다.
- 비공개 운영 환경에서 이미지를 계속 표시하려면, 사내 정적 자산 호스트의 읽기 전용 HTTPS URL로 Sticky Note의 이미지 주소만 교체한다. 만료되는 SAS URL이나 API 키가 포함된 URL은 커밋하지 않는다.

## 책임 경계

- n8n: 09:00 일정, 당일 신규·정정 전량의 재개 가능한 batch, 3년 낙찰 refresh, 7일 피드와 통합 mock 카드
- Document Worker: PDF/HWPX 구조·페이지 근거 추출, binary HWP는 변환 경로 마련 전 REVIEW
- OpenAI: 고정 스키마를 따르는 조건·배점·근거 구조화
- Rule Engine: 결정론적 자격·정량·준비도·리스크 판정
- PostgreSQL: 기준 버전과 판단·snapshot·결과 이력 보관; 원문 Object Storage는 운영 TARGET
- Web/Teams: 사람의 검토, 의사결정, 알림과 결과 환류

## 변경 규칙

1. SVG를 먼저 수정한다.
2. 운영 도식은 1600×900, 공유 도식은 1920×1080으로 PNG를 다시 렌더링한다.
3. n8n의 노드 이름과 도식의 컴포넌트 이름이 어긋나지 않는지 확인한다.
4. `node scripts/deploy-workflows.mjs --validate-only`로 manifest와 workflow JSON을 검증한다.

워크플로별 책임, 환경변수, 보호된 API 계약과 Teams mock 교체 Gate는
[`N8N_WORKFLOWS.md`](N8N_WORKFLOWS.md)에 기록한다.
