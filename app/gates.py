"""Eligibility and safety gates. They run before any ranking and name the rule that fired."""
from __future__ import annotations

import json
from datetime import datetime, time

from .ranker import effective_dpd

# states a person or a verified event must resolve; gates never override them
STICKY = {
    "paid": "Paid. Confirmed payment, nothing further to collect.",
    "disputed": "Dispute open. Routed to review; no collection contact.",
    "hardship_hold": "Hardship reported. Support path only; no pressure.",
    "claim_under_check": "Borrower says already paid. Contact paused until verified.",
    "do_not_contact": "Do-not-contact preference recorded.",
    "needs_review": "Held for a person to review.",
}
HOLD_STATES = set(STICKY) | {"not_overdue", "awaiting_payment", "attempt_cap", "promise_recorded"}


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def within_window(now: datetime, start: str, end: str) -> bool:
    return parse_hhmm(start) <= now.time() < parse_hhmm(end)


def evaluate(acc: dict, now: datetime, settings, has_open_link: bool = False) -> dict:
    """Returns {eligible, state, hold_reason, rule}. Eligible accounts go on to ranking."""
    flags = json.loads(acc.get("flags_json") or "[]") if isinstance(acc.get("flags_json"), str) else (acc.get("flags") or [])
    state = acc.get("state")

    if state == "needs_review" or flags:
        return {"eligible": False, "state": "needs_review", "rule": "review_hold",
                "hold_reason": acc.get("hold_reason") or ("Held for review: " + "; ".join(flags))}
    if state in STICKY:
        return {"eligible": False, "state": state, "rule": f"sticky_{state}",
                "hold_reason": acc.get("hold_reason") or STICKY[state]}
    if acc.get("dnc"):
        return {"eligible": False, "state": "do_not_contact", "rule": "do_not_contact", "hold_reason": STICKY["do_not_contact"]}
    if acc.get("status") == "paid" or (acc.get("outstanding") is not None and acc["outstanding"] <= 0):
        return {"eligible": False, "state": "paid", "rule": "confirmed_paid",
                "hold_reason": "Excluded: the file shows this account as paid or with nothing outstanding."}
    dpd = effective_dpd(acc, now.date())
    if acc.get("status") == "not_overdue" or (dpd is not None and dpd <= 0):
        return {"eligible": False, "state": "not_overdue", "rule": "not_overdue",
                "hold_reason": "Excluded: not overdue."}
    if acc.get("outstanding") is None or dpd is None:
        return {"eligible": False, "state": "needs_review", "rule": "unconfirmed",
                "hold_reason": "Held for review: balance or overdue status cannot be confirmed."}
    if has_open_link and state == "awaiting_payment":
        return {"eligible": False, "state": "awaiting_payment", "rule": "link_open",
                "hold_reason": "Payment link sent. Waiting for Cashfree to confirm; no further contact meanwhile."}
    if int(acc.get("attempts_7d") or 0) >= settings.max_attempts_per_week:
        return {"eligible": False, "state": "attempt_cap", "rule": "attempt_cap",
                "hold_reason": f"Deferred: contact-attempt cap reached ({settings.max_attempts_per_week} in 7 days)."}
    new_state = "partially_paid" if state == "partially_paid" else "ranked"
    return {"eligible": True, "state": new_state, "rule": "eligible", "hold_reason": None}


def contact_refusal(acc: dict, now: datetime, settings) -> str | None:
    """Checked again at the moment of contact. Returns the reason to refuse, or None."""
    if acc.get("state") not in ("ranked", "partially_paid", "contact_ready", "promise_lapsed", "eligible"):
        return f"Account is in state '{acc.get('state')}', which is not open for contact. {acc.get('hold_reason') or ''}".strip()
    if acc.get("dnc"):
        return "Do-not-contact preference recorded."
    if not acc.get("phone"):
        return "No usable phone number."
    if not within_window(now, settings.window_start, settings.window_end):
        return (f"Outside the permitted contact window ({settings.window_start} to {settings.window_end} "
                f"{settings.timezone}). It is {now.strftime('%H:%M')}.")
    if int(acc.get("attempts_7d") or 0) >= settings.max_attempts_per_week:
        return f"Contact-attempt cap reached ({settings.max_attempts_per_week} in 7 days)."
    if acc.get("stated_pay_date") and acc["stated_pay_date"] > now.date().isoformat():
        return f"Borrower named {acc['stated_pay_date']} as the pay date; contact is scheduled for that day."
    return None
