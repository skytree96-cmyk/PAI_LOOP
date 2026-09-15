"""Exact extraction contracts for persisted reads, independent of runtime modules.

Compatibility never rewrites a stored version or blesses an unknown combination.
The predecessor can supply only the CASE vocabulary it originally validated.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, NamedTuple


class ExtractionContract(NamedTuple):
    prompt: str
    schema: str
    validator: str
    processing: str


CURRENT_EXTRACTION_CONTRACT = ExtractionContract(
    # Explicit count intervals/submission state and source-subtotal guidance.
    # Preserve exactly attested predecessor records; a new prompt alone must
    # not turn already audited attachments into another paid extraction queue.
    "pai-loop-extraction-0.5.7",
    "pai-loop-requirements-0.4.2",
    "pai-loop-quantitative-attachment-validator-0.6.20",
    "pps-document-processing-0.5.2",
)
PREVIOUS_PROCESSING_CONTRACT = ExtractionContract(
    # Same prompt/schema/validator and CASE vocabulary; only source processing
    # changes. Read the exact stored proof without rewriting its fingerprint.
    "pai-loop-extraction-0.5.7",
    "pai-loop-requirements-0.4.2",
    "pai-loop-quantitative-attachment-validator-0.6.20",
    "pps-document-processing-0.5.1",
)
PREVIOUS_CASE_CONTRACT = ExtractionContract(
    "pai-loop-extraction-0.5.6",
    "pai-loop-requirements-0.4.1",
    "pai-loop-quantitative-attachment-validator-0.6.18",
    "pps-document-processing-0.5.0",
)
LEGACY_CASE_CONTRACT = ExtractionContract(
    "pai-loop-extraction-0.5.4",
    "pai-loop-requirements-0.4.0",
    "pai-loop-quantitative-attachment-validator-0.6.17",
    "pps-document-processing-0.5.0",
)
EXTRACTION_READ_POLICY_VERSION = "exact-case-contract-read-v3"
LEGACY_CASE_KINDS = frozenset({"LEGACY_CASE_V1", "LEGACY_CASE_V2"})
CURRENT_SEMANTICS_KINDS = frozenset({"CURRENT", "EXACT_PREVIOUS_PROCESSING"})
BOUND_PREDECESSOR_KINDS = LEGACY_CASE_KINDS | {"EXACT_PREVIOUS_PROCESSING"}
ContractKind = Literal[
    "CURRENT", "EXACT_PREVIOUS_PROCESSING", "LEGACY_CASE_V1", "LEGACY_CASE_V2", "UNSUPPORTED"
]


def classify_attempt_header(payload: Mapping[str, object]) -> ContractKind:
    header = (
        payload.get("prompt_version"), payload.get("schema_version"),
        payload.get("processing_version"),
    )
    for name, contract in (
        ("CURRENT", CURRENT_EXTRACTION_CONTRACT),
        ("EXACT_PREVIOUS_PROCESSING", PREVIOUS_PROCESSING_CONTRACT),
        ("LEGACY_CASE_V1", LEGACY_CASE_CONTRACT),
        ("LEGACY_CASE_V2", PREVIOUS_CASE_CONTRACT),
    ):
        if header == (contract.prompt, contract.schema, contract.processing):
            return name
    return "UNSUPPORTED"


def classify_record_contract(
    payload: Mapping[str, object], record: Mapping[str, object],
) -> ContractKind:
    kind = classify_attempt_header(payload)
    contract = {
        "CURRENT": CURRENT_EXTRACTION_CONTRACT,
        "EXACT_PREVIOUS_PROCESSING": PREVIOUS_PROCESSING_CONTRACT,
        "LEGACY_CASE_V1": LEGACY_CASE_CONTRACT,
        "LEGACY_CASE_V2": PREVIOUS_CASE_CONTRACT,
    }.get(kind)
    if contract is None or (
        record.get("prompt_version"), record.get("extraction_schema_version"),
        record.get("validator_version"), payload.get("processing_version"),
    ) != contract:
        return "UNSUPPORTED"
    return kind
