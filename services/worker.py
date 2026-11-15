import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import Depends, FastAPI

from services import readmodel
from services.common import authorised, connect, headers, health, logs, record, url

logger = logging.getLogger(__name__)


def set_status(status: str, error: str | None = None):
    app.state.consumer_status = status
    app.state.consumer_error = error


async def log_event(action: str, message: str, **details):
    try:
        await asyncio.to_thread(record, "worker", action, message, **details)
    except sqlite3.Error:
        # Operational logging must not stop inventory processing.
        logger.warning("Could not write worker telemetry.", exc_info=True)


async def consume():
    pending_retry = False
    async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
        while True:
            delivery = None
            try:
                response = await client.post(url("broker") + "/messages/claim", headers=headers())
                response.raise_for_status()
                delivery = response.json()["delivery"]
                if delivery is None:
                    if not pending_retry:
                        set_status("running")
                    await asyncio.sleep(.15)
                    continue
                event = delivery["event"]
                # SQLite can wait for a writer lock. Keep the HTTP health server responsive.
                result = await asyncio.to_thread(readmodel.apply, event)
                await log_event("duplicate_ignored" if result == "duplicate" else "event_applied",
                       f"Duplicate #{event['sequence']} ignored · no second stock effect" if result == "duplicate"
                       else f"{event['kind']} #{event['sequence']} applied atomically with checkpoint",
                       edge="worker-db" if result != "duplicate" else None,
                       correlation=event["event_id"], details=event)
                ack = await client.post(url("broker") + f"/messages/{delivery['id']}/ack", headers=headers(),
                                        json={"receipt": delivery["receipt"]})
                ack.raise_for_status()
                pending_retry = False
                set_status("running")
            except sqlite3.Error as exc:
                pending_retry = delivery is not None or pending_retry
                set_status("retrying", str(exc))
                logger.warning("Inventory database unavailable; delivery will be retried: %s", exc)
                # Do not NACK storage outages into the dead-letter queue. The lease
                # expires and the same delivery is retried, with idempotent application.
                await asyncio.sleep(.6)
            except (ValueError, readmodel.SequenceGap) as exc:
                await log_event("processing_rejected", str(exc),
                       correlation=delivery["event"]["event_id"] if delivery else None)
                if delivery:
                    try:
                        nack = await client.post(url("broker") + f"/messages/{delivery['id']}/nack", headers=headers(),
                                                 json={"receipt": delivery["receipt"], "error": str(exc)[:500]})
                        nack.raise_for_status()
                        pending_retry = False
                        set_status("running")
                    except httpx.HTTPError as retry_error:
                        pending_retry = True
                        set_status("retrying", str(retry_error))
                await asyncio.sleep(.5)
            except httpx.HTTPError as exc:
                # A crash between apply and ACK re-delivers; apply() is idempotent.
                pending_retry = delivery is not None or pending_retry
                set_status("retrying", str(exc))
                await asyncio.sleep(.6)


def consumer_finished(task: asyncio.Task):
    if task.cancelled():
        return
    error = task.exception()
    set_status("failed", str(error) if error else "Consumer exited unexpectedly.")
    if error:
        logger.error("Synchronisation consumer failed.",
                     exc_info=(type(error), error, error.__traceback__))


@asynccontextmanager
async def lifespan(app):
    readmodel.initialise()
    set_status("starting")
    await log_event("started", "Worker started. Reads queue over HTTP; never reads the legacy database.")
    task = asyncio.create_task(consume())
    app.state.consumer_task = task
    task.add_done_callback(consumer_finished)
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="ABC Enquiry Synchronisation Worker", lifespan=lifespan,
              dependencies=[Depends(authorised)])


@app.get("/health")
def get_health():
    task = getattr(app.state, "consumer_task", None)
    status = getattr(app.state, "consumer_status", "starting")
    error = getattr(app.state, "consumer_error", None)
    if task is not None and task.done():
        status = "failed"
        error = error or "The synchronisation consumer is not running."
    return {**health("worker"), "status": status, "last_error": error}


@app.get("/diagnostics")
def diagnostics():
    result = {**get_health(), "checkpoint": None, "duplicates": None, "logs": []}
    try:
        with connect("enquiry", readonly=True) as db:
            result["checkpoint"] = dict(db.execute("SELECT * FROM checkpoint WHERE id=1").fetchone())
            result["duplicates"] = db.execute("SELECT duplicates FROM stats WHERE id=1").fetchone()[0]
    except sqlite3.Error as exc:
        if result["status"] == "running":
            result["status"] = "retrying"
        result["last_error"] = str(exc)
    with suppress(sqlite3.Error):
        result["logs"] = logs("worker")
    return result
