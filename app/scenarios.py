"""The seven demo scenarios, each runnable with one click. They use the same code paths as manual use.

Where a real sandbox payment is needed (live mode), the scenario stops after creating the link and says
what to do next. It never pretends a payment happened.
"""
from __future__ import annotations

from pathlib import Path

from . import contact, db, ingest, payments, queue

ROOT = Path(__file__).resolve().parent.parent

LIST = [
    {"id": "success", "title": "1. Success", "about": "Top borrower is called in Hindi, agrees to pay, gets a link, pays, leaves the queue."},
    {"id": "already_paid", "title": "2. Already paid", "about": "Borrower says 'branch mein de diya'. Contact pauses, Cashfree is checked, case is held for a person."},
    {"id": "part_payment", "title": "3. Part payment", "about": "Borrower pays part. Balance drops; the account is not treated as fully paid."},
    {"id": "promise_lapse", "title": "4. Promise lapse", "about": "Borrower promises tomorrow. The simulated clock moves two days. The promise lapses and the queue re-forms."},
    {"id": "hardship", "title": "5. Hardship and dispute", "about": "One borrower reports job loss, another disputes the amount. Both move to support or review."},
    {"id": "fairness", "title": "6. Fairness swap", "about": "Two accounts with identical permitted fields but different name, area and language get identical points."},
    {"id": "messy_file", "title": "7. Messy second file", "about": "A file with unseen headers and junk values is imported. Unclear rows are flagged, not fixed."},
]


def _fresh(ctx):
    db.set_clock(ctx.conn, None)
    p = ROOT / "data" / "demo_borrowers.csv"
    ingest.import_file(ctx.conn, p.read_bytes(), p.name)
    queue.reform(ctx.conn, ctx.settings, "demo data loaded")
    # demos must run at any hour; if the real time is outside the window, use a labelled simulated time
    from . import gates
    now = db.now(ctx.conn, ctx.settings.timezone)
    note = None
    if not gates.within_window(now, ctx.settings.window_start, ctx.settings.window_end):
        db.set_clock(ctx.conn, now.replace(hour=11, minute=0, second=0), ctx.settings.timezone)
        queue.reform(ctx.conn, ctx.settings, "simulated clock set to 11:00 for the demo")
        note = "Real time is outside the contact window, so the clock is SIMULATED at 11:00 for this demo."
    return note


def _pos(ctx, ref):
    r = ctx.conn.execute("SELECT position,state,points,outstanding,hold_reason,reason,lane FROM accounts WHERE account_ref=?", (ref,)).fetchone()
    return dict(r) if r else None


def _pay(ctx, steps, link, amount, what):
    """Mock mode: simulate the borrower paying. Live mode: stop and tell the presenter what to do."""
    if ctx.cashfree.mode == "mock":
        ctx.cashfree.simulate_payment(link["link_id"], amount)
        res = payments.reconcile_link(ctx, link["link_id"], "mock")
        steps.append({"step": f"MOCK payment of ₹{amount:,.0f} ({what}), then status read back", "result": res.get("message")})
        return True
    steps.append({"step": "LIVE SANDBOX: open the link, pay with Cashfree test details, then press 'Check payment status'",
                  "result": link["link_url"], "action_needed": True})
    return False


def success(ctx):
    steps, note = [], _fresh(ctx)
    top = ctx.conn.execute("SELECT account_ref FROM accounts WHERE position=1").fetchone()["account_ref"]
    before = _pos(ctx, top)
    steps.append({"step": f"Queue formed. Position 1 is {top}", "result": before["reason"]})
    c = contact.start_contact(ctx, top)
    steps.append({"step": f"Contact approved ({'SIMULATED call' if c.get('mode') == 'simulated' else 'Relay voice call'})",
                  "result": " ".join(l["text"] for l in c["script"]["lines"][:3]) if c.get("ok") else c.get("error")})
    r = contact.record_reply(ctx, top, "haan theek hai, link bhej do, abhi pay kar deta hoon", "scripted_demo_reply")
    steps.append({"step": "Borrower reply understood", "result": r["summary"]})
    link = r["link"]["link"]
    steps.append({"step": "Payment link created", "result": f"{link['link_id']} for ₹{link['amount']:,.0f} [{link['mode'].upper()}] {link['link_url']}"})
    paid = _pay(ctx, steps, link, link["amount"], "full amount")
    steps.append({"step": "Account after", "result": _pos(ctx, top)})
    return {"scenario": "success", "account": top, "note": note, "steps": steps, "complete": paid}


def already_paid(ctx):
    steps, note = [], _fresh(ctx)
    ref = "KS-1002"
    steps.append({"step": f"{ref} before", "result": _pos(ctx, ref)})
    contact.start_contact(ctx, ref)
    r = contact.record_reply(ctx, ref, "maine to kal branch mein de diya", "scripted_demo_reply")
    steps.append({"step": "Borrower reply understood", "result": r["summary"]})
    steps.append({"step": "Cashfree checked for a confirmed payment", "result": r["claim_check"]["message"]})
    steps.append({"step": f"{ref} after: safely held, not chased", "result": _pos(ctx, ref)})
    again = contact.start_contact(ctx, ref)
    steps.append({"step": "Attempt to call again", "result": again.get("error")})
    return {"scenario": "already_paid", "account": ref, "note": note, "steps": steps, "complete": True}


