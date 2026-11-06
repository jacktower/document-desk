"""Single source of truth for categories, extractor families and fields.

Analyzer definitions, the normalizer, SQL indexing and UI labels are all generated from this module. Changing anything
here changes the configuration hash; deploy such a change under a new ANALYZER_CONFIG_VERSION (analyzer definitions
under an existing id are never modified)."""
from __future__ import annotations

from dataclasses import dataclass

OTHER = "other"


@dataclass(frozen=True)
class Prop:
    name: str
    type: str  # string | number | integer | date | boolean
    description: str = ""


@dataclass(frozen=True)
class FieldDef:
    name: str
    type: str  # string | date | number | integer | boolean | array<string> | array<object>
    description: str
    method: str = "extract"  # extract | generate
    props: tuple[Prop, ...] = ()
    label: str = ""

    @property
    def title(self) -> str:
        return self.label or self.name.replace("_", " ").capitalize()

    @property
    def base_type(self) -> str:
        return "array" if self.type.startswith("array") else self.type


@dataclass(frozen=True)
class Category:
    name: str
    family: str | None
    description: str
    key_fields: tuple[str, ...] = ()
    applicable: tuple[str, ...] = ()  # filled from family + extras
    label: str = ""

    @property
    def title(self) -> str:
        return self.label or self.name.replace("_", " ").capitalize()


NOT_STATED = " Leave empty if it is not stated in the document; never guess or compute."

COMMON: tuple[FieldDef, ...] = (
    FieldDef("document_title", "string", "Title or heading of the document as printed." + NOT_STATED),
    FieldDef("document_date", "date", "Main date of the document: issue, transaction, statement, order or agreement date." + NOT_STATED),
    FieldDef("reference_number", "string", "Primary reference identifier printed on the document: invoice, PO, receipt, report, agreement, statement or policy number." + NOT_STATED),
    FieldDef("currency", "string", "Currency of the monetary amounts, as printed (symbol or ISO 4217 code)." + NOT_STATED),
    FieldDef("summary", "string", "One neutral, factual sentence describing what the document is. Informational only; no legal or financial judgement.", method="generate"),
)

_LINE = (Prop("description", "string"), Prop("quantity", "number"), Prop("unit_price", "number"), Prop("amount", "number"))

