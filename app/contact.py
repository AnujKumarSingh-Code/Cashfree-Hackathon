"""Borrower contact: fixed respectful script, recording disclosure, and reply handling.

The borrower only ever hears fixed wording with slots (amount, lender, grievance contact).
Nothing free-form is generated for the borrower, so the agent cannot threaten or shame.
A transcript is uncertain evidence. It can record a promise or a claim; it can never confirm a payment.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db, gates, payments, queue, signals
from .adapters import relay as relay_adapter
from .adapters.sarvam import SarvamError, TTS_LANGUAGES

SCRIPT_VERSION = "kramsetu-script-1.0"

# {lender} {amount} {min_part} {grievance}
SCRIPTS: dict[str, dict[str, str]] = {
    "hi-IN": {
        "greeting": "नमस्ते। मैं {lender} की ओर से बोल रहा हूँ।",
        "disclosure": "यह कॉल रिकॉर्ड की जा रही है।",
        "purpose": "आपकी किस्त के {amount} रुपये बकाया हैं।",
        "options": "आप अभी लिंक से भुगतान कर सकते हैं, या कम से कम {min_part} रुपये अभी देकर बाकी बाद में दे सकते हैं, या हमें वह तारीख बता सकते हैं जब आप भुगतान कर पाएँगे।",
        "human": "अगर आप किसी व्यक्ति से बात करना चाहते हैं, तो बता दीजिए।",
        "optout": "अगर आप चाहते हैं कि हम दोबारा कॉल न करें, तो वह भी बता सकते हैं।",
        "close": "धन्यवाद। शिकायत के लिए संपर्क: {grievance}।",
    },
    "en-IN": {
        "greeting": "Hello. I am calling on behalf of {lender}.",
        "disclosure": "This call is being recorded.",
        "purpose": "An instalment of {amount} rupees is overdue on your loan.",
        "options": "You can pay now using a link, or pay at least {min_part} rupees now and the rest later, or tell us the date on which you can pay.",
        "human": "If you would like to speak to a person, please say so.",
        "optout": "If you would prefer that we do not call again, you can tell us that too.",
        "close": "Thank you. For any grievance, contact: {grievance}.",
    },
    "ta-IN": {
        "greeting": "வணக்கம். நான் {lender} சார்பாக பேசுகிறேன்.",
        "disclosure": "இந்த அழைப்பு பதிவு செய்யப்படுகிறது.",
        "purpose": "உங்கள் தவணையில் {amount} ரூபாய் நிலுவையில் உள்ளது.",
        "options": "நீங்கள் இப்போதே லிங்க் மூலம் செலுத்தலாம், அல்லது குறைந்தது {min_part} ரூபாய் இப்போது செலுத்தி மீதியை பிறகு செலுத்தலாம், அல்லது எந்த தேதியில் செலுத்த முடியும் என்று சொல்லலாம்.",
        "human": "ஒரு நபரிடம் பேச விரும்பினால், சொல்லுங்கள்.",
        "optout": "நாங்கள் மீண்டும் அழைக்க வேண்டாம் என்றால், அதையும் சொல்லலாம்.",
        "close": "நன்றி. புகார்களுக்கு தொடர்பு: {grievance}.",
    },
}
LINE_ORDER = ["greeting", "disclosure", "purpose", "options", "human", "optout", "close"]


def build_script(acc: dict, settings) -> dict:
    lang = acc.get("language") if acc.get("language") in SCRIPTS else "hi-IN"
    amount = float(acc.get("outstanding") or 0)
    min_part = max(round(amount * settings.min_part_share), 1)
    slots = {"lender": settings.lender_name, "amount": f"{amount:,.0f}", "min_part": f"{min_part:,.0f}",
             "grievance": settings.grievance_contact}
    lines = [{"key": k, "text": SCRIPTS[lang][k].format(**slots)} for k in LINE_ORDER]
    return {"language": lang, "requested_language": acc.get("language"), "version": SCRIPT_VERSION, "lines": lines,
            "fallback_note": None if acc.get("language") == lang else
            "Preferred language not available in the script set or not known; Hindi used."}


def _acc(conn, ref):
    r = conn.execute("SELECT * FROM accounts WHERE account_ref=?", (ref,)).fetchone()
    return dict(r) if r else None


def start_contact(ctx, ref: str, actor: str = "manager") -> dict:
    """A person approves each contact. Gates are checked again at this moment."""
    conn, s = ctx.conn, ctx.settings
    acc = _acc(conn, ref)
    if not acc:
        return {"ok": False, "error": "account not found"}
    now = db.now(conn, s.timezone)
    refusal = gates.contact_refusal(acc, now, s)
    if refusal:
        db.audit(conn, "contact_refused", {"why": refusal}, ref, actor=actor)
        return {"ok": False, "refused": True, "error": "Contact not made. " + refusal}
    script = build_script(acc, s)
    mode, relay_note, audio_path = "simulated", None, None
    if s.relay_mode == "relay":
        payload = {"event": "collections_contact", "account_ref": ref, "amount": acc["outstanding"],
                   "min_part_amount": max(round(acc["outstanding"] * s.min_part_share), 1),
                   # the call always goes to the one approved demo number, never to a synthetic borrower number
                   "customer": {"name": acc["name"], "phone": s.demo_call_number, "language": script["language"]},
                   "script": [l["text"] for l in script["lines"]], "recording_disclosure": script["lines"][1]["text"],
                   "grievance_contact": s.grievance_contact,
                   "callback_url": f"{s.public_base_url}/api/relay/callback" if s.public_base_url else None}
        try:
            relay_adapter.trigger(s.relay_webhook_url, payload)
            mode = "relay"
        except relay_adapter.RelayError as e:
            relay_note = f"Relay trigger failed ({e}); fell back to a simulated call."
    if mode == "simulated":
        # optional: real Bulbul audio of the script, so the simulated call can still be played aloud
        try:
            text = " ".join(l["text"] for l in script["lines"])
            if script["language"] in TTS_LANGUAGES:
                audio = ctx.sarvam.speak(text, script["language"])
                d = Path(s.db_path).resolve().parent / "data" / "audio"
                d.mkdir(parents=True, exist_ok=True)
                p = d / f"call_{ref}_{now.strftime('%Y%m%d%H%M%S')}.wav"
                p.write_bytes(audio)
                audio_path = str(p)
        except SarvamError as e:
            relay_note = (relay_note + " " if relay_note else "") + str(e)
    cur = conn.execute("INSERT INTO contacts(account_ref,ts,language,mode,disclosure_given,script_json,audio_path,outcome)"
                       " VALUES(?,?,?,?,?,?,?,?)",
                       (ref, now.isoformat(), script["language"], mode, 1, json.dumps(script, ensure_ascii=False),
                        audio_path, "awaiting_reply"))
    conn.execute("UPDATE accounts SET state='contacted', attempts_7d=COALESCE(attempts_7d,0)+1, last_contact_at=?,"
                 " last_contact_outcome='awaiting_reply' WHERE account_ref=?", (now.isoformat(), ref))
    db.audit(conn, "contacted", {"contact_id": cur.lastrowid, "mode": mode, "language": script["language"],
             "recording_disclosed": True, "script_version": SCRIPT_VERSION, "phone": db.mask_phone(acc["phone"]),
             "within_window": True, "window": f"{s.window_start}-{s.window_end} {s.timezone}", "note": relay_note,
             "label": "SIMULATED CALL" if mode == "simulated" else "Relay voice call requested"},
             ref, actor=actor, source=mode)
    return {"ok": True, "contact_id": cur.lastrowid, "mode": mode, "script": script, "note": relay_note,
            "audio": bool(audio_path)}


def record_reply(ctx, ref: str, transcript: str, transcript_source: str = "typed", actor: str = "agent") -> dict:
    """Turn the borrower's words into one event. Low confidence goes to a person."""
    conn, s = ctx.conn, ctx.settings
    acc = _acc(conn, ref)
    if not acc:
        return {"ok": False, "error": "account not found"}
    now = db.now(conn, s.timezone)
    sig = signals.extract(transcript, now.date())
    c = conn.execute("SELECT id FROM contacts WHERE account_ref=? ORDER BY id DESC LIMIT 1", (ref,)).fetchone()
    confident = sig.confidence >= s.intent_confidence_floor
    intent = sig.intent if confident else "unclear"
    result: dict = {"ok": True, "transcript": transcript, "signal": sig.to_dict(), "acted_on": intent}
    outcome = intent

    def set_state(state, hold=None, **extra):
        cols = ["state=?", "hold_reason=?", "last_contact_outcome=?"] + [f"{k}=?" for k in extra]
        conn.execute(f"UPDATE accounts SET {','.join(cols)} WHERE account_ref=?",
                     [state, hold, outcome, *extra.values(), ref])

    ev = f" (heard: \"{sig.evidence}\")" if sig.evidence else ""
    if intent in ("pay_now", "part_pay"):
        if intent == "part_pay" and sig.pay_date:
            conn.execute("UPDATE accounts SET stated_pay_date=?, promise_open=1, promises_made=COALESCE(promises_made,0)+1"
                         " WHERE account_ref=?", (sig.pay_date, ref))
        conn.execute("UPDATE accounts SET state='ranked', last_contact_outcome=? WHERE account_ref=?", (outcome, ref))
        summary = ("Borrower can pay part now" if intent == "part_pay" else "Borrower agreed to pay now") + ev
        db.audit(conn, "reply_understood", {"intent": intent, "evidence": sig.evidence, "confidence": sig.confidence,
                 "transcript": transcript, "transcript_source": transcript_source, "summary": summary,
                 "note": "Intent only. No payment is recorded until Cashfree confirms."}, ref, actor=actor)
        link_res = payments.create_link_for_account(
            ctx, ref, actor, "borrower asked for a part-payment link" if intent == "part_pay" else "borrower agreed to pay now")
        result["link"] = link_res
        result["summary"] = summary + ". Payment link sent; waiting for Cashfree."
    else:
        if intent == "promise":
            set_state("promise_recorded", None, stated_pay_date=sig.pay_date, promise_open=1,
                      promises_made=(acc["promises_made"] or 0) + 1)
            summary = f"Promise recorded for {sig.pay_date}{ev}. This is a promise, not a payment."
            cause = f"borrower promised to pay on {sig.pay_date}"
        elif intent == "already_paid":
            set_state("claim_under_check", f"Borrower says already paid{ev}. Contact paused until verified.")
            summary = f"Already-paid claim{ev}. Contact paused; checking Cashfree."
            cause = "borrower says already paid; contact paused"
        elif intent == "hardship":
            set_state("hardship_hold", f"Hardship reported{ev}. Support path only: smaller amount or later date. No pressure.")
            summary = f"Hardship reported{ev}. Moved to support; no further automated contact."
            cause = "borrower reported hardship"
        elif intent == "dispute":
            set_state("disputed", f"Dispute raised{ev}. Routed to review. Grievance contact: {s.grievance_contact}")
            summary = f"Dispute raised{ev}. Routed to review."
            cause = "borrower disputed the amount"
        elif intent == "do_not_contact":
            set_state("do_not_contact", f"Borrower asked not to be called{ev}.", dnc=1)
            summary = f"Do-not-contact request{ev}. No more calls."
            cause = "borrower asked not to be called"
        elif intent == "human":
            set_state("needs_review", f"Borrower asked to speak to a person{ev}.")
            summary = f"Asked for a person{ev}. Human callback needed."
            cause = "borrower asked for a person"
        else:
            set_state("needs_review", "Reply not understood with enough confidence. A person should listen to the call.")
            summary = "Reply unclear. Held for a person; nothing assumed."
            cause = "reply unclear; held for review"
        db.audit(conn, "reply_understood" if confident else "reply_unclear",
                 {"intent": sig.intent, "acted_on": intent, "evidence": sig.evidence, "confidence": sig.confidence,
                  "pay_date": sig.pay_date, "transcript": transcript, "transcript_source": transcript_source,
                  "summary": summary, "note": "Transcript is uncertain evidence, not a verified payment event."},
                 ref, actor=actor)
        queue.reform(conn, s, cause, ref, actor)
        result["summary"] = summary
        if intent == "already_paid":
            result["claim_check"] = check_claim(ctx, ref)
    if c:
        conn.execute("UPDATE contacts SET transcript=?, transcript_source=?, intent=?, confidence=?, evidence=?, outcome=?"
                     " WHERE id=?", (transcript, transcript_source, sig.intent, sig.confidence, sig.evidence, outcome, c["id"]))
    return result


