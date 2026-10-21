"""Reproduce every number in the README.

    python scripts/run_benchmark.py                       # all downloaded suites
    python scripts/run_benchmark.py --suite RE2-OB RE3-OB  # a subset
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chronotrace.benchmark import run  # noqa: E402

ALL = ["RE1-OB", "RE1-SS", "RE1-TT", "RE2-OB", "RE2-SS", "RE2-TT", "RE3-OB", "RE3-SS", "RE3-TT"]
SHOW = ["chronotrace", "no_traces", "no_logs", "no_new_behaviour", "no_trace_gate", "metric_only", "metric_no_novelty", "baro", "nsigma"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", nargs="+", default=ALL)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    rep = run(args.suite, args.out)
    table = rep["summary"]["datasets"]
    cols = ["AC@1", "AC@3", "Avg@5", "Retrieval@all", "Rerank@1", "Chance@5", "Lift@5"]
    for ds, methods in table.items():
        n = next(iter(methods.values()))["cases"]
        print(f"\n== {ds} ({n} cases) ==")
        print("method".ljust(20) + "".join(c.rjust(14) for c in cols))
        for m in SHOW:
            s = methods.get(m)
            if not s:
                continue
            print(m.ljust(20) + "".join((f"{s[c]:.3f}" if s[c] is not None else "n/a").rjust(14) for c in cols))
    lat = rep["summary"]["latency_ms"]
    print(f"\nanalysis latency per case: median {lat['median']} ms, p90 {lat['p90']} ms")
    if rep["skipped_unusable_cases"]:
        print("skipped (broken in the dataset itself):", ", ".join(rep["skipped_unusable_cases"]))
    if rep["not_downloaded_cases"]:
        print(f"{rep['not_downloaded_cases']} cases in the selected suites are not downloaded")


if __name__ == "__main__":
    main()
