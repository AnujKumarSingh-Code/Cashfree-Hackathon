"""Payment loop. Only a Cashfree status changes a balance.

Rules: a created link is not a payment; a promise is not a payment; a transcript is not a payment.
The same event applied twice changes nothing the second time. No second link while one is open
or its status is unknown.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta

from . import db, queue
from .adapters import cashfree as cf


def _acc(conn, ref: str) -> dict | None:
    r = conn.execute("SELECT * FROM accounts WHERE account_ref=?", (ref,)).fetchone()
    return dict(r) if r else None


def open_link(conn, ref: str) -> dict | None:
    r = conn.execute("SELECT * FROM payment_links WHERE account_ref=? AND status IN ('ACTIVE','PARTIALLY_PAID','UNKNOWN') "
                     "ORDER BY created_at DESC LIMIT 1", (ref,)).fetchone()
    return dict(r) if r else None


def create_link_for_account(ctx, ref: str, actor: str = "agent", reason: str = "borrower asked to pay") -> dict:
    conn, s = ctx.conn, ctx.settings
    acc = _acc(conn, ref)
    if not acc:
        return {"ok": False, "error": "account not found"}
    if acc["state"] in ("paid", "disputed", "do_not_contact", "needs_review", "not_overdue"):
        db.audit(conn, "link_refused", {"why": f"account is '{acc['state']}'"}, ref, actor=actor)
        return {"ok": False, "refused": True, "error": f"No link created: account is '{acc['state']}'."}
    if not acc["outstanding"] or acc["outstanding"] <= 0:
        return {"ok": False, "refused": True, "error": "No link created: no confirmed outstanding balance."}
    existing = open_link(conn, ref)
    if existing:
        db.audit(conn, "link_reused", {"link_id": existing["link_id"], "status": existing["status"],
                 "why": "an open link already exists; a second link is never created automatically"}, ref,
                 actor=actor, source=existing["mode"])
        return {"ok": True, "reused": True, "link": existing}
    seq = conn.execute("SELECT COUNT(*) c FROM payment_links WHERE account_ref=?", (ref,)).fetchone()["c"] + 1
    # unique per database install, so a fresh database never collides with links Cashfree already holds;
    # stable per attempt, so a retry of the same attempt can never create a second link
    install = db.get_meta(conn, "install_id")
    if not install:
        install = secrets.token_hex(3)
        db.set_meta(conn, "install_id", install)
    link_id = f"ks_{install}_{re.sub(r'[^A-Za-z0-9_-]', '-', ref)[-32:]}_{seq}"[:50]
    amount = round(acc["outstanding"], 2)
    min_part = max(round(amount * s.min_part_share), 1) if amount > 2 else None
    now = db.now(conn, s.timezone)
    expiry = (now + timedelta(days=s.link_expiry_days)).isoformat()
    notify = f"{s.public_base_url}/api/webhooks/cashfree" if s.public_base_url else None
    try:
        res = ctx.cashfree.create_link(
            link_id=link_id, amount=amount, purpose=f"{s.lender_name}: overdue instalment for {ref}",
            customer={"phone": acc["phone"], "name": acc["name"]}, partial=bool(min_part), min_partial=min_part,
            expiry_iso=expiry, notes={"account_ref": ref, "app": "kramsetu"}, notify_url=notify,
            send_sms=False)   # borrower numbers here are synthetic; the link is shared by the agent, not by SMS
    except cf.CashfreeError as e:
        # the request may have reached Cashfree even though we got an error back: look before giving up
        try:
            res = ctx.cashfree.get_link(link_id)
        except cf.CashfreeError:
            res = None
        if not res or abs(res["link_amount"] - amount) > 0.005:
            db.audit(conn, "link_create_failed", {"link_id": link_id, "error": str(e),
                     "found_at_cashfree": bool(res)}, ref, actor=actor, source=ctx.cashfree.mode)
            return {"ok": False, "error": f"Cashfree did not create the link: {e}. Nothing changed."}
        db.audit(conn, "link_adopted", {"link_id": link_id, "why": "create returned an error but the link exists at Cashfree; "
                 "adopted instead of creating another", "error": str(e)}, ref, actor=actor, source=ctx.cashfree.mode)
    ts = now.isoformat()
    conn.execute("INSERT INTO payment_links(link_id,account_ref,cf_link_id,link_url,amount,min_part,partial,status,"
                 "amount_paid,mode,created_at,updated_at,expiry) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (link_id, ref, res["cf_link_id"], res["link_url"], amount, min_part, 1 if min_part else 0,
                  res["link_status"] if res["link_status"] in cf.KNOWN_STATUSES else "ACTIVE", 0.0,
                  ctx.cashfree.mode, ts, ts, expiry))
    conn.execute("UPDATE accounts SET state='awaiting_payment' WHERE account_ref=?", (ref,))
    db.audit(conn, "link_created", {"link_id": link_id, "cf_link_id": res["cf_link_id"], "amount": amount,
             "minimum_first_amount": min_part, "expiry": expiry, "why": reason,
             "note": "A link is not a payment. Balance is unchanged until Cashfree confirms."},
             ref, actor=actor, source=ctx.cashfree.mode)
    queue.reform(conn, s, f"payment link sent ({reason})", ref, actor)
    return {"ok": True, "reused": False, "link": dict(conn.execute("SELECT * FROM payment_links WHERE link_id=?", (link_id,)).fetchone())}


def apply_link_status(ctx, link_id: str, status: str, amount_paid: float, source: str, raw: dict | None = None) -> dict:
    """Idempotent: keyed on (link, status, amount paid). Replays and duplicate webhooks are no-ops."""
    conn, s = ctx.conn, ctx.settings
    link = conn.execute("SELECT * FROM payment_links WHERE link_id=?", (link_id,)).fetchone()
    if not link:
        return {"ok": False, "error": "unknown link_id; ignored"}
    link = dict(link)
    ref = link["account_ref"]
    status = str(status).upper()
    amount_paid = round(float(amount_paid or 0), 2)
    if status not in cf.KNOWN_STATUSES:
        db.audit(conn, "payment_status_unrecognised", {"link_id": link_id, "status": status}, ref, source=link["mode"])
        return {"ok": False, "error": f"status '{status}' not recognised; nothing changed"}
    now = db.now(conn, s.timezone)
    if status == "ACTIVE" and amount_paid == 0:
        if link["status"] == "UNKNOWN":
            conn.execute("UPDATE payment_links SET status='ACTIVE', updated_at=? WHERE link_id=?", (now.isoformat(), link_id))
        return {"ok": True, "changed": False, "status": "ACTIVE", "message": "Link is active and unpaid. No change."}
    key = f"{link_id}|{status}|{amount_paid:.2f}"
    cur = conn.execute("INSERT OR IGNORE INTO payment_events(dedupe_key,link_id,account_ref,status,amount_paid,source,"
                       "received_at,raw_json) VALUES(?,?,?,?,?,?,?,?)",
                       (key, link_id, ref, status, amount_paid, source, now.isoformat(),
                        json.dumps(raw or {}, ensure_ascii=False, default=str)))
    if cur.rowcount == 0:
        return {"ok": True, "changed": False, "duplicate": True, "status": status,
                "message": "Already processed. Duplicate event ignored."}
    if amount_paid + 0.001 < (link["amount_paid"] or 0):
        conn.execute("UPDATE accounts SET state='needs_review', hold_reason=? WHERE account_ref=?",
                     ("Held for review: Cashfree reported a lower paid amount than before.", ref))
        db.audit(conn, "payment_regression", {"link_id": link_id, "was": link["amount_paid"], "now": amount_paid},
                 ref, source=link["mode"])
        queue.reform(conn, s, "paid amount went down; held for review", ref)
        return {"ok": True, "changed": True, "status": status, "message": "Paid amount decreased; held for review."}
    acc = _acc(conn, ref)
    remaining = max(round(link["amount"] - amount_paid, 2), 0.0)
    conn.execute("UPDATE payment_links SET status=?, amount_paid=?, updated_at=? WHERE link_id=?",
                 (status, amount_paid, now.isoformat(), link_id))
    kept = 1 if (acc.get("promise_open") and status in ("PAID", "PARTIALLY_PAID")) else 0
    label = "MOCK " if link["mode"] == "mock" else ""
    if status == "PAID":
        conn.execute("UPDATE accounts SET state='paid', outstanding=0, status='paid', stated_pay_date=NULL, promise_open=0,"
                     " promises_kept=COALESCE(promises_kept,0)+?, hold_reason=?, last_contact_outcome='paid' WHERE account_ref=?",
                     (kept, f"Paid in full. {label}Cashfree confirmed ₹{amount_paid:,.2f} on link {link_id}.", ref))
        cause = f"{label}Cashfree confirmed full payment of ₹{amount_paid:,.2f}"
    elif status == "PARTIALLY_PAID":
        conn.execute("UPDATE accounts SET state='partially_paid', outstanding=?, promise_open=0, stated_pay_date=NULL,"
                     " promises_kept=COALESCE(promises_kept,0)+?, last_contact_outcome='part_paid' WHERE account_ref=?",
                     (remaining, kept, ref))
        cause = f"{label}Cashfree confirmed part payment of ₹{amount_paid:,.2f}; ₹{remaining:,.2f} remains"
    elif status == "EXPIRED":
        new_state = "partially_paid" if amount_paid > 0 else "eligible"
        conn.execute("UPDATE accounts SET state=?, last_contact_outcome='link_expired' WHERE account_ref=?", (new_state, ref))
        cause = f"{label}payment link expired unpaid" if amount_paid == 0 else f"{label}payment link expired after part payment"
    else:  # CANCELLED
        conn.execute("UPDATE accounts SET state='needs_review', hold_reason=? WHERE account_ref=?",
                     ("Held for review: the payment link was cancelled.", ref))
        cause = f"{label}payment link cancelled"
    db.audit(conn, "payment_status_applied", {"link_id": link_id, "status": status, "amount_paid": amount_paid,
             "remaining": remaining if status != "PAID" else 0.0, "source": source,
             "promise_counted_as_kept": bool(kept)}, ref, source=link["mode"])
    queue.reform(conn, s, cause, ref)
    return {"ok": True, "changed": True, "status": status, "amount_paid": amount_paid, "message": cause + "."}


def reconcile_link(ctx, link_id: str, source: str = "poll") -> dict:
    """Ask Cashfree for the status. A failed lookup never changes a balance."""
    conn, s = ctx.conn, ctx.settings
    link = conn.execute("SELECT * FROM payment_links WHERE link_id=?", (link_id,)).fetchone()
    if not link:
        return {"ok": False, "error": "unknown link"}
    try:
        res = ctx.cashfree.get_link(link_id)
    except cf.CashfreeError as e:
        if link["status"] in ("ACTIVE",):
            conn.execute("UPDATE payment_links SET status='UNKNOWN', updated_at=? WHERE link_id=?",
                         (db.now(conn, s.timezone).isoformat(), link_id))
        db.audit(conn, "status_lookup_failed", {"link_id": link_id, "error": str(e),
                 "note": "Balance unchanged. Not assumed paid or unpaid. No new link will be created."},
                 link["account_ref"], source=link["mode"])
        return {"ok": False, "changed": False, "error": f"Could not confirm status: {e}. Nothing changed."}
    return apply_link_status(ctx, link_id, res["link_status"], res["link_amount_paid"], source, res)


def reconcile_all(ctx) -> list[dict]:
    out = []
    for r in ctx.conn.execute("SELECT link_id FROM payment_links WHERE status IN ('ACTIVE','PARTIALLY_PAID','UNKNOWN')").fetchall():
        res = reconcile_link(ctx, r["link_id"])
        res["link_id"] = r["link_id"]
        out.append(res)
    return out


def handle_webhook(ctx, raw_body: bytes, timestamp: str | None, signature: str | None) -> tuple[int, dict]:
    """Verify the signature on the raw body, then apply. Fails closed."""
    conn, s = ctx.conn, ctx.settings
    if s.cashfree_mode != "live":
        return 503, {"ok": False, "error": "webhook ignored: Cashfree keys are not configured, signature cannot be verified"}
    if not cf.verify_webhook_signature(raw_body, timestamp, signature, s.cashfree_client_secret):
        db.audit(conn, "webhook_rejected", {"why": "signature did not verify"}, None, source="webhook")
        return 401, {"ok": False, "error": "signature verification failed"}
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return 400, {"ok": False, "error": "body is not JSON"}
    if payload.get("type") != "PAYMENT_LINK_EVENT":
        return 200, {"ok": True, "ignored": True, "why": f"event type {payload.get('type')} is not handled"}
    d = payload.get("data") or {}
    res = apply_link_status(ctx, str(d.get("link_id")), str(d.get("link_status")), float(d.get("link_amount_paid") or 0),
                            "webhook", payload)
    return 200, res
