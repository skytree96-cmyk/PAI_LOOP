# Whole-notice progress and source-bound award validation

The dashboard now reports aggregate progress for all current, open, non-cancelled PPS notices. Attachment audit and acceptance, analysis attempts, eligibility outcomes, quantitative snapshot statuses, and notices with a score range have separate counts. A score range may be partial or estimated; it is not evidence of a confirmed company score. The frontend uses this server aggregate rather than extrapolating from a paginated notice list. Missing statistics remain unavailable instead of appearing as zero progress.

Dashboard reads retain the 25-notice memory bound and load only aggregate quantitative score rows. The public projection validates the latest current run and its matching snapshot without exposing company bindings or searching older successful runs. Existing source/version invalidation remains authoritative.

Quantitative validation recognizes an exact trailing `배점의 N%` award when the extracted points equal the criterion maximum multiplied by that percentage. It rejects multiple awards, percentages outside 0–100, and mismatched points. Bounds and comparators still require their own source proof. An omitted maximum may extend the criterion literal only when its own anchored quote consists of that exact literal immediately followed by the exact maximum, optionally with the points unit. Other fields or unrelated rows cannot supply it.

Narrow document-gap grammars distinguish an explicit reference to a sibling RFP from a missing table, and distinguish purely qualitative narrative exclusions from omitted quantitative rules. Cross-document resolution still requires the current sibling evidence. Unknown references and additional quantitative omissions remain blocking. Only affected validation fingerprints change, including existing available percent-award records that require the new proof.

The eligibility policy also recognizes the statutory qualification wording `자격을 갖출 것`. It still requires the existing company fact and deadline policy. Operator diagnostics add bounded comparator/category shape information without returning raw quotes or private numeric inputs.

This change does not add a performance-amount percentage denominator. Amount-to-budget ratios still require explicit source-bound semantics and supported company evidence before scoring. Deployment and production reanalysis must be verified separately from passing tests.
