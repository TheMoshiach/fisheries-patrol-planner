"""Starts the app against a throwaway copy of the data, for browser checks and the demo.

Nothing done through this copy touches the real decision log or saved settings.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def running_app(port: int = 8502, token: str | None = ""):
    """Yield (url, data_root) for an app instance serving a copy of data/real.

    ``token`` replaces the API token for this instance; the default empty string
    guarantees the copy can never start a real download.
    """
    work = Path(tempfile.mkdtemp(prefix="patrol_planner_"))
    shutil.copytree(
        PROJECT / "data" / "real", work / "real",
        ignore=shutil.ignore_patterns("http_cache", "decisions"),
    )
    env = {
        **os.environ,
        "PATROL_PLANNER_DATA_ROOT": str(work),
        "PATROL_PLANNER_OVERRIDES": str(work / "overrides.json"),
    }
    if token is not None:
        env["GFW_API_TOKEN"] = token
    process = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(PROJECT / "app" / "streamlit_app.py"),
         "--server.headless", "true", "--server.port", str(port), "--browser.gatherUsageStats", "false"],
        cwd=PROJECT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://localhost:{port}"
    try:
        for _ in range(60):
            try:
                if urllib.request.urlopen(f"{url}/_stcore/health", timeout=2).status == 200:
                    break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("the app did not start")
        yield url, work
    finally:
        process.terminate()
        with contextlib.suppress(Exception):
            process.wait(timeout=10)
        shutil.rmtree(work, ignore_errors=True)
