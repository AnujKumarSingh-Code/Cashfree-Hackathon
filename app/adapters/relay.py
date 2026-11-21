"""Cashfree Relay trigger.

A Relay agent with an "Inbound Webhook" trigger starts when an external system POSTs JSON to its
webhook URL (Relay docs, Create Relay Agent > Trigger types > Inbound Webhook). The Relay agent then
runs its own nodes: the Sarvam AI voice action places the call, and an HTTP node can post the outcome
back to this app at /api/relay/callback.
"""
from __future__ import annotations

import httpx


class RelayError(Exception):
    pass


def trigger(webhook_url: str, payload: dict, timeout: float = 15.0,
            transport: httpx.BaseTransport | None = None) -> dict:
    try:
        with httpx.Client(timeout=timeout, transport=transport) as c:
            r = c.post(webhook_url, json=payload)
    except httpx.HTTPError as e:
        raise RelayError(f"network error calling Relay: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise RelayError(f"Relay webhook returned {r.status_code}")
    return {"status_code": r.status_code}
