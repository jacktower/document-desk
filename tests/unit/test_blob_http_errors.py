"""The real Azure Blob SDK adapter against a local HTTP responder that answers every request with a fixed status.
(This is not Azure Blob Storage; it checks how the adapter translates the SDK's exceptions.)"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from azure.core.exceptions import HttpResponseError
from azure.storage.blob import BlobServiceClient

from docdesk.adapters.blobstore import AzureBlobStore, BlobUnavailable
from docdesk.config import Settings


def serve(status: int):
    class H(BaseHTTPRequestHandler):
        def _reply(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            body = b'<?xml version="1.0"?><Error><Code>ServerBusy</Code><Message>busy</Message></Error>'
            self.send_response(status)
            self.send_header("Content-Type", "application/xml")
            self.send_header("x-ms-error-code", "ServerBusy")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        do_GET = do_PUT = do_HEAD = do_POST = _reply

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def store_for(srv):
    svc = BlobServiceClient(account_url=f"http://127.0.0.1:{srv.server_port}", credential=None, retry_total=0)
    return AzureBlobStore(Settings(app_env="dev"), service=svc)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_http_errors_become_blob_unavailable(status):
    srv = serve(status)
    try:
        s = store_for(srv)
        with pytest.raises(BlobUnavailable):
            s.put("artifacts", "a/b.bin", b"x", "application/octet-stream", overwrite=True)
        with pytest.raises(BlobUnavailable):
            s.get("artifacts", "a/b.bin")
        with pytest.raises(BlobUnavailable):
            s.head("artifacts", "a/b.bin")
        with pytest.raises(BlobUnavailable):
            next(s.stream("artifacts", "a/b.bin"))
    finally:
        srv.shutdown()


def test_non_transient_http_errors_are_not_disguised():
    srv = serve(403)
    try:
        with pytest.raises(HttpResponseError):
            store_for(srv).put("artifacts", "a/b.bin", b"x", "application/octet-stream", overwrite=True)
    finally:
        srv.shutdown()
