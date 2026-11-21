"""Import a messy borrower file. Normalize what is clear, flag what is not. Never guess.

Sensitive columns (gender, religion, caste, community) are dropped at the door and never stored.
"""
from __future__ import annotations

import difflib
import io
import json
import re
from datetime import date, datetime

import pandas as pd

from . import db, signals

ALIASES: dict[str, list[str]] = {
    "account_ref": ["account_ref", "loan_no", "loan_number", "loan_id", "loan_account", "account", "account_no",
                    "account_number", "acct", "acct_no", "lan", "loan", "id", "customer_loan_id"],
    "name": ["name", "borrower", "borrower_name", "customer", "customer_name", "cust_name", "applicant"],
    "phone": ["phone", "mobile", "mobile_no", "mobile_number", "contact", "contact_no", "phone_no", "phone_number", "mob"],
    "language": ["language", "lang", "preferred_language", "pref_lang", "bhasha"],
    "area": ["area", "pincode", "pin", "pin_code", "city", "location", "district", "branch", "village", "town", "state"],
    "days_overdue": ["days_overdue", "dpd", "days_past_due", "overdue_days", "days_late", "delay_days"],
    "due_date": ["due_date", "emi_due_date", "due_dt", "due_on", "instalment_due_date", "installment_due_date"],
    "outstanding": ["outstanding", "overdue_amount", "overdue_amt", "amount_due", "amt_due", "balance", "pending_amount",
                    "due_amount", "overdue", "total_due", "outstanding_amount", "bakaya"],
    "emi_amount": ["emi_amount", "emi", "instalment_amount", "installment_amount", "emi_amt"],
    "instalments_due": ["instalments_due", "installments_due", "emis_due", "emi_due_count", "total_emis_due", "emis_billed"],
    "instalments_paid": ["instalments_paid", "installments_paid", "emis_paid", "emi_paid_count", "paid_count", "emis_cleared"],
    "usual_pay_day": ["usual_pay_day", "usual_payment_day", "typical_pay_day", "pay_day", "payment_day"],
    "last_payment_date": ["last_payment_date", "last_paid_on", "last_payment", "last_paid_date", "last_pmt_dt"],
    "promises_made": ["promises_made", "ptp_made", "ptp_count", "promise_count"],
    "promises_kept": ["promises_kept", "ptp_kept", "promise_kept", "ptp_honoured", "ptp_honored"],
    "attempts_7d": ["attempts_7d", "calls_last_7d", "attempts_last_week", "contact_attempts", "calls_7d", "attempts"],
    "last_contact_outcome": ["last_contact_outcome", "last_call_result", "disposition", "last_disposition", "call_outcome"],
    "dnc": ["dnc", "do_not_call", "do_not_contact", "dnd"],
    "status": ["status", "payment_status", "account_status", "loan_status"],
    "note": ["note", "notes", "field_note", "field_notes", "remarks", "remark", "agent_notes", "comments", "comment"],
}
SENSITIVE = {"gender", "sex", "religion", "caste", "community", "category", "sub_caste", "jati", "dharm"}
_MISSING = {"", "na", "n/a", "-", "--", "null", "none", "nan", "?", "tbd", "nil", "not available"}
LANG_MAP = {"hi": "hi-IN", "hindi": "hi-IN", "hi-in": "hi-IN", "ta": "ta-IN", "tamil": "ta-IN", "ta-in": "ta-IN",
            "kn": "kn-IN", "kannada": "kn-IN", "kn-in": "kn-IN", "te": "te-IN", "telugu": "te-IN", "te-in": "te-IN",
            "mr": "mr-IN", "marathi": "mr-IN", "mr-in": "mr-IN", "bn": "bn-IN", "bengali": "bn-IN", "bangla": "bn-IN",
            "bn-in": "bn-IN", "gu": "gu-IN", "gujarati": "gu-IN", "gu-in": "gu-IN", "ml": "ml-IN", "malayalam": "ml-IN",
            "ml-in": "ml-IN", "pa": "pa-IN", "punjabi": "pa-IN", "pa-in": "pa-IN", "od": "od-IN", "odia": "od-IN",
            "or": "od-IN", "od-in": "od-IN", "en": "en-IN", "english": "en-IN", "en-in": "en-IN",
            "हिंदी": "hi-IN", "हिन्दी": "hi-IN", "தமிழ்": "ta-IN", "ಕನ್ನಡ": "kn-IN"}
