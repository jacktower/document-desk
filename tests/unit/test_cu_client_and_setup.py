import json

import httpx
import pytest

from docdesk.config import Settings
from docdesk.cu import analyzer_config as AC
from docdesk.cu.client import CuClient, CuSetupError
from docdesk.cu.setup import setup_analyzers
from docdesk.domain.errors import StageError
from docdesk_devtools.sim_cu import SimulatedCu

PDF = b"%PDF-1.4 fake"


def make(sim, **kw):
    s = Settings(app_env="dev", cu_endpoint="http://cu.invalid", cu_auth="none", **kw)
    http = httpx.Client(transport=httpx.WSGITransport(app=sim), base_url="http://cu.invalid")
    return s, CuClient(s, http=http, sleep=lambda x: None)


@pytest.fixture
def ready():
    sim = SimulatedCu()
    s, c = make(sim)
    sim.defaults = {"gpt-4.1": "g", "prebuilt-analyzer-embedding": "e"}
    sim.analyzers.update(AC.all_definitions(s))
    return sim, s, c


def analyze(c, s):
    return c.analyze(AC.classifier_id("1"), PDF, "application/pdf", "req-1")


def test_analyze_success_stores_final_response_bytes(ready):
    sim, s, c = ready
    out = analyze(c, s)
    doc = json.loads(out.raw)
    assert doc["status"] == "Succeeded" and out.operation_id == doc["id"] and out.request_id == "req-1" and doc["_simulated"] is True


@pytest.mark.parametrize("behaviour,code,retryable,status", [
    ("throttle", "PROVIDER_THROTTLED", True, 429), ("unavailable", "PROVIDER_UNAVAILABLE", True, 503),
    ("rejected", "PROVIDER_REQUEST_REJECTED", False, 400), ("forbidden", "PROVIDER_CONFIGURATION", False, 403),
])
def test_submit_errors_mapped(ready, behaviour, code, retryable, status):
    sim, s, c = ready
    sim.script = [behaviour]
    with pytest.raises(StageError) as e:
        analyze(c, s)
    assert (e.value.code, e.value.retryable, e.value.http_status) == (code, retryable, status) and e.value.provider_request_id == "req-1"
    if behaviour == "throttle":
        assert e.value.retry_after_s == 1.0


@pytest.mark.parametrize("behaviour,code,retryable", [
    ("failed_content", "PROVIDER_OPERATION_FAILED", False), ("failed_transient", "PROVIDER_OPERATION_FAILED", True),
    ("empty", "PROVIDER_RESPONSE_MALFORMED", True), ("malformed", "PROVIDER_RESPONSE_MALFORMED", True),
])
def test_poll_outcomes_mapped(ready, behaviour, code, retryable):
    sim, s, c = ready
    sim.script = [behaviour]
    with pytest.raises(StageError) as e:
        analyze(c, s)
    assert (e.value.code, e.value.retryable) == (code, retryable)


def test_poll_timeout():
    sim = SimulatedCu()
    s, c = make(sim, cu_poll_timeout_s=0)
    sim.analyzers.update(AC.all_definitions(s))
    sim.script = ["never_finishes"]
    with pytest.raises(StageError) as e:
        analyze(c, s)
    assert e.value.code == "PROVIDER_TIMEOUT" and e.value.retryable


def test_network_failure_is_unavailable():
    def boom(request):
        raise httpx.ConnectError("no route")
    s = Settings(app_env="dev", cu_endpoint="http://cu.invalid", cu_auth="none")
    c = CuClient(s, http=httpx.Client(transport=httpx.MockTransport(boom)), sleep=lambda x: None)
    with pytest.raises(StageError) as e:
        analyze(c, s)
    assert e.value.code == "PROVIDER_UNAVAILABLE" and e.value.retryable


def test_empty_2xx_submit_is_malformed():
    s = Settings(app_env="dev", cu_endpoint="http://cu.invalid", cu_auth="none")
    c = CuClient(s, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(202))), sleep=lambda x: None)
    with pytest.raises(StageError) as e:
        analyze(c, s)
    assert e.value.code == "PROVIDER_RESPONSE_MALFORMED"


def test_transient_poll_blips_are_tolerated_inside_one_try():
    calls = {"n": 0}
    def handler(request: httpx.Request):
        if request.method == "POST":
            return httpx.Response(202, headers={"Operation-Location": "http://cu.invalid/op/1"})
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] <= 2 else httpx.Response(200, json={"id": "1", "status": "Succeeded", "result": {"contents": []}})
    s = Settings(app_env="dev", cu_endpoint="http://cu.invalid", cu_auth="none")
    c = CuClient(s, http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda x: None)
    assert json.loads(analyze(c, s).raw)["status"] == "Succeeded" and calls["n"] == 3


def test_lease_callback_runs_each_poll_and_can_abort(ready):
    sim, s, c = ready
    sim.polls_before_done = 3
    ticks = []
    c.analyze(AC.classifier_id("1"), PDF, "application/pdf", "r", on_poll=lambda: ticks.append(1))
    assert len(ticks) == 4
    class Abort(Exception): ...
    def stop():
        raise Abort()
    with pytest.raises(Abort):
        c.analyze(AC.classifier_id("1"), PDF, "application/pdf", "r", on_poll=stop)


# ---- setup-analyzers: defaults first, analyzers second ---------------------------------------------
def fresh(**kw):
    sim = SimulatedCu()
    s, c = make(sim, **kw)
    return sim, s, c


