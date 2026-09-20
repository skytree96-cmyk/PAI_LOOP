"""SYN-only search-stop diagnostics; no source ownership or score permission."""
from dataclasses import FrozenInstanceError, asdict
from copy import deepcopy
import hashlib

import pytest

from pai_loop.integrations.openai_extraction import ExtractionPayload
import pai_loop.quantitative_rule_extraction as qre
from test_quantitative_count_ranges import ATT, fixture, record


def window(*, size=12, boundary=None, fence=None, blank=None, section=None, header=(0, 1)):
    lines = [f"SYN paragraph {index}" for index in range(size)]
    if blank is not None:
        lines[blank] = ""
    if section is not None:
        lines[section] = "[HWP SECTION 1]"
    lines = tuple(lines)
    kwargs = dict(
        header_span=header,
        boundary_spans=() if boundary is None else ((boundary, boundary + 1),),
        table_fences=() if fence is None else ((fence, fence + 1),),
        next_blank_or_section=qre._next_blank_or_section_boundaries(lines),
    )
    unchanged = qre._bounded_header_candidate_region(lines, **kwargs)
    captured = []
    observed = qre._bounded_header_candidate_region(lines, **kwargs, diagnostics=captured)
    assert unchanged == observed
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize("kwargs,end,origins,status", [
    ({"boundary": 5}, 5, ("NEXT_STRUCTURAL_SPAN",), "STRUCTURAL_BOUNDARY"),
    ({"fence": 6}, 6, ("TABLE_FENCE",), "STRUCTURAL_BOUNDARY"),
    ({"section": 7}, 7, ("HWP_SECTION",), "STRUCTURAL_BOUNDARY"),
    ({"blank": 8}, 8, ("BLANK",), "UNPROVEN"),
    ({}, 12, ("EOF",), "UNPROVEN"),
    ({"size": 80}, 64, ("LINE_CAP",), "UNPROVEN"),
    ({"size": 80, "boundary": 70}, 64, ("LINE_CAP",), "UNPROVEN"),
    ({"size": 64}, 64, ("EOF", "LINE_CAP"), "UNPROVEN"),
    # A real stop exactly at the cap stays observable, with BOTH causes.
    ({"size": 80, "boundary": 64}, 64, ("LINE_CAP", "NEXT_STRUCTURAL_SPAN"), "STRUCTURAL_BOUNDARY"),
    ({"size": 80, "blank": 64}, 64, ("BLANK", "LINE_CAP"), "UNPROVEN"),
    ({"boundary": 5, "fence": 5, "section": 5}, 5,
     ("HWP_SECTION", "NEXT_STRUCTURAL_SPAN", "TABLE_FENCE"), "STRUCTURAL_BOUNDARY"),
])
def test_window_stop_causes_preserve_real_boundaries_and_fallbacks(kwargs, end, origins, status):
    item = window(**kwargs)
    assert item.region == (0, end)
    assert item.end_line_index == end
    assert item.end_origins == origins
    assert item.end_status == status


def test_rejected_header_window_preserves_stop_but_is_unresolved():
    item = window(boundary=1)
    assert item.region is None
    assert item.end_line_index == 1
    assert item.end_origins == ("NEXT_STRUCTURAL_SPAN",)
    assert item.end_status == "UNRESOLVED"


def test_actual_next_table_heading_is_scanned_before_diagnostic_stop_selection():
    # The next heading is discovered from a SYN document, not a hand-injected
    # boundary span. A supported textual heading is still only a search fence.
    source = "\n".join((
        "[HWP SECTION 0]", "SYN 수행실적 건수 (5점)", "7건 이상 5점",
        "정량평가 세부기준", "SYN 다음 표 본문",
    ))
    lines = qre._source_lines(source)
    boundaries, overflow = qre._sourcewide_structural_boundary_spans(lines)
    assert not overflow
    assert (3, 4) in boundaries
    items = []
    assert qre._bounded_header_candidate_region(
        lines, header_span=(1, 2), boundary_spans=boundaries, table_fences=(),
        next_blank_or_section=qre._next_blank_or_section_boundaries(lines), diagnostics=items,
    ) == (1, 3)
    assert items[0].end_origins == ("NEXT_STRUCTURAL_SPAN",)
    assert items[0].end_status == "STRUCTURAL_BOUNDARY"
    assert lines[items[0].end_line_index] == "정량평가 세부기준"


