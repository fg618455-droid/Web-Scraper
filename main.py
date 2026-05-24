"""
Deal Scraper - Desktop App Entry Point
Starts Flask in a background thread, then opens Edge/Chrome in app mode.

Run: python main.py
"""

import json
import os
import sys
import time
import threading
import subprocess
import webbrowser

# -- Playwright browser path fix --
# When running as a PyInstaller .exe, Playwright automatically looks for
# browser binaries in _internal/play (sys._MEIPASS/play).  That folder only
# exists when the browsers were bundled at build-time (see build-scraper.bat).
# If the bundled folder is missing we fall back to the system-wide installation
# that setup.bat creates (usually %LOCALAPPDATA%\ms-playwright).
# Setting PLAYWRIGHT_BROWSERS_PATH here propagates to the Node.js subprocess
# that Playwright spawns, so it must happen before ANY playwright import.
def _has_chromium(base_dir: str) -> bool:
    """Prueft ob im Verzeichnis ein gueltiges Chromium-Binary liegt."""
    import glob
    for pattern in ["**/chrome.exe", "**/chromium.exe", "**/chrome-linux", "**/chrome"]:
        if glob.glob(os.path.join(base_dir, pattern), recursive=True):
            return True
    return False


def _fix_playwright_browser_path():
    """
    Stelle sicher dass PLAYWRIGHT_BROWSERS_PATH auf einen gueltigen Pfad zeigt
    UND dort wirklich ein Chromium-Binary liegt.

    Problem: Wenn die kompilierte .exe einmal gestartet wurde, kann
    PLAYWRIGHT_BROWSERS_PATH als Windows-Umgebungsvariable auf
    dist\\DealScraper\\_internal\\play gesetzt sein. Dieser Pfad kann
    existieren aber LEER sein (keine Browser drin). Ohne Fix schlaegt jeder
    Playwright-Aufruf fehl -- auch im Source-Mode.
    """
    current = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")

    # Wenn der aktuelle Pfad existiert UND ein Chromium-Binary enthaelt: nichts tun
    if current and os.path.isdir(current) and _has_chromium(current):
        return

    # PyInstaller .exe: gebundelte Browser in _internal/play bevorzugen
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        bundled = os.path.join(meipass, "play")
        if os.path.isdir(bundled):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bundled
            return

    # Source-Mode ODER .exe ohne gebundelte Browser:
    # Systemweit installierte Browser suchen (von setup.bat / playwright install)
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"),
        os.path.join(os.environ.get("USERPROFILE", ""),
                     "AppData", "Local", "ms-playwright"),
        # Playwright default path in AppData\Local
        os.path.join(os.environ.get("APPDATA", "").replace("Roaming", "Local"), "ms-playwright"),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate) and _has_chromium(candidate):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = candidate
            print(f"[PW-Fix] Using system Playwright at: {candidate}")
            return

    # Nichts gefunden: veraltete Env-Variable loeschen damit Playwright
    # seinen eigenen Default-Pfad verwendet (statt einem falschen)
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        print(f"[PW-Fix] No valid Chromium found, removing bad PLAYWRIGHT_BROWSERS_PATH={current!r}")
        del os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    else:
        print("[PW-Fix] No PLAYWRIGHT_BROWSERS_PATH set, Playwright will use its own default.")


_fix_playwright_browser_path()
# -- End Playwright fix --

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

# Load external config (optional - fallback to defaults)
_config_path = os.path.join(BASE_DIR, "config.json")
try:
    with open(_config_path, encoding="utf-8") as _f:
        CONFIG = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError):
    CONFIG = {}

HOST = CONFIG.get("host", "127.0.0.1")
PORT = CONFIG.get("port", 5001)

import database as db
db.init_db()

from app import app as flask_app, _load_persisted_interval
_load_persisted_interval()

URL = f"http://{HOST}:{PORT}"

BROWSER_CANDIDATES = [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
]


def _run_flask():
    flask_app.run(host=HOST, port=PORT, debug=False,
                  use_reloader=False, threaded=True)


def _find_browser():
    for path in BROWSER_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def check_updates_and_prompt():
    try:
        import tkinter as tk
        from tkinter import messagebox
        from updater import check_for_updates, download_and_update

        latest_version, release_url = check_for_updates()
        if latest_version and release_url:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)

            result = messagebox.askyesno(
                "Update verfuegbar!",
                "Eine neue Version (v{}) des Deal Scrapers ist verfuegbar."
                "\n\nMoechtest du die Release-Seite im Browser oeffnen, um"
                " den Download zu starten?".format(latest_version)
            )

            if result:
                download_and_update(release_url, latest_version)
            root.destroy()
    except Exception:
        pass


def main():
    check_updates_and_prompt()

    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()

    for _ in range(20):
        time.sleep(0.3)
        try:
            import urllib.request
            urllib.request.urlopen(URL, timeout=1)
            break
        except Exception:
            pass

    browser = _find_browser()
    if browser:
        subprocess.Popen([
            browser,
            "--app={}".format(URL),
            "--start-fullscreen",
            "--start-maximized",
            "--disable-extensions",
        ])
    else:
        webbrowser.open(URL)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
