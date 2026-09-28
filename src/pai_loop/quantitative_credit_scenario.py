"""Private, source-bound credit scenarios. Never a verified scoring fact."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


CREDIT_SCENARIO_FACT_KEY = "company.credit_rating.scenario"
CREDIT_SCENARIO_SOURCE = "PRIVATE_CREDIT_SCENARIO"
CREDIT_SCENARIO_WARNING = (
    "현재 신용등급과 단독입찰을 가정한 추정치이며, 공고 조건과 제출 시점의 증빙 재확인이 필요합니다."
)


class CreditScenarioApplication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notice_key: str = Field(min_length=1, max_length=255)
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fact_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    conditions_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class CreditScenarioPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["pai-loop-credit-scenario-1.0.0"] = "pai-loop-credit-scenario-1.0.0"
    certificate_id: UUID
    certificate_registration_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attestation: Literal["HUMAN_APPROVED_CERTIFICATE_SCENARIO"]
    accepted_on: date
    effective_through: date
    qualified_issuer: StrictBool
    procurement_system_visible: StrictBool
    single_unchanged_rating: StrictBool
    same_legal_entity: StrictBool
    no_succession_or_cooperative_exception: StrictBool
    assumption: Literal["SOLE_BID_UNCHANGED_CERTIFICATE"]
    applications: list[CreditScenarioApplication] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_approval(self) -> "CreditScenarioPolicy":
        if not all((self.qualified_issuer, self.procurement_system_visible,
                    self.single_unchanged_rating, self.same_legal_entity,
                    self.no_succession_or_cooperative_exception)):
            raise ValueError("Every common certificate assertion must be explicitly approved.")
        if self.effective_through < self.accepted_on:
            raise ValueError("The scenario horizon precedes its approval.")
        keys = [item.notice_key for item in self.applications]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate notice applications are not allowed.")
        return self


def credit_conditions_sha256(literals: list[str]) -> str:
    return hashlib.sha256(json.dumps(literals, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _compact(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"[\s「」『』‘’“”'\".,·]", "", value)


# Composed, full-consumption grammar for the limited standard clauses supported
# by the common assertions. In particular no wildcard swallows extra conditions.
_GRADE = r"신용평가등급"
_PUBLICATION = r"(?:입찰)?공고일"
_SYSTEM = r"국가종합전자조달시스템"
_ASSESS = r"평가한다"
_ISSUER = (
    r"신용정보의이용및보호에관한법률제2조제8의3에해당하는신용조회사또는"
    r"자본시장과금융투자업에관한법률제335조의3에따라업무를영위하는신용평가사가"
)
_GRADE_TYPES = r"회사채기업어음및기업신용평가등급"
_CERT_DETAIL = (
    r"\(위의신용조회사또는신용평가사가등급평가일및등급유효기간등을명시하여작성한"
    + _GRADE + r"확인서\)"
)
_LATEST = r"가장최근의" + _GRADE
_LOWEST_TIE = (
    r"다만" + _LATEST + r"이다수가있으며그결과가서로다른경우에는가장낮은등급으로" + _ASSESS
)
_PREPUB_BASE = _ISSUER + _PUBLICATION + r"이전에평가하고유효기간내에있는" + _GRADE_TYPES
_CLAUSES = {
    "DOCUMENT_PREPUBLICATION": _PREPUB_BASE + r"(?:" + _CERT_DETAIL + r")?을기준으로" + _ASSESS,
    "SYSTEM_LATEST_PREPUBLICATION": _PREPUB_BASE + r"을" + _SYSTEM + r"에조회된" + _GRADE
        + r"으로평가하되" + _LATEST + r"으로" + _ASSESS + r"(?:" + _LOWEST_TIE + r")?",
    "HIGHER_POINTS_PRESENT_CERTIFICATE": r"평가대상자의회사채\(또는기업어음\)및기업신용평가등급에따른평점이다른경우높은평점으로평가하며"
        + _GRADE + r"확인서가확인되지않은경우에는최저등급으로" + _ASSESS,
    "SYSTEM_VALIDITY": _SYSTEM + r"에서" + _GRADE + r"확인서가확인되지않은경우에는최저등급으로평가하며"
        + r"유효기간시작일또는만료일이" + _PUBLICATION + r"인경우에도유효한것으로" + _ASSESS
        + r"(?:다만" + _PUBLICATION + r"다음날이후에발생또는수정된자료는평가에서제외한다)?",
    "SUCCESSION_EXCEPTION": r"\[주1\]에도불구하고합병또는분할한자가" + _PUBLICATION
        + r"이전에평가한" + _GRADE + r"이없는경우에는입찰서제출마감일전일까지발급된유효기간내에있는"
        + _LATEST + r"으로" + _ASSESS,
    "JOINT_SHARE": r"공동수급체의경우구성원별해당점수에지분율을곱한후그점수들을합산하여최종평가하고"
        + r"평가결과소수점이하의숫자가있는경우소수점다섯째자리에서반올림한다",
    "COOPERATIVE": r"중소기업협동조합이입찰에참여하는경우중소기업협동조합의" + _GRADE + r"으로" + _ASSESS,
}


def recognized_credit_scenario_conditions(literals: list[str]) -> tuple[str, ...] | None:
    """Unknown, contradictory, duplicated or incomplete clauses stay REVIEW."""
    codes: list[str] = []
    for literal in literals:
        matches = [name for name, pattern in _CLAUSES.items() if re.fullmatch(pattern, _compact(literal))]
        if len(matches) != 1 or matches[0] in codes:
            return None
        codes.append(matches[0])
    found = set(codes)
    document = {"DOCUMENT_PREPUBLICATION", "HIGHER_POINTS_PRESENT_CERTIFICATE"}
    system = {"SYSTEM_LATEST_PREPUBLICATION", "SYSTEM_VALIDITY"}
    if not ((document <= found and not found & system) or (system <= found and not found & document)):
        return None
    return tuple(codes)
