# 정량점수 조사 3차: 병목은 파싱이 아니라 원문 확보 (2026-09-17)

`docs/QUANTITATIVE_SCORE_INVESTIGATION_20260911.md` 와
`docs/QUANTITATIVE_SCORE_HANDOFF_PROMPT.md` 를 이어받는 문서다. 인수인계 시점의
"첫 번째 할 일"이던 PR #147 은 병합됐고(`52b3bf8`), 이후 #151·#152·#163 과
`82561f3` 까지 정량 작업이 계속됐다. **그런데 정량점수가 나온 공고는 여전히 0건이다.**

이 문서는 왜 0건인지를 저장된 데이터만으로 측정한 결과다. LLM 호출 0회, DB 읽기 전용,
추가 비용 없음.

## 1. 결론

배점표 파싱 검증기를 더 고쳐도 정량점수는 나오지 않는다. **압도적 다수의 공고에서
배점표가 우리가 가진 문서에 애초에 존재하지 않는다.** 배점표는 공고문이 아니라 별도
제안요청서·과업지시서에 있고, 그 문서는 조달청이 공고 첨부(`ntceSpecDocUrl1~10`)로
주지 않는다.

1~2차 조사가 5일간 매달린 "표 구조 해석" 문제는 **공고 8건**에 해당한다.

그 문서를 받아올 조달청 오퍼레이션은 실재한다. 4장에서 직접 호출해 확인했다 —
`getBidPblancListInfoEorderAtchFileInfo` 가 30일에 제안요청서 1,207건을 돌려주고,
우리 열린 공고 43건과 겹친다. 우리는 이 오퍼레이션을 한 번도 부른 적이 없다.

## 2. 측정

### 2.1 깔때기

열린 공고(`status='OPEN' AND deadline >= now()`) 기준.

| 단계 | 수치 |
|---|---|
| 열린 공고 | 221 |
| `notice_versions.document_complete = True` | **17 (7.7%)** |
| 정량점수(`quantitative.total`)에 값이 있는 공고 | **0** |

`score_snapshots` 에서 v13 정책으로 재판정된 40건은 전부
`rule_source_status = INCOMPLETE` 였다. 하나의 예외도 없다.

### 2.2 어디서 막히나

`quantitative.total` 의 `activation_reasons` 를 최신 run 기준으로 분류(191건).

| 막힌 단계 | 공고 | 비율 |
|---|---|---|
| 추출 미완료 (`EXTRACTION_DECLARED_INCOMPLETE`) | 130 | 68% |
| 첨부 미완료 (`ATTACHMENT_INCOMPLETE`) | 45 | 24% |
| 표 해석 단계 도달 | 16 | 8% |

`extraction_status` 로 봐도 같은 그림이다: PARTIAL 167 · METADATA 28 · ACCEPTED 17 ·
REVIEW 9. 그리고 PARTIAL 155건의 `review_code` 는 전부 `R07` 이다.

### 2.3 첨부는 다 받아왔다

예산 부족으로 첨부를 못 받는 것이 아니다.

- PARTIAL 공고 167건의 `expected_attachment_ids` 553개가 `attachment_ids` 553개로
  **100% materialize** 됐다.
- 전체로는 첨부 614개 중 `accepted_attachment_ids` 434개(70.7%).
- 첨부가 전부 accepted 된 공고가 76건인데 `document_complete` 는 17건이다. 즉
  **읽어도 완료가 안 되는 층이 따로 있다.**

### 2.4 추출기가 스스로 남긴 이유

`R07` 의 출처는 `analysis_pipeline.py:558` — `data.missing_or_unreadable` 가
비어있지 않으면 `SOURCE_MISSING_OR_UNREADABLE` 를 붙인다. 이것은 **추출기가 문서를
읽고 직접 선언한 결손**이다. 파이프라인의 실패가 아니다.

열린 공고 177건에서 그 선언 1,056건을 전수 분류했다.

| 원인 | 선언 | 공고 |
|---|---|---|
| **A. 배점표가 문서에 없음** | **781+** | **170** |
| C. 원문 자체가 결손 (오탈자·공란) | 56 | 27 |
| B. 정성평가 항목 (배점표가 없는 게 정상) | 25 | 22 |
| D. 도면·이미지 PDF (텍스트 없음) | 12 | 7 |
| **E. 표 구조 해석 실패** | **9** | **8** |
| F. 미분류 | 173 | 99 |

A 수치는 보수적이다. F(미분류)를 두 번 무작위 표본(각 10~12건)으로 눈으로 확인했더니
매번 9~10건이 다시 A 였다 — `미기재`·`미첨부`·`포함되지 않아`·`참조로만` 처럼 표현만
다른 경우다. 실제 A 비중은 95% 이상이다.

