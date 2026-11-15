"""Authoritative stock owner. Stock, stream sequence and outbox commit atomically."""
import asyncio
import json
import time
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from services.common import (HEARTBEAT, InventoryEvent, authorised, connect, event_id,
                             headers, health, inventory, inventory_schema, logs, record, url)


def initialise():
    with connect("legacy") as db:
        inventory_schema(db)
        db.execute("CREATE TABLE IF NOT EXISTS stream (id INTEGER PRIMARY KEY CHECK(id=1), sequence INTEGER)")
        db.execute("INSERT OR IGNORE INTO stream VALUES (1,0)")
        db.execute("""CREATE TABLE IF NOT EXISTS outbox (
            sequence INTEGER PRIMARY KEY, event_id TEXT UNIQUE, payload TEXT, published INTEGER DEFAULT 0)""")


def commit_event(sku: str | None = None, quantity: int | None = None) -> dict:
    with connect("legacy") as db:
        db.execute("BEGIN IMMEDIATE")
        version = None
        if sku is not None:
            row = db.execute("SELECT * FROM inventory WHERE sku=?", (sku,)).fetchone()
            if row is None:
                raise HTTPException(404, "Unknown medicine SKU.")
            version = row["version"] + 1
            db.execute("UPDATE inventory SET quantity=?,version=? WHERE sku=?", (quantity, version, sku))
        seq = db.execute("SELECT sequence FROM stream WHERE id=1").fetchone()[0] + 1
        event = InventoryEvent(event_id=event_id(), sequence=seq,
                               kind="InventoryChanged" if sku else "SourceCheckpoint",
                               source_at=time.time(), sku=sku, quantity=quantity, version=version).model_dump()
        db.execute("UPDATE stream SET sequence=? WHERE id=1", (seq,))
        db.execute("INSERT INTO outbox (sequence,event_id,payload) VALUES (?,?,?)",
                   (seq, event["event_id"], json.dumps(event)))
    record("legacy", "stock_committed" if sku else "checkpoint_created",
           f"{sku}: authoritative stock set to {quantity} · version {version}" if sku else f"Source checkpoint #{seq} committed",
           edge="legacy-db" if sku else None, correlation=event["event_id"], details=event)
    return event


async def heartbeat_loop():
    while True:
        commit_event()
        await asyncio.sleep(HEARTBEAT)


async def publish_loop():
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        while True:
            with connect("legacy") as db:
                row = db.execute("SELECT * FROM outbox WHERE published=0 ORDER BY sequence LIMIT 1").fetchone()
            if not row:
                await asyncio.sleep(.15)
                continue
            payload = json.loads(row["payload"])
            try:
                response = await client.post(url("broker") + "/messages", headers=headers(),
                                             json={"event": payload, "delivery_key": payload["event_id"]})
                response.raise_for_status()
                with connect("legacy") as db:
                    db.execute("UPDATE outbox SET published=1 WHERE sequence=?", (row["sequence"],))
                record("legacy", "event_published", f"{payload['kind']} #{row['sequence']} durably accepted by queue",
                       edge="legacy-queue", correlation=payload["event_id"], details=payload)
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(.7)


@asynccontextmanager
async def lifespan(app):
    initialise()
    record("legacy", "started", "Legacy application started. Stock writes remain authoritative here.")
    tasks = [asyncio.create_task(heartbeat_loop()), asyncio.create_task(publish_loop())]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="ABC Legacy Stock Application", lifespan=lifespan,
              dependencies=[Depends(authorised)])


class StockUpdate(BaseModel):
    quantity: int = Field(ge=0, le=1_000_000)


@app.get("/health")
def get_health():
    return health("legacy")


@app.get("/inventory")
def get_inventory():
    with connect("legacy") as db:
        return {"items": inventory(db)}


@app.put("/inventory/{sku}")
def update_stock(sku: str, update: StockUpdate):
    return {"status": "committed", "event": commit_event(sku, update.quantity)}


@app.get("/diagnostics")
def diagnostics():
    with connect("legacy") as db:
        return {**health("legacy"), "items": inventory(db),
                "sequence": db.execute("SELECT sequence FROM stream").fetchone()[0],
                "outbox_pending": db.execute("SELECT COUNT(*) FROM outbox WHERE published=0").fetchone()[0],
                "logs": logs("legacy")}
