# Current attachment status visibility — 2026-09-06

A failed current attachment could disappear from the structured-document list while another successful document made the section appear complete. Notice detail now includes every valid current manifest slot as ANALYZED, REVIEW or PENDING. Invalid manifest entries produce an explicit review marker. The browser suppresses an older successful card when the current slot is failed or pending.

Public responses contain only redacted filenames, fixed states, fixed reason codes and fixed Korean explanations. Known API transport, incomplete-response and schema failures are distinguished through an exact allowlist; arbitrary provider errors, source payloads, response identifiers, URLs and private evidence remain excluded. Existing aggregate notice reason codes and scoring/eligibility logic are unchanged.

Validation: 96 focused tests passed across PPS enrichment, public read-only API and frontend public contracts. Cases cover failed/pending/current/stale manifest inputs, public API serialization, unknown private errors, and a browser rendering harness with mixed successful, failed and pending attachments. Production verification follows the required full CI gate and deployment.
