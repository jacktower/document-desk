import io
import json
import logging
import random

import pytest

from docdesk.config import ConfigError, Settings, get_settings
from docdesk.domain import errors as E
from docdesk.domain.logs import JsonFormatter, log, setup_logging
from docdesk.domain.retry import backoff_seconds
from docdesk.pipeline.messages import BadMessage, parse_blob_events, parse_stage_message
from docdesk.web.uploads import clean_filename, validate_header
from docdesk.domain.errors import UploadRejection
from docdesk_devtools.events import blob_created_event

DOC = "3f2b8f5e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


def test_backoff_is_bounded_exponential_with_jitter_and_retry_after_floor():
    rng = random.Random(1)
    for n, base in [(1, 5), (2, 10), (3, 20), (6, 160), (7, 300), (12, 300)]:
        for _ in range(50):
            d = backoff_seconds(n, rng=rng)
            assert base * 0.79 <= d <= base * 1.21 + 1
    assert backoff_seconds(1, retry_after_s=90) == 90


def test_error_catalog_retry_policy():
    assert E.StageError("PROVIDER_THROTTLED").retryable and E.StageError("PROVIDER_UNAVAILABLE").retryable
    assert E.StageError("PROVIDER_TIMEOUT").retryable and E.StageError("PROVIDER_RESPONSE_MALFORMED").retryable
    for code in ("PROVIDER_REQUEST_REJECTED", "PROVIDER_CONFIGURATION", "PROVIDER_RESULT_UNUSABLE", "INPUT_UNSUPPORTED", "CRASH_LOOP_EXHAUSTED"):
        assert not E.StageError(code).retryable
    for code in ("INPUT_UNSUPPORTED", "INPUT_CORRUPT", "INPUT_TOO_LARGE", "INPUT_TOO_MANY_PAGES"):
        assert E.StageError(code).rejects_document
    assert not E.StageError("PROVIDER_THROTTLED").rejects_document
    assert E.StageError("PROVIDER_OPERATION_FAILED", retryable=True).retryable          # inner-code override


def test_blob_event_shapes():
    for kw in ({}, {"as_array": True}, {"cloudevents": True}, {"cloudevents": True, "as_array": True}):
        evs, ignored = parse_blob_events(blob_created_event("originals", f"{DOC}/source.pdf", etag='"0x1"', size=10, **kw))
        assert ignored == 0 and len(evs) == 1
        assert evs[0].document_id == DOC and evs[0].container == "originals" and evs[0].etag == "0x1" and evs[0].size == 10


def test_blob_events_ignored_and_bad():
    evs, ignored = parse_blob_events(blob_created_event("originals", "not-a-guid/source.pdf"))
    assert evs == [] and ignored == 1
    evs, ignored = parse_blob_events(json.dumps({"eventType": "Microsoft.Storage.BlobDeleted", "subject": f"/containers/originals/blobs/{DOC}/s.pdf"}))
    assert evs == [] and ignored == 1
    with pytest.raises(BadMessage):
        parse_blob_events("not json")


def test_stage_message_parse():
    m = parse_stage_message(json.dumps({"v": 1, "document_id": DOC.upper(), "attempt_id": DOC, "stage": "analyze"}))
    assert m.stage == "analyze" and m.document_id == DOC
    for bad in ("x", "{}", json.dumps({"v": 1, "document_id": "zz", "attempt_id": DOC, "stage": "analyze"}),
                json.dumps({"v": 1, "document_id": DOC, "attempt_id": DOC, "stage": "bogus"}), json.dumps({"v": 2})):
        with pytest.raises(BadMessage):
            parse_stage_message(bad)


@pytest.mark.parametrize("name,head,size,code,status", [
    ("a.docx", b"PK", 10, "UNSUPPORTED_TYPE", 415), ("a.pdf", b"", 0, "EMPTY_FILE", 400),
    ("a.pdf", b"%PDF-1.7", 21 * 1024 * 1024, "TOO_LARGE", 413), ("a.pdf", b"hello world", 11, "CORRUPT_HEADER", 400),
    ("a.tif", b"%PDF-1.7", 10, "CORRUPT_HEADER", 400), ("noext", b"%PDF-", 5, "UNSUPPORTED_TYPE", 415),
])
def test_upload_validation_errors(name, head, size, code, status):
    with pytest.raises(UploadRejection) as e:
        validate_header(name, head, size, 20 * 1024 * 1024)
    assert (e.value.code, e.value.status) == (code, status)


def test_upload_validation_accepts_pdf_tiff():
    assert validate_header("A.PDF", b"%PDF-1.4", 10, 1000) == "application/pdf"
    assert validate_header("x.tif", b"II*\x00....", 10, 1000) == "image/tiff"
    assert validate_header("x.TIFF", b"MM\x00*....", 10, 1000) == "image/tiff"
    assert clean_filename("..\\..\\evil/../name\x00.pdf") == "name.pdf"


def test_prod_rejects_dev_only_relaxations(monkeypatch):
    for k, v in [("SQL_AUTH", "sql_password"), ("CU_AUTH", "none"), ("SQL_TRUST_SERVER_CERTIFICATE", "true"), ("COOKIE_SECURE", "false")]:
        with pytest.raises(ValueError):
            Settings(**{k.lower(): v})
        Settings(app_env="dev", **{k.lower(): v})  # allowed in dev


def test_config_error_never_echoes_values(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("SQL_PASSWORD", "s3cret-value")
    monkeypatch.setenv("MAX_UPLOAD_MB", "not-a-number")
    with pytest.raises(ConfigError) as e:
        get_settings()
    assert "s3cret" not in str(e.value) and "not-a-number" not in str(e.value) and "max_upload_mb" in str(e.value)
    get_settings.cache_clear()


def test_deployments_parsing_and_required():
    assert Settings(app_env="dev", cu_model_deployments='{"a":"b"}').deployments() == {"a": "b"}
    with pytest.raises(ConfigError):
        Settings(app_env="dev", cu_model_deployments="[1]").deployments()
    with pytest.raises(ConfigError) as e:
        Settings(app_env="dev").require("sql_server", "cu_endpoint")
    assert "SQL_SERVER" in str(e.value)


def test_log_formatter_allow_list_drops_unknown_keys_and_exception_text():
    buf = io.StringIO()
    setup_logging("DEBUG", buf)
    log("something", document_id="d", stage="intake", filename="SECRET.pdf", extracted_text="SECRET text", password="x")
    try:
        raise ValueError("SECRET in exception message")
    except ValueError:
        logging.getLogger("docdesk").error("boom", exc_info=True)
    out = buf.getvalue()
    assert "SECRET" not in out and "password" not in out and '"document_id":"d"' in out and '"error_class":"ValueError"' in out
