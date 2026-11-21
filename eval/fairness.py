"""Fairness checks, kept outside the ranker.

1. Structural: the scorer's input record has no banned field.
2. Counterfactual: change name, area, language (and gender/religion/caste columns if present) on every
   account, keep permitted fields the same, and the points and order must be identical.
3. Proxy audit: can the points separate synthetic protected groups? AUC near 0.5 means no.
   In this synthetic data the attributes are independent of behaviour by construction, so the audit
   demonstrates the harness. It is not proof about any real portfolio.

Run: python -m eval.fairness
"""
from __future__ import annotations

import csv
import io
import random
from pathlib import Path

from app import db, ingest, queue, ranker
from app.config import get_settings

DATA = Path(__file__).resolve().parent.parent / "data"
SWAP_NAMES = ["Asha Rao", "Mohammed Ali", "Joseph Pinto", "Gurleen Kaur", "Karthik Subramanian", "Zainab Mirza"]
SWAP_AREAS = ["110001", "600001", "560001", "700001", "400001", "226001"]
SWAP_LANGS = ["Hindi", "Tamil", "Kannada", "Bengali", "Marathi", "English"]


def _rank(csv_text: str):
    s = get_settings()
    conn = db.connect(":memory:")
    ingest.import_file(conn, csv_text, "fairness.csv", "fair")
    queue.reform(conn, s, "fairness run")
    return {r["account_ref"]: (r["points"], r["position"], r["lane"], r["state"])
            for r in conn.execute("SELECT account_ref,points,position,lane,state FROM accounts")}


def counterfactual(path: Path | None = None, seed: int = 7) -> dict:
    path = path or DATA / "borrowers_main.csv"
    text = path.read_text(encoding="utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    fields = list(rows[0].keys())
    name_cols = [f for f in fields if ingest._ALIAS_INDEX.get(ingest.norm_header(f)) == "name"]
    area_cols = [f for f in fields if ingest._ALIAS_INDEX.get(ingest.norm_header(f)) == "area"]
    lang_cols = [f for f in fields if ingest._ALIAS_INDEX.get(ingest.norm_header(f)) == "language"]
    sens_cols = [f for f in fields if ingest.norm_header(f) in ingest.SENSITIVE]
    ref_cols = [f for f in fields if ingest._ALIAS_INDEX.get(ingest.norm_header(f)) == "account_ref"]
    swapped = []
    for r in rows:
        r2 = dict(r)
        # the same account always gets the same replacement, so duplicate rows stay duplicates
        rng = random.Random(f"{seed}|{r.get(ref_cols[0]) if ref_cols else ''}")
        for c in name_cols:
            r2[c] = rng.choice(SWAP_NAMES)
        for c in area_cols:
            r2[c] = rng.choice(SWAP_AREAS)
        for c in lang_cols:
            r2[c] = rng.choice(SWAP_LANGS)
        for c in sens_cols:
            r2[c] = rng.choice(["x", "y", "z"])
        swapped.append(r2)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    w.writerows(swapped)
    a, b = _rank(text), _rank(buf.getvalue())
    diffs = [ref for ref in a if a[ref] != b.get(ref)]
    return {"accounts": len(a), "fields_swapped": name_cols + area_cols + lang_cols + sens_cols,
            "accounts_whose_rank_or_points_changed": len(diffs), "passed": not diffs, "examples": diffs[:5]}


def _auc(pos: list[float], neg: list[float]) -> float:
    """Probability a random member of one group outscores a random member of the other (rank-sum form)."""
    if not pos or not neg:
        return 0.5
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    ranks, i = {}, 0
    rank_sum = 0.0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        avg = (i + j + 1) / 2
        rank_sum += avg * sum(1 for k in range(i, j) if allv[k][1] == 1)
        i = j
    return (rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def proxy_audit(path: Path | None = None) -> dict:
    path = path or DATA / "borrowers_main.csv"
    ranked = _rank(path.read_text(encoding="utf-8"))
    with open(DATA / "protected_attrs_test_only.csv", encoding="utf-8") as f:
        prot = {r["account_ref"]: r for r in csv.DictReader(f)}
    scored = {ref: v[0] for ref, v in ranked.items() if v[0] is not None and ref in prot}
    out, worst = {}, 0.0
    for attr in ("gender", "religion", "caste_category"):
        groups = sorted({prot[r][attr] for r in scored})
        for g in groups:
            pos = [p for r, p in scored.items() if prot[r][attr] == g]
            neg = [p for r, p in scored.items() if prot[r][attr] != g]
            auc = round(_auc(pos, neg), 3)
            out[f"{attr}={g}"] = {"n": len(pos), "auc_vs_rest": auc, "mean_points": round(sum(pos) / len(pos), 1)}
            worst = max(worst, abs(auc - 0.5))
    return {"scored_accounts": len(scored), "groups": out, "largest_gap_from_0.5": round(worst, 3),
            "threshold": 0.05, "passed": worst <= 0.05,
            "note": "AUC 0.5 means points cannot tell the group apart. Synthetic attributes are independent of "
                    "behaviour by construction, so this demonstrates the harness, not a real portfolio."}


def structural() -> dict:
    leaked = [b for b in ranker.BANNED if b in ranker.ALLOWLIST]
    return {"scorer_input_fields": list(ranker.ALLOWLIST), "banned_fields": list(ranker.BANNED),
            "banned_fields_present_in_scorer_input": leaked, "passed": not leaked}


def run_all() -> dict:
    return {"structural": structural(), "counterfactual": counterfactual(), "proxy_audit": proxy_audit()}


if __name__ == "__main__":
    import json
    print(json.dumps(run_all(), indent=2, ensure_ascii=False))
