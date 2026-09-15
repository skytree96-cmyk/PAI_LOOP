"""Regression for the structural local-table-absence classifier.

The sentence path binds one regex per observed statement and matches with
``fullmatch``. Measured over the 3,027 distinct production gap declarations it
classified 7 of them; the other 2,192 fell through to
``EXTRACTION_DECLARED_INCOMPLETE``, a terminal code that no sibling attachment
can satisfy and that withholds the whole notice even when another current
attachment proved its quantitative table. 61% of those restated the very same
fact in different words.

These cases pin the classifier that now runs when no sentence regex matched:
what it admits, what it still fails closed on, and the record-level invariant
that replaces the wording-based partiality test.
"""

from __future__ import annotations

import json

import pytest

from pai_loop.quantitative_rule_extraction import (
    merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
)
from pai_loop.source_gap_policy import quantitative_table_local_absence_targets

from test_quantitative_rule_extraction import (
    ATTACHMENT_ID,
    VALID_SOURCE,
    issue_codes,
    payload_with_gap,
    payload_with_table,
    valid_table,
)

# Verbatim production statements. Each says the scoring table is not in this
# document and names no document that must supply one.
UNNAMED_LOCAL_ABSENCES = [
    "제안서 평가 배점표(정량평가 기준표)가 본 공고문에는 포함되어 있지 않음",
    "기술능력평가의 세부 배점표는 본문에 포함되어 있지 않음",
    "정량평가(배점표) 관련 내용이 본 문서에 존재하지 않음",
    "정량 평가 20점, 정성 평가 80점의 세부 배점 기준(항목별 세부 평가표)이 "
    "본문에 제시되지 않음",
    "평가위원회 평가 배점표(세부 평가항목 및 배점 기준)가 본 문서에 포함되어 있지 않음",
]

# The same fact, but pointing at a document that must carry the table.
NAMED_LOCAL_ABSENCES = [
    (
        "제안요청서의 세부 기술능력 평가기준 및 배점표가 본문에 포함되어 있지 않음",
        ("RFP",),
    ),
    (
        "과업지시서의 정량평가표가 본 공고문에 포함되어 있지 않음",
        ("SCOPE",),
    ),
]


@pytest.mark.parametrize("gap", UNNAMED_LOCAL_ABSENCES)
def test_an_unnamed_local_absence_binds_no_sibling_role(gap: str) -> None:
    assert quantitative_table_local_absence_targets(gap) == ()


@pytest.mark.parametrize("gap,document_types", NAMED_LOCAL_ABSENCES)
def test_a_named_local_absence_binds_that_document(
    gap: str, document_types: tuple[str, ...],
) -> None:
    targets = quantitative_table_local_absence_targets(gap)

    assert targets is not None
    assert any(types == document_types for types, _markers in targets)


# Each keeps blocking outright: the direction cannot be read, the pointer names
# no bindable document, a second non-scoring subject is missing with it, part of
# a table is the missing subject, or the source could not be read at all.
@pytest.mark.parametrize("gap", [
    "제안요청서에 본 SOURCE가 포함되지 않아 세부 기술평가 배점표를 확인할 수 없음",
    "정량평가 배점표는 본 공고문에 없고 다른 문서 참조로만 안내됨",
    "제안서 평가 배점표와 수행계획서가 본 공고문에 포함되어 있지 않음",
    "본 공고문의 정량평가표 점수 구간이 제공되지 않음",
    "본 공고문에 정량평가표 일부가 포함되어 있지 않음",
    "정량평가표의 등급 열 대응이 본 공고문에서 불명확함",
    "정량평가 기준표 및 입찰참가자격 세부요건이 본 공고문에 포함되어 있지 않음",
])
def test_an_unclassifiable_or_partial_gap_still_fails_closed(gap: str) -> None:
    assert quantitative_table_local_absence_targets(gap) is None


# The container may be repeated in apposition, narrowed, extended to the rest of
# the manifest, or left without a particle. Each names the same place, and each
# shape appeared in production while the statement fell through as terminal.
@pytest.mark.parametrize("gap", [
    "규격[기술]입찰(제안서) 평가의 세부 배점표(항목별 점수 배분표)는 "
    "본 공고문 내에 포함되어 있지 않아 확인 불가",
    "기술능력평가분야의 세부 배점표가 본 공고문 및 첨부문서에 제시되어 있지 않음",
    "기술능력평가 세부 배점표가 본 첨부문서에 포함되어 있지 않음",
    "제안서 평가 배점표 원문은 본 공고문 발췌본에 제시되어 있지 않음",
    # The artifact is named 기준표/채점표/점수표 about as often as 배점표.
    "정량평가 20점 세부 배점 기준표가 본 공고문에 제시되어 있지 않음",
    "제안서 평가 채점표가 본 문서에 포함되어 있지 않음",
    "항목별 점수표가 본문에 제시되어 있지 않음",
])
def test_a_container_variant_or_table_synonym_is_classified(gap: str) -> None:
    assert quantitative_table_local_absence_targets(gap) is not None


def test_a_named_document_binds_even_when_the_container_is_also_locative() -> None:
    """``제안요청서에 있으나 본 문서에는 없음`` puts both in locative position.

    The attachment being read is named, so the other document is where the table
    IS and binds as the required sibling. Binding is stricter than the unnamed
    fallback, so reading it this way narrows what may resolve the issue.
    """

    targets = quantitative_table_local_absence_targets(
        "제안서 평가 세부 배점표(정량평가 기준표)는 제안요청서에 명시되어 있으나 "
        "본 문서(입찰공고서)에는 포함되어 있지 않음"
    )

    assert targets == ((("RFP",), ("제안요청서",)),)