_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%d/%m/%y",
                 "%Y/%m/%d", "%d %B %Y", "%d-%B-%Y"]
_UNITS = {"lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "l": 1e5, "k": 1e3, "thousand": 1e3, "hazaar": 1e3,
          "hazar": 1e3, "crore": 1e7, "cr": 1e7}
_ALIAS_INDEX = {a: canon for canon, als in ALIASES.items() for a in als}


def norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(h).strip().lower()).strip("_")


def map_headers(headers: list[str]) -> tuple[dict[str, str], list[str], list[str], list[str]]:
    """Returns (column -> canonical field, unmapped columns, dropped sensitive columns, mapping notes)."""
    mapping, unmapped, dropped, notes = {}, [], [], []
    taken: set[str] = set()
    for h in headers:
        n = norm_header(h)
        if n in SENSITIVE:
            dropped.append(h)
            continue
        canon = _ALIAS_INDEX.get(n)
        if canon is None:
            close = difflib.get_close_matches(n, list(_ALIAS_INDEX), n=1, cutoff=0.86)
            if close:
                canon = _ALIAS_INDEX[close[0]]
                notes.append(f"column '{h}' matched to '{canon}' by similarity to '{close[0]}'")
        if canon is None or canon in taken:
            unmapped.append(h)
            continue
        taken.add(canon)
        mapping[h] = canon
    return mapping, unmapped, dropped, notes


def is_missing(v) -> bool:
    return v is None or str(v).strip().lower() in _MISSING


def parse_amount(raw) -> tuple[float | None, str | None]:
    if is_missing(raw):
        return None, "balance missing"
    s = str(raw).strip().lower()
    s = s.replace("₹", "").replace("inr", "").replace("rs.", "").replace("rs", "").replace("/-", "").strip()
    m = re.fullmatch(r"([\d,]*\.?\d+)\s*([a-z]+)?", s)
    if not m or (m.group(2) and m.group(2) not in _UNITS):
        return None, f"balance unreadable: '{raw}'"
    num = float(m.group(1).replace(",", "")) * _UNITS.get(m.group(2) or "", 1)
    return round(num, 2), None


def parse_date(raw) -> tuple[date | None, str | None]:
    if is_missing(raw):
        return None, None
    s = str(raw).strip()
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date(), None
        except ValueError:
            continue
    return None, f"date unreadable: '{raw}'"


def parse_int(raw) -> tuple[int | None, bool]:
    """Returns (value, unreadable)."""
    if is_missing(raw):
        return None, False
    s = str(raw).strip().replace(",", "")
    try:
        f = float(s)
        return (int(f), False) if f == int(f) else (None, True)
    except ValueError:
        return None, True


def parse_bool(raw) -> bool:
    return str(raw).strip().lower() in ("1", "y", "yes", "true", "t", "dnc", "dnd", "haan")


def norm_status(raw) -> str | None:
    if is_missing(raw):
        return None
    s = str(raw).strip().lower()
    if any(w in s for w in ("paid", "closed", "settled", "cleared", "foreclosed")) and "unpaid" not in s \
            and "not paid" not in s and "part" not in s:
        return "paid"
    if any(w in s for w in ("overdue", "pending", "npa", "default", "delinquent", "unpaid", "not paid", "due", "part")):
        if "not due" in s or "no due" in s:
            return "not_overdue"
        return "overdue"
    if any(w in s for w in ("current", "regular", "standard", "active")):
        return "not_overdue"
    return None


def norm_phone(raw) -> str | None:
    if is_missing(raw):
        return None
    d = re.sub(r"\D", "", str(raw))
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    if len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d if len(d) == 10 else None


def read_table(content: bytes | str, filename: str = "") -> pd.DataFrame:
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig", errors="replace")
    else:
        text = content
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, sep=None, engine="python")


