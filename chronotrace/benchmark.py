"""Benchmark ChronoTrace, its ablations, and official RCAEval baselines.

Each case is scored once per modality; every variant below is a different
fusion of those same scores, so the comparison is like-for-like. All numbers
come from RCAEval's own Evaluator at service level.
"""
from __future__ import annotations

import json
import os
import time
import warnings
from typing import Dict, List, Optional

warnings.filterwarnings("ignore")

from .baselines import protocol_candidates, run_baro, run_nsigma
from .engine import fuse, rank
from .evaluation import summarise
from .loader import case_index, list_cases, load_case
from .log_scorer import score_logs
from .metric_scorer import score_metrics, service_scores
from .trace_scorer import score_traces

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")

VARIANTS = {
    "chronotrace": "Full engine: metrics + logs + traces, decisiveness-gated fusion",
    "no_traces": "Ablation: drop the trace scorer",
    "no_logs": "Ablation: drop the log scorer",
    "no_new_behaviour": "Ablation: logs without the new-template (restart) signal",
    "no_trace_gate": "Ablation: traces weighted like other modalities even when complete",
    "metric_only": "Ablation: metric scorer alone",
    "metric_no_novelty": "Ablation: metric scorer without the pre-fault peak filter",
}
BASELINES = {"baro": "BARO (official RCAEval implementation)", "nsigma": "N-Sigma (official RCAEval implementation)"}


def score_case(case_id: str) -> Optional[Dict]:
    case = load_case(case_id)
    if not case.usable:
        return None
    t0 = time.perf_counter()
    m_ev = score_metrics(case)
    l_ev = score_logs(case)
    tr = score_traces(case)
    t1 = time.perf_counter()
    m_nov = score_metrics(case, novelty_filter=False)
    m = service_scores(m_ev)
    m0 = service_scores(m_nov)
    l_full = {s: e.score for s, e in l_ev.items()}
    l_err = {s: e.error_score for s, e in l_ev.items() if e.error_score > 0}
    t = {s: e.score for s, e in tr.services.items()}
    svcs = case.services
    st = tr.status_codes

    def r(*args, **kw):
        return rank(fuse(*args, **kw)[0])

    ranks = {
        "chronotrace": r(m, l_full, t, svcs, st),
        "no_traces": r(m, l_full, {}, svcs, st),
        "no_logs": r(m, {}, t, svcs, st),
        "no_new_behaviour": r(m, l_err, t, svcs, st),
        "no_trace_gate": r(m, l_full, t, svcs, False),
        "metric_only": r(m, {}, {}, svcs, st),
        "metric_no_novelty": r(m0, {}, {}, svcs, st),
    }
    baro = run_baro(case)["ranks"]
    nsig = run_nsigma(case)["ranks"]
    return {
        "case": case_id,
        "dataset": case.dataset,
        "fault": case.fault,
        "root": case.root_cause,
        "n_candidates": len(svcs),
        "n_candidates_protocol": protocol_candidates(case),
        "analysis_ms": round((t1 - t0) * 1000, 1),
        "ranks": {k: v[:10] for k, v in ranks.items()},
        "baselines": {"baro": baro[:10], "nsigma": nsig[:10]},
    }


def summarise_results(per_case: List[Dict]) -> Dict:
    datasets = sorted({r["dataset"] for r in per_case})
    methods = list(VARIANTS) + list(BASELINES)
    table: Dict[str, Dict[str, Dict]] = {}
    for ds in datasets + ["ALL"]:
        rows = [r for r in per_case if ds == "ALL" or r["dataset"] == ds]
        table[ds] = {}
        for meth in methods:
            if meth in VARIANTS:
                items = [dict(ranks=r["ranks"][meth], answer=r["root"], n_candidates=r["n_candidates"]) for r in rows]
            else:
                items = [
                    dict(ranks=r["baselines"][meth], answer=r["root"], n_candidates=r["n_candidates_protocol"])
                    for r in rows
                ]
            table[ds][meth] = summarise(items)
    by_fault: Dict[str, Dict[str, Dict]] = {}
    for fault in sorted({r["fault"] for r in per_case}):
        rows = [r for r in per_case if r["fault"] == fault]
        by_fault[fault] = {
            "chronotrace": summarise([dict(ranks=r["ranks"]["chronotrace"], answer=r["root"], n_candidates=r["n_candidates"]) for r in rows]),
            "baro": summarise([dict(ranks=r["baselines"]["baro"], answer=r["root"], n_candidates=r["n_candidates_protocol"]) for r in rows]),
        }
    lat = sorted(r["analysis_ms"] for r in per_case)
    return {
        "datasets": table,
        "by_fault": by_fault,
        "latency_ms": {
            "median": lat[len(lat) // 2] if lat else None,
            "p90": lat[int(len(lat) * 0.9)] if lat else None,
            "by_dataset_median": {
                ds: sorted(r["analysis_ms"] for r in per_case if r["dataset"] == ds)[
                    len([r for r in per_case if r["dataset"] == ds]) // 2
                ]
                for ds in datasets
            },
        },
    }


def run(datasets: List[str], out_path: Optional[str] = None, verbose: bool = True) -> Dict:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_path = out_path or os.path.join(RESULTS_DIR, "benchmark.json")
    idx = case_index()
    wanted = idx[idx["dataset"].isin([d.upper() for d in datasets])]
    missing = wanted[~wanted["available"]]["case"].tolist()
    per_case, skipped = [], []
    t0 = time.time()
    for i, cid in enumerate(list_cases(datasets)):
        res = score_case(cid)
        if res is None:
            skipped.append(cid)
            continue
        per_case.append(res)
        if verbose and (i + 1) % 25 == 0:
            print(f"  {i + 1} cases scored ({time.time() - t0:.0f}s)", flush=True)
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "protocol": "RCAEval main.py window (5 min before / 5 min after injection), service-level, official Evaluator",
        "methods": {**VARIANTS, **BASELINES},
        "skipped_unusable_cases": skipped,
        "not_downloaded_cases": len(missing),
        "summary": summarise_results(per_case),
        "cases": per_case,
    }
    with open(out_path, "w") as f:
        json.dump(report, f, indent=1)
    return report
