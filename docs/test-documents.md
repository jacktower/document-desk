# Test documents

All documents are **entirely fictional**: invented parties, identifiers and amounts, each footed "FICTIONAL SAMPLE". They are generated
deterministically by `tools/gen_testdocs.py` (needs the `dev` extra: reportlab and Pillow):

```bash
python tools/gen_testdocs.py testdocs        # writes ~20 files; testdocs/ is git-ignored
```

| File | Purpose | What to expect from a real Content Understanding run (qualitative) |
|---|---|---|
| `invoice_northwind.pdf` | invoice with dates, three line items, subtotal/tax/total | category `invoice`; invoice number, dates, vendor, customer, total likely found; line items may or may not be |
| `invoice_scan.tif` | the same invoice as a raster TIFF (text only as pixels) | exercises TIFF and OCR; `invoice` likely, fields depend on OCR quality |
| `multipage_invoice.pdf` | two-page invoice | page count 2; tests multi-page routing (not observed live yet) |
| `nda_fabrikam.pdf` | mutual NDA with parties, effective date, 3-year confidentiality term, governing law | category `nda`; parties, effective date, governing law, confidentiality wording likely found; termination clause absent (not in the text) |
| `sow_tailspin.pdf` | statement of work with scope, deliverables, three milestones, fees | category `sow` (could be confused with `vendor_agreement`/`msa`); milestones/fees likely found |
| `msa_wingtip.pdf` | master service agreement with termination and limitation of liability | category `msa`; clause fields hold quoted text only |
| `purchase_order_alpine.pdf`, `receipt_lakeside.pdf`, `bank_statement_ffcu.pdf` | optional extra types | `purchase_order`, `receipt`, `bank_statement`; masked account reference stays masked |
| `other_bake_sale_flyer.pdf` | unrelated flyer | category `other`; no fields; "needs review" |
| `invalid_text_renamed.pdf`, `unsupported.docx`, `empty.pdf` | rejected at **upload** (`CORRUPT_HEADER`, `UNSUPPORTED_TYPE`, `EMPTY_FILE`) with a clear message and no record | no processing |
| `corrupt_header_only.pdf` | passes the upload header check but is not a parseable PDF | accepted, then **rejected at intake** (`INPUT_CORRUPT`); no Content Understanding call |
| `oversized.pdf` | 21 MiB, above the 20 MB default limit | rejected at upload (`TOO_LARGE`) |

> **Results are model-dependent.** These are observations to compare against, not assertions. The test suite never asserts
> a real model's output, and the project makes no accuracy claim. When running against Azure, expect some fields to be missing or
> wrong; the application shows missing fields as "not found in document" and flags results that look doubtful.

The local simulated provider (`devtools/`) classifies these files with simple keyword rules and reads `Label: value` lines. That
only demonstrates the plumbing; its output is stamped `_simulated` and shown with a "SIMULATED PROVIDER" banner.
