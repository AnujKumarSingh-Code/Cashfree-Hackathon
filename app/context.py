"""One object that carries the database, settings and adapters through a request."""
from __future__ import annotations

from dataclasses import dataclass

from . import db
from .adapters import cashfree as cf, sarvam as sv
from .config import Settings, get_settings


@dataclass
class Ctx:
    conn: object
    settings: Settings
    cashfree: object
    sarvam: object


def make_ctx(settings: Settings | None = None, db_path: str | None = None) -> Ctx:
    s = settings or get_settings()
    conn = db.connect(db_path or s.db_path)
    return Ctx(conn, s, cf.get_adapter(s, conn), sv.get_adapter(s))
