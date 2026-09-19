"""첨부 manifest 의 정원. 응답 스키마와 수집·추출이 같은 값을 보게 한다.

이 값들은 원래 `pps_enrichment` 안에 있었고, 응답 스키마 쪽은 같은 숫자를
리터럴로 따로 적고 있었다. 제안요청서 정원을 더하면서 한쪽만 올라가자 공고
상세·요약 응답이 검증에서 막혀 500 이 났다. 무거운 `pps_enrichment` 를
import 하지 않고도 참조할 수 있도록 여기로 분리한다.

`pps_enrichment` 는 하위 호환을 위해 이 이름들을 다시 내보낸다.
"""

from __future__ import annotations

# 조달청 공고가 선언하는 첨부 슬롯은 정확히 열 개다(`ntceSpecDocUrl1~10`).
MAX_ATTACHMENTS_IN_MANIFEST = 10

# 제안요청서는 그 슬롯 바깥의 전자주문 오퍼레이션에서 오므로 별도 정원을 준다.
MAX_RFP_ATTACHMENTS_IN_MANIFEST = 2

# 한 공고의 manifest 가 가질 수 있는 최대 첨부 수.
MAX_MANIFEST_ATTACHMENTS = (
    MAX_ATTACHMENTS_IN_MANIFEST + MAX_RFP_ATTACHMENTS_IN_MANIFEST
)

# 공고 상세의 첨부 상태 목록은 유효 첨부마다 한 행을 내고, 검증에 실패한 항목이
# 있으면 "첨부 목록 확인 필요" 행을 하나 더 붙인다.
MAX_ATTACHMENT_STATUS_ROWS = MAX_MANIFEST_ATTACHMENTS + 1
