"""Validate a running demo and save measured HTTP evidence.

Run `python validate.py` while the dashboard is running. This changes AMX-500,
stops and restarts the sync worker, then restores the original stock quantity.
Record versions and event history advance during the checks and restoration.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


TESTS = [
    ("stock_update", "Stock update synchronises"),
    ("zero_stock", "Zero stock is a verified business result"),
    ("interruption", "Worker interruption withholds stale stock"),
    ("recovery", "Worker restart recovers the stock update"),
    ("duplicate", "Duplicate event has no second stock effect"),
    ("access_control", "Missing service key is denied"),
]


class Validation:
    def __init__(self, base_url, timeout):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}))
        self.evidence = []
        self.results = []
        self.cleanup = []
        self.original_quantity = None
        self.changed_stock = False
        self.worker_stopped = False
        self.sku = "AMX-500"

    def request(self, path, method="GET", body=None):
        started = time.perf_counter()
        request = Request(
            self.base_url + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=method,
            headers={"Content-Type": "application/json", "X-Demo-Control": "local"},
        )
        try:
            response = self.opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        except (URLError, TimeoutError, OSError) as error:
            self.evidence.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "method": method, "path": path, "error": str(error),
            })
            raise
        with response:
            status = response.code
            raw = response.read().decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"raw_response": raw}
        self.evidence.append({
            "at": datetime.now(timezone.utc).isoformat(),
            "method": method, "path": path, "request_body": body,
            "http_status": status, "response": data,
            "round_trip_ms": round((time.perf_counter() - started) * 1000, 3),
        })
        return status, data

    def state(self):
        status, data = self.request("/api/state")
        assert status == 200, f"State endpoint returned {status}"
        return data

    def wait_for(self, read, matches, description, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            value = read()
            if matches(value):
                return value
            if time.monotonic() >= deadline:
                raise AssertionError(f"Timed out waiting for {description}")
            time.sleep(0.5)

    def enquiry(self):
        return self.request(f"/api/enquiry/{self.sku}")

    def verified_stock(self, quantity, version):
        def matches(reply):
            status, data = reply
            stock = data.get("stock") or {}
            return (status == 200 and data.get("status") == "verified"
                    and stock.get("quantity") == quantity
                    and stock.get("version") == version
                    and data.get("verification", {}).get("verified") is True)

        return self.wait_for(self.enquiry, matches, "matching verified stock")

    def write_stock(self, quantity):
        # A timeout can happen after a write commits. Restoration still runs.
        self.changed_stock = True
        status, data = self.request(
            f"/api/legacy/inventory/{self.sku}", "PUT", {"quantity": quantity})
        assert status == 200, f"Stock update returned {status}"
        assert data.get("status") == "committed", "Stock update was not committed"
        return data["event"]

    def control_worker(self, action):
        status, data = self.request(f"/api/control/worker/{action}", "POST")
        assert status == 200 and data.get("accepted"), f"Worker {action} failed"

    def preflight(self):
        state = self.state()
        assert state.get("mode") == "local educational prototype", "Expected the local demo"
        for name in ("legacy", "broker", "worker", "enquiry"):
            service = state["services"][name]
            assert service.get("reachable") and service.get("status") == "running", (
                f"Start a healthy {name} before validation")
        stock = next(item for item in state["services"]["legacy"]["items"]
                     if item["sku"] == self.sku)
        self.original_quantity = stock["quantity"]
        self.verified_stock(stock["quantity"], stock["version"])
        return state["freshness_seconds"]

    def run(self):
        freshness = self.preflight()
        baseline_quantity = 81 if self.original_quantity == 80 else 80
        current_id = TESTS[0][0]
        try:
            event = self.write_stock(baseline_quantity)
            _, data = self.verified_stock(baseline_quantity, event["version"])
            self.passed(current_id, f"HTTP 200; quantity {baseline_quantity}; version {event['version']}")

            current_id = "zero_stock"
            event = self.write_stock(0)
            self.verified_stock(0, event["version"])
            self.passed(current_id, f"HTTP 200; quantity 0; version {event['version']}")

            current_id = "interruption"
            self.worker_stopped = True
            self.control_worker("stop")
            self.wait_for(self.state, lambda s: not s["services"]["worker"]["reachable"],
                          "worker to stop")
            event = self.write_stock(baseline_quantity + 1)

            def stale(reply):
                status, result = reply
                verification = result.get("verification") or {}
                return (status == 503 and result.get("status") == "unavailable"
                        and result.get("stock", "missing") is None
                        and verification.get("reason") == "stale_feed"
                        and verification.get("feed_age", 0) > freshness)

            _, data = self.wait_for(self.enquiry, stale, "HTTP 503 for stale stock", freshness + 20)
            feed_age = data["verification"]["feed_age"]
            self.passed(current_id, f"Write committed while worker stopped; HTTP 503; stock null; feed age {feed_age:.3f} s")

            current_id = "recovery"
            recovery_started = time.perf_counter()
            self.control_worker("start")
            self.verified_stock(event["quantity"], event["version"])
            recovery_seconds = time.perf_counter() - recovery_started
            self.worker_stopped = False
            self.passed(current_id, f"HTTP 200; quantity {event['quantity']}; version {event['version']}; recovered in {recovery_seconds:.3f} s",
                        recovery_seconds=round(recovery_seconds, 3))

            current_id = "duplicate"
            self.wait_for(self.state, lambda s: s["services"]["broker"]["pending"] == 0
                          and s["services"]["broker"]["leased"] == 0,
                          "stock event acknowledgement")
            before = self.state()["services"]["worker"]["duplicates"]
            status, replay = self.request("/api/demo/replay", "POST")
            assert status == 200, f"Replay returned {status}"
            assert replay["event"]["event_id"] == event["event_id"], "Unexpected event replayed"
            after_state = self.wait_for(
                self.state, lambda s: s["services"]["worker"].get("duplicates", 0) > before,
                "duplicate to be processed")
            self.verified_stock(event["quantity"], event["version"])
            after = after_state["services"]["worker"]["duplicates"]
            self.passed(current_id, f"Duplicate counter {before} -> {after}; quantity and version unchanged")

            current_id = "access_control"
            status, result = self.request("/api/demo/security-check")
            assert status == 200 and result.get("passed") is True and result.get("http_status") == 401, (
                "Missing-key request was not denied with HTTP 401")
            self.passed(current_id, "Direct backend enquiry without service key returned HTTP 401")
        except Exception as error:
            self.results.append({"id": current_id, "status": "FAIL", "observed": str(error)})
            raise
        finally:
            self.restore()

    def passed(self, test_id, observed, **metrics):
        self.results.append({"id": test_id, "status": "PASS", "observed": observed, **metrics})
        print(f"PASS {test_id}: {observed}", flush=True)

    def restore(self):
        try:
            if self.worker_stopped:
                self.control_worker("start")
                self.wait_for(self.state, lambda s: s["services"]["worker"].get("status") == "running",
                              "worker restart during cleanup")
                self.worker_stopped = False
            if self.changed_stock:
                event = self.write_stock(self.original_quantity)
                self.verified_stock(self.original_quantity, event["version"])
                self.cleanup.append(f"Restored {self.sku} quantity to {self.original_quantity}; version is now {event['version']}.")
            state = self.state()
            healthy = all(service.get("reachable") and service.get("status") == "running"
                          for service in state["services"].values())
            assert healthy, "Some components need attention after validation"
            self.cleanup.append("All four backend processes are running.")
        except Exception as error:
            self.cleanup.append(f"Cleanup needs attention: {error}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "docs" / "validation")
    parser.add_argument("--timeout", type=float, default=60,
                        help="Maximum wait per check in seconds; a test timeout, not a service agreement")
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost"} or address.path not in {"", "/"} or address.query or address.fragment or address.username:
        parser.error("Use the HTTP root URL of a local demo")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    lock = args.output_dir / ".validation.lock"
    try:
        lock_handle = lock.open("x")
    except FileExistsError:
        parser.error(f"A validation lock exists at {lock}; check for an active run")
    started = datetime.now(timezone.utc)
    run_dir = args.output_dir / started.strftime("run-%Y%m%dT%H%M%S-%fZ")
    suite = Validation(args.base_url, args.timeout)
    failure = None
    try:
        run_dir.mkdir()
        print(f"Validating {args.base_url}; evidence folder {run_dir}", flush=True)
        print("The interruption check waits for the configured freshness limit to expire.", flush=True)
        try:
            suite.run()
        except Exception as error:
            failure = str(error)
            print(f"Validation stopped: {failure}", file=sys.stderr, flush=True)
        completed = {row["id"] for row in suite.results}
        for test_id, _ in TESTS:
            if test_id not in completed:
                suite.results.append({"id": test_id, "status": "NOT RUN", "observed": "An earlier check or preflight failed."})
        clean = not any("needs attention" in line for line in suite.cleanup)
        passed = failure is None and clean and all(row["status"] == "PASS" for row in suite.results)
        payload = {
            "started_utc": started.isoformat(), "finished_utc": datetime.now(timezone.utc).isoformat(),
            "base_url": args.base_url, "sku": suite.sku, "passed": passed,
            "test_wait_timeout_seconds": args.timeout, "failure": failure,
            "results": suite.results, "cleanup": suite.cleanup, "http_evidence": suite.evidence,
        }
        (run_dir / "evidence.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        names = dict(TESTS)
        rows = ["# Prototype validation results", "", f"Run started {started.isoformat()}.", "",
                f"System tested at {args.base_url}. Overall result {'PASS' if passed else 'NEEDS ATTENTION'}.", "",
                "| Test | Result | Observed evidence |", "| --- | --- | --- |"]
        for result in suite.results:
            observed = result["observed"].replace("|", "\\|").replace("\n", " ")
            rows.append(f"| {names[result['id']]} | {result['status']} | {observed} |")
        rows.extend(["", "## Session restoration", "", *suite.cleanup, "",
                     "## Scope", "",
                     "These checks exercise the running local prototype through its HTTP interfaces. "
                     "The wait timeout is a test execution limit. Recovery time is measured from the start "
                     "request until the first matching verified reply and includes polling overhead.", "",
                     "A group member still needs to validate the dashboard wording for zero and unavailable stock. "
                     "Human usability, cloud capacity, production security and industry-standard conformance require separate evidence.", "",
                     "Full request and response evidence is saved in [evidence.json](evidence.json).", ""])
        (run_dir / "results.md").write_text("\n".join(rows), encoding="utf-8")
        print(f"Results saved to {run_dir / 'results.md'}", flush=True)
        for line in suite.cleanup:
            print(line, flush=True)
        return 0 if passed else 1
    finally:
        lock_handle.close()
        lock.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
