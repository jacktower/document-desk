"""The real azure-storage-blob SDK adapter against the Azurite EMULATOR (set AZURITE_BLOB_URL to enable).
Managed-identity authentication is not exercised by the emulator and remains unverified until a real storage account is used."""
import os
import uuid

import pytest

from docdesk.adapters.blobstore import AzureBlobStore, BlobExists, BlobNotFound, sha256_hex
from docdesk.config import Settings

URL = os.environ.get("AZURITE_BLOB_URL", "")
# Azurite's published, well-known development account (not a secret)
DEV_CONN = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
             f"BlobEndpoint={URL}/devstoreaccount1;")


@pytest.fixture
def store():
    if not URL:
        pytest.skip("AZURITE_BLOB_URL not set: Azure Blob adapter tests against the emulator skipped")
    from azure.storage.blob import BlobServiceClient
    svc = BlobServiceClient.from_connection_string(DEV_CONN)
    s = AzureBlobStore(Settings(app_env="dev"), service=svc)
    s.ensure_container("originals")
    s.ensure_container("originals")      # idempotent
    return s


def test_put_create_only_get_head_stream_and_missing(store):
    name = f"{uuid.uuid4()}/source.pdf"
    data = b"%PDF-1.4 emulator " + os.urandom(2048)
    store.put("originals", name, data, "application/pdf", overwrite=False)
    with pytest.raises(BlobExists):
        store.put("originals", name, b"other", "application/pdf", overwrite=False)       # create-only semantics
    assert store.get("originals", name) == data
    info = store.head("originals", name)
    assert info.size == len(data) and info.content_type == "application/pdf" and info.etag
    assert b"".join(store.stream("originals", name)) == data and sha256_hex(store.get("originals", name)) == sha256_hex(data)
    assert store.exists("originals", name) and not store.exists("originals", name + ".nope")
    with pytest.raises(BlobNotFound):
        store.get("originals", name + ".nope")
    with pytest.raises(BlobNotFound):
        next(store.stream("originals", name + ".nope"))
    store.put("originals", name, b"replaced", "text/plain", overwrite=True)
    assert store.get("originals", name) == b"replaced"
