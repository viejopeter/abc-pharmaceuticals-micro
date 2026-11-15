"""Local demonstration console and allowlisted process supervisor.

Not a production API gateway. Does not read business databases.
"""
import asyncio
import os
import secrets
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from services.common import (BASE_PORT, DATA, FRESHNESS, ROOT, SERVICES, headers, url)

PROCESSES: dict[str, subprocess.Popen] = {}
HANDLES = {}
CONTROL_LOCK = asyncio.Lock()
CLIENT: httpx.AsyncClient | None = None
STARTED = time.time()


def start(name: str):
    if name in PROCESSES and PROCESSES[name].poll() is None:
        return
    previous = HANDLES.pop(name, None)
    if previous:
        previous.close()
    handle = open(DATA / f"{name}.log", "a", buffering=1)
    HANDLES[name] = handle
    PROCESSES[name] = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", f"services.{name}:app", "--host", "127.0.0.1",
         "--port", str(SERVICES[name]["port"]), "--log-level", "warning", "--no-access-log"],
        cwd=ROOT, env=os.environ.copy(), stdout=handle, stderr=handle)


async def stop(name: str):
    process = PROCESSES.get(name)
    if process and process.poll() is None:
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 5)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)
    handle = HANDLES.pop(name, None)
    if handle:
        handle.close()


@asynccontextmanager
async def lifespan(app):
    global CLIENT
    DATA.mkdir(parents=True, exist_ok=True)
    # Refuse to take control of unrelated processes occupying demo ports.
    for spec in SERVICES.values():
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", spec["port"]))
            except OSError:
                raise RuntimeError(f"Port {spec['port']} is occupied. Set DEMO_BASE_PORT to a free five-port range.")
    os.environ["DEMO_SERVICE_TOKEN"] = os.environ.get("DEMO_SERVICE_TOKEN") or secrets.token_urlsafe(32)
    # common.TOKEN was imported before generating the local shared credential.
    from services import common
    common.TOKEN = os.environ["DEMO_SERVICE_TOKEN"]
    CLIENT = httpx.AsyncClient(timeout=2, trust_env=False)
    try:
        for name in ("broker", "worker", "legacy", "enquiry"):
            start(name)
        print(f"\n  ABC Pharmaceuticals — Microservices Demonstrator → http://127.0.0.1:{BASE_PORT}\n"
              f"  Four real child processes · local-only controls · Ctrl+C to stop\n", flush=True)
        yield
    finally:
        await asyncio.gather(*(stop(name) for name in SERVICES))
        await CLIENT.aclose()


app = FastAPI(title="ABC Pharmaceuticals — Microservices Demonstrator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.middleware("http")
async def local_only(request: Request, call_next):
    # The console deliberately exposes powerful local process controls.
    host = request.headers.get("host", "").split(":")[0]
    allowed = {"127.0.0.1", "localhost", "testserver"}
    if host not in allowed:
        return JSONResponse({"detail": "This demonstration console is localhost-only."}, status_code=403)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin not in {f"http://127.0.0.1:{BASE_PORT}", f"http://localhost:{BASE_PORT}"}:
            return JSONResponse({"detail": "Cross-origin control requests are not allowed."}, status_code=403)
        if request.headers.get("x-demo-control") != "local":
            return JSONResponse({"detail": "The local demonstration-control header is required."}, status_code=403)
    return await call_next(request)


@app.get("/")
def home():
    return FileResponse(ROOT / "web" / "index.html")


async def request_service(name: str, method: str, path: str, **kwargs):
    try:
        response = await CLIENT.request(method, url(name) + path, headers=headers(), **kwargs)
        return response
    except httpx.HTTPError:
        raise HTTPException(503, f"{SERVICES[name]['name']} is not reachable. Its process may be stopped.")


async def diagnostic(name: str):
    try:
        response = await CLIENT.get(url(name) + "/diagnostics", headers=headers(), timeout=.7)
        response.raise_for_status()
        return {"id": name, "reachable": True, **response.json()}
    except (httpx.HTTPError, ValueError):
        process = PROCESSES.get(name)
        running = process is not None and process.poll() is None
        return {"id": name, "reachable": False, "status": "starting" if running else "stopped",
                "port": SERVICES[name]["port"], "pid": process.pid if running else None, "logs": []}


@app.get("/api/state")
async def get_state():
    results = await asyncio.gather(*(diagnostic(name) for name in SERVICES))
    all_logs = sorted([log for result in results for log in result.get("logs", [])],
                      key=lambda log: log["at"], reverse=True)[:100]
    return {"services": {result["id"]: result for result in results}, "logs": all_logs,
            "freshness_seconds": FRESHNESS, "base_port": BASE_PORT, "started_at": STARTED,
            "mode": "local educational prototype",
            "architecture": "One extracted business microservice; API and worker share its owned read model."}


class Update(BaseModel):
    quantity: int = Field(ge=0, le=1_000_000)


@app.put("/api/legacy/inventory/{sku}")
async def update_stock(sku: str, body: Update):
    response = await request_service("legacy", "PUT", f"/inventory/{sku}", json=body.model_dump())
    return JSONResponse(response.json(), status_code=response.status_code)


@app.get("/api/enquiry/{sku}")
async def enquire(sku: str):
    response = await request_service("enquiry", "GET", f"/inventory/{sku}")
    return JSONResponse(response.json(), status_code=response.status_code)


@app.post("/api/control/{name}/{action}")
async def control(name: str, action: str):
    if name not in SERVICES or action not in {"stop", "start", "restart"}:
        raise HTTPException(404, "Only the four named demonstration processes can be controlled.")
    async with CONTROL_LOCK:
        if action in {"stop", "restart"}:
            await stop(name)
        if action in {"start", "restart"}:
            start(name)
    return {"service": name, "action": action, "accepted": True}


@app.post("/api/demo/replay")
async def replay():
    response = await request_service("broker", "POST", "/demo/replay")
    return JSONResponse(response.json(), status_code=response.status_code)


@app.get("/api/demo/security-check")
async def security_check():
    try:
        response = await CLIENT.get(url("enquiry") + "/inventory/AMX-500")
        return {"passed": response.status_code == 401, "http_status": response.status_code,
                "message": "Direct enquiry request without a service key",
                "detail": response.json()}
    except httpx.HTTPError:
        raise HTTPException(503, "Start the enquiry API before running the access-control check.")
