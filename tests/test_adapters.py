"""The live adapters are checked against the documented request shapes using a fake transport.
No request leaves the machine."""
import base64
import json

import httpx
import pytest

from app.adapters import cashfree as cf, relay, sarvam as sv


def test_cashfree_create_link_request_matches_the_docs():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(method=req.method, url=str(req.url), headers=dict(req.headers), body=json.loads(req.content))
        return httpx.Response(200, json={"cf_link_id": 1996567, "link_id": "ks_A1_1", "link_status": "ACTIVE", "link_amount": 5000,
                                         "link_amount_paid": 0, "link_url": "https://payments-test.cashfree.com/links/abc"})
    a = cf.CashfreeLive("APPID", "SECRET", "https://sandbox.cashfree.com/pg", "2026-01-01", transport=httpx.MockTransport(handler))
    res = a.create_link("ks_A1_1", 5000, "Overdue instalment", {"phone": "9000000001", "name": "Test"}, True, 1000,
                        "2026-11-28T11:00:00+05:30", {"account_ref": "A1"}, "https://example.invalid/hook")
    assert seen["method"] == "POST" and seen["url"] == "https://sandbox.cashfree.com/pg/links"
    h = seen["headers"]
    assert h["x-client-id"] == "APPID" and h["x-client-secret"] == "SECRET" and h["x-api-version"] == "2026-01-01"
    assert h["x-idempotency-key"] == cf.idempotency_key("ks_A1_1")
    b = seen["body"]
    assert b["link_id"] == "ks_A1_1" and b["link_amount"] == 5000 and b["link_currency"] == "INR" and b["link_purpose"]
    assert b["customer_details"]["customer_phone"] == "9000000001"
    assert b["link_partial_payments"] is True and b["link_minimum_partial_amount"] == 1000
    assert b["link_meta"]["notify_url"] == "https://example.invalid/hook" and b["link_notes"] == {"account_ref": "A1"}
    assert res == {"link_id": "ks_A1_1", "cf_link_id": "1996567", "link_url": "https://payments-test.cashfree.com/links/abc",
                   "link_status": "ACTIVE", "link_amount": 5000.0, "link_amount_paid": 0.0, "link_expiry_time": None}


def test_cashfree_idempotency_key_is_stable_per_link():
    assert cf.idempotency_key("ks_A1_1") == cf.idempotency_key("ks_A1_1") != cf.idempotency_key("ks_A1_2")


def test_cashfree_fetch_uses_get_links_path_and_normalises_status():
    def handler(req):
        assert req.method == "GET" and req.url.path == "/pg/links/ks_A1_1"
        return httpx.Response(200, json={"link_id": "ks_A1_1", "link_status": "PARTIALLY_PAID", "link_amount": 200.12, "link_amount_paid": 55})
    a = cf.CashfreeLive("i", "s", "https://sandbox.cashfree.com/pg", "2026-01-01", transport=httpx.MockTransport(handler))
    r = a.get_link("ks_A1_1")
    assert r["link_status"] == "PARTIALLY_PAID" and r["link_amount_paid"] == 55.0


def test_cashfree_errors_do_not_leak_secrets_and_raise():
    a = cf.CashfreeLive("APPID", "TOPSECRET", "https://sandbox.cashfree.com/pg", "2026-01-01",
                        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "authentication Failed"})))
    with pytest.raises(cf.CashfreeError) as e:
        a.get_link("x")
    assert e.value.status_code == 401 and "TOPSECRET" not in str(e.value)

    def boom(req):
        raise httpx.ConnectError("no route")
    b = cf.CashfreeLive("APPID", "TOPSECRET", "https://sandbox.cashfree.com/pg", "2026-01-01", transport=httpx.MockTransport(boom))
    with pytest.raises(cf.CashfreeError) as e:
        b.get_link("x")
    assert "TOPSECRET" not in str(e.value)


def test_production_is_never_used(settings):
    import dataclasses
    s = dataclasses.replace(settings, cashfree_client_id="a", cashfree_client_secret="b", cashfree_env="production", force_mock=False)
    assert s.cashfree_mode == "mock" and "sandbox" in s.cashfree_base_url


def test_mock_adapter_enforces_minimum_first_amount(ctx):
    m = cf.CashfreeMock(ctx.conn)
    m.create_link("L1", 1000, "p", {"phone": "9"}, True, 200, None)
    with pytest.raises(cf.CashfreeError):
        m.simulate_payment("L1", 50)
    assert m.simulate_payment("L1", 200)["link_status"] == "PARTIALLY_PAID"
    assert m.simulate_payment("L1", 800)["link_status"] == "PAID"
    with pytest.raises(cf.CashfreeError):
        m.simulate_payment("L1", 1)


def test_sarvam_stt_request_matches_the_docs():
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), key=req.headers.get("api-subscription-key"), ctype=req.headers["content-type"], body=req.content)
        return httpx.Response(200, json={"request_id": "r1", "transcript": "salary 5th ko aayega", "language_code": "hi-IN", "language_probability": 0.97})
    a = sv.SarvamLive("KEY", transport=httpx.MockTransport(handler))
    r = a.transcribe(b"RIFFfake", "reply.wav")
    assert seen["url"] == "https://api.sarvam.ai/speech-to-text" and seen["key"] == "KEY"
    assert seen["ctype"].startswith("multipart/form-data")
    assert b'name="model"' in seen["body"] and b"saaras:v4" in seen["body"] and b"codemix" in seen["body"] and b'name="file"' in seen["body"]
    assert r["transcript"] == "salary 5th ko aayega" and r["language_code"] == "hi-IN"


def test_sarvam_tts_request_matches_the_docs():
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), key=req.headers.get("api-subscription-key"), body=json.loads(req.content))
        return httpx.Response(200, json={"request_id": "r2", "audios": [base64.b64encode(b"WAVDATA").decode()]})
    a = sv.SarvamLive("KEY", transport=httpx.MockTransport(handler))
    audio = a.speak("नमस्ते", "hi-IN")
    assert seen["url"] == "https://api.sarvam.ai/text-to-speech" and seen["key"] == "KEY"
    assert seen["body"] == {"text": "नमस्ते", "language_code": "hi-IN", "model": "bulbul:v3", "speaker": "shubh"}
    assert audio == b"WAVDATA"


def test_sarvam_errors_are_clear():
    a = sv.SarvamLive("KEY", transport=httpx.MockTransport(lambda r: httpx.Response(402, json={})))
    with pytest.raises(sv.SarvamError, match="credits"):
        a.speak("hi", "hi-IN")
    with pytest.raises(sv.SarvamError, match="Bulbul"):
        a.speak("hi", "xx-IN")
    with pytest.raises(sv.SarvamError, match="not configured"):
        sv.SarvamMock().transcribe(b"x")


def test_relay_trigger_posts_json_and_raises_on_failure():
    seen = {}

    def handler(req):
        seen.update(method=req.method, body=json.loads(req.content))
        return httpx.Response(200, json={"ok": True})
    assert relay.trigger("https://relay.invalid/hook", {"event": "collections_contact"}, transport=httpx.MockTransport(handler))["status_code"] == 200
    assert seen == {"method": "POST", "body": {"event": "collections_contact"}}
    with pytest.raises(relay.RelayError):
        relay.trigger("https://relay.invalid/hook", {}, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
