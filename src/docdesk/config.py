"""Application settings: environment variables only (secrets arrive via Key Vault references or Key Vault reads)."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.stages import DEFAULT_MAX_TRIES


class ConfigError(Exception):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    app_env: Literal["prod", "dev"] = "prod"
    log_level: str = "INFO"

    # SQL
    sql_server: str = ""
    sql_database: str = ""
    sql_auth: Literal["managed_identity", "sql_password"] = "managed_identity"  # sql_password is dev-only
    sql_user: str = ""
    sql_password: str = ""
    sql_trust_server_certificate: bool = False  # dev-only (local SQL Server has a self-signed certificate)
    azure_client_id: str = ""  # user-assigned managed identity

    # Storage / Service Bus
    storage_account_url: str = ""
    servicebus_namespace: str = ""
    originals_container: str = "originals"
    artifacts_container: str = "artifacts"
    queue_prefix: str = "docdesk-"

    # Content Understanding
    cu_endpoint: str = ""
    cu_auth: Literal["managed_identity", "none"] = "managed_identity"  # none is dev-only (simulated server)
    cu_api_version: str = "2025-11-01"
    analyzer_config_version: str = "1"
    cu_completion_model: str = "gpt-4.1"
    cu_embedding_model: str = "text-embedding-3-large"
    cu_model_deployments: str = ""  # JSON {model-or-alias: deployment-name}; used by setup-analyzers --set-defaults
    cu_poll_timeout_s: int = 600
    cu_max_concurrency: int = 4
    cu_return_details: bool = False
    cu_estimate_source_confidence: bool = True

    # Operator sign-in
    operator_password: str = ""
    key_vault_url: str = ""
    operator_password_secret_name: str = ""
    cookie_secure: bool = True
    session_hours: int = 8

    # Limits and behaviour
    max_upload_mb: int = 20
    max_pages: int = 100
    retention_days: int = 2555
    low_confidence_threshold: float = 0.5

    max_tries_intake: int = DEFAULT_MAX_TRIES["intake"]
    max_tries_analyze: int = DEFAULT_MAX_TRIES["analyze"]
    max_tries_index: int = DEFAULT_MAX_TRIES["index"]
    max_tries_result: int = DEFAULT_MAX_TRIES["result"]
    lease_seconds: int = 300
    lease_seconds_analyze: int = 600
    arrival_grace_s: int = 300
    upload_timeout_s: int = 900
    stage_recovery_s: int = 900
    dispatch_poll_s: float = 1.0
    reaper_poll_s: float = 30.0
    health_poll_s: float = 60.0
    worker_conc_arrival: int = 2
    worker_conc_intake: int = 4
    worker_conc_analyze: int = 4
    worker_conc_index: int = 4
    worker_conc_result: int = 2

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def max_tries(self, stage: str) -> int:
        return getattr(self, f"max_tries_{stage}")

    def lease_for(self, stage: str) -> int:
        return self.lease_seconds_analyze if stage == "analyze" else self.lease_seconds

    def conc(self, name: str) -> int:
        return getattr(self, f"worker_conc_{name}")

    def deployments(self) -> dict[str, str]:
        if not self.cu_model_deployments.strip():
            return {}
        try:
            data = json.loads(self.cu_model_deployments)
        except ValueError as e:
            raise ConfigError("CU_MODEL_DEPLOYMENTS is not valid JSON") from e
        if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            raise ConfigError("CU_MODEL_DEPLOYMENTS must be a JSON object of strings")
        return data

    @model_validator(mode="after")
    def _dev_only_guard(self) -> "Settings":
        if self.app_env == "prod":
            bad = []
            if self.sql_auth != "managed_identity":
                bad.append("SQL_AUTH")
            if self.cu_auth != "managed_identity":
                bad.append("CU_AUTH")
            if self.sql_trust_server_certificate:
                bad.append("SQL_TRUST_SERVER_CERTIFICATE")
            if not self.cookie_secure:
                bad.append("COOKIE_SECURE")
            if bad:
                raise ValueError(f"{', '.join(bad)} may only be relaxed when APP_ENV=dev")
        return self

    def require(self, *names: str) -> None:
        """Raise ConfigError (secret-free) naming every missing required setting."""
        missing = [n.upper() for n in names if not getattr(self, n)]
        if missing:
            raise ConfigError(f"missing required settings: {', '.join(missing)}")


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as e:  # report names and reasons only, never the offending values
        raise ConfigError("; ".join(f"{'.'.join(map(str, x['loc'])) or 'settings'}: {x['msg']}" for x in e.errors())) from None
