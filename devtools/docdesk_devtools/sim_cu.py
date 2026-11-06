"""Simulated Content Understanding service (WSGI) shaped like the documented/observed 2025-11-01 API.

Every result carries `"_simulated": true`. Classification and fields come from simple keyword/label rules over the PDF
text so the local stack shows plausible, differing outputs. It is a SIMULATION used for local tests and the dev stack;
it says nothing about the real service's accuracy."""
from __future__ import annotations

import base64
import io
import json
import re
import threading
import uuid
from typing import Callable
from urllib.parse import parse_qs

KEYWORDS = [
    ("non-disclosure", "nda"), ("statement of work", "sow"), ("master service agreement", "msa"),
    ("purchase order", "purchase_order"), ("bank statement", "bank_statement"), ("credit card statement", "credit_card_statement"),
    ("certificate of insurance", "certificate_of_insurance"), ("invoice", "invoice"), ("receipt", "receipt"),
]
FAMILY = {"invoice": "transaction", "receipt": "transaction", "purchase_order": "transaction", "expense_report": "transaction",
          "bank_statement": "statement", "credit_card_statement": "statement", "msa": "agreement", "nda": "agreement",
          "sow": "agreement", "vendor_agreement": "agreement", "tax_document": "tax", "certificate_of_insurance": "insurance"}
LABELS = {  # label in text -> (field, type)
    "invoice no": ("reference_number", "string"), "po number": ("reference_number", "string"), "receipt no": ("reference_number", "string"),
    "agreement no": ("reference_number", "string"), "statement no": ("reference_number", "string"),
    "invoice date": ("document_date", "date"), "order date": ("document_date", "date"), "date": ("document_date", "date"),
    "statement date": ("document_date", "date"), "due date": ("due_date", "date"), "from": ("vendor_name", "string"),
    "vendor": ("vendor_name", "string"), "merchant": ("vendor_name", "string"), "bill to": ("customer_name", "string"),
    "total": ("total_amount", "number"), "subtotal": ("subtotal", "number"), "tax": ("tax_amount", "number"),
    "amount due": ("amount_due", "number"), "payment terms": ("payment_terms", "string"), "currency": ("currency", "string"),
    "effective date": ("effective_date", "date"), "parties": ("parties", "array"), "governing law": ("governing_law", "string"),
    "confidentiality term": ("confidentiality_duration_text", "string"), "term": ("term_description", "string"),
    "scope": ("scope_summary", "string"), "total fees": ("total_fees", "number"), "institution": ("institution_name", "string"),
    "closing balance": ("closing_balance", "number"), "opening balance": ("opening_balance", "number"),
    "period end": ("period_end", "date"), "period start": ("period_start", "date"), "title": ("document_title", "string"),
    "termination": ("termination_clause_text", "string"), "deliverables": ("deliverables", "array"),
}


def pdf_text(data: bytes) -> str:
    if data[:5] != b"%PDF-":
        return ""
    try:
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return ""


def _field(kind: str, value: str) -> dict | None:
    value = value.strip()
    if not value:
        return None
    try:
        if kind == "number":
            return {"type": "number", "valueNumber": float(re.sub(r"[^0-9.\-]", "", value)), "confidence": 0.9, "source": "D(1,0,0,1,0,1,1,0,1)"}
        if kind == "date":
            return {"type": "date", "valueDate": value[:10], "confidence": 0.9}
        if kind == "array":
            return {"type": "array", "valueArray": [{"type": "string", "valueString": p.strip()} for p in re.split(r";|\band\b", value) if p.strip()]}
    except ValueError:
        return {"type": "string", "valueString": value}
    return {"type": "string", "valueString": value, "confidence": 0.85}


