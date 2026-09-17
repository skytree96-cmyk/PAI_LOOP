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

## 4. 사전규격 파이프라인은 만들어놓고 돌리지 않는다

제안요청서를 가져올 다른 통로도 이미 구현돼 있다 —
`prespec_api.py` · `prespec_service.py` · `prespec_models.py`,
테이블 4종, `docs/PPS_PRESPEC_DISCOVERY_v0.9.0.md`.

실제 데이터:

```
pre_specifications                1
pre_specification_documents       2
pre_specification_versions        1
pre_specification_analysis_runs   1
```

원인은 두 가지다.

1. **트리거가 없다.** `workflows/*.json` 중 사전규격을 참조하는 워크플로가 **하나도
   없다.** W10~W14 어디에도 없다.
2. **수동 전용으로 설계돼 있다.** 수집 경로가
   `POST /pre-specifications/search` → `POST /pre-specifications` →
   `POST .../analysis` 3단계이고, 전부 `_require_pre_spec_operator` 로 운영자를
   요구한다. 사람이 하나씩 검색해서 저장해야 한다.

즉 자동 수집이 존재하지 않는다. 남은 1건은 누군가 한 번 시험한 흔적이다.

## 5. 다음 수 (우선순위)

검증기 작업은 8건짜리 문제이므로 뒤로 미룬다.

1. **`getBidPblancListPPIFnlRfpIssAtchFileInfo` 가 실제로 배점표를 주는지 실물 확인.**
   막힌 공고 몇 건의 `bid_notice_no` 로 호출해 응답에 첨부 URL 이 오는지 본다.
   조달청 API 호출이라 LLM 비용은 0. 이 하나가 A(170 공고)의 해결 가능성을 결정한다.
   카탈로그는 "live base URLs, operation names and response fields must be verified
   before production activation" 이라고 경고하고 있으므로 이름만 믿지 말 것.
2. 1이 되면 manifest 확장 → 표본 재추출(~$1)로 배점표가 실제로 파싱되는지 확인 →
   그 다음에 규모를 정한다.
3. 1이 안 되면 사전규격 자동 수집을 붙인다(트리거 + 공고 매칭). 단 사전규격은 공고
   *이전* 문서이므로 최종 배점표와 다를 수 있다. 확인 후 판단.
4. 어느 쪽이든 **"배점표 미첨부"를 명확한 종료 상태로 표시한다.** 지금은 REVIEW 로
   뭉뚱그려져서 사용자가 이유를 알 수 없다. C(원문 오탈자)와 D(도면)는 영구적으로
   점수가 나올 수 없는 종류이므로 특히 구분이 필요하다.

## 6. 이 조사에서 내가 틀렸던 것

1~2차 문서의 4장과 같은 목적이다.

1. **"첨부 예산이 부족해 배점표가 든 첨부를 못 읽는다"** — 틀렸다. 인수인계 문서의
   남은 과제 4번(보강 예산 550초 한계)을 그대로 이어받아 의심했는데, PARTIAL 공고의
   기대 첨부가 100% materialize 돼 있었다. 예산은 이 병목이 아니다.
2. **결손 선언 분류를 키워드로 한 번에 끝내려 했다** — A 를 781 로 과소 계산했다.
   `첨부되지 않` 계열만 넣어서 `미기재`·`명시되지 않`·`특정되지 않` 이 '기타'로 빠졌다.
   1~2차 문서 원칙 5("표본을 눈으로 확인하고 나서 수치를 믿어라") 그대로였다. 표본을
   두 번 보고 나서야 알았다.

## 7. 재현

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