def normalize_row(raw: dict, mapping: dict[str, str], row_index: int, today: date) -> dict:
    """One file row -> account dict with flags (critical, hold for review) and notes (informational)."""
    v = {canon: raw.get(col) for col, canon in mapping.items()}
    flags: list[str] = []
    notes: list[str] = []
    acc: dict = {"row_index": row_index}

    ref = None if is_missing(v.get("account_ref")) else re.sub(r"\s+", "", str(v["account_ref"]).strip())
    if not ref:
        ref = f"ROW-{row_index + 1}"
        flags.append("account reference missing; a placeholder was assigned")
    acc["account_ref"] = ref
    acc["name"] = None if is_missing(v.get("name")) else str(v["name"]).strip()
    acc["area"] = None if is_missing(v.get("area")) else str(v["area"]).strip()

    acc["phone"] = norm_phone(v.get("phone"))
    if acc["phone"] is None:
        flags.append("phone number missing or unreadable; cannot be contacted")

    lang_raw = None if is_missing(v.get("language")) else str(v["language"]).strip()
    acc["language"] = LANG_MAP.get(lang_raw.lower(), None) if lang_raw else None
    if acc["language"] is None:
        notes.append("language not given or not recognised; Hindi will be used for contact")

    amt, why = parse_amount(v.get("outstanding"))
    acc["outstanding"] = amt
    if why:
        flags.append(why)
    emi, _ = parse_amount(v.get("emi_amount")) if not is_missing(v.get("emi_amount")) else (None, None)
    acc["emi_amount"] = emi

    due, why = parse_date(v.get("due_date"))
    if why:
        notes.append("due " + why)
    acc["due_date"] = due.isoformat() if due else None
    dpd, bad = parse_int(v.get("days_overdue"))
    if bad:
        flags.append(f"days overdue unreadable: '{v.get('days_overdue')}'")
    if dpd is None and not bad and due is not None:
        dpd = (today - due).days
        notes.append("days overdue computed from the due date")
    acc["days_overdue"] = dpd
    acc["dpd_asof"] = today.isoformat()

    acc["status"] = norm_status(v.get("status"))
    if acc["status"] is None and not is_missing(v.get("status")):
        flags.append(f"payment status not recognised: '{v.get('status')}'")
    if acc["status"] is None and dpd is None and not bad:
        flags.append("cannot confirm the account is overdue: no status and no days overdue")
    if acc["status"] == "paid" and dpd is not None and dpd > 0 and amt:
        flags.append("status says paid but a balance and days overdue are present")

    for key in ("instalments_due", "instalments_paid", "promises_made", "promises_kept", "attempts_7d", "usual_pay_day"):
        val, bad = parse_int(v.get(key))
        if bad:
            notes.append(f"{key.replace('_', ' ')} unreadable; treated as not known")
        acc[key] = val
    if acc["instalments_due"] is not None and acc["instalments_paid"] is not None \
            and acc["instalments_paid"] > acc["instalments_due"]:
        notes.append("instalments paid exceeds instalments due; payment record treated as not known")
        acc["instalments_due"] = acc["instalments_paid"] = None
    if acc["promises_made"] is not None and acc["promises_kept"] is not None \
            and acc["promises_kept"] > acc["promises_made"]:
        notes.append("promises kept exceeds promises made; promise record treated as not known")
        acc["promises_made"] = acc["promises_kept"] = None
    if acc["usual_pay_day"] is not None and not 1 <= acc["usual_pay_day"] <= 31:
        acc["usual_pay_day"] = None
    if acc["usual_pay_day"] is None:
        lp, _ = parse_date(v.get("last_payment_date"))
        if lp:
            acc["usual_pay_day"] = lp.day
            notes.append(f"usual pay day taken from the last payment date ({lp.day})")
    acc["attempts_7d"] = acc["attempts_7d"] or 0
    acc["last_contact_outcome"] = None if is_missing(v.get("last_contact_outcome")) \
        else norm_header(v["last_contact_outcome"])
    acc["dnc"] = 1 if parse_bool(v.get("dnc")) else 0
    acc["note"] = None if is_missing(v.get("note")) else str(v["note"]).strip()

    # signals buried in the field note (evidence only)
    acc["stated_pay_date"] = None
    acc["note_signal"] = None
    if acc["note"]:
        sig = signals.extract(acc["note"], today)
        if sig.intent not in ("none", "unclear"):
            acc["note_signal"] = sig.to_dict()
            if sig.pay_date and sig.intent in ("promise", "part_pay"):
                acc["stated_pay_date"] = sig.pay_date
        elif sig.intent == "unclear":
            notes.append("field note present but not understood; left for a person to read")
    acc["flags"], acc["notes"] = flags, notes
    return acc


