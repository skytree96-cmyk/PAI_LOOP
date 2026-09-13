# Fable 후속 검토: 커버리지 복구 설계 반박·확인

2026-09-13. 별도 worktree `PAI_LOOP_coverage_verification_0913`, detached `d817ce5e6157f4cd18b8546bd31f911940240695` 기준이다. 현재 원문 범위 구현 worktree와 미커밋 변경은 건드리지 않았다. 이 보고서만 생성하며 커밋·코드 수정·push·PR·병합·다운로드·유료 호출·운영 DB/n8n/Render 접근은 모두 0회다.

먼저 정정할 핵심은 **항상 재다운로드, 모든 진단에서 같은 상태, 실패 첨부 캠페인만 재시도 가능, Issue 필드 추가 시 모든 record의 fingerprint 변경**이라는 단정이다. 커버리지 우선 진단이라는 방향은 맞지만, 제안된 분류와 계획만으로 29공고가 복구된다는 증거는 없다.

근거의 코드 위치는 모두 이 worktree 기준이다. 아래 `pps_enrichment.py`, `analysis_api.py`, `quantitative_scoring.py`, `quantitative_rule_extraction.py`, `recovery_diagnostics.py`는 `src/pai_loop/` 아래 파일이다. 회사 자료와 공고 원문은 인용하지 않는다.

## 1. A~I 코드 주장 검증

