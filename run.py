"""Run with `uv run python run.py`. No Docker or external accounts required."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("services.console:app", host="127.0.0.1",
                port=int(os.environ.get("DEMO_BASE_PORT", "8100")),
                log_level="warning", access_log=False)
