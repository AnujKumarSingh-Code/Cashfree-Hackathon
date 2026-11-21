"""Synthetic data generator. Everything here is made up. No real customer data.

Writes:
  borrowers_main.csv            messy 4,200-row overdue book (the app imports this)
  borrowers_holdout.csv         separate batch with shifted patterns, for evaluation
  borrowers_curveball.csv       different headers, unseen values, junk
  demo_borrowers.csv            16 hand-written accounts that drive the demo scenarios
  outcomes_*_hidden.csv         ground truth. Read ONLY by eval/evaluate.py, never by the app or the ranker
  protected_attrs_test_only.csv synthetic protected attributes. Read ONLY by eval/fairness.py

Run: python -m data.generate
"""
from __future__ import annotations

import csv
import math
import random
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASOF = date(2026, 11, 21)

NAMES = {  # synthetic pools; names correlate with the synthetic attributes on purpose, to test for proxies
    ("hindu", "F"): ["Lakshmi Devi", "Sunita Sharma", "Meena Kumari", "Kavita Yadav", "Anita Patel", "Rekha Verma"],
    ("hindu", "M"): ["Ramesh Kumar", "Suresh Yadav", "Arjun Reddy", "Vijay Patil", "Manoj Gupta", "Raghu Iyer"],
    ("muslim", "F"): ["Fatima Begum", "Ayesha Khan", "Shabana Sheikh", "Nasreen Ansari"],
    ("muslim", "M"): ["Imran Khan", "Salim Sheikh", "Arif Ansari", "Yusuf Qureshi"],
    ("christian", "F"): ["Mary Thomas", "Grace D'Souza", "Annie Joseph"],
    ("christian", "M"): ["John Mathew", "Peter Fernandes", "Thomas Varghese"],
    ("sikh", "F"): ["Harpreet Kaur", "Simran Kaur"],
    ("sikh", "M"): ["Gurpreet Singh", "Manjit Singh"],
}
AREAS = [("560001", "kannada"), ("560068", "Kannada"), ("600001", "Tamil"), ("641001", "tamil"), ("110001", "Hindi"),
         ("226001", "hindi"), ("400001", "Marathi"), ("411001", "marathi"), ("500001", "Telugu"), ("700001", "Bengali"),
         ("380001", "Gujarati"), ("302001", "HINDI"), ("800001", "hi"), ("682001", "Malayalam")]
CASTES = ["general", "obc", "sc", "st"]
PAY_DAYS = [1, 2, 3, 5, 5, 5, 7, 10, 10, 15, 20, 21, 22, 23, 25, 28, 30]


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def binom(rng, n, p):
    return sum(1 for _ in range(n) if rng.random() < p)


def fmt_amount(rng, v):
    r = rng.random()
    if r < 0.55:
        return f"{v:.0f}"
    if r < 0.70:
        return f"₹ {v:,.0f}"
    if r < 0.82:
        return f"{v:,.2f}"
    if r < 0.90:
        return f"{v:.0f}/-"
    if r < 0.95 and v >= 100000:
        return f"{v / 100000:.2f} lakh"
    return f"Rs. {v:.0f}"


def fmt_date(rng, d):
    return rng.choice([d.strftime("%Y-%m-%d"), d.strftime("%d/%m/%Y"), d.strftime("%d-%b-%Y")])


