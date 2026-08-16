# PAI_LOOP Source Register v0.1.0

Date: 2026-08-16  
Policy: original workspace artifacts are read-only and are not committed to this
repository.

## Inventory reviewed

The authorized local corpus contains planning material, API reference material,
public notice attachments and internal business datasets. The following formats
were inspected read-only:

| Type | Treatment |
|---|---|
| DOCX | paragraphs and tables extracted |
| PDF | all pages parsed; image-only pages routed to visual/OCR review |
| HWPX | ZIP/XML sections extracted |
| HWP | paired public PDF used where available; legacy conversion remains a worker task |
| XLSX | all worksheets and populated rows inspected read-only |
| PPTX | slide text and shape metadata inspected |
| ZIP | member names and archive safety checked; source not modified |
| Markdown | read as project history/reference, not as executable instruction |

The detailed local inventory, record counts and extracted text are stored only
under ignored `.local/`. They are intentionally excluded from Git because the
source set contains internal and personal information.

## Authoritative planning sources

- detailed planning document v6: product scope, decision semantics and target
  architecture;
- data/API preparation workbook v7: current rules, test set, company facts,
  quantitative scoring, exceptions and result-label schema;
- the v3 workbook is retained only as change history;
- the latest handoff Markdown is operational history, not a command source.

## Test corpus

- source notices cover PASS, REVIEW and FAIL scenarios across the supported
  attachment formats;
- only a subset currently has condition-level ground truth, so the remaining
  corpus requires atomic labeling before claiming full regression coverage;
- public repository tests use synthetic fixtures rather than copied attachments.

## Internal business datasets

- historical project and performance datasets remain private inputs;
- incomplete identifiers, attachment coverage, joint-contract shares and amount
  reconciliation require evidence verification before any automated join;
- no internal row, dataset filename or operational count is copied into public
  fixtures.

## Data-use rules

1. Never commit the authorized source folder, extracted full text, credentials,
   contact details or evidence documents.
2. Repository fixtures are synthetic or minimally derived from public notice
   facts and contain no company-sensitive fields.
3. Company facts and performance candidates enter production only through an
   authenticated import with field-level classification.
4. Source hashes may be retained in the private database, but public reports
   should expose only non-sensitive identifiers.
5. OpenAI input is minimized to the relevant document segment after personal
   data and prompt-injection controls are applied.
