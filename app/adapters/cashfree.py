"""Cashfree Payment Links adapter.

Verified against the official API reference (payments v2026-01-01) on 2026-11-21:
  POST {base}/links                      create
  GET  {base}/links/{link_id}            fetch details (link_status, link_amount_paid)
  GET  {base}/links/{link_id}/orders     orders for a link
  POST {base}/links/{link_id}/cancel     cancel
  headers: x-client-id, x-client-secret, x-api-version, optional x-idempotency-key (UUID)
  sandbox base: https://sandbox.cashfree.com/pg
Webhook: type PAYMENT_LINK_EVENT; headers x-webhook-timestamp and x-webhook-signature;
  expected = Base64(HMAC-SHA256(timestamp + rawBody, secretKey)).

CashfreeMock implements the same interface. Anything it returns is labelled MOCK
and must never be presented as a real Cashfree payment.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid

import httpx

OPEN_STATUSES = ("ACTIVE", "PARTIALLY_PAID", "UNKNOWN")
KNOWN_STATUSES = ("ACTIVE", "PAID", "PARTIALLY_PAID", "EXPIRED", "CANCELLED")
_NS = uuid.UUID("6f1d3c0e-5b0a-4c57-9d59-6b7a52a1c0de")


class CashfreeError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def idempotency_key(link_id: str) -> str:
    """Stable per link_id, so a retried create can never produce a second link."""
    return str(uuid.uuid5(_NS, link_id))


def normalize(body: dict) -> dict:
    return {
        "link_id": body.get("link_id"),
        "cf_link_id": str(body.get("cf_link_id")) if body.get("cf_link_id") is not None else None,
        "link_url": body.get("link_url"),
        "link_status": str(body.get("link_status") or "UNKNOWN").upper(),
        "link_amount": float(body.get("link_amount") or 0),
        "link_amount_paid": float(body.get("link_amount_paid") or 0),
        "link_expiry_time": body.get("link_expiry_time"),
    }


class CashfreeLive:
    mode = "live"

    def __init__(self, client_id: str, client_secret: str, base_url: str, api_version: str,
                 timeout: float = 20.0, transport: httpx.BaseTransport | None = None):
        self._secret = client_secret
        self._client = httpx.Client(
            base_url=base_url, timeout=timeout, transport=transport,
            headers={"x-client-id": client_id, "x-client-secret": client_secret,
                     "x-api-version": api_version, "Content-Type": "application/json", "Accept": "application/json"})

    def _call(self, method: str, path: str, **kw) -> dict | list:
        try:
            r = self._client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise CashfreeError(f"network error calling Cashfree: {type(e).__name__}") from e
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", "")
            except Exception:
                msg = r.text[:200]
            raise CashfreeError(f"Cashfree returned {r.status_code}: {msg}", r.status_code)
        return r.json()

    def create_link(self, link_id: str, amount: float, purpose: str, customer: dict, partial: bool,
                    min_partial: float | None, expiry_iso: str | None, notes: dict | None = None,
                    notify_url: str | None = None, send_sms: bool = True) -> dict:
        body: dict = {
            "link_id": link_id, "link_amount": round(float(amount), 2), "link_currency": "INR",
            "link_purpose": purpose[:500],
            "customer_details": {k: v for k, v in {
                "customer_phone": customer.get("phone"), "customer_name": customer.get("name"),
                "customer_email": customer.get("email")}.items() if v},
            "link_partial_payments": bool(partial),
            "link_notify": {"send_sms": bool(send_sms), "send_email": bool(customer.get("email"))},
            "link_auto_reminders": True,
        }
        if partial and min_partial:
            body["link_minimum_partial_amount"] = round(float(min_partial), 2)
        if expiry_iso:
            body["link_expiry_time"] = expiry_iso
        if notes:
            body["link_notes"] = {str(k): str(v) for k, v in notes.items()}
        if notify_url:
            body["link_meta"] = {"notify_url": notify_url}
        return normalize(self._call("POST", "/links", json=body,
                                    headers={"x-idempotency-key": idempotency_key(link_id)}))

    def get_link(self, link_id: str) -> dict:
        return normalize(self._call("GET", f"/links/{link_id}"))

    def get_link_orders(self, link_id: str) -> list:
        res = self._call("GET", f"/links/{link_id}/orders")
        return res if isinstance(res, list) else res.get("data", [])

    def cancel_link(self, link_id: str) -> dict:
        return normalize(self._call("POST", f"/links/{link_id}/cancel"))


class CashfreeMock:
    """Local stand-in used when keys or network are missing. Clearly labelled MOCK everywhere."""
    mode = "mock"

    def __init__(self, conn):
        self._conn = conn
        self.fail_lookups = False   # test hook: simulate a status lookup failure

    def _row(self, link_id: str):
        r = self._conn.execute("SELECT * FROM mock_cashfree_links WHERE link_id=?", (link_id,)).fetchone()
        if not r:
            raise CashfreeError("MOCK: link not found", 404)
        return r

    def _view(self, r) -> dict:
        body = json.loads(r["body_json"])
        body.update(link_status=r["status"], link_amount_paid=r["amount_paid"])
        return normalize(body)

    def create_link(self, link_id, amount, purpose, customer, partial, min_partial, expiry_iso,
                    notes=None, notify_url=None, send_sms=True) -> dict:
        existing = self._conn.execute("SELECT * FROM mock_cashfree_links WHERE link_id=?", (link_id,)).fetchone()
        if existing:   # same behaviour as an idempotent replay
            return self._view(existing)
        body = {"link_id": link_id, "cf_link_id": "MOCK-" + link_id, "link_amount": round(float(amount), 2),
                "link_url": f"mock://cashfree-link/{link_id}", "link_partial_payments": bool(partial),
                "link_minimum_partial_amount": min_partial, "link_expiry_time": expiry_iso}
        self._conn.execute("INSERT INTO mock_cashfree_links(link_id,body_json,status,amount_paid) VALUES(?,?,?,0)",
                           (link_id, json.dumps(body), "ACTIVE"))
        return self._view(self._row(link_id))

    def get_link(self, link_id: str) -> dict:
        if self.fail_lookups:
            raise CashfreeError("MOCK: simulated lookup failure")
        return self._view(self._row(link_id))

    def get_link_orders(self, link_id: str) -> list:
        return []

    def cancel_link(self, link_id: str) -> dict:
        self._row(link_id)
        self._conn.execute("UPDATE mock_cashfree_links SET status='CANCELLED' WHERE link_id=?", (link_id,))
        return self._view(self._row(link_id))

    # ---- mock-only controls, used by the demo buttons ----
    def simulate_payment(self, link_id: str, amount: float) -> dict:
        r = self._row(link_id)
        body = json.loads(r["body_json"])
        if r["status"] not in ("ACTIVE", "PARTIALLY_PAID"):
            raise CashfreeError(f"MOCK: link is {r['status']}, cannot be paid")
        paid = round(r["amount_paid"] + float(amount), 2)
        if paid > body["link_amount"] + 0.001:
            raise CashfreeError("MOCK: payment exceeds the link amount")
        minimum = body.get("link_minimum_partial_amount") or 0
        if paid < body["link_amount"] and (not body.get("link_partial_payments") or (r["amount_paid"] == 0 and amount < minimum)):
            raise CashfreeError("MOCK: part payment not allowed or below the minimum first amount")
        status = "PAID" if paid >= body["link_amount"] - 0.001 else "PARTIALLY_PAID"
        self._conn.execute("UPDATE mock_cashfree_links SET status=?, amount_paid=? WHERE link_id=?", (status, paid, link_id))
        return self._view(self._row(link_id))

    def simulate_expire(self, link_id: str) -> dict:
        self._row(link_id)
        self._conn.execute("UPDATE mock_cashfree_links SET status='EXPIRED' WHERE link_id=? AND status IN ('ACTIVE','PARTIALLY_PAID')",
                           (link_id,))
        return self._view(self._row(link_id))


def expected_signature(raw_body: bytes, timestamp: str, secret: str) -> str:
    signed = timestamp.encode() + raw_body
    return base64.b64encode(hmac.new(secret.encode(), signed, hashlib.sha256).digest()).decode()


def verify_webhook_signature(raw_body: bytes, timestamp: str | None, signature: str | None, secret: str) -> bool:
    """Constant-time check of x-webhook-signature against the raw body. Fails closed."""
    if not (raw_body and timestamp and signature and secret):
        return False
    return hmac.compare_digest(expected_signature(raw_body, timestamp, secret), signature.strip())


def get_adapter(settings, conn):
    if settings.cashfree_mode == "live":
        return CashfreeLive(settings.cashfree_client_id, settings.cashfree_client_secret,
                            settings.cashfree_base_url, settings.cashfree_api_version)
    return CashfreeMock(conn)
