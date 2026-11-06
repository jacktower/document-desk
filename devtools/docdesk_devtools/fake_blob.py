"""Filesystem blob store with the same behaviour the pipeline needs from Azure Blob (create-only puts, head, stream)."""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path
from typing import Iterator

from docdesk.adapters.blobstore import BlobExists, BlobInfo, BlobNotFound, BlobUnavailable


class FsBlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.fail_puts = 0          # raise BlobUnavailable on the next N puts (nothing stored)
        self.lose_ack_puts = 0      # store the blob, then raise BlobUnavailable (committed write, lost acknowledgement)
        self.down = False
        self._lock = threading.Lock()

    def _path(self, container: str, name: str) -> Path:
        p = (self.root / container / name).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError("path escape")
        return p

    def ensure_container(self, container: str) -> None:
        (self.root / container).mkdir(parents=True, exist_ok=True)

    def put(self, container, name, data, content_type, overwrite=False):
        with self._lock:
            if self.down:
                raise BlobUnavailable("down")
            if self.fail_puts > 0:
                self.fail_puts -= 1
                raise BlobUnavailable("injected")
            lose_ack = self.lose_ack_puts > 0
            if lose_ack:
                self.lose_ack_puts -= 1
        p = self._path(container, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = data if isinstance(data, bytes) else data.read()
        if not overwrite and p.exists():
            raise BlobExists(name)
        fd, tmp = tempfile.mkstemp(dir=p.parent)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        try:
            if overwrite:
                os.replace(tmp, p)
            else:
                os.link(tmp, p)  # atomic create-if-absent
                os.unlink(tmp)
        except FileExistsError:
            os.unlink(tmp)
            raise BlobExists(name) from None
        (p.parent / (p.name + ".ctype")).write_text(content_type)
        if lose_ack:
            raise BlobUnavailable("ack lost")

    def get(self, container, name) -> bytes:
        if self.down:
            raise BlobUnavailable("down")
        p = self._path(container, name)
        if not p.is_file():
            raise BlobNotFound(name)
        return p.read_bytes()

    def stream(self, container, name, chunk_size=1 << 20) -> Iterator[bytes]:
        p = self._path(container, name)
        if not p.is_file():
            raise BlobNotFound(name)
        with p.open("rb") as f:
            while chunk := f.read(chunk_size):
                yield chunk

    def head(self, container, name) -> BlobInfo:
        if self.down:
            raise BlobUnavailable("down")
        p = self._path(container, name)
        if not p.is_file():
            raise BlobNotFound(name)
        ct = p.parent / (p.name + ".ctype")
        return BlobInfo(p.stat().st_size, hashlib.md5(p.read_bytes()).hexdigest(), ct.read_text() if ct.exists() else None)

    def exists(self, container, name) -> bool:
        try:
            self.head(container, name)
            return True
        except BlobNotFound:
            return False

    def delete(self, container: str, name: str) -> None:  # test helper (simulates a lost artifact)
        self._path(container, name).unlink()