FAMILY_FIELDS: dict[str, tuple[FieldDef, ...]] = {
    "transaction": (
        FieldDef("vendor_name", "string", "Name of the seller, issuer, merchant or supplier as printed." + NOT_STATED),
        FieldDef("customer_name", "string", "Name of the customer, buyer or bill-to party as printed." + NOT_STATED),
        FieldDef("due_date", "date", "Payment due date." + NOT_STATED),
        FieldDef("subtotal", "number", "Subtotal before tax, numeric only." + NOT_STATED),
        FieldDef("tax_amount", "number", "Total tax amount, numeric only." + NOT_STATED),
        FieldDef("total_amount", "number", "Grand total including tax, numeric only." + NOT_STATED),
        FieldDef("amount_due", "number", "Amount currently due, numeric only." + NOT_STATED),
        FieldDef("payment_method", "string", "Payment method as printed (card brand, cash, transfer...)." + NOT_STATED),
        FieldDef("payment_terms", "string", "Payment terms as printed, e.g. 'Net 30'." + NOT_STATED),
        FieldDef("delivery_terms", "string", "Delivery or shipping terms as printed." + NOT_STATED),
        FieldDef("submitted_by", "string", "Person or department that submitted an expense report." + NOT_STATED),
        FieldDef("period_start", "date", "Start of the period covered (expense reports)." + NOT_STATED),
        FieldDef("period_end", "date", "End of the period covered (expense reports)." + NOT_STATED),
        FieldDef("line_items", "array<object>", "Line items or expense entries as printed.", props=_LINE),
    ),
    "statement": (
        FieldDef("institution_name", "string", "Bank or card issuer name as printed." + NOT_STATED),
        FieldDef("account_holder_name", "string", "Account holder name as printed." + NOT_STATED),
        FieldDef("account_reference", "string", "Account reference exactly as printed, typically masked. Never reconstruct or complete digits." + NOT_STATED),
        FieldDef("period_start", "date", "Statement period start date." + NOT_STATED),
        FieldDef("period_end", "date", "Statement period end date." + NOT_STATED),
        FieldDef("opening_balance", "number", "Opening or previous balance, numeric only." + NOT_STATED),
        FieldDef("closing_balance", "number", "Closing or new balance, numeric only." + NOT_STATED),
        FieldDef("total_credits", "number", "Total credits or payments received, numeric only." + NOT_STATED),
        FieldDef("total_debits", "number", "Total debits or charges, numeric only." + NOT_STATED),
        FieldDef("minimum_payment_due", "number", "Minimum payment due (card statements), numeric only." + NOT_STATED),
        FieldDef("payment_due_date", "date", "Payment due date (card statements)." + NOT_STATED),
        FieldDef("credit_limit", "number", "Credit limit (card statements), numeric only." + NOT_STATED),
        FieldDef("transactions", "array<object>", "Individual transactions listed on the statement.",
                 props=(Prop("date", "date"), Prop("description", "string"), Prop("amount", "number"))),
    ),
    "agreement": (
        FieldDef("parties", "array<string>", "Names of the contracting parties as written." + NOT_STATED),
        FieldDef("effective_date", "date", "Date the agreement takes effect." + NOT_STATED),
        FieldDef("expiration_date", "date", "Date the agreement expires or ends, if a fixed date is stated." + NOT_STATED),
        FieldDef("term_description", "string", "Term or duration wording, quoted from the document." + NOT_STATED),
        FieldDef("governing_law", "string", "Governing law or jurisdiction as stated." + NOT_STATED),
        FieldDef("confidentiality_duration_text", "string", "NDA confidentiality duration wording, quoted verbatim." + NOT_STATED),
        FieldDef("permitted_use_text", "string", "NDA permitted-use wording, quoted verbatim." + NOT_STATED),
        FieldDef("termination_clause_text", "string", "Termination clause text, quoted verbatim. Do not interpret it." + NOT_STATED),
        FieldDef("liability_limitation_clause_text", "string", "Limitation-of-liability clause text, quoted verbatim. Do not interpret it." + NOT_STATED),
        FieldDef("scope_summary", "string", "Neutral summary of the scope of work (SOW). Informational only.", method="generate"),
        FieldDef("deliverables", "array<string>", "Deliverables listed in the document." + NOT_STATED),
        FieldDef("milestones", "array<object>", "Milestones with dates, as listed.",
                 props=(Prop("description", "string"), Prop("date", "date"))),
        FieldDef("total_fees", "number", "Total fees or contract value, numeric only." + NOT_STATED),
        FieldDef("related_agreement_reference", "string", "Reference to a related or parent agreement (e.g. the MSA a SOW falls under)." + NOT_STATED),
    ),
    "tax": (
        FieldDef("form_type", "string", "Tax form type as printed, e.g. W-9, 1099-NEC, 941." + NOT_STATED),
        FieldDef("tax_year", "integer", "Tax year as printed." + NOT_STATED),
        FieldDef("entity_name", "string", "Name of the entity or filer as printed. Do not extract taxpayer identification numbers." + NOT_STATED),
        FieldDef("reported_amounts", "array<object>", "Labelled amounts reported on the form.",
                 props=(Prop("label", "string"), Prop("amount", "number"))),
    ),
    "insurance": (
        FieldDef("insured_name", "string", "Name of the insured entity as printed." + NOT_STATED),
        FieldDef("insurer_name", "string", "Name of the insurance company as printed." + NOT_STATED),
        FieldDef("certificate_holder_name", "string", "Name of the certificate holder as printed." + NOT_STATED),
        FieldDef("policy_numbers", "array<string>", "Policy numbers as printed." + NOT_STATED),
        FieldDef("policy_types", "array<string>", "Types of coverage (general liability, workers compensation...) as printed." + NOT_STATED),
        FieldDef("effective_date", "date", "Earliest policy effective date shown." + NOT_STATED),
        FieldDef("expiration_date", "date", "Earliest policy expiration date shown." + NOT_STATED),
        FieldDef("coverage_limits", "array<object>", "Coverage limits as printed.",
                 props=(Prop("coverage", "string"), Prop("limit_text", "string"))),
    ),
}

FAMILIES: tuple[str, ...] = tuple(FAMILY_FIELDS)

_TRANSACTION_BASE = ("vendor_name", "customer_name", "subtotal", "tax_amount", "total_amount", "line_items")
_COMMON_NAMES = tuple(f.name for f in COMMON)


def _app(family: str, *names: str) -> tuple[str, ...]:
    return _COMMON_NAMES + tuple(n for n in names)


