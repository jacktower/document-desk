# Normalized result and SQL index model (`docdesk.result/1`)

The provider's response is stored **unmodified** (artifact kind `provider_response`). The application derives from it a versioned
normalized result (artifact kind `normalized_result`, JSON Schema in `src/docdesk/schemas/result-1.schema.json`, validated by tests)
and indexes selected values in SQL. Nothing is invented: a value, `confidence` or `source` appears only if the provider returned it.

## Document

```json
{
  "schema_version": "docdesk.result/1", "result_id": "...", "document_id": "...", "attempt_no": 1, "generated_at": "...",
  "provider": { "name": "azure-content-understanding", "api_version": "2025-11-01",
                "classifier_analyzer_id": "docdesk-classifier-v1", "extractor_analyzer_id": "docdesk-transaction-v1",
                "config_version": "1", "config_hash": "sha256:...", "operation_id": "...", "simulated": false },
  "classification": { "category": "invoice", "provider_category": "invoice",
                      "status": "classified | other | unrecognized_category | not_classified", "confidence": null },
  "text": { "artifact_id": "...", "status": "present | missing", "char_count": 1234, "page_count": 2 },
  "fields": { "total_amount": { "status": "present", "type": "number", "value": 408.24, "confidence": 0.91, "source": "D(1,...)", "provider_type": "number" },
              "due_date":     { "status": "absent",  "type": "date",   "value": null, "confidence": null, "source": null, "provider_type": null } },
  "flags": [ { "code": "FIELD_INVALID", "severity": "warning", "field": "due_date", "message": "..." } ],
  "needs_review": true
}
```

* **Classification**: `category` is one of the 12 types or `other`; `unrecognized_category` (a category the application does not know) is stored as `other` with the provider's raw value kept in `provider_category`; `not_classified` (no category anywhere in the response) has `category: null`. **`confidence` is always `null`**: the GA service returns no category-level confidence when segmentation is off, and the application does not compute one.
* **Fields**: every field *applicable to the category* is listed, so "not found" is explicit. `present` / `absent` (missing, empty, or no value member) / `invalid` (the provider's value does not fit the expected type; the raw value is kept in SQL `value_string`). Inapplicable fields the shared extractor returns are dropped from the normalized result but remain in the raw response.
* **Types**: `string`, `date` (ISO `YYYY-MM-DD`), `number` (provider double; stored as `DECIMAL(19,4)`), `integer`, `boolean`, `array` (of strings or of small objects). Money is a number plus the separate `currency` field.
* **Needs review** is set when the category is `other`/unrecognized/not classified, or any of `KEY_FIELDS_ABSENT`, `TEXT_MISSING`, `FIELD_INVALID`, `PROVIDER_WARNING`, `PROVIDER_SHAPE_UNEXPECTED`, `MULTIPLE_CATEGORIES` is raised. `KEY_FIELDS_ABSENT` is a plain plausibility check (a classified document where none of its key fields was found), not a probability. `FIELD_LOW_CONFIDENCE` (provider confidence below `LOW_CONFIDENCE_THRESHOLD`) is informational and a display heuristic, not a calibrated score.

## Field catalogue (single source: `src/docdesk/cu/registry.py`)

* **Common** (all categories but `other`): `document_title`, `document_date`, `reference_number`, `currency`, `summary` (generated, one neutral sentence).
* **transaction** (invoice, receipt, purchase_order, expense_report): `vendor_name`, `customer_name`, `due_date`, `subtotal`, `tax_amount`, `total_amount`, `amount_due`, `payment_method`, `payment_terms`, `delivery_terms`, `submitted_by`, `period_start`, `period_end`, `line_items[description, quantity, unit_price, amount]` - each category reports the applicable subset.
* **statement** (bank_statement, credit_card_statement): `institution_name`, `account_holder_name`, `account_reference` (as printed, typically masked; never reconstructed), `period_start/end`, `opening_balance`, `closing_balance`, `total_credits`, `total_debits`, `minimum_payment_due`, `payment_due_date`, `credit_limit`, `transactions[date, description, amount]`.
* **agreement** (msa, nda, sow, vendor_agreement): `parties[]`, `effective_date`, `expiration_date`, `term_description`, `governing_law`, `confidentiality_duration_text`, `permitted_use_text`, `termination_clause_text`, `liability_limitation_clause_text` (clause fields are **verbatim quotes, informational only, not a legal interpretation**), `scope_summary`, `deliverables[]`, `milestones[description, date]`, `total_fees`, `related_agreement_reference`.
* **tax** (tax_document): `form_type`, `tax_year`, `entity_name`, `reported_amounts[label, amount]`. Taxpayer identification numbers are deliberately not extracted.
* **insurance** (certificate_of_insurance): `insured_name`, `insurer_name`, `certificate_holder_name`, `policy_numbers[]`, `policy_types[]`, `effective_date`, `expiration_date`, `coverage_limits[coverage, limit_text]`.

Every extractor field description tells the model to leave the field empty when the document does not state it; only `summary` and
`scope_summary` use generation, the rest extract.

## SQL (the stable, queryable surface)

| Table | Content |
|---|---|
| `documents` | identity, original filename, content type, size, SHA-256, pages, status (`uploading` `processing` `completed` `failed` `rejected` `upload_failed`), current attempt, `category` (copied from the current result), `needs_review`, `retain_until`, last error |
| `processing_attempts` | one row per attempt (`initial` / `replay`, replay point, reason, prior attempt, status) - append-only in effect |
| `stage_states` | per attempt and stage: status, tries / max tries, lease, next run, timestamps, last error |
| `stage_tries` | one row per try: worker, start/finish, outcome (`succeeded` `retryable_error` `terminal_error` `lease_lost` `lease_expired`), error code/message, HTTP status, provider request id |
| `artifacts` | addressable blobs: `original`, `provider_response`, `extracted_text`, `normalized_result` (container, blob name, SHA-256, size). Rows are never updated or deleted |
| `document_results` | one per attempt (unique): category, classification status, provider/API/analyzer/config version and hash, operation id, `simulated`, artifact ids, page and text counts, flags (JSON) |
| `document_fields` | one row per field applicable to the category: `status`, typed value columns (`value_text` complete string, `value_string` 400-char search key + truncation bit, `value_number`, `value_date`, `value_bool`, `value_json`), `confidence`, `source` (<= 500 chars; full value in the artifact). Indexed by `(field_name, value_date)`, `(field_name, value_number)`, `(field_name, value_string)` |
| `outbox` | next-stage events committed with the state change; dispatched to Service Bus by the worker |
| `worker_heartbeats`, `dependency_health` | operational status written by the workers |

Example: invoices due in April 2026 with their totals:

```sql
SELECT d.original_filename, due.value_date, tot.value_number
FROM dbo.documents d
JOIN dbo.document_fields due ON due.result_id = d.current_result_id AND due.field_name = 'due_date' AND due.status = 'present'
JOIN dbo.document_fields tot ON tot.result_id = d.current_result_id AND tot.field_name = 'total_amount'
WHERE d.category = 'invoice' AND due.value_date >= '2026-04-01' AND due.value_date < '2026-05-01';
```
