"""Direct Sarvam Voice Agents path. No request leaves the machine: a fake transport or monkeypatched adapter is used."""
import dataclasses
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import contact
from app.adapters import sarvam_voice as sv
from app.main import create_app
from tests.conftest import acc

VOICE = dict(sarvam_voice_api_key="VOICEKEY", sarvam_org_id="org1", sarvam_workspace_id="ws1", sarvam_agent_id="agent-1",
             sarvam_agent_version=1, sarvam_connection_id="conn-1", sarvam_caller_number="+918000000001",
             demo_call_number="9876500000", force_mock=False)


@pytest.fixture
def voice(demo, monkeypatch):
    demo.settings = dataclasses.replace(demo.settings, **VOICE)
    calls = []

    def fake_place(settings, phone, variables=None, webhook_url=None, metadata=None, transport=None):
        calls.append({"phone": phone, "variables": variables, "webhook_url": webhook_url, "metadata": metadata})
        return {"attempt_id": f"att-{len(calls)}"}
    monkeypatch.setattr(sv, "place_call", fake_place)
    demo.calls = calls
    return demo


def test_voice_mode_selection(settings):
    full = dataclasses.replace(settings, **VOICE)
    assert full.voice_mode == "sarvam"
    assert dataclasses.replace(full, relay_webhook_url="https://r.invalid/h", voice_provider="relay").voice_mode == "relay"
    assert dataclasses.replace(full, sarvam_voice_api_key="", relay_webhook_url="https://r.invalid/h").voice_mode == "relay"
    assert dataclasses.replace(full, sarvam_voice_api_key="").voice_mode == "simulated"
    assert dataclasses.replace(full, demo_call_number="").voice_mode == "simulated"       # no approved number, no live call
    assert dataclasses.replace(full, voice_provider="simulated").voice_mode == "simulated"
    assert dataclasses.replace(full, force_mock=True).voice_mode == "simulated"
    assert full.relay_mode == full.voice_mode


def test_place_call_request_matches_the_docs():
    s = dataclasses.make_dataclass("S", [(k, type(v)) for k, v in VOICE.items()])(**VOICE)
    seen = {}

    def handler(req):
        seen.update(method=req.method, url=str(req.url), key=req.headers.get("x-api-key"), body=json.loads(req.content))
        return httpx.Response(200, json={"attempt_id": "att-9"})
    res = sv.place_call(s, "+919876500000", {"amount": "8400"}, "https://app.invalid/api/sarvam/callback",
                        {"account_ref": "KS-1"}, transport=httpx.MockTransport(handler))
    assert res == {"attempt_id": "att-9"} and seen["method"] == "POST" and seen["key"] == "VOICEKEY"
    assert seen["url"] == "https://apps.sarvam.ai/api/outbounds/v1/orgs/org1/workspaces/ws1/outbounds"
    assert seen["body"] == {
        "app_config": {"app_id": "agent-1", "app_version": 1, "agent_variables": {"amount": "8400"},
                       "connection_config": {"connection_id": "conn-1", "agent_phone_number": "+918000000001"}},
        "user_config": {"user_phone_number": "+919876500000"},
        "webhook_config": {"url": "https://app.invalid/api/sarvam/callback", "metadata": {"account_ref": "KS-1"}}}


def test_attempt_lookup_and_transcript_urls():
    s = dataclasses.make_dataclass("S", [(k, type(v)) for k, v in VOICE.items()])(**VOICE)
    seen = []

    def handler(req):
        seen.append(req.url)
        if "attempts" in req.url.path:
            return httpx.Response(200, json={"items": [{"attempt_id": "other"}, {"attempt_id": "att-9", "interaction_id": "20261121/ab-15:39:02-x",
                                  "connectivity_status": "connected", "duration_in_seconds": 22.07, "end_datetime": "2026-11-21T10:09:30",
                                  "user_contact": "+919876500000"}]})
        return httpx.Response(200, json={"messages": [{"turn_id": 1, "role": "assistant", "content": "नमस्ते"}, {"turn_id": 2, "role": "user", "content": "हाँ"}]})
    t = httpx.MockTransport(handler)
    att = sv.find_attempt(s, "att-9", datetime(2026, 11, 21, 10, 9, tzinfo=timezone.utc), transport=t)
    assert att["interaction_id"] == "20261121/ab-15:39:02-x" and att["connectivity_status"] == "connected"
    assert "user_contact" not in att                                   # the unmasked number is never carried forward
    assert seen[0].path == "/api/analytics/v1/org1/ws1/agent-1/attempts"
    assert "start_datetime" in seen[0].params and "end_datetime" in seen[0].params and "+" not in str(seen[0].query)
    assert sv.find_attempt(s, "missing", datetime(2026, 11, 21, 10, 9, tzinfo=timezone.utc), transport=t) is None
    turns = sv.get_transcript(s, "20261121/ab-15:39:02-x", transport=t)
    assert turns == [{"role": "assistant", "content": "नमस्ते"}, {"role": "user", "content": "हाँ"}]
    assert seen[-1].raw_path.decode().endswith("/transcripts/20261121%2Fab-15%3A39%3A02-x")