| 주장 | 판정 | 반박·확인 및 근거 |
|---|---|---|
| B: 시도 없음·계약 불일치·세대 교체·record 무효가 모두 같은 공개/진단 상태 | 코드로 확인 — 일부만 맞음 | 선택 시도가 없는 **첨부 행**은 PENDING/COVERAGE, `attempt_contract=NONE`으로 합쳐진다(`pps_enrichment.py:2229`, `recovery_diagnostics.py:194`). 그러나 공고 전체는 선택 시도 전무이면 PENDING/NOT_SELECTED, 일부 있으면 REVIEW/COVERAGE로 다르다(`pps_enrichment.py:1515`). 진단에는 이미 기록된 첨부 수와 동적 정량 오류가 별도로 남는다(`recovery_diagnostics.py:235`, `:237`). |
| B: 최신 거절 시도가 항상 첨부를 사라지게 함 | 코드로 확인 — 반례 있음 | 최신 invalid CURRENT 또는 UNSUPPORTED 뒤에 **이전 valid CURRENT**가 있으면 기본 reader는 이전 것을 선택할 수 있다. 세대 장벽은 구 CASE/구 processing 회복을 막으며 같은 CURRENT의 모든 fallback을 막지 않는다(`pps_enrichment.py:650`, `:657`, `:671`, `:675`; `tests/test_previous_processing_contract.py:154`). |
| E: PPS의 새 영속 검증 기록 생성 경로 | 코드로 확인 — 맞음 | `enrich_notice_from_pps → _enrich_selected_pps_attachment`에서 검증하고 `_persist_extraction_version`으로 저장한다(`pps_enrichment.py:3279`, `:3457`, `:3466`, `:3592`, `:3605`). CLI도 메모리에서 검증 객체를 만들 수 있지만 영속 자격과 저장 경로가 없다(`scripts/replay-quantitative-sources.py:356`). 운영에 실제로 실행됐는지는 이번에 조회하지 않았다. |
| E: 항상 다운로드, 무료 경로는 두 개뿐 | 코드로 확인 — 틀림 | `_current_manifest_attempts → _stored_attachment_result`는 다운로드 전에 재사용하고 종료한다. 호환 구계약도 이 경로의 대상이다(`pps_enrichment.py:3910`, `:3935`, `:3223`). deterministic/cooldown REVIEW, 미지원·파싱 실패 등도 API 호출 0일 수 있다. **호출 0회와 사용 가능한 새 정량 기록 확보는 별개**다. |
| E: 동일 바이트 재사용은 같은 공고 안에서만 | 코드로 확인 — 맞음 | `_accepted_outcome_for_duplicate_content`의 SQL은 `notice_id`와 `file_sha256`을 제한하며 ACCEPTED, 현행 prompt/schema, source/analysis SHA를 확인한다(`pps_enrichment.py:3100`, `:3114`). 같은 attachment ID나 processing/manifest까지 같아야 하는 것은 아니다. 첨부 근거를 다시 연결하고 현재 입력으로 검증한다(`:3457`). 공고 간 재사용 반례는 없다. |
| E: 마감 공고는 복구 불가 | 코드로 확인 — 일반화가 틀림 | OPEN·마감 전 제한은 자동 계획/operation 및 고정 실패 재시도에 적용된다. 일반 직접 batch는 `operation_id=None`이면 해당 inactive 분기를 지나갈 수 있고 취소 공고만 별도 선차단한다(`analysis_api.py:3187`, `:3344`, `:3410`, `:3579`; `pps_enrichment.py:3950`). 이는 실행 경로의 차이이며 마감 공고에 유료 호출하라는 제안이 아니다. 복구 계획은 계속 활성 공고로 제한해야 한다. |
| G: 유료 재시도는 FAILED_ATTACHMENTS 허용 목록·최대 3첨부만 가능 | 코드로 확인 — 범위를 넓혀 해석하면 틀림 | 그 제한은 특정 실패 첨부 캠페인에 맞다(`pps_enrichment.py:112`, `:809`). `retry_reviewed=True, retry_scope=None` 캠페인, 일부 ACCEPTED 정량 검토 재시도, 일반 REVIEW의 24시간 cooldown 이후 경로도 있다(`analysis_api.py:351`; `pps_enrichment.py:747`, `:2396`). MODEL_REFUSAL은 해당 허용 목록 밖이지만 모든 경로의 영구 금지는 아니다. |
| I: Issue 필드 추가 시 저장 record 전부 fingerprint 변경 | 코드로 확인 — 전부는 아님 | dump에 새 필드가 포함되면 기존 issue가 있는 record의 hash는 달라진다. `issues=[]`는 이 변경만으로 달라지지 않는다(`quantitative_rule_extraction.py:124`, `:8023`). 진단을 별도 모델에 두는 권고는 타당하지만 유일한 기술적 선택은 아니다. 이 검토에서는 fingerprint를 변경하지 않았다. |
| A: 실패 정보는 payload에 이미 있음 | 코드로 확인 — nullable·시대 차이 보완 필요 | 상태·오류·버전·result·record를 저장한다(`pps_enrichment.py:2770`). `gateway_failure`, `document_processing`은 조건부다(`:2782`, `:2792`). 정상 `_processing_audit`에는 download_complete/digest_basis가 없고 다운로드 실패 감사 등에서 별도 기록된다(`:3185`, `:3341`). ACCEPTED 추출의 정량 검증이 실패해도 raw result는 남을 수 있다(`:2747`, `:2763`). 모든 버전에 모든 필드가 존재하지는 않는다. |
| C: 프로필은 validate_accepted=False로 구체 오류 보존 | 코드로 확인 — 조건부로 맞음 | `quantitative_scoring.py:1640`의 reader 설정은 맞다. 파싱 가능한 record의 fingerprint 오류는 merge까지 도달한다. record 자체 누락/형태 오류는 VALIDATED_RECORD_MISSING 등으로 합쳐지고, UNSUPPORTED 헤더는 이 설정에서도 제외된다(`:1683`, `:1703`; `pps_enrichment.py:657`). recovery diagnostics도 이 프로필 정보를 포함한다. |
| D: 예산 중단은 NoticeVersion에 없음 | 코드로 확인 — 핵심 맞음 | 예산 break는 첨부 버전을 만들지 않는다(`pps_enrichment.py:3978`). 다만 warnings뿐 아니라 batch item의 document_status, 작업 result_json, 재대기 목록에도 남는다(`analysis_api.py:3535`, `:973`, `:979`). **버전만으로** 미시도 이유 복원 불가는 맞고, 작업 감사까지 정보가 없다는 뜻은 아니다. |
| F: 옛 HWP deterministic 마커가 파서 재시도를 막음 | 코드로 확인 — 조건부 위험 | `.hwp`와 HWP5 파서는 지원된다(`pps_enrichment.py:146`, `document_extraction.py:271`). 선택 가능한 manifest·호환 계약·세대의 deterministic 마커는 시간 경과나 단순 retry ID로 해제되지 않는다(`pps_enrichment.py:2377`, `:3910`). prompt 하나의 일치만으로 충분하지 않다. 다운로드 뒤 matching 경로는 manifest/document/prompt/schema/processing 및 retry boundary까지 검사한다(`:2292`, `:2306`, `:2320`). |
| H: 구계약 raw 무료 진단 | 코드로 확인 — 맞음, 입력 조건 보완 | 로컬 텍스트만으로 충분하지 않다. native bytes SHA가 version·attempt·source-map과 일치하고 raw가 현재 스키마로 파싱돼야 한다. 실제 고정 파서로 검증하되 저장·커버리지·운영 점수 자격은 모두 false다(`scripts/replay-quantitative-sources.py:223`, `:239`, `:249`, `:259`). |

