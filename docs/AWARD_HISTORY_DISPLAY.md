# Award history display

The collection and display scope is the same demand agency AND the derived
business keywords. The existing three-calendar-year collection window is
unchanged. API queries include the confirmed demand-agency code when available,
otherwise its name. Returned candidates are checked again before any company
opening-detail request. Agency codes take precedence; an exact normalized name
is used only when one side lacks the code. Names are not fuzzy-matched across
parent agencies, subdivisions or announcing intermediaries.

The target demand agency comes from the latest PPS metadata or an append-only
agency observation tied to that exact notice, revision and metadata version.
Notice.agency is an announcing-agency display value and is never substituted for
missing demand agency. Missing agency pauses collection with
AWARD_AGENCY_UNAVAILABLE. Supplemental agency observations do not create or
rewrite document/analysis versions. New PPS ingestion retains the four allowed
announcing/demand agency name/code fields without retaining raw contact data.

Previously collected broad candidates remain in the audit store. Notice detail,
history and intelligence endpoints expose only rows matching the current agency
and keywords. Scope changes invalidate the automation basis and requeue open
targets; old broad completion is not promoted to scoped completion. Closed and
superseded notices remain excluded. Public search criteria report the institution
name and keywords without exposing internal codes or source metadata.

Each empty date window still costs a request. Agency filtering reduces excess
pages and unrelated company lookups, but is not a shared-query cache. HTTP 429
or provider codes 22/23 stop the current collection before fallback and further
opening calls; successful earlier rows remain partial. This path uses no AI.

Provider reference: [PPS award API](https://www.data.go.kr/data/15129397/openapi.do).

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
