# OpenAI extraction contract v0.1.0

This contract defines the boundary between document extraction and the
deterministic PAI_LOOP decision engine. The language model is an evidence
extractor, not the final decision maker.

## Request policy

- Use the Responses API with a server-configured model; never expose the API key
  or model call in the browser.
- Set `store: false` for tender-document analysis.
- Send only the minimum relevant, redacted document segments.
- Mark every attachment and page body as untrusted source text. Instructions
  found inside a tender document are evidence to quote, never commands to obey.
- Pin the prompt version and JSON Schema version in every request and audit row.
- Bound input size, output tokens, retries and request time. A timeout or invalid
  output becomes `REVIEW`; it never becomes an inferred `PASS`.

OpenAI documents that Structured Outputs in the Responses API adheres to a JSON
Schema when `text.format.type` is `json_schema` and `strict` is enabled. It also
requires all fields to be listed as required and objects to use
`additionalProperties: false`.

Reference:

- <https://developers.openai.com/api/docs/guides/structured-outputs>
- <https://developers.openai.com/api/docs/guides/your-data>

## Output schema

The production request uses a strict schema equivalent to the following. A
nullable value represents “not established by the supplied evidence”; it must
not be guessed.

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "document_type": {
      "type": "string",
      "enum": ["NOTICE", "RFP", "SCOPE", "FORM", "OTHER"]
    },
    "requirements": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "requirement_id": { "type": "string" },
          "category": {
            "type": "string",
            "enum": [
              "ENTITY", "INDUSTRY_CODE", "CERTIFICATION",
              "DIRECT_PRODUCTION", "REGION", "PERFORMANCE",
              "PERSONNEL", "FACILITY", "CONSORTIUM", "SANCTION",
              "SUBMISSION", "OTHER"
            ]
          },
          "logic": { "type": "string", "enum": ["AND", "OR", "SINGLE"] },
          "normalized_condition": { "type": "string" },
          "mandatory": { "type": "boolean" },
          "deadline_basis": { "type": ["string", "null"] },
          "evidence": {
            "type": "array",
            "items": {
              "type": "object",
              "additionalProperties": false,
              "properties": {
                "attachment_id": { "type": "string" },
                "page": { "type": ["integer", "null"], "minimum": 1 },
                "section": { "type": ["string", "null"] },
                "quote": { "type": "string", "maxLength": 500 },
                "confidence": { "type": "number", "minimum": 0, "maximum": 1 }
              },
              "required": [
                "attachment_id", "page", "section", "quote", "confidence"
              ]
            }
          },
          "ambiguity_reason": { "type": ["string", "null"] }
        },
        "required": [
          "requirement_id", "category", "logic", "normalized_condition",
          "mandatory", "deadline_basis", "evidence", "ambiguity_reason"
        ]
      }
    },
    "missing_or_unreadable": {
      "type": "array",
      "items": { "type": "string" }
    },
    "summary": { "type": "string", "maxLength": 1000 }
  },
  "required": ["document_type", "requirements", "missing_or_unreadable", "summary"]
}
```

## Validation and decision hand-off

1. Reject refusals, incomplete responses, schema errors and unknown attachment
   identifiers.
2. Verify each quote exists in the normalized extraction and that its page or
   section anchor resolves to the claimed source.
3. De-duplicate requirements without merging distinct AND/OR branches.
4. Persist the accepted extraction with the source file SHA-256, model snapshot,
   prompt version, schema version and validation result.
5. Feed accepted atomic requirements to the versioned deterministic rules. The
   model may propose a category or ambiguity, but cannot emit the authoritative
   `PASS`, `FAIL`, `REVIEW`, quantitative score or final GO/NO-GO result.

## Failure behavior

| Condition | System behavior |
|---|---|
| OCR or table reconstruction uncertain | `R07` document-quality review |
| Eligibility phrase ambiguous | linked `R01`–`R06`/`R09` review rule |
| Quote or anchor cannot be verified | discard claim and record validation error |
| Model refusal or safety block | record refusal metadata, expose no raw payload, route to review |
| Timeout/rate limit | bounded retry with jitter, then review queue |
| Schema or parser failure | no partial acceptance; quarantine the run |

## Data-retention note

OpenAI states that API inputs and outputs are not used to train models unless an
organization explicitly opts in. The API may retain abuse-monitoring logs for up
to 30 days by default, and the Responses API has endpoint-specific application
state behavior. `store: false`, data minimization and the organization's approved
retention configuration therefore remain mandatory controls; they are not a
substitute for removing unnecessary personal or confidential data before upload.

## Connectivity verification

On 2026-08-16, the configured project key was verified without logging the key
or response payload:

- `GET /v1/models`: authenticated successfully;
- `POST /v1/responses`: completed with `gpt-5.6-luna`;
- strict schema result: the synthetic regional requirement was returned as the
  allowed `REGION` enum with a boolean mandatory flag;
- the production boundary's full requirement/evidence schema was then called
  with one synthetic attachment and returned `ACCEPTED` with one verified
  evidence-anchored requirement;
- the request explicitly used `store: false`;
- no source attachment or company data was sent during this smoke test.

This confirms connectivity and schema enforcement only. It is not a quality
evaluation of real tender extraction; the labeled corpus and evidence-anchor
validator remain the release gates for that claim.