def import_file(conn, content: bytes | str, filename: str, batch: str | None = None, replace: bool = True) -> dict:
    today = db.now(conn).date()
    df = read_table(content, filename)
    mapping, unmapped, dropped, map_notes = map_headers(list(df.columns))
    batch = batch or re.sub(r"[^a-zA-Z0-9_-]", "_", filename or "upload")
    if replace:
        # accounts and working tables are replaced; the audit log is kept (append-only)
        for t in ("accounts", "payment_links", "payment_events", "contacts", "queue_changes", "mock_cashfree_links"):
            conn.execute(f"DELETE FROM {t}")
    seen: dict[str, dict] = {}
    duplicates = 0
    rows = df.to_dict(orient="records")
    for i, raw in enumerate(rows):
        safe_raw = {k: val for k, val in raw.items() if norm_header(k) not in SENSITIVE}
        acc = normalize_row(safe_raw, mapping, i, today)
        acc["original"] = safe_raw
        ref = acc["account_ref"]
        if ref in seen:
            duplicates += 1
            first = seen[ref]
            if first["original"] != safe_raw:
                msg = "duplicate rows in the file with different values"
                if msg not in first["flags"]:
                    first["flags"].append(msg)
            continue
        seen[ref] = acc
    flagged = 0
    ts = db.now(conn).isoformat()
    conn.execute("BEGIN")
    for acc in seen.values():
        state = "needs_review" if acc["flags"] else "eligible"
        hold = None
        sig = acc.get("note_signal")
        if acc["flags"]:
            flagged += 1
            hold = "Held for review: " + "; ".join(acc["flags"])
        elif sig:
            # holds implied by the field note; a person or a Cashfree check resolves them
            m = {"already_paid": ("claim_under_check", "Field note says already paid; needs verification before any contact"),
                 "hardship": ("hardship_hold", "Field note reports hardship; support path, no pressure"),
                 "dispute": ("disputed", "Field note reports a dispute; routed to review"),
                 "do_not_contact": ("do_not_contact", "Field note says do not contact")}
            if sig["intent"] in m:
                state, hold = m[sig["intent"]]
                hold += f" (note: \"{sig['evidence']}\")"
        conn.execute(
            """INSERT OR REPLACE INTO accounts(account_ref,batch,row_index,original_json,name,phone,language,area,
               days_overdue,dpd_asof,due_date,outstanding,emi_amount,instalments_due,instalments_paid,usual_pay_day,
               promises_made,promises_kept,attempts_7d,last_contact_outcome,dnc,status,note,stated_pay_date,
               promise_open,state,hold_reason,flags_json,notes_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (acc["account_ref"], batch, acc["row_index"], json.dumps(acc["original"], ensure_ascii=False),
             acc["name"], acc["phone"], acc["language"], acc["area"], acc["days_overdue"], acc["dpd_asof"],
             acc["due_date"], acc["outstanding"], acc["emi_amount"], acc["instalments_due"], acc["instalments_paid"],
             acc["usual_pay_day"], acc["promises_made"], acc["promises_kept"], acc["attempts_7d"],
             acc["last_contact_outcome"], acc["dnc"], acc["status"], acc["note"], acc["stated_pay_date"],
             1 if acc["stated_pay_date"] else 0, state, hold,
             json.dumps(acc["flags"], ensure_ascii=False), json.dumps(acc["notes"], ensure_ascii=False), ts))
        db.audit(conn, "imported", {"batch": batch, "row": acc["row_index"] + 1, "flags": acc["flags"],
                                    "notes": acc["notes"], "note_signal": sig}, acc["account_ref"])
    summary = {"batch": batch, "filename": filename, "rows": len(rows), "stored": len(seen), "flagged": flagged,
               "duplicates": duplicates, "mapping": mapping, "mapping_notes": map_notes, "unmapped": unmapped,
               "dropped_sensitive": dropped}
    conn.execute("INSERT OR REPLACE INTO import_batches VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (batch, filename, ts, len(rows), len(seen), flagged, duplicates,
                  json.dumps({"mapping": mapping, "notes": map_notes}), json.dumps(unmapped), json.dumps(dropped)))
    db.audit(conn, "file_imported", summary)
    conn.execute("COMMIT")
    return summary
