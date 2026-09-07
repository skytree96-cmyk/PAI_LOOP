# Recognize the explicit SME/nonprofit alternative

A condition allowing either an SME certificate holder or a nonprofit entity
fell through to the confirmed-absent SME certificate fact. The resulting message
incorrectly said the notice contained no nonprofit exception, even though the
alternative was explicit in the same condition and the curated nonprofit fact
was already present.

The policy now recognizes the complete clause form consisting of an SME
certificate-holder branch, `또는`, a nonprofit branch with an optional
incorporation-permit evidence-submission parenthesis, and `중 (어느) 하나에 해당`.
Matching is anchored over the whole normalized condition (`re.fullmatch`), and
that anchor is what keeps negation, exclusions, AND requirements, additional
qualification duties, or altered parenthetical conditions from opening this
path. Other nonprofit exception grammars keep their existing interpretation.
Relaxing the anchor to a substring search is not an acceptable alternative: it
opens this path on the tracked negation, exclusion and AND counterexamples.

The anchor absorbs one closed allowlist of inert declarative endings
(`_INERT_CLAUSE_TAIL`): an optional `하는`/`되는`, an optional subject noun, an
optional obligation copula `여야`/`이어야`, an optional obligation verb
`해야`/`하여야`/`되어야`, an optional `함`/`한다`/`합니다`, and an optional period.
Every token is a literal that carries no obligation of its own and no polarity,
so absorbing it cannot import a duty, a negation, or an exclusion. There is
deliberately no leading-affix allowlist, because observed extracted conditions
begin at the clause itself.

A nonprofit branch restricted to a legal subset — a preferential-procurement
exception, a named enabling-decree clause, or a `특정`/`일부` qualifier — does not
prove that the company belongs to that subset. A closed list of complete legal-
subset OR clauses now returns a blocking REVIEW for unbound subset membership.
Neither generic nonprofit status nor absence of the SME certificate proves the
result of that OR. Other unrecognized shapes retain their existing gates.

The recognized path uses the existing `nonprofit_entity` fact and eligibility
helper. Missing or false facts and deadline-invalid facts or evidence remain
REVIEW. This parser does not create evidence or confirm a submission.
Attachment completeness and the final notice decision remain independent.

The recognized clause does **not** feed the notice-wide
`nonprofit_exception_present` flag. That flag downgrades a *different*
requirement's explicit `COMPANY_CONFIRMED_ABSENT` FAIL to a scope REVIEW, and a
self-contained SME/nonprofit OR clause says nothing about another certificate
family. The global exclusion is retained. A separate direct-production requirement,
explicit independent AND duty, or nonprofit exclusion keeps its own gate.
However, a standalone possession/validity sentence in the same SME-certificate
family does not establish independent scope merely by being a separate row.
Only those closed sentence shapes receive a blocking scope REVIEW when a complete
SME/nonprofit OR exists. Legal-subset OR clauses also remain SME-scoped even when
their qualifier contains the word `예외`; they do not weaken direct-production FAILs.

Where a clause names a nonprofit alternative in a grammar the parser cannot
bind, the outcome stays the same confirmed-absence FAIL, but the explanation no
longer asserts `공고 원문에도 비영리법인 예외가 없어`. That claim is about the
notice, so it is guarded by notice-wide presence rather than by the single
clause: a separate certificate requirement no longer denies a nonprofit
exception that another requirement in the same notice states verbatim. The
truthful wording is kept byte-identical for a notice in which no requirement
mentions a nonprofit, so the claim is not over-suppressed. Asserting the absence
of an exception the source states is a false statement about that source; the
message now says only that the alternative was not recognized in a decidable
form and that the original text must be read. This wording guard itself changes
no outcome; the scoped REVIEW paths above are separate policy changes.

