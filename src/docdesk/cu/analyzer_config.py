"""Generates the Content Understanding analyzer definitions (API 2025-11-01) from the registry, and validates them locally."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import Settings
from . import registry as R

ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
FAMILY_SUFFIX = {"transaction": "transaction", "statement": "statement", "agreement": "agreement", "tax": "tax", "insurance": "insurance"}


def classifier_id(version: str) -> str:
    return f"docdesk-classifier-v{version}"


def extractor_id(family: str, version: str) -> str:
    return f"docdesk-{FAMILY_SUFFIX[family]}-v{version}"


def _prop_schema(p: R.Prop) -> dict:
    s: dict[str, Any] = {"type": p.type}
    if p.description:
        s["description"] = p.description
    return s


def _field_schema(f: R.FieldDef) -> dict:
    if f.type == "array<string>":
        return {"type": "array", "description": f.description, "items": {"type": "string"}}
    if f.type == "array<object>":
        return {"type": "array", "description": f.description,
                "items": {"type": "object", "properties": {p.name: _prop_schema(p) for p in f.props}}}
    return {"type": f.type, "method": f.method, "description": f.description}


def extractor_definition(family: str, s: Settings) -> dict:
    return {
        "description": f"Document Desk {family} extractor, config v{s.analyzer_config_version}",
        "baseAnalyzerId": "prebuilt-document",
        "models": {"completion": s.cu_completion_model, "embedding": s.cu_embedding_model},
        "config": {
            "returnDetails": s.cu_return_details,
            "estimateFieldSourceAndConfidence": s.cu_estimate_source_confidence,
            "enableOcr": True,
            "enableLayout": True,
        },
        "fieldSchema": {
            "name": family,
            "description": f"Common and {family}-specific fields. Every field is optional; leave a field empty when the document does not state it.",
            "fields": {f.name: _field_schema(f) for f in R.family_fields(family)},
        },
        "tags": {"app": "docdesk", "configVersion": s.analyzer_config_version},
    }


def classifier_definition(s: Settings) -> dict:
    v = s.analyzer_config_version
    cats: dict[str, dict] = {c.name: {"description": c.description, "analyzerId": extractor_id(c.family, v)} for c in R.CATEGORIES}
    cats[R.OTHER] = {"description": R.OTHER_DESCRIPTION}
    return {
        "description": f"Document Desk classifier, config v{v}",
        "baseAnalyzerId": "prebuilt-document",
        "models": {"completion": s.cu_completion_model, "embedding": s.cu_embedding_model},
        "config": {"returnDetails": s.cu_return_details, "enableSegment": False, "contentCategories": cats},
        "tags": {"app": "docdesk", "configVersion": v},
    }


def all_definitions(s: Settings) -> dict[str, dict]:
    """analyzer id -> definition, in registration order: extractors first, the classifier (which routes to them) last."""
    v = s.analyzer_config_version
    defs = {extractor_id(f, v): extractor_definition(f, s) for f in R.FAMILIES}
    defs[classifier_id(v)] = classifier_definition(s)
    return defs


def config_hash(s: Settings) -> str:
    canon = json.dumps({"api": s.cu_api_version, "defs": all_definitions(s)}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()


# --- local pre-validation mirroring the documented request schema. The authoritative validation is the live PUT. ---
class _Cat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(max_length=4096)
    analyzerId: str | None = None

    @field_validator("analyzerId")
    @classmethod
    def _id(cls, v):
        if v is not None and not ID_RE.match(v):
            raise ValueError("bad analyzer id")
        return v


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    returnDetails: bool | None = None
    enableSegment: bool | None = None
    enableOcr: bool | None = None
    enableLayout: bool | None = None
    estimateFieldSourceAndConfidence: bool | None = None
    contentCategories: dict[str, _Cat] | None = Field(default=None, max_length=200)


_TYPES = {"string", "date", "time", "number", "integer", "boolean", "array", "object"}


class _FieldDefn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    method: str | None = None
    description: str | None = Field(default=None, max_length=4096)
    items: dict | None = None
    properties: dict | None = None

    @field_validator("type")
    @classmethod
    def _t(cls, v):
        if v not in _TYPES:
            raise ValueError(f"unknown type {v}")
        return v

    @field_validator("method")
    @classmethod
    def _m(cls, v):
        if v is not None and v not in {"extract", "generate", "classify"}:
            raise ValueError("unknown method")
        return v


class _FieldSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str | None = None
    fields: dict[str, _FieldDefn] = Field(max_length=1000)


class _Analyzer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    baseAnalyzerId: str
    models: dict[str, str]
    config: _Config
    fieldSchema: _FieldSchema | None = None
    tags: dict[str, str] | None = None


def validate_definition(analyzer_id: str, definition: dict) -> None:
    """Raises ValueError describing the first problem."""
    if not ID_RE.match(analyzer_id):
        raise ValueError(f"analyzer id {analyzer_id!r} violates ^[a-zA-Z0-9._-]{{1,64}}$")
    a = _Analyzer.model_validate(definition)
    if a.fieldSchema:
        for name in a.fieldSchema.fields:
            if not FIELD_NAME_RE.match(name):
                raise ValueError(f"bad field name {name}")


def validate_all(s: Settings) -> None:
    defs = all_definitions(s)
    for aid, d in defs.items():
        validate_definition(aid, d)
    cats = defs[classifier_id(s.analyzer_config_version)]["config"]["contentCategories"]
    if R.OTHER not in cats:
        raise ValueError("classifier lacks the `other` category")
    for name, c in cats.items():
        if c.get("analyzerId") and c["analyzerId"] not in defs:
            raise ValueError(f"category {name} routes to unknown analyzer {c['analyzerId']}")


def equal_ignoring_empty(authored: Any, actual: Any) -> bool:
    """K-005: the service omits authored empty objects on GET, so an authored empty object equals its absence.
    The service also returns server-populated keys we never authored; only authored keys are compared."""
    if isinstance(authored, dict):
        if not isinstance(actual, dict):
            return authored == {} and actual is None
        for k, v in authored.items():
            if k not in actual:
                if v in ({}, [], None):
                    continue
                return False
            if not equal_ignoring_empty(v, actual[k]):
                return False
        return True
    if isinstance(authored, list):
        return isinstance(actual, list) and len(authored) == len(actual) and all(
            equal_ignoring_empty(a, b) for a, b in zip(authored, actual))
    return authored == actual