def hwp_payload(*, suffix="", summary_first=False):
    raw, source = fixture(inline=True)
    if summary_first:
        heading, source = source.rsplit("\n", 1)
        source = source + "\n" + heading
    return ExtractionPayload.model_validate(raw), "[HWP SECTION 0]\n" + source + suffix


def phase(items, name, table=0, criterion=None):
    selected = [item for item in items if item.phase == name
                and item.table_index == table and item.criterion_index == criterion]
    assert len(selected) == 1
    return selected[0]


def test_final_table_and_core_keep_different_end_origins():
    payload, source = hwp_payload()
    items = qre.diagnose_quantitative_region_ends(payload, source=source, attachment_id=ATT)
    criterion = phase(items, "CRITERION", criterion=0)
    table, core = phase(items, "TABLE"), phase(items, "TABLE_CORE")
    assert criterion.end_origins == core.end_origins == ("TABLE_FENCE",)
    assert criterion.end_status == core.end_status == "STRUCTURAL_BOUNDARY"
    assert table.end_origins == ("EOF",)
    assert table.end_status == "UNPROVEN"
    assert table.end_line_index > core.end_line_index
    # A structural stop is a search fence, not a new validation/proof field.
    assert not hasattr(core, "persistence_eligible")
    assert not hasattr(core, "table_end_proven")
    assert not hasattr(core, "source_verified")


def test_table_and_core_inherit_hwp_section_when_no_trailing_total_fence():
    payload, source = hwp_payload(summary_first=True, suffix="\n[HWP SECTION 1]\nSYN next section")
    items = qre.diagnose_quantitative_region_ends(payload, source=source, attachment_id=ATT)
    for item in (phase(items, "CRITERION", criterion=0), phase(items, "TABLE"), phase(items, "TABLE_CORE")):
        assert item.end_origins == ("HWP_SECTION",)
        assert item.end_status == "STRUCTURAL_BOUNDARY"
        assert source.splitlines()[item.end_line_index] == "[HWP SECTION 1]"


@pytest.mark.parametrize("separate_tables", [False, True])
def test_next_criterion_and_other_table_are_distinct_dynamic_stops(separate_tables):
    raw, source = fixture(inline=True)
    first = raw["quantitative_tables"][0]
    second = deepcopy(first)
    second["table_id"] = "SYN-T2"
    second["criteria"][0]["criterion_id"] = "SYN-C2"
    old_heading = second["criteria"][0]["criterion_literal"]
    new_heading = "SYN-두번째 수행실적 건수 (5점)"
    second["criteria"][0]["criterion_literal"] = new_heading
    second["criteria"][0]["evidence"]["quote"] = new_heading
    first_body, _ = source.rsplit("\n", 1)
    second_body = first_body.replace(old_heading, new_heading)
    if separate_tables:
        first["total_evidence"]["quote"] = "SYN 첫 정량평가 합계 5점"
        second["total_evidence"]["quote"] = "SYN 둘 정량평가 합계 5점"
        raw["quantitative_tables"].append(second)
        summary = first["total_evidence"]["quote"] + "\n" + second["total_evidence"]["quote"]
    else:
        first["criteria"].extend(second["criteria"])
        first["total_points"] = 10
        summary = first["total_evidence"]["quote"] = "SYN 정량평가 합계 10점"
    source = "\n".join(("[HWP SECTION 0]", summary, first_body, second_body))
    payload = ExtractionPayload.model_validate(raw)
    items = qre.diagnose_quantitative_region_ends(payload, source=source, attachment_id=ATT)
    item = phase(items, "CRITERION", criterion=0)
    assert item.end_origins == (("OTHER_TABLE",) if separate_tables else ("NEXT_CRITERION",))
    assert source.splitlines()[item.end_line_index] == new_heading
    if separate_tables:
        assert phase(items, "TABLE").end_origins == ("OTHER_TABLE",)
        assert phase(items, "TABLE_CORE").end_origins == ("OTHER_TABLE",)


def test_tied_dynamic_total_and_hwp_section_are_both_retained():
    payload, source = hwp_payload()
    # This intentionally invalid score anchor is useful as a diagnostic input:
    # observing a dynamic fence is NEVER equivalent to approving that anchor.
    source = source.replace("정량평가 합계 5점", "[HWP SECTION 1]")
    payload.quantitative_tables[0].total_evidence.quote = "[HWP SECTION 1]"
    items = qre.diagnose_quantitative_region_ends(payload, source=source, attachment_id=ATT)
    for item in (phase(items, "CRITERION", criterion=0), phase(items, "TABLE_CORE")):
        assert item.end_origins == ("HWP_SECTION", "TABLE_FENCE")
        assert item.end_status == "STRUCTURAL_BOUNDARY"
    assert record(payload.model_dump(mode="json"), source).status != "AVAILABLE"


