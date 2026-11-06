"""Builds the real (Azure) runtime from settings. Tests and the dev stack construct `Runtime` with fakes instead."""
from __future__ import annotations

from ..adapters.blobstore import AzureBlobStore
from ..adapters.broker import AzureServiceBusBroker
from ..config import Settings
from ..cu.client import CuClient
from ..db.connection import Database
from .runtime import Runtime


def build_runtime(settings: Settings) -> Runtime:
    settings.require("sql_server", "sql_database", "storage_account_url", "servicebus_namespace", "cu_endpoint")
    return Runtime(settings=settings, db=Database(settings), blob=AzureBlobStore(settings), broker=AzureServiceBusBroker(settings),
                   cu=CuClient(settings))