`POLICY_VERSION` advances from v11 to
`pai-loop-requirement-policy-2026.09.07-v12`. The policy version participates in
the analysis input digest and stored basis versions. After deployment, v11
snapshots are version-stale, and a later eligible analysis creates a new
versioned result instead of reusing or overwriting the v11 result. Existing
queue selection may offer open, unexpired stale snapshots for refresh under its
current retry and deduplication rules. The quantitative engine and extraction
contract versions are unchanged.

The reviewed-retry campaign freezes the request, PPS source boundary, reviewed
attachment versions and `CURRENT_EXTRACTION_CONTRACT`. Requirement policy v12
is not part of that frozen extraction contract or source boundary, so this
version bump alone does not trigger `REVIEW_CAMPAIGN_CONTRACT_CHANGED` or require
a new campaign. A deployment during a campaign can still leave earlier results
at v11 and subsequent results at v12; campaign completion would not prove that
all stored results use v12. The bump also moves every open, unexpired notice
with a v11 current run into the version-refresh partition, and those refresh
parents reserve the same notice keys, so prefer landing v12 after a running
reviewed campaign reaches a terminal state. This change does not restart,
execute or alter a production campaign.

## Verification and what remains unproven

Regression coverage checks the reported synthetic clause across three category labels, the
inert declarative tails, equivalent complete-clause spellings, missing/false/
expired facts, expired evidence, AND/negation/exclusion/conflicting conditions,
subset-restricted nonprofit branches, separation from another SME requirement and
from a direct-production requirement, and the corrected failure message.
Existing policy, performance-recovery, reviewed-campaign and version-refresh
checks retain their contracts.

The tail allowlist was sized against a read-only production requirement snapshot
(2026-08-31; 236 open notices with a current evaluation, 7,107 requirement
items). In that snapshot 6,655 of 6,666 unique conditions begin at the clause
itself with no leading affix, while declarative obligation endings dominate the
tails: 2,395 end in `함`, 1,452 in `해야 함`, 607 in `여야 함`, and 414 in a bare
subject noun.

Two limits are not closed by this change. First, that snapshot contains 24
conditions pairing an SME certificate pattern with `비영리법인`, and every one of
them restricts the nonprofit branch to a legal subset, so none is recognized by
this path and none should be; the snapshot therefore does not itself demonstrate
the reported symptom. Second, the exact `normalized_condition` of the notice that
produced the reported wrong message is not available in this repository, so
whether that specific case now resolves to `PASS_EXCEPTION` is unverified.
Closing it requires the stored ACCEPTED extraction payload for that notice, read
only, added as a `SYN-` identified fixture. Do not reconstruct or guess that
string.

## Follow-up review on 2026-09-07

The live public policy projection now displays a different, legally restricted
condition: `소기업·소상공인 확인서를 소지한 업체 또는 우선조달계약 예외 규정에 따른 비영리법인 중 하나에 해당`.
The visible evidence panel also quotes a statutory subset. This is not the raw
ACCEPTED payload or an exact provenance join, so it is not evidence that the
original reported clause now produces PASS_EXCEPTION. A SYN regression labels
this input `PUBLIC_POLICY_PROJECTION` and expects REVIEW, never generic nonprofit
PASS. No historical condition, attachment ID, or quote relationship was invented.

Direct read-only JSON navigation was unavailable in the internal browser, and
the deployed free Render instance does not offer Shell access. Current-manifest
raw attachment error joins and stored whitespace source-gap occurrences therefore
remain unverified. A local aggregate audit draft was prepared, but not run against
production. No quantitative source-gap retry or stored-payload parsing contract
was changed without observing the required production case.

Focused validation of the follow-up: `tests/test_eligibility_policy.py` passes
117 cases, including 22 new scope/subset boundary cases; `git diff --check` passes.
The final PR-head CI result is recorded in the PR discussion. Intermediate
browser-upload commits skip CI while the companion tests and this document are
being assembled; the final commit has no skip directive and runs the required gate.
No production deployment, queue change, account activation, or PIN retirement
was performed by this review.
