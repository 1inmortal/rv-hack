"""Log scorer: local error bursts and new behaviour after the fault.

Code-level faults (RE3) often leave CPU and memory flat. Three rules keep the
log signal pointed at the faulty service rather than its callers:

1. Errors that describe a failed call to something else ("503 Service
   Unavailable", "rpc error: code = Unavailable", "failed to get ...") are
   evidence against the callee, so they count at a fraction of their weight.
2. Error templates that never appeared while healthy get a novelty multiplier,
   and ones carrying a real stack frame get a stack-trace multiplier.
3. A burst of templates the service never logged before (for example a restart
   after a bad deploy) is scored separately as "new behaviour", net of the
   template churn the service already showed while healthy.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .loader import TelemetryCase, owner_of

# Plain RE2-compatible patterns (no look-arounds) so pandas runs them
# vectorised over Arrow strings.
SEVERE = (
    r"(?i)(exception|traceback|\berror\b|\berr(or)?=|\bfatal\b|\bpanic\b|\bsevere\b|"
    r"\bfailed\b|\bfailure\b|unavailable|deadline.?exceeded|refused|timed? ?out|segmentation fault|"
    r"status(code)?[=: ]+5\d\d|HTTP/\d\.\d\"? 5\d\d|statuscode\.(internal|unknown|unavailable|deadline_exceeded))"
)
NULL_ERR = r"(?i)\berr(or)?=(null|nil|<nil>|none|\"\")"
STRONG_ERR = r"(?i)(exception|traceback|\berror\b|\bfatal\b|\bpanic\b|\bfailed\b)"
QUIET_LEVEL = r"(?i)(^.{0,60}\b(INFO|DEBUG|TRACE)\b|\"s\":\"I\"|level=(info|debug))"
STACK = (
    r"(?im)(traceback \(most recent call last\)|exception in thread|^\s*at [\w$.<>]+\([\w$]+\.(java|kt|scala):\d+\)|"
    r"file \"[^\"]+\.py\", line \d+|goroutine \d+ \[|\.go:\d+ \+0x|\.cs:line \d+)"
)
PROPAGATED = (
    r"(?i)(rpc error|code ?= ?(unavailable|deadline|internal|unknown)|service unavailable|bad gateway|gateway timeout|"
    r"\b50[234]\b|connection refused|connection reset|i/o timeout|no healthy upstream|upstream connect|"
    r"failed to (get|retrieve|send|charge|convert|complete|place|prepare|ship|connect|call|fetch|load|list|"
    r"query|reach)|request error|httpservererrorexception|httpclienterrorexception|resourceaccessexception|"
    r"remote|downstream)"
)
_MASKS = [
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "<EMAIL>"),
    (r"(?i)\b(bearer|token|jwt)[=: ]+\S+", "<TOKEN>"),
    (r"(?i)\b[0-9a-z]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<UUID>"),
    (r"\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b", "<IP>"),
    (r"\b(?:\d[ -]?){13,19}\b", "<CARD>"),
    (r"(?i)\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", "<HEX>"),
    (r"\b\d+(\.\d+)?\b", "<NUM>"),
    (r"\"[^\"]{20,}\"", "\"<STR>\""),
]
_MASKS_RE = [(re.compile(p), r) for p, r in _MASKS]

NOVEL_BOOST = 2.5
STACK_BOOST = 3.0
PROPAGATED_WEIGHT = 0.3
NEW_BEHAVIOUR_WEIGHT = 1.0


def scrub(message: str) -> str:
    """Mask PII and variable fields in one message (used for exported samples)."""
    out = message
    for pat, rep in _MASKS_RE:
        out = pat.sub(rep, out)
    return out


def template_of(message: str) -> str:
    first = message.strip().splitlines()[0] if message.strip() else ""
    return scrub(first[:400])[:160]


def classify_all(messages: pd.Series) -> pd.DataFrame:
    """Vectorised classification + templating of distinct messages."""
    m = messages.astype("string[pyarrow]")

    def has(pat: str) -> pd.Series:
        return m.str.contains(pat, regex=True).fillna(False).astype(bool)

    stack = has(STACK)
    sev_hit = has(SEVERE) & ~(has(NULL_ERR) & ~has(STRONG_ERR))
    severe = sev_hit & (stack | ~has(QUIET_LEVEL))
    propagated = severe & has(PROPAGATED)
    first = m.str.strip().str.replace(r"(?s)\n.*", "", regex=True).str.slice(0, 400)
    for pat, rep in _MASKS:
        first = first.str.replace(pat, rep, regex=True)
    return pd.DataFrame(
        {
            "severe": severe.to_numpy(bool),
            "stack": stack.to_numpy(bool),
            "propagated": propagated.to_numpy(bool),
            "template": first.str.slice(0, 160).fillna("").to_numpy(object),
        },
        index=pd.Index(messages.to_numpy(object)),
    )


@dataclass
class LogEvidence:
    service: str
    score: float
    error_score: float
    new_behaviour_score: float
    template: str
    sample: str  # scrubbed example (stack frames kept)
    pre_count: int
    post_count: int
    novel: bool
    stack_trace: bool
    propagated: bool
    onset: Optional[int]
    error_pre: int = 0
    error_post: int = 0
    new_templates: int = 0
    templates: List[Dict] = field(default_factory=list)


def score_logs(case: TelemetryCase, new_behaviour: bool = True) -> Dict[str, LogEvidence]:
    logs = case.logs
    if logs is None or len(logs) == 0:
        return {}
    it = case.inject_time
    known = set(case.services)

    # Template each distinct message once; logs repeat heavily.
    uniq = pd.Series(logs["message"].unique())
    info = classify_all(uniq)

    df = pd.DataFrame(
        {
            "service": logs["container_name"].map(lambda c: owner_of(c, known)).to_numpy(),
            "ts": logs["timestamp"].to_numpy(),
            "message": logs["message"].to_numpy(),
        }
    )
    df = df.join(info, on="message")
    df["post"] = df["ts"] >= it

    pre_minutes = max((it - int(df["ts"].min())) / 60.0, 1.0)
    post_minutes = max((int(df["ts"].max()) - it) / 60.0, 1.0)

    out: Dict[str, LogEvidence] = {}
    for svc, g in df.groupby("service", sort=False):
        pre_g = g[~g["post"]]
        pre_tpl = set(pre_g["template"])
        post_g = g[g["post"]]
        new_tpls = set(post_g["template"]) - pre_tpl if len(pre_tpl) else set()
        # Baseline churn: templates in the 2nd half of the healthy window that the
        # 1st half never showed. Only novelty above that counts.
        mid = it - (it - int(df["ts"].min())) / 2
        churn = len(set(pre_g.loc[pre_g["ts"] >= mid, "template"]) - set(pre_g.loc[pre_g["ts"] < mid, "template"]))
        excess = max(len(new_tpls) - churn, 0)
        new_score = math.log1p(excess) if new_behaviour else 0.0

        best = None
        entries = []
        errs = g[g["severe"]]
        for tpl, t in errs.groupby("template", sort=False):
            pre_n = int((~t["post"]).sum())
            post_n = int(t["post"].sum())
            if post_n == 0:
                continue
            ratio = (post_n / post_minutes + 1.0) / (pre_n / pre_minutes + 0.1)
            if ratio <= 1.0:
                continue
            novel = pre_n == 0
            stack = bool(t["stack"].any())
            propagated = bool(t["propagated"].all())
            s = math.log1p(ratio)
            s *= NOVEL_BOOST if novel else 1.0
            s *= STACK_BOOST if (stack and not propagated) else 1.0
            s *= PROPAGATED_WEIGHT if propagated else 1.0
            entries.append(
                {"template": tpl, "pre": pre_n, "post": post_n, "novel": novel, "stack_trace": stack,
                 "propagated": propagated, "score": round(s, 3)}
            )
            if best is None or s > best[0]:
                rows = t[t["post"]]
                pick = rows[rows["stack"]] if stack else rows
                best = (s, tpl, str(pick["message"].iloc[0]), pre_n, post_n, novel, stack, propagated, int(rows["ts"].min()))

        err_score = best[0] if best else 0.0
        total = err_score + NEW_BEHAVIOUR_WEIGHT * new_score
        if total <= 0:
            continue
        if best is None:
            first_new = post_g[post_g["template"].isin(new_tpls)]
            sample = str(first_new["message"].iloc[0]) if len(first_new) else ""
            best = (0.0, next(iter(sorted(new_tpls)), ""), sample, 0, len(first_new), True, False, False,
                    int(first_new["ts"].min()) if len(first_new) else None)
        out[svc] = LogEvidence(
            service=svc,
            score=float(total),
            error_score=float(err_score),
            new_behaviour_score=float(new_score),
            template=best[1],
            sample=scrub(best[2])[:600],
            pre_count=best[3],
            post_count=best[4],
            novel=best[5],
            stack_trace=best[6],
            propagated=best[7],
            onset=best[8],
            error_pre=int((~errs["post"]).sum()),
            error_post=int(errs["post"].sum()),
            new_templates=len(new_tpls),
            templates=sorted(entries, key=lambda e: -e["score"])[:5],
        )
    return out