메모리 SYN 확인: 첨부행이 같은 no-attempt/unsupported-only라도 recorded count는 0/1이었다. invalid fingerprint는 진단의 정량 오류에 남았다. 최신 invalid CURRENT/UNSUPPORTED 뒤 valid CURRENT는 각각 ANALYZED/CURRENT가 됐다. Issue dump 필드 추가 가정에서 issue 0개는 hash 동일, 2개는 변경됐다. 테스트 파일이나 결과 파일은 만들지 않았다.

## 2. 30공고·163첨부 분류와 활성 조건

**private 자료로 확인.** 입력은 `cohort_snapshot.private.json`과 `scope_totals_final_offline_replay.private.json`이다. 저장 snapshot 시각은 2026-09-13 10:26:59 UTC, 마감 비교 시각은 같은 날 14:09:59 UTC다. 운영 상태의 후속 변경은 읽지 않았으므로 **현재 운영의 OPEN 여부는 확인 불가**다. 아래 활성은 저장 OPEN 상태와 검토 시각의 미마감을 결합한 값이다.

제안의 단일 코드에는 우선순위가 없다. 이 표는 중복을 피하려고 **최신 manifest-bound 시도 → 직접 다운로드/파싱/모델 결과 실패 → 계약 → record 증명 → 완독·검증 상태** 순으로 손으로 분류했다. 제품 함수는 구현하지 않았다. 역사 전체의 원인이나 실제 reader의 fallback 선택과 같은 뜻으로 쓰지 않는다.

- MODEL_NO_RESULT는 수용 가능한 최종 raw가 없다는 뜻이다. 모델 호출 자체가 없었다는 뜻은 아니다.
- DOWNLOAD_FAILED 2는 ATTACHMENT_TOO_LARGE다. 1개는 download_complete=false가 명시되고, 다른 1개는 옛 payload에 해당 필드가 없다. 후자는 오류 코드의 다운로드 단계 의미로 배정했다(`pps_enrichment.py:1604`).
- PARSE_FAILED 5에는 문서 크기 제한도 포함한다. 파서 결함이 수정됐다는 뜻은 아니다.
- ACCEPTED_INCOMPLETE 11은 record INCOMPLETE 10개와 source_read_complete=false인 NO_TABLE 1개다.
- ACCEPTED_VALID 12는 record proof와 처리 완료가 유효한 첨부다. AVAILABLE 1·NO_TABLE 10·NOT_APPLICABLE 1이며 점수 활성 12건이 아니다.

| 단일 분류 코드 | 전체 첨부 | 활성 공고의 첨부 |
|---|---:|---:|
| NO_ATTEMPT | 51 | 36 |
| UNSUPPORTED_FILE | 0 | 0 |
| DOWNLOAD_FAILED | 2 | 1 |
| PARSE_FAILED | 5 | 4 |
| MODEL_NO_RESULT | 15 | 12 |
| CONTRACT_UNSUPPORTED | 67 | 44 |
| CONTRACT_SUPERSEDED | 0 | 0 |
| RECORD_BINDING_INVALID | 0 | 0 |
| ACCEPTED_INCOMPLETE | 11 | 10 |
| ACCEPTED_VALID | 12 | 11 |
| **합계** | **163** | **118** |

