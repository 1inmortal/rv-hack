# What's left to win the hack

Handover for whoever picks this up next. Read the README first: it has the results, architecture and how to run everything. This file covers what still needs doing, in priority order.

**Where we are:** the full pipeline works end to end on 643 real RCAEval cases. Top-1 accuracy is 0.84 overall, against 0.49 for the official BARO and 0.76 for N-Sigma. The pipeline covers data loading, three scorers, fusion, the evidence bundle, the explanation layer with its verification checks, the API, the dashboard, the benchmark, 12 tests and CI.

---

## 0. Check first: the "built on the day" rule

The event brief says every project must be built on the day of the event and published to a public repo. This code already exists and the git history will show it. **Before the event, ask the organisers** how they treat pre-existing work. Then decide with the team how to handle it: for example, present this as the base and build the day's features (sections 2–3 below) live, with clear commits. Don't leave this to chance; it's the one thing that could disqualify us regardless of quality.

---

## 1. Must do before submission (about 3 hours)

| # | Task | Why | How |
|---|---|---|---|
| 1 | **Get a Gemini API key and test the Gemma 4 path** | Needed for the *Best Use of Gemma 4* prize. The code exists but has never run against the real API. | Free key at aistudio.google.com. Set `$env:GEMINI_API_KEY="..."`, start the server, click **Generate runbook** on 3–4 showcase cases. Check the result says `provider: gemini` and "verified". If the API rejects JSON mode, the code already retries without it. If the model id is wrong, set `CHRONOTRACE_GEMINI_MODEL`. |
| 2 | **Pull Gemma 4 into Ollama for an offline backup** | Venue Wi-Fi can fail mid-demo. | `ollama pull gemma4` (several GB; needs disk space), then `$env:CHRONOTRACE_LLM="ollama"`. If it's not possible, the deterministic template still works offline. |
| 3 | **Make the CI badge green** | Judges open the repo; a red CI looks bad. | Check the Actions tab on GitHub after the first push. CI downloads RE3-OB (140 MB) and runs pytest plus a small benchmark. Fix anything that breaks on Linux. |
| 4 | **5-slide deck** | Required for judging. | Use the *real* numbers from the README, not the ones in the original strategy doc; many of those were never measured. Slides: problem → why BARO/LLMs fail (show the `redis_diskio` trap) → our 3 fixes → live demo + results table → limitations and next steps. |
| 5 | **Record a backup demo video (2–3 min)** | If the laptop or projector fails. | Screen-record the demo script in the README. |
| 6 | **Rehearse the 3-minute demo three times** | Timing. | Follow the "Demo script" section in the README. Start the server *before* walking up (`uvicorn api.main:app --port 8000`) and open each showcase case once so it's cached (the first load of a case takes ~2 s). |
| 7 | **Repo polish** | Hacktoberfest judges look at open-source quality. | Add repo topics (`hacktoberfest`, `aiops`, `root-cause-analysis`, `gemma`) and a description. Open 4 issues labelled `hacktoberfest` + `good first issue` from the list in CONTRIBUTING.md. Add a screenshot of the dashboard to the README. |

---

## 2. Technical improvements, ranked by impact

Each one should finish with `python scripts/run_benchmark.py` and the README tables updated (`python scripts/results_table.py` prints them). **Report every suite, including ones that get worse.**

### 2.1 Run RE2-TT (90 cases), about 1 hour, mostly download time
It's the only RCAEval suite we haven't benchmarked, and a judge will notice. It needs ~2 GB of free disk (the build laptop didn't have it).
```
python scripts/download_data.py --suites re2tt
python scripts/run_benchmark.py
```
Train Ticket traces have no status codes, so the trace gate already treats them as incomplete. Watch the trace scorer's runtime: RE2-TT traces are large, and memory use may need checking.

### 2.2 Call graph for Sock Shop (no traces), the biggest accuracy gap
Most RE2-SS misses follow one pattern: `carts`, `user` or `payment` is slowed, and `orders` (which calls them) shows a bigger latency jump and gets ranked first. N-Sigma beats us here (0.84 vs 0.82).
- Idea A: infer `caller → callee` edges from lagged correlation of latency series (does `orders_latency` follow `carts_latency` by 0–2 s?).
- Idea B: parse service names or URLs out of log lines (`orders` logs mention calls to `carts`).
- Once edges exist, the engine already labels upstream victims. Add a down-weighting step for a caller whose latency rise is explained by an anomalous callee. A version of this was tried with traces and didn't help there, but Sock Shop is a different situation; measure it.
- Files: `chronotrace/trace_scorer.py` (or a new `graph_inference.py`) and `engine.py` (the `edges` used by `_propagation`).

### 2.3 Delay/loss faults without traces, the weakest fault type
Pooled top-1 is 0.64 for delay and 0.63 for loss, mainly on RE1 and Sock Shop. The same victim problem as 2.2 applies. Also check whether network metrics (`*_socket`, `*_error`) can be scored more strongly for these faults.

### 2.4 Relevance gate for logs on resource faults
On RE2 CPU/memory/disk faults, removing logs slightly *improves* Avg@5 (RE2-OB 0.976 → 0.987). Idea: if no service has a novel local error or stack trace, set the log weight to zero. Test it with the ablation table.

### 2.5 Speed: trace scorer to under 500 ms
RE2-OB takes ~1.7 s per case, almost all of it in `trace_scorer.py`: the pandas `groupby("sec").quantile(0.9)` per service and per edge, run twice. Compute the per-second p90 for all groups in one `groupby([key, "sec"])` call, and reuse the span frame between `score_traces` and `latency_series`. Report the new latency in the README.

### 2.6 Add RCD / CIRCA baselines
Judges will ask "what about RCD?". RCAEval's RCD needs its patched `causal-learn` fork (see RCAEval's own install docs); on Python 3.11 the import worked but RCD itself failed (`local_skeleton_discovery` missing). Try a Python 3.10 venv with RCAEval's pinned requirements, run only those baselines, and add their columns to `chronotrace/baselines.py` and the benchmark.

