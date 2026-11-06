"""Pure functions that turn a stored Content Understanding response into the application's `docdesk.result/1`.

Nothing here invents a value: a field that is missing, empty or unreadable is reported as `absent` or `invalid`, and
confidence/source are copied only when the provider returned them."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from ..config import Settings
from ..domain.errors import StageError
from . import registry as R
from .analyzer_config import classifier_id, config_hash, extractor_id

SCHEMA_VERSION = "docdesk.result/1"
PROVIDER_NAME = "azure-content-understanding"
VALUE_STRING_KEY_LEN = 400
SOURCE_MAX = 500


@dataclass
class FieldRow:
    """One document_fields row, ready for SQL."""
    name: str
    value_type: str
    status: str
    value: Any = None  # JSON-friendly normalized value
    value_text: str | None = None
    value_string: str | None = None
    value_string_truncated: bool = False
    value_number: Decimal | None = None
    value_date: date | None = None
    value_bool: bool | None = None
    value_json: str | None = None
    confidence: float | None = None
    source: str | None = None
    provider_type: str | None = None


@dataclass
class Normalized:
    document: dict  # the docdesk.result/1 JSON
    fields: list[FieldRow]
    text: str | None
    category: str | None
    classification_status: str
    provider_category_raw: str | None
    needs_review: bool
    flags: list[dict]
    page_count: int | None
    simulated: bool
    operation_id: str | None
    extractor_analyzer_id: str | None
    classifier_analyzer_id: str


def parse_operation(raw: bytes) -> dict:
    """Validate the envelope of a stored operation response; returns the `result` object."""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise StageError("PROVIDER_RESPONSE_MALFORMED", "stored provider response is not JSON") from None
    if not isinstance(doc, dict) or str(doc.get("status", "")).lower() != "succeeded":
        raise StageError("PROVIDER_RESPONSE_MALFORMED", "stored provider response is not a succeeded operation")
    result = doc.get("result")
    contents = result.get("contents") if isinstance(result, dict) else None
    if not isinstance(contents, list) or not contents or not all(isinstance(c, dict) for c in contents):
        raise StageError("PROVIDER_RESULT_UNUSABLE", "provider result has no usable contents")
    return doc


def split_contents(contents: list[dict]) -> tuple[dict | None, list[dict]]:
    """Routed items carry a category and a nested path such as `input1/segment1` (K-006: they have no mimeType);
    the root item is the first item that is not routed. A classifier result with no routed item (e.g. `other`) is just a root."""
    routed = [c for c in contents if c.get("category") is not None and "/" in str(c.get("path", ""))]
    root = next((c for c in contents if not any(c is r for r in routed)), None)
    return root, routed


def extract_text(doc: dict) -> str | None:
    contents = doc["result"]["contents"]
    root, routed = split_contents(contents)
    if root and isinstance(root.get("markdown"), str) and root["markdown"].strip():
        return root["markdown"]
    parts = [c["markdown"] for c in sorted(routed, key=lambda c: str(c.get("path", ""))) if isinstance(c.get("markdown"), str) and c["markdown"].strip()]
    return "\n\n".join(parts) if parts else None


# -- value conversion ---------------------------------------------------------------------------------------
class _Bad(Exception):
    pass


def _scalar(ptype: str, pf: dict) -> Any:
    """Typed scalar from a provider field object; raises _Bad when the provider value does not match the expected type."""
    if ptype == "string":
        v = pf.get("valueString")
        if not isinstance(v, str):
            raise _Bad()
        return v.strip() or None
    if ptype == "date":
        v = pf.get("valueDate", pf.get("valueString"))
        if not isinstance(v, str):
            raise _Bad()
        try:
            return date.fromisoformat(v.strip()).isoformat()
        except ValueError:
            raise _Bad() from None
    if ptype in ("number", "integer"):
        v = pf.get("valueNumber", pf.get("valueInteger"))
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise _Bad()
        if ptype == "integer":
            if int(v) != v:
                raise _Bad()
            return int(v)
        return v
    if ptype == "boolean":
        v = pf.get("valueBoolean")
        if not isinstance(v, bool):
            raise _Bad()
        return v
    raise _Bad()


def _has_value(pf: Any) -> bool:
    return isinstance(pf, dict) and any(k.startswith("value") and pf[k] is not None for k in pf)


def _convert(fd: R.FieldDef, pf: Any) -> FieldRow:
    row = FieldRow(fd.name, fd.base_type, "absent")
    if not _has_value(pf):
        return row
    conf = pf.get("confidence")
    row.confidence = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None
    src = pf.get("source")
    row.source = src[:SOURCE_MAX] if isinstance(src, str) and src else None
    row.provider_type = pf.get("type") if isinstance(pf.get("type"), str) else None
    try:
        if fd.type == "array<string>":
            arr = pf.get("valueArray")
            if not isinstance(arr, list):
                raise _Bad()
            if not all(isinstance(i, dict) for i in arr):
                raise _Bad()
            vals = [x for x in (_scalar("string", i) for i in arr) if x]
            if not vals:
                return row
            row.value, row.status = vals, "present"
            row.value_json = json.dumps(vals, ensure_ascii=False)
        elif fd.type == "array<object>":
            arr = pf.get("valueArray")
            if not isinstance(arr, list):
                raise _Bad()
            items = []
            ptypes = {p.name: p.type for p in fd.props}
            for it in arr:
                obj = it.get("valueObject") if isinstance(it, dict) else None
                if not isinstance(obj, dict):
                    raise _Bad()
                out = {}
                for pname, ptype in ptypes.items():
                    sub = obj.get(pname)
                    if _has_value(sub):
                        try:
                            out[pname] = _scalar(ptype, sub)
                        except _Bad:
                            out[pname] = None
                    else:
                        out[pname] = None
                if any(v is not None for v in out.values()):
                    items.append(out)
            if not items:
                return row
            row.value, row.status = items, "present"
            row.value_json = json.dumps(items, ensure_ascii=False)
        else:
            v = _scalar(fd.type, pf)
            if v is None:
                return row
            row.value, row.status = v, "present"
            if fd.type == "string":
                row.value_text = v
                row.value_string = v[:VALUE_STRING_KEY_LEN]
                row.value_string_truncated = len(v) > VALUE_STRING_KEY_LEN
            elif fd.type == "date":
                row.value_date = date.fromisoformat(v)
            elif fd.type in ("number", "integer"):
                row.value_number = Decimal(str(v))
                if abs(row.value_number) >= Decimal("1e15"):
                    raise _Bad()
            elif fd.type == "boolean":
                row.value_bool = v
    except (_Bad, InvalidOperation):
        row.status, row.value = "invalid", None
        row.value_text = row.value_date = row.value_number = row.value_bool = row.value_json = None
        raw = json.dumps({k: v for k, v in pf.items() if k.startswith("value")}, ensure_ascii=False, default=str)
        row.value_string = raw[:VALUE_STRING_KEY_LEN]
        row.value_string_truncated = len(raw) > VALUE_STRING_KEY_LEN
    return row


# -- main entry point ---------------------------------------------------------------------------------------
def normalize(raw: bytes, *, document_id: str, attempt_no: int, settings: Settings,
              intake_page_count: int | None = None, raw_artifact_id: str | None = None, provenance: dict | None = None,
              text_artifact_id: str | None = None) -> Normalized:
    doc = parse_operation(raw)
    result = doc["result"]
    contents = result["contents"]
    root, routed = split_contents(contents)
    flags: list[dict] = []

    def flag(code: str, severity: str, message: str, fld: str | None = None) -> None:
        flags.append({"code": code, "severity": severity, "field": fld, "message": message})

    if root is not None and not root.get("mimeType"):
        flag("PROVIDER_SHAPE_UNEXPECTED", "warning", "top-level content item has no mimeType")

    # classification
    provider_cat = None
    if routed:
        provider_cat = routed[0].get("category")
    elif root is not None and root.get("category") is not None:
        provider_cat = root.get("category")
    provider_cat = provider_cat.strip() if isinstance(provider_cat, str) else None
    if len({str(r.get("category")) for r in routed}) > 1:
        flag("MULTIPLE_CATEGORIES", "warning", "provider returned more than one category; the first is used")
    if not provider_cat:
        status, category = "not_classified", None
    elif provider_cat.lower() == R.OTHER:
        status, category = "other", R.OTHER
    elif provider_cat.lower() in R.CATEGORY_BY_NAME:
        status, category = "classified", provider_cat.lower()
    else:
        status, category = "unrecognized_category", R.OTHER
        flag("CATEGORY_UNRECOGNIZED", "warning", "provider category is not in this application's category list")

    # fields (only from the routed extractor item, only those applicable to the category)
    fields: list[FieldRow] = []
    extra = 0
    if status == "classified":
        pfields = routed[0].get("fields") if routed else None
        pfields = pfields if isinstance(pfields, dict) else {}
        applicable = R.applicable_fields(category)
        known = {f.name for f in R.family_fields(R.CATEGORY_BY_NAME[category].family)}
        extra = len([k for k in pfields if k not in known])
        fields = [_convert(fd, pfields.get(fd.name)) for fd in applicable]
        for fr in fields:
            if fr.status == "invalid":
                flag("FIELD_INVALID", "warning", "provider value does not match the expected type", fr.name)
            elif fr.confidence is not None and fr.confidence < settings.low_confidence_threshold:
                flag("FIELD_LOW_CONFIDENCE", "info", f"provider confidence {fr.confidence:.2f} is below the display threshold "
                     f"{settings.low_confidence_threshold:.2f} (heuristic, not a calibrated score)", fr.name)
        key_fields = R.CATEGORY_BY_NAME[category].key_fields
        if key_fields and not any(f.status == "present" for f in fields if f.name in key_fields):
            flag("KEY_FIELDS_ABSENT", "warning", "none of the key fields for this category was found: " + ", ".join(key_fields))
        if extra:
            flag("PROVIDER_EXTRA_FIELDS", "info", f"{extra} provider field(s) are not part of this schema (kept in the raw response)")

    # text
    text = extract_text(doc)
    if text is None:
        flag("TEXT_MISSING", "warning", "the provider response contains no text")

    for w in result.get("warnings") or []:
        code = w.get("code") if isinstance(w, dict) else None
        msg = w.get("message") if isinstance(w, dict) else None
        flag("PROVIDER_WARNING", "warning", f"{str(code or 'warning')[:60]}: {str(msg or '')[:200]}")

    # page count
    page_count = intake_page_count
    if root is not None:
        s, e = root.get("startPageNumber"), root.get("endPageNumber")
        if isinstance(s, int) and isinstance(e, int) and e >= s:
            page_count = e - s + 1

    needs_review = (status != "classified" or any(f["code"] in ("KEY_FIELDS_ABSENT", "TEXT_MISSING", "FIELD_INVALID", "PROVIDER_WARNING",
                                                                    "PROVIDER_SHAPE_UNEXPECTED", "MULTIPLE_CATEGORIES") for f in flags))

    # Provenance is the configuration the analysis was actually requested with (stored with the provider-response artifact).
    # Only when none is supplied (unit use) does it fall back to the current settings.
    prov = provenance or {"api_version": settings.cu_api_version, "classifier_analyzer_id": classifier_id(settings.analyzer_config_version),
                          "config_version": settings.analyzer_config_version, "config_hash": config_hash(settings)}
    v = prov["config_version"]
    cat_obj = R.CATEGORY_BY_NAME.get(category) if category else None
    ext_id = extractor_id(cat_obj.family, v) if (cat_obj and routed) else None
    cls_id = result.get("analyzerId") if isinstance(result.get("analyzerId"), str) else prov["classifier_analyzer_id"]
    simulated = bool(doc.get("_simulated") or result.get("_simulated"))
    document = {
        "schema_version": SCHEMA_VERSION,
        "document_id": document_id,
        "attempt_no": attempt_no,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": {
            "name": PROVIDER_NAME,
            "api_version": result.get("apiVersion") if isinstance(result.get("apiVersion"), str) else prov["api_version"],
            "classifier_analyzer_id": cls_id,
            "extractor_analyzer_id": ext_id,
            "config_version": v,
            "config_hash": prov["config_hash"],
            "operation_id": doc.get("id") if isinstance(doc.get("id"), str) else None,
            "simulated": simulated,
        },
        "classification": {"category": category, "provider_category": provider_cat, "status": status, "confidence": None},
        "text": {"artifact_id": text_artifact_id, "status": "present" if text else "missing",
                 "char_count": len(text) if text else 0, "page_count": page_count},
        "raw_artifact_id": raw_artifact_id,
        "fields": {f.name: {"status": f.status, "type": f.value_type, "value": f.value, "confidence": f.confidence,
                            "source": f.source, "provider_type": f.provider_type} for f in fields},
        "flags": flags,
        "needs_review": needs_review,
    }
    return Normalized(document, fields, text, category, status, provider_cat, needs_review, flags, page_count, simulated,
                      document["provider"]["operation_id"], ext_id, cls_id)