CATEGORIES: tuple[Category, ...] = (
    Category("invoice", "transaction",
             "Request for payment from a vendor or service provider: issuer, customer, invoice number, dates, line items, taxes and amount due.",
             key_fields=("reference_number", "total_amount", "amount_due", "vendor_name"),
             applicable=_app("transaction", "vendor_name", "customer_name", "due_date", "subtotal", "tax_amount", "total_amount", "amount_due", "payment_terms", "line_items")),
    Category("receipt", "transaction",
             "Evidence of a completed purchase or payment: merchant, transaction date, items, taxes, total and payment method. Already paid; not a request for payment.",
             key_fields=("vendor_name", "total_amount"),
             applicable=_app("transaction", "vendor_name", "subtotal", "tax_amount", "total_amount", "payment_method", "line_items")),
    Category("purchase_order", "transaction",
             "Authorization or request by a buyer to purchase goods or services from a supplier: PO number, requested items, quantities, prices and delivery terms.",
             key_fields=("reference_number", "vendor_name", "total_amount"),
             applicable=_app("transaction", "vendor_name", "customer_name", "subtotal", "tax_amount", "total_amount", "payment_terms", "delivery_terms", "line_items"),
             label="Purchase order"),
    Category("expense_report", "transaction",
             "Summary of business expenses submitted by a person, with dates, categories, amounts or supporting references.",
             key_fields=("submitted_by", "total_amount", "line_items"),
             applicable=_app("transaction", "submitted_by", "period_start", "period_end", "total_amount", "line_items")),
    Category("bank_statement", "statement",
             "Periodic bank account statement: institution, account reference, statement period, balances and transactions.",
             key_fields=("institution_name", "closing_balance", "period_end"),
             applicable=_app("statement", "institution_name", "account_holder_name", "account_reference", "period_start", "period_end",
                             "opening_balance", "closing_balance", "total_credits", "total_debits", "transactions")),
    Category("credit_card_statement", "statement",
             "Periodic credit card statement: issuer, account reference, statement period, charges, credits and payment information.",
             key_fields=("institution_name", "closing_balance", "minimum_payment_due"),
             applicable=_app("statement", "institution_name", "account_holder_name", "account_reference", "period_start", "period_end",
                             "closing_balance", "total_credits", "total_debits", "minimum_payment_due", "payment_due_date", "credit_limit", "transactions")),
    Category("msa", "agreement",
             "Master Service Agreement: umbrella terms governing an ongoing business relationship, expecting separate statements of work or orders. Parties, effective date, term, governing law, termination and limitation of liability.",
             key_fields=("parties", "effective_date"),
             applicable=_app("agreement", "parties", "effective_date", "expiration_date", "term_description", "governing_law",
                             "termination_clause_text", "liability_limitation_clause_text"), label="Master Service Agreement"),
    Category("nda", "agreement",
             "Non-Disclosure Agreement: confidentiality agreement with parties, effective date, confidentiality duration, permitted use, exclusions and governing law.",
             key_fields=("parties", "effective_date", "confidentiality_duration_text"),
             applicable=_app("agreement", "parties", "effective_date", "expiration_date", "term_description", "governing_law",
                             "confidentiality_duration_text", "permitted_use_text", "termination_clause_text"), label="Non-Disclosure Agreement"),
    Category("sow", "agreement",
             "Statement of Work: project-specific scope, deliverables, milestones, schedule and fees under a business relationship, often referencing a master agreement.",
             key_fields=("parties", "scope_summary", "deliverables", "milestones"),
             applicable=_app("agreement", "parties", "effective_date", "expiration_date", "term_description", "scope_summary", "deliverables",
                             "milestones", "total_fees", "related_agreement_reference"), label="Statement of Work"),
    Category("vendor_agreement", "agreement",
             "Contract for a defined product or service with a vendor or service provider that is not better described as a master agreement, NDA or statement of work.",
             key_fields=("parties", "effective_date"),
             applicable=_app("agreement", "parties", "effective_date", "expiration_date", "term_description", "governing_law", "total_fees",
                             "termination_clause_text", "liability_limitation_clause_text"), label="Vendor / service agreement"),
    Category("tax_document", "tax",
             "Tax form or filing such as W-9, 1099 or a business tax statement: tax year, form type, entity and reported amounts.",
             key_fields=("form_type", "tax_year", "entity_name"),
             applicable=_app("tax", "form_type", "tax_year", "entity_name", "reported_amounts")),
    Category("certificate_of_insurance", "insurance",
             "Certificate of insurance: evidence of business insurance coverage with insured entity, insurer, policy types, effective and expiration dates and limits.",
             key_fields=("insured_name", "insurer_name", "policy_numbers"),
             applicable=_app("insurance", "insured_name", "insurer_name", "certificate_holder_name", "policy_numbers", "policy_types",
                             "effective_date", "expiration_date", "coverage_limits")),
)

OTHER_DESCRIPTION = ("Anything that is not clearly one of the other categories, or a document with too little content "
                     "to classify reliably.")

CATEGORY_BY_NAME: dict[str, Category] = {c.name: c for c in CATEGORIES}
CATEGORY_NAMES: tuple[str, ...] = tuple(c.name for c in CATEGORIES) + (OTHER,)

# Family field names are unique within a family; across families the same name may appear (e.g. period_start) with the
# same type and meaning, so the registry looks fields up by family.
_FIELDS_BY_FAMILY: dict[str, dict[str, FieldDef]] = {
    fam: {f.name: f for f in (*COMMON, *fields)} for fam, fields in FAMILY_FIELDS.items()
}


def family_fields(family: str) -> tuple[FieldDef, ...]:
    return (*COMMON, *FAMILY_FIELDS[family])


def applicable_fields(category: str) -> tuple[FieldDef, ...]:
    """Fields reported (present, absent or invalid) for a category, in registry order. Empty for `other`."""
    cat = CATEGORY_BY_NAME.get(category)
    if cat is None or cat.family is None:
        return ()
    by_name = _FIELDS_BY_FAMILY[cat.family]
    return tuple(by_name[n] for n in by_name if n in cat.applicable)


def field_label(name: str) -> str:
    for fam in _FIELDS_BY_FAMILY.values():
        if name in fam:
            return fam[name].title
    return name.replace("_", " ").capitalize()


def category_label(name: str | None) -> str:
    if not name:
        return "Unclassified"
    if name == OTHER:
        return "Other / unknown"
    c = CATEGORY_BY_NAME.get(name)
    return c.title if c else name


def sql_type(f: FieldDef) -> str:
    return f.base_type
