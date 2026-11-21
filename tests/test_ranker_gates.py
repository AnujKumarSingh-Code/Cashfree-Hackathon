import dataclasses
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import db, gates, queue, ranker
from app.ranker import RankFeatures, score
from tests.conftest import acc

T = date(2026, 11, 21)
BASE = RankFeatures(days_overdue=20, outstanding=6000, instalments_due=10, instalments_paid=8, usual_pay_day=21,
                    stated_pay_date=None, promises_made=2, promises_kept=1, attempts_7d=0, last_contact_outcome=None)


def test_scorer_input_contains_no_banned_field():
    assert not set(ranker.BANNED) & set(ranker.ALLOWLIST)


def test_points_are_deterministic_and_bounded():
    a, b = score(BASE, T), score(BASE, T)
    assert a == b and 0 <= a["points"] <= 100


def test_breakdown_adds_up_to_the_points():
    s = score(BASE, T)
    assert round(sum(p["points"] for p in s["breakdown"]), 1) == s["points"]


def test_better_payment_record_scores_higher():
    assert score(dataclasses.replace(BASE, instalments_paid=10), T)["points"] > score(dataclasses.replace(BASE, instalments_paid=3), T)["points"]


def test_missing_history_is_neutral_not_punished():
    unknown = score(dataclasses.replace(BASE, instalments_due=None, instalments_paid=None), T)
    worst = score(dataclasses.replace(BASE, instalments_paid=0), T)
    best = score(dataclasses.replace(BASE, instalments_paid=10), T)
    assert worst["points"] < unknown["points"] < best["points"]
    assert "neutral" in unknown["breakdown"][0]["detail"]


def test_contact_fatigue_lowers_points():
    assert score(dataclasses.replace(BASE, attempts_7d=2), T)["points"] < score(BASE, T)["points"]


def test_every_score_has_a_readable_reason():
    s = score(BASE, T)
    assert len(s["reason"]) > 20 and s["reason"][0].isupper()


def test_lanes():
    assert score(BASE, T)["lane"] == "call_now"
    assert score(dataclasses.replace(BASE, stated_pay_date="2026-12-05"), T)["lane"] == "call_on_date"
    reliable = dataclasses.replace(BASE, instalments_due=12, instalments_paid=11, days_overdue=4)
    assert score(reliable, T)["lane"] == "reminder_only"


def test_queue_order_is_points_then_reference(demo):
    rows = demo.conn.execute("SELECT account_ref, points, position FROM accounts WHERE position IS NOT NULL ORDER BY position").fetchall()
    assert [r["position"] for r in rows] == list(range(1, len(rows) + 1))
    keys = [(-r["points"], r["account_ref"]) for r in rows]
    assert keys == sorted(keys)


def test_gates_hold_the_right_accounts(demo):
    assert acc(demo, "KS-1011")["state"] == "do_not_contact"
    assert acc(demo, "KS-1012")["state"] == "attempt_cap"
    assert acc(demo, "KS-1009")["state"] == "needs_review"
    assert acc(demo, "KS-1010")["position"] is None            # status says paid: never called
    assert acc(demo, "KS-1007")["lane"] == "reminder_only" and acc(demo, "KS-1007")["position"] is None
    for ref in ("KS-1009", "KS-1010", "KS-1011", "KS-1012", "KS-1014"):
        assert acc(demo, ref)["hold_reason"], ref


def test_contact_window_boundaries():
    z = ZoneInfo("Asia/Kolkata")
    assert not gates.within_window(datetime(2026, 11, 21, 7, 59, tzinfo=z), "08:00", "19:00")
    assert gates.within_window(datetime(2026, 11, 21, 8, 0, tzinfo=z), "08:00", "19:00")
    assert gates.within_window(datetime(2026, 11, 21, 18, 59, tzinfo=z), "08:00", "19:00")
    assert not gates.within_window(datetime(2026, 11, 21, 19, 0, tzinfo=z), "08:00", "19:00")


def test_exploration_slice_is_labelled_and_deterministic(ctx):
    from pathlib import Path
    from app import ingest
    p = Path(__file__).resolve().parent.parent / "data" / "borrowers_main.csv"
    ingest.import_file(ctx.conn, p.read_bytes(), p.name)
    q1 = queue.reform(ctx.conn, ctx.settings, "t")
    first = [r["account_ref"] for r in ctx.conn.execute("SELECT account_ref FROM accounts WHERE is_exploration=1 ORDER BY position")]
    queue.reform(ctx.conn, ctx.settings, "t again")
    second = [r["account_ref"] for r in ctx.conn.execute("SELECT account_ref FROM accounts WHERE is_exploration=1 ORDER BY position")]
    assert q1["exploration_slots"] == round(ctx.settings.daily_call_capacity * ctx.settings.exploration_share)
    assert first == second and len(first) == q1["exploration_slots"]
    in_cap = ctx.conn.execute("SELECT COUNT(*) n FROM accounts WHERE is_exploration=1 AND position<=?",
                              (ctx.settings.daily_call_capacity,)).fetchone()["n"]
    assert in_cap == len(first)
    r = ctx.conn.execute("SELECT reason FROM accounts WHERE is_exploration=1 LIMIT 1").fetchone()["reason"]
    assert r.startswith("Exploration slot")


def test_audit_log_is_append_only(demo):
    import sqlite3
    import pytest
    with pytest.raises(sqlite3.DatabaseError):
        demo.conn.execute("UPDATE audit_events SET kind='x'")
    with pytest.raises(sqlite3.DatabaseError):
        demo.conn.execute("DELETE FROM audit_events")


def test_phone_masking():
    assert db.mask_phone("9000012345") == "••••••2345"
    assert "90000" not in db.mask_phone("9000012345")
