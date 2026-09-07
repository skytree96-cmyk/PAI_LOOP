# Explicit bid-rate calculation

The result editor now supports manual entry or automatic calculation of **our
submitted bid rate**. Operators must explicitly enter a planned price
(`PLANNED_PRICE`, 예정가격) or base amount (`BASE_AMOUNT`, 기초금액), its positive
amount in KRW, and a reviewable source reference. The editor labels which basis
the percentage uses. It never substitutes notice estimated price, budget,
historical awards, or winning amount. Winning bid rate remains manual.

## Calculation and compatibility

`submitted_rate_calculation` is an allowlisted object on the result-learning
create/update/output DTOs. Its fields are `mode` (`MANUAL` or `AUTO`),
`basis_kind`, `basis_amount`, and `basis_reference`. An explicit AUTO object must
provide the complete basis. MANUAL requires the basis fields to be empty.

The server computes `submitted_bid_amount / basis_amount * 100` with Python
Decimal and rounds to four decimal places using `ROUND_HALF_UP`. The UI previews
the same rounding with integer ratios and explains the formula and required basis. The persisted
rate remains the existing Float column; the calculation uses decimal strings of
the validated numeric inputs. A missing submitted amount cannot be calculated;
an actual zero amount produces zero percent. A zero/nonfinite denominator and
an unrounded ratio above the existing 200% limit are rejected, never clamped.
The server recomputes AUTO values even if a client sends a stale or forged rate.

Omitted metadata on a new or legacy record means MANUAL. On PATCH, omission
preserves the current mode and complete basis: an old client changing the amount
of an AUTO record triggers recalculation. Explicit null metadata is invalid.
Switching to MANUAL is explicit and retains the supplied/current rate as a manual
value while clearing active basis metadata. Switching an AUTO result to NO_BID
requires explicitly selecting MANUAL and clearing its bid fields.

The date-only editor preserves the original occurrence timestamp, including its
offset and precision, when that date field is unchanged. This also applies when
creating a review copy of an external result. An explicitly changed date uses
midnight in Korea; clearing the field removes the occurrence date. Changing only
the rate or its calculation basis does not rewrite the result's occurrence time.

## Storage, audit, and boundaries

No model or database migration is required. Existing `evidence_json` stores a
dedicated `_submitted_bid_rate` object containing `calculation` and an append-only
`history`. Each amount/rate/calculation change records before/after snapshots,
the `DECIMAL_HALF_UP_4` policy for AUTO, record revision, timestamp, and actor.
Unrelated edits do not manufacture calculation-history entries. Existing
evidence and external-review pointers are preserved. Clients cannot supply
audit entries, policy strings, or arbitrary JSON through the new DTO.

Result-learning responses expose only the calculation object, never raw JSON or
the audit history. The generic outcome endpoint remains server authenticated;
public notice responses do not acquire these fields. Department ownership,
expected latest outcome on create, idempotency checks, revision CAS, account
session handling, and external-source immutability retain their existing rules.

## Validation and integration

Focused synthetic API tests cover decimal rounding, zero/missing/invalid inputs,
server-owned results, basis-bound idempotency, legacy compatibility, immutable
external originals, audit preservation, and department ownership/CAS. Node
runtime tests exercise basis-required preview and the actual form payload.
Existing operator/editor, generic outcome, and frontend contracts remain part of
the focused gate.

The original implementation was based on accounts release commit `1f31460`.
It is now integrated onto accounts release `f0e5651` in the local
`feat/results-followup-0908` branch, preserving account and annual award-table
behavior. Assets and all four asset contracts use `20260908-results-v1`.
No deployment, operational API call, live password/account operation, or full CI run
is claimed by this handoff.