별도 축으로 보면 최신 bound 시도 112개 중 UNSUPPORTED 헤더는 **86**개, 지원 구 CASE 헤더는 **26**개, CURRENT는 **0**개다. 86과 위 67의 차이 19는 직접 실패 원인으로 먼저 분류된 시도다. 숨은 구계약을 허용하거나 실패 원인을 지운 결과가 아니다.

공고 번호는 snapshot의 notices 배열 1-based 순서이며 실제 제목·식별자를 넣지 않는다. 코드 약칭은 N=NO_ATTEMPT, D=DOWNLOAD_FAILED, P=PARSE_FAILED, M=MODEL_NO_RESULT, C=CONTRACT_UNSUPPORTED, I=ACCEPTED_INCOMPLETE, V=ACCEPTED_VALID이다.

| 공고 번호 | 저장 상태 | 마감 전 | 첨부 수 | 단일 코드별 수 |
|---:|---|---|---:|---|
| 1 | OPEN | 아니오 | 2 | I1 V1 |
| 2 | OPEN | 예 | 4 | C3 M1 |
| 3 | OPEN | 예 | 5 | C2 I1 V2 |
| 4 | OPEN | 예 | 5 | C4 M1 |
| 5 | OPEN | 예 | 4 | N4 |
| 6 | OPEN | 아니오 | 3 | N3 |
| 7 | OPEN | 예 | 4 | N2 M1 I1 |
| 8 | OPEN | 아니오 | 4 | C4 |
| 9 | OPEN | 예 | 7 | C4 V3 |
| 10 | OPEN | 예 | 4 | N1 C2 M1 |
| 11 | OPEN | 예 | 4 | C2 M2 |
| 12 | OPEN | 예 | 7 | C4 I1 V2 |
| 13 | OPEN | 예 | 4 | C3 M1 |
| 14 | OPEN | 예 | 4 | C4 |
| 15 | OPEN | 예 | 8 | N8 |
| 16 | OPEN | 예 | 6 | C3 M1 D1 I1 |
| 17 | OPEN | 예 | 9 | N9 |
| 18 | OPEN | 아니오 | 6 | C5 M1 |
| 19 | OPEN | 예 | 5 | N4 I1 |
| 20 | OPEN | 아니오 | 7 | N7 |
| 21 | OPEN | 예 | 7 | C4 M1 I1 V1 |
| 22 | OPEN | 예 | 4 | C2 I2 |
| 23 | OPEN | 예 | 5 | N3 C2 |
| 24 | OPEN | 예 | 4 | N3 I1 |
| 25 | OPEN | 예 | 10 | C4 P4 V2 |
| 26 | OPEN | 아니오 | 8 | C5 M1 P1 D1 |
| 27 | OPEN | 아니오 | 5 | N5 |
| 28 | OPEN | 예 | 4 | C1 M2 V1 |
| 29 | OPEN | 아니오 | 10 | C9 M1 |
| 30 | OPEN | 예 | 4 | N2 M1 I1 |

추가 발견: **6·20·27번은 metadata schema 0.1.0으로 현 reader의 manifest 경계도 무효**다(`pps_enrichment.py:630`). 총 15첨부이며 모두 마감 후다. 위 N은 그 저장 descriptor에 묶인 시도가 없다는 관측값일 뿐, 유효 manifest라는 승인이 아니다. 제안 A의 코드만으로는 이 선행 오류를 표현하지 못하므로 manifest 상태를 별도 축으로 둬야 한다.

| 요청한 복구 집합 계산 | 첨부 / 공고 | 해석 |
|---|---|---|
| 저장 OPEN 및 검토 시각 마감 전 | 118 / 22 | 운영의 최신 상태는 확인 불가 |
| 활성 N ∪ M ∪ C | 36 + 12 + 44 = **92 / 22** | 새 추출을 검토할 후보 상한 |
| 활성 P 중 같은 바이트를 현 파서로 읽는 경우 | **0 / 0** | 실패 4첨부가 한 공고에 있으며 성공한 첨부·공고는 모두 0 |
| 요청식의 최종 상한 | **92첨부 / 22공고** | 실행 계획·성공 예측·확정 점수 수가 아님 |
| 그 집합 밖에 남는 활성 차단 | **15첨부 / 10공고** | I10 + D1 + P4. 위 92개가 모두 성공해도 이 문제는 별도로 남음 |

