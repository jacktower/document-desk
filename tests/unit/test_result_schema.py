import json
from pathlib import Path

import jsonschema
import pytest

from docdesk.config import Settings
from docdesk.cu.normalize import normalize
from test_normalize import op, root, routed

SCHEMA = json.loads((Path(__file__).resolve().parents[2] / "src/docdesk/schemas/result-1.schema.json").read_text())
S = Settings(app_env="dev")


@pytest.mark.parametrize("contents", [
    [root(), routed("invoice", {"total_amount": {"type": "number", "valueNumber": 3}, "due_date": {"type": "date", "valueDate": "bad"}})],
    [root(category="other")], [root()], [root(), routed("zzz", {})],
    [root(), routed("sow", {"milestones": {"type": "array", "valueArray": [{"type": "object", "valueObject": {"description": {"type": "string", "valueString": "m"}}}]}})],
])
def test_normalized_documents_validate_against_the_published_schema(contents):
    n = normalize(op(contents), document_id="d", attempt_no=2, settings=S)
    n.document["result_id"] = "r"
    jsonschema.validate(n.document, SCHEMA)


def test_schema_rejects_invented_confidence_and_bad_status():
    n = normalize(op([root(), routed("invoice", {})]), document_id="d", attempt_no=1, settings=S).document
    n["classification"]["confidence"] = 0.9
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(n, SCHEMA)
