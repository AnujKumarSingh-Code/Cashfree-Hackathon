"""Live smoke test. Run on a machine that can reach Cashfree and Sarvam, with keys in .env.

    python scripts/smoke_live.py

It makes real SANDBOX calls and prints PASS / FAIL / SKIP per step. It never prints a key.
Nothing here moves real money: the Cashfree adapter refuses any environment except sandbox.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters import cashfree as cf, sarvam as sv  # noqa: E402
from app.config import get_settings  # noqa: E402

results = []


def step(name, fn, skip_reason=None):
    if skip_reason:
        results.append((name, "SKIP", skip_reason))
        print(f"SKIP  {name}: {skip_reason}")
        return None
    try:
        out = fn()
        results.append((name, "PASS", ""))
        print(f"PASS  {name}: {out}")
        return out
    except Exception as e:  # noqa: BLE001
        results.append((name, "FAIL", str(e)))
        print(f"FAIL  {name}: {e}")
        return None


def main():
    s = get_settings()
    print("Configured (presence only):", {"CASHFREE_CLIENT_ID": bool(s.cashfree_client_id),
          "CASHFREE_CLIENT_SECRET": bool(s.cashfree_client_secret), "SARVAM_API_KEY": bool(s.sarvam_api_key),
          "RELAY_WEBHOOK_URL": bool(s.relay_webhook_url), "DEMO_CALL_NUMBER": bool(s.demo_call_number)})
    print("Cashfree mode:", s.cashfree_mode, "| base:", s.cashfree_base_url, "| api version:", s.cashfree_api_version)

    no_cf = None if s.cashfree_mode == "live" else "Cashfree test keys not set (or KRAMSETU_FORCE_MOCK=1)"
    link_id = f"ks_smoke_{int(time.time())}"
    c = None if no_cf else cf.CashfreeLive(s.cashfree_client_id, s.cashfree_client_secret, s.cashfree_base_url, s.cashfree_api_version)
    created = step("Cashfree: create payment link with part payment",
                   lambda: (lambda r: f"{r['link_id']} status={r['link_status']} url={r['link_url']}")(
                       c.create_link(link_id, 500, "Kramsetu smoke test", {"phone": "9999999999", "name": "Smoke Test"},
                                     True, 100, None, {"app": "kramsetu", "kind": "smoke"})), no_cf)
    step("Cashfree: create again with the same link_id is safe (idempotent or rejected, never a second link)",
         lambda: _second_create(c, link_id), no_cf or (None if created else "first create failed"))
    step("Cashfree: fetch link status",
         lambda: (lambda r: f"status={r['link_status']} paid={r['link_amount_paid']}")(c.get_link(link_id)),
         no_cf or (None if created else "first create failed"))
    step("Cashfree: orders for the link", lambda: f"{len(c.get_link_orders(link_id))} order(s)",
         no_cf or (None if created else "first create failed"))
    step("Cashfree: cancel the smoke link",
         lambda: f"status={c.cancel_link(link_id)['link_status']}", no_cf or (None if created else "first create failed"))

    no_sv = None if s.sarvam_mode == "live" else "SARVAM_API_KEY not set (or KRAMSETU_FORCE_MOCK=1)"
    v = None if no_sv else sv.SarvamLive(s.sarvam_api_key, s.sarvam_stt_model, s.sarvam_tts_model, s.sarvam_tts_speaker)
    audio = step("Sarvam: Bulbul text to speech (Hindi)",
                 lambda: _tts(v), no_sv)
    step("Sarvam: Saaras speech to text on that audio (round trip)",
         lambda: (lambda r: f"transcript='{r['transcript']}' lang={r['language_code']}")(
             v.transcribe(Path("smoke_tts.wav").read_bytes(), "smoke_tts.wav", mode="codemix")),
         no_sv or (None if audio else "TTS failed"))

    print("\nSummary:", ", ".join(f"{n.split(':')[0]} {st}" for n, st, _ in results))
    print("Relay is not exercised here: trigger it from the dashboard with RELAY_WEBHOOK_URL and DEMO_CALL_NUMBER set.")
    return 1 if any(st == "FAIL" for _, st, _ in results) else 0


def _second_create(c, link_id):
    try:
        r = c.create_link(link_id, 500, "Kramsetu smoke test", {"phone": "9999999999", "name": "Smoke Test"}, True, 100, None)
        return f"replayed, same link_id={r['link_id']}"
    except cf.CashfreeError as e:
        return f"rejected as expected ({e})"


def _tts(v):
    audio = v.speak("नमस्ते। यह कॉल रिकॉर्ड की जा रही है। salary 5 tarikh ko aayegi.", "hi-IN")
    Path("smoke_tts.wav").write_bytes(audio)
    return f"{len(audio)} bytes written to smoke_tts.wav"


if __name__ == "__main__":
    raise SystemExit(main())
