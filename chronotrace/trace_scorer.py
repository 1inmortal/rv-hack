"""Trace scorer: server-side exclusive time per service + network gap per call edge.

Two separate questions are asked of the spans:

1. Did a service get slower at its own work? Server spans are scored by their
   exclusive time (own duration minus the time spent waiting on children). A
   delay deep in the call chain inflates every caller's total duration, but not
   their exclusive time.

2. Did the network around a service get slower? For every client call
   caller -> callee, the "gap" is the client span duration minus the callee's
   server span (or the whole client span when the callee emits no spans, like
   Online Boutique's adservice and cartservice). Netem delay/loss on a pod slows
   every edge touching that pod, in both directions. So a service is blamed for
   network trouble in proportion to how many of its edges shifted and what share
   of its edges that is: a caller whose callee is slow has one bad edge out of
   many; the slow callee has all of its edges bad.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from .loader import TelemetryCase, normalise_trace_service
from .metric_scorer import score_column

_GRPC_CALLEE = re.compile(r"hipstershop\.(\w+)/")
MIN_PRE_SECONDS = 10
MIN_POST_SECONDS = 5


@dataclass
class TraceEvidence:
    service: str
    score: float
    server_score: float
    network_score: float
    error_score: float
    pre_p90_ms: float
    post_p90_ms: float
    pre_error_rate: float
    post_error_rate: float
    spans_pre: int
    spans_post: int
    onset: int
    shifted_edges: List[str] = field(default_factory=list)


@dataclass
class EdgeEvidence:
    caller: str
    callee: str
    calls: int
    score: float
    pre_gap_p90_ms: float
    post_gap_p90_ms: float
    onset: int


@dataclass
class TraceResult:
    services: Dict[str, TraceEvidence]
    edges: Dict[Tuple[str, str], int]  # caller -> callee, call count
    edge_evidence: Dict[Tuple[str, str], EdgeEvidence]
    status_codes: bool


def _callee_from_operation(op: str, lookup: Dict[str, str]) -> str:
    if not isinstance(op, str):
        return ""
    m = _GRPC_CALLEE.search(op)
    return lookup.get(m.group(1).lower(), "") if m else ""


def build_span_frame(case: TelemetryCase) -> pd.DataFrame:
    """One row per span with service, exclusive time, and client-call info."""
    t = case.traces
    known = set(case.services)
    lookup = {s.replace("-", "").lower(): s for s in known}
    df = pd.DataFrame(
        {
            "span": t["spanID"].astype(str).to_numpy(),
            "parent": t["parentSpanID"].astype(str).to_numpy(),
            "svc": t["serviceName"].astype(str).map(lambda s: normalise_trace_service(s, known)).to_numpy(),
            "op": t["operationName"].astype(str).to_numpy(),
            "start_us": t["startTime"].astype("float64").to_numpy(),
            "dur": t["duration"].astype("float64").fillna(0).to_numpy(),
            "status": t["statusCode"].astype("float64").to_numpy(),
        }
    )
    kids = df.groupby("parent").agg(child_sum=("dur", "sum"), n_child=("dur", "size"), child_svc=("svc", "first"))
    df = df.join(kids, on="span")
    df["child_sum"] = df["child_sum"].fillna(0.0)
    df["n_child"] = df["n_child"].fillna(0).astype(int)
    df["excl"] = (df["dur"] - df["child_sum"]).clip(lower=0.0)

    named = df["op"].map(lambda o: _callee_from_operation(o, lookup))
    single_remote = (df["n_child"] == 1) & df["child_svc"].notna() & (df["child_svc"] != df["svc"])
    callee = np.where((named != "") & (named != df["svc"]), named, np.where(single_remote, df["child_svc"], ""))
    df["callee"] = callee
    df["is_client"] = df["callee"] != ""
    df["sec"] = (df["start_us"] // 1_000_000).astype("int64")
    df["is_err"] = np.where(np.isfinite(df["status"]), (df["status"] != 0).astype(float), np.nan)
    return df


def _series_score(g: pd.DataFrame, value: str, it: int):
    per_sec = g.groupby("sec")[value].quantile(0.9)
    pre = per_sec[per_sec.index < it]
    post = per_sec[per_sec.index >= it]
    if len(pre) < MIN_PRE_SECONDS or len(post) < MIN_POST_SECONDS:
        return None
    score, *_rest, onset = score_column(pre.to_numpy(), post.to_numpy(), post.index.to_numpy())
    pre_v = g.loc[g["sec"] < it, value]
    post_v = g.loc[g["sec"] >= it, value]
    return (
        float(max(score, 0.0)),
        int(onset),
        float(np.percentile(pre_v, 90) / 1000.0) if len(pre_v) else 0.0,
        float(np.percentile(post_v, 90) / 1000.0) if len(post_v) else 0.0,
    )


def score_traces(case: TelemetryCase) -> TraceResult:
    if case.traces is None or len(case.traces) == 0:
        return TraceResult({}, {}, {}, False)
    it = case.inject_time
    known = set(case.services)
    df = build_span_frame(case)
    status_codes = bool(np.isfinite(df["status"]).any())

    # Call graph from parent/child links and from client spans.
    parent_svc = df.drop_duplicates("span").set_index("span")["svc"]
    df["parent_svc"] = df["parent"].map(parent_svc)
    edges: Dict[Tuple[str, str], int] = {}
    cross = df[df["parent_svc"].notna() & (df["parent_svc"] != df["svc"])]
    for (a, b), n in cross.groupby(["parent_svc", "svc"]).size().items():
        edges[(a, b)] = edges.get((a, b), 0) + int(n)
    client = df[df["is_client"]]
    for (a, b), n in client.groupby(["svc", "callee"]).size().items():
        if (a, b) not in edges:
            edges[(a, b)] = int(n)

    # 1. Network gap per edge.
    edge_ev: Dict[Tuple[str, str], EdgeEvidence] = {}
    for (a, b), g in client.groupby(["svc", "callee"]):
        if a not in known or b not in known:
            continue
        r = _series_score(g, "excl", it)
        if r is None:
            continue
        edge_ev[(a, b)] = EdgeEvidence(a, b, int(len(g)), r[0], r[2], r[3], r[1])

    incident: Dict[str, List[EdgeEvidence]] = {}
    for (a, b), e in edge_ev.items():
        incident.setdefault(a, []).append(e)
        incident.setdefault(b, []).append(e)
    max_edge = max([e.score for e in edge_ev.values()], default=0.0)

    # 2. Server-side exclusive time and error rate per service.
    server = df[~df["is_client"]]
    out: Dict[str, TraceEvidence] = {}
    for svc in sorted(known):
        g = server[server["svc"] == svc]
        r = _series_score(g, "excl", it) if len(g) else None
        server_score, onset, pre_ms, post_ms = r if r else (0.0, it + 10**6, 0.0, 0.0)

        inc = incident.get(svc, [])
        net = 0.0
        shifted = []
        if inc and max_edge > 0:
            norm = [e.score / max_edge for e in inc]
            net = sum(e.score for e in inc) * float(np.mean(norm))
            shifted = [f"{e.caller}->{e.callee}" for e, n in zip(inc, norm) if n > 0.3]
            onsets = [e.onset for e, n in zip(inc, norm) if n > 0.3]
            if onsets and net > server_score:
                onset = min(onsets)
            if not r and inc:
                pre_ms = float(np.median([e.pre_gap_p90_ms for e in inc]))
                post_ms = float(np.median([e.post_gap_p90_ms for e in inc]))

        err_score, pre_err, post_err = 0.0, 0.0, 0.0
        if status_codes:
            ge = df[(df["svc"] == svc) | (df["callee"] == svc)]
            per = ge.groupby("sec")["is_err"].mean().fillna(0)
            pe, qe = per[per.index < it].to_numpy(), per[per.index >= it].to_numpy()
            if len(pe) >= MIN_PRE_SECONDS and len(qe) >= MIN_POST_SECONDS:
                pre_err, post_err = float(pe.mean()), float(qe.mean())
                if np.any(pe != pe[0]) or np.any(qe != pe[0]):
                    err_score = max(score_column(pe, qe, per[per.index >= it].index.to_numpy())[0], 0.0)

        if server_score == 0 and net == 0 and err_score == 0 and not len(g) and not inc:
            continue
        g_all = df[(df["svc"] == svc) | (df["callee"] == svc)]
        out[svc] = TraceEvidence(
            service=svc,
            score=float(max(server_score, net) + 0.5 * err_score),
            server_score=float(server_score),
            network_score=float(net),
            error_score=float(err_score),
            pre_p90_ms=pre_ms,
            post_p90_ms=post_ms,
            pre_error_rate=pre_err,
            post_error_rate=post_err,
            spans_pre=int((g_all["sec"] < it).sum()),
            spans_post=int((g_all["sec"] >= it).sum()),
            onset=int(onset),
            shifted_edges=shifted,
        )
    return TraceResult(out, edges, edge_ev, status_codes)


def latency_series(case: TelemetryCase, services: List[str]) -> Dict[str, List[Tuple[int, float]]]:
    """Per-second p90 exclusive time (ms) of server spans, for the UI chart.

    Services with no server spans fall back to the gap on their incoming calls.
    """
    if case.traces is None or len(case.traces) == 0:
        return {}
    df = build_span_frame(case)
    res = {}
    for s in services:
        g = df[(df["svc"] == s) & ~df["is_client"]]
        if len(g) < 20:
            g = df[df["callee"] == s]
        if len(g) == 0:
            continue
        p = g.groupby("sec")["excl"].quantile(0.9) / 1000.0
        res[s] = [(int(k), round(float(v), 3)) for k, v in p.items()]
    return res
