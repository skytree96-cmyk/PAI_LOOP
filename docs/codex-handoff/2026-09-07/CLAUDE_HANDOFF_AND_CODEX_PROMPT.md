# Claude → Codex 인계: 비영리법인 자격 오판정 · 첨부 추출 · 정량 미산정

기준 시각 2026-09-07. 완료 보고서가 아니다. 우선순위 1·2는 PR로 제출했고, 3은 코드 변경 없이 원인만
분류했다. 4(부서 계정)는 착수하지 않았다.

**이 작업에서 하지 않은 것**: `main` 병합, Render 배포, n8n 변경, 새 재분석 캠페인 실행.
진행 중인 재검토 캠페인은 건드리지 않았고 큐 상태도 재조회하지 않았다.

## 제출한 PR (둘 다 Draft, 원격 CI 통과)

| PR | 브랜치 | 내용 | CI |
|---|---|---|---|
| #111 | `claude/nonprofit-sme-or-eligibility-20260907` | 비영리법인 OR 참가자격 — 범위 과확장 제거, 접미 허용 보정, 거짓 사유 제거 | pass 6m24s |
| #112 | `claude/attachment-extraction-audit-20260907` | 첨부 공유 실패 라벨 정정, 인코딩 미확정 XLS 거부, `.xls` 오표기 OOXML 복구 | pass 6m10s |