실제 선언 문장:

> 제안서 평가항목 및 배점기준(제안요청서 서식3)은 본 공고문 본문에 첨부되어 있지 않아
> 정량평가표를 확인할 수 없음

> 기술제안서 평가배점표(정량/정성 평가 세부기준)가 본 공고문에 첨부되지 않고 별도
> 제안요청서를 참조하도록 되어 있어 확인 불가

> 첨부 문서에 정량평가(배점) 표는 존재하지 않으며 서약서/확인서 양식만 포함되어 있음

C(원문 결손)도 고칠 수 없는 종류다:

> 제안업체 경영상태 B등급 상한값 '00%' 표기가 원문 오탈자로 추정되어 정확한 수치 확인 불가

## 3. 왜 배점표가 안 들어오나

우리가 읽는 첨부는 공고 목록 응답의 `ntceSpecDocUrl1~10` / `ntceSpecFileNm1~10`
슬롯뿐이다(`pps_enrichment.py:409`, `MAX_ATTACHMENTS_IN_MANIFEST = 10`). 공고당 관측
최대 9개이므로 **상한에 걸린 것도 아니다.** 그 슬롯에 제안요청서가 들어오지 않는다.

현재 호출하는 조달청 오퍼레이션은 8개다.

```
getBidPblancListInfoServcPPSSrch      getScsbidListSttusServc
getOpengResultListInfoOpengCompt      getScsbidListSttusServcPPSSrch
getPublicPrcureThngInfoServc          getScsbidListSttusThngPPSSrch
getScsbidListSttusCnstwkPPSSrch       getScsbidListSttusFrgcptPPSSrch
```

`docs/PPS_API_CATALOG_v0.1.0.md` 가 P0 로 올려둔 다음 두 개는 **한 번도 호출하지 않는다.**

- `getBidPblancListPPIFnlRfpIssAtchFileInfo` — RFP(제안요청서) 발급 첨부파일 정보
- `getBidPblancListInfoEorderAtchFileInfo` — 전자주문 첨부파일 정보

## 4. 확보 통로를 실물로 확인했다

`getBidPblancListInfoEorderAtchFileInfo` 와 `getBidPblancListPPIFnlRfpIssAtchFileInfo`
를 2026-08-18 ~ 09-17 구간으로 직접 호출했다(조달청 오픈API, LLM 비용 0).

