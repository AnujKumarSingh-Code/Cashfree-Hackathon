import base64
import hashlib
import hmac
import json

from app import contact, payments
from app.adapters import cashfree as cf
from tests.conftest import acc


def link_for(ctx, ref="KS-1001"):
    contact.start_contact(ctx, ref)
    return contact.record_reply(ctx, ref, "haan link bhej do")["link"]["link"]


def test_a_link_is_not_a_payment(demo):
    before = acc(demo, "KS-1001")["outstanding"]
    link = link_for(demo)
    a = acc(demo, "KS-1001")
    assert a["outstanding"] == before and a["state"] == "awaiting_payment" and link["status"] == "ACTIVE"
    assert a["position"] is None      # not called again while the link is open


def test_full_payment_closes_the_account(demo):
    link = link_for(demo)
    demo.cashfree.simulate_payment(link["link_id"], link["amount"])
    res = payments.reconcile_link(demo, link["link_id"])
    a = acc(demo, "KS-1001")
    assert res["changed"] and a["state"] == "paid" and a["outstanding"] == 0 and a["position"] is None


def test_part_payment_reduces_balance_but_does_not_close(demo):
    contact.start_contact(demo, "KS-1003")
    link = contact.record_reply(demo, "KS-1003", "aadha de sakti hoon")["link"]["link"]
    assert link["partial"] == 1 and link["min_part"] == 3000
    demo.cashfree.simulate_payment(link["link_id"], 7500)
    payments.reconcile_link(demo, link["link_id"])
    a = acc(demo, "KS-1003")
    assert a["state"] == "partially_paid" and a["outstanding"] == 7500 and a["status"] != "paid"


def test_duplicate_events_change_nothing_the_second_time(demo):
    link = link_for(demo, "KS-1003")
    first = payments.apply_link_status(demo, link["link_id"], "PARTIALLY_PAID", 5000, "webhook")
    balance = acc(demo, "KS-1003")["outstanding"]
    events = demo.conn.execute("SELECT COUNT(*) n FROM payment_events").fetchone()["n"]
    again = payments.apply_link_status(demo, link["link_id"], "PARTIALLY_PAID", 5000, "poll")
    assert first["changed"] and again["duplicate"] and not again["changed"]
    assert acc(demo, "KS-1003")["outstanding"] == balance == 10000
    assert demo.conn.execute("SELECT COUNT(*) n FROM payment_events").fetchone()["n"] == events


def test_no_second_link_while_one_is_open(demo):
    link = link_for(demo)
    again = payments.create_link_for_account(demo, "KS-1001")
    assert again["reused"] and again["link"]["link_id"] == link["link_id"]
    assert demo.conn.execute("SELECT COUNT(*) n FROM payment_links WHERE account_ref='KS-1001'").fetchone()["n"] == 1


def test_status_lookup_failure_changes_no_balance_and_blocks_new_links(demo):
    link = link_for(demo)
    demo.cashfree.fail_lookups = True
    res = payments.reconcile_link(demo, link["link_id"])
    a = acc(demo, "KS-1001")
    assert not res["ok"] and a["outstanding"] == 8400 and a["state"] == "awaiting_payment"
    assert demo.conn.execute("SELECT status FROM payment_links WHERE link_id=?", (link["link_id"],)).fetchone()["status"] == "UNKNOWN"
    assert payments.create_link_for_account(demo, "KS-1001")["reused"]      # unknown status: still no second link
    demo.cashfree.fail_lookups = False
    demo.cashfree.simulate_payment(link["link_id"], 8400)
    assert payments.reconcile_link(demo, link["link_id"])["status"] == "PAID"


def test_expired_link_returns_the_account_to_the_queue(demo):
    link = link_for(demo)
    demo.cashfree.simulate_expire(link["link_id"])
    payments.reconcile_link(demo, link["link_id"])
    a = acc(demo, "KS-1001")
    assert a["state"] == "ranked" and a["position"] is not None and a["outstanding"] == 8400


