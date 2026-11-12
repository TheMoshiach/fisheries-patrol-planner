"""Runs the data refresh in the background so a front end can start it and watch it.

The refresh is the same ``fetch`` command the command line offers, started as a
separate process. It reports through two small files next to the data: a status
file (running, done or failed) and a log of its progress lines.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT
from .storage import data_root

STALLED_AFTER_SECONDS = 15 * 60


def status_path() -> Path:
    return data_root() / "refresh_status.json"


def log_path() -> Path:
    return data_root() / "refresh.log"


def write_status(state: str, message: str = "") -> None:
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = read_status()
    payload = {
        "state": state,
        "message": message,
        "started": previous.get("started") if state != "running" else _now(),
        "updated": _now(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_status() -> dict:
    """The last refresh: {"state": "idle" | "running" | "done" | "failed" | "stalled", ...}."""
    path = status_path()
    if not path.exists():
        return {"state": "idle"}
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "idle"}
    if status.get("state") == "running" and _seconds_since_progress() > STALLED_AFTER_SECONDS:
        status["state"] = "stalled"
        status["message"] = "The refresh stopped reporting progress. You can start it again."
    return status


def progress_lines(count: int = 8) -> list[str]:
    path = log_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [line for line in lines if line.strip()][-count:]


def start_refresh() -> bool:
    """Start a refresh unless one is already running. Returns True if it was started."""
    if read_status().get("state") == "running":
        return False
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    write_status("running", "Starting")
    handle = open(log, "a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [sys.executable, "-u", "-m", "patrol_planner", "fetch"],
        cwd=PROJECT_ROOT,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        env=_utf8_environment(),
    )
    return True


def _utf8_environment() -> dict:
    import os

    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def _seconds_since_progress() -> float:
    newest = max(
        (p.stat().st_mtime for p in (log_path(), status_path()) if p.exists()), default=time.time()
    )
    return time.time() - newest


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
