import json

import pytest

from docdesk.config import Settings
from docdesk.cu import analyzer_config as AC
from docdesk.cu import registry as R


def S(**kw):
    return Settings(app_env="dev", **kw)


def test_all_definitions_validate_and_route():
    s = S()
    AC.validate_all(s)
    defs = AC.all_definitions(s)
    cls = defs[AC.classifier_id("1")]
    cats = cls["config"]["contentCategories"]
    assert set(cats) == set(R.CATEGORY_NAMES) and "other" in cats
    assert "analyzerId" not in cats["other"]                      # `other` has no extractor
    assert all(c["analyzerId"] in defs for n, c in cats.items() if n != "other")
    assert list(defs)[-1] == AC.classifier_id("1")                # classifier registered after its extractors
    assert cls["baseAnalyzerId"] == "prebuilt-document" and cls["config"]["enableSegment"] is False


def test_extractor_fields_come_from_registry():
    s = S()
    d = AC.extractor_definition("agreement", s)
    names = set(d["fieldSchema"]["fields"])
    assert {"parties", "effective_date", "termination_clause_text", "milestones"} <= names
    f = d["fieldSchema"]["fields"]
    assert f["effective_date"]["type"] == "date" and f["parties"]["type"] == "array" and f["milestones"]["items"]["type"] == "object"
    assert f["summary"]["method"] == "generate" and f["parties"].get("method") is None
    assert all(v["description"] for v in f.values())              # every field tells the model what to do when absent


def test_no_taxpayer_identifier_fields():
    for fam in R.FAMILIES:
        for fd in R.family_fields(fam):
            assert not any(x in fd.name for x in ("ssn", "ein", "tin", "taxpayer_id")), fd.name


def test_hash_stable_and_sensitive_to_version():
    assert AC.config_hash(S()) == AC.config_hash(S())
    assert AC.config_hash(S()) != AC.config_hash(S(analyzer_config_version="2"))
    ids2 = set(AC.all_definitions(S(analyzer_config_version="2")))
    assert ids2.isdisjoint(set(AC.all_definitions(S())))          # a version bump never reuses an analyzer id


@pytest.mark.parametrize("bad", ["", "a b", "x" * 65, "ünï"])
def test_analyzer_id_regex(bad):
    with pytest.raises(ValueError):
        AC.validate_definition(bad, AC.classifier_definition(S()))


def test_local_validation_rejects_bad_definition():
    d = AC.classifier_definition(S())
    d["config"]["contentCategories"]["x"] = {"description": "x" * 5000}
    with pytest.raises(ValueError):
        AC.validate_definition("docdesk-classifier-v1", d)
    d2 = AC.extractor_definition("tax", S())
    d2["fieldSchema"]["fields"]["bad name"] = {"type": "string"}
    with pytest.raises(ValueError):
        AC.validate_definition("docdesk-tax-v1", d2)
    d3 = AC.extractor_definition("tax", S())
    d3["fieldSchema"]["fields"]["x"] = {"type": "money"}
    with pytest.raises(ValueError):
        AC.validate_definition("docdesk-tax-v1", d3)


def test_equal_ignoring_empty_objects_k005():
    authored = {"fieldSchema": {"name": "x", "fields": {}}, "config": {"a": 1}}
    assert AC.equal_ignoring_empty(authored, {"fieldSchema": {"name": "x"}, "config": {"a": 1}, "status": "ready"})
    assert not AC.equal_ignoring_empty(authored, {"fieldSchema": {"name": "x"}, "config": {"a": 2}})
    assert not AC.equal_ignoring_empty({"a": {"b": 1}}, {"a": {}})


def test_applicable_fields_and_key_fields_consistent():
    for c in R.CATEGORIES:
        names = {f.name for f in R.applicable_fields(c.name)}
        assert set(c.key_fields) <= names
        assert R.applicable_fields("other") == ()