def make_book(n, seed, prefix, shift=False):
    rng = random.Random(seed)
    rows, outcomes, protected = [], [], []
    for i in range(n):
        ref = f"{prefix}{rng.randint(100000, 999999)}-{i:04d}"
        religion = rng.choices(["hindu", "muslim", "christian", "sikh"], [70, 18, 7, 5])[0]
        gender = rng.choice(["F", "M"])
        caste = rng.choice(CASTES)
        name = rng.choice(NAMES[(religion, gender)])
        pin, lang = rng.choice(AREAS)
        kind = rng.choices(["normal", "serial_promiser", "quiet_payer"], [70, 15, 15])[0]
        r = rng.betavariate(1.6, 2.2) if shift else rng.betavariate(2.2, 1.8)
        if kind == "serial_promiser":
            r = clamp(r * 0.45)
        elif kind == "quiet_payer":
            r = clamp(0.55 + r * 0.45)
        due = rng.randint(3, 24)
        paid = binom(rng, due, clamp(r * 0.95 + 0.03))
        band = rng.choices([0, 1, 2, 3], [55 * (0.5 + r), 22, 12 * (1.5 - r), 11 * (1.6 - r)])[0]
        dpd = [rng.randint(1, 30), rng.randint(31, 60), rng.randint(61, 90), rng.randint(91, 240)][band]
        emi = round(rng.uniform(1500, 18000), -1)
        outstanding = round(emi * (1 + dpd // 30) * rng.uniform(1.0, 1.06), 0)
        pay_day = rng.choice(PAY_DAYS)
        if kind == "serial_promiser":
            made = rng.randint(3, 6); kept = binom(rng, made, 0.12)
        elif kind == "quiet_payer":
            made, kept = 0, 0
        else:
            made = rng.randint(0, 4); kept = binom(rng, made, clamp(r + rng.uniform(-0.15, 0.15)))
        attempts = rng.choices([0, 1, 2, 3], [60, 22, 12, 6])[0]
        last = rng.choice(["", "no_answer", "promise", "callback", ""]) if attempts else ""
        status = rng.choices(["Overdue", "overdue", "OVERDUE", "Pending", "Paid", "Current", ""], [50, 15, 8, 14, 4, 2, 7])[0]
        dnd = "Y" if rng.random() < 0.01 else rng.choice(["", "N", "no"])
        note, note_kind = "", ""
        nr = rng.random()
        if nr < 0.05:
            d = rng.choice([1, 5, 7, 10, 25, 28, 30])
            note = rng.choice([f"salary {d}th ko aayega", f"bola {d} tarikh ko de dunga", f"सैलरी {d} तारीख को आएगी"])
            note_kind = "promise"
        elif nr < 0.07:
            note = rng.choice(["branch mein de diya", "customer says already paid at branch", "कल ब्रांच में जमा कर दिया"])
            note_kind = "already_paid"
        elif nr < 0.09:
            note = rng.choice(["naukri chali gayi, paise nahi hai", "hospital mein hai, baad mein", "नौकरी चली गई"])
            note_kind = "hardship"
        elif nr < 0.10:
            note = rng.choice(["amount galat hai bol raha hai", "dispute: extra charge lagaya"])
            note_kind = "dispute"
        elif nr < 0.13:
            note = rng.choice(["phone uthaya nahi", "ghar pe nahi tha", "will revert", "rnr"])
        missing_hist = rng.random() < (0.30 if shift else 0.08)

        # ---- hidden ground truth (never visible to the app) ----
        since = (ASOF.day - pay_day) % 30
        timing = 1.0 if (since <= 3 or since >= 28) else 0.0
        stage = [0.6, 0.0, -0.5, -1.1][band]
        pk = (kept / made) if made else 0.5
        z = -2.2 + 3.2 * r + (0.6 if shift else 1.0) * timing + stage + 0.8 * (pk - 0.5) - 0.35 * attempts + rng.gauss(0, 0.6)
        p_contact = 1 / (1 + math.exp(-z))
        if note_kind in ("hardship", "dispute"):
            p_contact *= 0.1
        self_cure = rng.random() < (0.85 if (due >= 4 and paid / due >= 0.85 and dpd <= 10) else 0.08 * r)
        truly_paid = status == "Paid" or (note_kind == "already_paid" and rng.random() < 0.6)
        pays_if_contacted = (not truly_paid) and (self_cure or rng.random() < p_contact)
        recovered = 0.0
        if pays_if_contacted:
            recovered = outstanding if rng.random() < 0.8 else round(outstanding * 0.4, 0)
        outcomes.append({"account_ref": ref, "pays_if_contacted": int(pays_if_contacted), "pays_without_contact": int(self_cure and not truly_paid),
                         "already_paid": int(truly_paid), "amount_recovered_if_contacted": recovered})
        protected.append({"account_ref": ref, "gender": gender, "religion": religion, "caste_category": caste})

        row = {"Loan No": ref, "Borrower Name": name, "Mobile": f"90000{rng.randint(10000, 99999)}", "Lang": lang,
               "Pincode": pin, "DPD": str(dpd), "EMI Due Date": fmt_date(rng, ASOF - timedelta(days=dpd)),
               "Overdue Amt": fmt_amount(rng, outstanding), "EMI": f"{emi:.0f}",
               "EMIs Due": "" if missing_hist else str(due), "EMIs Paid": "" if missing_hist else str(paid),
               "Usual Pay Day": "" if rng.random() < 0.1 else str(pay_day), "PTP Made": str(made), "PTP Kept": str(kept),
               "Calls Last 7d": str(attempts), "Last Call Result": last, "DND": dnd, "Status": status, "Remarks": note}
        # deliberate mess
        m = rng.random()
        if m < 0.03:
            row["Overdue Amt"] = rng.choice(["", "NA", "-"])
        elif m < 0.05:
            row["Overdue Amt"] = rng.choice(["abc", "??", "see file", "-4500"])
        elif m < 0.06:
            row["Mobile"] = rng.choice(["", "12345", "not available"])
        elif m < 0.07:
            row["DPD"] = rng.choice(["many", "3 months"])
        rows.append(row)
        if rng.random() < 0.01:   # duplicate row, sometimes with a different balance
            dup = dict(row)
            if rng.random() < 0.5:
                dup["Overdue Amt"] = f"{outstanding + 500:.0f}"
            rows.append(dup)
    return rows, outcomes, protected


def write(path, rows, fields=None):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields or list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def make_curveball(seed=77, n=300):
    """Same kind of book, but headers and conventions the importer has not been tuned to."""
    rows, outcomes, protected = make_book(n, seed, "CB", shift=True)
    rng = random.Random(seed + 1)
    prot = {p["account_ref"]: p for p in protected}
    out = []
    for r in rows:
        p = prot[r["Loan No"]]
        last_paid = ASOF - timedelta(days=int(r["DPD"]) + 30) if r["DPD"].isdigit() else None
        row = {"LAN": r["Loan No"], "Customer": r["Borrower Name"], "Contact No": "+91 " + r["Mobile"] if r["Mobile"].isdigit() else r["Mobile"],
               "Bhasha": r["Lang"], "District": r["Pincode"], "Days Late": r["DPD"], "Bakaya": r["Overdue Amt"],
               "Paid Count": r["EMIs Paid"], "EMIs Billed": r["EMIs Due"],
               "Last Paid On": last_paid.strftime("%d.%m.%Y") if last_paid else "", "Promise Count": r["PTP Made"],
               "PTP Honoured": r["PTP Kept"], "Attempts": r["Calls Last 7d"], "Disposition": r["Last Call Result"],
               "Loan Status": r["Status"], "Agent Notes": r["Remarks"], "Caste": p["caste_category"],
               "Religion": p["religion"], "Gender": p["gender"], "Branch Manager": rng.choice(["A", "B", "C"]),
               "Risk Grade (old)": rng.choice(["A1", "B2", "C3", ""])}
        x = rng.random()
        if x < 0.04:
            row["Loan Status"] = rng.choice(["W/O", "Legal", "Sub-standard?"])      # unseen statuses
        elif x < 0.07:
            row["Agent Notes"] = rng.choice(["சம்பளம் 5 ம் தேதி வரும்", "வேலை போச்சு", "கட்டிட்டேன் என்று சொல்கிறார்"])
        elif x < 0.09 and row["Paid Count"].isdigit():
            row["Paid Count"] = str(int(row["Paid Count"]) + 40)                      # impossible value
        elif x < 0.11:
            row["Days Late"] = ""                                                    # must be derived or flagged
        out.append(row)
    return out, outcomes


DEMO = [
    # ref, name, mobile, lang, pin, dpd, amount, emi, due, paid, payday, ptp_made, ptp_kept, calls, last, dnd, status, remarks
    ("KS-1001", "Lakshmi Devi", "9000010001", "Hindi", "226001", 12, "8,400", 8400, 12, 11, 21, 2, 2, 0, "", "", "Overdue", ""),
    ("KS-1002", "Ravi Kumar", "9000010002", "Hindi", "110001", 18, "6200", 6200, 10, 8, 22, 1, 1, 0, "", "", "Overdue", ""),
    ("KS-1003", "Meena Kumari", "9000010003", "Hindi", "302001", 35, "₹ 15,000", 7500, 14, 10, 20, 2, 1, 0, "", "", "Overdue", ""),
    ("KS-1004", "Suresh Yadav", "9000010004", "Hindi", "800001", 22, "5400/-", 5400, 9, 6, 5, 1, 0, 0, "", "", "Pending", ""),
    ("KS-1005", "Fatima Begum", "9000010005", "Hindi", "226001", 41, "9800", 4900, 11, 8, 10, 1, 1, 0, "", "", "Overdue", ""),
    ("KS-1006", "Arjun Reddy", "9000010006", "Tamil", "600001", 27, "7300", 7300, 8, 6, 23, 0, 0, 0, "", "", "Overdue", ""),
    ("KS-1007", "Anita Patel", "9000010007", "Gujarati", "380001", 4, "3100", 3100, 12, 11, 15, 0, 0, 0, "", "", "Overdue", ""),
    ("KS-1008", "Imran Khan", "9000010008", "Hindi", "110001", 15, "4600", 4600, 10, 7, 5, 1, 1, 0, "", "", "Overdue", "salary 5th ko aayega"),
    ("KS-1009", "Mary Thomas", "9000010009", "English", "682001", 20, "NA", 5200, 9, 7, 7, 0, 0, 0, "", "", "Overdue", ""),
    ("KS-1010", "Vijay Patil", "9000010010", "Marathi", "411001", 9, "4100", 4100, 12, 12, 1, 0, 0, 0, "", "", "Paid", ""),
    ("KS-1011", "Harpreet Kaur", "9000010011", "Hindi", "110001", 30, "6600", 6600, 10, 6, 10, 1, 0, 0, "", "Y", "Overdue", ""),
    ("KS-1012", "Manoj Gupta", "9000010012", "Hindi", "226001", 45, "12,300", 6150, 12, 7, 15, 3, 1, 3, "no_answer", "", "Overdue", ""),
    ("KS-1013", "Salim Sheikh", "9000010013", "Hindi", "400001", 95, "1.10 lakh", 9000, 20, 6, 2, 5, 0, 1, "no_answer", "", "Overdue", ""),
    ("KS-1014", "Rekha Verma", "9000010014", "Hindi", "302001", 14, "5000", 5000, 8, 7, 20, 0, 0, 0, "", "", "Overdue", "branch mein de diya"),
    # fairness pair: identical permitted fields, different name, area and language
    ("KS-1015", "Sunita Sharma", "9000010015", "Hindi", "226001", 25, "7000", 7000, 10, 7, 21, 2, 1, 0, "", "", "Overdue", ""),
    ("KS-1016", "Ayesha Khan", "9000010016", "Tamil", "600001", 25, "7000", 7000, 10, 7, 21, 2, 1, 0, "", "", "Overdue", ""),
]
DEMO_FIELDS = ["Loan No", "Borrower Name", "Mobile", "Lang", "Pincode", "DPD", "Overdue Amt", "EMI", "EMIs Due", "EMIs Paid",
               "Usual Pay Day", "PTP Made", "PTP Kept", "Calls Last 7d", "Last Call Result", "DND", "Status", "Remarks"]


def main():
    rows, outcomes, protected = make_book(4200, 2026, "LN")
    write(HERE / "borrowers_main.csv", rows)
    write(HERE / "outcomes_main_hidden.csv", outcomes)
    write(HERE / "protected_attrs_test_only.csv", protected)
    rows, outcomes, protected_h = make_book(4200, 4242, "HO", shift=True)
    write(HERE / "borrowers_holdout.csv", rows)
    write(HERE / "outcomes_holdout_hidden.csv", outcomes)
    with open(HERE / "protected_attrs_test_only.csv", "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=list(protected_h[0].keys())).writerows(protected_h)
    cb, cb_out = make_curveball()
    write(HERE / "borrowers_curveball.csv", cb)
    write(HERE / "outcomes_curveball_hidden.csv", cb_out)
    write(HERE / "demo_borrowers.csv", [dict(zip(DEMO_FIELDS, map(str, d))) for d in DEMO], DEMO_FIELDS)
    print("wrote synthetic files to", HERE)


if __name__ == "__main__":
    main()