따라서 92개 재추출만으로 22공고를 완결할 수 있다는 결론도 성립하지 않는다. 기존 규칙 AUTO_ACTIVE 1공고는 마감 후인 1번이며, 활성 22공고는 모두 검토 상태다. 회사 점수는 이 작업에서 평가하지 않았다.

## 3. 구계약 raw 진단과 유료 호출 선별 한계

**private 자료로 확인.** `cohort_snapshot.private.json`, `source_map_before.private.json`의 로컬 원본을 사용했다. 기존 재생에 기록된 구현 파일 SHA는 d817 checkout bytes와 일치하지 않아 그 저장 결과만 믿지 않았다. d817의 `diagnostic_raw_candidate`를 외부 효과 guard 안에서 메모리로 다시 호출했다. 출력 파일·새 record는 생성하지 않았다.

| 범위 / 결과 | 첨부 수 |
|---|---:|
| 단일 분류 CONTRACT_UNSUPPORTED | 67 |
| 위 67의 정확한 로컬 native 확보 | 67 |
| RAW_SCHEMA_INVALID | 0 |
| CURRENT_NATIVE_RAW_DIAGNOSED | 67 |
| 위 진단 중 parser_complete | 64 |
| 저장 canonical과 현재 파서 텍스트 일치 | 35 |
| raw에 표 선언 있음 | 20 |
| raw에 표 선언 없음 | 47 |
| AVAILABLE 표가 있는 첨부 | **0** |
| AVAILABLE / REVIEW 후보 | **0 / 70** |

분류 순서와 무관한 **UNSUPPORTED 헤더 전체 86**의 교차 집계는 진단 67, raw 없음 13, 정확한 원문 매핑 없음 6이다. 로컬 exact source mapping이 있는 부분은 80이다. 그중 raw가 있는 67개에서 schema 오류는 0이며, raw 없는 13개는 schema 평가 불가다.

**판단: 이 결과로 ‘재추출해도 표가 없을 첨부’를 제외하면 안 된다.** 이 함수는 기존 raw의 후보만 원문과 대조한다. 누락됐던 표를 새로 찾거나 미래 추출 결과를 예측하지 않는다. 47개 raw 무표도 원문 무정량 증거가 아니다. 진단 이름의 CURRENT는 현행 계약 수용이나 완독을 뜻하지 않으며, 32개는 저장 당시 canonical과도 다르다. 근거: `scripts/replay-quantitative-sources.py:223`, `:249`, `:259`, `:264`.

## 4. NO_ATTEMPT의 동일 공고 재사용 후보

| 판정 | 확인 범위 | 결과 |
|---|---|---|
| private 자료로 확인 | NO_ATTEMPT 51첨부의 로컬 native SHA 확인 | 51 |
| private 자료로 확인 | 30공고 전체 289개 이력에서 대상과 같은 공고로 제한하고 같은 file/document SHA + 현행 계약 ACCEPTED 비교 | **0** |
| private 자료로 확인 | 같은 비교를 모든 구계약 ACCEPTED로 확장 | **0** |
| private 자료로 확인 | 전체 289개 이력 중 현행 prompt 0.5.7 | **0** |

이 표본의 NO_ATTEMPT를 대상으로 한 STORED_REUSE 기대 효과는 0이다. 기존 시도가 있는 다른 첨부의 다운로드 전 재사용 기능까지 무의미하다는 뜻은 아니다. 이 비교는 새로 다운로드하지 않고 이미 받은 로컬 바이트와 저장 이력만 사용했다. 근거: `cohort_snapshot.private.json`, `parsed_after.private.json` 및 연결된 로컬 파일, `pps_enrichment.py:3100`.

## 5. HWP 마커와 파서 실패

