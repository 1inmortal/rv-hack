# ChronoTrace — Diagnose the Outage

**Deterministic multi-source root cause analysis for microservice outages, benchmarked on RCAEval.**
ChronoTrace reads the metrics, logs and traces around an incident and ranks which service broke,
what broke in it, and how the failure spread. No language model takes part in that ranking.
An explanation layer (Gemma 4 via the Gemini API, or a local Ollama model) then turns the sealed,
hashed evidence into an SRE runbook, and every name and command in that runbook is checked against the evidence.

Built for Hacktoberfest Hack Day, Track 2 (Cloud — Diagnose the Outage). MIT licence.

---

## Results on RCAEval (643 real failure cases)

Service-level ranking, scored by RCAEval's own `Evaluator` class, using the same 10-minute window
as RCAEval's `main.py`. BARO and N-Sigma are the **official RCAEval implementations** run on the
same windows. Reproduce with `python scripts/run_benchmark.py`.

| Suite | Cases | ChronoTrace AC@1 | BARO AC@1 | N-Sigma AC@1 | ChronoTrace Avg@5 | BARO Avg@5 | ChronoTrace Lift@5 | BARO Lift@5 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| RE1-OB | 123 | **0.79** | 0.73 | 0.72 | **0.92** | 0.90 | 0.62 | **0.67** |
| RE1-SS | 125 | **0.87** | 0.83 | 0.86 | **0.97** | 0.95 | 0.67 | **0.68** |
| RE1-TT | 125 | **0.69** | 0.59 | 0.54 | **0.87** | 0.81 | **0.80** | 0.77 |
| RE2-OB | 90 | **0.97** | 0.13 | 0.80 | **0.98** | 0.73 | **0.68** | 0.48 |
| RE2-SS | 90 | 0.82 | 0.13 | **0.84** | **0.93** | 0.78 | **0.63** | 0.51 |
| RE3-OB | 30 | **1.00** | 0.00 | 0.90 | **1.00** | 0.77 | **0.71** | 0.52 |
| RE3-SS | 30 | **0.87** | 0.00 | 0.77 | **0.97** | 0.73 | **0.68** | 0.46 |
| RE3-TT | 30 | **0.97** | 0.87 | 0.87 | **0.99** | 0.95 | **0.92** | 0.91 |
| **ALL** | 643 | **0.84** | 0.49 | 0.76 | **0.94** | 0.84 | **0.69** | 0.64 |

**How to read this honestly**