def check_claim(ctx, ref: str) -> dict:
    """Already-paid claim: look for a Cashfree-confirmed payment. If none, a person verifies. No chasing."""
    conn, s = ctx.conn, ctx.settings
    links = conn.execute("SELECT link_id FROM payment_links WHERE account_ref=?", (ref,)).fetchall()
    for l in links:
        res = payments.reconcile_link(ctx, l["link_id"], "claim_check")
        if res.get("changed") and res.get("status") in ("PAID", "PARTIALLY_PAID"):
            db.audit(conn, "claim_verified", {"link_id": l["link_id"], "status": res["status"]}, ref, source=ctx.cashfree.mode)
            return {"verified": True, "message": res["message"]}
    msg = ("Cashfree shows no payment for this account. The claim may be an offline payment (for example at a branch). "
           "Held for a person to check the receipt. No further contact meanwhile.")
    db.audit(conn, "claim_not_verified", {"links_checked": len(links), "note": msg}, ref, source=ctx.cashfree.mode)
    return {"verified": False, "message": msg}


def review(ctx, ref: str, action: str, note: str = "", actor: str = "manager") -> dict:
    """Human decisions on held accounts. Recorded with the person's note."""
    conn, s = ctx.conn, ctx.settings
    acc = _acc(conn, ref)
    if not acc:
        return {"ok": False, "error": "account not found"}
    if action == "release":
        flags = json.loads(acc["flags_json"] or "[]")
        if flags:
            return {"ok": False, "error": "Cannot release: the imported data still has unresolved flags. Fix the source file and re-import."}
        conn.execute("UPDATE accounts SET state='eligible', hold_reason=NULL, dnc=0 WHERE account_ref=?", (ref,))
        cause = "released to the queue by a person"
    elif action == "confirm_paid_offline":
        conn.execute("UPDATE accounts SET state='paid', status='paid', outstanding=0, hold_reason=? WHERE account_ref=?",
                     (f"Marked paid by {actor} after checking an offline receipt. Not a Cashfree confirmation. Note: {note}", ref))
        cause = "offline payment confirmed by a person (not Cashfree)"
    elif action == "keep_hold":
        cause = "hold kept by a person"
    else:
        return {"ok": False, "error": "unknown action"}
    db.audit(conn, "human_review", {"action": action, "note": note, "previous_state": acc["state"]}, ref,
             actor=actor, source="human")
    queue.reform(conn, s, cause, ref, actor)
    return {"ok": True, "message": cause}


def transcribe_reply(ctx, ref: str, audio: bytes, filename: str) -> dict:
    """Saaras turns reply audio into text; then the same rule-based path as a typed transcript."""
    acc = _acc(ctx.conn, ref)
    if not acc:
        return {"ok": False, "error": "account not found"}
    try:
        res = ctx.sarvam.transcribe(audio, filename, mode="codemix")
    except SarvamError as e:
        return {"ok": False, "error": str(e)}
    out = record_reply(ctx, ref, res["transcript"], "sarvam_saaras")
    out["stt"] = {"language_code": res.get("language_code"), "language_probability": res.get("language_probability")}
    return out
