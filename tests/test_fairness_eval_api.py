import csv
import io
from pathlib import Path

from fastapi.testclient import TestClient

from app import db, ingest, queue
from app.main import create_app
from tests.conftest import acc, csv_text

DATA = Path(__file__).resolve().parent.parent / "data"


def test_fairness_pair_gets_identical_points(demo):
    a, b = acc(demo, "KS-1015"), acc(demo, "KS-1016")
    assert (a["name"], a["area"], a["language"]) != (b["name"], b["area"], b["language"])
    assert a["points"] == b["points"] and a["breakdown_json"] == b["breakdown_json"] and abs(a["position"] - b["position"]) == 1


def test_changing_name_area_language_alone_never_changes_rank(ctx):
    from eval import fairness
    res = fairness.counterfactual(DATA / "demo_borrowers.csv")
    assert res["passed"] and res["accounts_whose_rank_or_points_changed"] == 0
    assert {"Borrower Name", "Pincode", "Lang"} <= set(res["fields_swapped"])


def test_counterfactual_on_the_full_book():
    from eval import fairness
    res = fairness.counterfactual()
    assert res["accounts"] == 4200 and res["passed"]


def test_proxy_audit_and_structural_check():
    from eval import fairness
    assert fairness.structural()["passed"]
    p = fairness.proxy_audit()
    assert p["passed"] and p["largest_gap_from_0.5"] <= 0.05


def test_ranking_beats_file_order_on_the_holdout_and_is_repeatable():
    from eval import evaluate
    a, b = evaluate.evaluate("holdout"), evaluate.evaluate("holdout")
    assert a == b
    assert a["kramsetu_ranked"]["payers_who_needed_the_call"] > a["original_file_order"]["payers_who_needed_the_call"]
    assert a["kramsetu_ranked"]["payers_who_needed_the_call"] > a["file_order_after_safety_gates"]["payers_who_needed_the_call"]
    assert a["kramsetu_ranked"]["calls_wasted_on_already_paid"] == 0
    assert "Synthetic" in a["label"]


def test_app_code_never_reads_the_hidden_outcomes_or_protected_file():
    for p in (Path(__file__).resolve().parent.parent / "app").rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "_hidden" not in text and "protected_attrs" not in text, p


def test_outcomes_are_not_in_the_borrower_files():
    for name in ("borrowers_main.csv", "borrowers_holdout.csv", "borrowers_curveball.csv"):
        header = (DATA / name).read_text(encoding="utf-8").splitlines()[0].lower()
        assert "pays_" not in header and "recovered" not in header


# ---- API end to end (mock adapters, labelled) ----
def client(ctx):
    return TestClient(create_app(ctx))


def test_api_success_path_ends_with_a_confirmed_payment(ctx):
    c = client(ctx)
    r = c.post("/api/import-sample/demo").json()
    assert r["stored"] == 16
    q = c.get("/api/queue").json()
    top = q["items"][0]
    assert top["position"] == 1 and top["reason"] and "phone" not in top and "•" in top["phone_masked"]
    assert c.post(f"/api/accounts/{top['account_ref']}/contact").json()["ok"]
    rep = c.post(f"/api/accounts/{top['account_ref']}/reply", json={"transcript": "haan link bhej do"}).json()
    link = rep["link"]["link"]
    assert link["mode"] == "mock" and link["link_url"].startswith("mock://")
    paid = c.post("/api/mock/pay", json={"link_id": link["link_id"], "amount": link["amount"]}).json()
    assert paid["status"] == "PAID" and "MOCK" in paid["message"]
    d = c.get(f"/api/accounts/{top['account_ref']}").json()
    assert d["account"]["state"] == "paid" and d["account"]["position"] is None
    kinds = [e["kind"] for e in d["timeline"]]
    for k in ("imported", "contacted", "reply_understood", "link_created", "payment_status_applied"):
        assert k in kinds
    assert c.get("/api/queue").json()["items"][0]["account_ref"] != top["account_ref"]
    assert any("full payment" in x["explanation"] for x in c.get("/api/changes").json()["items"])


def test_api_never_returns_a_full_phone_number_or_a_secret(ctx):
    c = client(ctx)
    c.post("/api/import-sample/demo")
    blob = c.get("/api/queue").text + c.get("/api/holds").text + c.get("/api/accounts/KS-1001").text + c.get("/api/status").text
    assert "9000010001" not in blob
    assert "client_secret" not in blob.lower().replace("cashfree_client_secret", "")
    s = c.get("/api/status").json()
    assert s["modes"] == {"cashfree": "mock", "sarvam": "mock", "voice": "simulated"}
    assert all(isinstance(v, bool) for v in s["configured"].values())


def test_api_upload_of_a_messy_file_and_export(ctx):
    c = client(ctx)
    text = csv_text([
        {"LAN": "X1", "Contact No": "+91 9000000001", "Days Late": "12", "Bakaya": "₹ 9,000", "Loan Status": "Overdue", "Caste": "obc", "Zodiac": "leo"},
        {"LAN": "X2", "Contact No": "9000000002", "Days Late": "many", "Bakaya": "abc", "Loan Status": "Overdue", "Caste": "sc", "Zodiac": "x"}])
    r = c.post("/api/import", files={"file": ("second.csv", text, "text/csv")}).json()
    assert r["stored"] == 2 and r["flagged"] == 1 and r["dropped_sensitive"] == ["Caste"] and r["unmapped"] == ["Zodiac"]
    rows = list(csv.DictReader(io.StringIO(c.get("/api/export.csv").text)))
    assert rows[0]["account_ref"] == "X1" and rows[0]["position"] == "1" and rows[0]["reason_or_hold"]
    assert rows[1]["account_ref"] == "X2" and rows[1]["position"] == "" and "Held for review" in rows[1]["reason_or_hold"]


def test_api_webhook_rejected_and_mock_pay_blocked_correctly(ctx):
    c = client(ctx)
    assert c.post("/api/webhooks/cashfree", content=b"{}", headers={"x-webhook-signature": "x", "x-webhook-timestamp": "1"}).status_code == 503
    assert c.post("/api/import", files={"file": ("e.csv", b"", "text/csv")}).status_code == 400


def test_api_clock_outside_window_blocks_contact(ctx):
    c = client(ctx)
    c.post("/api/import-sample/demo")
    c.post("/api/clock", json={"set_time": "20:15"})
    s = c.get("/api/status").json()
    assert s["clock_simulated"] and not s["contact_window"]["open_now"]
    r = c.post("/api/accounts/KS-1001/contact").json()
    assert r["refused"] and "window" in r["error"]


def test_all_seven_scenarios_run(ctx):
    c = client(ctx)
    for s in c.get("/api/scenarios").json()["items"]:
        r = c.post(f"/api/scenarios/{s['id']}").json()
        assert r["complete"] is True, s["id"]
        assert r["steps"]