둘 다 최신 `main`(`458fbc9`, PR #102 병합) 기준이며 서로 독립적이다. `workflows/`·`scripts/`·
`manifest.json`은 어느 쪽도 건드리지 않는다.

---

## Codex에게 줄 프롬프트

아래 블록을 그대로 붙여넣는다.

```text
지금 돌리고 있는 ping/모니터링 루프는 그대로 유지해. 중단·재시작·교체하지 말고, 그 결과 추적도
계속하면서 아래 작업을 병행해줘. 모니터링이 쓰는 폴더·브랜치·워크플로는 건드리지 말고, 아래
작업은 최신 main에서 만든 별도 브랜치·별도 작업 폴더에서 해줘.

먼저 docs/codex-handoff/2026-09-07/CLAUDE_HANDOFF_AND_CODEX_PROMPT.md 를 읽어.
과거 로그 전체를 처음부터 읽지 말고 이 문서와 아래에서 지정한 파일만 확인해줘.

우선순위대로 진행해줘.

1. PR #111의 "범위 반론"을 판단해줘. 인식된 SME/비영리 OR 조항을 공고 전체
   nonprofit_exception_present 플래그에서 빼는 변경이다. Claude는 보수적인 쪽(다른 요구사항은
   각자의 FAIL_CONFIRMED 유지)을 택했고, 독립 검토 2건은 기존부터 있던 의도된 경계라고 반론했다.
   PR #111 본문과 코멘트에 양쪽 근거와 되돌리는 방법이 있다. 코드를 읽고 결정한 뒤, 유지하든
   되돌리든 그 결정을 PR에 남겨줘. 되돌릴 경우 두 테스트의 기대값도 함께 바꿔야 한다.

2. 보고된 오판정 공고의 원문을 확보해줘. 저장된 ACCEPTED 추출 페이로드에서
   requirements[].normalized_condition 중 근거 인용에 소기업·소상공인 확인서와 비영리법인이 같이
   나오는 값을 읽기 전용으로 가져와, SYN- 식별자 픽스처로 tests/test_eligibility_policy.py 에
   추가하고 PR #111의 수정이 그 건을 실제로 PASS_EXCEPTION 으로 해결하는지 확정해줘.
   문자열을 추측해서 만들지 마. 못 찾으면 못 찾았다고 보고해줘.

3. PR #112의 운영 발생 건수를 확정해줘. 실패한 첨부 시도의 저장된 error_code 를 읽기 전용으로
   현재 PPS manifest와 attachment_id 로 조인해서, 전수 관측의 PDF_EXTRACT_FAILED 14 /
   DOCUMENT_EXTRACT_FAILED 8 / HWPX_EXTRACT_FAILED 7 / UNSUPPORTED_ATTACHMENT 1 중 몇 건이
   (a) 공유 컨테이너·예산 계층 실패라서 OPENAI_REVIEW 로 잘못 표시되고 있었는지, (b) .xls 인데
   OOXML 패키지이거나 CODEPAGE 없는 pre-BIFF8 인지, (c) 실제 파일 누락·미지원 형식·원본 품질
   문제인지 구분해줘. 코드 오류와 자료 부족을 섞지 마.

4. 정량 교착 결함의 운영 발생 여부를 확인해줘. 공백만으로 정규화되는 결손 문자열
   (missing_or_unreadable 항목)이 저장된 공급자 응답에 실제로 있는지 읽기 전용으로 조회해줘.
   있으면 이 문서의 "정량 교착" 절에 적은 권장 처리(새로 받는 응답에 한해 스키마 위반으로 보고
   기존 보정 추출 경로로 넘기기)를 적용해줘. 과거 저장 payload 의 검증은 깨지 마. 없으면 잠재
   결함으로만 기록하고 코드를 바꾸지 마.

5. 위에서 바꾼 부분에 맞는 회귀 검증과 필수 CI를 돌려줘. 이미 통과한 동일 소스 검사를 근거 없이
   반복하지 마. 기존 테스트의 기대값을 통과 목적으로 바꾸지 마. 부득이하게 바꿔야 하면 왜 그
   계약이 틀렸는지 근거를 남겨줘.

지켜야 할 것:
- main 병합, Render 배포, n8n 변경, 새 재분석 캠페인 실행은 조율 전에 하지 마. 진행 중인 재검토
  캠페인은 같은 parent 를 유지하고 새 캠페인을 만들어 중복 분석하지 마.
- POLICY_VERSION v12 는 open·미마감 공고를 version-refresh 파티션으로 옮기고 그 refresh parent
  가 같은 공고 키를 예약한다. 캠페인이 종료 상태에 도달한 뒤 올려야 안전하다.
- PAI_LOOP_codex_repair 폴더의 누적 변경을 reset/restore 하거나 오래된 파일로 덮어쓰지 마.
  (확인 결과 그 폴더의 소스 변경은 프런트엔드 3파일을 빼고 전부 이미 main 에 병합돼 있다.)
- 자격 PASS/FAIL/REVIEW, 정량 확정·추정·미산정, 제출 준비도, AI 추천, 담당자 판단을 구분하고
  기존 담당자 판단을 보존해줘.
- 부족한 증빙을 추정하거나 검증 조건을 완화해서 통과시키지 마. 미조회·조회 실패를 미결정이나
  0점으로 표시하지 마. 미산정을 0점으로 바꾸지 마.
- 내부 자료, 비밀번호, 개인 식별자를 코드·GitHub·로그·PR 본문에 올리지 마. 실제 첨부나 원본
  워크북을 커밋하지 마. 픽스처는 SYN- 식별자를 쓰고 실제 회사·개인 데이터를 복사하지 마.
- 부서 계정 시스템은 복구 완료 이후 별도 브랜치·PR 이다. 지금 착수하지 마.

완료한 변경, 검증 결과, 아직 미해결인 코드 문제와 자료 부족, 실제 배포 여부를 구분해서 PR에
남겨줘. 계획 설명에서 멈추지 말고 구현·검증까지 진행해줘.
```

---

## 우선순위 1 — 비영리법인 참가자격 (PR #111)

인계받은 로컬 전용 패치(기준 커밋과 패치 커밋 모두 원격에 없음)를 최신 `main` 위에서 검토·통합했다.
패치는 충돌 없이 적용됐고 focused 191건이 통과했지만, 심층 검토에서 세 가지를 고쳐야 했다.

1. **범위 과확장 제거 (강화).** 인식된 OR 조항이 공고 전체 `nonprofit_exception_present` 플래그를
   세우고 있었다. 이 플래그는 *다른* 요구사항의 명시적 `COMPANY_CONFIRMED_ABSENT` FAIL을 범위
   REVIEW로 내린다. 자기 완결적인 "확인서 소지 업체 또는 비영리법인" 조항은 다른 확인서 계열에
   대해 아무것도 말하지 않으므로, 그대로 두면 REVIEW가 명시적 필수 FAIL을 가린다
   (AGENTS.md: *REVIEW must never hide another explicit mandatory FAIL*). 패치 자신의 변경 노트도
   "별도 요구사항은 영향받지 않는다"고 주장했는데 코드는 반대였다. 이제 별도 중소기업확인서
   요구사항과 별도 직접생산확인증명서 요구사항은 각자의 `FAIL_CONFIRMED`를 유지한다.
2. **접미 허용 보정.** 인식기는 전체 조건에 대한 `re.fullmatch`인데 꼬리 허용이 `해당(?:함)?\.?`
   뿐이어서 추출이 실제로 만드는 서술형 종결에 걸리지 않았다. 닫힌 리터럴 allowlist
   `_INERT_CLAUSE_TAIL` 하나만 앵커에 흡수시켰다. 각 토큰은 자체 의무도 극성도 없다. 선행 접두
   allowlist는 두지 않았다. 앵커를 `re.search`로 완화하는 대안은 기존 부정/제외/AND 반례 8개 중
   5개를 통과시키므로 채택하지 않았다.
3. **거짓 사유 제거.** DF-000 설명이 조항에 `비영리법인`이 명시돼 있는데도 "공고 원문에도 비영리법인
   예외가 없어"라고 단정했다. 판정은 그대로 `FAIL_CONFIRMED`로 두고 사유 문구만 사실에 맞게 바꿨다.
   처음에는 가드를 **조항 단위**로 걸었는데 그 문구의 주장은 **공고 단위**여서 다른 요구사항에서
   같은 거짓이 남았다. 공고 단위 존재 여부로 가드를 다시 잡았고, 비영리 문구가 아예 없는 공고에서는
   원래 문구가 바이트 단위로 그대로다.

### 검증

- focused 219 passed (통합 직후 191 → 회귀 28개 추가)
- 자격정책에 연결된 전체 파일 402 passed
- 로컬 CI 게이트: public-release boundary · `compileall -q src tests tools` · secret/source scan(271
  tracked) · frontend asset 모두 PASS
- 원격 CI pass 6m24s

### 아직 증명되지 않은 것 (가장 중요)

읽기 전용 운영 요구사항 스냅샷(2026-08-31, 현재 평가 보유 OPEN 공고 236건 / 요구사항 항목 7,107건)으로
측정했다. `eligibility_policy._base_item`에서 항목의 `condition`은 `normalized_condition`을 그대로
담으므로, 이 값은 정책이 실제로 보는 문자열과 동일하다.

- 확인서 패턴과 `비영리법인`을 함께 가진 조건 **24건**, 그중 원래 패치 인식기 매칭 **0건**
  (24건 전부 `중 하나에 해당`으로 끝나지 않아 `re.fullmatch` 실패)
- 24건의 현재 판정: PASS_EXCEPTION 16 / REVIEW 8. **FAIL 0건**, 문제의 메시지를 가진 항목 **0건**
- 24건의 `비영리법인`은 **전부 법령 하위집합으로 한정**돼 있다(예외 규정 해당 / 우선조달 예외 대상 /
  시행령 조항 해당 / `특정` / `일부` / 관계법령상). 회사가 그 하위집합에 속한다는 사실은 원문만으로
  증명되지 않으므로 REVIEW 유지가 옳다. 인식 범위를 무한정 OR까지 넓혀도 이 24건은 여전히 매칭되지
  않으며, 넓힐 근거도 없다.
- 접미 allowlist의 크기 근거: 고유 조건 6,666건 중 6,655건이 접두 없이 조항으로 시작하고, 꼬리는
  서술형 의무 종결이 지배적이다(`함` 2,395 · `해야 함` 1,452 · `여야 함` 607 · 주어 명사 414).

즉 이 수정은 fail-closed이고 회귀 위험은 없지만, **관측 가능한 운영 코퍼스에서는 아직 효과가 증명되지
않았다.** 스냅샷이 2026-08-31이라 정책 v7~v11 이후 원문은 보지 못했다는 한계도 있다.
프롬프트 2번이 이 한계를 닫는 작업이다.

### 반론 (담당자 판단 필요)

1번 변경에 대해 독립 검토 2건이 "패치가 만든 결함이 아니라 `main` 이전부터 있던 '공고에 비영리법인
예외 문구가 있고 범위가 확정되지 않으면 blocking REVIEW' 규칙이며 별도 테스트로 고정돼 있다"고
반론했다. 주된 근거였던 "거짓 메시지 부활"은 3번 변경으로 제거됐고, 남은 쟁점은 순수하게 범위
문제다. 되돌리는 방법은 PR #111 코멘트에 있다.

### PR #111에 넣지 않은 것

- `_eligibility_item`이 evidence 포인터가 끊긴(dangling) fact나 `evidence_state`가 미검증인 fact에서도
  PASS를 낼 수 있다(재현됨). 큐레이션 프로필이 계약 검사를 받으므로 현재 데이터로는 발생하지 않고,
  공용 헬퍼여서 13개 자격 경로 전체에 영향이 간다. **별도 강화 PR 후보.**
- 인식된 조항이 `증빙 제출`을 요구하는데도 freshness recheck가 발동하지 않는다.
  `_CURRENT_COPY_MARKERS`가 "현재/최신 사본"을 요구하는 문구만 인식하는 문서화된 경계다. 바꾸면 같은
  deadline policy를 쓰는 모든 fact에 영향이 가므로 별도 정책 결정이 필요하다.

---

## 우선순위 2 — 첨부 추출·검증 (PR #112)

재현 확인된 결함 3건. 수용 건수를 늘리기 위해 검증을 완화한 변경은 없다.

1. **공유 실패 코드가 "문서는 읽었다"로 표시되고 있었다.**
   `_public_attachment_failure_reason_code`가 형식 접두사 붙은 코드만 인식해서, 컨테이너·예산·안전
   계층이 올리는 실패가 전부 `OPENAI_REVIEW`로 떨어졌다 — `ARCHIVE_*` 15종, `DOCUMENT_EMPTY`,
   입력·압축해제 한도, `XML_DTD_FORBIDDEN`, `UNSAFE_DOCUMENT_FILENAME`,
   `UNSUPPORTED_ARCHIVE_MEMBER_TYPE`, `UNSUPPORTED_DOCUMENT_TYPE`,
   `LEAF_/MEMBER_EXTRACTION_FAILED`. 그 라벨의 담당자 문구는 "문서는 읽었지만 LLM 구조화 또는 검증
   단계를 완료하지 못해 재검토가 필요합니다."인데, 추출기가 열지도 못한 문서에 대해 거짓이고 감사가
   요구하는 결정적 추출 마커를 숨긴다. OLE 바이트를 담은 `.hwpx`는 의도적으로 HWP5 리더로
   우회되므로 실패가 `HWP_` 접두사로 도착하는데, `.hwpx` 분기가 이를 받지 않아 같은 오표시가 났다.
   → 첨부 자신의 형식에 맞는 추출 마커로 귀결시켰다. 코드를 **열거**해서 매칭하므로
   `UNSUPPORTED_ATTACHMENT_TYPE`·HWP-only 코드는 더 구체적인 기존 라벨을 유지하고, 기본값은
   `OPENAI_REVIEW`로 남겨 실제 LLM 단계 실패가 추출 실패로 잘못 표시되지 않는다.
   라벨 정확성 변경이다. `state`는 코드와 무관하게 `REVIEW`로 설정되고, 어떤 첨부도 `ACCEPTED`
   쪽으로 움직이지 않으며, 네 코드가 이미 동일한 retryable 집합에 속해 재시도 동작도 그대로다.
2. **인코딩을 확정할 수 없는 `.xls`가 근거로 채택되고 있었다.** `xlrd`는 `CODEPAGE` 레코드가 없는
   pre-BIFF8 워크북에서 조용히 `iso-8859-1`로 폴백한다. 한글 셀 바이트가 **원문에 없는 문자**로
   디코딩되고, 그 깨진 문자열이 의미성 검사를 통과해 경고도 member issue도 없이 성공한 추출로
   반환됐다. → 새 결정적 코드 `XLS_CODEPAGE_UNVERIFIED`로 거부한다. 인코딩을 추측하지 않는다.
   검사는 리더 자신의 폴백 조건만 정확히 탐지하고, 해당 필드를 보고하지 않는 리더는 추측했다고
   단정하지 않는다.
3. **`.xls` 이름으로 서빙된 OOXML 워크북을 읽지 못했다.** 이미 `.hwpx`-carrying-OLE와
   `.hwp`-carrying-HWPX 두 오표기가 복구돼 있는데(후자는 bounded archive read로 정확한 media type을
   증명한 뒤에만) 워크북 오표기는 빠져 있었다. → ZIP 서명으로 시작하는 `.xls`는 `_extract_xlsx`가
   스스로 요구하는 두 파트(`[content_types].xml`, `xl/workbook.xml`)를 bounded archive read로 증명한
   뒤에만 XLSX leaf로 우회한다. ZIP 서명만으로는 아무것도 우회되지 않고, 파일명·바이트·digest
   동일성은 그대로이며, 추출된 텍스트는 다른 워크북과 완전히 같은 게이트를 통과한다.

### 검증

`test_document_extraction` 33 · `test_pps_enrichment` 85 · (`test_api` / `test_analysis_pipeline` /
`test_daily_operations` / `test_manual_analysis` / `test_openai_extraction` /
`test_quantitative_rule_extraction` / `test_extraction_contract_compatibility`) 656 passed ·
compileall PASS · 원격 CI pass.

2번 가드가 처음에 xlrd를 stub하는 기존 테스트를 깨뜨렸는데, **그 테스트를 수정하지 않고** 가드 조건을
리더의 실제 폴백 조건으로 좁혔다(실제 xlrd에서는 동작 동일).

### 의도적으로 바꾸지 않은 것

- HWPX는 미판독 active content나 미판독 임베디드 객체가 있으면 파싱된 본문 텍스트를 **전부 버린다**
  (DOCX/XLSX/PPTX·HWP5는 `complete=False`로 텍스트를 남긴다). 이 비대칭은 기존 테스트 2개가 고정하고
  있고, 부분 텍스트를 반환하면 미판독 내용이 있는 첨부가 근거 인용을 지지할 수 있게 된다.
- `PdfReader(strict=True)`가 xref 복구로 텍스트를 읽을 수 있는 PDF를 거부한다. 이 엄격성은
  page-anchored 근거의 무결성 전제이고, leaf 계약에 "복구했음"을 결정적으로 기록할 채널이 없어서
  완화하면 감사 흔적 없이 fail-closed 검사만 사라진다. **커버리지 채널을 먼저 만들어야 하는 후보.**
- `_record_unsupported_pps_attachment`의 `.hwp` 분기는 도달 불가다(`.hwp`가
  `_EXTRACTABLE_EXTENSIONS`에 있으므로). `HWP_BINARY_UNSUPPORTED`는 죽은 코드이고 잘못된 결과는
  없다. 저장된 과거 감사 행이 계속 읽히도록 코드는 남겨 둔 채 정리만 하면 된다.

### 필요한 자료

여기 픽스처는 **전부 합성**이며 실제 첨부는 하나도 없다. 전수 관측의 실패 건수를 이 결함들에
귀속시키려면 각 실패 시도의 저장된 `error_code`를 읽기 전용으로 받아 현재 PPS manifest와
`attachment_id`로 조인해야 한다. 프롬프트 3번이 그 작업이다.

---

## 우선순위 3 — 정량 점수 미산정 (코드 변경 없음, 원인 분류만)

상위 사유 코드를 코드에서 역추적하고 각각 재현했다. 결론: **이번 감사에서 정당화되는 코드 변경은
없다.** 아래 분류가 "코드 문제 / 설계 경계 / 자료 부족" 구분이다.

| 사유 | 분류 | 근거 |
|---|---|---|
| `EXTRACTION_CONTRACT_PROOF_INVALID` · `EXTRACTION_VERSION_MISMATCH` · `VALIDATION_FINGERPRINT_MISMATCH` · `VALIDATOR_VERSION_MISMATCH` (각 66) | **버전 노후화, 코드 문제 아님** | 넷이 같은 `not contract_usable`에 걸려 동시 발생한다. 신규 record는 fingerprint가 self-consistent하고, 실패하는 record는 거부된다. 재분석으로 자연 해소된다. 비재계산 fingerprint나 비현행 validator를 받아주는 호환 shim을 넣으면 게이트가 무력화된다 |
| `EXTRACTION_DECLARED_INCOMPLETE` (201, 첨부 전량 수용 92건 중 83건) | **설계 경계** | 공급자가 선언한 비-benign 결손은 특정 형제 첨부가 공급했다고 증명할 수 없으므로 첨부 수용 후에도 계속 차단한다(첨부 수용은 문서 경계이지 정량 경계가 아니다). 회복 경로는 스냅샷당 1세대로 제한된 기존 retry다. `local_absence_is_resolved`를 generic marker까지 확장하면 안 된다 |
| `ATTACHMENT_INCOMPLETE` (170) · `VALIDATED_RECORD_MISSING` (166) | **자료 부족** | 현재 manifest 첨부에 사용 가능한 추출 시도가 없다는 뜻이다. (a) 시도 행 자체가 없음 (b) 있으나 비-ACCEPTED (c) ACCEPTED이나 검증 record 부재·무효 로 분해해야 하고, 그건 운영 `notice_versions` 조인이 필요하다 |
| `MAX_POINTS_LITERAL_MISMATCH` (48) | **설계 경계** | 자체 최대값 복구가 `항목(5점)` 같은 괄호형을 받지 않는다. 보수적이지만 배점을 만들어내지 않는다. 넓히려면 원문 근거가 필요하다 |
| `CASE_TABLE_NOT_DETERMINISTIC` (60) · `AMBIGUOUS_TABLE` (54) · `SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION` (50) 등 | **설계 경계** | 기계적으로 모호한 원문을 통과시키려면 수용 경로를 넓혀야 한다 |

### 정량 교착 — 발견한 실제 결함 1건 (수정 보류)

깨끗한 `main`에서 직접 재현했다. 퇴화된 결손 문자열이 정량 활성화를 **영구 교착**시킨다.

```
missing_or_unreadable = ["   　 "]     # 공백만. 추출 스키마가 그대로 통과시킨다
  record issues : ['EXTRACTION_DECLARED_INCOMPLETE']
  merged        : INCOMPLETE ['SOURCE_GAP_BINDING_MISMATCH']
  reusable      : True
  retryable     : False     <-- 어떤 코드 경로로도 정량 REVIEW를 벗어날 수 없다
```

`>1000자` 결손은 `retryable=True`로 회복 가능하고, 구두점만 있는 결손은 정상 처리된다. 즉 공백만인
경우에만 교착한다. 원인은 두 판단의 충돌이다.

- `merge_validated_quantitative_records`는 결손 목록에 빈 문자열로 정규화되는 항목이 있거나 1000자를
  넘는 항목이 있으면 목록 전체를 신뢰하지 않고, `source_gaps` 가 None인 분기에서
  `SOURCE_GAP_BINDING_MISMATCH`(영구 차단)를 낸다.
- `_accepted_quantitative_review_is_retryable`은 "정규화 후 비어 있지 않은 비-benign 결손"이 없으면
  재시도 대상이 아니라고 판단한다.

**왜 고치지 않았는가.** 재시도 게이트를 여는 방향을 시도했더니 기존 테스트
`test_generic_record_without_valid_actual_nonbenign_gap_is_not_retried`가 `["  "]`·`["​"]`를
**재시도 대상이 아니라고 명시적으로 고정**하고 있었다. 유료 호출을 아끼는 의도된 경계이므로 기존
테스트를 고쳐서 통과시키지 않았다. 쓰기 경계(`ExtractionPayload` 검증)에서 막는 대안도 검토했으나,
같은 모델이 **과거 저장 payload 검증에도 쓰이므로** 기존 감사 레코드가 파싱 불가가 되어 감사 레코드
보존 원칙에 걸린다. 유료 추출 경로(리스·중복 호출 안전)를 서둘러 바꾸는 것보다 보고가 옳다고
판단했다.

**권장 처리.** 새로 받는 공급자 응답에 한해(과거 payload 검증 경로는 그대로 두고) 빈 문자열로
정규화되는 결손 항목을 스키마 위반으로 처리하고, PR107이 넣은 **기존 보정 추출(최대 2회 전송 한도)**
경로로 보낸다. `>1000자` 항목은 이미 회복 경로가 있으므로 계약을 더 조이지 않는다.
운영 발생 여부는 미확인이다 — 프롬프트 4번이 그 확인이다.

---

## 우선순위 4 — 부서 계정

**착수하지 않았다.** 복구 이후 별도 브랜치·PR 작업이다. 정책·배정은 확정됐고 서버 구현·등록·활성화·
PIN 폐기는 미실시다. 수행기간 등 회사 자료가 부족한 부분은 임의로 채우지 말고 자료 보완 요청으로
분리해야 한다. 계정 임시 비밀번호를 코드·GitHub·로그에 올리지 않는다. 초기 등록은 dry-run 후 신규
계정만 생성하고 기존 비밀번호·역할을 덮어쓰지 않는다.

---

## 배포 순서 주의

`POLICY_VERSION`이 v11 → `pai-loop-requirement-policy-2026.09.07-v12`로 올라간다(PR #111). 이 값은
분석 입력 digest와 저장 basis version에 참여한다.

- 배포 후 v11 스냅샷은 version-stale이 되고, open·미마감 공고가 version-refresh 파티션으로 이동한다.
  그 refresh parent가 같은 공고 키를 예약하므로, **진행 중인 재검토 캠페인이 종료 상태에 도달한 뒤
  v12를 올리는 것이 안전하다.** 캠페인 중에 올리면 이후 캠페인 계획이 예약 충돌로 409가 날 수 있다.
- 정책 버전 변경만으로 고정 추출계약이나 source boundary가 바뀌지는 않으므로
  `REVIEW_CAMPAIGN_CONTRACT_CHANGED`를 유발하지 않고 새 캠페인을 요구하지 않는다.
- 캠페인 완료가 모든 저장 결과의 v12 갱신을 증명하지 않는다. 배포 시점에 따라 v11/v12가 섞인다.
- PR #112는 `POLICY_VERSION`·추출계약·정량 엔진을 바꾸지 않는다. 새 코드
  `XLS_CODEPAGE_UNVERIFIED`는 결정적 실패 코드이므로 배포하면 인코딩 미확정 `.xls`가 조용한 과대
  주장 대신 명시적 REVIEW 마커로 나타난다.

## 확인해 둔 사실

- 인계 패치의 기준 커밋과 패치 커밋은 원격에도 로컬 clone에도 없는 로컬 전용 커밋이다. 그래도 최신
  `main`에 충돌 없이 적용된다.
- 공유 폴더 `PAI_LOOP_codex_repair`의 누적 변경은 프런트엔드 3파일과 프런트 테스트를 제외하고
  **전부 이미 `main`에 병합돼 있다**(`src`/`tests`/`scripts`/`tools`/`workflows` 전 파일 바이트 비교).
  이 폴더는 읽기만 했고 reset/restore/덮어쓰기를 하지 않았다.
- `private-review` 후보 패치 중 `main` 미반영은 **`performance-binding-integration` 하나뿐**이다
  (`PerformanceEligibilityComparison` 심볼이 `main`에 없다). attachment-format-recovery /
  accepted-gap-recovery / schema-corrective-recovery / case-award-evidence /
  reviewed-analysis-campaign / schema-award-integration / unit-award / eligibility-confidence /
  case-contract-compatibility 는 반영 완료다.

## 재현 환경 메모

- Windows 사용자 프로필 폴더(Desktop·Documents·홈)에서는 `git clone`/`git init`이 `.git/config`·
  `.git/description` 쓰기에서 Permission denied로 실패한다. `%LOCALAPPDATA%\Temp` 아래에서는 정상이다.
- worktree마다 **별도 venv**가 필요하다. venv 하나를 공유하면 editable install이 다른 worktree의
  `src`를 가리켜 엉뚱한 코드를 테스트한다.
- pytest는 basetemp를 지정한다: `-q -p no:cacheprovider --basetemp=.pytest-tmp`
  (기본 `pytest-of-<user>` 폴더 소유자가 다르면 PermissionError).
- 로컬에 Node가 없으면 `tests/test_frontend_public_contract.py` 6건과 CI의 n8n 워크플로 검증 게이트를
  로컬에서 실행할 수 없다. 두 PR 모두 `workflows/`·`scripts/`·`manifest.json`을 건드리지 않는다.
- 로컬 Python 3.13, CI 3.12.