def test_voice_errors_never_contain_the_key_or_a_phone():
    s = dataclasses.make_dataclass("S", [(k, type(v)) for k, v in VOICE.items()])(**VOICE)
    with pytest.raises(sv.SarvamVoiceError) as e:
        sv.place_call(s, "+919876500000", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"detail": "VOICEKEY bad +919876500000"})))
    assert e.value.status_code == 401 and "VOICEKEY" not in str(e.value) and "9876500000" not in str(e.value)

    def boom(req):
        raise httpx.ConnectError("x")
    with pytest.raises(sv.SarvamVoiceError):
        sv.place_call(s, "+919876500000", transport=httpx.MockTransport(boom))
    with pytest.raises(sv.SarvamVoiceError):
        sv.place_call(s, "+919876500000", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))


def test_sarvam_call_dials_only_the_demo_number(voice):
    res = contact.start_contact(voice, "KS-1001")
    assert res["mode"] == "sarvam" and res["attempt_id"] == "att-1"
    call = voice.calls[0]
    assert call["phone"] == "+919876500000" and acc(voice, "KS-1001")["phone"] not in call["phone"]
    v = call["variables"]
    assert v["account_ref"] == "KS-1001" and v["amount"] == "8400" and v["min_part_amount"] == "1680"
    assert v["recording_disclosure"] in v["script"] and v["customer_language"] == "hi-IN"
    assert call["webhook_url"] is None                                  # no public URL configured
    row = voice.conn.execute("SELECT mode, provider_ref, disclosure_given FROM contacts").fetchone()
    assert (row["mode"], row["provider_ref"], row["disclosure_given"]) == ("sarvam", "att-1", 1)


def test_webhook_url_is_sent_only_when_a_public_url_exists(voice):
    voice.settings = dataclasses.replace(voice.settings, public_base_url="https://app.invalid")
    contact.start_contact(voice, "KS-1001")
    assert voice.calls[0]["webhook_url"] == "https://app.invalid/api/sarvam/callback"


def test_sarvam_failure_falls_back_to_a_labelled_simulated_call(voice, monkeypatch):
    def fail(*a, **k):
        raise sv.SarvamVoiceError("Sarvam Voice returned 500", 500)
    monkeypatch.setattr(sv, "place_call", fail)
    res = contact.start_contact(voice, "KS-1001")
    assert res["ok"] and res["mode"] == "simulated" and "fell back" in res["note"]


def test_rejected_agent_variables_retry_without_them(voice, monkeypatch):
    seen = []

    def picky(settings, phone, variables=None, webhook_url=None, metadata=None, transport=None):
        seen.append(variables)
        if variables:
            raise sv.SarvamVoiceError("Sarvam Voice returned 422", 422)
        return {"attempt_id": "att-x"}
    monkeypatch.setattr(sv, "place_call", picky)
    res = contact.start_contact(voice, "KS-1001")
    assert res["mode"] == "sarvam" and seen[1] is None and "without them" in res["note"]


def test_contact_gates_still_apply_before_a_real_call(voice):
    assert contact.start_contact(voice, "KS-1011").get("refused")          # do not contact
    assert contact.start_contact(voice, "KS-1012").get("refused")          # attempt cap
    assert voice.calls == []


def _finish(monkeypatch, status="connected", turns=None, ended=True):
    monkeypatch.setattr(sv, "find_attempt", lambda s, aid, since, transport=None: {
        "attempt_id": aid, "interaction_id": "20261121/i1" if status == "connected" else None, "connectivity_status": status,
        "failure_reason": None, "duration_in_seconds": 21.0, "end_datetime": "2026-11-21T05:40:00" if ended else None})
    monkeypatch.setattr(sv, "get_transcript", lambda s, iid, transport=None: turns or [])