def test_final_candidate_without_next_structure_ends_at_eof_not_a_64_line_cap():
    payload, source = hwp_payload(summary_first=True, suffix="\n" + "\n".join(f"SYN tail {i}" for i in range(90)))
    items = qre.diagnose_quantitative_region_ends(payload, source=source, attachment_id=ATT)
    for item in (phase(items, "CRITERION", criterion=0), phase(items, "TABLE"), phase(items, "TABLE_CORE")):
        assert item.end_origins == ("EOF",)
        assert item.end_status == "UNPROVEN"
        assert item.region[1] - item.region[0] > 64


def test_missing_anchor_is_diagnostic_unresolved_and_does_not_create_a_region():
    payload, source = hwp_payload()
    candidate = payload.quantitative_tables[0].criteria[0]
    candidate.criterion_literal = "SYN missing owner 123점"
    candidate.evidence = candidate.evidence.model_copy(update={"quote": candidate.criterion_literal})
    candidate.metric = "OTHER"
    items = qre.diagnose_quantitative_region_ends(payload, source=source, attachment_id=ATT)
    for item in (phase(items, "CRITERION", criterion=0), phase(items, "TABLE"), phase(items, "TABLE_CORE")):
        assert item.region is None
        assert item.end_line_index is None
        assert item.end_origins == ("UNRESOLVED_ANCHOR",)
        assert item.end_status == "UNRESOLVED"


@pytest.mark.parametrize("no_tables", [False, True])
def test_unvisited_non_hwp_or_empty_payload_has_no_diagnostics(no_tables):
    raw, source = fixture()
    if no_tables:
        raw["quantitative_tables"] = []
        source = "[HWP SECTION 0]\n" + source
    assert qre.diagnose_quantitative_region_ends(
        ExtractionPayload.model_validate(raw), source=source, attachment_id=ATT,
    ) == ()


def test_observation_keeps_payload_record_fingerprint_schema_and_blockers_identical(monkeypatch):
    payload, source = hwp_payload()
    raw = payload.model_dump(mode="json")
    baseline = record(raw, source)
    assert baseline.status == "AVAILABLE", [issue.code for issue in baseline.issues]
    # Captured at f0d4096 using this exact SYN fixture and contract. Pin its
    # metadata explicitly: a newer contract legitimately changes the current
    # record's fingerprint. All other synthetic fixture bytes must stay equal
    # to the historical golden, and current observation is compared below.
    historical = baseline.model_copy(update={
        "prompt_version": "pai-loop-extraction-0.5.7",
        "extraction_schema_version": "pai-loop-requirements-0.4.2",
        "validator_version": "pai-loop-quantitative-attachment-validator-0.6.20",
        "validation_fingerprint_sha256": "3258ad87794ffce8dcb6216559a73b4c50738a6ab625c236d816df6329fb860e",
    })
    assert qre.validated_quantitative_record_fingerprint(historical) == historical.validation_fingerprint_sha256
    assert hashlib.sha256(historical.model_dump_json().encode()).hexdigest() == "456a9366c8e247570c86ec6e27c73b7a0c2f57c0b62ef3f2e2957dd2da84c082"
    original = qre._rebind_split_table_cell_literals
    expected = original(payload, source=source, attachment_id=ATT)
    captured = []
    observed = original(payload, source=source, attachment_id=ATT, diagnostics=captured)
    assert observed == expected
    assert len(observed) == 2
    assert payload.model_dump(mode="json") == raw
    assert captured
    assert isinstance(qre.diagnose_quantitative_region_ends(payload, source=source, attachment_id=ATT), tuple)
    with pytest.raises(FrozenInstanceError):
        captured[0].region = (0, 1)

    # Run the real record builder with observation enabled only internally.
    def observed_rebind(value, **kwargs):
        return original(value, **kwargs, diagnostics=[])
    monkeypatch.setattr(qre, "_rebind_split_table_cell_literals", observed_rebind)
    again = record(raw, source)
    assert again.model_dump_json() == baseline.model_dump_json()
    assert again.validation_fingerprint_sha256 == baseline.validation_fingerprint_sha256
    assert not {"diagnostics", "end_origins", "end_status", "region"}.intersection(again.model_fields)
    assert set(asdict(captured[0])) == {
        "phase", "table_index", "criterion_index", "header_span", "region",
        "end_line_index", "end_origins", "source_line_count",
    }
