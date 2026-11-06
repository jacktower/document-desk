"""The Azure smoke checker must not report success without real evidence (tested with a stub HTTP server; no network)."""
import httpx
import pytest

from docdesk.smoke import run_smoke


def fake_api(results: dict[str, dict]):
    """results: filename -> terminal document JSON (without id fields) the stub serves after upload."""
    docs, posts = {}, []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if "authorization" not in req.headers and req.url.path.startswith("/api/"):
            return httpx.Response(401, json={"error": {"code": "UNAUTHENTICATED"}})
        if req.method == "GET" and req.url.path == "/api/documents":
            return httpx.Response(200, json={"items": []})
        if req.method == "POST" and req.url.path == "/api/documents":
            name = req.content.split(b'filename="')[1].split(b'"')[0].decode()
            did = f"id-{len(posts)}"
            posts.append(name)
            docs[did] = {"original_filename": name, **results[name]}
            return httpx.Response(202, json={"document_id": did, "status": "uploading"})
        if req.method == "GET" and req.url.path.startswith("/api/documents/"):
            return httpx.Response(200, json=docs[req.url.path.rsplit("/", 1)[1]])
        return httpx.Response(404)
    return httpx.MockTransport(handler), posts


def completed(simulated=False, with_result=True, with_raw=True):
    res = {"category": "invoice", "classification_status": "classified", "fields": [{"name": "total_amount", "status": "present"}],
           "provider": {"name": "azure-content-understanding", "api_version": "2025-11-01", "classifier_analyzer_id": "c",
                        "extractor_analyzer_id": "e", "config_version": "1", "simulated": simulated}}
    return {"status": "completed", "attempts": [{"attempt_no": 1, "stages": []}], "result": res if with_result else None,
            "artifacts": [{"kind": "provider_response", "size_bytes": 10}] if with_raw else []}


REJECTED = {"status": "rejected", "attempts": [{"attempt_no": 1, "stages": []}], "result": None, "artifacts": []}


@pytest.fixture
def docs(tmp_path):
    for n in ("invoice_a.pdf", "nda_b.pdf", "corrupt_c.pdf"):
        (tmp_path / n).write_bytes(b"%PDF-1.4 x")
    return tmp_path


def run(docs, results, files):
    transport, posts = fake_api(results)
    lines = []
    rc = run_smoke("https://x.invalid", "pw", docs, files, timeout_s=5, out=lines.append, transport=transport)
    return rc, lines, posts


FILES = ["invoice_a.pdf", "nda_b.pdf", "corrupt_c.pdf"]


def test_passes_with_two_real_results_and_a_rejected_invalid_file(docs):
    rc, lines, _ = run(docs, {"invoice_a.pdf": completed(), "nda_b.pdf": completed(), "corrupt_c.pdf": REJECTED}, FILES)
    assert rc == 0 and lines[-1] == "SMOKE PASSED"


def test_empty_document_directory_fails_without_any_upload(tmp_path):
    rc, lines, posts = run(tmp_path, {}, FILES)
    assert rc == 1 and posts == [] and lines[-1] == "SMOKE FAILED" and sum("required test file not found" in l for l in lines) == 3


def test_completed_document_without_a_result_fails(docs):
    rc, lines, _ = run(docs, {"invoice_a.pdf": completed(with_result=False), "nda_b.pdf": completed(), "corrupt_c.pdf": REJECTED}, FILES)
    assert rc == 1 and any("completed but has no result" in l for l in lines)


@pytest.mark.parametrize("bad", [completed(simulated=True), completed(with_raw=False)])
def test_simulated_or_unretained_result_fails(docs, bad):
    rc, _, _ = run(docs, {"invoice_a.pdf": bad, "nda_b.pdf": completed(), "corrupt_c.pdf": REJECTED}, FILES)
    assert rc == 1


def test_a_single_valid_document_is_not_enough(docs):
    rc, lines, _ = run(docs, {"invoice_a.pdf": completed(), "corrupt_c.pdf": REJECTED}, ["invoice_a.pdf", "corrupt_c.pdf"])
    assert rc == 1 and any("at least 2 are required" in l for l in lines)


def test_invalid_file_that_is_not_rejected_fails(docs):
    rc, _, _ = run(docs, {"invoice_a.pdf": completed(), "nda_b.pdf": completed(), "corrupt_c.pdf": completed()}, FILES)
    assert rc == 1
