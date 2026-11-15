"""Inventory-enquiry-owned data. Only the worker writes this read model."""
import json
import time

from services.common import InventoryEvent, connect, inventory_schema


class SequenceGap(Exception):
    pass


def initialise():
    with connect("enquiry") as db:
        inventory_schema(db)
        db.execute("""CREATE TABLE IF NOT EXISTS checkpoint (
            id INTEGER PRIMARY KEY CHECK(id=1), sequence INTEGER, source_at REAL,
            applied_at REAL, gap_expected INTEGER, gap_received INTEGER)""")
        # Deterministic, reconciled educational seed at source checkpoint zero.
        # Do not trust it until the first contiguous source checkpoint arrives.
        db.execute("INSERT OR IGNORE INTO checkpoint VALUES (1,0,NULL,NULL,NULL,NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS applied (
            event_id TEXT PRIMARY KEY, sequence INTEGER UNIQUE, payload TEXT)""")
        db.execute("CREATE TABLE IF NOT EXISTS stats (id INTEGER PRIMARY KEY, duplicates INTEGER)")
        db.execute("INSERT OR IGNORE INTO stats VALUES (1,0)")


def apply(event: dict) -> str:
    event = InventoryEvent.model_validate(event).model_dump()
    canonical = json.dumps(event, sort_keys=True)
    gap = None
    with connect("enquiry") as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT payload FROM applied WHERE event_id=?", (event["event_id"],)).fetchone()
        if previous:
            if previous["payload"] != canonical:
                raise ValueError("Event identity reused with a different payload")
            db.execute("UPDATE stats SET duplicates=duplicates+1 WHERE id=1")
            return "duplicate"
        checkpoint = db.execute("SELECT * FROM checkpoint WHERE id=1").fetchone()
        expected = checkpoint["sequence"] + 1
        if event["sequence"] != expected:
            db.execute("UPDATE checkpoint SET gap_expected=?,gap_received=? WHERE id=1",
                       (expected, event["sequence"]))
            gap = f"Expected sequence {expected}, received {event['sequence']}. Reconciliation required."
        else:
            if checkpoint["source_at"] is not None and event["source_at"] < checkpoint["source_at"]:
                raise ValueError("Source checkpoint time cannot move backwards")
            if event["kind"] == "InventoryChanged":
                row = db.execute("SELECT version FROM inventory WHERE sku=?", (event["sku"],)).fetchone()
                if not row or event["version"] != row["version"] + 1:
                    raise ValueError("Unknown SKU or non-contiguous record version")
                db.execute("UPDATE inventory SET quantity=?,version=? WHERE sku=?",
                           (event["quantity"], event["version"], event["sku"]))
            db.execute("INSERT INTO applied VALUES (?,?,?)", (event["event_id"], event["sequence"], canonical))
            # Stock and checkpoint become visible together, in one transaction.
            # An outstanding gap is cleared only when its missing range is recovered.
            clear_gap = checkpoint["gap_received"] is None or event["sequence"] >= checkpoint["gap_received"]
            db.execute("""UPDATE checkpoint SET sequence=?,source_at=?,applied_at=?,
                gap_expected=?,gap_received=? WHERE id=1""",
                       (event["sequence"], event["source_at"], time.time(),
                        None if clear_gap else checkpoint["gap_expected"],
                        None if clear_gap else checkpoint["gap_received"]))
    if gap:
        raise SequenceGap(gap)
    return "applied"


def state(db, now: float, freshness: float) -> dict:
    checkpoint = dict(db.execute("SELECT * FROM checkpoint WHERE id=1").fetchone())
    age = None if checkpoint["source_at"] is None else max(0, now - checkpoint["source_at"])
    reason = ("sequence_gap" if checkpoint["gap_expected"] is not None else
              "initialising" if age is None else "stale_feed" if age > freshness else None)
    return {**checkpoint, "feed_age": age, "threshold": freshness,
            "verified": reason is None, "reason": reason}
