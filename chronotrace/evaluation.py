"""Score service rankings with RCAEval's official Evaluator class."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from RCAEval.benchmark.evaluation import Evaluator
from RCAEval.classes.graph import Node


def summarise(rows: Iterable[Dict], k: int = 5) -> Optional[Dict]:
    """rows: dicts with keys ranks (list[str]), answer (str), n_candidates (int)."""
    ev = Evaluator(max_k=k)
    n = 0
    for r in rows:
        ranks = [Node(s, "unknown") for s in r["ranks"]]
        ev.add_case(ranks=ranks, answer=Node(r["answer"], "unknown"), n_candidates=r.get("n_candidates"))
        n += 1
    if n == 0:
        return None
    rr = ev.rerank_service(1, None)
    out = {
        "cases": n,
        "AC@1": ev.accuracy_service(1),
        "AC@3": ev.accuracy_service(3),
        "AC@5": ev.accuracy_service(5),
        "Avg@5": ev.average_service(5),
        "Retrieval@all": ev.retrieval_service(None),
        "Rerank@1": rr,
    }
    chance = ev.chance_average(5)
    out["Chance@5"] = chance
    out["Lift@5"] = None if chance is None else out["Avg@5"] - chance
    return {k2: (round(v, 4) if isinstance(v, float) else v) for k2, v in out.items()}


def rank_of(ranks: List[str], answer: str) -> Optional[int]:
    return ranks.index(answer) + 1 if answer in ranks else None
