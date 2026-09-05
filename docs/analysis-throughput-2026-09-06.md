# Document validation throughput and analysis confirmation

Date: 2026-09-06

Repeated source-anchor lookups normalized every paragraph again. A document-local immutable paragraph sequence now reuses normalized lines and exact span answers. The cache includes the existing window bounds, is limited to 256 keys and 4,096 spans, and is never shared across documents or persisted. Unicode matching, repeated locations, raw character limits and ambiguity decisions are unchanged.

An existing local real-source reconstruction (not the current production model response) retained AVAILABLE with three candidates and zero review candidates. Under cProfile, candidate-profile construction decreased from 37.544 seconds to 2.907 seconds on the same workstation. This measures validator throughput; it does not prove production score recovery.

Manual analysis confirmation now uses an accessible HTML dialog. Browser-native confirmation was blocking internal-browser automation. The original scope and usage explanation remain, cancellation issues no request, and analysis is awaited until explicit confirmation. This is separate from the operator PIN.

Validation: 355 focused tests passed, including the independent exhaustive anchor oracle, duplicate/Unicode cases, document isolation and cache bounds, existing quantitative-rule tests, and frontend contracts. Full CI remains the merge gate.

Operational follow-up: verify the deployed dialog, finish current-manifest extraction, and inspect live diagnostics and all-notice statistics. A prompt-version change excludes historical extraction from current results; a low current-version coverage count is not evidence that historical records were deleted. Do not report the synthetic 20/20 regression target as a verified live score.
