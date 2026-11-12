"""Opens the app in Google Chrome (or the default browser) once it is answering.

    open_app.py --if-running   open the browser only if the app is already up;
                               exit code 0 if it was, 1 if it was not
    open_app.py                wait for the app to come up, then open the browser
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

URL = "http://localhost:8501"


def is_up() -> bool:
    try:
        return urllib.request.urlopen(f"{URL}/_stcore/health", timeout=2).status == 200
    except OSError:
        return False


def open_browser() -> None:
    candidates = [
        Path(os.environ.get(variable, "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
    ]
    for chrome in candidates:
        if chrome.is_file():
            subprocess.Popen([str(chrome), URL])
            return
    webbrowser.open(URL)


def main() -> int:
    if "--if-running" in sys.argv:
        if is_up():
            open_browser()
            return 0
        return 1
    for _ in range(90):
        if is_up():
            open_browser()
            return 0
        time.sleep(1)
    print(f"The app did not start. Open {URL} yourself once it is running.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
