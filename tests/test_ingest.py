import json
from datetime import date

import pytest

from app import ingest, queue
from tests.conftest import acc, csv_text

T = date(2026, 11, 21)


@pytest.mark.parametrize("raw,expected", [
    ("12500", 12500.0), ("₹ 12,500", 12500.0), ("1,25,000.00", 125000.0), ("4500/-", 4500.0), ("Rs. 4500", 4500.0),
    ("1.2 lakh", 120000.0), ("12k", 12000.0), ("2 lac", 200000.0)])
def test_amount_formats_are_read(raw, expected):
    assert ingest.parse_amount(raw) == (expected, None)


@pytest.mark.parametrize("raw", ["", "NA", "-", "abc", "??", "-4500", "see file", "12 bananas"])
def test_bad_amounts_are_flagged_not_guessed(raw):
    value, why = ingest.parse_amount(raw)
    assert value is None and why


@pytest.mark.parametrize("raw", ["2026-11-01", "01/11/2026", "01-Nov-2026", "01.11.2026"])
def test_date_formats(raw):
    assert ingest.parse_date(raw) == (date(2026, 11, 1), None)


def test_unreadable_date_is_reported():
    d, why = ingest.parse_date("sometime in Nov")
    assert d is None and "unreadable" in why


def test_header_aliases_unmapped_and_sensitive_columns():
    mapping, unmapped, dropped, _ = ingest.map_headers(
        ["Loan No", "Borrower Name", "Mobile", "DPD", "Overdue Amt", "Gender", "Religion", "Caste", "Remarks", "Favourite colour"])
    assert mapping["Loan No"] == "account_ref" and mapping["Overdue Amt"] == "outstanding" and mapping["DPD"] == "days_overdue"
    assert unmapped == ["Favourite colour"]
    assert dropped == ["Gender", "Religion", "Caste"]


def test_sensitive_columns_are_never_stored(ctx):
    text = csv_text([{"Loan No": "A1", "Mobile": "9000000001", "DPD": "10", "Overdue Amt": "5000", "Status": "Overdue",
                      "Religion": "x", "Caste": "y", "Gender": "F"}])
    s = ingest.import_file(ctx.conn, text, "t.csv")
    assert set(s["dropped_sensitive"]) == {"Religion", "Caste", "Gender"}
    stored = json.loads(acc(ctx, "A1")["original_json"])
    assert not {"Religion", "Caste", "Gender"} & set(stored)


def test_missing_balance_is_held_for_review_not_ranked(demo):
    a = acc(demo, "KS-1009")
    assert a["state"] == "needs_review" and a["position"] is None and a["points"] is None
    assert "balance missing" in a["hold_reason"]


def test_missing_phone_and_unknown_status_are_flagged(ctx):
    text = csv_text([
        {"Loan No": "P1", "Mobile": "12345", "DPD": "10", "Overdue Amt": "5000", "Status": "Overdue"},
        {"Loan No": "P2", "Mobile": "9000000002", "DPD": "10", "Overdue Amt": "5000", "Status": "W/O"},
        {"Loan No": "P3", "Mobile": "9000000003", "DPD": "", "Overdue Amt": "5000", "Status": ""}])
    ingest.import_file(ctx.conn, text, "t.csv")
    queue.reform(ctx.conn, ctx.settings, "t")
    assert "phone" in acc(ctx, "P1")["hold_reason"]
    assert "not recognised" in acc(ctx, "P2")["hold_reason"]
    assert "cannot confirm" in acc(ctx, "P3")["hold_reason"]
    assert all(acc(ctx, r)["position"] is None for r in ("P1", "P2", "P3"))


def test_duplicate_rows_with_different_values_are_flagged(ctx):
    base = {"Loan No": "D1", "Mobile": "9000000001", "DPD": "10", "Overdue Amt": "5000", "Status": "Overdue"}
    s = ingest.import_file(ctx.conn, csv_text([base, {**base, "Overdue Amt": "5500"}]), "t.csv")
    assert s["duplicates"] == 1 and s["stored"] == 1
    assert "duplicate rows" in acc(ctx, "D1")["hold_reason"]


def test_identical_duplicates_are_merged_without_a_flag(ctx):
    base = {"Loan No": "D2", "Mobile": "9000000001", "DPD": "10", "Overdue Amt": "5000", "Status": "Overdue"}
    s = ingest.import_file(ctx.conn, csv_text([base, base]), "t.csv")
    assert s["duplicates"] == 1 and s["flagged"] == 0


def test_field_note_signals_create_holds_or_schedule(demo):
    assert acc(demo, "KS-1014")["state"] == "claim_under_check"        # "branch mein de diya"
    a = acc(demo, "KS-1008")                                            # "salary 5th ko aayega"
    assert a["lane"] == "call_on_date" and a["stated_pay_date"] == "2026-12-05"


def test_curveball_file_imports_and_flags(ctx):
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "data" / "borrowers_curveball.csv"
    s = ingest.import_file(ctx.conn, p.read_bytes(), p.name)
    assert s["mapping"]["Bakaya"] == "outstanding" and s["mapping"]["LAN"] == "account_ref"
    assert set(s["dropped_sensitive"]) == {"Caste", "Religion", "Gender"}
    assert "Risk Grade (old)" in s["unmapped"]
    assert s["flagged"] > 0 and s["stored"] == 300
    q = queue.reform(ctx.conn, ctx.settings, "t")
    assert q["in_call_queue"] > 0
    # impossible history (paid > billed) is treated as unknown, not trusted
    bad = ctx.conn.execute("SELECT COUNT(*) n FROM accounts WHERE instalments_paid > instalments_due").fetchone()["n"]
    assert bad == 0
