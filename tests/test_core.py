"""Unit tests that need no dataset, plus integration tests that run when data is present."""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chronotrace.engine import _margin, fuse, rank  # noqa: E402
from chronotrace.loader import DATA_DIR, is_service_column, owner_of  # noqa: E402
from chronotrace.log_scorer import classify_all, scrub  # noqa: E402
from chronotrace.metric_scorer import score_column  # noqa: E402

HAS_DATA = os.path.exists(os.path.join(DATA_DIR, "cases.parquet"))


# ---------------------------------------------------------------- loader hygiene


def test_datastore_folding():
    known = {"carts", "carts-db", "ts-auth-service", "ts-auth-mongo", "cartservice", "redis", "front-end", "session-db"}
    assert owner_of("carts-db", known) == "carts"
    assert owner_of("ts-auth-mongo", known) == "ts-auth-service"
    assert owner_of("redis", known) == "cartservice"
    assert owner_of("session-db", known) == "front-end"
    assert owner_of("frontend", known) == "frontend"


def test_non_service_columns_dropped():
    assert not is_service_column("time")
    assert not is_service_column("time.1")
    assert not is_service_column("192-168-1-10_cpu")
    assert not is_service_column("PassthroughCluster_latency")
    assert not is_service_column("frontend-external_workload")
    assert is_service_column("checkoutservice_cpu")


# ---------------------------------------------------------------- metric scorer


def test_single_spike_does_not_beat_sustained_shift():
    rng = np.random.default_rng(0)
    pre = rng.normal(10, 1, 300)
    spike = rng.normal(10, 1, 300)
    spike[50] = 500  # one-sample spike on a healthy service
    shift = rng.normal(16, 1, 300)  # sustained +6 sigma
    t = np.arange(300)
    s_spike = score_column(pre, spike, t)[0]
    s_shift = score_column(pre, shift, t)[0]
    assert s_shift > 5 * max(s_spike, 1e-9)


def test_bursty_series_with_same_peaks_scores_low():
    # Periodic flushes: zero most of the time, same bursts before and after.
    pre = np.zeros(300)
    post = np.zeros(300)
    pre[::60] = 85000
    pre[1::60] = 85000
    post[::50] = 85000
    post[1::50] = 85000
    assert score_column(pre, post, np.arange(300))[0] == 0.0
    # BARO-style max z would be 85000 here.


def test_novelty_filter_toggle():
    pre = np.zeros(300)
    pre[::60] = 100
    post = pre.copy()
    t = np.arange(300)
    assert score_column(pre, post, t, novelty_filter=True)[0] == 0.0


# ---------------------------------------------------------------- log scorer


def test_log_classification():
    msgs = pd.Series(
        [
            "2023-01-01 10:00:00  INFO 1 --- [main] Started in 3s",
            "Traceback (most recent call last):\n  File \"app.py\", line 3, in <module>",
            "rpc error: code = Unavailable desc = connection refused",
            "level=info caller=logging.go:65 method=Get err=null",
            "Exception in thread \"main\" java.io.IOException: bind\n\tat io.grpc.Server.start(Server.java:12)",
        ]
    )
    c = classify_all(msgs).reset_index(drop=True)
    assert list(c["severe"]) == [False, True, True, False, True]
    assert list(c["stack"]) == [False, True, False, False, True]
    assert bool(c.loc[2, "propagated"]) and not bool(c.loc[1, "propagated"])


def test_pii_scrub():
    out = scrub("user bob@example.com paid with 4111 1111 1111 1111 from 10.1.2.3:443 token=abc.def")
    assert "bob@example.com" not in out and "4111" not in out and "10.1.2.3" not in out and "abc.def" not in out


# ---------------------------------------------------------------- fusion


def test_margin_and_fusion_are_deterministic():
    m = {"a": 10.0, "b": 9.5, "c": 1.0}  # indecisive
    t = {"a": 1.0, "b": 20.0, "c": 0.5}  # decisive
    assert _margin(m) < 0.1 and _margin(t) > 0.9
    s1, g1 = fuse(m, {}, t, ["a", "b", "c"], True)
    s2, g2 = fuse(m, {}, t, ["a", "b", "c"], True)
    assert s1 == s2 and rank(s1)[0] == "b"
    assert g1["trace"]["share"] > g1["metric"]["share"]


def test_engine_does_not_import_llm_code():
    import chronotrace.engine  # noqa: F401

    assert "chronotrace.explainer" not in sys.modules
    assert "google.genai" not in sys.modules


# ---------------------------------------------------------------- integration (needs data)


@pytest.mark.skipif(not HAS_DATA, reason="RCAEval data not downloaded")
def test_diagnose_is_reproducible():
    from chronotrace.engine import diagnose
    from chronotrace.loader import list_cases

    cases = list_cases(["RE3-OB"]) or list_cases()
    b1 = diagnose(cases[0], with_baseline=False)
    b2 = diagnose(cases[0], with_baseline=False)
    assert b1["reproducibility"]["evidence_sha256"] == b2["reproducibility"]["evidence_sha256"]
    assert b1["reproducibility"]["llm_used_in_diagnosis"] is False
    assert b1["diagnosis"]["where_service_ranks"][0]["role"] == "ROOT_CAUSE"


@pytest.mark.skipif(not HAS_DATA, reason="RCAEval data not downloaded")
def test_explainer_template_is_grounded():
    from chronotrace.engine import diagnose
    from chronotrace.explainer import explain
    from chronotrace.loader import list_cases

    b = diagnose((list_cases(["RE3-OB"]) or list_cases())[0], with_baseline=False)
    e = explain(b, provider="template")
    assert e["verified"] and e["provider"] == "template"
    assert b["diagnosis"]["root_cause"] in e["report"]["executive_summary"]


def test_guardrail_rejects_hallucinations():
    from chronotrace.explainer import RemediationReport, Step, verify

    bundle = {
        "system": "online-boutique",
        "diagnosis": {
            "root_cause": "adservice",
            "where_service_ranks": [{"service": s} for s in ["adservice", "frontend", "cartservice", "emailservice", "redis", "paymentservice"]],
            "why_causal_chain": [{"service": "adservice"}, {"service": "frontend"}],
            "call_graph": [{"caller": "frontend", "callee": "adservice", "calls": 10}],
            "what": {"metric_evidence": [{"column": "adservice_latency"}], "primary_indicator": "adservice_latency"},
        },
    }
    good = RemediationReport(
        executive_summary="adservice crashed after a bad build.",
        root_cause_reasoning="adservice_latency rose; frontend only failed on calls into adservice.",
        causal_chain=["adservice", "frontend->adservice"],
        remediation_steps=[Step(command="kubectl rollout undo deployment/adservice -n online-boutique", rationale="roll back")],
        risk_notes="none",
    )
    assert verify(good, bundle) == []
    bad = good.model_copy(update={"root_cause_reasoning": "paymentservice_cpu spiked in billingservice"})
    problems = verify(bad, bundle)
    assert any("paymentservice" in p for p in problems) and any("billingservice" in p for p in problems)
    bad_chain = good.model_copy(update={"causal_chain": ["adservice", "cartservice->adservice"]})
    assert any("not in the observed call graph" in p for p in verify(bad_chain, bundle))