두 오퍼레이션 모두 `bidNtceNo` 단건 조회를 받지 않는다. `inqryDiv` + 날짜 구간이
필수이며(단건으로 부르면 resultCode 08), 구간 상한은 1개월이다(초과 시 "입력범위값
초과"). `inqryDiv` 는 Eorder 가 1, PPI 가 2다.

### 4.1 Eorder — 이것이 답이다

`getBidPblancListInfoEorderAtchFileInfo` (inqryDiv=1), 30일 기준:

| 항목 | 값 |
|---|---|
| 공고 | 1,165 |
| 첨부 행 | 1,561 |
| 문서구분 | **제안요청서 1,207** · 기타문서 352 · 긴급발주사유서 2 |
| 우리 열린 공고(221)와 겹침 | **43** |

응답 필드는 `bidNtceNo, bidNtceOrd, atchSno, eorderDocDivNm, eorderAtchFileNm,
eorderAtchFileUrl` 이다. **파일 URL 이 온다.** 실제 파일명:

```
제안요청서.hwpx
붙임4. 제안요청서(2026년도)_조달청 요청사항(2차수정).hwp
붙임3. 과업지시서(2026년도)_조달청 요청사항(2차수정).hwp
```

3장에서 "공고 첨부 슬롯에 들어오지 않는다"고 한 바로 그 문서다.

### 4.2 절반은 레거시 .hwp 다

파이프라인은 PDF 와 HWPX 만 읽는다(`HWP_ONLY_UNSUPPORTED`).

| 범위 | 공고 | 제안요청서 | hwpx | hwp | 즉시 사용 가능 공고 |
|---|---|---|---|---|---|
| 30일 전체 | 1,165 | 1,207 | 622 | 543 | **622** |
| 우리 열린 공고와 겹침 | 43 | 44 | 22 | 21 | **22** |

즉 통로를 붙이면 절반은 바로 읽히고, 절반은 레거시 .hwp 지원이라는 별도 과제가 된다.

### 4.3 PPI 는 이 문제의 답이 아니다

`getBidPblancListPPIFnlRfpIssAtchFileInfo` (inqryDiv=2) 는 30일에 공고 3개,
문서 6건(최종제안요청서 3 · 교부안내서 3)이고 **우리 공고와 겹치는 것이 0개**다.
협상에 의한 계약의 최종 제안요청서 교부 절차용으로 보인다. 카탈로그가 P0 로 올려둔
이름만 보고 이쪽을 먼저 의심했는데 규모가 맞지 않는다.

### 4.4 사전규격 모듈은 이 문제와 무관하다

`prespec_api.py` 는 워크플로 트리거가 없고 `_require_pre_spec_operator` 로 운영자
수동 호출만 받는다. 레코드도 1건이다. 처음에는 이것을 "만들어놓고 안 돌리는" 결함으로
봤으나 **틀렸다.** 사전규격은 조기 발굴을 위해 사람이 검색해서 추가하는 도구이고,
자동 수집이 목적이 아니다. 설계대로 동작하고 있다. 게다가 사전규격은 공고 *이전*
문서라 최종 배점표와 다를 수 있다. 이 문제의 해법은 4.1 이다.

## 5. 파일럿: 그 문서에 배점표가 실제로 있다

4.2 에서 고른 22건(우리 열린 공고 · 문서구분 `제안요청서` · 확장자 `.hwpx`)을 전부
내려받아 내용을 확인했다. LLM 호출 없음, zip/XML 판독만.

| 결과 | 건수 |
|---|---|
| 표가 있고 본문에 `배점` 이 등장 | **20 / 22 (91%)** |
| hwpx 로 열리지 않음 | 2 |

단어만 스친 것이 아니다. 실제로 꺼낸 내용:

```
나. 분야별 배점한도
평가분야              배점     근거
기술능력평가(제안평가)   80점   계약예규 「협상에 의한 계약체결기준」 [별표]
입찰가격평가           20점   동 [별표] 입찰가격 평점산식
합  계               100점
※ 기술능력평가 80점은 정성평가 60점과 정량평가 20점으로 구분한다.
   조달청 세부기준 제9조제4항에 따라 정량평가의 총 배점한도는 20점을 초과하지 아니한다.
```

```
평가구분   배점          평가기관
가격평가   10점          조달청
기술평가   정량적평가 20점  수행 실적, 기술인력 보유현황 / 경영상태, 사회적책임
          정성적평가 70점  조달청
계        100점
```

정량 엔진이 다루는 항목(수행 실적 · 기술인력 · 경영상태)이 배점과 함께 표로 들어있다.
2장에서 추출기가 "확인 불가"라고 선언한 그 표다.

### 5.1 확장자를 믿으면 안 된다

열리지 않은 2건(`R26BK01709389-000`, `R26BK01721354-000`)의 매직바이트는
`d0 cf 11 e0 a1 b1 1a e1` — OLE Compound File, 즉 **이름만 `.hwpx` 인 레거시 .hwp**다.
4.2 의 "hwpx 22건"은 따라서 상한이고 실제로는 20건이다. 확장자 대신 매직바이트로
판정해야 한다.

### 5.2 URL 경로가 기존 허용목록 밖이다

Eorder 첨부 URL 은 `https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/**downloadRfpFile.do**`
이다. 코드의 허용목록은 `G2B_ATTACHMENT_PATH = "/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"`
하나뿐이므로(`pps_enrichment.py:66`), `_safe_g2b_attachment_url` 이 지금은 이 URL 을
거부한다. 통로를 붙일 때 같이 넓혀야 한다.

## 6. 이어서 할 일 (집에서 재개용)

파일럿이 통과했으므로 남은 것은 구현이다. 순서대로.

### 6.1 Eorder 첨부를 manifest 에 넣는다 (핵심)

- `pps_enrichment.py` 에 `getBidPblancListInfoEorderAtchFileInfo` 수집을 추가한다.
  - `inqryDiv=1` + `inqryBgnDt`/`inqryEndDt`, **구간 상한 1개월**이므로 월 단위 페이징.
  - `bidNtceNo` 단건 조회는 받지 않는다(resultCode 08). 구간으로 받아 공고번호로 조인.
  - 응답 필드: `bidNtceNo, bidNtceOrd, atchSno, eorderDocDivNm, eorderAtchFileNm,
    eorderAtchFileUrl`. `eorderDocDivNm = '제안요청서'` 를 우선 취한다.
- `G2B_ATTACHMENT_PATH` 허용목록에 `downloadRfpFile.do` 를 추가한다(5.2).
- `build_attachment_manifest` 를 `ntceSpecDocUrl*` 단독에서 **합집합**으로 바꾼다.
  `MAX_ATTACHMENTS_IN_MANIFEST = 10` 상한을 넘을 수 있으므로 상한 정책을 먼저 정할 것
  (현재 공고당 관측 최대 9 + 제안요청서 1~2).
- 형식 판정을 확장자가 아니라 **매직바이트**로 바꾼다(5.1). `d0cf11e0` 은 레거시 .hwp
  이므로 `HWP_ONLY_UNSUPPORTED` 로 보내야 한다.

### 6.2 확인

W10 에 붙인 뒤 20건을 재분석해 `rule_source_status` 가 `AVAILABLE` 로 바뀌는지 본다.
첨부당 약 $0.108 이므로 **약 $2~3**. 여기서 통과하면 그때 2장의 57개 검증 코드가
비로소 의미를 갖는다 — 지금까지는 그 단계에 도달한 공고가 8건뿐이었다.

### 6.3 커버리지 실측

30일 창에서 우리 열린 공고와 겹친 것이 43/221 이다. 창 밖 공고 때문인지, 애초에
Eorder 첨부가 없는 공고인지 아직 모른다. 6.1 을 붙인 뒤 여러 달을 수집해 실측한다.

### 6.4 레거시 .hwp

제안요청서의 약 45% 다. 변환 라이브러리 도입 비용과 커버리지 증가를 비교해 결정한다.
6.1~6.3 이 끝난 뒤에 판단해도 늦지 않다.

### 6.5 "배점표 미첨부" 를 명확한 종료 상태로

지금은 REVIEW 로 뭉뚱그려져 사용자가 이유를 알 수 없다. 2장 C(원문 오탈자)와 D(도면)는
영구적으로 점수가 나올 수 없으므로 특히 구분이 필요하다.

### 6.6 미뤄둔 것

- **검증기(57개 코드) 작업은 6.2 이후로.** 지금 손대면 8건짜리 문제다.
- W11 스케줄 노드는 `queueName: 'DAILY'` 다. v13 재판정용 BACKFILL 부모
  (`13b4dec4-68e8-4a94-8f6b-f627adbb0ebf`)를 서버가 이어받게 하려면 `'ANY'` 로
  바꿔야 한다(`resumeOnly: true` 는 그대로 두면 새 작업을 만들 수 없다). 끝나면 되돌릴 것.

## 7. 이 조사에서 내가 틀렸던 것

1~2차 문서의 4장과 같은 목적이다.

1. **"첨부 예산이 부족해 배점표가 든 첨부를 못 읽는다"** — 틀렸다. 인수인계 문서의
   남은 과제 4번(보강 예산 550초 한계)을 그대로 이어받아 의심했는데, PARTIAL 공고의
   기대 첨부가 100% materialize 돼 있었다. 예산은 이 병목이 아니다.
2. **"사전규격 모듈을 만들어놓고 안 돌린다"** — 틀렸다. 사용자가 지적한 대로
   사전규격은 사람이 검색해서 추가하는 조기 발굴 도구이고 자동 수집이 목적이 아니다.
   레코드 1건과 트리거 부재를 결함으로 읽었는데 설계대로 동작하는 것이었다.
   이름이 비슷하다는 이유로 해법 후보로 삼은 것이 문제였다.

3. **카탈로그에서 `PPIFnlRfpIss` 라는 이름만 보고 그쪽을 유력 후보로 봤다** — 30일에
   공고 3개, 우리와 겹침 0개였다. 실제로 규모가 있는 것은 이름이 덜 그럴듯한
   `EorderAtchFileInfo` 쪽이었다. 이름 대신 호출해서 셌어야 했다.

4. **결손 선언 분류를 키워드로 한 번에 끝내려 했다** — A 를 781 로 과소 계산했다.
   `첨부되지 않` 계열만 넣어서 `미기재`·`명시되지 않`·`특정되지 않` 이 '기타'로 빠졌다.
   1~2차 문서 원칙 5("표본을 눈으로 확인하고 나서 수치를 믿어라") 그대로였다. 표본을
   두 번 보고 나서야 알았다.

## 8. 재현

전부 읽기 전용 SQL 이다. 핵심 세 개만 적는다.

```sql
-- 깔때기
SELECT v.document_complete, v.extraction_status, count(*)
FROM notices n JOIN notice_versions v ON v.notice_id = n.id
WHERE n.status='OPEN' AND n.deadline >= now()
GROUP BY 1,2;

-- 막힌 단계
SELECT s.basis_json::jsonb->'activation_reasons', count(*)
FROM score_snapshots s WHERE s.score_key='quantitative.total'
GROUP BY 1 ORDER BY 2 DESC;

-- 추출기가 선언한 결손 (원문 그대로)
SELECT n.notice_key, gap
FROM notices n JOIN notice_versions v ON v.notice_id=n.id
CROSS JOIN LATERAL jsonb_array_elements_text(
    COALESCE(v.source_payload::jsonb->'result'->'missing_or_unreadable','[]'::jsonb)) AS gap
WHERE n.status='OPEN' AND n.deadline >= now()
  AND v.source_payload::jsonb->>'kind'='OPENAI_REQUIREMENT_EXTRACTION';
```
