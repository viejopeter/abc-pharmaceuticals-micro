"""Read-only business API. No imports from the legacy application or its data."""
import sqlite3
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from services.common import FRESHNESS, authorised, connect, event_id, health, inventory, logs, record
from services.readmodel import state


@asynccontextmanager
async def lifespan(app):
    record("enquiry", "started", "Enquiry API started. Inventory database connections are READ ONLY.")
    yield


app = FastAPI(title="ABC Inventory Enquiry Microservice", lifespan=lifespan,
              dependencies=[Depends(authorised)])


def snapshot(sku: str | None = None) -> dict:
    with connect("enquiry", readonly=True) as db:
        # Both inventory and completeness are evaluated in the same read snapshot.
        db.execute("BEGIN")
        status = state(db, time.time(), FRESHNESS)
        if not status["verified"]:
            return {"status": "unavailable", "stock": None, "verification": status,
                    "message": "Stock cannot be verified. Unavailable does NOT mean zero stock."}
        items = inventory(db)
        if sku is not None:
            match = next((item for item in items if item["sku"] == sku), None)
            if match is None:
                raise HTTPException(404, "Unknown medicine SKU.")
            return {"status": "verified", "stock": match, "verification": status}
        return {"status": "verified", "items": items, "verification": status}


@app.get("/health")
def get_health():
    return health("enquiry")


@app.get("/inventory/{sku}")
def enquire(sku: str):
    started = time.perf_counter()
    correlation = event_id()
    try:
        result = snapshot(sku)
    except sqlite3.OperationalError:
        result = {"status": "unavailable", "stock": None, "verification": None,
                  "message": "Enquiry copy is initialising or inaccessible. No unverified quantity is returned."}
    code = 200 if result["status"] == "verified" else 503
    result["request_id"] = correlation
    result["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
    record("enquiry", "query_verified" if code == 200 else "query_unavailable",
           f"{sku}: HTTP {code} · " + (f"verified stock {result['stock']['quantity']}" if code == 200 else "stock withheld"),
           edge="enquiry-db", correlation=correlation, details=result)
    return JSONResponse(result, status_code=code)


@app.get("/diagnostics")
def diagnostics():
    try:
        result = snapshot()
    except sqlite3.OperationalError:
        result = {"status": "unavailable", "verification": None}
    return {**health("enquiry"), "data": result, "logs": logs("enquiry")}
