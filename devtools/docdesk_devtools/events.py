"""Schema-correct Event Grid `BlobCreated` payloads, used to SIMULATE Event Grid locally."""
from __future__ import annotations

import json
import uuid


def blob_created_event(container: str, blob_name: str, etag: str = "0x8DC0000000000", size: int = 0,
                       content_type: str = "application/pdf", cloudevents: bool = False, as_array: bool = False) -> str:
    subject = f"/blobServices/default/containers/{container}/blobs/{blob_name}"
    data = {"api": "PutBlob", "contentType": content_type, "contentLength": size, "eTag": etag, "blobType": "BlockBlob",
            "url": f"https://example.invalid/{container}/{blob_name}"}
    if cloudevents:
        ev = {"specversion": "1.0", "id": str(uuid.uuid4()), "source": "/subscriptions/x/storageAccounts/y",
              "type": "Microsoft.Storage.BlobCreated", "subject": subject, "data": data}
    else:
        ev = {"id": str(uuid.uuid4()), "topic": "/subscriptions/x/storageAccounts/y", "subject": subject,
              "eventType": "Microsoft.Storage.BlobCreated", "eventTime": "2026-01-01T00:00:00Z", "data": data,
              "dataVersion": "", "metadataVersion": "1"}
    return json.dumps([ev] if as_array else ev)
