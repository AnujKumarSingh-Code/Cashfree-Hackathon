"""Configuration. Values come from the environment or a local .env file.

Secrets are never logged or returned by any endpoint; only their presence is reported.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    p = path or ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    cashfree_client_id: str
    cashfree_client_secret: str
    cashfree_env: str
    cashfree_api_version: str
    sarvam_api_key: str
    sarvam_stt_model: str
    sarvam_tts_model: str
    sarvam_tts_speaker: str
    relay_webhook_url: str
    public_base_url: str
    window_start: str
    window_end: str
    timezone: str
    max_attempts_per_week: int
    daily_call_capacity: int
    exploration_share: float
    lender_name: str
    grievance_contact: str
    force_mock: bool
    db_path: str
    demo_call_number: str = ""
    min_part_share: float = 0.2
    link_expiry_days: int = 7
    intent_confidence_floor: float = 0.7

    @property
    def cashfree_mode(self) -> str:
        if self.force_mock or not (self.cashfree_client_id and self.cashfree_client_secret):
            return "mock"
        if self.cashfree_env.lower() != "sandbox":
            return "mock"   # this prototype never talks to production
        return "live"

    @property
    def sarvam_mode(self) -> str:
        return "mock" if (self.force_mock or not self.sarvam_api_key) else "live"

    @property
    def relay_mode(self) -> str:
        # a live call needs both the Relay URL and one approved demo number; synthetic numbers are never dialled
        return "simulated" if (self.force_mock or not self.relay_webhook_url or not self.demo_call_number) else "relay"

    @property
    def cashfree_base_url(self) -> str:
        return "https://sandbox.cashfree.com/pg"


def get_settings() -> Settings:
    load_dotenv()
    e = os.environ.get
    return Settings(
        cashfree_client_id=e("CASHFREE_CLIENT_ID", ""),
        cashfree_client_secret=e("CASHFREE_CLIENT_SECRET", ""),
        cashfree_env=e("CASHFREE_ENV", "sandbox"),
        cashfree_api_version=e("CASHFREE_API_VERSION", "2026-01-01"),
        sarvam_api_key=e("SARVAM_API_KEY", ""),
        sarvam_stt_model=e("SARVAM_STT_MODEL", "saaras:v4"),
        sarvam_tts_model=e("SARVAM_TTS_MODEL", "bulbul:v3"),
        sarvam_tts_speaker=e("SARVAM_TTS_SPEAKER", "shubh"),
        relay_webhook_url=e("RELAY_WEBHOOK_URL", ""),
        public_base_url=e("PUBLIC_BASE_URL", "").rstrip("/"),
        window_start=e("CONTACT_WINDOW_START", "08:00"),
        window_end=e("CONTACT_WINDOW_END", "19:00"),
        timezone=e("CONTACT_TIMEZONE", "Asia/Kolkata"),
        max_attempts_per_week=int(e("MAX_ATTEMPTS_PER_WEEK", "3")),
        daily_call_capacity=int(e("DAILY_CALL_CAPACITY", "360")),
        exploration_share=float(e("EXPLORATION_SHARE", "0.10")),
        lender_name=e("LENDER_NAME", "Kramsetu Demo Finance"),
        grievance_contact=e("GRIEVANCE_OFFICER_CONTACT",
                            "Grievance Officer, 1800-000-0000, grievance@example.invalid"),
        force_mock=e("KRAMSETU_FORCE_MOCK", "0") in ("1", "true", "yes"),
        db_path=e("KRAMSETU_DB", str(ROOT / "kramsetu.db")),
        demo_call_number=re.sub(r"\D", "", e("DEMO_CALL_NUMBER", ""))[-10:],
    )
