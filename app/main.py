"""Kramsetu API and dashboard.

Run: uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from . import contact, db, gates, ingest, payments, queue, ranker, scenarios
from .context import make_ctx

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
SAMPLES = {"demo": "demo_borrowers.csv", "main": "borrowers_main.csv", "holdout": "borrowers_holdout.csv",
           "curveball": "borrowers_curveball.csv"}


def create_app(ctx=None) -> FastAPI:
    app = FastAPI(title="Kramsetu", description="Explainable, fair collections cockpit (synthetic data, Cashfree sandbox only)")
    app.state.ctx = ctx or make_ctx()
    _cache: dict = {}

    def C():
        return app.state.ctx

    def view(acc: dict, full: bool = False) -> dict:
        today = db.now(C().conn, C().settings.timezone).date()
        out = {k: acc[k] for k in ("account_ref", "name", "language", "area", "outstanding", "state", "hold_reason", "lane",
                                   "points", "reason", "position", "next_action", "is_exploration", "stated_pay_date",
                                   "attempts_7d", "last_contact_outcome", "batch", "note")}
        out["phone_masked"] = db.mask_phone(acc["phone"])
        out["days_overdue"] = ranker.effective_dpd(acc, today)
        out["breakdown"] = json.loads(acc["breakdown_json"]) if acc.get("breakdown_json") else []
        out["flags"] = json.loads(acc.get("flags_json") or "[]")
        if full:
            out["notes"] = json.loads(acc.get("notes_json") or "[]")
            out["original_row"] = json.loads(acc.get("original_json") or "{}")
            for k in list(out["original_row"]):
                if ingest._ALIAS_INDEX.get(ingest.norm_header(k)) == "phone":
                    out["original_row"][k] = db.mask_phone(out["original_row"][k])
            out["ranking_inputs"] = {k: acc.get(k) for k in ranker.ALLOWLIST}
            out["ranking_inputs"]["days_overdue"] = out["days_overdue"]
        return out

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def status():
        c, s = C().conn, C().settings
        now = db.now(c, s.timezone)
        counts = {r["state"]: r["n"] for r in c.execute("SELECT state, COUNT(*) n FROM accounts GROUP BY state")}
        lanes = {r["lane"]: r["n"] for r in c.execute("SELECT lane, COUNT(*) n FROM accounts WHERE lane IS NOT NULL GROUP BY lane")}
        batch = c.execute("SELECT * FROM import_batches ORDER BY imported_at DESC LIMIT 1").fetchone()
        return {
            "product": "Kramsetu", "rule_version": ranker.RULE_VERSION,
            "now": now.isoformat(), "clock_simulated": db.clock_is_simulated(c),
            "contact_window": {"start": s.window_start, "end": s.window_end, "timezone": s.timezone,
                               "open_now": gates.within_window(now, s.window_start, s.window_end)},
            "modes": {"cashfree": C().cashfree.mode, "sarvam": C().sarvam.mode, "voice": s.relay_mode},
            "configured": {"CASHFREE_CLIENT_ID": bool(s.cashfree_client_id), "CASHFREE_CLIENT_SECRET": bool(s.cashfree_client_secret),
                           "SARVAM_API_KEY": bool(s.sarvam_api_key), "RELAY_WEBHOOK_URL": bool(s.relay_webhook_url),
                           "DEMO_CALL_NUMBER": bool(s.demo_call_number), "PUBLIC_BASE_URL": bool(s.public_base_url)},
            "capacity": s.daily_call_capacity, "max_attempts_per_week": s.max_attempts_per_week,
            "accounts": sum(counts.values()), "states": counts, "lanes": lanes,
            "last_import": ({**dict(batch), "mapping": json.loads(batch["mapping_json"]), "unmapped": json.loads(batch["unmapped_json"]),
                             "dropped_sensitive": json.loads(batch["dropped_sensitive_json"])} if batch else None),
            "data_notice": "Synthetic data and Cashfree sandbox only.",
        }

    def _import(content: bytes, filename: str):
        s = ingest.import_file(C().conn, content, filename)
        s["queue"] = queue.reform(C().conn, C().settings, f"file imported: {filename}")
        _cache.clear()
        return s

    @app.post("/api/import")
    async def import_upload(file: UploadFile = File(...)):
        content = await file.read()
        if not content:
            raise HTTPException(400, "empty file")
        try:
            return _import(content, file.filename or "upload.csv")
        except Exception as e:
            raise HTTPException(400, f"could not read the file as a table: {type(e).__name__}: {e}")

    @app.post("/api/import-sample/{name}")
    def import_sample(name: str):
        if name not in SAMPLES:
            raise HTTPException(404, "unknown sample")
        p = ROOT / "data" / SAMPLES[name]
        return _import(p.read_bytes(), p.name)

    @app.get("/api/queue")
    def get_queue(lane: str = "call_now", limit: int = 100, offset: int = 0):
        c = C().conn
        if lane == "call_now":
            rows = c.execute("SELECT * FROM accounts WHERE position IS NOT NULL ORDER BY position LIMIT ? OFFSET ?", (limit, offset))
        else:
            rows = c.execute("SELECT * FROM accounts WHERE lane=? ORDER BY stated_pay_date, points DESC, account_ref LIMIT ? OFFSET ?",
                             (lane, limit, offset))
        total = c.execute("SELECT COUNT(*) n FROM accounts WHERE lane=?", (lane,)).fetchone()["n"]
        return {"lane": lane, "total": total, "items": [view(dict(r)) for r in rows]}

    @app.get("/api/holds")
    def holds(limit: int = 200):
        rows = C().conn.execute("SELECT * FROM accounts WHERE lane IS NULL ORDER BY state, account_ref LIMIT ?", (limit,))
        total = C().conn.execute("SELECT COUNT(*) n FROM accounts WHERE lane IS NULL").fetchone()["n"]
        return {"total": total, "items": [view(dict(r)) for r in rows]}

    @app.get("/api/accounts/{ref}")
    def account(ref: str):
        c = C().conn
        r = c.execute("SELECT * FROM accounts WHERE account_ref=?", (ref,)).fetchone()
        if not r:
            raise HTTPException(404, "account not found")
        return {
            "account": view(dict(r), full=True),
            "timeline": [{**dict(e), "detail": json.loads(e["detail_json"])} for e in
                         c.execute("SELECT id,ts,kind,detail_json,actor,source,rule_version FROM audit_events WHERE account_ref=? ORDER BY id", (ref,))],
            "links": [dict(l) for l in c.execute("SELECT * FROM payment_links WHERE account_ref=? ORDER BY created_at", (ref,))],
            "contacts": [{**{k: x[k] for k in ("id", "ts", "language", "mode", "disclosure_given", "transcript", "transcript_source",
                                                "intent", "confidence", "evidence", "outcome")},
                          "script": json.loads(x["script_json"]) if x["script_json"] else None, "has_audio": bool(x["audio_path"])}
                         for x in c.execute("SELECT * FROM contacts WHERE account_ref=? ORDER BY id", (ref,))],
        }

    @app.get("/api/contacts/{contact_id}/audio")
    def contact_audio(contact_id: int):
        r = C().conn.execute("SELECT audio_path FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if not r or not r["audio_path"] or not Path(r["audio_path"]).exists():
            raise HTTPException(404, "no audio")
        return FileResponse(r["audio_path"], media_type="audio/wav")

    @app.post("/api/accounts/{ref}/contact")
    def do_contact(ref: str):
        return contact.start_contact(C(), ref)

    @app.post("/api/accounts/{ref}/reply")
    async def do_reply(ref: str, request: Request):
        body = await request.json()
        text = (body.get("transcript") or "").strip()
        if not text:
            raise HTTPException(400, "transcript is empty")
        return contact.record_reply(C(), ref, text, body.get("source", "typed"))

    @app.post("/api/accounts/{ref}/reply-audio")
    async def do_reply_audio(ref: str, file: UploadFile = File(...)):
        return contact.transcribe_reply(C(), ref, await file.read(), file.filename or "reply.wav")

    @app.post("/api/accounts/{ref}/link")
    def do_link(ref: str):
        return payments.create_link_for_account(C(), ref, "manager", "reminder link sent by the manager")

    @app.post("/api/accounts/{ref}/review")
    async def do_review(ref: str, request: Request):
        body = await request.json()
        return contact.review(C(), ref, body.get("action", ""), body.get("note", ""))

    @app.post("/api/links/{link_id}/check")
    def check_link(link_id: str):
        return payments.reconcile_link(C(), link_id)

    @app.post("/api/links/check-all")
    def check_all():
        return {"results": payments.reconcile_all(C())}

    @app.post("/api/mock/pay")
    async def mock_pay(request: Request):
        """MOCK ONLY. Simulates the borrower paying a mock link, then reads status the normal way."""
        if C().cashfree.mode != "mock":
            raise HTTPException(409, "Not in mock mode. Pay the real sandbox link, then press Check status.")
        body = await request.json()
        try:
            if body.get("expire"):
                C().cashfree.simulate_expire(body["link_id"])
            else:
                C().cashfree.simulate_payment(body["link_id"], float(body["amount"]))
        except Exception as e:
            raise HTTPException(400, str(e))
        return payments.reconcile_link(C(), body["link_id"], "mock")

    @app.post("/api/webhooks/cashfree")
    async def cashfree_webhook(request: Request):
        raw = await request.body()
        code, res = payments.handle_webhook(C(), raw, request.headers.get("x-webhook-timestamp"),
                                            request.headers.get("x-webhook-signature"))
        return JSONResponse(res, status_code=code)

    @app.post("/api/relay/callback")
    async def relay_callback(request: Request):
        """Relay's HTTP node posts the call outcome here: {account_ref, transcript}."""
        body = await request.json()
        ref, text = body.get("account_ref"), (body.get("transcript") or "").strip()
        if not ref or not text:
            raise HTTPException(400, "account_ref and transcript are required")
        return contact.record_reply(C(), ref, text, "relay_callback")

    @app.post("/api/clock")
    async def clock(request: Request):
        body = await request.json()
        c, s = C().conn, C().settings
        if body.get("reset"):
            db.set_clock(c, None)
        elif body.get("advance_days") is not None:
            db.advance_clock(c, int(float(body["advance_days"]) * 1440))
        elif body.get("set_time"):
            h, m = body["set_time"].split(":")
            db.set_clock(c, db.now(c, s.timezone).replace(hour=int(h), minute=int(m), second=0), s.timezone)
        now = db.now(c, s.timezone)
        db.audit(c, "clock_changed", {"now": now.isoformat(), "simulated": db.clock_is_simulated(c)}, actor="manager", source="human")
        q = queue.reform(c, s, "clock changed (simulated)" if db.clock_is_simulated(c) else "clock reset to real time")
        return {"now": now.isoformat(), "simulated": db.clock_is_simulated(c), "queue": q}

    @app.get("/api/changes")
    def changes(limit: int = 40):
        return {"items": [dict(r) for r in C().conn.execute("SELECT * FROM queue_changes ORDER BY id DESC LIMIT ?", (limit,))]}

    @app.get("/api/evaluation")
    def evaluation(book: str = "holdout"):
        if book not in ("holdout", "main", "curveball"):
            raise HTTPException(404, "unknown book")
        if ("eval", book) not in _cache:
            from eval import evaluate as ev
            _cache[("eval", book)] = ev.evaluate(book)
        return _cache[("eval", book)]

    @app.get("/api/fairness")
    def fairness():
        if "fair" not in _cache:
            from eval import fairness as fz
            _cache["fair"] = fz.run_all()
        return _cache["fair"]

    @app.get("/api/formula")
    def formula():
        return {"rule_version": ranker.RULE_VERSION,
                "note": "Points set calling order. They are not a prediction that anyone will pay.",
                "terms": [
                    {"term": "Verified payment record", "max": ranker.MAX_RECORD, "rule": "instalments paid / instalments due; unknown scores half"},
                    {"term": "Promise follow-through", "max": ranker.MAX_PROMISE, "rule": "promises kept / promises made; none scores half"},
                    {"term": "Timing fit", "max": ranker.MAX_TIMING, "rule": "usual or stated pay date close to today"},
                    {"term": "Delinquency stage", "max": ranker.MAX_STAGE, "rule": "1-30 days full, 31-60 70%, 61-90 45%, over 90 25%"},
                    {"term": "Amount at stake", "max": ranker.MAX_AMOUNT, "rule": "log10 of confirmed balance, full at 1 lakh"},
                    {"term": "Contact fatigue", "max": 0, "rule": f"minus {ranker.FATIGUE_PER_ATTEMPT} per attempt in 7 days, minus {ranker.FATIGUE_NO_ANSWER} more if the last was unanswered"}],
                "inputs_allowed": list(ranker.ALLOWLIST), "inputs_never_used": list(ranker.BANNED),
                "lanes": {"reminder_only": "paid at least 85% of 4+ instalments, 10 days overdue or less, no recent attempts",
                          "call_on_date": "borrower named a future pay date", "call_now": "everyone else who passes the gates"}}

    @app.get("/api/export.csv", response_class=PlainTextResponse)
    def export():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["position", "account_ref", "lane", "state", "points", "reason_or_hold", "next_action", "rule_version"])
        rows = C().conn.execute("SELECT * FROM accounts ORDER BY position IS NULL, position, lane IS NULL, lane, account_ref")
        for r in rows:
            w.writerow([r["position"] or "", r["account_ref"], r["lane"] or "", r["state"], r["points"] if r["points"] is not None else "",
                        r["reason"] or r["hold_reason"] or "", r["next_action"] or "", ranker.RULE_VERSION])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=kramsetu_ranked.csv"})

    @app.get("/api/scenarios")
    def list_scenarios():
        return {"items": scenarios.LIST}

    @app.post("/api/scenarios/{name}")
    def run_scenario(name: str):
        if name not in scenarios.RUN:
            raise HTTPException(404, "unknown scenario")
        _cache.clear()
        return scenarios.RUN[name](C())

    @app.post("/api/reset")
    def reset():
        db.set_clock(C().conn, None)
        return import_sample("demo")

    return app


app = create_app()
