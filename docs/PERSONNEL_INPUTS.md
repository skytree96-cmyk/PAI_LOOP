# Reviewed Personnel Inputs

Personnel scoring reads one applicable `company.personnel.roster` company fact.
The versioned value uses `pai-loop-personnel-roster-1.0`. It is a reviewed raw
input for `ESTIMATED` calculations, never sufficient evidence for a `CONFIRMED`
score. The fact's `verified` flag records operator review of this input; it does
not certify future employment or replace payroll/credential evidence.

## Local preparation

`tools/prepare_personnel_roster.py` reads the supported personnel workbook using
the existing bounded XLSX reader. Run it with the package available on
`PYTHONPATH=src` and explicit snapshot, attestation, corrected-grade and employment
confirmations. It does not contact a server or database. Default output contains
aggregate counts only. An optional `--output` creates a private JSON outside this
repository and refuses to overwrite an existing file.

The source workbook stays local. Only joined-on dates, completed degrees paired
with their own majors, credential entries and completeness flags, reviewed
research grades, opaque row keys and a source digest enter the private payload.
Birth dates, departments, position descriptions and external career narratives
are not copied. Corrected research grades come from the explicit corrected
column, not a volatile tenure formula. Formula/error cells in scoring inputs,
unknown headers, duplicate rows and unsupported degree/grade values fail closed.

Course completion, candidacy and explicitly unfinished attendance are not degree
awards. Blank credential cells remain unknown. Blank majors remain unknown for
the corresponding degree; a bachelor's major cannot satisfy a master's-major
condition by being combined with an unrelated master's degree.

This tool prepares input, not an operational import. Registering or replacing a
company fact in production remains a separate authorized action. Keep the raw
workbook and prepared JSON out of source control, logs and public responses.

## Effective dates and versions

The fact must be operator-reviewed and have an effective start date. Its outer
effective range and the envelope's snapshot/attestation dates must cover the
criterion's reference time. Exactly one applicable fact is allowed: overlapping
versions are a visible review condition, never concatenated. Old unversioned
flat-major/static-tenure input must be prepared again, not silently upgraded.

Tenure is recomputed from the actual joining date at the KST calendar cutoff.
Explicit publication-date or fixed-date source rules select that reference;
otherwise the notice deadline applies. Missing/ambiguous dates, unmodeled career
requirements, and project-assigned teams cannot be answered by the whole-company
roster. Missing degree/grade information stays unresolved.

## Bounded future scenarios

The default has no future-employment assumption. `--project-through` together
with `--confirm-no-change-projection` can add a bounded
`CURRENT_ROSTER_UNCHANGED` scenario. `verified_through` remains the actual
attestation horizon; it is not extended. The outer company-fact effective range
must also explicitly admit the scenario date. Dates beyond the projection remain
unscorable. Projected results are always estimates with a deadline recheck warning.

Public estimated personnel items retain a fixed, nonprivate warning about roster
assumptions and rechecking. Private counts, joining dates, certificate strings,
source hashes and the roster itself are not restored into public snapshots.

## Re-analysis and source rules

Engine version 1.8.7 tracks all raw roster versions in the private hashed input
manifest. A roster content, provenance, date, review-state or projection change
invalidates analysis reuse even when the numerical result stays in the same band.
Unchanged inputs remain idempotent. This does not change financial fact selection.

A complete roster does not validate an unread or inconsistent notice score table.
Existing source anchors, operators, awards and attachment-coverage checks remain
mandatory. Source-review cases stay visible rather than acquiring invented scores.
