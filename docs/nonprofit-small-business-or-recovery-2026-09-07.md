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
prove that the company belongs to that subset. Such a clause stays outside the
recognized path and keeps its existing REVIEW or FAIL outcome.

The recognized path uses the existing `nonprofit_entity` fact and eligibility
helper. Missing or false facts and deadline-invalid facts or evidence remain
REVIEW. This parser does not create evidence or confirm a submission.
Attachment completeness and the final notice decision remain independent.

The recognized clause does **not** feed the notice-wide
`nonprofit_exception_present` flag. That flag downgrades a *different*
requirement's explicit `COMPANY_CONFIRMED_ABSENT` FAIL to a scope REVIEW, and a
self-contained SME/nonprofit OR clause says nothing about another certificate
family. A separate SME-certificate requirement and a separate direct-production
requirement therefore keep their own `FAIL_CONFIRMED`, which regression coverage
now asserts.

Where a clause names a nonprofit alternative in a grammar the parser cannot
bind, the outcome stays the same confirmed-absence FAIL, but the explanation no
longer asserts `공고 원문에도 비영리법인 예외가 없어`. That claim is about the
notice, so it is guarded by notice-wide presence rather than by the single
clause: a separate certificate requirement no longer denies a nonprofit
exception that another requirement in the same notice states verbatim. The
truthful wording is kept byte-identical for a notice in which no requirement
mentions a nonprofit. Asserting the absence of an
exception that is present in the clause is a false statement about the source
text; the message now says the alternative was not recognized in a decidable
form and that the original text must be read. No outcome changes with it.

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

Regression coverage checks the observed clause across three category labels, the
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
