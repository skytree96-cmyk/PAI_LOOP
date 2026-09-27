# Repeated quantitative tables

Engine `pai-loop-quantitative-engine-1.8.9` removes a duplicate representation
only after comparing complete, source-validated table programs. This is a
conservative semantic port of PR #199, not a merge of that historical branch.

## Proof Required

- Both attachments belong to the same current manifest and have document bindings.
- Their document roles agree. Their document digests are identical, or their
  filenames identify different HWP/HWPX/PDF representations of the same stem.
  A filename alone is never sufficient; the entire table program must also agree.
- Every table is AVAILABLE with complete row linkage, an anchored total equal to
  its row maxima, and no review rows. All table labels, totals, minimums, quoted
  source/section context and all candidate semantics agree, including labels,
  recognition conditions, evidence requirements, units and scoring values.
- Neither attachment has unresolved issues, review candidates or a non-applicable
  declaration. An unlocated profile issue prevents folding. The existing table
  count bound also applies to this comparison.
- Duplicate-looking tables within one attachment are ambiguous and are not folded.
  Repeated rows inside a table retain their multiplicity.

Only attachment IDs, physical page positions, confidence and table/criterion
identifiers are ignored during comparison. Original selected anchors and fact
bindings remain unchanged. Different personnel labels such as retained staff and
assigned project staff cannot be folded. Different lots/sections, different
recognition conditions, missing source context and same-format revisions remain
separate. Existing automatic-activation guards are unchanged; a partial-source
subtotal remains REVIEW with no minimum-score claim.

A reviewed company fact bound only to a discarded representation is not silently
transferred to the selected representation. That case requires a fresh reviewed
binding; losing a numeric item is preferable to inventing binding authorization.

The engine-version change invalidates pre-change public score snapshots. Tests
exercise a native synthetic extraction and roster through local persistence and
the public API, as well as rejection of the prior engine's cached result.

## Limits

This does not merge overlapping portions of different tables or promote a REVIEW
table. The historical 16-item/138-point measurement is not asserted for the current
notice cohort. A case with different recognition conditions or an unresolved
owning table requires source review before it can be called an exact duplicate.
No production inputs, private documents or roster records are included here.
