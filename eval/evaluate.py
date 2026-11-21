"""Honest evaluation on a synthetic holdout.

The ranker never sees outcomes. This script ranks the holdout file with the same pipeline the app uses,
and only then joins the hidden outcomes to count what the first N contacts would have reached.
This is a synthetic demonstration, not evidence of production performance.

Run: python -m eval.evaluate
"""
from __future__ import annotations

import csv
import dataclasses
import random
from pathlib import Path

from app import db, ingest, queue
from app.config import get_settings

DATA = Path(__file__).resolve().parent.parent / "data"


def rank_file(path: Path, settings=None, capacity: int | None = None):
    s = settings or get_settings()
    s = dataclasses.replace(s, exploration_share=0.0)  # measure the ranking itself; exploration is a live-ops choice
    conn = db.connect(":memory:")
    ingest.import_file(conn, path.read_bytes(), path.name, "eval")
    queue.reform(conn, s, "evaluation run")
    accounts = [dict(r) for r in conn.execute("SELECT account_ref,row_index,position,lane,state,points FROM accounts")]
    return accounts


def load_outcomes(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return {r["account_ref"]: r for r in csv.DictReader(f)}


def _tally(refs, outcomes):
    payers = inc = already = 0
    rupees = 0.0
    for ref in refs:
        o = outcomes.get(ref)
        if not o:
            continue
        if o["pays_if_contacted"] == "1":
            payers += 1
            rupees += float(o["amount_recovered_if_contacted"])
            if o["pays_without_contact"] == "0":
                inc += 1
        if o["already_paid"] == "1":
            already += 1
    return {"contacts": len(refs), "payers_reached": payers, "payers_who_needed_the_call": inc,
            "rupees_recovered": round(rupees), "calls_wasted_on_already_paid": already}


def evaluate(book: str = "holdout", capacity: int | None = None) -> dict:
    s = get_settings()
    n = capacity or s.daily_call_capacity
    accounts = rank_file(DATA / f"borrowers_{book}.csv", s)
    outcomes = load_outcomes(DATA / f"outcomes_{book}_hidden.csv")
    file_order = [a["account_ref"] for a in sorted(accounts, key=lambda a: a["row_index"])]
    gated = [a["account_ref"] for a in sorted(accounts, key=lambda a: a["row_index"]) if a["lane"] == "call_now"]
    ranked = [a["account_ref"] for a in sorted([a for a in accounts if a["position"]], key=lambda a: a["position"])]
    rand = []
    for seed in range(20):
        pool = file_order[:]
        random.Random(seed).shuffle(pool)
        rand.append(_tally(pool[:n], outcomes))
    rand_avg = {k: round(sum(r[k] for r in rand) / len(rand), 1) for k in rand[0]}
    res = {
        "book": book, "capacity": n, "accounts": len(accounts), "eligible_for_call": len(ranked),
        "label": "Synthetic demonstration. Not evidence of production performance.",
        "original_file_order": _tally(file_order[:n], outcomes),
        "file_order_after_safety_gates": _tally(gated[:n], outcomes),
        "random_order_avg_of_20": rand_avg,
        "kramsetu_ranked": _tally(ranked[:n], outcomes),
        "reminder_only_lane": sum(1 for a in accounts if a["lane"] == "reminder_only"),
        "held_or_excluded": sum(1 for a in accounts if a["lane"] is None),
    }
    base = res["original_file_order"]["payers_who_needed_the_call"] or 1
    res["lift_vs_file_order"] = round(res["kramsetu_ranked"]["payers_who_needed_the_call"] / base, 2)
    return res


if __name__ == "__main__":
    for book in ("holdout", "main"):
        r = evaluate(book)
        print(f"\n== {book}: first {r['capacity']} contacts of {r['accounts']} accounts ({r['label']})")
        for k in ("original_file_order", "file_order_after_safety_gates", "random_order_avg_of_20", "kramsetu_ranked"):
            print(f"  {k:32s} {r[k]}")
        print(f"  lift vs file order (payers who needed the call): {r['lift_vs_file_order']}x")
