"""SQLite storage. One local file: simple, reliable, zero setup for a demo.

audit_events is append-only: triggers reject UPDATE and DELETE.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS import_batches (
  batch TEXT PRIMARY KEY, filename TEXT, imported_at TEXT, rows INTEGER, stored INTEGER,
  flagged INTEGER, duplicates INTEGER, mapping_json TEXT, unmapped_json TEXT, dropped_sensitive_json TEXT);
CREATE TABLE IF NOT EXISTS accounts (
  account_ref TEXT PRIMARY KEY, batch TEXT, row_index INTEGER, original_json TEXT,
  name TEXT, phone TEXT, language TEXT, area TEXT,
  days_overdue INTEGER, dpd_asof TEXT, due_date TEXT, outstanding REAL, emi_amount REAL,
  instalments_due INTEGER, instalments_paid INTEGER, usual_pay_day INTEGER,
  promises_made INTEGER, promises_kept INTEGER, attempts_7d INTEGER DEFAULT 0,
  last_contact_outcome TEXT, last_contact_at TEXT, dnc INTEGER DEFAULT 0,
  status TEXT, note TEXT, stated_pay_date TEXT, promise_open INTEGER DEFAULT 0,
  state TEXT, hold_reason TEXT, flags_json TEXT DEFAULT '[]', notes_json TEXT DEFAULT '[]',
  lane TEXT, points REAL, breakdown_json TEXT, reason TEXT, position INTEGER,
  next_action TEXT, is_exploration INTEGER DEFAULT 0, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_accounts_pos ON accounts(position);
CREATE INDEX IF NOT EXISTS idx_accounts_state ON accounts(state);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, account_ref TEXT, kind TEXT NOT NULL,
  detail_json TEXT NOT NULL, actor TEXT NOT NULL, source TEXT NOT NULL, rule_version TEXT);
CREATE INDEX IF NOT EXISTS idx_audit_ref ON audit_events(account_ref);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TABLE IF NOT EXISTS payment_links (
  link_id TEXT PRIMARY KEY, account_ref TEXT NOT NULL, cf_link_id TEXT, link_url TEXT,
  amount REAL, min_part REAL, partial INTEGER, status TEXT, amount_paid REAL DEFAULT 0,
  mode TEXT, created_at TEXT, updated_at TEXT, expiry TEXT);
CREATE INDEX IF NOT EXISTS idx_links_ref ON payment_links(account_ref);
CREATE TABLE IF NOT EXISTS payment_events (
  dedupe_key TEXT PRIMARY KEY, link_id TEXT, account_ref TEXT, status TEXT, amount_paid REAL,
  source TEXT, received_at TEXT, raw_json TEXT);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_ref TEXT, ts TEXT, language TEXT, mode TEXT,
  disclosure_given INTEGER, script_json TEXT, audio_path TEXT, transcript TEXT, transcript_source TEXT,
  intent TEXT, confidence REAL, evidence TEXT, outcome TEXT, provider_ref TEXT);
CREATE TABLE IF NOT EXISTS queue_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, cause TEXT, cause_ref TEXT, account_ref TEXT,
  old_position INTEGER, new_position INTEGER, old_state TEXT, new_state TEXT, explanation TEXT);
CREATE TABLE IF NOT EXISTS mock_cashfree_links (
  link_id TEXT PRIMARY KEY, body_json TEXT, status TEXT, amount_paid REAL DEFAULT 0);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    try:   # databases created before the direct voice path
        conn.execute("ALTER TABLE contacts ADD COLUMN provider_ref TEXT")
    except sqlite3.OperationalError:
        pass
    return conn


def get_meta(conn, key: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


# ---- clock (real time plus a labelled simulated offset) -------------------
def now(conn, tz: str = "Asia/Kolkata") -> datetime:
    offset = int(get_meta(conn, "clock_offset_minutes", "0") or 0)
    return datetime.now(ZoneInfo(tz)).replace(microsecond=0) + timedelta(minutes=offset)


def clock_is_simulated(conn) -> bool:
    return int(get_meta(conn, "clock_offset_minutes", "0") or 0) != 0


def set_clock(conn, target: datetime | None, tz: str = "Asia/Kolkata") -> None:
    if target is None:
        set_meta(conn, "clock_offset_minutes", "0")
        return
    real = datetime.now(ZoneInfo(tz)).replace(microsecond=0)
    if target.tzinfo is None:
        target = target.replace(tzinfo=ZoneInfo(tz))
    set_meta(conn, "clock_offset_minutes", str(int((target - real).total_seconds() // 60)))


def advance_clock(conn, minutes: int) -> None:
    cur = int(get_meta(conn, "clock_offset_minutes", "0") or 0)
    set_meta(conn, "clock_offset_minutes", str(cur + minutes))


# ---- audit ----------------------------------------------------------------
def audit(conn, kind: str, detail: dict, account_ref: str | None = None, actor: str = "agent",
          source: str = "system", rule_version: str | None = None, ts: datetime | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_events(ts,account_ref,kind,detail_json,actor,source,rule_version) VALUES(?,?,?,?,?,?,?)",
        ((ts or now(conn)).isoformat(), account_ref, kind, json.dumps(detail, ensure_ascii=False, default=str),
         actor, source, rule_version))


def mask_phone(phone: str | None) -> str:
    if not phone:
        return ""
    d = "".join(ch for ch in str(phone) if ch.isdigit())
    return ("•" * max(len(d) - 4, 0)) + d[-4:] if len(d) >= 4 else "••••"
