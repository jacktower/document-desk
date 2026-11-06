"""`docdesk setup-analyzers`: model defaults first, analyzers second (K-004), safe to repeat (K-015)."""
from __future__ import annotations

from typing import Callable

from ..config import ConfigError, Settings
from . import analyzer_config as AC
from .client import CuClient, CuSetupError

EMBEDDING_ALIAS = "prebuilt-analyzer-embedding"


READY = {"ready", "succeeded"}
IN_PROGRESS = {"creating", "notstarted", "running", "inprogress"}
READY_POLLS = 30  # x the client's poll interval (2 s) = about a minute


def _status(analyzer: dict) -> str:
    """The service reports `status`; a response without one is treated as ready (as the worker's health probe does)."""
    return str(analyzer.get("status", "ready")).lower()


def required_defaults(s: Settings) -> list[str]:
    return [s.cu_completion_model, EMBEDDING_ALIAS]


def _deployments(defaults: dict | None) -> dict:
    if not defaults:
        return {}
    return defaults.get("modelDeployments", defaults) or {}


def setup_analyzers(client: CuClient, s: Settings, *, check: bool = False, set_defaults: bool = False,
                    out: Callable[[str], None] = print) -> int:
    """Returns a process exit code: 0 ok, 1 analyzer problem / missing in --check, 2 prerequisite (defaults) missing."""
    AC.validate_all(s)  # fail on a locally detectable definition error before touching the service

    # ---- phase 1: model defaults (no analyzer request may be made before this passes) ---------------
    try:
        current = _deployments(client.get_defaults())
        missing = [k for k in required_defaults(s) if k not in current]
        if missing:
            if check or not set_defaults:
                out(f"PREREQUISITE MISSING: Content Understanding has no default model deployment for: {', '.join(missing)}.")
                out("Analyzers based on prebuilt-document are rejected with DefaultDeploymentModelNotFound until these exist.")
                out(f"Deploy the models in Foundry, set CU_MODEL_DEPLOYMENTS='{{\"<model or alias>\": \"<deployment name>\", ...}}' "
                    f"(it must include {EMBEDDING_ALIAS}) and run: docdesk setup-analyzers --set-defaults")
                return 2
            wanted = s.deployments()
            if EMBEDDING_ALIAS not in wanted:
                out(f"CONFIGURATION: CU_MODEL_DEPLOYMENTS must contain the embedding alias {EMBEDDING_ALIAS}; nothing was changed.")
                return 2
            client.patch_defaults(wanted)
            current = _deployments(client.get_defaults())
            still = [k for k in required_defaults(s) if k not in current]
            if still:
                out(f"PREREQUISITE MISSING: after setting defaults these are still unset: {', '.join(still)} "
                    f"(include them in CU_MODEL_DEPLOYMENTS).")
                return 2
            out("defaults: set and confirmed")
        else:
            out("defaults: present")

        # ---- phase 2: analyzers, in dependency order (extractors, then the classifier that routes to them) ---------
        rc = 0
        for aid, definition in AC.all_definitions(s).items():
            existing = client.get_analyzer(aid)
            if existing is None:
                if check:
                    out(f"analyzer {aid}: MISSING")
                    rc = 1
                    continue
                client.put_analyzer(aid, definition, allow_replace=False)
                out(f"analyzer {aid}: registered and ready")
            elif AC.equal_ignoring_empty(definition, existing):
                status = _status(existing)
                if status in IN_PROGRESS and not check:  # an interrupted or slow registration: wait a bounded time
                    for _ in range(READY_POLLS):
                        client.pause()
                        status = _status(client.get_analyzer(aid) or {})
                        if status not in IN_PROGRESS:
                            break
                if status in READY:
                    out(f"analyzer {aid}: skipped (already registered, matches and is ready)")
                else:
                    out(f"analyzer {aid}: matches the authored definition but is NOT READY (status: {status}). "
                        + ("Still being created; run again shortly." if status in IN_PROGRESS else
                           "It cannot be repaired under this id: delete it in the Foundry resource, or bump ANALYZER_CONFIG_VERSION."))
                    rc = 1
            else:
                out(f"analyzer {aid}: DIFFERS from the authored definition. An analyzer under an existing id is never changed; "
                    f"bump ANALYZER_CONFIG_VERSION to register a new set.")
                rc = 1
        return rc
    except CuSetupError as e:
        out(f"FAILED: {e}")
        return 1
    except ConfigError as e:
        out(f"CONFIGURATION: {e}")
        return 2
