import dataclasses
from datetime import datetime
from zoneinfo import ZoneInfo

from app import contact, db, queue
from tests.conftest import acc

Z = ZoneInfo("Asia/Kolkata")


def test_recording_disclosure_is_in_every_script_and_comes_before_the_amount(settings):
    for lang in contact.SCRIPTS:
        s = contact.build_script({"language": lang, "outstanding": 5000}, settings)
        keys = [l["key"] for l in s["lines"]]
        assert keys.index("disclosure") < keys.index("purpose")
        assert all(l["text"].strip() for l in s["lines"])
        assert settings.grievance_contact in s["lines"][-1]["text"]
        assert {"human", "optout"} <= set(keys)


def test_script_has_no_threat_or_pressure_words(settings):
    banned = ["legal action", "police", "court", "seize", "jail", "shame", "last warning", "कानूनी", "पुलिस", "जेल", "அபராதம்"]
    for lang in contact.SCRIPTS:
        text = " ".join(l["text"] for l in contact.build_script({"language": lang, "outstanding": 5000}, settings)["lines"]).lower()
        assert not [w for w in banned if w in text]


def test_unknown_language_falls_back_to_hindi_with_a_note(settings):
    s = contact.build_script({"language": None, "outstanding": 100}, settings)
    assert s["language"] == "hi-IN" and s["fallback_note"]


def test_contact_is_refused_outside_the_window(demo):
    db.set_clock(demo.conn, datetime(2026, 11, 21, 20, 30, tzinfo=Z))
    res = contact.start_contact(demo, "KS-1001")
    assert res["refused"] and "window" in res["error"]
    assert acc(demo, "KS-1001")["attempts_7d"] == 0
    assert demo.conn.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"] == 0
    assert demo.conn.execute("SELECT COUNT(*) n FROM audit_events WHERE kind='contact_refused'").fetchone()["n"] == 1


def test_contact_is_refused_for_held_capped_and_dnc_accounts(demo):
    for ref in ("KS-1009", "KS-1011", "KS-1012", "KS-1014", "KS-1008"):
        res = contact.start_contact(demo, ref)
        assert res.get("refused"), ref


def test_contact_records_disclosure_and_counts_the_attempt(demo):
    res = contact.start_contact(demo, "KS-1001")
    assert res["ok"] and res["mode"] == "simulated"
    c = demo.conn.execute("SELECT * FROM contacts WHERE account_ref='KS-1001'").fetchone()
    assert c["disclosure_given"] == 1 and c["mode"] == "simulated"
    assert acc(demo, "KS-1001")["attempts_7d"] == 1


def test_promise_is_recorded_as_a_promise_not_a_payment(demo):
    contact.start_contact(demo, "KS-1004")
    contact.record_reply(demo, "KS-1004", "salary 25 tarikh ko aayegi")
    a = acc(demo, "KS-1004")
    assert a["state"] == "promise_recorded" and a["lane"] == "call_on_date" and a["stated_pay_date"] == "2026-11-25"
    assert a["outstanding"] == 5400 and a["promises_made"] == 2 and a["promises_kept"] == 0
    assert contact.start_contact(demo, "KS-1004").get("refused")       # not before the promised date


def test_promise_lapse_returns_the_account_with_an_explanation(demo):
    contact.start_contact(demo, "KS-1004")
    contact.record_reply(demo, "KS-1004", "kal de dunga")
    db.advance_clock(demo.conn, 2 * 1440)
    queue.reform(demo.conn, demo.settings, "clock advanced", "KS-1004")
    a = acc(demo, "KS-1004")
    assert a["state"] == "ranked" and a["position"] is not None and a["stated_pay_date"] is None
    assert a["reason"].startswith("Promise date passed")
    assert a["promises_made"] == 2 and a["promises_kept"] == 0       # follow-through now counts the lapse
    assert demo.conn.execute("SELECT COUNT(*) n FROM audit_events WHERE kind='promise_lapsed'").fetchone()["n"] == 1


def test_kept_promise_is_credited_only_on_cashfree_confirmation(demo):
    from app import payments
    contact.start_contact(demo, "KS-1004")
    contact.record_reply(demo, "KS-1004", "thoda abhi de sakta hoon, baaki 25 tarikh ko")
    link = demo.conn.execute("SELECT * FROM payment_links WHERE account_ref='KS-1004'").fetchone()
    assert acc(demo, "KS-1004")["promises_kept"] == 0
    demo.cashfree.simulate_payment(link["link_id"], 2000)
    payments.reconcile_link(demo, link["link_id"])
    assert acc(demo, "KS-1004")["promises_kept"] == 1


