"""Explanation layer: turn a finished EvidenceBundle into an SRE runbook.

This module never sees raw telemetry and never changes the ranking. It gets the
bundle *after* the diagnosis (and its SHA-256) is final, asks a language model
to write the narrative as schema-locked JSON, and then verifies the answer:

* every service it names must be one the evidence names;
* every metric column it names must be one the evidence names;
* every command must be kubectl and target only the namespace and services in
  the evidence;
* the summary must name the diagnosed root cause.

If any check fails, or no model is reachable, a deterministic template built
from the same evidence is returned instead, and the reason is recorded.

Providers, in order for CHRONOTRACE_LLM=auto (default):
  gemini  - Gemma 4 via the Gemini API (needs GEMINI_API_KEY or GOOGLE_API_KEY)
  ollama  - a local Ollama model (CHRONOTRACE_OLLAMA_MODEL, default "gemma4")
  template
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import requests
from pydantic import BaseModel, Field, ValidationError

GEMINI_MODEL = os.environ.get("CHRONOTRACE_GEMINI_MODEL", "gemma-4-26b-a4b-it")
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("CHRONOTRACE_OLLAMA_MODEL", "gemma4")
TIMEOUT_S = float(os.environ.get("CHRONOTRACE_LLM_TIMEOUT", "60"))


class Step(BaseModel):
    command: str = Field(description="One kubectl command from the candidate list, adapted only if needed")
    rationale: str = Field(description="Why this step, in one sentence")


class RemediationReport(BaseModel):
    executive_summary: str = Field(description="Two sentences: what broke, where, and the impact")
    root_cause_reasoning: str = Field(description="Why the evidence points at this service and not its callers")
    causal_chain: List[str] = Field(description="Ordered hops from the origin service outwards")
    remediation_steps: List[Step]
    risk_notes: str = Field(description="What to double-check before acting; mention low confidence if flagged")


SYSTEM_PROMPT = (
    "You are an SRE incident assistant. You receive a JSON evidence bundle produced by a deterministic "
    "root-cause engine. The diagnosis is final: do not re-rank services, do not invent services, metrics, "
    "numbers or commands. Only explain the evidence and pick remediation steps from the candidate commands. "
    "Reply with JSON only, matching the requested schema."
)


def compact_evidence(bundle: Dict) -> Dict:
    d = bundle["diagnosis"]
    what = d["what"]
    log = what.get("log_evidence") or {}
    return {
        "case_id": bundle["case_id"],
        "system": bundle["system"],
        "namespace": bundle["system"],
        "root_cause": d["root_cause"],
        "confidence": d["confidence"],
        "ambiguous": d["ambiguous"],
        "top_services": [
            {k: r[k] for k in ("rank", "service", "score", "role", "metric", "log", "trace", "onset_sec_after_inject")}
            for r in d["where_service_ranks"][:5]
        ],
        "what": {
            "category": what["category"],
            "summary": what["summary"],
            "primary_indicator": what["primary_indicator"],
            "metric_evidence": what["metric_evidence"][:2],
            "log_evidence": {k: log.get(k) for k in ("template", "stack_trace", "novel", "pre_count", "post_count")}
            if log
            else None,
            "log_sample": (log.get("sample") or "")[:400] if log else None,
            "trace_evidence": what.get("trace_evidence"),
        },
        "why_causal_chain": d["why_causal_chain"],
        "candidate_commands": d["next_steps"]["commands"],
        "modality_share": {k: v.get("share") for k, v in bundle["modality_gate"].items()},
    }


def _user_prompt(ev: Dict) -> str:
    schema = json.dumps(RemediationReport.model_json_schema())
    return (
        f"Evidence bundle:\n{json.dumps(ev, indent=1)}\n\n"
        f"Write the incident report as JSON matching this schema:\n{schema}\n"
        "Rules: name the root cause service in executive_summary; causal_chain starts at the root cause; "
        "remediation_steps use the candidate_commands (2-4 steps)."
    )


# ---------------------------------------------------------------- providers


def _gemini(ev: Dict) -> Tuple[str, str]:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("no GEMINI_API_KEY / GOOGLE_API_KEY set")
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key)
    prompt = SYSTEM_PROMPT + "\n\n" + _user_prompt(ev)
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
                response_schema=RemediationReport,
            ),
        )
    except Exception:
        # Some hosted open-weight models reject JSON mode; ask for JSON in plain text.
        resp = client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt, config=types.GenerateContentConfig(temperature=0.2)
        )
    return resp.text or "", GEMINI_MODEL


def _ollama(ev: Dict) -> Tuple[str, str]:
    tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).json()
    names = {m["name"] for m in tags.get("models", [])} | {m["name"].split(":")[0] for m in tags.get("models", [])}
    if OLLAMA_MODEL not in names:
        raise RuntimeError(f"ollama model '{OLLAMA_MODEL}' not pulled (have: {sorted(names)})")
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "stream": False,
            "format": RemediationReport.model_json_schema(),
            "options": {"temperature": 0.2},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(ev)},
            ],
        },
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    return r.json()["message"]["content"], f"ollama:{OLLAMA_MODEL}"


# ---------------------------------------------------------------- guardrails

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse(text: str) -> RemediationReport:
    m = _JSON_BLOCK.search(text or "")
    if not m:
        raise ValueError("no JSON object in model output")
    return RemediationReport.model_validate(json.loads(m.group(0)))


def verify(report: RemediationReport, bundle: Dict) -> List[str]:
    """Return a list of problems; empty means the report is grounded in the evidence."""
    d = bundle["diagnosis"]
    problems: List[str] = []
    all_svcs = {r["service"] for r in d["where_service_ranks"]}
    allowed_svcs = {r["service"] for r in d["where_service_ranks"][:5]} | {c["service"] for c in d["why_causal_chain"]}
    tr_ev = d["what"].get("trace_evidence") or {}
    for edge in tr_ev.get("shifted_edges") or []:
        allowed_svcs |= set(edge.split("->"))
    for e in d.get("call_graph", []):
        if d["root_cause"] in (e["caller"], e["callee"]):
            allowed_svcs |= {e["caller"], e["callee"]}
    allowed_svcs &= all_svcs | {d["root_cause"]}
    cols = {e["column"] for e in d["what"]["metric_evidence"]}
    if d["what"].get("primary_indicator"):
        cols.add(d["what"]["primary_indicator"])
    text = " ".join(
        [report.executive_summary, report.root_cause_reasoning, report.risk_notes, *report.causal_chain]
        + [s.rationale for s in report.remediation_steps]
    )
    low = text.lower()
    if d["root_cause"].lower() not in report.executive_summary.lower():
        problems.append("executive_summary does not name the diagnosed root cause")
    for s in all_svcs - allowed_svcs:
        if re.search(rf"(?<![\w-]){re.escape(s.lower())}(?![\w-])", low):
            problems.append(f"mentions service '{s}' that is not in the evidence")
    for tok in set(re.findall(r"\b[a-z][\w-]*service\b", low)):
        if tok not in {x.lower() for x in all_svcs} and tok not in {"service", "microservice"}:
            problems.append(f"mentions unknown service-like name '{tok}'")
    prefixes = {c.split("_")[0] for c in cols}
    for tok in set(re.findall(r"\b[a-z][\w-]*_[a-z][\w-]*\b", low)):
        if tok.split("_")[0] in {s.lower() for s in all_svcs} | prefixes and tok not in {c.lower() for c in cols}:
            problems.append(f"mentions metric '{tok}' that is not in the evidence")
    if report.causal_chain and d["root_cause"].lower() not in report.causal_chain[0].lower():
        problems.append("causal_chain does not start at the root cause")
    chain_svcs = {c["service"].lower() for c in d["why_causal_chain"]}
    graph = {(e["caller"].lower(), e["callee"].lower()) for e in d.get("call_graph", [])}
    for hop in report.causal_chain:
        h = hop.lower()
        for s in all_svcs:
            if s.lower() not in chain_svcs and re.search(rf"(?<![\w-]){re.escape(s.lower())}(?![\w-])", h):
                problems.append(f"causal_chain names '{s}', which the engine did not place on the propagation path")
        for a, b in re.findall(r"([\w-]+)\s*(?:->|→|<-|←)\s*([\w-]+)", h):
            if (a, b) not in graph and (b, a) not in graph:
                problems.append(f"causal_chain edge {a}->{b} is not in the observed call graph")
    ns = bundle["system"]
    for st in report.remediation_steps:
        cmd = st.command.strip()
        if not cmd.startswith("kubectl "):
            problems.append(f"non-kubectl command: {cmd[:60]}")
            continue
        m_ns = re.findall(r"(?:^|\s)(?:-n|--namespace)[\s=]+([a-z][\w-]*)", cmd.split("|")[0])
        if any(x != ns for x in m_ns):
            problems.append(f"command targets another namespace: {cmd[:60]}")
        for s in all_svcs - allowed_svcs:
            if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", cmd):
                problems.append(f"command targets service outside the evidence: {cmd[:60]}")
        if re.search(r"\b(delete\s+(ns|namespace|deployment|svc|service)|drain|cordon|--all\b)", cmd):
            problems.append(f"destructive command not in candidates: {cmd[:60]}")
    if not report.remediation_steps:
        problems.append("no remediation steps")
    return problems


def template_report(bundle: Dict) -> RemediationReport:
    d = bundle["diagnosis"]
    root = d["root_cause"]
    what = d["what"]
    conf = d["confidence"]
    ranks = d["where_service_ranks"]
    second = ranks[1]["service"] if len(ranks) > 1 else None
    lines = [f"{root}: {what['summary'].lower()}"]
    if what.get("primary_indicator"):
        me = what["metric_evidence"][0]
        lines.append(
            f"{me['column']} moved from a median of {me['pre_median']} to {me['post_median']} "
            f"(sustained {me['sustained_z']} robust z)"
        )
    log = what.get("log_evidence")
    if log and log.get("template"):
        kind = "new stack trace" if log.get("stack_trace") else "new error template" if log.get("novel") else "error burst"
        lines.append(f"logs show a {kind}: \"{log['template'][:120]}\" ({log['pre_count']} before, {log['post_count']} after)")
    tr = what.get("trace_evidence")
    if tr and (tr.get("server_score") or tr.get("network_score")):
        lines.append(f"trace p90 {tr['p90_ms_before']} ms -> {tr['p90_ms_after']} ms")
        if tr.get("shifted_edges"):
            lines.append("slower call edges: " + ", ".join(tr["shifted_edges"][:4]))
    chain = [c["service"] + (f" (via {c['via']})" if c.get("via") else " (origin)") for c in d["why_causal_chain"]]
    victims = [r["service"] for r in ranks if r["role"] == "UPSTREAM_VICTIM"]
    reasoning = "; ".join(lines) + "."
    if victims:
        reasoning += f" {', '.join(victims)} also degraded, but only on calls into {root}."
    steps = [Step(command=c, rationale=f"Standard first check for a {what['category'].replace('_', ' ')} fault.") for c in d["next_steps"]["commands"]]
    risk = (
        f"Low confidence ({conf}); inspect {root} and {second} together before acting."
        if d["ambiguous"]
        else f"Confidence {conf}. Confirm on the live cluster before rolling back."
    )
    return RemediationReport(
        executive_summary=f"Root cause: {root} ({what['summary'].lower()}). Detected by the deterministic engine with confidence {conf}.",
        root_cause_reasoning=reasoning,
        causal_chain=chain,
        remediation_steps=steps,
        risk_notes=risk,
    )


def explain(bundle: Dict, provider: Optional[str] = None) -> Dict:
    provider = (provider or os.environ.get("CHRONOTRACE_LLM", "auto")).lower()
    ev = compact_evidence(bundle)
    order = {"auto": ["gemini", "ollama"], "gemini": ["gemini"], "ollama": ["ollama"], "template": []}.get(provider, ["gemini", "ollama"])
    attempts = []
    for name in order:
        t0 = time.perf_counter()
        try:
            raw, model = (_gemini if name == "gemini" else _ollama)(ev)
            report = _parse(raw)
            problems = verify(report, bundle)
            ms = round((time.perf_counter() - t0) * 1000)
            if problems:
                attempts.append({"provider": name, "model": model, "rejected": problems, "latency_ms": ms})
                continue
            return {
                "provider": name,
                "model": model,
                "verified": True,
                "latency_ms": ms,
                "attempts": attempts,
                "evidence_sha256": bundle["reproducibility"]["evidence_sha256"],
                "report": report.model_dump(),
            }
        except (ValidationError, ValueError, RuntimeError, requests.RequestException, KeyError, ImportError) as e:
            attempts.append({"provider": name, "error": f"{type(e).__name__}: {str(e)[:200]}"})
        except Exception as e:  # SDK-specific errors
            attempts.append({"provider": name, "error": f"{type(e).__name__}: {str(e)[:200]}"})
    report = template_report(bundle)
    return {
        "provider": "template",
        "model": "deterministic-template",
        "verified": not verify(report, bundle),
        "latency_ms": 0,
        "attempts": attempts,
        "evidence_sha256": bundle["reproducibility"]["evidence_sha256"],
        "report": report.model_dump(),
    }
