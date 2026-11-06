"""Operator password source: an environment value (Container Apps Key Vault secret reference) or a direct Key Vault read."""
from __future__ import annotations

import threading
import time
from typing import Callable

from ..config import ConfigError, Settings


class PasswordProvider:
    def __init__(self, settings: Settings, fetch: Callable[[], str] | None = None, ttl_s: float = 300.0):
        self._env = settings.operator_password
        self._ttl = ttl_s
        self._cached: tuple[str, float] | None = None
        self._lock = threading.Lock()
        self._fetch = fetch
        if not self._env and not (settings.key_vault_url and settings.operator_password_secret_name):
            raise ConfigError("no operator password source: set OPERATOR_PASSWORD or KEY_VAULT_URL + OPERATOR_PASSWORD_SECRET_NAME")
        if not self._env and self._fetch is None:
            self._fetch = self._key_vault_fetch(settings)
        self.get()  # fail at startup, not on first login

    @staticmethod
    def _key_vault_fetch(settings: Settings) -> Callable[[], str]:
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
        from azure.keyvault.secrets import SecretClient

        cid = settings.azure_client_id or None
        cred = ManagedIdentityCredential(client_id=cid) if cid and settings.app_env == "prod" else DefaultAzureCredential(managed_identity_client_id=cid)
        client = SecretClient(settings.key_vault_url, cred)
        name = settings.operator_password_secret_name
        return lambda: client.get_secret(name).value or ""

    def get(self) -> str:
        if self._env:
            return self._env
        with self._lock:
            now = time.monotonic()
            if self._cached and now - self._cached[1] < self._ttl:
                return self._cached[0]
            try:
                value = self._fetch()  # type: ignore[misc]
            except Exception as e:
                if self._cached:  # keep serving the last good value through a Key Vault blip
                    return self._cached[0]
                raise ConfigError(f"operator password could not be read from Key Vault ({type(e).__name__})") from None
            if not value:
                raise ConfigError("operator password secret is empty")
            self._cached = (value, now)
            return value