# A scoring table published in an external rule is outside this notice's
# manifest, so no sibling attachment can supply it. These must stay terminal
# even though the looser container forms above would otherwise admit them.
@pytest.mark.parametrize("gap", [
    "경영상태(신용평가등급) 배점표 [별표 10] 본문 미포함",
    "조달청 일반용역 적격심사세부기준의 세부 배점표가 본 첨부문서에 포함되어 있지 않음",
    "기술능력평가 세부 배점표(협상에 의한 계약체결기준 [별표])는 본 공고문에 첨부되지 않음",
    "본 문서는 용역계약 일반조건(법령/예규) 텍스트로서 정량평가표가 포함되어 있지 않음",
    "시행령이 정한 적격심사 배점표는 본 공고문에 수록되어 있지 않음",
])
def test_an_external_rule_table_is_never_resolvable_by_a_sibling(gap: str) -> None:
    assert quantitative_table_local_absence_targets(gap) is None


def _merge(local_gap: str, *, local_table: dict | None) -> object:
    manifest_sha = "7" * 64
    local_attachment_id = "ATT-LOCAL-1"
    local_record = validate_quantitative_attachment_extraction(
        payload_with_gap(local_gap, table=local_table, document_type="NOTICE"),
        source_text=VALID_SOURCE if local_table is not None else "입찰공고 일반사항",
        attachment_id=local_attachment_id,
        document_sha256="8" * 64,
        manifest_sha256=manifest_sha,
    )
    table_record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )
    return merge_validated_quantitative_records(
        [local_record, table_record],
        expected_documents={
            local_attachment_id: "8" * 64,
            ATTACHMENT_ID: "a" * 64,
        },
        manifest_sha256=manifest_sha,
        attachment_profiles={
            local_attachment_id: {
                "document_type": "NOTICE",
                "source_label": "입찰공고문.pdf",
                "missing_or_unreadable": [local_gap],
            },
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "제안요청서.hwp",
                "missing_or_unreadable": [],
            },
        },
    )


@pytest.mark.parametrize("gap", UNNAMED_LOCAL_ABSENCES)
def test_a_proven_sibling_table_satisfies_an_unnamed_local_absence(gap: str) -> None:
    """The notice's rule source exists, so the profile must not stay terminal."""

    profile = _merge(gap, local_table=None)

    assert "EXTRACTION_DECLARED_INCOMPLETE" not in issue_codes(profile)
    assert profile.status != "INCOMPLETE"
    assert profile.tables


def test_an_unnamed_local_absence_alone_still_blocks_without_a_sibling_table() -> None:
    manifest_sha = "9" * 64
    gap = UNNAMED_LOCAL_ABSENCES[0]
    record = validate_quantitative_attachment_extraction(
        payload_with_gap(gap, document_type="NOTICE"),
        source_text="입찰공고 일반사항",
        attachment_id="ATT-ONLY-1",
        document_sha256="b" * 64,
        manifest_sha256=manifest_sha,
    )
    profile = merge_validated_quantitative_records(
        [record],
        expected_documents={"ATT-ONLY-1": "b" * 64},
        manifest_sha256=manifest_sha,
        attachment_profiles={
            "ATT-ONLY-1": {
                "document_type": "NOTICE",
                "source_label": "입찰공고문.pdf",
                "missing_or_unreadable": [gap],
            },
        },
    )

    assert profile.status == "INCOMPLETE"
    assert not profile.tables


def test_a_declaring_attachment_that_kept_its_own_table_is_never_resolved() -> None:
    """The record, not the wording, decides whether a part is missing.

    An attachment that produced a table and still reports a scoring table
    missing is describing its own table, so a sibling's table cannot stand in
    for it. This invariant is what lets the classifier stop reading partiality
    out of phrases such as 세부 기준 or 항목별 배점.
    """

    manifest_sha = "c" * 64
    gap = UNNAMED_LOCAL_ABSENCES[0]
    sibling_id = "ATT-SIBLING-1"
    sibling_table = json.loads(
        json.dumps(valid_table()).replace(ATTACHMENT_ID, sibling_id)
    )
    sibling_table["table_id"] = "QUANT-TABLE-2"
    declaring = validate_quantitative_attachment_extraction(
        payload_with_gap(gap, table=valid_table(), document_type="NOTICE"),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )
    sibling = validate_quantitative_attachment_extraction(
        payload_with_table(sibling_table),
        source_text=VALID_SOURCE,
        attachment_id=sibling_id,
        document_sha256="d" * 64,
        manifest_sha256=manifest_sha,
    )
    profile = merge_validated_quantitative_records(
        [declaring, sibling],
        expected_documents={ATTACHMENT_ID: "a" * 64, sibling_id: "d" * 64},
        manifest_sha256=manifest_sha,
        attachment_profiles={
            ATTACHMENT_ID: {
                "document_type": "NOTICE",
                "source_label": "입찰공고문.pdf",
                "missing_or_unreadable": [gap],
            },
            sibling_id: {
                "document_type": "RFP",
                "source_label": "제안요청서.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert profile.status == "INCOMPLETE"
