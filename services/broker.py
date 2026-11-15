"""Educational durable HTTP queue: persistence, leases, ACK, retries and DLQ.

Infrastructure, NOT a business microservice or a production message broker.
"""
import json
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from services.common import InventoryEvent, authorised, connect, event_id, health, logs, record

LEASE_SECONDS = 8
MAX_ATTEMPTS = 5


def initialise():
    with connect("broker") as db:
        db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_key TEXT UNIQUE,
            payload TEXT NOT NULL, status TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0, available_at REAL DEFAULT 0,
            lease_until REAL, receipt TEXT, created_at REAL, error TEXT)""")


def publish(event: dict, delivery_key: str) -> dict:
    with connect("broker") as db:
        cursor = db.execute("""INSERT OR IGNORE INTO messages
            (delivery_key,payload,created_at) VALUES (?,?,?)""",
                            (delivery_key, json.dumps(event), time.time()))
        inserted = cursor.rowcount > 0
        row = db.execute("SELECT id FROM messages WHERE delivery_key=?", (delivery_key,)).fetchone()
    if inserted:
        record("broker", "enqueued", f"{event['kind']} #{event['sequence']} persisted in queue",
               correlation=event["event_id"], details={"delivery_id": row["id"], **event})
    return {"id": row["id"], "duplicate_delivery": not inserted}


def claim() -> dict | None:
    now = time.time()
    with connect("broker") as db:
        db.execute("BEGIN IMMEDIATE")
        # The oldest outstanding delivery blocks newer deliveries while leased.
        # One worker is deliberate: the demo uses one globally sequenced stream.
        row = db.execute("SELECT * FROM messages WHERE status IN ('pending','leased') ORDER BY id LIMIT 1").fetchone()
        if not row or row["available_at"] > now or (row["status"] == "leased" and row["lease_until"] > now):
            return None
        receipt = event_id()
        db.execute("UPDATE messages SET status='leased',attempts=attempts+1,lease_until=?,receipt=? WHERE id=?",
                   (now + LEASE_SECONDS, receipt, row["id"]))
        result = {"id": row["id"], "receipt": receipt, "attempt": row["attempts"] + 1,
                  "event": json.loads(row["payload"])}
    event = result["event"]
    record("broker", "delivered", f"Delivered event #{event['sequence']} · attempt {result['attempt']}",
           edge="queue-worker", correlation=event["event_id"], details=result)
    return result


def settle(message_id: int, receipt: str, error: str | None = None):
    with connect("broker") as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row or row["status"] != "leased" or row["receipt"] != receipt or row["lease_until"] < time.time():
            raise HTTPException(409, "Invalid or expired delivery receipt.")
        status = "acked" if error is None else ("dead" if row["attempts"] >= MAX_ATTEMPTS else "pending")
        db.execute("UPDATE messages SET status=?,available_at=?,lease_until=NULL,receipt=NULL,error=? WHERE id=?",
                   (status, time.time() + (.8 if error else 0), error, message_id))
    if error:
        record("broker", "dead_letter" if status == "dead" else "retry",
               f"Event delivery {message_id}: {error}", details={"status": status})
    return {"status": status}


@asynccontextmanager
async def lifespan(app):
    initialise()
    record("broker", "started", "Durable local queue started. Unacknowledged deliveries survive restarts.")
    yield


app = FastAPI(title="ABC Local Demonstration Queue", lifespan=lifespan,
              dependencies=[Depends(authorised)])


class Publish(BaseModel):
    event: InventoryEvent
    delivery_key: str = Field(min_length=1, max_length=100)


class Receipt(BaseModel):
    receipt: str
    error: str | None = Field(default=None, max_length=500)


@app.get("/health")
def get_health():
    return health("broker")


@app.post("/messages")
def post_message(body: Publish):
    return publish(body.event.model_dump(), body.delivery_key)


@app.post("/messages/claim")
def claim_message():
    return {"delivery": claim()}


@app.post("/messages/{message_id}/ack")
def ack(message_id: int, body: Receipt):
    return settle(message_id, body.receipt)


@app.post("/messages/{message_id}/nack")
def nack(message_id: int, body: Receipt):
    return settle(message_id, body.receipt, body.error or "Processing failed")


@app.post("/demo/replay")
def replay():
    with connect("broker") as db:
        row = db.execute("""SELECT payload FROM messages WHERE status='acked'
            AND json_extract(payload,'$.kind')='InventoryChanged' ORDER BY id DESC LIMIT 1""").fetchone()
    if not row:
        raise HTTPException(409, "First update stock and wait for synchronisation, then replay that event.")
    event = json.loads(row["payload"])
    result = publish(event, "replay-" + event_id())
    record("broker", "replayed", f"Replayed the SAME domain event #{event['sequence']} with a new transport delivery",
           correlation=event["event_id"], details=event)
    return {**result, "event": event}


@app.get("/diagnostics")
def diagnostics():
    with connect("broker") as db:
        counts = {r["status"]: r["n"] for r in db.execute("SELECT status,COUNT(*) n FROM messages GROUP BY status")}
        rows = db.execute("""SELECT id,payload,status,attempts,error FROM messages
            ORDER BY id DESC LIMIT 10""").fetchall()
        recent = [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
    return {**health("broker"), "pending": counts.get("pending", 0), "leased": counts.get("leased", 0),
            "acked": counts.get("acked", 0), "dead": counts.get("dead", 0),
            "recent": recent, "logs": logs("broker")}
