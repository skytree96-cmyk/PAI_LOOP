from __future__ import annotations

import re
import unicodedata


_LEGACY_QUALITATIVE_ONLY_EXCLUSION_RE = re.compile(
    r"(?s)(?:가\.\s*)?평가\s*항목별\s*배점\s*표에서\s*"
    r"매우우수/우수/보통/미흡\s*등급의\s*실제\s*점수\s*구간\s*기준이\s*"
    r"정성\s*평가\s*항목에\s*대해\s*상세\s*서술되지\s*않음\s*"
    r"\(\s*정성\s*평가이므로\s*정량\s*테이블에서\s*제외\s*\)\s*\.?"
)
_QUALITATIVE_RATING_ONLY_EXCLUSION_RE = re.compile(
    r"(?s)정성(?:적)?\s*평가\s*"
    r"(?:\(\s*\d+(?:\.\d+)?\s*점\s*\)\s*)?"
    r"세부\s*평가\s*항목은\s*등급\s*척도\s*"
    r"\(\s*매우우수\s*/\s*우수\s*/\s*보통\s*/\s*미흡\s*\)\s*만\s*"
    r"제시되어\s*있어\s*정성\s*판단\s*항목으로\s*"
    r"정량\s*테이블에서\s*제외(?:됨|함)\s*\.?"
)
_QUALITATIVE_NARRATIVE_ONLY_EXCLUSION_RE = re.compile(
    r"정성(?:적)?\s*평가\s*(?:항목\s*)?"
    r"\((?P<subjects>[^()]{1,160})\)(?:은|는)\s*판단[·/ㆍ]\s*서술형\s*"
    r"(?:평가요소로서\s*정량적\s*채점표\s*규칙이\s*아니어서\s*별도\s*정량\s*테이블로\s*전사하지\s*않음|"
    r"배점으로\s*정량\s*기준표에서\s*제외되어\s*세부\s*채점기준이\s*원문에\s*제시되지\s*않음)\s*[.]?"
)
_NOTICE_REFERENCED_QUANTITATIVE_TABLE_GAP_RE = re.compile(
    r"평가\s*항목\s*및\s*배점\s*기준(?:은\s*['‘]제안요청서\s*참조['’]로만\s*"
    r"안내되어\s*있어\s*본\s*공고문에는\s*실제\s*정량평가\s*배점표"
    r"\(기술능력평가\s*세부항목/배점\)가\s*수록되어\s*있지\s*않음|"
    r"\(정량\s*평가표\)은\s*본\s*공고문에\s*포함되어\s*있지\s*않고\s*"
    r"['‘]제안요청서\s*참조['’]로만\s*명시되어\s*있어\s*세부\s*배점표를\s*확인할\s*수\s*없음)\s*[.]?"
)
_QUALITATIVE_TABLE_LOCAL_ABSENCE_RE = re.compile(
    r"(?s)(?:제안\s*요청서(?:의|\s*내)?\s*)?"
    r"정성(?:적)?\s*평가\s*(?:세부\s*)?(?:배점\s*)?표\s*"
    r"(?:은|는|이|가)?\s*(?:본\s*)?(?:입찰\s*)?공고문(?:\s*본문)?에\s*"
    r"(?:포함되어\s*있지\s*않(?:음|습니다)|"
    r"포함되지\s*않(?:음|았습니다))\s*\.?"
)
_NON_QUANTITATIVE_NOTICE_SCHEDULE_GAP_RE = re.compile(
    r"입찰\s*공고문?\s*\(\s*"
    r"(?:제출|접수)\s*기한\s*등\s*(?:구체\s*)?일정\s*"
    r"\)\s*원문은\s*본\s*첨부에\s*"
    r"(?:포함되어\s*있지\s*않(?:음|습니다)|포함되지\s*않(?:음|았습니다))\s*\.?"
)
_NOTICE_THRESHOLD_TABLE_REFERENCE_RE = re.compile(
    r"(?:기술입찰제안서|제안서)\s*평가결과\s*\d{1,3}(?:\.\d+)?점\s*이상\s*"
    r"득점\s*시\s*적격자로\s*선정한다는\s*임계값만\s*제시되어\s*있으며,\s*"
    r"세부\s*배점\s*항목[·ㆍ,]배점표는\s*본\s*공고문에\s*포함되어\s*있지\s*않고\s*"
    r"별도\s*제안\s*요청서를\s*참조하도록\s*되어\s*있어\s*정량평가표를\s*전사할\s*수\s*없음\s*[.]?"
)
_NOTICE_REFERENCED_SUBMISSION_DATE_RE = re.compile(
    r"제안서\s*제출기한의\s*구체적\s*날짜는\s*본문에\s*명시되지\s*않고\s*"
    r"['‘]공고문\s*명시['’]로만\s*표기되어\s*있어\s*실제\s*마감일자는\s*확인\s*불가\s*[.]?"
)