### 2.7 Answer "did you overfit?"
The few constants (log multipliers ×2.5/×3/×0.3, trace weight ×2, 3-point smoothing) were picked while looking at these suites. Strongest answer: a **leave-one-system-out** table. Pick constants on Online Boutique only, report Sock Shop and Train Ticket, and the reverse. Even a quick sensitivity table (vary each constant ±50%, show AC@1 barely moves) helps. The trace weight is already known to be flat between 1.5 and 3.

---

## 3. Nice-to-have (only if 1 and 2.1–2.2 are done)

- **Live mode:** a small OpenTelemetry/Prometheus adapter that builds a `TelemetryCase` from a running cluster, even as a stub. It makes "production ready" believable.
- **"Why BARO failed here" overlay:** the bundle already includes BARO's top-5 and top metric. Add a one-line explanation in the UI, e.g. "BARO picked `redis_diskio`: zero spread before the fault, periodic bursts".
- **Export the runbook** as Markdown/JSON from the UI.
- **Dark/light theme toggle** for projector visibility (the dark theme can wash out on some projectors; check on the venue screen).

---

## 4. Judge questions: honest answers with our numbers

| Question | Answer |
|---|---|
| Is the LLM doing the ranking? | No. `engine.py` never imports the explainer; a test enforces it, and `/api/health` shows `llm_loaded: false` until someone clicks "Generate runbook". The benchmark runs with no model or key at all. |
| What's new versus running RCAEval's baselines? | Three things, each shown in the ablation table: the novelty filter (0.64 → 0.79 overall), exclusive time plus per-edge network gap from traces (RE2-OB 0.76 → 0.97), and the log scorer that discounts "call to X failed" errors and boosts new stack traces. Plus decisiveness-gated fusion. |
| Do you exploit the fact that RCAEval only injects into 5 services per system? | No training and no service names in the code. Every service in the telemetry is scored the same way from that case's data. |
| Where does it fail? | Sock Shop delay/loss, where callers look worse than the slowed service because there are no traces to build a call graph. N-Sigma is better on RE2-SS top-1. |
| Didn't you tune on the test set? | Yes, a few constants, with no held-out split. Show the sensitivity / leave-one-system-out table (2.7) once it exists. |
| Is the "new log templates" signal cheating on RE3? | RE3 injects bugs by deploying a new build, so the service restarts. Correlating a fault with a recent restart or deploy is standard SRE practice. Its effect is reported separately in the ablation (`without new-template signal`). |
| How fast is it? | Median 350 ms per incident; 70–330 ms on metric-only suites; 0.5–1.7 s with traces of 30k–400k spans on a laptop. |
| How do you stop the LLM from hallucinating? | Schema-locked JSON, then `verify()`: every service, metric, call edge, namespace and command must come from the evidence. Otherwise the deterministic template is used and the rejection is shown in the UI. A local 3B model invented call edges during testing and was rejected. |

---

## 5. Setting up on a new machine

```bash
git clone https://github.com/darkhorse0204/rv-hack.git
cd rv-hack
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
pip install --no-deps RCAEval==1.8.1
python scripts/download_data.py        # ~1.5 GB; data/ is git-ignored
pytest -q
uvicorn api.main:app --port 8000
```

`results/benchmark.json` is committed, so the Benchmark tab works without re-running the benchmark. Re-run it after any algorithm change.

## 6. Where to change what

| Change | File |
|---|---|
| Metric scoring | `chronotrace/metric_scorer.py` (`score_column`) |
| Log rules, regexes, multipliers | `chronotrace/log_scorer.py` (top of file) |
| Trace / call graph | `chronotrace/trace_scorer.py` |
| Fusion, roles, fault categories, kubectl suggestions | `chronotrace/engine.py` (`fuse`, `_category`, `REMEDIATION`) |
| Prompt, providers, verification | `chronotrace/explainer.py` |
| Benchmark variants / ablations | `chronotrace/benchmark.py` (`score_case`) |
| API endpoints, showcase list | `api/main.py` (`SHOWCASE`) |
| UI | `dashboard/app.js`, `dashboard/styles.css` |
| Datastore → owner mapping | `chronotrace/loader.py` (`_STORE_OWNERS`, `owner_of`) |
