# Estimated Credit Scenarios

This additive private operator path records common certificate assertions and a
bounded sole-bid, unchanged-certificate assumption. It is not a substitute for
`HUMAN_REVIEWED_NOTICE_CONDITIONS`. Existing reviewed certificate binding remains
unchanged. No canonical `company.credit_rating` fact is created by this path.

1. Register the certificate through the existing private certificate registry.
2. GET `/api/v1/operator-evidence/notices/{notice_key}/credit-rating/scenario-binding`
   for each intended notice. A 422 means the source or its complete extracted
   condition program is not supported. Never replace conditions or bypass this gate.
3. POST `/api/v1/operator-evidence/credit-rating-scenarios` with
   `CreditScenarioPolicy` from `quantitative_credit_scenario.py`: exact certificate
   ID and canonical registration hash, approval date and bounded end date,
   `HUMAN_APPROVED_CERTIFICATE_SCENARIO`, all five explicit common assertions,
   `SOLE_BID_UNCHANGED_CERTIFICATE`, and the four binding fields for each notice
   returned by step 2. The certificate registration hash is SHA-256 of the complete
   normalized registration model serialized as sorted-key, compact UTF-8 JSON
   with `ensure_ascii=False`; it is not the source document hash.
4. Run the normal analysis and verify the persisted public result. Registration
   itself does not execute analysis or grant a score.

The assertions cover qualified issuer, procurement-system visibility, one
unchanged rating, same legal entity, and no succession/cooperative exception.
`CompanyFact.verified=True` on this raw input means operator approval only;
the resolver always emits `ESTIMATED`, never `CONFIRMED`. The public result
explicitly discloses the sole-bid/current-rating assumption. A partial source
can supply an estimated criterion but cannot supply a complete notice total.

The narrow full-clause grammar supports standard pre-publication issuer and
validity clauses, higher-score or latest/same-date-lowest alternatives,
procurement lookup/validity boundaries, and conditional succession/joint/share/
cooperative clauses made inapplicable by the explicit scenario assertions.
Unknown, appended, negated, duplicated or contradictory clauses remain REVIEW.
This is not a general interpretation of all procurement conditions. Issuance
must precede publication, and the certificate must remain valid at publication
and deadline. No publication date or a deadline outside the approved horizon
fails closed. Automatic external issuer authentication is not claimed.

Every calculation rechecks the current source binding and complete condition
digest, certificate integrity/status, policy dates and legal entity. Duplicate
applicable policies fail closed. Registration is idempotent for the exact same
policy; replacement/overlapping policies require separate review and are rejected.
Private cache manifests hash every raw scenario candidate and its certificate
metadata, including invalid/out-of-window inputs. Public views expose neither
the policy nor certificate identifiers, private references or internal digests.

No production registration, provider call, deployment or notification is implied
by these code changes. All tests use synthetic sources and local databases.
