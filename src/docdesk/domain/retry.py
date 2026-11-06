from __future__ import annotations

import random


def backoff_seconds(try_no: int, *, retry_after_s: float | None = None, rng: random.Random | None = None) -> int:
    """min(300, 5 * 2**(try-1)) +/- 20% jitter; provider Retry-After is a floor."""
    r = rng or random
    base = min(300.0, 5.0 * (2 ** max(0, try_no - 1)))
    delay = base * (1 + r.uniform(-0.2, 0.2))
    if retry_after_s is not None:
        delay = max(delay, retry_after_s)
    return max(1, int(round(delay)))