_QUANTITATIVE_TABLE_LOCAL_ABSENCE_RE = re.compile(
    r"(?:"
    r"(?:제안\s*요청서(?:의|\s*내)?\s*)?"
    r"정량(?:적)?\s*(?:평가\s*)?(?:세부\s*)?"
    r"(?:평가\s*)?(?:배점\s*)?표|"
    r"기술\s*평가\s*세부\s*배점표\s*"
    r"\(\s*제안\s*요청서\s*내\s*배점\s*기준\s*\)"
    r")\s*(?:은|는|이|가)?\s*"
    r"(?:본\s*)?(?:입찰\s*)?공고문(?:\s*본문)?에\s*"
    r"(?:포함되어\s*있지\s*않(?:음|습니다)|"
    r"포함되지\s*않(?:음|았습니다))\s*\.?"
)
_ATTACHMENT_LOCAL_ABSENCE_TERMS = (
    "포함되지",
    "포함되어 있지 않",
    "별도 첨부",
    "별도 문서",
    "별도 제공",
    "첨부되지",
    "제공되지",
    "미포함",
)
_QUANTITATIVE_GAP_TERMS = (
    "정량평가표",
    "정량 평가표",
    "평가배점표",
    "평가 배점표",
    "평가표",
    "배점표",
)
_UNREADABLE_GAP_TERMS = ("판독", "식별 불가", "불명확", "훼손", "흐림")
_PARTIAL_TABLE_GAP_TERMS = (
    "일부",
    "일부분",
    "페이지",
    "행",
    "열",
    "항목",
    "기준",
    "등급",
    "구간",
    "산식",
    "점수",
)
_NON_TABLE_DECISION_GAP_TERMS = (
    "참가자격",
    "입찰참가",
    "자격요건",
    "세부요건",
    "면허",
    "사업자등록",
    "직접생산",
    "지역제한",
    "제출서류",
)
_ATTACHMENT_LOCAL_CONTEXT_TERMS = (
    "이 첨부",
    "해당 첨부",
    "공고문",
    "본문",
    "제안요청서",
    "제안 요청서",
    "과업지시서",
    "과업 지시서",
    "과업내용서",
    "과업 내용서",
    "규격서",
    "사양서",
    "세부사양",
    "시방서",
    "내역서",
)
_SIBLING_DOCUMENT_TARGETS = (
    (("제안요청서", "제안 요청서"), ("RFP",)),
    (("과업지시서", "과업 지시서", "과업내용서", "과업 내용서"), ("SCOPE",)),
    (("입찰공고", "공고문"), ("NOTICE",)),
    (("규격서", "사양서", "세부사양", "시방서", "내역서"), ("RFP", "SCOPE")),
)
_EXPLICIT_TABLE_PROVENANCE_PAREN_RE = re.compile(
    r"\(\s*제안\s*요청서\s*내\s*배점\s*기준\s*\)"
)
_LOCAL_DOCUMENT_PATTERN = (
    r"(?:"
    r"(?:이|해당)\s*첨부(?:\s*본문)?|"
    r"(?:별도\s*)?(?:본\s*)?(?:입찰\s*)?공고문(?:\s*본문)?|"
    r"(?:별도\s*)?제안\s*요청서(?:\s*\(\s*붙임\s*\))?(?:\s*본문)?|"
    r"(?:별도\s*)?과업\s*(?:지시서|내용서)(?:\s*본문)?|"
    r"(?:별도\s*)?(?:규격서|사양서|세부사양|시방서|내역서)(?:\s*본문)?"
    r")"
)
_LOCAL_TABLE_PATTERN = (
    r"(?:"
    r"(?:(?:별도\s*)?제안\s*요청서(?:\s*\(\s*붙임\s*\))?\s*의\s*)?"
    r"(?:"
    r"기술\s*평가\s*세부\s*배점표|"
    r"(?:세부\s*)?(?:정량(?:적)?\s*(?:평가\s*)?)?평가\s*배점표|"
    r"정량(?:적)?\s*(?:평가\s*)?(?:세부\s*)?(?:평가\s*)?"
    r"(?:배점\s*)?표|"
    r"평가표|배점표"
    r")"
    r"(?:\s*\(\s*(?:제안\s*요청서\s*내\s*배점\s*기준|"
    r"정량(?:적)?\s*평가\s*기준)\s*\))?"
    r")"
)
_LOCAL_ABSENCE_PATTERN = (
    r"(?:"
    r"포함되어\s*있지\s*않(?:음|습니다)|"
    r"포함되지\s*않(?:음|았습니다)|"
    r"제공되지\s*않(?:음|았습니다)|"
    r"첨부되지\s*않(?:음|았습니다)|"
    r"미포함|누락"
    r")"
)
_NAMED_DOCUMENT_PATTERN = (
    r"(?:"
    r"(?:입찰\s*)?공고문|입찰\s*공고|"
    r"제안\s*요청서(?:\s*\(\s*붙임\s*\))?|"
    r"과업\s*(?:지시서|내용서)|"
    r"규격서|사양서|세부사양|시방서|내역서"
    r")"
)
_NAMED_DOCUMENT_ATOM_PATTERN = rf"(?:별도\s*)?{_NAMED_DOCUMENT_PATTERN}"
_MISSING_DOCUMENT_LIST_THEN_TABLE_RE = re.compile(
    rf"(?x)^\s*{_NAMED_DOCUMENT_ATOM_PATTERN}"
    rf"(?:\s*(?:와|과|및|[,·])\s*{_NAMED_DOCUMENT_ATOM_PATTERN})*"
    rf"\s*(?:이|가|은|는)?\s*(?:별도\s*)?"
    rf"(?:제공|첨부|포함)되지\s*않아\s*"
    rf"{_LOCAL_TABLE_PATTERN}\s*(?:을|를)?\s*"
    rf"확인(?:할)?\s*수\s*없(?:음|습니다)\s*[.]?\s*$"
)
_PRODUCTION_RFP_SOURCE_LOCAL_TECHNICAL_TABLE_GAP_RE = re.compile(
    r"(?x)^\s*"
    r"제안\s*요청서\s*원문\s*\(\s*붙임\s*\)\s*"
    r"(?:이|가|은|는)\s*본\s*SOURCE\s*(?:에|에는)\s*"
    r"포함되지\s*않아\s*세부\s*기술\s*평가\s*배점표\s*"
    r"(?:을|를)\s*확인(?:할)?\s*수\s*없(?:음|습니다)\s*[.]?\s*$"
)
# A whole named RFP absence with an explicitly quantitative technical table.
# Full matching prevents other missing subjects, unreadable cells, or broad
# technical/qualitative prose from becoming a sibling-table shortcut.
_RFP_ORIGINAL_QUANTITATIVE_TECHNICAL_TABLE_GAP_RE = re.compile(
    r"제안\s*요청서\s*원문\s*(?:이|가|은|는)\s*"
    r"첨부되지\s*않아\s*기술\s*능력\s*평가\s*세부\s*배점표\s*"
    r"\(\s*정량(?:적)?\s*평가\s*기준\s*\)\s*(?:을|를)\s*"
    r"확인(?:할)?\s*수\s*없(?:음|습니다)\s*[.]?"
)
_OWNED_TABLE_IN_LOCAL_DOCUMENT_ABSENCE_RE = re.compile(
    rf"(?x)^\s*{_NAMED_DOCUMENT_ATOM_PATTERN}\s*의\s*"
    rf"{_LOCAL_TABLE_PATTERN}\s*(?:은|는|이|가)?\s*"
    rf"{_LOCAL_DOCUMENT_PATTERN}\s*(?:에|에는|에서)?\s*"
    rf"{_LOCAL_ABSENCE_PATTERN}\s*[.]?\s*$"
)
_TABLE_ONLY_LOCAL_GAP_RE = re.compile(
    rf"(?x)^\s*(?:"
    rf"{_LOCAL_TABLE_PATTERN}\s*(?:은|는|이|가)?\s*"
    rf"{_LOCAL_DOCUMENT_PATTERN}\s*(?:에|에는|에서)?\s*"
    rf"{_LOCAL_ABSENCE_PATTERN}|"
    rf"{_LOCAL_DOCUMENT_PATTERN}\s*(?:에는|에|의|은|는|이|가)?\s*"
    rf"{_LOCAL_TABLE_PATTERN}\s*(?:은|는|이|가|을|를)?\s*"
    rf"{_LOCAL_ABSENCE_PATTERN}|"
    rf"(?:{_LOCAL_DOCUMENT_PATTERN}\s*(?:에는|에|은|는)\s*)?"
    rf"{_LOCAL_DOCUMENT_PATTERN}\s*(?:의\s*)?본문(?:은|는|이|가)?\s*"
    rf"제공되지\s*않아\s*{_LOCAL_TABLE_PATTERN}\s*(?:을|를)?\s*"
    rf"확인(?:할)?\s*수\s*없(?:음|습니다)"
    rf")\s*[.]?\s*$"
)
_SOURCE_ABSENCE_CLAIM_RE = re.compile(
    r"(?:"
    r"포함되어\s*있지\s*않(?:음|습니다)|"
    r"포함되지\s*않(?:음|았습니다)|"
    r"제공되지\s*않(?:음|았습니다)|"
    r"첨부되지\s*않(?:음|았습니다)|"
    r"미포함|누락"
    r")"
)
_SECONDARY_UNAVAILABLE_CLAIM_RE = re.compile(
    r"(?:[.;]|그리고|또한|아울러)\s*\S.{0,240}?"
    r"(?:확인(?:할)?\s*수\s*없(?:음|습니다)|정보가\s*없음|내용이\s*없음)"
)