def test_already_paid_claim_pauses_contact_and_is_not_trusted(demo):
    contact.start_contact(demo, "KS-1002")
    res = contact.record_reply(demo, "KS-1002", "maine to kal branch mein de diya")
    a = acc(demo, "KS-1002")
    assert a["state"] == "claim_under_check" and a["outstanding"] == 6200 and a["position"] is None
    assert res["claim_check"]["verified"] is False
    assert contact.start_contact(demo, "KS-1002").get("refused")


def test_already_paid_claim_is_verified_when_cashfree_confirms(demo):
    link = (contact.start_contact(demo, "KS-1001"), contact.record_reply(demo, "KS-1001", "link bhej do"))[1]["link"]["link"]
    demo.cashfree.simulate_payment(link["link_id"], link["amount"])     # paid, but our app has not polled yet
    res = contact.record_reply(demo, "KS-1001", "maine pay kar diya")
    assert res["claim_check"]["verified"] is True and acc(demo, "KS-1001")["state"] == "paid"


def test_hardship_and_dispute_reduce_pressure(demo):
    contact.start_contact(demo, "KS-1005")
    contact.record_reply(demo, "KS-1005", "naukri chali gayi hai")
    contact.start_contact(demo, "KS-1006")
    contact.record_reply(demo, "KS-1006", "amount galat hai")
    assert acc(demo, "KS-1005")["state"] == "hardship_hold" and acc(demo, "KS-1006")["state"] == "disputed"
    for ref in ("KS-1005", "KS-1006"):
        assert acc(demo, ref)["position"] is None and contact.start_contact(demo, ref).get("refused")
    assert demo.conn.execute("SELECT COUNT(*) n FROM payment_links").fetchone()["n"] == 0


def test_do_not_contact_request_is_honoured(demo):
    contact.start_contact(demo, "KS-1001")
    contact.record_reply(demo, "KS-1001", "mujhe call mat karo")
    a = acc(demo, "KS-1001")
    assert a["dnc"] == 1 and a["state"] == "do_not_contact" and contact.start_contact(demo, "KS-1001").get("refused")


def test_unclear_reply_goes_to_a_person_and_changes_nothing_else(demo):
    contact.start_contact(demo, "KS-1001")
    res = contact.record_reply(demo, "KS-1001", "hmm dekhte hain")
    a = acc(demo, "KS-1001")
    assert res["acted_on"] == "unclear" and a["state"] == "needs_review" and a["outstanding"] == 8400
    assert demo.conn.execute("SELECT COUNT(*) n FROM payment_links").fetchone()["n"] == 0


def test_human_review_actions_are_audited(demo):
    contact.start_contact(demo, "KS-1002")
    contact.record_reply(demo, "KS-1002", "branch mein de diya")
    res = contact.review(demo, "KS-1002", "confirm_paid_offline", "receipt 4471")
    a = acc(demo, "KS-1002")
    assert res["ok"] and a["state"] == "paid" and "Not a Cashfree confirmation" in a["hold_reason"]
    ev = demo.conn.execute("SELECT source FROM audit_events WHERE kind='human_review'").fetchone()
    assert ev["source"] == "human"
    assert not contact.review(demo, "KS-1009", "release")["ok"]         # flagged data cannot simply be released


def test_live_calls_only_go_to_the_approved_demo_number(demo, monkeypatch):
    sent = {}
    from app.adapters import relay
    monkeypatch.setattr(relay, "trigger", lambda url, payload, **kw: sent.update(payload) or {"status_code": 200})
    demo.settings = dataclasses.replace(demo.settings, relay_webhook_url="https://relay.invalid/hook",
                                        demo_call_number="9876500000", force_mock=False)
    res = contact.start_contact(demo, "KS-1001")
    assert res["mode"] == "relay" and sent["customer"]["phone"] == "9876500000"
    assert sent["customer"]["phone"] != acc(demo, "KS-1001")["phone"]
    assert sent["recording_disclosure"] and sent["customer"]["language"] == "hi-IN"


def test_without_a_demo_number_the_call_is_simulated(demo):
    demo.settings = dataclasses.replace(demo.settings, relay_webhook_url="https://relay.invalid/hook", force_mock=False)
    assert demo.settings.relay_mode == "simulated"
