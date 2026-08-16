# PAI_LOOP 아키텍처 도식

`PAI_LOOP_architecture.svg`가 편집 가능한 기준본이고, `PAI_LOOP_architecture.png`는 문서·n8n Sticky Note에서 빠르게 표시하기 위한 렌더링본이다.

## 표시 방식

- n8n의 `PAI_LOOP 00 - Architecture` 워크플로는 Sticky Note의 Markdown 이미지 문법으로 GitHub raw PNG를 읽는다.
- URL에는 인증정보나 서명 토큰을 넣지 않는다.
- 저장소를 비공개로 전환하면 raw 이미지는 n8n 편집기에서 보이지 않을 수 있다. 이때도 워크플로 아래의 실제 노드 맵과 설명 Sticky Note가 독립적인 대체 표현으로 남는다.
- 비공개 운영 환경에서 이미지를 계속 표시하려면, 사내 정적 자산 호스트의 읽기 전용 HTTPS URL로 Sticky Note의 이미지 주소만 교체한다. 만료되는 SAS URL이나 API 키가 포함된 URL은 커밋하지 않는다.

## 책임 경계

- n8n: 일정, API 페이징, 재시도, 중복 방지, 워크플로 오케스트레이션
- Document Worker: PDF/HWPX/HWP 변환과 구조·페이지 근거 추출
- OpenAI: 고정 스키마를 따르는 조건·배점·근거 구조화
- Rule Engine: 결정론적 자격·정량·준비도·리스크 판정
- PostgreSQL/Object Storage: 버전·증빙·예외·원본 보관
- Web/Teams: 사람의 검토, 의사결정, 알림과 결과 환류

## 변경 규칙

1. SVG를 먼저 수정한다.
2. 같은 크기(1600×900)로 PNG를 다시 렌더링한다.
3. n8n의 노드 이름과 도식의 컴포넌트 이름이 어긋나지 않는지 확인한다.
4. `node scripts/deploy-workflows.mjs --validate-only`로 manifest와 workflow JSON을 검증한다.