def part_payment(ctx):
    steps, note = [], _fresh(ctx)
    ref = "KS-1003"
    steps.append({"step": f"{ref} before", "result": _pos(ctx, ref)})
    contact.start_contact(ctx, ref)
    r = contact.record_reply(ctx, ref, "abhi poora nahi ho payega, aadha de sakti hoon", "scripted_demo_reply")
    steps.append({"step": "Borrower reply understood", "result": r["summary"]})
    link = r["link"]["link"]
    steps.append({"step": "Part-payment link created", "result": f"{link['link_id']} for ₹{link['amount']:,.0f}, minimum first amount ₹{link['min_part']:,.0f} [{link['mode'].upper()}]"})
    paid = _pay(ctx, steps, link, round(link["amount"] / 2), "half")
    steps.append({"step": f"{ref} after: balance reduced, not closed", "result": _pos(ctx, ref)})
    second = payments.create_link_for_account(ctx, ref)
    steps.append({"step": "Request for another link", "result": "Existing open link reused; no second link created." if second.get("reused") else second})
    return {"scenario": "part_payment", "account": ref, "note": note, "steps": steps, "complete": paid}


def promise_lapse(ctx):
    steps, note = [], _fresh(ctx)
    ref = "KS-1004"
    steps.append({"step": f"{ref} before", "result": _pos(ctx, ref)})
    contact.start_contact(ctx, ref)
    r = contact.record_reply(ctx, ref, "kal de dunga pakka", "scripted_demo_reply")
    steps.append({"step": "Borrower reply understood", "result": r["summary"]})
    steps.append({"step": f"{ref} with promise: out of the call list until the date", "result": _pos(ctx, ref)})
    db.advance_clock(ctx.conn, 2 * 1440)
    queue.reform(ctx.conn, ctx.settings, "simulated clock advanced 2 days", ref)
    steps.append({"step": "SIMULATED clock advanced 2 days. No payment arrived.", "result": _pos(ctx, ref)})
    return {"scenario": "promise_lapse", "account": ref, "note": (note or "") + " Clock is SIMULATED after this scenario; press Reset clock.",
            "steps": steps, "complete": True}


def hardship(ctx):
    steps, note = [], _fresh(ctx)
    contact.start_contact(ctx, "KS-1005")
    r = contact.record_reply(ctx, "KS-1005", "naukri chali gayi hai, abhi paise nahi hai", "scripted_demo_reply")
    steps.append({"step": "KS-1005 reply understood", "result": r["summary"]})
    steps.append({"step": "KS-1005 after: support path, no pressure", "result": _pos(ctx, "KS-1005")})
    contact.start_contact(ctx, "KS-1006")
    r = contact.record_reply(ctx, "KS-1006", "amount galat hai, itna nahi banta", "scripted_demo_reply")
    steps.append({"step": "KS-1006 reply understood", "result": r["summary"]})
    steps.append({"step": "KS-1006 after: routed to review", "result": _pos(ctx, "KS-1006")})
    steps.append({"step": "Attempt to call KS-1005 again", "result": contact.start_contact(ctx, "KS-1005").get("error")})
    return {"scenario": "hardship", "account": "KS-1005", "note": note, "steps": steps, "complete": True}


def fairness(ctx):
    steps, note = [], _fresh(ctx)
    a, b = _pos(ctx, "KS-1015"), _pos(ctx, "KS-1016")
    rows = {r["account_ref"]: dict(r) for r in ctx.conn.execute(
        "SELECT account_ref,name,area,language FROM accounts WHERE account_ref IN ('KS-1015','KS-1016')")}
    steps.append({"step": "KS-1015", "result": {**rows["KS-1015"], "points": a["points"], "position": a["position"]}})
    steps.append({"step": "KS-1016 (same permitted fields; different name, area, language)",
                  "result": {**rows["KS-1016"], "points": b["points"], "position": b["position"]}})
    same = a["points"] == b["points"] and abs(a["position"] - b["position"]) == 1
    steps.append({"step": "Check", "result": "Identical points, adjacent positions (tie broken by account reference)." if same
                  else "MISMATCH: points differ. This must be investigated."})
    return {"scenario": "fairness", "account": "KS-1015", "note": note, "steps": steps, "complete": same}


def messy_file(ctx):
    db.set_clock(ctx.conn, None)
    p = ROOT / "data" / "borrowers_curveball.csv"
    s = ingest.import_file(ctx.conn, p.read_bytes(), p.name)
    q = queue.reform(ctx.conn, ctx.settings, "curveball file imported")
    ex = [dict(r) for r in ctx.conn.execute("SELECT account_ref, hold_reason FROM accounts WHERE state='needs_review' LIMIT 4")]
    steps = [
        {"step": "Headers matched by alias", "result": s["mapping"]},
        {"step": "Sensitive columns dropped at import, never stored", "result": s["dropped_sensitive"]},
        {"step": "Columns not understood (listed, not used)", "result": s["unmapped"]},
        {"step": f"{s['rows']} rows read, {s['stored']} accounts, {s['duplicates']} duplicate rows, {s['flagged']} flagged for review", "result": ex},
        {"step": "Queue still forms for the rows that are clear", "result": q},
    ]
    return {"scenario": "messy_file", "account": None, "note": "The demo book was replaced by the curveball file. Press 'Load demo data' to return.",
            "steps": steps, "complete": True}


RUN = {"success": success, "already_paid": already_paid, "part_payment": part_payment, "promise_lapse": promise_lapse,
       "hardship": hardship, "fairness": fairness, "messy_file": messy_file}