def test_sync_is_pending_until_sarvam_lists_the_call(voice, monkeypatch):
    contact.start_contact(voice, "KS-1001")
    monkeypatch.setattr(sv, "find_attempt", lambda *a, **k: None)
    assert contact.sync_call(voice, "KS-1001")["pending"]
    _finish(monkeypatch, turns=[], ended=True)
    assert contact.sync_call(voice, "KS-1001")["pending"]                  # transcript not ready
    _finish(monkeypatch, ended=False)
    assert contact.sync_call(voice, "KS-1001")["pending"]                  # call still in progress
    assert acc(voice, "KS-1001")["state"] == "contacted"


def test_sync_applies_a_promise_from_the_real_transcript_once(voice, monkeypatch):
    contact.start_contact(voice, "KS-1004")
    _finish(monkeypatch, turns=[{"role": "assistant", "content": "नमस्ते"}, {"role": "user", "content": "हाँ"},
                                {"role": "user", "content": "सैलरी 25 तारीख को आएगी"}])
    res = contact.sync_call(voice, "KS-1004")
    a = acc(voice, "KS-1004")
    assert res["connected"] and a["state"] == "promise_recorded" and a["stated_pay_date"] == "2026-11-25" and a["outstanding"] == 5400
    again = contact.sync_call(voice, "KS-1004")
    assert again["duplicate"] and acc(voice, "KS-1004")["promises_made"] == a["promises_made"]
    kinds = [r["kind"] for r in voice.conn.execute("SELECT kind FROM audit_events WHERE account_ref='KS-1004'")]
    assert "call_transcript" in kinds and "reply_understood" in kinds


def test_pay_now_on_the_call_creates_a_link_but_not_a_payment(voice, monkeypatch):
    contact.start_contact(voice, "KS-1001")
    _finish(monkeypatch, turns=[{"role": "user", "content": "हाँ, link भेज दो"}])
    res = contact.sync_call(voice, "KS-1001")
    a = acc(voice, "KS-1001")
    assert res["link"]["ok"] and a["state"] == "awaiting_payment" and a["outstanding"] == 8400


def test_unanswered_call_returns_the_account_to_the_queue(voice, monkeypatch):
    contact.start_contact(voice, "KS-1001")
    _finish(monkeypatch, status="no_answer")
    res = contact.sync_call(voice, "KS-1001")
    a = acc(voice, "KS-1001")
    assert res["connected"] is False and a["state"] == "ranked" and a["last_contact_outcome"] == "no_answer"
    assert a["attempts_7d"] == 1 and a["position"] is not None and a["outstanding"] == 8400


def test_silent_connected_call_goes_to_a_person(voice, monkeypatch):
    contact.start_contact(voice, "KS-1001")
    _finish(monkeypatch, turns=[{"role": "assistant", "content": "नमस्ते"}])
    contact.sync_call(voice, "KS-1001")
    assert acc(voice, "KS-1001")["state"] == "needs_review"


def test_webhook_unknown_attempt_is_refused_and_known_one_is_idempotent(voice, monkeypatch):
    contact.start_contact(voice, "KS-1005")
    monkeypatch.setattr(sv, "get_transcript", lambda *a, **k: [])
    c = TestClient(create_app(voice))
    assert c.post("/api/sarvam/callback", json={"attempt_id": "nope", "status": "connected"}).status_code == 404
    payload = {"attempt_id": "att-1", "status": "connected", "interaction_id": "20261121/i9", "duration": 12.5,
               "interaction_transcript": [{"role": "agent", "en_text": "Hello"}, {"role": "user", "en_text": "I lost my job, paise nahi hai"}]}
    r = c.post("/api/sarvam/callback", json=payload)
    assert r.status_code == 200 and acc(voice, "KS-1005")["state"] == "hardship_hold"
    assert c.post("/api/sarvam/callback", json=payload).json()["duplicate"]
    assert c.post("/api/accounts/KS-1005/sync-call").json()["duplicate"]
    assert c.post("/api/accounts/KS-1009/sync-call").json()["ok"] is False
    s = c.get("/api/status").json()
    assert s["modes"]["voice"] == "sarvam" and s["configured"]["SARVAM_VOICE_API_KEY"] is True and "VOICEKEY" not in c.get("/api/status").text
