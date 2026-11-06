"""Azure smoke test as a pytest. SKIPPED unless DOCDESK_LIVE=1 and a deployed environment is supplied:
    DOCDESK_LIVE=1 LIVE_BASE_URL=https://<web fqdn> LIVE_OPERATOR_PASSWORD=... pytest tests/live -s
Equivalent to `docdesk smoke`. It asserts only that the real pipeline completed; it never asserts a classification."""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live():
    if os.environ.get("DOCDESK_LIVE") != "1":
        pytest.skip("DOCDESK_LIVE != 1: no live Azure environment was exercised")
    base, pw = os.environ.get("LIVE_BASE_URL"), os.environ.get("LIVE_OPERATOR_PASSWORD")
    if not base or not pw:
        pytest.skip("LIVE_BASE_URL / LIVE_OPERATOR_PASSWORD not set")
    return base, pw


def test_live_smoke(live, testdocs):
    from docdesk.smoke import run_smoke
    lines = []
    rc = run_smoke(live[0], live[1], Path(testdocs), timeout_s=900, out=lambda s: (lines.append(s), print(s)))
    assert rc == 0, "\n".join(lines)
