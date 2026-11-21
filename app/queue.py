"""Re-form the queue: gates, then ranking, then a diff that explains every move."""
from __future__ import annotations

import hashlib
import json

from . import db, gates, ranker

NEXT_ACTION = {
    "call_now": "Call in the borrower's language; offer pay now, part payment, or a date.",
    "reminder_only": "Send the payment link as a reminder. No call needed.",
    "call_on_date": "Wait. Contact on the date the borrower named.",
    "needs_review": "A person reviews the flagged data or the unclear reply.",
    "claim_under_check": "Verify the claimed payment before any further contact.",
    "hardship_hold": "Support follow-up by a person: smaller amount or a later date. No pressure.",
    "disputed": "Review the dispute and share grievance contact details.",
    "do_not_contact": "No calls. Written notices only where required.",
    "awaiting_payment": "Wait for Cashfree to confirm the payment link status.",
    "paid": "None. Closed for collection.",
    "not_overdue": "None. Not overdue.",
    "attempt_cap": "Wait until the 7-day attempt window frees up.",
}


def _explore_key(ref: str, day: str) -> str:
    return hashlib.sha256(f"{ref}|{day}".encode()).hexdigest()


def reform(conn, settings, cause: str, cause_ref: str | None = None, actor: str = "agent") -> dict:
    now = db.now(conn, settings.timezone)
    today = now.date()
    rows = [dict(r) for r in conn.execute("SELECT * FROM accounts").fetchall()]
    open_links = {r["account_ref"] for r in conn.execute(
        "SELECT account_ref FROM payment_links WHERE status IN ('ACTIVE','UNKNOWN','PARTIALLY_PAID')")}
    before = {r["account_ref"]: (r["position"], r["state"], r["lane"]) for r in rows}

    call_now, updates = [], {}
    for acc in rows:
        ref = acc["account_ref"]
        # a promise whose date has passed without payment lapses here
        if acc["state"] == "promise_recorded":
            if acc["stated_pay_date"] and acc["stated_pay_date"] < today.isoformat():
                acc["state"] = "promise_lapsed"
                acc["last_contact_outcome"] = "promise_lapsed"
                lapsed_date = acc["stated_pay_date"]
                acc["stated_pay_date"], acc["promise_open"] = None, 0
                conn.execute("UPDATE accounts SET state='promise_lapsed', last_contact_outcome='promise_lapsed', "
                             "stated_pay_date=NULL, promise_open=0 WHERE account_ref=?", (ref,))
                db.audit(conn, "promise_lapsed", {"promised_date": lapsed_date,
                         "note": "No confirmed payment by the promised date. Promise follow-through now counts this."},
                         ref, rule_version=ranker.RULE_VERSION, ts=now)
            else:
                acc["state"] = "ranked"   # shown in the call-on-date lane
        g = gates.evaluate(acc, now, settings, has_open_link=ref in open_links)
        u = {"state": g["state"], "hold_reason": g["hold_reason"], "lane": None, "points": None,
             "breakdown_json": None, "reason": None, "position": None, "is_exploration": 0,
             "next_action": NEXT_ACTION.get(g["state"], "")}
        if g["eligible"]:
            s = ranker.score(ranker.features_from_account(acc, today), today)
            u.update(lane=s["lane"], points=s["points"], breakdown_json=json.dumps(s["breakdown"], ensure_ascii=False),
                     reason=s["reason"], next_action=NEXT_ACTION[s["lane"]])
            if acc["state"] == "promise_lapsed":
                u["reason"] = "Promise date passed with no confirmed payment. " + s["reason"]
            if s["lane"] == "call_on_date":
                u["state"] = "promise_recorded"
            if s["lane"] == "call_now":
                call_now.append((ref, s["points"]))
        updates[ref] = u

    # deterministic order: points high to low, ties by account reference
    call_now.sort(key=lambda x: (-x[1], x[0]))
    order = [r for r, _ in call_now]
    cap = settings.daily_call_capacity
    explored: set[str] = set()
    if len(order) > cap and settings.exploration_share > 0:
        k = max(1, round(cap * settings.exploration_share))
        head, tail = order[:cap - k], order[cap - k:]
        picks = sorted(tail, key=lambda r: _explore_key(r, today.isoformat()))[:k]
        explored = set(picks)
        rest = [r for r in tail if r not in explored]
        step = max(cap // k, 1)
        merged = head[:]
        for i, p in enumerate(picks):
            merged.insert(min((i + 1) * step - 1, len(merged)), p)
        order = merged + rest
    for pos, ref in enumerate(order, start=1):
        updates[ref]["position"] = pos
        if ref in explored:
            updates[ref]["is_exploration"] = 1
            updates[ref]["reason"] = ("Exploration slot: called to test the ranking on a lower-ranked account. "
                                      + updates[ref]["reason"])

    ts = now.isoformat()
    conn.execute("BEGIN")
    changes = []
    for ref, u in updates.items():
        conn.execute("UPDATE accounts SET state=?,hold_reason=?,lane=?,points=?,breakdown_json=?,reason=?,position=?,"
                     "is_exploration=?,next_action=?,updated_at=? WHERE account_ref=?",
                     (u["state"], u["hold_reason"], u["lane"], u["points"], u["breakdown_json"], u["reason"],
                      u["position"], u["is_exploration"], u["next_action"], ts, ref))
        old_pos, old_state, _ = before[ref]
        # the triggering account is always logged, even when its state was already updated by the caller
        if old_pos != u["position"] or old_state != u["state"] or ref == cause_ref:
            changes.append((ref, old_pos, u["position"], old_state, u["state"]))

    def explain(ref, op, np_, os_, ns):
        if ref == cause_ref:
            if np_ is None and op is None:
                return f"{cause[0].upper() + cause[1:]}. Now '{ns}'. {updates[ref]['hold_reason'] or updates[ref]['reason'] or ''}".strip()
            if np_ is None:
                return f"Left the call queue: {cause}. Now '{ns}'. {updates[ref]['hold_reason'] or ''}".strip()
            if op is None:
                return f"Entered the call queue at position {np_}: {cause}."
            return f"Moved from position {op} to {np_}: {cause}. {updates[ref]['reason']}"
        if op is not None and np_ is not None:
            direction = "up" if np_ < op else "down"
            return f"Moved {direction} from {op} to {np_} because {cause_ref or 'another account'} changed ({cause})."
        if np_ is None:
            return f"Left the call queue. Now '{ns}'. {updates[ref]['hold_reason'] or ''}".strip()
        return f"Entered the call queue at position {np_}."

    first_run = all(v[0] is None and v[1] in ("eligible", "needs_review") for v in before.values()) and cause_ref is None
    if not first_run:
        # the triggering account first, then the largest moves, capped so the log stays readable
        changes.sort(key=lambda c: (c[0] != cause_ref, -abs((c[1] or 0) - (c[2] or 0)) if c[1] and c[2] else -10**6, c[0]))
        for ref, op, np_, os_, ns in changes[:40]:
            conn.execute("INSERT INTO queue_changes(ts,cause,cause_ref,account_ref,old_position,new_position,old_state,"
                         "new_state,explanation) VALUES(?,?,?,?,?,?,?,?,?)",
                         (ts, cause, cause_ref, ref, op, np_, os_, ns, explain(ref, op, np_, os_, ns)))
        for ref, op, np_, os_, ns in changes:
            if ref == cause_ref or os_ != ns:
                if ref == cause_ref and op == np_ and os_ == ns and cause.startswith("clock"):
                    continue
                db.audit(conn, "queue_position_changed", {"cause": cause, "old_position": op, "new_position": np_,
                         "old_state": os_, "new_state": ns, "points": updates[ref]["points"],
                         "reason": updates[ref]["reason"] or updates[ref]["hold_reason"]},
                         ref, actor=actor, rule_version=ranker.RULE_VERSION, ts=now)
    summary = {"cause": cause, "cause_ref": cause_ref, "in_call_queue": len(order),
               "moved": len(changes), "exploration_slots": len(explored)}
    db.audit(conn, "queue_reformed", summary, cause_ref, actor=actor, rule_version=ranker.RULE_VERSION, ts=now)
    conn.execute("COMMIT")
    return summary