def simulate_result(text: str, mime: str, analyzer_id: str) -> dict:
    low = text.lower()
    category = next((c for k, c in KEYWORDS if k in low), "other")
    contents = [{"markdown": text, "mimeType": mime, "kind": "document", "startPageNumber": 1,
                 "endPageNumber": max(1, text.count("\f") + 1)}]
    if category == "other":  # no extractor for `other`: the classifier's own segment item carries the category, with no fields
        contents.append({"path": "input1/segment1", "category": "other", "markdown": text, "kind": "document"})
    else:
        fields: dict = {}
        for line in text.splitlines():
            m = re.match(r"^\s*([A-Za-z ]+?)\s*:\s*(.+)$", line)
            if m and m[1].strip().lower() in LABELS:
                name, kind = LABELS[m[1].strip().lower()]
                f = _field(kind, m[2])
                if f:
                    fields[name] = f
        contents.append({"path": "input1/segment1", "category": category, "markdown": text, "fields": fields, "kind": "document"})
    return {"analyzerId": analyzer_id, "apiVersion": "2025-11-01", "createdAt": "2026-01-01T00:00:00Z", "warnings": [],
            "contents": contents}


class SimulatedCu:
    """WSGI app. Use `httpx.Client(transport=httpx.WSGITransport(app=sim))` or serve it with wsgiref."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.defaults: dict | None = None
        self.analyzers: dict[str, dict] = {}
        self.ops: dict[str, dict] = {}
        self.script: list[str] = []                      # one behaviour consumed per analyze submit; default "ok"
        self.rules: list[Callable[[str], str | None]] = []  # text -> behaviour, applied when script is empty
        self.polls_before_done = 1
        self.analyze_calls = 0
        self.put_calls: list[str] = []
        self.calls: list[tuple[str, str]] = []           # (method, path) of every request
        self.require_defaults_for_put = True
        self.analyzer_status: dict[str, str | list[str]] = {}   # analyzer id -> status, or a sequence (last value repeats) served on GET
        self.field_overrides: dict = {}                  # merged into the routed item's fields (scripted values for tests)

    # -- helpers --------------------------------------------------------------------------------
    @staticmethod
    def _resp(start, status: int, body: dict | bytes | None = None, headers: dict | None = None):
        raw = b"" if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
        h = [("Content-Type", "application/json"), ("Content-Length", str(len(raw)))] + list((headers or {}).items())
        start(f"{status} X", h)
        return [raw]

    @staticmethod
    def _err(start, status: int, code: str, inner: str | None = None, headers: dict | None = None):
        err = {"code": code, "message": "simulated"}
        if inner:
            err["innererror"] = {"code": inner}
        return SimulatedCu._resp(start, status, {"error": err}, headers)

    # -- WSGI -----------------------------------------------------------------------------------
    def __call__(self, environ, start):
        method, path = environ["REQUEST_METHOD"], environ["PATH_INFO"]
        qs = parse_qs(environ.get("QUERY_STRING", ""))
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length) if length else b""
        with self.lock:
            self.calls.append((method, path))
            return self._route(method, path, qs, body, environ, start)

    def _route(self, method, path, qs, body, environ, start):
        base = path.split("/contentunderstanding", 1)[-1]
        if base == "/defaults":
            if method == "GET":
                if self.defaults is None:
                    return self._err(start, 400, "InvalidRequest", "DefaultsNotSet")
                return self._resp(start, 200, {"modelDeployments": self.defaults})
            if method == "PATCH":
                self.defaults = {**(self.defaults or {}), **json.loads(body)["modelDeployments"]}
                return self._resp(start, 200, {"modelDeployments": self.defaults})
        m = re.match(r"^/analyzers/([^/:]+):analyze$", base)
        if m and method == "POST":
            return self._analyze(m[1], body, environ, start)
        m = re.match(r"^/analyzers/([^/:]+)$", base)
        if m:
            aid = m[1]
            if method == "GET":
                if aid not in self.analyzers:
                    return self._err(start, 404, "NotFound")
                st = self.analyzer_status.get(aid, "ready")
                if isinstance(st, list):
                    st = st.pop(0) if len(st) > 1 else st[0]
                return self._resp(start, 200, {"analyzerId": aid, "status": st, **self._strip_empty(self.analyzers[aid])})
            if method == "PUT":
                self.put_calls.append(aid)
                if self.require_defaults_for_put and not (self.defaults and "prebuilt-analyzer-embedding" in self.defaults):
                    return self._err(start, 400, "InvalidRequest", "DefaultDeploymentModelNotFound")
                if aid in self.analyzers and qs.get("allowReplace", ["false"])[0] != "true":
                    return self._err(start, 409, "Conflict", "AnalyzerAlreadyExists")
                self.analyzers[aid] = json.loads(body)
                return self._resp(start, 201, {"analyzerId": aid, "status": "ready"})
        m = re.match(r"^/analyzerResults/([^/]+)$", base)
        if m and method == "GET":
            return self._poll(m[1], start)
        return self._err(start, 404, "NotFound")

    @staticmethod
    def _strip_empty(d):  # K-005: the service omits authored empty objects on GET
        if isinstance(d, dict):
            return {k: SimulatedCu._strip_empty(v) for k, v in d.items() if v != {}}
        return d

    def _analyze(self, analyzer_id, body, environ, start):
        self.analyze_calls += 1
        try:
            req = json.loads(body)["inputs"][0]
            data = base64.b64decode(req["data"])
            mime = req.get("mimeType", "application/pdf")
        except Exception:
            return self._err(start, 400, "InvalidRequest", "InvalidContent")
        if analyzer_id not in self.analyzers:
            return self._err(start, 404, "ModelNotFound")
        text = pdf_text(data)
        behaviour = self.script.pop(0) if self.script else next((b for r in self.rules if (b := r(text))), "ok")
        host = environ.get("HTTP_HOST", "localhost")
        if behaviour == "throttle":
            return self._err(start, 429, "TooManyRequests", None, {"Retry-After": "1"})
        if behaviour == "unavailable":
            return self._err(start, 503, "ServiceUnavailable")
        if behaviour == "rejected":
            return self._err(start, 400, "InvalidRequest", "InvalidContent")
        if behaviour == "forbidden":
            return self._err(start, 403, "AccessDenied")
        op_id = uuid.uuid4().hex
        result = simulate_result(text, mime, analyzer_id)
        for item in result["contents"]:
            if "fields" in item:
                item["fields"].update(self.field_overrides)
        self.ops[op_id] = {"behaviour": behaviour, "polls": 0, "result": result}
        return self._resp(start, 202, None, {"Operation-Location": f"http://{host}/contentunderstanding/analyzerResults/{op_id}?api-version=2025-11-01"})

    def _poll(self, op_id, start):
        op = self.ops.get(op_id)
        if op is None:
            return self._err(start, 404, "NotFound")
        op["polls"] += 1
        b = op["behaviour"]
        if b == "never_finishes" or op["polls"] <= self.polls_before_done:
            return self._resp(start, 200, {"id": op_id, "status": "Running"})
        if b == "failed_content":
            return self._resp(start, 200, {"id": op_id, "status": "Failed", "error": {"code": "InvalidRequest", "innererror": {"code": "InvalidContent"}}})
        if b == "failed_transient":
            return self._resp(start, 200, {"id": op_id, "status": "Failed", "error": {"code": "ServerError", "innererror": {"code": "ServiceUnavailable"}}})
        if b == "empty":
            return self._resp(start, 200, b"")
        if b == "malformed":
            return self._resp(start, 200, b"<html>not json</html>")
        if b == "no_contents":
            return self._resp(start, 200, {"id": op_id, "status": "Succeeded", "result": {"contents": []}, "_simulated": True})
        return self._resp(start, 200, {"id": op_id, "status": "Succeeded", "result": op["result"], "_simulated": True})
