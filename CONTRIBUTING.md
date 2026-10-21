# Contributing to ChronoTrace

Thanks for helping. The one rule that matters: **nothing that ranks services may call a language model.**
The engine (`chronotrace/engine.py` and everything it imports) must stay deterministic; the
explanation layer (`chronotrace/explainer.py`) only reads a finished evidence bundle. A test
(`test_engine_does_not_import_llm_code`) enforces this.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install --no-deps RCAEval==1.8.1
python scripts/download_data.py --suites re3ob     # 140 MB, enough for the tests
pytest -q
```

## Changing the algorithm

Every change to a scorer or to fusion must come with the benchmark numbers before and after:

```bash
python scripts/run_benchmark.py --suite RE1-OB RE2-OB RE2-SS RE3-OB RE3-SS RE3-TT
```

Report all suites, not only the ones that improved. Do not add per-dataset or per-service
constants; the engine scores every candidate symmetrically and has no notion of which services
RCAEval injects faults into.

## Good first issues

- **OpenTelemetry adapter**: map OTLP spans/metrics/logs into `TelemetryCase` so the engine runs on a live cluster.
- **Prometheus adapter**: build the metrics frame from PromQL range queries.
- **Call graph without traces**: infer caller → callee edges for Sock Shop–style systems from log content or lagged metric correlation, so upstream victims can be labelled there too.
- **RE2-TT run**: download Train Ticket RE2 (2 GB) and add its numbers to the README.
- **RCD baseline**: wire RCAEval's RCD (needs its patched causal-learn) into `chronotrace/baselines.py`.

## Style

Plain Python, type hints, short docstrings that say *why*. Keep the dashboard dependency-free
(`dashboard/` is plain HTML/CSS/JS served by FastAPI).
