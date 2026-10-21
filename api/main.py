"""ChronoTrace API + dashboard.

    uvicorn api.main:app --port 8000

The diagnosis endpoints never import the explanation layer. It is imported
lazily inside /api/explain, after the evidence bundle and its hash are final.
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from collections import OrderedDict
from typing import Dict, Optional

warnings.filterwarnings("ignore")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from chronotrace.engine import diagnose, evidence_series  # noqa: E402
from chronotrace.loader import case_index  # noqa: E402

RESULTS = os.path.join(ROOT, "results")
DASHBOARD = os.path.join(ROOT, "dashboard")
AUDIT = os.path.join(RESULTS, "audit.jsonl")

# Cases worth opening first in a demo (picked from benchmark results, see README).
SHOWCASE = [
    {"case": "re2ob_checkoutservice_delay_1", "why": "Network delay: BARO and callers' latency point elsewhere; edge-gap scoring isolates checkoutservice"},
    {"case": "re3ob_adservice_f5_1", "why": "Code fault in an uninstrumented Java service: stack trace + client-side gap"},
    {"case": "re3ob_cartservice_f1_1", "why": "Code fault: flat resources, new exception in cartservice logs"},
    {"case": "re2ss_carts_cpu_1", "why": "Sock Shop has no traces: gate falls back to metrics and logs"},
    {"case": "re3ss_orders_f3_1", "why": "Code fault without traces: logs carry the diagnosis"},
    {"case": "re1tt_ts-auth-service_cpu_4", "why": "64-service Train Ticket, metrics only"},
]

app = FastAPI(title="ChronoTrace", version="1.0.0", description="Deterministic multi-source root cause analysis on RCAEval")
_cache: "OrderedDict[str, Dict]" = OrderedDict()


def _bundle(case_id: str, fresh: bool = False) -> Dict:
    if not fresh and case_id in _cache:
        _cache.move_to_end(case_id)
        return _cache[case_id]
    try:
        b = diagnose(case_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    _cache[case_id] = b
    while len(_cache) > 24:
        _cache.popitem(last=False)
    os.makedirs(RESULTS, exist_ok=True)
    with open(AUDIT, "a") as f:
        f.write(
            json.dumps(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "case": case_id,
                    "root_cause": b["diagnosis"]["root_cause"],
                    "confidence": b["diagnosis"]["confidence"],
                    "input_sha256": b["reproducibility"]["input_sha256"],
                    "evidence_sha256": b["reproducibility"]["evidence_sha256"],
                    "analysis_ms": b["timing_ms"]["analysis_total"],
                }
            )
            + "\n"
        )
    return b


@app.get("/api/health")
def health():
    return {"ok": True, "llm_loaded": "chronotrace.explainer" in sys.modules}


@app.get("/api/cases")
def cases(dataset: Optional[str] = None):
    idx = case_index()
    idx = idx[idx["available"]]
    if dataset:
        idx = idx[idx["dataset"] == dataset.upper()]
    cols = ["case", "dataset", "system_name", "root_cause_service", "fault", "has_logs", "has_traces", "n_metrics"]
    rows = idx[cols].to_dict(orient="records")
    return {"cases": rows, "datasets": sorted(idx["dataset"].unique().tolist()), "showcase": SHOWCASE}


@app.get("/api/diagnose/{case_id}")
def api_diagnose(case_id: str, fresh: bool = False):
    return JSONResponse(_bundle(case_id, fresh))


@app.get("/api/series/{case_id}")
def api_series(case_id: str, services: str = Query(..., description="comma-separated")):
    svcs = [s for s in services.split(",") if s][:6]
    try:
        return evidence_series(case_id, svcs)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.post("/api/explain/{case_id}")
def api_explain(case_id: str, provider: Optional[str] = None):
    from chronotrace.explainer import explain  # imported only here, after the diagnosis is final

    return explain(_bundle(case_id), provider)


@app.get("/api/benchmark")
def api_benchmark(include_cases: bool = False):
    p = os.path.join(RESULTS, "benchmark.json")
    if not os.path.exists(p):
        raise HTTPException(404, "run scripts/run_benchmark.py first")
    with open(p) as f:
        rep = json.load(f)
    if not include_cases:
        rep.pop("cases", None)
    return rep


@app.get("/")
def index():
    return FileResponse(os.path.join(DASHBOARD, "index.html"))


app.mount("/static", StaticFiles(directory=DASHBOARD), name="static")
