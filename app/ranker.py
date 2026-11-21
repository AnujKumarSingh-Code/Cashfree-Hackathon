"""Deterministic, explainable priority points. No model, no LLM.

The scorer only ever receives a RankFeatures record. Name, gender, religion, caste,
area and language are not fields of that record, so they cannot influence a score.
Points set the calling order. They are not a prediction that anyone will pay.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields
from datetime import date

RULE_VERSION = "kramsetu-rules-1.0"

MAX_RECORD, MAX_PROMISE, MAX_TIMING, MAX_STAGE, MAX_AMOUNT = 35, 20, 20, 15, 10
FATIGUE_PER_ATTEMPT, FATIGUE_NO_ANSWER = 8, 4


@dataclass(frozen=True)
class RankFeatures:
    days_overdue: int
    outstanding: float
    instalments_due: int | None
    instalments_paid: int | None
    usual_pay_day: int | None
    stated_pay_date: str | None
    promises_made: int | None
    promises_kept: int | None
    attempts_7d: int
    last_contact_outcome: str | None


ALLOWLIST = tuple(f.name for f in fields(RankFeatures))
BANNED = ("name", "gender", "religion", "caste", "community", "area", "pincode", "language", "phone")


def effective_dpd(acc: dict, today: date) -> int | None:
    """Days overdue grows with the calendar after import."""
    dpd = acc.get("days_overdue")
    if dpd is None:
        return None
    asof = acc.get("dpd_asof")
    if asof:
        dpd += max((today - date.fromisoformat(asof)).days, 0)
    return dpd


def features_from_account(acc: dict, today: date) -> RankFeatures:
    return RankFeatures(
        days_overdue=int(effective_dpd(acc, today) or 0),
        outstanding=float(acc.get("outstanding") or 0),
        instalments_due=acc.get("instalments_due"),
        instalments_paid=acc.get("instalments_paid"),
        usual_pay_day=acc.get("usual_pay_day"),
        stated_pay_date=acc.get("stated_pay_date"),
        promises_made=acc.get("promises_made"),
        promises_kept=acc.get("promises_kept"),
        attempts_7d=int(acc.get("attempts_7d") or 0),
        last_contact_outcome=acc.get("last_contact_outcome"),
    )


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _days_since_pay_day(pay_day: int, today: date) -> int:
    """How many days ago the usual pay day last occurred (0 = today)."""
    return (today.day - pay_day) % 30


def lane_for(f: RankFeatures, today: date) -> str:
    if f.stated_pay_date and f.stated_pay_date > today.isoformat():
        return "call_on_date"
    if (f.instalments_due and f.instalments_due >= 4 and f.instalments_paid is not None
            and f.instalments_paid / f.instalments_due >= 0.85 and f.days_overdue <= 10 and f.attempts_7d == 0):
        return "reminder_only"
    return "call_now"


def score(f: RankFeatures, today: date) -> dict:
    parts = []

    if f.instalments_due and f.instalments_paid is not None:
        ratio = f.instalments_paid / f.instalments_due
        parts.append(("Verified payment record", round(MAX_RECORD * ratio, 1), MAX_RECORD,
                      f"paid {f.instalments_paid} of {f.instalments_due} instalments"))
    else:
        parts.append(("Verified payment record", round(MAX_RECORD * 0.5, 1), MAX_RECORD, "no payment history, scored neutral"))

    if f.promises_made:
        ratio = (f.promises_kept or 0) / f.promises_made
        parts.append(("Promise follow-through", round(MAX_PROMISE * ratio, 1), MAX_PROMISE,
                      f"kept {f.promises_kept or 0} of {f.promises_made} promises"))
    else:
        parts.append(("Promise follow-through", round(MAX_PROMISE * 0.5, 1), MAX_PROMISE, "no promises on record, scored neutral"))

    if f.stated_pay_date and f.stated_pay_date <= today.isoformat():
        parts.append(("Timing fit", float(MAX_TIMING), MAX_TIMING, f"said they would pay by {f.stated_pay_date}"))
    elif f.usual_pay_day:
        since = _days_since_pay_day(f.usual_pay_day, today)
        if since <= 3:
            fit, when = 1.0, "which is this week"
        elif since >= 28:
            fit, when = 0.8, "which is in the next two days"
        elif since <= 7:
            fit, when = 0.6, "which was last week"
        else:
            fit, when = 0.3, "which is not close to today"
        parts.append(("Timing fit", round(MAX_TIMING * fit, 1), MAX_TIMING,
                      f"usually pays around the {_ordinal(f.usual_pay_day)}, {when}"))
    else:
        parts.append(("Timing fit", round(MAX_TIMING * 0.5, 1), MAX_TIMING, "usual pay day not known, scored neutral"))

    d = f.days_overdue
    stage, label = ((1.0, "1 to 30 days overdue") if d <= 30 else (0.7, "31 to 60 days overdue") if d <= 60
                    else (0.45, "61 to 90 days overdue") if d <= 90 else (0.25, "over 90 days overdue"))
    parts.append(("Delinquency stage", round(MAX_STAGE * stage, 1), MAX_STAGE, label))

    amt = min(1.0, math.log10(max(f.outstanding, 1.0)) / 5.0)
    parts.append(("Amount at stake", round(MAX_AMOUNT * amt, 1), MAX_AMOUNT, f"₹{f.outstanding:,.0f} outstanding"))

    fatigue = FATIGUE_PER_ATTEMPT * f.attempts_7d + (FATIGUE_NO_ANSWER if f.last_contact_outcome == "no_answer" else 0)
    if fatigue:
        parts.append(("Contact fatigue", -float(fatigue), 0,
                      f"{f.attempts_7d} contact attempt(s) in the last 7 days"
                      + (", last one unanswered" if f.last_contact_outcome == "no_answer" else "")))

    points = round(max(0.0, min(100.0, sum(p[1] for p in parts))), 1)
    lane = lane_for(f, today)

    # reason: the two terms that earned the largest share of their maximum, plus any deduction
    positive = sorted([p for p in parts if p[2] > 0], key=lambda p: (-(p[1] / p[2]), -p[2]))
    bits = [positive[0][3], positive[1][3]]
    reason = (bits[0][0].upper() + bits[0][1:]) + "; " + bits[1] + "."
    if fatigue:
        reason += f" Lowered for {f.attempts_7d} recent attempt(s)."
    if lane == "reminder_only":
        reason = "Pays reliably without a call: " + bits[0] + ". Send the link, keep the caller free."
    elif lane == "call_on_date":
        reason = f"Borrower named {f.stated_pay_date} as the pay date. Contact is scheduled for that day, not before."
    return {"points": points, "lane": lane, "reason": reason, "rule_version": RULE_VERSION,
            "breakdown": [{"term": p[0], "points": p[1], "max": p[2], "detail": p[3]} for p in parts]}