| 판정 | 확인 항목 | 결과 |
|---|---|---|
| 코드로 확인 | `.hwp` 지원 및 HWP5 처리 | 지원됨. `document_extraction.py:271` |
| private 자료로 확인 | 전체 289개 이력의 `.hwp` deterministic 마커 | **0** |
| private 자료로 확인 | 현행 prompt의 과거 HWP_ONLY_UNSUPPORTED_R07 마커 | **0** |
| 코드로 확인 | 같은 계약·manifest·세대에서 마커가 계속 재사용되는 조건 | 존재함. `pps_enrichment.py:2377`, `:3910`. 실제 표본의 발생 증거와 구분 |
| private 자료로 확인 | 활성 PARSE_FAILED 4첨부의 같은 native 바이트 재진단 | TEXT_EMPTY 1은 여전히 비어 있고 미완료, HWP_CONTAINER_INVALID 3은 파서 예외. **성공 0/4** |
| 확인 불가 | 마감 후 TEXT_TOO_LARGE 1첨부의 원래 실패 재현 | 로컬 다운로드와 저장 SHA가 달라 동일 입력 재현으로 세지 않음 |

F의 코드상 위험은 있으나 이 표본의 원인으로 잡고 우선 수정할 근거는 없다. 기존 deterministic 재시도 금지 테스트는 이유를 유지해야 하며 HWP 전용 재현이 필요하다(`tests/test_pps_enrichment.py:2479`).

## 6. 수정안 A·B 평가와 더 작은 대안

| 제안 | 판정 | 필요한 보완 |
|---|---|---|
| A: 별도 관측용 첨부 진단 | 코드로 확인 + 판단 — 채택 가능 | 계약·reader·fingerprint를 바꾸지 않고 서버 전용으로 추가할 수 있다. 최신 metadata schema, 전체 manifest SHA, 첨부 descriptor SHA를 먼저 확인해야 한다. |
| A: 첨부당 코드 하나 | 판단 — 그대로는 불충분 | 최신 실패와 실제 선택된 이전 유효 시도가 공존할 수 있다. `selected_attempt`와 `latest_observed_attempt`, manifest 상태, 계약과 직접 실패 원인을 분리해야 한다. 이 보고서의 단일 코드 표는 통계용 투영이다. |
| B: enrichment dry_run에 예정 행동 추가 | 코드로 확인 + 판단 — 설계 수정 필요 | 현재 dry_run은 PLANNED를 만들 뿐이며 실제 다운로드 전 경로는 `_current_manifest_attempts → _stored_attachment_result`다(`pps_enrichment.py:3873`, `:3910`). 제안한 두 함수만 호출하면 실제 판단과 다를 수 있다. |
| B: 다운로드 전 matching으로 재사용 확정 | 코드로 확인 — 일부 확인 불가 | `_matching_extraction_version`는 document SHA를 요구한다(`pps_enrichment.py:2292`). 아직 확인하지 않은 바이트에 옛 SHA를 대입하면 안 된다. 미확정은 `NEEDS_DOWNLOAD_TO_DECIDE`로 남겨야 하며 NEW_ATTEMPT를 유료 호출 확정으로 표현하지 않는다. |
| B: dry_run을 읽기 전용 운영 계획으로 사용 | 코드로 확인 — 부적절 | enrichment 함수의 dry_run과 API 작업은 다르다. backfill plan은 DRY_RUN parent/lease를 commit하고, 직접 batch도 작업 행을 저장한다(`analysis_api.py:2278`, `:2750`, `:924`). 이번 검증에서는 호출하지 않았다. |
| 더 작은 대안 | 판단 | 이미 존재하는 `pps_recorded_attachment_attempt_count`, recovery의 quantitative 진단, CLI의 latest-bound `source_attempts`를 재사용해 **선택 결과와 관측 원인 차이**만 별도 보고한다. 먼저 snapshot 기반 계획으로 분류·대상·미확정 수를 보여 주고, reader나 Issue 모델에는 필드를 넣지 않는다. |

계약·fingerprint·커버리지·합계 검증 완화 없이 구현 가능하다. 다만 진단 코드를 선택이나 점수 활성 조건으로 재사용하면 안전성이 깨지므로 소비 경계를 분리해야 한다. 중복·다단계 표를 단일표처럼 활성화하거나 0.5.5를 허용할 필요도 없다.

