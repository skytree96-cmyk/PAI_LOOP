# Inline binary bracket validation — 2026-09-06

Some financial-ratio tables state two complementary score bands in one sentence
and omit the repeated threshold in the second band. The ordinary comparator
parser reads only the explicitly numbered comparator, leaving an accurately
extracted second band incomplete. Merely bypassing that comparator check would
also allow the first band to borrow the second band's award.

The validator now recognizes a deliberately bounded grammar: two numeric
percentage bands in one comma-separated clause, with `이상` / `미만` or
`초과` / `이하`, one explicit threshold, and an explicit point award per arm.
Both extracted BRACKET rows must retain the same complete literal and quote.
Their existing bounds, inclusivity and awards must match the two source arms
exactly, with neither a missing arm nor an extra arm. The FINANCIAL_RATIO metric
and percent unit must agree. No extracted value, operator or quoted text is
rewritten, and no company score is produced by this validation step.

The complete clause must follow its own criterion's stated maximum and colon,
end that criterion, and occupy one complete source paragraph together with that
criterion. The criterion and its anchor must agree. The full criterion and
clause must each locate one unique source occurrence. Source prefixes, suffixes,
additional conditions, paragraph boundaries, repeated anchors and borrowed
clauses remain incomplete. This initial scope intentionally leaves separately
cited or split paragraphs for later source review.

A different criterion cannot claim the whole clause or borrow a partial arm with
its literal and quote. Partial claims are aligned against the complete owning
criterion, so retaining its heading or extending a quote into a preceding or
following paragraph cannot conceal shared ownership. Every overlapping character
must agree; a separate criterion with its own heading and the same numbers does
not establish a shared claim. The ownership check includes bracket, case,
threshold, formula and recognition evidence. The same clause/award
proof and cross-criterion ownership check run when persisted AVAILABLE records
are restored and merged. Current attachment, confidence and document/manifest
binding checks continue to apply.

The issue `BRACKET_COMPARATOR_MISMATCH` receives the targeted fingerprint
revision `inline-binary-bracket-proof-v1`. Existing AVAILABLE records containing
the corresponding inline marker also receive this revision, including partial
one-row records that the former number-membership check could accept. Unrelated
AVAILABLE records retain their fingerprints. A changed fingerprint requires
normal current-source revalidation; it does not authorize an old record as a
fallback or declare extraction/reanalysis complete.

Missing table totals, unresolved CASE tables, sourcewide ambiguity, incomplete
attachment coverage and missing company facts remain independent blockers.

Validation uses synthetic fixtures only. Coverage includes all four ordered
complementary operator pairs, preserved extraction values, swapped awards,
missing/duplicate/extra arms, wrong thresholds, inclusivity and units, additional
source conditions, source/attachment ownership, partial claim collisions,
persisted-record tampering, unrelated source claims and targeted fingerprint
scope. Full-suite, CI and production verification are separate release gates;
this document does not assert a deployment.
