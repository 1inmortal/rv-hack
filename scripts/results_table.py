"""Print markdown tables from results/benchmark.json (used for the README)."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "results", "benchmark.json")
rep = json.load(open(path))
t = rep["summary"]["datasets"]
ds = [d for d in t if d != "ALL"] + ["ALL"]


def f(v):
    return "–" if v is None else f"{v:.2f}"


print("### Headline (service level, RCAEval Evaluator)\n")
print("| Suite | Cases | ChronoTrace AC@1 | BARO AC@1 | N-Sigma AC@1 | ChronoTrace Avg@5 | BARO Avg@5 | ChronoTrace Lift@5 | BARO Lift@5 |")
print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
for d in ds:
    c, b, n = t[d]["chronotrace"], t[d]["baro"], t[d]["nsigma"]
    name = f"**{d}**" if d == "ALL" else d
    def best(vals):
        top = max(v for v in vals if v is not None)
        return [f"**{f(v)}**" if v is not None and round(v, 2) == round(top, 2) else f(v) for v in vals]

    a1 = best([c["AC@1"], b["AC@1"], n["AC@1"]])
    a5 = best([c["Avg@5"], b["Avg@5"]])
    l5 = best([c["Lift@5"], b["Lift@5"]])
    print(f"| {name} | {c['cases']} | {a1[0]} | {a1[1]} | {a1[2]} | {a5[0]} | {a5[1]} | {l5[0]} | {l5[1]} |")

print("\n### Retrieval / rerank decomposition (ChronoTrace vs BARO)\n")
print("| Suite | CT Retrieval@all | CT Rerank@1 | BARO Retrieval@all | BARO Rerank@1 | CT Chance@5 | BARO Chance@5 |")
print("|---|---:|---:|---:|---:|---:|---:|")
for d in ds:
    c, b = t[d]["chronotrace"], t[d]["baro"]
    print(f"| {d} | {f(c['Retrieval@all'])} | {f(c['Rerank@1'])} | {f(b['Retrieval@all'])} | {f(b['Rerank@1'])} | {f(c['Chance@5'])} | {f(b['Chance@5'])} |")

abl = ["chronotrace", "no_traces", "no_logs", "no_new_behaviour", "no_trace_gate", "metric_only", "metric_no_novelty"]
print("\n### Ablations (AC@1)\n")
print("| Variant | " + " | ".join(ds) + " |")
print("|---|" + "---:|" * len(ds))
for a in abl:
    print(f"| {a} | " + " | ".join(f(t[d][a]["AC@1"]) for d in ds) + " |")

print("\n### By fault type (all suites pooled)\n")
print("| Fault | Cases | ChronoTrace AC@1 | BARO AC@1 | ChronoTrace Avg@5 | BARO Avg@5 |")
print("|---|---:|---:|---:|---:|---:|")
for k, v in rep["summary"]["by_fault"].items():
    print(f"| {k} | {v['chronotrace']['cases']} | {f(v['chronotrace']['AC@1'])} | {f(v['baro']['AC@1'])} | {f(v['chronotrace']['Avg@5'])} | {f(v['baro']['Avg@5'])} |")
lat = rep["summary"]["latency_ms"]
print(f"\nAnalysis latency per case (file load excluded): median {lat['median']} ms, p90 {lat['p90']} ms; by suite {lat['by_dataset_median']}.")
print(f"Skipped (broken in the dataset): {', '.join(rep['skipped_unusable_cases']) or 'none'}.")
