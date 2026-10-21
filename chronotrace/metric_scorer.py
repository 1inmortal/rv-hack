"""Metric scorer: sustained robust drift instead of BARO's single max point.

BARO scores a metric by the largest robust z-score seen after the fault. One
isolated spike on a healthy service can win that contest. Here each series is
smoothed with a 3-point rolling median (kills one-sample spikes), then scored
by the mean of its upper half plus its 90th percentile, scaled by how much the
whole distribution moved (two-sample KS statistic).
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Dict, List

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

from .loader import TelemetryCase

warnings.filterwarnings("ignore", category=RuntimeWarning)

ONSET_Z = 3.0  # z above this for 2 consecutive seconds marks the onset


@dataclass
class MetricEvidence:
    column: str
    service: str
    score: float
    pre_median: float
    pre_iqr: float
    post_median: float
    peak_z: float
    sustained_z: float
    ks: float
    onset: int  # unix seconds; inject_time + window if never crossed


def _scale(iqr: float) -> float:
    # Same rule as sklearn's RobustScaler used by BARO: IQR, or 1 when IQR is 0.
    return iqr if iqr > 0 else 1.0


def score_column(a: np.ndarray, b: np.ndarray, t_post: np.ndarray, novelty_filter: bool = True):
    """Score one series. a = normal window, b = faulty window."""
    med = float(np.median(a))
    q1, q3 = np.percentile(a, [25, 75])
    iqr = float(q3 - q1)
    scale = _scale(iqr)
    z = (b - med) / scale
    z_s = pd.Series(z).rolling(3, center=True, min_periods=1).median().to_numpy()
    upper = z_s[z_s >= np.median(z_s)]
    sustained = float(upper.mean()) if len(upper) else 0.0
    q90 = float(np.percentile(z_s, 90))
    ks = float(ks_2samp(a, b).statistic)
    # Novelty: share of faulty-window points above the highest (smoothed) level
    # the series already reached while healthy. Bursty series such as periodic
    # disk flushes hit the same peaks before the fault, so they score ~0 here.
    pre_peak = pd.Series((a - med) / scale).rolling(3, center=True, min_periods=1).median().max()
    novelty = float(np.mean(z_s > pre_peak)) if novelty_filter else 1.0
    score = (0.65 * sustained + 0.35 * q90) * (1.0 + ks) * novelty
    above = (z_s > ONSET_Z).astype(int)
    hits = np.where(above[:-1] & above[1:])[0]
    onset = int(t_post[hits[0]]) if len(hits) else int(t_post[-1]) + 1
    return score, med, iqr, float(np.median(b)), float(z_s.max()), sustained, ks, onset


def score_metrics(case: TelemetryCase, novelty_filter: bool = True) -> Dict[str, List[MetricEvidence]]:
    """Return per-service metric evidence, best column first."""
    df = case.metrics
    it = case.inject_time
    pre = df[df["time"] < it]
    post = df[df["time"] >= it]
    t_post = post["time"].to_numpy(np.int64)
    out: Dict[str, List[MetricEvidence]] = {}
    for col in df.columns:
        if col == "time":
            continue
        a = pre[col].to_numpy(np.float64)
        b = post[col].to_numpy(np.float64)
        if len(a) < 10 or len(b) < 10:
            continue
        if np.all(a == a[0]) and np.all(b == a[0]):
            continue  # constant over the whole window: no information
        if col.endswith("_mem"):
            a, b = a / 1e6, b / 1e6  # bytes -> MB, as RCAEval does
        score, med, iqr, post_med, peak, sust, ks, onset = score_column(a, b, t_post, novelty_filter)
        svc = case.column_owner[col]
        out.setdefault(svc, []).append(
            MetricEvidence(col, svc, float(score), med, iqr, post_med, peak, sust, ks, onset)
        )
    for svc in out:
        out[svc].sort(key=lambda e: -e.score)
    return out


def service_scores(evidence: Dict[str, List[MetricEvidence]]) -> Dict[str, float]:
    """Best column per service; a second anomalous column adds a little."""
    res = {}
    for svc, items in evidence.items():
        top = items[0].score
        second = items[1].score if len(items) > 1 else 0.0
        res[svc] = top + 0.25 * max(second, 0.0) if top > 0 else top
    return res