| 회귀 위험 | 확인할 테스트 파일 / 근거 위치 |
|---|---|
| 공개/관리자 진단 분리, recorded count와 선택 count | `tests/test_recovery_diagnostics.py:192`, `:233`, `:384` |
| manifest 오류·동일 세대 fallback·이전 processing 장벽 | `tests/test_pps_enrichment.py:650`, `:2790`; `tests/test_previous_processing_contract.py:106`, `:154`, `:172` |
| 다운로드 전 재사용·동일 바이트/processing·cooldown·deterministic | `tests/test_pps_enrichment.py:1419`, `:2082`, `:2479` |
| retry campaign·inactive·lease·예산 중단 기록 | `tests/test_analysis_api.py:1929` 및 해당 dry_run/backfill/batch 테스트 |
| record proof·fingerprint·첨부 결손 유지 | `tests/test_quantitative_rule_extraction.py:605`, `:2139`, `:2303`; `tests/test_extraction_contract_compatibility.py` |
| raw 진단 비승격·정량 합계·부분 활성 보호 | `tests/test_quantitative_replay_cli.py`, `tests/test_source_gap_quantitative_scope.py`, `tests/test_quantitative_partial_activation.py` |

코드 변경이 없어 전체 테스트는 반복하지 않았다. 독립 검토자의 메모리 SYN, d817 raw 재진단, 전수 분류 및 외부 효과 guard만 수행했다. 새로운 자동 테스트를 추가했다는 주장은 하지 않는다.

## 7. 다음 단계 판단

| 선택 | 판단 | 근거 |
|---|---|---|
| (a) 활성 공고 한정 재추출 범위 결정 | **우선** | snapshot상 22공고·92첨부는 선별 후보 상한이다. 실행 전 최신 lifecycle 확인, 남은 15첨부/10공고의 별도 차단, 원문 판독·무정량 증명 여부를 함께 계획해야 한다. 아직 실행 승인이나 비용 발생은 없다. |
| (b) 0.5.5 정책 변경 | **근거 부족, 제안하지 않음** | 헤더 허용만으로 원문 오류가 풀린다는 근거가 없다. 구계약 raw 67의 AVAILABLE 표는 0이며, 기존 기록을 최신 계약으로 덧씌우지 않는다. |
| (c) 누락 게이트·다표 부분 활성 완화 | **현재 복구의 선행 과제로는 근거 부족** | 첫 차단 29/30은 커버리지다. 다만 이것이 원문 범위 보존 구현이 불필요하다는 증거는 아니다. 저장 경로 복구와 후보 구조 오류는 다른 계층이다. |

### 틀린 주장 목록

- 공개·진단의 모든 구분이 사라지는 것은 아니다. 기록 수·정량 오류·이전 현행 유효 fallback이 남는다.
- 항상 다운로드하지 않으며 API 호출 0인 경로도 두 가지로 제한되지 않는다.
- 마감 공고의 모든 코드 경로가 금지되거나 FAILED_ATTACHMENTS만 재시도 가능한 것은 아니다. 실행 계획은 별도로 활성·승인 경계를 지켜야 한다.
- Issue 필드 추가가 issue 없는 record까지 반드시 바꾸지는 않는다.
- CURRENT_NATIVE_RAW_DIAGNOSED와 AVAILABLE 0은 원문에 정량표가 없다는 증명이 아니다.
- 후보 92첨부 처리가 성공해도 범위 밖 차단 15첨부/10공고가 남는다. 29건 복구 설계가 완성됐다고 볼 수 없다.

### 맞는 주장 목록

- 첨부행의 NONE/PENDING만으로는 중요한 실패 원인을 구분하기 어렵다.
- 버전만으로 예산 때문에 미시도인지 복원할 수 없다.
- 동일 바이트 결과 재사용은 같은 공고 안으로 제한된다.
- 선택 가능한 deterministic 마커가 새 파서 실행을 막는 조건은 존재한다. 실제 표본 영향은 0이다.
- 읽기 전용 진단을 fingerprint 밖에 두고 커버리지부터 측정하자는 방향은 타당하다.

**다음 단계: 선택/관측/manifest 상태를 분리한 무료 계획표로 활성 22공고의 92첨부 후보와 별도 15첨부 차단을 먼저 정리한 뒤, 원문 근거에 따라 재추출 범위를 결정한다.**
