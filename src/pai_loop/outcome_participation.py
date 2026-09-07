"""Server-owned, redacted proof for one company's exact opening participation."""
from __future__ import annotations

import re
from typing import Any

from .outcome_identity import normalise_opening_identity

PARTICIPATION_KIND = "PROVIDER_PARTICIPANT_EXACT"
PARTICIPATION_OPERATION = "as/ScsbidInfoService/getOpengResultListInfoOpengCompt"
# The existing approved company identifier is verified against this public
# organization source; certificates, contacts and identifiers are not copied.
COMPANY_IDENTITY_SOURCE = "https://www.kma.or.kr/kr/usrs/eduRegMgnt/eduRegMgntForm.do?cateNm=abtKma"


def provider_participation_verified(source: str, status: str, evidence: Any) -> bool:
    if source != "PPS_AUTO_FEEDBACK" or status not in {"WON", "LOST"} or not isinstance(evidence, dict):
        return False
    proof = evidence.get("participation_basis")
    opening = normalise_opening_identity(evidence.get("opening_identity"))
    exact = evidence.get("exact_match")
    return bool(
        isinstance(proof, dict) and proof.get("kind") == PARTICIPATION_KIND
        and proof.get("operation") == PARTICIPATION_OPERATION
        and proof.get("company_identity_source") == COMPANY_IDENTITY_SOURCE
        and proof.get("complete") is True and proof.get("company_identifier_match") is True
        and proof.get("winner_is_company") is (status == "WON")
        and opening is not None and normalise_opening_identity(proof.get("opening_identity")) == opening
        and isinstance(exact, dict) and exact.get("verified") is True
        and normalise_opening_identity({**opening, "bid_notice_no": exact.get("bid_notice_no"),
                                        "revision_no": exact.get("revision_no")}) == opening
        and proof.get("final_result_sha256") == evidence.get("provider_result_sha256")
        and all(isinstance(proof.get(field), str) and re.fullmatch(r"[0-9a-f]{64}", proof[field])
                for field in ("final_result_sha256", "participant_result_sha256"))
    )
