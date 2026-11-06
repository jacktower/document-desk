from __future__ import annotations

from typing import Callable

from ...db.repo_pipeline import Claim
from ..runtime import Runtime
from . import analyze, index, intake, result

# A handler does its work outside any SQL transaction and returns the `effects` function that the runner executes
# inside the fenced completion transaction (or None when the stage has no effects of its own).
HANDLERS: dict[str, Callable[[Runtime, Claim], Callable | None]] = {
    "intake": intake.run,
    "analyze": analyze.run,
    "index": index.run,
    "result": result.run,
}