def _strip_unicode_format_controls(value: str) -> str:
    """Remove invisible format controls before matching persisted labels/gaps."""

    return "".join(character for character in value if unicodedata.category(character) != "Cf")


def normalise_source_gap(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", _strip_unicode_format_controls(value))
    return re.sub(r"\s+", " ", normalized).strip()


def _compact_document_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", _strip_unicode_format_controls(value))
    return re.sub(r"\s+", "", normalized).casefold()


def has_compound_source_absence_claim(value: str) -> bool:
    """Reject a sibling shortcut when one gap declares another missing subject."""

    gap = normalise_source_gap(value)
    if len(tuple(_SOURCE_ABSENCE_CLAIM_RE.finditer(gap))) > 1:
        return True
    return bool(_SECONDARY_UNAVAILABLE_CLAIM_RE.search(gap))


def _contains_partial_quantitative_gap_term(value: str) -> bool:
    scan_value = _EXPLICIT_TABLE_PROVENANCE_PAREN_RE.sub("", value)
    return any(term in scan_value for term in _PARTIAL_TABLE_GAP_TERMS)


def _table_is_conjoined_with_another_missing_subject(
    gap: str,
    table_positions: list[re.Match[str]],
) -> bool:
    """Reject a table mention joined to any second, unproved missing subject."""

    for table in table_positions:
        prefix = gap[max(0, table.start() - 100) : table.start()]
        suffix = gap[table.end() : min(len(gap), table.end() + 100)]
        if re.match(r"\s*(?:와|과|및|·|,|，)\s*\S", suffix):
            return True
        if re.search(r"(?:와|과|및|·|,|，)\s*$", prefix) and not re.search(
            r"평가\s*항목\s*및\s*$",
            prefix,
        ):
            return True
    return False


def _named_quantitative_table_target_terms(
    gap: str,
    *,
    absence_positions: list[re.Match[str]],
    table_positions: list[re.Match[str]],
) -> set[str]:
    """Return only document names that own the missing table/body.

    A container phrase such as ``공고문에는`` describes where a body was
    expected; it is not itself the missing sibling.  Ownership is therefore
    limited to an explicit ``DOCUMENT의 TABLE`` provenance, a named missing
    ``DOCUMENT 본문``, or a complete list of named documents that were not
    supplied before the table became unavailable.
    """

    named_positions = [
        (term, match)
        for markers, _document_types in _SIBLING_DOCUMENT_TARGETS
        for term in markers
        for match in re.finditer(re.escape(term), gap)
    ]
    targets: set[str] = set()

    if _EXPLICIT_TABLE_PROVENANCE_PAREN_RE.search(gap):
        targets.add("제안요청서")

    for term, context in named_positions:
        for table in table_positions:
            if context.end() > table.start():
                continue
            ownership_bridge = gap[context.end() : table.start()]
            if re.fullmatch(
                r"\s*(?:\(\s*붙임\s*\))?\s*(?:의|내)\s*(?:세부\s*)?",
                ownership_bridge,
            ):
                targets.add(term)

        for absence in absence_positions:
            if context.end() > absence.start():
                continue
            body_bridge = gap[context.end() : absence.start()]
            if not re.fullmatch(
                r"\s*(?:\(\s*붙임\s*\))?\s*(?:의\s*)?"
                r"본문(?:에는|에|이|가|은|는)?\s*",
                body_bridge,
            ):
                continue
            if any(absence.end() <= table.start() for table in table_positions):
                targets.add(term)

    if _MISSING_DOCUMENT_LIST_THEN_TABLE_RE.fullmatch(gap):
        first_absence_start = min(
            (match.start() for match in absence_positions),
            default=len(gap),
        )
        targets.update(
            term
            for term, context in named_positions
            if context.start() < first_absence_start
        )

    return targets


def is_explicit_qualitative_only_exclusion(value: str) -> bool:
    """Return true only for an exhaustive, explicitly non-quantitative statement."""

    gap = normalise_source_gap(value)
    narrative = _QUALITATIVE_NARRATIVE_ONLY_EXCLUSION_RE.fullmatch(gap)
    narrative_only = bool(narrative and not re.search(
        r"정량|신용|실적|재무|자격|입찰|필수", narrative.group("subjects")
    ))
    return bool(
        gap
        and (
            _LEGACY_QUALITATIVE_ONLY_EXCLUSION_RE.fullmatch(gap)
            or _QUALITATIVE_RATING_ONLY_EXCLUSION_RE.fullmatch(gap)
            or narrative_only
        )
    )


def is_explicit_qualitative_table_local_absence(value: str) -> bool:
    """Recognise only an exhaustive qualitative-table local absence."""

    gap = normalise_source_gap(value)
    return bool(gap and _QUALITATIVE_TABLE_LOCAL_ABSENCE_RE.fullmatch(gap))


def is_explicit_non_quantitative_notice_schedule_gap(value: str) -> bool:
    """Recognise one bounded notice-schedule omission as irrelevant to scoring."""

    gap = normalise_source_gap(value)
    return bool(gap and (
        _NON_QUANTITATIVE_NOTICE_SCHEDULE_GAP_RE.fullmatch(gap)
        or _NOTICE_REFERENCED_SUBMISSION_DATE_RE.fullmatch(gap)
    ))


def is_explicit_quantitative_table_local_absence(value: str) -> bool:
    """Recognise only a quantitative-table-only local absence statement."""

    gap = normalise_source_gap(value)
    return bool(gap and _QUANTITATIVE_TABLE_LOCAL_ABSENCE_RE.fullmatch(gap))


def quantitative_table_local_absence_targets(
    value: str,
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] | None:
    """Classify a complete table-local gap and bind every named sibling role."""

    gap = normalise_source_gap(value)
    if (
        not gap
        or not re.fullmatch(r"(?s).{1,1000}", gap)
        or any(term in gap for term in _UNREADABLE_GAP_TERMS)
        or any(term in gap for term in _NON_TABLE_DECISION_GAP_TERMS)
        or is_explicit_qualitative_table_local_absence(gap)
        or ("정성" in gap and "정량" not in gap)
        or has_compound_source_absence_claim(gap)
    ):
        return None
    if (
        _PRODUCTION_RFP_SOURCE_LOCAL_TECHNICAL_TABLE_GAP_RE.fullmatch(gap)
        or _RFP_ORIGINAL_QUANTITATIVE_TECHNICAL_TABLE_GAP_RE.fullmatch(gap)
        or _NOTICE_THRESHOLD_TABLE_REFERENCE_RE.fullmatch(gap)
        or _NOTICE_REFERENCED_QUANTITATIVE_TABLE_GAP_RE.fullmatch(gap)
    ):
        return ((("RFP",), ("제안요청서",)),)
    if not (
        _TABLE_ONLY_LOCAL_GAP_RE.fullmatch(gap)
        or _MISSING_DOCUMENT_LIST_THEN_TABLE_RE.fullmatch(gap)
        or _OWNED_TABLE_IN_LOCAL_DOCUMENT_ABSENCE_RE.fullmatch(gap)
    ):
        return None

    context_positions = [
        (term, match)
        for term in _ATTACHMENT_LOCAL_CONTEXT_TERMS
        for match in re.finditer(re.escape(term), gap)
    ]
    absence_positions = [
        match
        for term in _ATTACHMENT_LOCAL_ABSENCE_TERMS
        for match in re.finditer(re.escape(term), gap)
    ]
    table_positions = [
        match
        for term in _QUANTITATIVE_GAP_TERMS
        for match in re.finditer(re.escape(term), gap)
    ]
    if not context_positions or not absence_positions or not table_positions:
        return None
    if _table_is_conjoined_with_another_missing_subject(gap, table_positions):
        return None
    if any(
        _contains_partial_quantitative_gap_term(gap[: table.start()])
        for table in table_positions
    ):
        return None

    qualifying_context_terms = _named_quantitative_table_target_terms(
        gap,
        absence_positions=absence_positions,
        table_positions=table_positions,
    )

    if not qualifying_context_terms:
        # The statement is still an exact attachment-local table absence, but
        # it names no sibling that may resolve it (for example, ``이 첨부``).
        return ()
    compact_contexts = {
        _compact_document_label(term) for term in qualifying_context_terms
    }
    targets: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for markers, document_types in _SIBLING_DOCUMENT_TARGETS:
        compact_markers = tuple(
            sorted(
                {
                    _compact_document_label(marker)
                    for marker in markers
                    if _compact_document_label(marker) in compact_contexts
                }
            )
        )
        if compact_markers:
            targets.append((document_types, compact_markers))
    return tuple(targets)


def source_label_document_types(value: str | None) -> tuple[str, ...]:
    """Infer only unambiguous primary/auxiliary roles from a manifest label."""

    if not isinstance(value, str) or not value.strip():
        return ()
    compact = _compact_document_label(value)
    matches: set[str] = set()
    if any(marker in compact for marker in ("입찰공고", "공고문")):
        matches.add("NOTICE")
    if "제안요청서" in compact:
        matches.add("RFP")
    if any(marker in compact for marker in ("과업지시서", "과업내용서")):
        matches.add("SCOPE")
    if any(
        marker in compact
        for marker in (
            "작성양식",
            "제출서식",
            "서식",
            "양식",
            "별지",
            "예시",
            "예제",
            "샘플",
            "견본",
            "template",
            "참고",
            "부록",
            "초안",
        )
    ) or "제안요청서(안)" in compact:
        matches.add("FORM")
    return tuple(
        item for item in ("NOTICE", "RFP", "SCOPE", "FORM") if item in matches
    )


# The classifiers above transcribe one observed sentence each and match with
# ``fullmatch``. Model-authored gap prose is free text, so that shape cannot
# keep up: measured against 3,027 distinct production statements it recognised
# 6. The predicate below classifies instead of transcribing. A gap blocks the
# quantitative path when the source is unreadable, when an omission is stated
# outright, when a second clause raises its own gap, or when it names a scoring
# artifact and claims that artifact is absent. A missing submission deadline,
# page number, or contract term does none of those and cannot change an
# objective score program, so it must not withhold one.
_SCORING_ARTIFACT_RE = re.compile(
    r"배점|평가\s*표|평가\s*기준\s*표|채점|"
    r"(?:점수|평가|배점|등급)\s*구간|점수\s*(?:기준|산정)|"
    r"(?:등급|항목|구간)\s*별?\s*점수|"
    r"정량\s*(?:적)?\s*(?:평가|기준|테이블)|평점\s*산식|가중치|만점|"
    r"등급\s*(?:별)?\s*기준|산식"
)
# ``정량 테이블에서 제외`` names the artifact a row was kept out of. That is a
# scoping decision about where a row went, not a claim that the table is gone.
_EXCLUSION_TARGET_RE = re.compile(
    r"정량(?:적)?\s*(?:평가\s*)?(?:테이블|기준표|배점표|평가표|표)"
    r"\s*(?:에서|에|으로|로)?\s*"
    r"(?:제외|전사하지\s*않|반영하지\s*않|포함하지\s*않)"
)
# ``정성평가표``/``정성 판단 항목`` belong to the qualitative half of a mixed
# sentence. Only the artifact directly qualified by 정성 is masked, so
# ``정성평가 기준과 정량평가표의 …`` keeps its quantitative subject.
_QUALITATIVE_ARTIFACT_RE = re.compile(
    r"정성(?:적)?\s*(?:평가|판단)?\s*(?:세부\s*)?(?:평가\s*)?(?:배점\s*)?"
    r"(?:표|항목|기준(?:표)?|점수|요소|구간)"
)
# Unreadable source blocks scoring whatever the sentence names: nothing in the
# table can be verified against an original nobody could read.
_SOURCE_UNREADABLE_RE = re.compile(
    r"판독\s*(?:이\s*)?(?:할\s*수\s*)?(?:없|불가)|훼손|흐려|흐림|"
    r"식별\s*(?:할\s*수\s*)?(?:없|불가)|불명확"
)
# A second clause raising its own gap keeps the whole statement blocking, so an
# irrelevant lead sentence cannot carry a real omission through with it.
_SECONDARY_GAP_CLAIM_RE = re.compile(
    # ``·`` is a Korean word joiner (입찰·계약, 판단·서술), never a clause break.
    r"(?:[.;]|그리고|또한|아울러|하며|으며)\s*\S.{0,200}?"
    r"(?:확인(?:할\s*수)?\s*(?:없|불가)|판독\s*(?:할\s*수\s*)?(?:없|불가)|"
    r"정보가\s*없음|내용이\s*없음)"
)
_ABSENCE_CLAIM_RE = re.compile(
    # Match the negation form, not a list of verbs. Enumerating stems leaked
    # repeatedly - ``배점표 미제공``, ``배점표가 존재하지 않음``, ``산식 추출 불가``
    # and ``판독되지 않음`` each named a scoring artifact and each was read as
    # irrelevant, releasing a score with no rule behind it. Over-recognising an
    # absence only withholds a score, which is the safe direction here, and the
    # artifact requirement in the caller already keeps the scope narrow.
    r"[가-힣]지\s*않|"
    r"미(?:포함|제공|첨부|기재|명시|제시|확인|수록|산정|공개)|"
    r"불가|부재|누락|결락|손상|훼손|불명확|"
    r"없(?:음|으며|고|어|다|는|이)"
)
# A deliberate qualitative exclusion is a scoping decision, not a source defect.
_QUALITATIVE_SCOPE_RE = re.compile(
    r"정성(?:적)?|등급\s*척도|매우우수|서술형|판단[·ㆍ/]?\s*서술|주관적"
)
_DELIBERATE_EXCLUSION_RE = re.compile(
    r"제외|전사하지\s*않|반영하지\s*않|포함하지\s*않|대상(?:이)?\s*아(?:님|니)"
)
# An explicit omission blocks whatever it names. ``정성평가 일부가 누락되어 정량
# 테이블에서 제외`` reads as a scoping decision but states a real source omission
# first, and an attachment missing part of itself cannot prove its table whole.
_OMISSION_RE = re.compile(r"누락|결락|빠(?:져|짐|뜨)")


def _has_unqualified_scoring_artifact(gap: str) -> bool:
    """True when a scoring artifact is named as its own missing subject.

    Artifacts that only appear as the destination of a qualitative exclusion,
    or that 정성 directly qualifies, are masked out first. Masking beats a
    proximity window: ``정성평가 기준과 정량평가표의 …`` puts a quantitative
    subject within a few characters of 정성 while still asserting a real gap.
    """

    masked = _QUALITATIVE_ARTIFACT_RE.sub(" ", _EXCLUSION_TARGET_RE.sub(" ", gap))
    return bool(_SCORING_ARTIFACT_RE.search(masked))


def asserts_scoring_artifact_absence(value: str) -> bool:
    """True when the gap withholds something an objective score needs.

    Exposed so the attachment diagnostics and the corpus regression can assert
    the same predicate the aggregate gate uses.
    """

    gap = normalise_source_gap(value)
    if not gap:
        return False
    if (
        _SOURCE_UNREADABLE_RE.search(gap)
        or _OMISSION_RE.search(gap)
        or _SECONDARY_GAP_CLAIM_RE.search(gap)
    ):
        return True
    if (
        _QUALITATIVE_SCOPE_RE.search(gap)
        and _DELIBERATE_EXCLUSION_RE.search(gap)
        and not _has_unqualified_scoring_artifact(gap)
    ):
        return False
    return bool(
        _has_unqualified_scoring_artifact(gap) and _ABSENCE_CLAIM_RE.search(gap)
    )


def is_quantitative_irrelevant_gap(value: str) -> bool:
    """Keep only source gaps that can change an objective score program."""

    return bool(
        is_explicit_qualitative_only_exclusion(value)
        or is_explicit_qualitative_table_local_absence(value)
        or is_explicit_non_quantitative_notice_schedule_gap(value)
        or not asserts_scoring_artifact_absence(value)
    )


__all__ = [
    "has_compound_source_absence_claim",
    "is_explicit_non_quantitative_notice_schedule_gap",
    "is_explicit_quantitative_table_local_absence",
    "is_explicit_qualitative_only_exclusion",
    "is_explicit_qualitative_table_local_absence",
    "asserts_scoring_artifact_absence",
    "is_quantitative_irrelevant_gap",
    "normalise_source_gap",
    "quantitative_table_local_absence_targets",
    "source_label_document_types",
]
