"""Blob storage port plus the Azure Blob adapter (managed identity). A filesystem fake lives in devtools/."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import BinaryIO, Iterator, Protocol

from ..config import Settings


class BlobExists(Exception):
    pass


class BlobNotFound(Exception):
    pass


class BlobUnavailable(Exception):
    """Transient storage failure; the caller decides whether it is retryable."""


@dataclass(frozen=True)
class BlobInfo:
    size: int
    etag: str | None
    content_type: str | None


class BlobStore(Protocol):
    def put(self, container: str, name: str, data: bytes | BinaryIO, content_type: str, overwrite: bool = False) -> None: ...
    def get(self, container: str, name: str) -> bytes: ...
    def stream(self, container: str, name: str, chunk_size: int = 1 << 20) -> Iterator[bytes]: ...
    def head(self, container: str, name: str) -> BlobInfo: ...
    def exists(self, container: str, name: str) -> bool: ...
    def ensure_container(self, container: str) -> None: ...


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}


def _as_unavailable(e: Exception) -> "BlobUnavailable | None":
    """Transient SDK failures (transport errors, and HTTP 408/429/5xx such as 503 ServerBusy once the SDK's own retries are
    exhausted) become BlobUnavailable, which the pipeline treats as retryable storage trouble; anything else is left alone."""
    from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError

    if isinstance(e, (ServiceRequestError, ServiceResponseError)):
        return BlobUnavailable(type(e).__name__)
    if isinstance(e, HttpResponseError) and getattr(e, "status_code", None) in TRANSIENT_HTTP:
        return BlobUnavailable(f"HTTP {e.status_code}")
    return None


class AzureBlobStore:
    def __init__(self, settings: Settings, credential=None, service=None):
        from azure.storage.blob import BlobServiceClient

        if service is not None:  # injected (tests against an emulator)
            self._svc = service
        else:
            if credential is None:
                from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

                cid = settings.azure_client_id or None
                credential = (ManagedIdentityCredential(client_id=cid) if cid and settings.app_env == "prod"
                              else DefaultAzureCredential(managed_identity_client_id=cid))
            self._svc = BlobServiceClient(account_url=settings.storage_account_url, credential=credential)

    def _blob(self, container: str, name: str):
        return self._svc.get_blob_client(container, name)

    def put(self, container, name, data, content_type, overwrite=False):
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings

        try:
            self._blob(container, name).upload_blob(data, overwrite=overwrite, content_settings=ContentSettings(content_type=content_type))
        except ResourceExistsError as e:
            raise BlobExists(name) from e
        except Exception as e:
            u = _as_unavailable(e)
            if u is None:
                raise
            raise u from e

    def get(self, container, name) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self._blob(container, name).download_blob().readall()
        except ResourceNotFoundError as e:
            raise BlobNotFound(name) from e
        except Exception as e:
            u = _as_unavailable(e)
            if u is None:
                raise
            raise u from e

    def stream(self, container, name, chunk_size=1 << 20):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            dl = self._blob(container, name).download_blob()
        except ResourceNotFoundError as e:
            raise BlobNotFound(name) from e
        except Exception as e:
            u = _as_unavailable(e)
            if u is None:
                raise
            raise u from e
        yield from dl.chunks()

    def head(self, container, name) -> BlobInfo:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            p = self._blob(container, name).get_blob_properties()
        except ResourceNotFoundError as e:
            raise BlobNotFound(name) from e
        except Exception as e:
            u = _as_unavailable(e)
            if u is None:
                raise
            raise u from e
        return BlobInfo(p.size, p.etag.strip('"') if p.etag else None, p.content_settings.content_type)

    def exists(self, container, name) -> bool:
        try:
            self.head(container, name)
            return True
        except BlobNotFound:
            return False

    def ensure_container(self, container: str) -> None:
        from azure.core.exceptions import ResourceExistsError

        try:
            self._svc.create_container(container)
        except ResourceExistsError:
            pass
