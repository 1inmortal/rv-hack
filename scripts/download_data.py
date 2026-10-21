"""Download RCAEval suites from Hugging Face (Parquet, MIT licence).

    python scripts/download_data.py                  # RE1 + RE2-OB + RE2-SS + RE3 (~1.5 GB)
    python scripts/download_data.py --suites re2tt   # add Train Ticket RE2 (~2 GB)
    python scripts/download_data.py --suites re3ob   # just one suite (~140 MB)
"""
import argparse
import os

from huggingface_hub import snapshot_download

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = ["re1ob", "re1ss", "re1tt", "re2ob", "re2ss", "re3ob", "re3ss", "re3tt"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suites", nargs="+", default=DEFAULT, help="e.g. re2ob re3ob re2tt")
    ap.add_argument("--dir", default=os.path.join(ROOT, "data"))
    args = ap.parse_args()
    patterns = ["cases.parquet", "README.md"] + [f"{s.lower().replace('-', '')}_*" for s in args.suites]
    snapshot_download(repo_id="phamquiluan/RCAEval", repo_type="dataset", allow_patterns=patterns, local_dir=args.dir, max_workers=8)
    print("downloaded to", args.dir)


if __name__ == "__main__":
    main()