- The big gains are on multi-source data (RE2/RE3). On RE1 (metrics only) BARO is already strong, and ChronoTrace is ahead by 4–10 points of top-1 accuracy.
- N-Sigma beats ChronoTrace on RE2-SS top-1 (0.84 vs 0.82). See [Limitations](#limitations).
- Lift@5 = Avg@5 − Chance@5, where chance is computed over each method's own candidate set. ChronoTrace folds datastores into their owning service (`carts-db` → `carts`), so its pool is smaller and its chance floor higher (0.24 vs 0.21 overall). That is why BARO's Lift@5 is higher on RE1-OB and RE1-SS even though its accuracy is lower. Pooled over all cases, ChronoTrace's Lift@5 (0.695) and N-Sigma's (0.696) are tied.
- Retrieval@all is ~1.0 for every method (every service is ranked), so Rerank@1 equals AC@1 here. The difference between methods is entirely in *ordering*, not retrieval.
- Not run: **RE2-TT** (90 cases, 2 GB of traces) did not fit on the build machine. Two RE1-OB cases are excluded for every method because the data itself is broken (`re1ob_currencyservice_loss_1` has a truncated injection timestamp; `re1ob_productcatalogservice_cpu_3` ends before its injection).

### What each idea contributes (ablations, AC@1)

| Variant | RE1-OB | RE1-SS | RE1-TT | RE2-OB | RE2-SS | RE3-OB | RE3-SS | RE3-TT | ALL |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **ChronoTrace** | 0.79 | 0.87 | 0.69 | 0.97 | 0.82 | 1.00 | 0.87 | 0.97 | **0.84** |
| without traces | 0.79 | 0.87 | 0.69 | 0.68 | 0.82 | 0.83 | 0.87 | 0.97 | 0.79 |
| without logs | 0.79 | 0.87 | 0.69 | 0.97 | 0.82 | 1.00 | 0.83 | 0.97 | 0.84 |
| without new-template signal | 0.79 | 0.87 | 0.69 | 0.97 | 0.71 | 1.00 | 0.87 | 0.87 | 0.82 |
| without trace gate | 0.79 | 0.87 | 0.69 | 0.86 | 0.82 | 1.00 | 0.87 | 0.97 | 0.82 |
| metrics only | 0.79 | 0.87 | 0.69 | 0.76 | 0.82 | 0.73 | 0.83 | 0.97 | 0.79 |
| metrics only, no novelty filter | 0.76 | 0.86 | 0.65 | 0.54 | 0.37 | 0.13 | 0.50 | 0.90 | 0.64 |

The single most important idea is the **novelty filter** in the metric scorer (0.64 → 0.79 overall).
The trace scorer is what takes Online Boutique from 0.76 / 0.73 to 0.97 / 1.00. Logs help little
on their own in this table; on RE2 resource faults, dropping them slightly *raises* Avg@5
(0.976 → 0.987 on RE2-OB), which is listed as an open issue.

By fault type (all suites pooled), top-1: cpu 0.90 (BARO 0.58), mem 0.96 (0.75), disk 0.94 (0.57),
socket 0.87 (0.00), delay 0.64 (0.45), loss 0.63 (0.44), code faults F1–F5 0.89–1.00 (0.00–0.54).
Network delay/loss without traces (Sock Shop, RE1) is the weakest area.

Analysis time per case, file loading excluded, on a laptop: median 350 ms; 70–330 ms on RE1;
0.5–1.7 s on RE2/RE3, where traces have 30k–400k spans.

Full tables, including AC@3, AC@5, Chance@5 and the per-fault breakdown: `results/benchmark.json`,
the dashboard's **Benchmark** tab, or `python scripts/results_table.py`.

---

## Why existing approaches fail, and the fix for each

| Failure seen on real RCAEval cases | Fix in ChronoTrace |
|---|---|
| BARO ranks a metric by its single largest post-fault z-score. A bursty series with zero spread before the fault (Redis `diskio`: 0 most of the time, 85,000 during snapshots) gets a z of ~100,000 and wins rank 1. BARO's top-1 is 0.00–0.13 on RE2/RE3 for this reason. | **Novelty filter:** the score is multiplied by the share of faulty-window points above the highest (smoothed) level the series already reached while healthy. A series that hit the same peaks before the fault scores ~0. Plus 3-point median smoothing and an upper-half "sustained drift" score instead of the maximum. |
| Delay injected into one service inflates the total span time of every caller on the path, so callers look guilty. | **Exclusive time:** each server span's duration minus the time spent waiting on its children. |
| Netem delay on a pod also slows that pod's *outgoing* calls, so its callees' client-side latency rises too. | **Edge-gap scoring:** for every call edge, the client span minus the callee's server span. A service is blamed in proportion to how many of its edges slowed *and* what share of its edges that is. The faulty pod has all its edges slow; a callee has one. |
| Uninstrumented services (Online Boutique's adservice and cartservice) emit no spans. | Client spans name their callee (`hipstershop.AdService/GetAds`), so the gap is charged to that callee. |
| Code faults (RE3) leave CPU and memory flat; callers log many generic "request failed" lines while the faulty service logs a few new stack traces. | **Log scorer:** error templates that are new get ×2.5, templates with a real stack frame get ×3, and "call to X failed" errors (`503`, `rpc error: Unavailable`, `failed to get …`) count ×0.3 because they implicate the callee. A burst of templates never seen before (for example a restart after a bad deploy) is scored separately, net of the service's normal template churn. |
| Modalities disagree, and some are missing (Sock Shop has no traces; Train Ticket spans have no status codes). | **Decisiveness gate:** each modality's scores are normalised, then weighted by its top-1/top-2 margin in *this* incident. Traces with status codes count double. Missing modalities drop out. No per-dataset weights. |

Nothing is trained, and nothing knows which five services RCAEval injects faults into: every service
in the telemetry is scored symmetrically, from that case's data alone.

---

## Architecture

```
RCAEval case (metrics.parquet, logs.parquet, traces.parquet, inject_time.txt)
  │
  ▼  loader.py         window = 5 min before / 5 min after injection (RCAEval main.py protocol)
  │                    drop duplicate time cols, mesh/IP columns; fold datastores into owners
  ├─ metric_scorer.py  robust z, smoothing, sustained drift × KS shift × novelty
  ├─ log_scorer.py     vectorised templating + PII masking, local vs propagated errors, new templates
  ├─ trace_scorer.py   server exclusive time, per-edge network gap, call graph
  ▼
  engine.py            decisiveness-gated fusion → ranking, roles, what/where/why,
                       rule-based next steps, input SHA-256 + evidence SHA-256
  │
  ══════════ determinism firewall: nothing below can change anything above ══════════
  │
  ▼  explainer.py      Gemma 4 (Gemini API) → local Ollama → deterministic template
                       schema-locked JSON; every service, metric, edge and command verified
  ▼
  api/main.py          FastAPI: /api/cases /api/diagnose/{id} /api/series/{id} /api/explain/{id} /api/benchmark
  dashboard/           war-room UI (plain JS + SVG, no build step)
```

How the firewall is enforced:

- `chronotrace/engine.py` does not import the explainer or any LLM SDK. A test (`test_engine_does_not_import_llm_code`) checks this, and `/api/health` reports `llm_loaded: false` until the first explain call.
- The benchmark never touches the explainer, so the numbers above do not depend on any model or API key.
- The explainer receives a compact copy of the finished bundle, including its `evidence_sha256`, and returns that hash with the report.
- `verify()` rejects a model answer that names a service or metric outside the evidence, invents a call edge that is not in the traced call graph, puts a service on the propagation path that the engine did not, targets another namespace, or proposes a destructive command. A rejected answer is replaced by the deterministic template, and the rejection reasons are shown in the UI. In testing, a local Llama 3.2 3B model invented call edges and was correctly rejected.

---

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate                    # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install --no-deps RCAEval==1.8.1       # official Evaluator + BARO/N-Sigma; its pinned deps are not needed

python scripts/download_data.py            # ~1.5 GB: RE1, RE2-OB, RE2-SS, RE3 (add --suites re2tt for +2 GB)
python scripts/run_benchmark.py            # ~15 min on a laptop; writes results/benchmark.json
uvicorn api.main:app --port 8000           # open http://localhost:8000
pytest -q
```

Explanation layer (optional; without it the deterministic template is used):

```bash
$env:GEMINI_API_KEY = "..."      # PowerShell; bash: export GEMINI_API_KEY=...  — Gemma 4 via the Gemini API (model: gemma-4-26b-a4b-it)
# or, fully local:
ollama pull gemma4
$env:CHRONOTRACE_LLM = "ollama"           # CHRONOTRACE_OLLAMA_MODEL to use another local model
```

| Variable | Default | Meaning |
|---|---|---|
| `CHRONOTRACE_DATA` | `./data` | RCAEval download folder |
| `CHRONOTRACE_LLM` | `auto` | `auto`, `gemini`, `ollama`, or `template` |
| `CHRONOTRACE_GEMINI_MODEL` | `gemma-4-26b-a4b-it` | Gemini API model id |
| `CHRONOTRACE_OLLAMA_MODEL` | `gemma4` | local Ollama model |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server |

---

## Demo script (3 minutes)

1. **`re2ob_checkoutservice_delay_1`** (showcase list). BARO's #1 is `redis` (top metric `redis_diskio`, the bursty-flush trap). ChronoTrace says `checkoutservice`, network category. In the call graph, every edge touching checkoutservice is red dashed: delay on that pod slows its calls in both directions. Its callees each have only one slow edge. Tick **Show ground truth**.
2. **`re3ob_adservice_f5_1`**. A code fault in an uninstrumented Java service. The **What** card shows the new stack trace (`java.io.IOException: Failed to bind`), PII-masked. Click **Generate runbook**: provider, model, "verified against evidence" and the evidence hash are shown on the result.
3. **`re2ss_carts_cpu_1`**. Sock Shop has no traces: the modality gate shows trace = 0, and metrics carry the decision.
4. **Benchmark tab**: AC@1 by suite against the official baselines, with the ablations below it.

---

## Limitations

- **Sock Shop victims.** Sock Shop has no traces, so there is no call graph. When `carts` or `user` is slowed, `orders` (which calls them) often shows a larger latency jump and is ranked first. Most of the RE2-SS misses are this pattern. Inferring edges from logs or lagged metric correlation is the next step (see CONTRIBUTING).
- **Logs on resource faults.** On RE2 CPU/memory/disk faults, log evidence is mostly noise; removing it slightly improves Avg@5 there. The gate limits the damage, but a better log relevance test is open.
- **Restart signal.** In RE3 the buggy build is deployed, so the faulty service restarts and logs new startup lines. ChronoTrace uses "new log templates after the fault" as evidence, as an SRE would correlate with a deploy. This helps RE2-SS and RE3-TT (see ablation). It is a real-world signal, but it is also a property of how RE3 injects faults.
- **Tuning.** The few constants (smoothing width, ×2.5 / ×3 / ×0.3 log multipliers, ×2 trace weight) were chosen while looking at these suites; there is no separate held-out split. The trace weight is insensitive in the range 1.5–3 (RE2-OB AC@1 0.96–0.97).
- **Latency** is 0.5–1.7 s per incident on RE2/RE3 on a laptop, not the sub-350 ms the original plan targeted; the median over all cases is 350 ms.
- **Baselines.** RCD and CIRCA were not run: RCAEval's RCD needs its patched causal-learn fork. Only BARO and N-Sigma are compared.
- **Gemma 4 path** is implemented against the `google-genai` SDK but was not exercised during development (no API key on the build machine). The Ollama path was tested with a local Llama 3.2 3B model, and the template path is covered by tests.

---

## Repository layout

```
chronotrace/loader.py         RCAEval loading, windowing, schema hygiene, datastore folding
chronotrace/metric_scorer.py  sustained drift + novelty filter
chronotrace/log_scorer.py     templating, PII masking, local/propagated errors, new templates
chronotrace/trace_scorer.py   exclusive time, edge gaps, call graph
chronotrace/engine.py         fusion, evidence bundle, hashes, remediation rules
chronotrace/explainer.py      Gemma 4 / Ollama / template + verification
chronotrace/baselines.py      official RCAEval BARO and N-Sigma
chronotrace/evaluation.py     RCAEval Evaluator wrapper
chronotrace/benchmark.py      variants, ablations, summary
api/main.py                   FastAPI backend + static dashboard
dashboard/                    UI (index.html, app.js, styles.css)
scripts/                      download_data.py, run_benchmark.py, results_table.py
tests/test_core.py            unit + integration tests
results/benchmark.json        the run reported above
```

Data: [RCAEval](https://github.com/phamquiluan/RCAEval) (Pham et al., WWW 2025), Hugging Face
`phamquiluan/RCAEval`, MIT licence.
