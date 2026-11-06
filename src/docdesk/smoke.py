"""`docdesk smoke`: upload documents through the deployed API and report what the real pipeline produced.

This is the Azure smoke test. It passes only when every valid document reaches `completed` with a result that is NOT
marked simulated; it prints observations and never asserts a particular classification (results are model-dependent)."""
from __future__ import annotations

import time
from pathlib import Path

import httpx

TERMINAL = {"completed", "failed", "rejected", "upload_failed"}
DEFAULT_FILES = ["invoice_northwind.pdf", "nda_fabrikam.pdf", "other_bake_sale_flyer.pdf", "invalid_text_renamed.pdf"]


def run_smoke(base_url: str, password: str, docs_dir: Path, files: list[str] | None = None, timeout_s: int = 600, out=print) -> int:
    base = base_url.rstrip("/")
    ok = True
    with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {password}"}, timeout=60.0) as c:
        r = c.get("/healthz")
        out(f"healthz: {r.status_code}")
        r = c.get("/api/documents?limit=1")
        out(f"authenticated API: {r.status_code}")
        if r.status_code != 200:
            out("FAIL: the API did not accept the operator password")
            return 1
        anon = httpx.get(base + "/api/documents", timeout=30.0)
        out(f"anonymous API access refused: {anon.status_code == 401}")
        ok &= anon.status_code == 401
        pending: dict[str, str] = {}
        for name in files or DEFAULT_FILES:
            p = docs_dir / name
            if not p.is_file():
                out(f"SKIP {name}: not found in {docs_dir}")
                continue
            expect_reject = name.startswith(("invalid", "empty", "unsupported", "corrupt", "oversized"))
            with p.open("rb") as f:
                resp = c.post("/api/documents", files={"file": (name, f, "application/octet-stream")})
            if resp.status_code == 202:
                if expect_reject:
                    out(f"NOTE {name}: accepted at upload; intake is expected to reject it")
                pending[resp.json()["document_id"]] = name
                out(f"uploaded {name} -> {resp.json()['document_id']}")
            else:
                err = (resp.json().get("error") or {}) if resp.headers.get("content-type", "").startswith("application/json") else {}
                good = expect_reject and 400 <= resp.status_code < 500
                out(f"{'ok  ' if good else 'FAIL'} {name}: rejected at upload with {resp.status_code} {err.get('code', '')}")
                ok &= good
        deadline = time.monotonic() + timeout_s
        done: dict[str, dict] = {}
        while pending and time.monotonic() < deadline:
            for did in list(pending):
                d = c.get(f"/api/documents/{did}").json()
                if d["status"] in TERMINAL:
                    done[did] = d
                    del pending[did]
            if pending:
                time.sleep(3)
        for did, name in pending.items():
            out(f"FAIL {name}: still not terminal after {timeout_s}s")
            ok = False
        for did, d in done.items():
            fname = d["original_filename"]
            res = d.get("result")
            expect_reject = fname.startswith(("invalid", "empty", "unsupported", "corrupt", "oversized"))
            out(f"--- {fname}: status={d['status']} attempts={len(d['attempts'])}")
            if res:
                present = [f["name"] for f in res["fields"] if f["status"] == "present"]
                absent = [f["name"] for f in res["fields"] if f["status"] != "present"]
                raw = next((a for a in d["artifacts"] if a["kind"] == "provider_response"), None)
                out(f"    category={res['category']} ({res['classification_status']}) provider={res['provider']['name']} api={res['provider']['api_version']}")
                out(f"    analyzers={res['provider']['classifier_analyzer_id']} -> {res['provider']['extractor_analyzer_id']} config=v{res['provider']['config_version']}")
                out(f"    fields present ({len(present)}): {', '.join(present) or '-'}")
                out(f"    fields not found ({len(absent)}): {', '.join(absent) or '-'}")
                out(f"    raw provider response artifact: {raw['size_bytes'] if raw else 'MISSING'} bytes; simulated={res['provider']['simulated']}")
                if res["provider"]["simulated"] or raw is None:
                    out("    FAIL: result is simulated or the provider response was not retained")
                    ok = False
            for a in d["attempts"]:
                for s in a["stages"]:
                    out(f"    attempt {a['attempt_no']} {s['stage']:<8} {s['status']:<10} tries={s['tries']} {s.get('last_error_code') or ''}")
            if expect_reject:
                if d["status"] != "rejected":
                    out("    FAIL: expected this invalid input to be rejected")
                    ok = False
            elif d["status"] != "completed":
                out(f"    FAIL: expected completed, got {d['status']} ({d.get('last_error_code')})")
                ok = False
    out("SMOKE " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def run_direct_cu(docs_dir: Path, out=print) -> int:
    """Call the classifier analyzer directly (no pipeline) to prove the CU endpoint, role and analyzers independently."""
    import uuid

    from .config import get_settings
    from .cu.analyzer_config import classifier_id
    from .cu.client import CuClient
    from .domain.errors import StageError

    s = get_settings()
    s.require("cu_endpoint")
    path = docs_dir / "invoice_northwind.pdf"
    client = CuClient(s)
    try:
        outcome = client.analyze(classifier_id(s.analyzer_config_version), path.read_bytes(), "application/pdf", str(uuid.uuid4()))
    except StageError as e:
        out(f"FAIL direct Content Understanding call: {e.code} {e.message}")
        return 1
    import json

    doc = json.loads(outcome.raw)
    cats = [c.get("category") for c in doc["result"]["contents"] if c.get("category")]
    out(f"direct CU call ok: operation={outcome.operation_id} categories={cats} response_bytes={len(outcome.raw)} simulated={bool(doc.get('_simulated'))}")
    return 0