def test_cancelled_link_goes_to_review(demo):
    link = link_for(demo)
    demo.cashfree.cancel_link(link["link_id"])
    payments.reconcile_link(demo, link["link_id"])
    assert acc(demo, "KS-1001")["state"] == "needs_review"


def test_no_link_for_held_or_closed_accounts(demo):
    for ref in ("KS-1009", "KS-1011"):
        res = payments.create_link_for_account(demo, ref)
        assert not res["ok"], ref
    assert demo.conn.execute("SELECT COUNT(*) n FROM payment_links").fetchone()["n"] == 0


def test_unknown_link_or_status_is_ignored(demo):
    assert not payments.apply_link_status(demo, "nope", "PAID", 100, "webhook")["ok"]
    link = link_for(demo)
    assert not payments.apply_link_status(demo, link["link_id"], "TELEPORTED", 8400, "webhook")["ok"]
    assert acc(demo, "KS-1001")["outstanding"] == 8400


def test_queue_reforms_and_explains_after_payment(demo):
    link = link_for(demo)
    demo.cashfree.simulate_payment(link["link_id"], link["amount"])
    payments.reconcile_link(demo, link["link_id"])
    ch = demo.conn.execute("SELECT explanation FROM queue_changes WHERE account_ref='KS-1001' ORDER BY id DESC LIMIT 1").fetchone()
    assert "full payment" in ch["explanation"]
    assert acc(demo, "KS-1002")["position"] == 1       # next account moved up


# ---- webhook signature (scheme from Cashfree docs: Base64(HMAC-SHA256(timestamp + rawBody, secret))) ----
def _sign(body: bytes, ts: str, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), ts.encode() + body, hashlib.sha256).digest()).decode()


def test_webhook_signature_accepts_valid_and_rejects_tampered():
    body = json.dumps({"type": "PAYMENT_LINK_EVENT", "data": {"link_id": "x", "link_status": "PAID", "link_amount_paid": "10"}}).encode()
    sig = _sign(body, "1617695238078", "secret123")
    assert cf.verify_webhook_signature(body, "1617695238078", sig, "secret123")
    assert not cf.verify_webhook_signature(body + b" ", "1617695238078", sig, "secret123")
    assert not cf.verify_webhook_signature(body, "1617695238079", sig, "secret123")
    assert not cf.verify_webhook_signature(body, "1617695238078", sig, "other")
    assert not cf.verify_webhook_signature(body, None, sig, "secret123")
    assert not cf.verify_webhook_signature(body, "1617695238078", None, "secret123")


def test_webhook_is_refused_without_keys(demo):
    code, res = payments.handle_webhook(demo, b"{}", "1", "sig")
    assert code == 503 and not res["ok"]


def test_signed_webhook_applies_once_and_forged_one_is_rejected(demo):
    import dataclasses
    link = link_for(demo, "KS-1003")
    live = dataclasses.replace(demo.settings, cashfree_client_id="id", cashfree_client_secret="s3cret", force_mock=False)
    demo.settings = live
    payload = {"type": "PAYMENT_LINK_EVENT", "data": {"link_id": link["link_id"], "link_status": "PARTIALLY_PAID", "link_amount_paid": "4000.00"}}
    body = json.dumps(payload).encode()
    code, res = payments.handle_webhook(demo, body, "1700000000000", "forged")
    assert code == 401 and acc(demo, "KS-1003")["outstanding"] == 15000
    sig = _sign(body, "1700000000000", "s3cret")
    code, res = payments.handle_webhook(demo, body, "1700000000000", sig)
    assert code == 200 and res["changed"] and acc(demo, "KS-1003")["outstanding"] == 11000
    code, res = payments.handle_webhook(demo, body, "1700000000000", sig)       # redelivery
    assert code == 200 and res["duplicate"] and acc(demo, "KS-1003")["outstanding"] == 11000
