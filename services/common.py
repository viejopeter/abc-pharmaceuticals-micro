import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("DEMO_DATA_DIR", str(ROOT / ".runtime"))).resolve()
BASE_PORT = int(os.environ.get("DEMO_BASE_PORT", "8100"))
TOKEN = os.environ.get("DEMO_SERVICE_TOKEN", "")
FRESHNESS = float(os.environ.get("DEMO_FRESHNESS_SECONDS", "30"))
HEARTBEAT = float(os.environ.get("DEMO_HEARTBEAT_SECONDS", "3"))
SERVICES = {
    "legacy": {"port": BASE_PORT + 1, "name": "Legacy stock application"},
    "broker": {"port": BASE_PORT + 2, "name": "Durable event queue"},
    "worker": {"port": BASE_PORT + 3, "name": "Synchronisation worker"},
    "enquiry": {"port": BASE_PORT + 4, "name": "Inventory Enquiry API"},
}
PRODUCTS = [
    {"sku": "AMX-500", "name": "Amoxicillin 500 mg", "category": "Antibiotic", "quantity": 120, "unit": "packs"},
    {"sku": "PAR-500", "name": "Paracetamol 500 mg", "category": "Analgesic", "quantity": 340, "unit": "packs"},
    {"sku": "IBU-200", "name": "Ibuprofen 200 mg", "category": "Anti-inflammatory", "quantity": 85, "unit": "packs"},
    {"sku": "CET-010", "name": "Cetirizine 10 mg", "category": "Antihistamine", "quantity": 0, "unit": "packs"},
    {"sku": "MET-500", "name": "Metformin 500 mg", "category": "Antidiabetic", "quantity": 210, "unit": "packs"},
    {"sku": "SAL-100", "name": "Salbutamol 100 mcg", "category": "Respiratory", "quantity": 46, "unit": "inhalers"},
]


def url(service: str) -> str:
    return f"http://127.0.0.1:{SERVICES[service]['port']}"


def headers() -> dict:
    return {"X-Service-Key": TOKEN}


def authorised(x_service_key: str | None = Header(default=None)):
    if not TOKEN or not secrets.compare_digest(x_service_key or "", TOKEN):
        raise HTTPException(401, "A valid local service key is required.")


@contextmanager
def connect(name: str, readonly: bool = False):
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / f"{name}.sqlite3"
    db = sqlite3.connect(f"file:{path}?mode=ro" if readonly else str(path),
                         uri=readonly, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    if readonly:
        db.execute("PRAGMA query_only=ON")
    else:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
    try:
        with db:
            yield db
    finally:
        db.close()


def inventory_schema(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS inventory (
        sku TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK(quantity >= 0), unit TEXT NOT NULL,
        version INTEGER NOT NULL)""")
    db.executemany("INSERT OR IGNORE INTO inventory VALUES (:sku,:name,:category,:quantity,:unit,1)", PRODUCTS)


def inventory(db: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in db.execute("SELECT * FROM inventory ORDER BY name")]


class InventoryEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=80)
    sequence: int = Field(ge=1)
    kind: Literal["InventoryChanged", "SourceCheckpoint"]
    source_at: float = Field(gt=0)
    sku: str | None = Field(default=None, max_length=50)
    quantity: int | None = Field(default=None, ge=0, le=1_000_000)
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def stock_fields(self):
        if self.kind == "InventoryChanged" and any(v is None for v in (self.sku, self.quantity, self.version)):
            raise ValueError("Stock events need SKU, quantity and version.")
        return self


def event_id() -> str:
    return str(uuid.uuid4())


def record(service: str, action: str, message: str, *, edge: str | None = None,
           correlation: str | None = None, details: dict | None = None):
    import json
    with connect(f"{service}-telemetry") as db:
        db.execute("""CREATE TABLE IF NOT EXISTS logs (
            id TEXT PRIMARY KEY, at REAL, service TEXT, action TEXT,
            message TEXT, edge TEXT, correlation TEXT, details TEXT)""")
        db.execute("INSERT INTO logs VALUES (?,?,?,?,?,?,?,?)",
                   (event_id(), time.time(), service, action, message, edge,
                    correlation, json.dumps(details or {})))
        db.execute("DELETE FROM logs WHERE id IN (SELECT id FROM logs ORDER BY at DESC LIMIT -1 OFFSET 400)")


def logs(service: str) -> list[dict]:
    import json
    try:
        with connect(f"{service}-telemetry", readonly=True) as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM logs ORDER BY at DESC LIMIT 45")]
            for row in rows:
                row["details"] = json.loads(row["details"])
            return rows
    except sqlite3.OperationalError:
        return []


def health(service: str) -> dict:
    return {"service": service, "pid": os.getpid(), "port": SERVICES[service]["port"], "status": "running"}
