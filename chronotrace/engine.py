"""ChronoTrace engine: fuse the three deterministic scorers into one diagnosis.

No language model is imported or called anywhere in this module. The output is
an EvidenceBundle (plain JSON) with a SHA-256 over its deterministic content.

Fusion rule (no weights fitted per dataset):
  * each modality's service scores are divided by that modality's maximum;
  * each modality is weighted by how decisive it is in *this* incident,
    margin = (top1 - top2) / top1, so a modality that cannot tell services
    apart contributes little;
  * traces count double when spans carry status codes (complete traces).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import asdict
from typing import Dict, List, Optional, Tuple

from .loader import DATA_DIR, TelemetryCase, load_case
from .log_scorer import score_logs
from .metric_scorer import score_metrics, service_scores
from .trace_scorer import score_traces

VERSION = "1.0.0"
TRACE_WEIGHT_WITH_STATUS = 2.0
ANOMALOUS = 0.35  # normalised fused score above which a service counts as affected


def _norm(d: Dict[str, float]) -> Dict[str, float]:
    pos = {k: max(v, 0.0) for k, v in d.items()}
    m = max(pos.values(), default=0.0)
    return {k: (v / m if m > 0 else 0.0) for k, v in pos.items()}


def _margin(d: Dict[str, float]) -> float:
    v = sorted((max(x, 0.0) for x in d.values()), reverse=True)
    if not v or v[0] <= 0:
        return 0.0
    return (v[0] - (v[1] if len(v) > 1 else 0.0)) / v[0]


def _sha256_files(case: TelemetryCase) -> str:
    h = hashlib.sha256()
    folder = os.path.join(DATA_DIR, case.case_id)
    for name in ("inject_time.txt", "metrics.parquet", "logs.parquet", "traces.parquet"):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            h.update(name.encode())
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
    return h.hexdigest()


def _canonical_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _r(x: Optional[float], n: int = 4):
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return round(float(x), n)


def fuse(
    metric: Dict[str, float],
    log: Dict[str, float],
    trace: Dict[str, float],
    services: List[str],
    trace_status_codes: bool,
    use: Tuple[str, ...] = ("metric", "log", "trace"),
) -> Tuple[Dict[str, float], Dict[str, Dict]]:
    """Return fused scores in [0, 1] and per-modality gate info."""
    base = {
        "metric": 1.0,
        "log": 1.0,
        "trace": TRACE_WEIGHT_WITH_STATUS if trace_status_codes else 1.0,
    }
    raw = {"metric": metric, "log": log, "trace": trace}
    fused = {s: 0.0 for s in services}
    gate: Dict[str, Dict] = {}
    total = 0.0
    for m in ("metric", "log", "trace"):
        d = raw[m]
        if m not in use or not d:
            gate[m] = {"available": bool(d), "used": False, "base_weight": base[m], "decisiveness": 0.0, "weight": 0.0}
            continue
        dec = _margin(d)
        w = base[m] * dec
        n = _norm(d)
        for s in services:
            fused[s] += w * n.get(s, 0.0)
        total += w
        gate[m] = {"available": True, "used": True, "base_weight": base[m], "decisiveness": _r(dec), "weight": _r(w)}
    if total > 0:
        fused = {s: v / total for s, v in fused.items()}
    for m in gate:
        gate[m]["share"] = _r(gate[m]["weight"] / total) if total > 0 else 0.0
    return fused, gate


def rank(scores: Dict[str, float]) -> List[str]:
    # Deterministic tie-break on name.
    return sorted(scores, key=lambda s: (-scores[s], s))


def _confidence(final: Dict[str, float], order: List[str], modal_norm: List[Dict[str, float]]) -> float:
    if not order or final[order[0]] <= 0:
        return 0.0
    s1 = final[order[0]]
    s2 = final[order[1]] if len(order) > 1 else 0.0
    sep = 1.0 / (1.0 + math.exp(-4.0 * (s1 - s2) / (s1 + 1e-6)))
    support = max((n.get(order[0], 0.0) for n in modal_norm if n), default=0.0)
    # sep is in [0.5, 1); rescale to [0, 1) before combining.
    return max(0.0, min(1.0, (2 * sep - 1) * support))


def _category(root: str, m_ev, l_ev, t_ev, gate) -> Tuple[str, str]:
    """Rule-based fault category for the root service (what broke)."""
    contrib = {}
    if m_ev:
        contrib["metric"] = gate["metric"].get("share", 0) or 0
    if l_ev:
        contrib["log"] = gate["log"].get("share", 0) or 0
    if t_ev:
        contrib["trace"] = gate["trace"].get("share", 0) or 0
    if l_ev and l_ev.stack_trace and not l_ev.propagated and l_ev.novel:
        return "code_exception", "New exception / stack trace in the service's own logs"
    if l_ev and l_ev.novel and l_ev.error_score > 0 and not l_ev.propagated and contrib.get("log", 0) >= 0.3:
        return "code_exception", "New error in the service's own logs"
    if t_ev and t_ev.network_score > max(t_ev.server_score, 1e-9) and contrib.get("trace", 0) >= 0.3:
        return "network", "Network delay or loss on calls to and from the service"
    if l_ev and l_ev.new_behaviour_score > 0 and l_ev.error_score == 0 and contrib.get("log", 0) >= 0.5:
        return "restart_or_deploy", "Service started emitting log lines it never produced before (restart or new build)"
    if m_ev:
        col = m_ev[0].column
        kind = col.split("_", 1)[1] if "_" in col else col
        table = {
            "cpu": ("cpu", "CPU saturation"),
            "mem": ("memory", "Memory growth"),
            "diskio": ("disk", "Disk I/O pressure"),
            "disk": ("disk", "Disk I/O pressure"),
            "socket": ("socket", "Socket / connection count change"),
            "latency": ("latency", "Latency increase at the service itself"),
            "error": ("errors", "Error-rate increase"),
            "workload": ("traffic", "Traffic level change"),
            "load": ("traffic", "Traffic level change"),
        }
        for key, val in table.items():
            if kind.startswith(key):
                return val
    if t_ev and t_ev.server_score > 0:
        return "latency", "Slower own processing time in traces"
    return "unknown", "Anomalous, category undetermined"


REMEDIATION = {
    "cpu": [
        "kubectl top pod -n {ns} -l app={svc}",
        "kubectl describe deployment/{svc} -n {ns}  # check CPU requests/limits",
        "kubectl scale deployment/{svc} -n {ns} --replicas=<current+1>",
    ],
    "memory": [
        "kubectl top pod -n {ns} -l app={svc}",
        "kubectl get events -n {ns} --field-selector involvedObject.name=<pod>  # look for OOMKilled",
        "kubectl rollout undo deployment/{svc} -n {ns}  # if growth started after a release",
    ],
    "disk": [
        "kubectl exec -n {ns} deploy/{svc} -- df -h",
        "kubectl describe pod -n {ns} -l app={svc}  # check volume and node disk pressure",
    ],
    "socket": [
        "kubectl exec -n {ns} deploy/{svc} -- ss -s",
        "kubectl logs -n {ns} deploy/{svc} --since=15m | grep -i -E 'too many open files|connection'",
    ],
    "network": [
        "kubectl describe pod -n {ns} -l app={svc}  # node, restarts, readiness",
        "kubectl get networkpolicy -n {ns}",
        "kubectl exec -n {ns} deploy/{svc} -- tc qdisc show  # look for stray netem rules",
        "kubectl delete pod -n {ns} -l app={svc}  # reschedule onto a healthy node if the pod network is degraded",
    ],
    "latency": [
        "kubectl top pod -n {ns} -l app={svc}",
        "kubectl logs -n {ns} deploy/{svc} --since=15m | tail -n 200",
    ],
    "errors": [
        "kubectl logs -n {ns} deploy/{svc} --since=15m | grep -i error | tail -n 50",
        "kubectl rollout history deployment/{svc} -n {ns}",
    ],
    "code_exception": [
        "kubectl logs -n {ns} deploy/{svc} --since=15m | grep -B2 -A20 -E 'Exception|Traceback'",
        "kubectl rollout history deployment/{svc} -n {ns}",
        "kubectl rollout undo deployment/{svc} -n {ns}  # roll back to the last good build",
    ],
    "restart_or_deploy": [
        "kubectl rollout history deployment/{svc} -n {ns}",
        "kubectl get pods -n {ns} -l app={svc} -o wide  # restarts / age",
        "kubectl rollout undo deployment/{svc} -n {ns}",
    ],
    "traffic": [
        "kubectl top pod -n {ns}",
        "kubectl get hpa -n {ns}",
    ],
    "unknown": ["kubectl describe pod -n {ns} -l app={svc}"],
}


def _propagation(root: str, edges: Dict[Tuple[str, str], int], affected: List[str], onset: Dict[str, int]) -> List[Dict]:
    """Walk callers of the root that are also affected (upstream victims)."""
    callers: Dict[str, List[str]] = {}
    for (a, b) in edges:
        if a != b:
            callers.setdefault(b, []).append(a)
    chain, seen, frontier = [], {root}, [root]
    while frontier:
        nxt = []
        for v in frontier:
            for u in sorted(callers.get(v, [])):
                if u in seen or u not in affected:
                    continue
                seen.add(u)
                chain.append({"service": u, "via": v, "onset": onset.get(u)})
                nxt.append(u)
        frontier = nxt
    return chain


def diagnose(
    case_or_id,
    use: Tuple[str, ...] = ("metric", "log", "trace"),
    new_behaviour: bool = True,
    with_baseline: bool = True,
) -> Dict:
    t0 = time.perf_counter()
    case = load_case(case_or_id) if isinstance(case_or_id, str) else case_or_id
    t_load = time.perf_counter()

    m_ev = score_metrics(case) if "metric" in use else {}
    t_m = time.perf_counter()
    l_ev = score_logs(case, new_behaviour=new_behaviour) if "log" in use else {}
    t_l = time.perf_counter()
    tr = score_traces(case) if "trace" in use else None
    t_t = time.perf_counter()

    services = case.services
    m_s = service_scores(m_ev)
    l_s = {s: e.score for s, e in l_ev.items()}
    t_s = {s: e.score for s, e in tr.services.items()} if tr else {}
    status = bool(tr and tr.status_codes)
    final, gate = fuse(m_s, l_s, t_s, services, status, use)
    order = rank(final)
    t_f = time.perf_counter()

    n_m, n_l, n_t = _norm(m_s), _norm(l_s), _norm(t_s)
    root = order[0]

    # Onset per service: earliest onset among modalities where it stands out.
    onset: Dict[str, int] = {}
    for s in services:
        c = []
        if n_m.get(s, 0) > ANOMALOUS and s in m_ev:
            c.append(m_ev[s][0].onset)
        if n_l.get(s, 0) > ANOMALOUS and s in l_ev and l_ev[s].onset:
            c.append(l_ev[s].onset)
        if n_t.get(s, 0) > ANOMALOUS and tr and s in tr.services:
            c.append(tr.services[s].onset)
        if c:
            onset[s] = int(min(c))

    affected = [s for s in order if final[s] >= ANOMALOUS]
    edges = tr.edges if tr else {}
    chain = _propagation(root, edges, affected, onset)
    victims = {c["service"] for c in chain}

    ranks = []
    for i, s in enumerate(order):
        role = "ROOT_CAUSE" if i == 0 else (
            "UPSTREAM_VICTIM" if s in victims else ("CO_ANOMALOUS" if final[s] >= ANOMALOUS else "NORMAL")
        )
        ranks.append(
            {
                "rank": i + 1,
                "service": s,
                "score": _r(final[s]),
                "role": role,
                "metric": _r(n_m.get(s)),
                "log": _r(n_l.get(s)),
                "trace": _r(n_t.get(s)),
                "onset_sec_after_inject": (onset[s] - case.inject_time) if s in onset else None,
            }
        )

    root_m = m_ev.get(root, [])
    root_l = l_ev.get(root)
    root_t = tr.services.get(root) if tr else None
    category, category_text = _category(root, root_m, root_l, root_t, gate)
    ns = case.system_name
    conf = _confidence(final, order, [n_m, n_l, n_t])

    what = {
        "category": category,
        "summary": category_text,
        "primary_indicator": root_m[0].column if root_m else None,
        "metric_evidence": [
            {
                "column": e.column,
                "score": _r(e.score, 2),
                "pre_median": _r(e.pre_median),
                "pre_iqr": _r(e.pre_iqr),
                "post_median": _r(e.post_median),
                "peak_z": _r(e.peak_z, 2),
                "sustained_z": _r(e.sustained_z, 2),
                "ks": _r(e.ks, 3),
                "onset_sec_after_inject": e.onset - case.inject_time,
            }
            for e in root_m[:4]
        ],
        "log_evidence": None
        if not root_l
        else {
            "template": root_l.template,
            "sample": root_l.sample,
            "stack_trace": root_l.stack_trace,
            "novel": root_l.novel,
            "propagated": root_l.propagated,
            "pre_count": root_l.pre_count,
            "post_count": root_l.post_count,
            "error_lines_pre": root_l.error_pre,
            "error_lines_post": root_l.error_post,
            "new_templates": root_l.new_templates,
            "score": _r(root_l.score, 2),
        },
        "trace_evidence": None
        if not root_t
        else {
            "server_score": _r(root_t.server_score, 2),
            "network_score": _r(root_t.network_score, 2),
            "error_score": _r(root_t.error_score, 2),
            "p90_ms_before": _r(root_t.pre_p90_ms, 3),
            "p90_ms_after": _r(root_t.post_p90_ms, 3),
            "error_rate_before": _r(root_t.pre_error_rate),
            "error_rate_after": _r(root_t.post_error_rate),
            "shifted_edges": root_t.shifted_edges,
        },
    }
    why = [{"service": root, "via": None, "onset_sec_after_inject": (onset.get(root) - case.inject_time) if root in onset else None, "role": "origin"}]
    why += [
        {"service": c["service"], "via": c["via"], "onset_sec_after_inject": (c["onset"] - case.inject_time) if c["onset"] else None, "role": "propagated"}
        for c in chain
    ]

    diagnosis = {
        "where_service_ranks": ranks,
        "root_cause": root,
        "confidence": _r(conf),
        "ambiguous": conf < 0.55,
        "what": what,
        "why_causal_chain": why,
        "call_graph": [{"caller": a, "callee": b, "calls": n} for (a, b), n in sorted(edges.items())],
        "next_steps": {
            "category": category,
            "commands": [c.format(ns=ns, svc=root) for c in REMEDIATION.get(category, REMEDIATION["unknown"])],
        },
    }
    deterministic_hash = _canonical_hash({"ranks": [(r["service"], r["score"]) for r in ranks], "what": what, "why": why})
    t_end = time.perf_counter()

    bundle = {
        "engine": f"chronotrace {VERSION}",
        "case_id": case.case_id,
        "dataset": case.dataset,
        "system": case.system_name,
        "inject_time": case.inject_time,
        "modalities": case.modalities,
        "modality_gate": gate,
        "hygiene": case.hygiene,
        "n_candidates": len(services),
        "diagnosis": diagnosis,
        "timing_ms": {
            "load": _r((t_load - t0) * 1000, 1),
            "metrics": _r((t_m - t_load) * 1000, 1),
            "logs": _r((t_l - t_m) * 1000, 1),
            "traces": _r((t_t - t_l) * 1000, 1),
            "fusion": _r((t_f - t_t) * 1000, 1),
            "analysis_total": _r((t_end - t_load) * 1000, 1),
        },
        "reproducibility": {
            "input_sha256": _sha256_files(case),
            "evidence_sha256": deterministic_hash,
            "llm_used_in_diagnosis": False,
        },
    }
    if case.root_cause:
        bundle["ground_truth"] = {
            "root_cause": case.root_cause,
            "fault": case.fault,
            "chronotrace_rank": order.index(case.root_cause) + 1 if case.root_cause in order else None,
        }
    if with_baseline:
        try:
            from .baselines import run_baro

            b = run_baro(case)
            bundle["baseline"] = {
                "baro_top5": b["ranks"][:5],
                "baro_top_metric": b["metric_ranks"][0] if b["metric_ranks"] else None,
                "baro_rank_of_truth": (b["ranks"].index(case.root_cause) + 1) if case.root_cause in b["ranks"] else None,
            }
        except Exception as e:  # baseline is informative only
            bundle["baseline"] = {"error": f"{type(e).__name__}: {e}"}
    return bundle


def evidence_series(case_id: str, services: List[str], max_points: int = 600) -> Dict:
    """Time series for the UI: top metric per service, trace p90, log error counts."""
    from .trace_scorer import latency_series
    import pandas as pd

    case = load_case(case_id)
    m_ev = score_metrics(case)
    out = {"inject_time": case.inject_time, "metrics": {}, "traces": {}, "logs": {}}
    df = case.metrics
    for s in services:
        if s in m_ev:
            col = m_ev[s][0].column
            out["metrics"][s] = {"column": col, "points": [[int(t), _r(v, 4)] for t, v in zip(df["time"], df[col])][:max_points]}
    out["traces"] = latency_series(case, services)
    if case.logs is not None and len(case.logs):
        from .log_scorer import classify_all
        from .loader import owner_of

        known = set(case.services)
        info = classify_all(pd.Series(case.logs["message"].unique()))
        sev = case.logs["message"].map(info["severe"])
        lg = case.logs[sev.to_numpy(bool)]
        svc = lg["container_name"].map(lambda c: owner_of(c, known))
        for s in services:
            g = lg[svc == s]
            if len(g):
                per = (g["timestamp"] // 10 * 10).value_counts().sort_index()
                out["logs"][s] = [[int(k), int(v)] for k, v in per.items()]
    return out
