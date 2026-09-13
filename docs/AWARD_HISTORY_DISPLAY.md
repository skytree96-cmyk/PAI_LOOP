# Award history display

The notice detail's recent-three-year panel defaults to expandable project
groups. Each group uses the server's year and response-scoped result group key,
with the latest result dates first. The key ties companies to one stored award
result, keeping separate classifications and rebids apart even under the same
notice number and revision. It is an opaque ordinal within that response and
does not expose internal or provider identities. Older responses without a
group key keep each row separate. Title similarity never combines projects or
promotes a similar candidate to an exact project-and-agency match.

The summary distinguishes project groups, displayed company rows and rows with
at least one recorded evaluation score. Year controls and the complete-table
switch operate on the same stored response without starting a provider request.
Changing the selected notice resets the view to all years and grouped projects.

Each project exposes its stored source reference, match basis, opening-data
collection status and company comparison table. Only the explicit WINNER field
identifies the winner; opening rank does not substitute for that field. Missing
amounts and scores remain unknown, and bid amounts never borrow final award
amounts. The result date is the award date when available, otherwise the opening
date. Partial and failed reads retain their visible status and the existing
snapshot. Empty years are not described as proof that no awards occurred.

The component inherits the site's existing Paperlogy/Pretendard font stack,
Classic Blue palette and shared size tokens. On narrow screens, project headings
reflow while company comparison tables scroll in labelled keyboard-accessible
regions. The application navigation and notice detail shell retain their shared
styles.

The earlier [annual-table release](codex-handoff/2026-09-08/AWARDS_TABLE_RELEASE.md)
is a historical integration record; this document describes the current display.
