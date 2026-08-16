from __future__ import annotations

from enum import StrEnum


class Eligibility(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"


class EvidenceStatus(StrEnum):
    VERIFIED = "VERIFIED"
    PROVISIONAL = "PROVISIONAL"
    MISSING = "MISSING"
    OVERRIDDEN = "OVERRIDDEN"


class ReadinessStatus(StrEnum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    GRAY = "GRAY"


class RiskBand(StrEnum):
    GO = "GO"
    CONDITIONAL_GO = "CONDITIONAL_GO"
    NO_GO = "NO_GO"
    UNKNOWN = "UNKNOWN"


class DecisionChoice(StrEnum):
    GO = "GO"
    CONDITIONAL_GO = "CONDITIONAL_GO"
    NO_GO = "NO_GO"
    HOLD = "HOLD"


class AtomicOperator(StrEnum):
    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    CONTAINS = "contains"
    GTE = "gte"
    LTE = "lte"
    EXISTS = "exists"

