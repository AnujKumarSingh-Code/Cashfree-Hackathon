import dataclasses
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# tests never touch real services or a developer's .env keys
for k in ("CASHFREE_CLIENT_ID", "CASHFREE_CLIENT_SECRET", "SARVAM_API_KEY", "RELAY_WEBHOOK_URL", "DEMO_CALL_NUMBER", "PUBLIC_BASE_URL",
          "SARVAM_VOICE_API_KEY", "SARVAM_ORG_ID", "SARVAM_WORKSPACE_ID", "SARVAM_AGENT_ID", "SARVAM_CONNECTION_ID",
          "SARVAM_CALLER_NUMBER"):
    os.environ[k] = ""
os.environ["KRAMSETU_FORCE_MOCK"] = "1"

from app import db, ingest, queue  # noqa: E402
from app.adapters.cashfree import CashfreeMock  # noqa: E402
from app.adapters.sarvam import SarvamMock  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.context import Ctx  # noqa: E402

DEMO = ROOT / "data" / "demo_borrowers.csv"


@pytest.fixture
def settings():
    return dataclasses.replace(get_settings(), db_path=":memory:")


@pytest.fixture
def ctx(settings):
    conn = db.connect(":memory:")
    # fixed simulated time inside the contact window, so tests do not depend on the wall clock
    db.set_clock(conn, datetime(2026, 11, 21, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
    return Ctx(conn, settings, CashfreeMock(conn), SarvamMock())


@pytest.fixture
def demo(ctx):
    ingest.import_file(ctx.conn, DEMO.read_bytes(), DEMO.name)
    queue.reform(ctx.conn, ctx.settings, "test import")
    return ctx


def acc(ctx, ref):
    return dict(ctx.conn.execute("SELECT * FROM accounts WHERE account_ref=?", (ref,)).fetchone())


def csv_text(rows, fields=None):
    import csv, io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields or list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()
