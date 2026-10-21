"""Reference baselines, called straight from the official RCAEval package.

The ranking of metric names is turned into a service ranking the same way
RCAEval's main.py does it: split on "_", drop "-db", keep first occurrence.
"""
from __future__ import annotations

import warnings
from typing import Dict, List

warnings.filterwarnings("ignore")

from RCAEval.io.time_series import preprocess  # noqa: E402

from .loader import TelemetryCase  # noqa: E402


def to_service_ranks(metric_ranks: List[str]) -> List[str]:
    out: List[str] = []
    for name in metric_ranks:
        svc = name.split("_")[0].replace("-db", "")
        if svc not in out:
            out.append(svc)
    return out


def protocol_candidates(case: TelemetryCase) -> int:
    cols = preprocess(case.raw_metrics.copy(), dataset=case.dataset.lower(), dk_select_useful=False).columns
    return len({c.split("_")[0].replace("-db", "") for c in cols if c != "time"})


def run_baro(case: TelemetryCase) -> Dict:
    from RCAEval.e2e.baro import baro

    out = baro(case.raw_metrics.copy(), case.inject_time, dataset=case.dataset.lower())
    return {"metric_ranks": out["ranks"], "ranks": to_service_ranks(out["ranks"])}


def run_nsigma(case: TelemetryCase) -> Dict:
    from RCAEval.e2e import nsigma

    out = nsigma(case.raw_metrics.copy(), case.inject_time, dataset=case.dataset.lower())
    return {"metric_ranks": out["ranks"], "ranks": to_service_ranks(out["ranks"])}


def run_rcd(case: TelemetryCase) -> Dict:
    import numpy as np
    from RCAEval.e2e import rcd

    np.random.seed(0)  # RCD partitions metrics at random; pin it so runs repeat
    out = rcd(case.raw_metrics.copy(), case.inject_time, dataset=case.dataset.lower())
    return {"metric_ranks": out["ranks"], "ranks": to_service_ranks(out["ranks"])}


BASELINES = {"baro": run_baro, "nsigma": run_nsigma, "rcd": run_rcd}