def test_setup_without_defaults_stops_before_any_analyzer_request():
    sim, s, c = fresh()
    out = []
    assert setup_analyzers(c, s, out=out.append) == 2
    assert sim.put_calls == [] and not any("analyzers/" in p for _, p in sim.calls)
    assert any("prebuilt-analyzer-embedding" in line and "--set-defaults" in line for line in out)


def test_setup_check_never_writes():
    sim, s, c = fresh(cu_model_deployments='{"gpt-4.1":"g","prebuilt-analyzer-embedding":"e"}')
    assert setup_analyzers(c, s, check=True, set_defaults=True, out=lambda _: None) == 2
    assert sim.defaults is None and sim.put_calls == [] and all(m == "GET" for m, _ in sim.calls)
    sim.defaults = {"gpt-4.1": "g", "prebuilt-analyzer-embedding": "e"}
    assert setup_analyzers(c, s, check=True, out=lambda _: None) == 1  # analyzers missing, reported, nothing written
    assert sim.put_calls == [] and all(m == "GET" for m, _ in sim.calls)


def test_set_defaults_requires_embedding_alias_in_variable():
    sim, s, c = fresh(cu_model_deployments='{"gpt-4.1":"g"}')
    out = []
    assert setup_analyzers(c, s, set_defaults=True, out=out.append) == 2
    assert sim.defaults is None and sim.put_calls == [] and any("embedding alias" in line for line in out)


def test_set_defaults_then_register_in_order_and_repeat_is_idempotent():
    sim, s, c = fresh(cu_model_deployments='{"gpt-4.1":"g","prebuilt-analyzer-embedding":"e"}')
    assert setup_analyzers(c, s, set_defaults=True, out=lambda _: None) == 0
    methods = [(m, p.rsplit("/", 1)[-1]) for m, p in sim.calls]
    first_put = next(i for i, (m, _) in enumerate(methods) if m == "PUT")
    patch = next(i for i, (m, _) in enumerate(methods) if m == "PATCH")
    assert patch < first_put                                                   # defaults before the first analyzer request
    assert sim.put_calls == list(AC.all_definitions(s)) and sim.put_calls[-1] == AC.classifier_id("1")
    out = []
    n_calls = len(sim.put_calls)
    assert setup_analyzers(c, s, out=out.append) == 0
    assert len(sim.put_calls) == n_calls and sum("skipped" in l for l in out) == 6   # safe to repeat: nothing re-registered


def test_drifted_analyzer_fails_and_asks_for_version_bump():
    sim, s, c = fresh(cu_model_deployments='{"gpt-4.1":"g","prebuilt-analyzer-embedding":"e"}')
    setup_analyzers(c, s, set_defaults=True, out=lambda _: None)
    sim.analyzers[AC.classifier_id("1")]["description"] = "someone edited this"
    out = []
    assert setup_analyzers(c, s, out=out.append) == 1
    assert any("bump ANALYZER_CONFIG_VERSION" in l for l in out)


def test_provider_inner_code_is_reported_on_failure():
    sim, s, c = fresh()
    sim.defaults = {"gpt-4.1": "g", "prebuilt-analyzer-embedding": "e"}
    sim.require_defaults_for_put = False
    sim.defaults = {"gpt-4.1": "g", "prebuilt-analyzer-embedding": "e"}
    # a 400 with an inner code on PUT must surface the inner code, not just 'InvalidRequest'
    orig = sim._route
    def route(method, path, *a, **k):
        if method == "PUT":
            return sim._err(a[-1], 400, "InvalidRequest", "DefaultDeploymentModelNotFound")
        return orig(method, path, *a, **k)
    sim._route = route
    out = []
    assert setup_analyzers(c, s, out=out.append) == 1
    assert any("InvalidRequest/DefaultDeploymentModelNotFound" in l for l in out)


# ---- readiness of existing, matching analyzers ---------------------------------------------------------
def registered():
    sim, s, c = fresh(cu_model_deployments='{"gpt-4.1":"g","prebuilt-analyzer-embedding":"e"}')
    assert setup_analyzers(c, s, set_defaults=True, out=lambda _: None) == 0
    return sim, s, c


@pytest.mark.parametrize("check", [False, True])
def test_matching_but_failed_analyzers_are_not_reported_ready(check):
    sim, s, c = registered()
    sim.analyzer_status = {aid: "failed" for aid in AC.all_definitions(s)}
    out = []
    puts, before = len(sim.put_calls), len(sim.calls)
    assert setup_analyzers(c, s, check=check, out=out.append) == 1
    assert sum("NOT READY (status: failed)" in l for l in out) == 6 and not any("skipped" in l for l in out)
    assert len(sim.put_calls) == puts and not any(m in ("PUT", "PATCH") for m, _ in sim.calls[before:])    # nothing is rewritten


def test_in_progress_analyzer_is_waited_for_in_setup_but_not_in_check():
    sim, s, c = registered()
    cid = AC.classifier_id("1")
    sim.analyzer_status = {cid: ["creating", "creating", "ready"]}
    assert setup_analyzers(c, s, out=lambda _: None) == 0                       # became ready within the bounded wait
    sim.analyzer_status = {cid: "creating"}
    out = []
    assert setup_analyzers(c, s, check=True, out=out.append) == 1               # --check reports, never waits or writes
    assert any("Still being created" in l for l in out)
    sim.analyzer_status = {cid: "creating"}
    assert setup_analyzers(c, s, out=out.append) == 1                           # bounded: gives up rather than hanging
