"""Load one RCAEval failure case and clean up the known schema quirks.

The windowing follows RCAEval's own main.py: the last 5 minutes before the
fault injection time (normal) and the first 5 minutes after it (faulty).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

DATA_DIR = os.environ.get(
    "CHRONOTRACE_DATA", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
)

# RCAEval main.py uses length=10 minutes, split in half around the injection.
WINDOW_SECONDS = 300

SYSTEM_NAMES = {"ob": "online-boutique", "ss": "sock-shop", "tt": "train-ticket"}

# Columns that are mesh / host artefacts rather than application services.
_NON_SERVICE_PREFIXES = ("main", "PassthroughCluster", "frontend-external", "istio-proxy")
_IP_COLUMN = re.compile(r"^\d+[-.]\d+[-.]\d+[-.]\d+")
_STORE_SUFFIXES = ("-db", "-mongo", "-mysql", "-mongodb", "-redis")

# Trace serviceName values that differ from the metric/container name.
_TRACE_ALIASES = {"frontendservice": "frontend"}


@dataclass
class TelemetryCase:
    case_id: str
    dataset: str  # e.g. "RE2-OB"
    system: str  # "ob" | "ss" | "tt"
    inject_time: int
    metrics: pd.DataFrame  # cleaned, windowed, time column + metric columns
    raw_metrics: pd.DataFrame  # main.py-protocol frame, used by the baselines
    logs: Optional[pd.DataFrame] = None
    traces: Optional[pd.DataFrame] = None
    root_cause: Optional[str] = None
    fault: Optional[str] = None
    services: List[str] = field(default_factory=list)
    column_owner: Dict[str, str] = field(default_factory=dict)
    hygiene: Dict[str, object] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """False when the data itself has no normal or no faulty period."""
        return self.hygiene.get("normal_rows", 0) >= 30 and self.hygiene.get("faulty_rows", 0) >= 30

    @property
    def system_name(self) -> str:
        return SYSTEM_NAMES.get(self.system, self.system)

    @property
    def modalities(self) -> Dict[str, bool]:
        has_status = bool(
            self.traces is not None
            and "statusCode" in self.traces
            and self.traces["statusCode"].notna().any()
        )
        return {
            "metrics": True,
            "logs": self.logs is not None and len(self.logs) > 0,
            "traces": self.traces is not None and len(self.traces) > 0,
            "trace_status_codes": has_status,
        }


@lru_cache(maxsize=1)
def case_index() -> pd.DataFrame:
    path = os.path.join(DATA_DIR, "cases.parquet")
    idx = pd.read_parquet(path)
    idx["available"] = idx["case"].map(lambda c: os.path.exists(os.path.join(DATA_DIR, c, "metrics.parquet")))
    return idx


def raw_service(col: str) -> str:
    return col.split("_")[0]


# Shared stores whose owner is not visible in the name (service catalogue).
_STORE_OWNERS = {"redis": "cartservice", "session": "front-end", "session-db": "front-end"}


def owner_of(name: str, known: set) -> str:
    """Fold a datastore container into the service that owns it.

    carts-db -> carts, ts-auth-mongo -> ts-auth-service, redis -> cartservice (OB),
    session-db -> front-end (SS).
    """
    if name in _STORE_OWNERS and _STORE_OWNERS[name] in known:
        return _STORE_OWNERS[name]
    for suf in _STORE_SUFFIXES:
        if name.endswith(suf):
            stem = name[: -len(suf)]
            for cand in (stem, f"{stem}-service", f"{stem}service"):
                if cand in known:
                    return cand
            return stem
    return name


def is_service_column(col: str) -> bool:
    if col == "time" or col.startswith("time."):
        return False
    if _IP_COLUMN.match(col):
        return False
    return not raw_service(col).startswith(_NON_SERVICE_PREFIXES)


def normalise_trace_service(name: str, known: set) -> str:
    name = _TRACE_ALIASES.get(name, name)
    return owner_of(name, known)


def _protocol_frame(df: pd.DataFrame, inject_time: int, system: str) -> pd.DataFrame:
    """Reproduce RCAEval main.py metric preprocessing exactly."""
    df = df.loc[:, ~df.columns.duplicated()]
    if "time.1" in df:
        df = df.drop(columns=["time.1"])
    df = df.loc[:, ~df.columns.str.endswith("_latency-50")]
    if system == "tt":
        t = df["time"]
        df = df.loc[:, df.columns.str.startswith("ts-")].copy()
        df["time"] = t
    df = df.replace([np.inf, -np.inf], np.nan).ffill().fillna(0)
    normal = df[df["time"] < inject_time].tail(WINDOW_SECONDS)
    faulty = df[df["time"] >= inject_time].head(WINDOW_SECONDS)
    df = pd.concat([normal, faulty], ignore_index=True)
    return df.rename(columns={c: c.replace("_latency-90", "_latency") for c in df.columns if c.endswith("_latency-90")})


def load_case(case_id: str, with_logs: bool = True, with_traces: bool = True) -> TelemetryCase:
    folder = os.path.join(DATA_DIR, case_id)
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"case {case_id} not downloaded (looked in {folder})")
    suite, system = case_id[:3], case_id[3:5]
    dataset = f"{suite.upper()}-{system.upper()}"
    with open(os.path.join(folder, "inject_time.txt")) as f:
        inject_time = int(f.read().strip().split()[0])

    raw = pd.read_parquet(os.path.join(folder, "metrics.parquet"))
    n_raw_cols = raw.shape[1]
    dup_time = int(raw.columns.duplicated().sum()) + int("time.1" in raw)
    protocol = _protocol_frame(raw, inject_time, system)

    # Our own cleaned frame: protocol frame minus non-service columns.
    keep = ["time"] + [c for c in protocol.columns if is_service_column(c)]
    dropped = [c for c in protocol.columns if c not in keep]
    metrics = protocol[keep]

    raw_names = {raw_service(c) for c in metrics.columns if c != "time"}
    column_owner = {c: owner_of(raw_service(c), raw_names) for c in metrics.columns if c != "time"}
    folded = sorted({raw_service(c) for c in column_owner if raw_service(c) != column_owner[c]})

    parts = case_id.split("_")
    root_cause, fault = None, None
    idx = case_index()
    row = idx[idx["case"] == case_id]
    if len(row):
        root_cause = str(row.iloc[0]["root_cause_service"])
        fault = str(row.iloc[0]["fault"])
    elif len(parts) >= 4:
        root_cause, fault = "_".join(parts[1:-2]), parts[-2]

    t0, t1 = int(metrics["time"].min()), int(metrics["time"].max())
    logs = traces = None
    log_path = os.path.join(folder, "logs.parquet")
    if with_logs and os.path.exists(log_path):
        logs = pd.read_parquet(
            log_path,
            columns=["timestamp", "container_name", "message"],
            filters=[("timestamp", ">=", t0), ("timestamp", "<=", t1)],
        )
        logs["timestamp"] = logs["timestamp"].astype("int64")
        logs["container_name"] = logs["container_name"].fillna("unknown").astype(str)
        logs["message"] = logs["message"].fillna("").astype(str)
    trace_path = os.path.join(folder, "traces.parquet")
    if with_traces and os.path.exists(trace_path):
        cols = ["traceID", "spanID", "parentSpanID", "serviceName", "operationName", "startTime", "duration", "statusCode"]
        traces = pd.read_parquet(
            trace_path,
            columns=cols,
            filters=[("startTimeMillis", ">=", t0 * 1000), ("startTimeMillis", "<=", t1 * 1000)],
        )

    services = sorted(set(column_owner.values()))
    return TelemetryCase(
        case_id=case_id,
        dataset=dataset,
        system=system,
        inject_time=inject_time,
        metrics=metrics,
        raw_metrics=protocol,
        logs=logs,
        traces=traces,
        root_cause=root_cause,
        fault=fault,
        services=services,
        column_owner=column_owner,
        hygiene={
            "raw_metric_columns": n_raw_cols,
            "duplicate_time_columns_removed": dup_time,
            "non_service_columns_dropped": dropped,
            "datastores_folded": folded,
            "normal_rows": int((metrics["time"] < inject_time).sum()),
            "faulty_rows": int((metrics["time"] >= inject_time).sum()),
        },
    )


def list_cases(datasets: Optional[List[str]] = None, available_only: bool = True) -> List[str]:
    idx = case_index()
    if datasets:
        idx = idx[idx["dataset"].isin([d.upper() for d in datasets])]
    if available_only:
        idx = idx[idx["available"]]
    return idx["case"].tolist()
