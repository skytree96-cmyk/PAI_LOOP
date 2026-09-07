# Bind automatic loss feedback to one PPS opening

An observed third-party winner proves our loss only when our independently
validated submission refers to the same notice number, revision, classification
and rebid number. A missing company name in the winner list is not participation
evidence. This change does not add another provider query or a company-history UI.

## Participation contract

Result-learning create and update requests accept an optional `opening_identity`:

```json
{
  "bid_notice_no": "SYN-NOTICE",
  "revision_no": "0",
  "classification_no": "1",
  "rebid_no": "2"
}
```

All four components are required when the object is supplied. Ordinals must be
explicit numeric strings; leading zeroes are normalized. Missing values never
become zero. The notice number and revision must match the record's stored notice.
The normalized object is retained in `BidOutcome.evidence_json.opening_identity`
and returned in result-learning outcome projections. No database migration is
needed. Existing manual saves can omit it; omission does not authorize an
automatic loss. Updates retain the existing version/CAS check, and changing the
identity under an already-used create idempotency key returns conflict.

Automatic `LOST` requires a non-automatic `SUBMITTED`, `WON` or `LOST` record with
`VALIDATED` workflow, actual human review and the same complete opening identity.
`CANCELLED` with an old bid amount cannot serve as participation. The automatic
evidence retains the specific submission ID, its verified opening identity and
its existing human-review status. It does not mark the automated operation as
human-reviewed or synthesize an explanation for why the bid lost.

## Final-result guards and preservation

The provider adapter separately records whether the four identity components were
actually present, before legacy display normalization can fill zero defaults.
The service checks that this identity agrees with the projected fields. Missing
components or conflicting identities produce `REVIEW`. A response containing
several distinct openings also produces `REVIEW`; this bounded endpoint does not
select any historical company win or combine classifications. Page/time-limited
results continue to require review.

The new automatic event key hashes the complete normalized opening identity. A
later rebid or another classification creates a separate observation. Corrections
within the same opening retain the existing idempotent update behavior. The key
does not depend on the evidence schema version. Existing schema-based and
notice/revision-only keys are not migrated, reused, overwritten or deleted.

Historical automatic `LOST` observations without matching complete participation
identity remain stored with their original status and evidence. Their
result-learning projection is `DRAFT` for review, rather than automatically
validated; `opening_identity` is null when absent. A legacy workflow flag alone
cannot establish the missing binding. Archived records remain archived. A later
verified observation can coexist with the legacy observation, so consumers must
not sum all historical rows as distinct bids without event-level reconciliation.

Server-key authentication, operator authorization, private evidence boundaries and
manual record update paths are unchanged. Automatic feedback rejects a key
collision owned by another source. This checkout predates department-account
integration; that integration must retain its account ownership and capability
checks around the unchanged result-learning paths. The new identity is evidence,
not an actor label or authorization claim.

Relevant review reasons: `PARTICIPATION_OPENING_NOT_CONFIRMED`,
`PPS_OPENING_IDENTITY_MISSING`, `PPS_OPENING_IDENTITY_MISMATCH`, and
`PPS_MULTIPLE_OPENING_IDENTITIES`.

Regression coverage uses only synthetic local data and mocked providers. It
exercises wrong/missing opening identity, separate event keys, legacy preservation,
mixed openings, omitted provider fields, manual validation/CAS, identity display,
and rejection of an automatic/manual namespace collision. No external lookup,
account activation, historical backfill or production validation is performed.
